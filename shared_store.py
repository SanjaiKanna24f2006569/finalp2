# shared_store.py
from typing import Optional

CURRENT_URL: Optional[str] = None
EMAIL: Optional[str] = None
SECRET: Optional[str] = None

def set_session(email: str, secret: str, url: str):
    global CURRENT_URL, EMAIL, SECRET
    EMAIL = email
    SECRET = secret
    CURRENT_URL = url

def get_session():
    return {
        "email": EMAIL,
        "secret": SECRET,
        "url": CURRENT_URL
    }

def update_url(url: str):
    global CURRENT_URL
    CURRENT_URL = url
