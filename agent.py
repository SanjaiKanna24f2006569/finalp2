from langgraph.graph import StateGraph, END, START
import shared_store
import time
from langchain_core.rate_limiters import InMemoryRateLimiter
from langgraph.prebuilt import ToolNode
from tools import (
    get_rendered_html, download_file, post_request,
    run_code, add_dependencies, ocr_image_tool, transcribe_audio, encode_image_to_base64
)
from typing import TypedDict, Annotated, List
from langchain_core.messages import trim_messages, HumanMessage
from langchain.chat_models import init_chat_model
from langgraph.graph.message import add_messages
import os
from dotenv import load_dotenv

load_dotenv()

EMAIL = os.getenv("EMAIL")
SECRET = os.getenv("SECRET")
SUBMIT_URL = "https://tds-llm-analysis.s-anand.net/submit"

RECURSION_LIMIT = 5000
MAX_TOKENS = 60000

MANUAL_MODE = os.getenv("MANUAL_MODE", "false").lower() == "true"

# -------------------------------------------------
# STATE
# -------------------------------------------------
class AgentState(TypedDict):
    messages: Annotated[List, add_messages]
    current_url: str


TOOLS = [
    run_code, get_rendered_html, download_file,
    post_request, add_dependencies, ocr_image_tool, transcribe_audio, encode_image_to_base64
]


# -------------------------------------------------
# LLM INIT
# -------------------------------------------------
rate_limiter = InMemoryRateLimiter(
    requests_per_second=4 / 60,
    check_every_n_seconds=1,
    max_bucket_size=4
)

llm = init_chat_model(
    model_provider="google_genai",
    model="gemini-2.5-flash",
    rate_limiter=rate_limiter
).bind_tools(TOOLS)


# -------------------------------------------------
# SYSTEM PROMPT
# -------------------------------------------------
SYSTEM_PROMPT = f"""
You are an autonomous quiz-solving agent.

Your job is to:
1. Load the quiz page from the current URL.
2. Extract instructions and solve the task.
3. Submit answers to: {SUBMIT_URL}
4. The submit payload MUST include:
   - email: {EMAIL}
   - secret: {SECRET}
   - url: <CURRENT_QUIZ_URL>
   - answer: <YOUR_ANSWER>
5. Parse the response JSON to get the next URL.
6. If "correct": true and a new "url" is provided, continue with that URL.
7. If no more URLs, output "END".

Critical Rules:
- ALWAYS submit to: {SUBMIT_URL}
- ALWAYS include the CURRENT quiz URL in the "url" field
- Extract the next URL from the response JSON "url" field
- For base64 encoding, use the "encode_image_to_base64" tool
- Never hallucinate endpoints or URLs
- Always inspect server response for next steps
"""


# -------------------------------------------------
# MANUAL ANSWER HANDLER
# -------------------------------------------------
def get_manual_answer():
    """Wait for manual answer submission via /answer endpoint"""
    print("\n================ MANUAL MODE ================")
    print("Waiting for manual answer via /answer endpoint...")
    
    timeout = 180  # 3 minutes
    start = time.time()
    
    while True:
        if time.time() - start > timeout:
            print("[MANUAL] Timeout waiting for answer. Returning None.")
            return None
            
        answer = shared_store.get_manual_answer()
        if answer is not None:
            print(f"[MANUAL] Using answer: {answer}")
            return answer
        
        time.sleep(1)


# -------------------------------------------------
# MALFORMED JSON HANDLER
# -------------------------------------------------
def handle_malformed_node(state: AgentState):
    """Handle malformed JSON from LLM"""
    print("--- DETECTED MALFORMED JSON. ASKING AGENT TO RETRY ---")
    return {
        "messages": [
            HumanMessage(content="SYSTEM ERROR: Your last tool call was Malformed (Invalid JSON). Please rewrite and try again. Ensure you escape newlines and quotes correctly.")
        ]
    }


# -------------------------------------------------
# AGENT NODE
# -------------------------------------------------
def agent_node(state: AgentState):
    current_url = state.get("current_url")
    
    # Update shared store
    if current_url:
        shared_store.set_current_url(current_url)
    
    # --- TIME TRACKING ---
    cur_time = time.time()
    prev_time = shared_store.url_time.get(current_url)
    
    if prev_time is not None:
        elapsed = cur_time - prev_time
        
        if elapsed >= 180:  # 3 minutes timeout
            print(f"⏰ Timeout exceeded ({elapsed:.1f}s) — forcing wrong answer submission")
            
            fail_instruction = f"""
            Time limit exceeded for {current_url}.
            Immediately submit a WRONG answer to {SUBMIT_URL} with:
            - email: {EMAIL}
            - secret: {SECRET}
            - url: {current_url}
            - answer: "TIMEOUT"
            """
            
            fail_msg = HumanMessage(content=fail_instruction)
            result = llm.invoke(state["messages"] + [fail_msg])
            return {"messages": [result]}
    
    # --- CONTEXT TRIMMING ---
    trimmed_messages = trim_messages(
        messages=state["messages"],
        max_tokens=MAX_TOKENS,
        strategy="last",
        include_system=True,
        start_on="human",
        token_counter=llm,
    )
    
    has_human = any(msg.type == "human" for msg in trimmed_messages)
    if not has_human and current_url:
        reminder = HumanMessage(content=f"Context trimmed. Current quiz URL: {current_url}")
        trimmed_messages.append(reminder)
    
    print(f"--- INVOKING AGENT (Context: {len(trimmed_messages)} messages) ---")
    
    # --- MANUAL VS AUTO MODE ---
    if MANUAL_MODE:
        manual_answer = get_manual_answer()
        
        if manual_answer is None:
            # Timeout in manual mode
            manual_answer = "MANUAL_TIMEOUT"
        
        manual_instruction = f"""
        Submit this answer immediately:
        - URL: {SUBMIT_URL}
        - Payload:
          {{
            "email": "{EMAIL}",
            "secret": "{SECRET}",
            "url": "{current_url}",
            "answer": "{manual_answer}"
          }}
        
        After submission, check the response JSON for the next "url" field.
        """
        
        manual_msg = HumanMessage(content=manual_instruction)
        result = llm.invoke(trimmed_messages + [manual_msg])
    else:
        result = llm.invoke(trimmed_messages)
    
    return {"messages": [result]}


# -------------------------------------------------
# URL EXTRACTION NODE
# -------------------------------------------------
def extract_next_url_node(state: AgentState):
    """
    After tools run, check if post_request returned a new URL.
    Extract it and update current_url in state.
    """
    last_message = state["messages"][-1]
    
    # Check if this is a tool response
    if hasattr(last_message, "content") and isinstance(last_message.content, str):
        content = last_message.content.lower()
        
        # Look for URL patterns in response
        import re
        url_pattern = r'https://tds-llm-analysis\.s-anand\.net/quiz-\d+'
        matches = re.findall(url_pattern, last_message.content)
        
        if matches:
            next_url = matches[0]
            print(f"🔗 Extracted next URL: {next_url}")
            
            # Update tracking
            shared_store.url_time[next_url] = time.time()
            
            return {
                "current_url": next_url,
                "messages": [
                    HumanMessage(content=f"Next quiz URL received: {next_url}. Now solve this quiz.")
                ]
            }
    
    # No new URL found
    return {}


# -------------------------------------------------
# ROUTING
# -------------------------------------------------
def route(state):
    last = state["messages"][-1]
    
    # Check for malformed calls
    if hasattr(last, "response_metadata") and "finish_reason" in last.response_metadata:
        if last.response_metadata["finish_reason"] == "MALFORMED_FUNCTION_CALL":
            return "handle_malformed"
    
    # Check for tool calls
    tool_calls = getattr(last, "tool_calls", None)
    if tool_calls:
        print("Route → tools")
        return "tools"
    
    # Check for END signal
    content = getattr(last, "content", "")
    if isinstance(content, str) and "END" in content.upper():
        return END
    
    print("Route → agent")
    return "agent"


def tools_route(state):
    """Route from tools node - check if we need to extract URL"""
    last = state["messages"][-1]
    
    # If it's a tool response, try to extract URL
    if hasattr(last, "content"):
        return "extract_url"
    
    return "agent"


# -------------------------------------------------
# GRAPH
# -------------------------------------------------
graph = StateGraph(AgentState)

# Add Nodes
graph.add_node("agent", agent_node)
graph.add_node("tools", ToolNode(TOOLS))
graph.add_node("extract_url", extract_next_url_node)
graph.add_node("handle_malformed", handle_malformed_node)

# Add Edges
graph.add_edge(START, "agent")
graph.add_edge("handle_malformed", "agent")
graph.add_edge("extract_url", "agent")

# Conditional Edges
graph.add_conditional_edges(
    "agent",
    route,
    {
        "tools": "tools",
        "agent": "agent",
        "handle_malformed": "handle_malformed",
        END: END
    }
)

graph.add_conditional_edges(
    "tools",
    tools_route,
    {
        "extract_url": "extract_url",
        "agent": "agent"
    }
)

app = graph.compile()


# -------------------------------------------------
# RUNNER
# -------------------------------------------------
def run_agent(url: str):
    """Start the agent with initial URL"""
    print(f"\n🚀 Starting agent with URL: {url}")
    
    # Initialize tracking
    shared_store.url_time[url] = time.time()
    shared_store.set_current_url(url)
    
    initial_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Start solving the quiz at: {url}"}
    ]
    
    try:
        app.invoke(
            {
                "messages": initial_messages,
                "current_url": url
            },
            config={"recursion_limit": RECURSION_LIMIT}
        )
        print("\n✅ All tasks completed successfully!")
    except Exception as e:
        print(f"\n❌ Agent error: {e}")
        raise
