"""Local fictional dev-auth adapter (dev/test ONLY).

Selection: the client sends `Authorization: Bearer <dev-token>`, where the token
is one of a small set of FICTIONAL dev tokens mapped to fictional principals.
There is no Firebase and no unauthenticated pathway.

Safety:
  * Constructed only when environment is dev/test AND `dev_auth_enabled` is true.
  * `build_verifier` returns the FailClosedAuthVerifier otherwise (deny-all).
  * env_validation additionally forbids `dev_auth_enabled` in prod, so this can
    never be wired in Prod even by misconfiguration.

Fictional tokens (dev/test only):
  * "dev-hannah"                 -> Hannah (therapist principal, connected)
  * "dev-elena"                  -> Elena (parent principal)
  * "dev-unconnected-therapist"  -> a therapist with no connections
  * "dev-priya"                  -> a therapist connected only to another child
"""

from __future__ import annotations

from typing import Dict, Optional

from ..domain.roles import UserRole
from .interface import AuthenticatedUser, AuthError, AuthVerifier, FailClosedAuthVerifier

# Fictional dev tokens → (uid, role, email). Kept tiny and explicit.
_DEV_TOKENS: Dict[str, Dict[str, object]] = {
    "dev-hannah": {"uid": "dev-hannah", "role": UserRole.SLP, "email": "hannah@talkshop.example"},
    "dev-elena": {"uid": "dev-elena", "role": UserRole.PARENT, "email": "elena@family.example"},
    "dev-unconnected-therapist": {
        "uid": "dev-unconnected-therapist",
        "role": UserRole.SLP,
        "email": "other@elsewhere.example",
    },
    "dev-priya": {"uid": "dev-priya", "role": UserRole.SLP, "email": "priya@northside.example"},
}


class DevAuthVerifier(AuthVerifier):
    """Maps fictional dev bearer tokens to fictional principals (dev/test only)."""

    def verify(self, token: Optional[str]) -> AuthenticatedUser:
        if not token:
            raise AuthError("Missing bearer token.")
        record = _DEV_TOKENS.get(token.strip())
        if record is None:
            raise AuthError("Unknown or invalid dev token.")
        return AuthenticatedUser(
            uid=str(record["uid"]),
            email=str(record["email"]),
            environment=self.environment,
            role=record["role"],  # type: ignore[arg-type]
            claims={"dev_auth": True},
        )


def build_verifier(environment: str, dev_auth_enabled: bool) -> AuthVerifier:
    """Return the dev-auth verifier ONLY in dev/test with the flag on; else deny-all."""
    if dev_auth_enabled and environment in ("dev", "test"):
        return DevAuthVerifier(environment)
    return FailClosedAuthVerifier(environment)
