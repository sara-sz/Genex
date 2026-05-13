"""
auth.py — Genex Parent Copilot Authentication
----------------------------------------------
Modular auth layer with two modes controlled by AUTH_MODE env var:

  AUTH_MODE=mock               Local dev — no external services needed.
                               Any allowlisted email can register with any
                               password (6+ chars). State is in-memory only.

  AUTH_MODE=identity_platform  Staging / prod — Google Cloud Identity Platform
                               (Firebase Authentication with Identity Platform).
                               Requires FIREBASE_API_KEY env var and a service
                               account with Identity Platform permissions.

Public API
----------
  register(email, password)   -> (success: bool, error: str, uid: str)
  login(email, password)      -> (success: bool, error: str, uid: str, token: str)
  send_password_reset(email)  -> (success: bool, message: str)
  is_authenticated()          -> bool
  get_current_user()          -> dict | None   {"uid": ..., "email": ...}
  sign_out()                  -> None
"""

import hashlib
import os
import uuid
from typing import Optional, Tuple

import streamlit as st

# ── Mode ───────────────────────────────────────────────────────────────────
AUTH_MODE = os.environ.get("AUTH_MODE", "mock").strip().lower()

# ── Identity Platform config ───────────────────────────────────────────────
_FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "").strip()
_IP_SIGN_IN_URL   = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
_IP_REGISTER_URL  = "https://identitytoolkit.googleapis.com/v1/accounts:signUp"
_IP_RESET_URL     = "https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode"

# ── Mock user store (in-memory, dev only — resets on server restart) ───────
_MOCK_USERS: dict = {}


# ── Internal helpers ───────────────────────────────────────────────────────

def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _ip_post(url: str, payload: dict) -> Tuple[bool, str, dict]:
    """POST to Identity Platform REST endpoint. Returns (ok, error_msg, data)."""
    import requests  # lazy import — not needed in mock mode
    try:
        resp = requests.post(
            f"{url}?key={_FIREBASE_API_KEY}",
            json=payload,
            timeout=10,
        )
        data = resp.json()
        if resp.status_code == 200:
            return True, "", data
        code = data.get("error", {}).get("message", "UNKNOWN_ERROR")
        return False, _friendly_error(code), {}
    except Exception as exc:
        return False, f"Connection error — please try again. ({exc})", {}


def _friendly_error(code: str) -> str:
    return {
        "EMAIL_NOT_FOUND":    "No account found with that email.",
        "INVALID_PASSWORD":   "Incorrect password. Please try again.",
        "INVALID_EMAIL":      "Please enter a valid email address.",
        "INVALID_LOGIN_CREDENTIALS": "Incorrect email or password.",
        "EMAIL_EXISTS":       "An account with this email already exists.",
        "WEAK_PASSWORD : Password should be at least 6 characters":
                              "Password must be at least 6 characters.",
        "TOO_MANY_ATTEMPTS_TRY_LATER":
                              "Too many attempts. Please try again later.",
        "USER_DISABLED":      "This account has been disabled. Please contact support.",
    }.get(code, f"Authentication error ({code}).")


# ── Public API ─────────────────────────────────────────────────────────────

def register(email: str, password: str) -> Tuple[bool, str, str]:
    """
    Create a new account.
    Returns (success, error_message, uid).
    uid is empty string on failure.
    """
    email = email.strip().lower()

    if len(password) < 6:
        return False, "Password must be at least 6 characters.", ""

    if AUTH_MODE == "identity_platform":
        if not _FIREBASE_API_KEY:
            return False, "Auth service not configured. Please email info@getgenex.com for help.", ""
        ok, err, data = _ip_post(_IP_REGISTER_URL, {
            "email": email,
            "password": password,
            "returnSecureToken": True,
        })
        if ok:
            return True, "", data.get("localId", "")
        return False, err, ""

    # ── Mock mode ──────────────────────────────────────────────────────────
    if email in _MOCK_USERS:
        return False, "An account with this email already exists.", ""
    uid = str(uuid.uuid4())
    _MOCK_USERS[email] = {"password_hash": _hash(password), "uid": uid}
    print(f"[auth:mock] Registered {email} → uid={uid}")
    return True, "", uid


def login(email: str, password: str) -> Tuple[bool, str, str, str]:
    """
    Sign in an existing user.
    Returns (success, error_message, uid, id_token).
    uid and id_token are empty strings on failure.
    """
    email = email.strip().lower()

    if AUTH_MODE == "identity_platform":
        if not _FIREBASE_API_KEY:
            return False, "Auth service not configured. Please email info@getgenex.com for help.", "", ""
        ok, err, data = _ip_post(_IP_SIGN_IN_URL, {
            "email": email,
            "password": password,
            "returnSecureToken": True,
        })
        if ok:
            return True, "", data.get("localId", ""), data.get("idToken", "")
        return False, err, "", ""

    # ── Mock mode ──────────────────────────────────────────────────────────
    user = _MOCK_USERS.get(email)
    if not user:
        return False, "No account found with that email.", "", ""
    if user["password_hash"] != _hash(password):
        return False, "Incorrect password. Please try again.", "", ""
    token = f"mock_token_{user['uid']}"
    return True, "", user["uid"], token


def send_password_reset(email: str) -> Tuple[bool, str]:
    """
    Send a password-reset email.
    Returns (success, message).
    Never reveals whether the email exists.
    """
    email = email.strip().lower()

    if AUTH_MODE == "identity_platform":
        if not _FIREBASE_API_KEY:
            return False, "Auth service not configured. Please email info@getgenex.com for help."
        ok, err, _ = _ip_post(_IP_RESET_URL, {
            "requestType": "PASSWORD_RESET",
            "email": email,
        })
        if ok:
            return True, "If that email has an account, a reset link has been sent. Check your inbox."
        # Surface real errors (e.g. INVALID_EMAIL) but not EMAIL_NOT_FOUND
        if "not found" in err.lower():
            return True, "If that email has an account, a reset link has been sent. Check your inbox."
        return False, err

    # ── Mock mode ──────────────────────────────────────────────────────────
    print(f"[auth:mock] Password reset requested for: {email}")
    return True, "If that email has an account, a reset link has been sent. Check your inbox."


# ── Session helpers ────────────────────────────────────────────────────────

def is_authenticated() -> bool:
    """Return True if a user is currently signed in."""
    return bool(st.session_state.get("auth_user"))


def get_current_user() -> Optional[dict]:
    """Return the signed-in user dict, or None."""
    return st.session_state.get("auth_user")


def sign_out():
    """Clear all auth and session state."""
    keys_to_clear = [
        "auth_user", "genex_state", "screen", "parent_plan",
        "interview_domain_idx", "child_display_name", "child_concern",
        "plan_just_built", "session_id", "child_id",
        "consent_given", "consent_timestamp", "_authenticated",
    ]
    for key in keys_to_clear:
        st.session_state.pop(key, None)
    # Clear interview band-state keys
    for key in list(st.session_state.keys()):
        if key.startswith(("bq_", "bm_", "bi_", "bf_", "bc_", "nav_")):
            del st.session_state[key]
