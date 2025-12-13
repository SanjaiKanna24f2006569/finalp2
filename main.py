from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import requests
import time
from urllib.parse import urlencode

import shared_store

# -----------------------------------
# CONFIG
# -----------------------------------
DEFAULT_SUBMIT_URL = "https://tds-llm-analysis.s-anand.net/submit"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

START_TIME = time.time()


# -----------------------------------
# HELPERS
# -----------------------------------
def make_authorized_url(url: str, email: str, secret: str) -> str:
    """
    Append email + secret as query params to any quiz URL
    """
    params = {
        "email": email,
        "secret": secret
    }
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{urlencode(params)}"


# -----------------------------------
# HEALTH
# -----------------------------------
@app.get("/healthz")
def healthz():
    session = shared_store.get_session()
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "current_url": session["url"]
    }


# -----------------------------------
# START QUIZ SESSION
# -----------------------------------
@app.post("/solve")
async def solve(request: Request):
    """
    Start a new quiz session.

    Body:
    {
      "email": "your_email@ds.study.iitm.ac.in",
      "secret": "your_secret",
      "url": "https://tds-llm-analysis.s-anand.net/project2"
    }
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    email = data.get("email")
    secret = data.get("secret")
    url = data.get("url")

    if not email or not secret or not url:
        raise HTTPException(
            status_code=400,
            detail="Missing required fields: email, secret, url"
        )

    shared_store.set_session(email, secret, url)
    authorized_url = make_authorized_url(url, email, secret)

    print("\n✅ QUIZ SESSION STARTED")
    print(f"📧 Email: {email}")
    print(f"🔗 Raw URL: {url}")
    print(f"🔐 Authorized URL:")
    print(f"   {authorized_url}")
    print("\n👉 Open the AUTHORIZED URL in browser")
    print("👉 Solve manually")
    print("👉 POST answer to /answer\n")

    return JSONResponse(
        status_code=200,
        content={
            "status": "session_started",
            "current_url": url,
            "authorized_url": authorized_url,
            "message": "Open authorized_url in browser, solve manually, then POST to /answer"
        }
    )


# -----------------------------------
# SUBMIT MANUAL ANSWER
# -----------------------------------
@app.post("/answer")
async def submit_answer(request: Request):
    """
    Submit manual answer.

    Body:
    {
      "answer": "your_answer_here",
      "submit_url": "https://tds-llm-analysis.s-anand.net/submit"
    }
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    answer = data.get("answer")
    submit_url = data.get("submit_url") or DEFAULT_SUBMIT_URL

    if answer is None:
        raise HTTPException(status_code=400, detail="Missing 'answer'")
    if not submit_url:
        raise HTTPException(status_code=400, detail="Missing 'submit_url'")

    session = shared_store.get_session()
    if not session["email"] or not session["url"]:
        raise HTTPException(
            status_code=400,
            detail="No active session. Call /solve first."
        )

    payload = {
        "email": session["email"],
        "secret": session["secret"],
        "url": session["url"],
        "answer": str(answer)
    }

    print("\n📤 SUBMITTING ANSWER")
    print(f"➡️ Submit URL: {submit_url}")
    print(f"📦 Payload: {payload}")

    try:
        r = requests.post(submit_url, json=payload, timeout=30)
        r.raise_for_status()
        response_data = r.json()

        print("\n📥 RAW RESPONSE:")
        print(response_data)

        next_url = response_data.get("url")
        authorized_next_url = None

        if next_url:
            shared_store.update_url(next_url)
            authorized_next_url = make_authorized_url(
                next_url,
                session["email"],
                session["secret"]
            )

            print("\n🔗 NEXT QUESTION")
            print(f"Raw URL: {next_url}")
            print(f"Authorized URL:")
            print(f"   {authorized_next_url}")
            print("👉 Open AUTHORIZED URL in browser\n")
        else:
            print("\n🎉 QUIZ COMPLETED\n")

        return JSONResponse(
            status_code=200,
            content={
                "status": "submitted",
                "correct": response_data.get("correct"),
                "reason": response_data.get("reason"),
                "next_url": next_url,
                "authorized_next_url": authorized_next_url,
                "raw_response": response_data
            }
        )

    except requests.exceptions.RequestException as e:
        raise HTTPException(
            status_code=500,
            detail=f"Submission failed: {str(e)}"
        )


# -----------------------------------
# RUN SERVER
# -----------------------------------
if __name__ == "__main__":
    print("\n🚀 MANUAL QUIZ HELPER SERVER")
    print("=" * 50)
    print("FLOW:")
    print("1️⃣ POST /solve")
    print("2️⃣ Open authorized_url in browser")
    print("3️⃣ Solve manually")
    print("4️⃣ POST /answer")
    print("5️⃣ Repeat until finished")
    print("=" * 50)

    uvicorn.run(app, host="0.0.0.0", port=7860)
