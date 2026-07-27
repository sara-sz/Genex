"""FastAPI dependencies for the read-slice.

Shared state (settings, repository, auth verifier, read service) is attached to
`app.state` in the app factory; these dependencies read it. Authentication runs
through the configured verifier (fail-closed by default; dev-auth only in
dev/test with the flag on).
"""

from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException, Request

from ..auth.interface import AuthenticatedUser, AuthError
from ..services.read_service import ReadService


def get_service(request: Request) -> ReadService:
    return request.app.state.read_service


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization.removeprefix("Bearer ").strip() or None


async def require_principal(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> AuthenticatedUser:
    """Authenticate the caller into a principal, or 401. No unauthenticated path."""
    verifier = request.app.state.verifier
    token = _extract_bearer(authorization)
    try:
        return verifier.verify(token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=f"Not authenticated: {exc}")
