"""
api/auth.py — Firebase ID token verification + beta access for Genex FastAPI
----------------------------------------------------------------------------
All protected endpoints require:
    Authorization: Bearer <firebase_id_token>

Verification flow (require_auth):
  1. Extract Bearer token from Authorization header → 401 if missing/malformed
  2. firebase_admin.auth.verify_id_token(token)  → 401 if invalid or expired
  3. Return AuthUser(uid, email) to the route handler

Beta access (verify_beta_code):
  Access is no longer gated by an email allowlist. Any signed-in Firebase user
  may use Genex by supplying a shared beta access code on /session/start.
  - REQUIRE_BETA_CODE (default "true") toggles enforcement.
  - BETA_ACCESS_CODE (default "genex") is the expected code.
  - The submitted code is normalised (trimmed + lowercased) before comparison,
    so "genex", "Genex", " GENEX " all match.
  - Only /session/start checks the code. Once a session exists it is owned by
    the Firebase uid; subsequent routes require a valid token + ownership only.
  - The beta code itself is never stored in GCS.

Notes:
  - This module is separate from the root auth.py, which is the Streamlit-based
    auth layer for the existing app. Do not merge them.
  - On Cloud Run, Application Default Credentials (ADC) are used automatically.
    Locally, set GOOGLE_APPLICATION_CREDENTIALS to a service account JSON path.
"""

import os
from typing import Annotated, Optional

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


# ── Beta access code ───────────────────────────────────────────────────────

_TRUE_VALUES = {"1", "true", "yes", "on"}


def _beta_code_required() -> bool:
    """Return True when /session/start must enforce the beta access code.

    Controlled by REQUIRE_BETA_CODE (default "true"). Read at call time so the
    setting can change without code edits and is easy to flip in tests.
    """
    return os.environ.get("REQUIRE_BETA_CODE", "true").strip().lower() in _TRUE_VALUES


def _configured_beta_code() -> str:
    """Return the normalised expected beta code (BETA_ACCESS_CODE, default 'genex')."""
    return os.environ.get("BETA_ACCESS_CODE", "genex").strip().lower()


def _normalize_code(code: Optional[str]) -> str:
    """Normalise a submitted code: trim surrounding spaces, lowercase."""
    return (code or "").strip().lower()


def verify_beta_code(submitted: Optional[str]) -> None:
    """Validate the shared beta access code submitted on /session/start.

    No-op when REQUIRE_BETA_CODE is disabled. Otherwise raises HTTPException 403
    unless the normalised submitted code matches the normalised configured code.
    The code is never persisted — callers store only a boolean authorisation flag.
    """
    if not _beta_code_required():
        return
    if _normalize_code(submitted) != _configured_beta_code():
        raise HTTPException(
            status_code=403,
            detail=(
                "Invalid or missing beta access code. "
                "Enter the beta access code to start using Genex."
            ),
        )


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

    Returns AuthUser(uid, email) on success. email may be "" for providers that
    do not supply one (e.g. phone sign-in) — access is gated by the beta code on
    /session/start and by session ownership thereafter, not by email.
    Raises HTTPException 401 if token is missing, malformed, invalid, or expired.
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
    # uid is required (it owns the session). email is optional — some Firebase
    # providers (e.g. phone) do not supply one. Beta access is enforced by the
    # access code on /session/start, not by email membership.
    uid: str = (decoded.get("uid") or "").strip()
    email: str = (decoded.get("email") or "").strip().lower()

    if not uid:
        raise HTTPException(status_code=401, detail="Token is missing uid claim.")

    return AuthUser(uid=uid, email=email)
