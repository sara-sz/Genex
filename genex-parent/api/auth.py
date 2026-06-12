"""
api/auth.py — Firebase ID token verification for Genex FastAPI
--------------------------------------------------------------
All protected endpoints require:
    Authorization: Bearer <firebase_id_token>

Verification flow:
  1. Extract Bearer token from Authorization header → 401 if missing/malformed
  2. firebase_admin.auth.verify_id_token(token)  → 401 if invalid or expired
  3. Check decoded email against ALLOWED_EMAILS env var → 403 if not in list
  4. Return AuthUser(uid, email) to the route handler

Notes:
  - This module is separate from the root auth.py, which is the Streamlit-based
    auth layer for the existing app. Do not merge them.
  - On Cloud Run, Application Default Credentials (ADC) are used automatically.
    Locally, set GOOGLE_APPLICATION_CREDENTIALS to a service account JSON path.
  - ALLOWED_EMAILS is a comma-separated env var. It is cached at startup.
    Changing it requires a container restart.
"""

import os
from functools import lru_cache
from typing import Annotated

import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials
from fastapi import Header, HTTPException
from pydantic import BaseModel


# ── Firebase app initialisation ────────────────────────────────────────────

def _init_firebase() -> None:
    """Initialise firebase_admin once at import time. Safe to call repeatedly."""
    if firebase_admin._apps:
        return  # already initialised (e.g. by another import or test setup)

    project_id = os.environ.get("FIREBASE_PROJECT_ID", "").strip()
    if not project_id:
        raise RuntimeError(
            "FIREBASE_PROJECT_ID env var is required to start the Genex API. "
            "Set it to your Firebase/Google Identity Platform project ID."
        )

    # ApplicationDefault uses ADC on Cloud Run; GOOGLE_APPLICATION_CREDENTIALS locally.
    cred = credentials.ApplicationDefault()
    firebase_admin.initialize_app(cred, {"projectId": project_id})


_init_firebase()


# ── Allowlist ──────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _get_allowed_emails() -> frozenset:
    """
    Load the ALLOWED_EMAILS allowlist once and cache it.
    Returns a frozenset of lowercase email strings.
    Raises RuntimeError at startup if the env var is missing or empty.
    """
    raw = os.environ.get("ALLOWED_EMAILS", "").strip()
    if not raw:
        raise RuntimeError(
            "ALLOWED_EMAILS env var is required and must not be empty. "
            "Set it to a comma-separated list of permitted email addresses, "
            "e.g. ALLOWED_EMAILS=sara@example.com,test@example.com"
        )
    emails = frozenset(e.strip().lower() for e in raw.split(",") if e.strip())
    print(f"[auth] Loaded allowlist with {len(emails)} email(s).")
    return emails


# ── Auth model returned to route handlers ─────────────────────────────────

class AuthUser(BaseModel):
    uid: str
    email: str


# ── FastAPI dependency ─────────────────────────────────────────────────────

async def require_auth(
    authorization: Annotated[str | None, Header()] = None,
) -> AuthUser:
    """
    FastAPI dependency. Inject into any protected route:

        @app.post("/some/endpoint")
        async def handler(auth: AuthUser = Depends(require_auth)):
            ...

    Returns AuthUser(uid, email) on success.
    Raises HTTPException 401 if token is missing, malformed, invalid, or expired.
    Raises HTTPException 403 if email is not in the ALLOWED_EMAILS allowlist.
    """
    # ── 1. Extract token ───────────────────────────────────────────────────
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail=(
                "Authorization header missing or malformed. "
                "Expected: Authorization: Bearer <firebase_id_token>"
            ),
        )

    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token is empty.")

    # ── 2. Verify token with Firebase ─────────────────────────────────────
    try:
        decoded = firebase_auth.verify_id_token(token)
    except firebase_auth.ExpiredIdTokenError:
        raise HTTPException(
            status_code=401,
            detail="Firebase ID token has expired. Please sign in again.",
        )
    except firebase_auth.RevokedIdTokenError:
        raise HTTPException(
            status_code=401,
            detail="Firebase ID token has been revoked. Please sign in again.",
        )
    except firebase_auth.InvalidIdTokenError as exc:
        raise HTTPException(
            status_code=401,
            detail=f"Firebase ID token is invalid: {exc}",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail=f"Token verification failed: {exc}",
        )

    # ── 3. Extract uid and email ───────────────────────────────────────────
    uid: str = decoded.get("uid", "").strip()
    email: str = decoded.get("email", "").strip().lower()

    if not uid:
        raise HTTPException(status_code=401, detail="Token is missing uid claim.")
    if not email:
        raise HTTPException(
            status_code=401,
            detail="Token is missing email claim. Ensure the Firebase user has a verified email.",
        )

    # ── 4. Allowlist check ─────────────────────────────────────────────────
    if email not in _get_allowed_emails():
        raise HTTPException(
            status_code=403,
            detail=(
                "Access not available yet. "
                "Your email is not on the Genex access list."
            ),
        )

    return AuthUser(uid=uid, email=email)
