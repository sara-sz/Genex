"""Authentication interface.

Real Firebase ID-token verification is intentionally NOT implemented in this
phase. The interface exists so routes can depend on it, and the DEFAULT
implementation fails closed (denies all) — wiring real verification is a later,
explicitly-approved step.

Environment isolation: a verifier is constructed for a specific environment and
MUST reject an identity minted for a different environment's Firebase project
(dev token -> prod = reject, and vice versa).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Mapping, Optional

from pydantic import BaseModel, Field

from ..domain.roles import UserRole


class AuthError(Exception):
    """Raised when authentication fails. Fail-closed: default behavior is to raise."""


class AuthenticatedUser(BaseModel):
    uid: str
    email: str = ""
    environment: str
    role: Optional[UserRole] = None
    claims: Mapping[str, object] = Field(default_factory=dict)


class AuthVerifier(ABC):
    """Verifies a bearer token into an AuthenticatedUser for a given environment."""

    def __init__(self, environment: str) -> None:
        self.environment = environment

    @abstractmethod
    def verify(self, token: Optional[str]) -> AuthenticatedUser:
        """Return an AuthenticatedUser or raise AuthError. Never returns None."""
        raise NotImplementedError


class FailClosedAuthVerifier(AuthVerifier):
    """Default verifier: denies everything.

    This is the safe default until real Firebase verification is wired and
    explicitly approved. It guarantees the service cannot accidentally treat an
    unauthenticated caller as authenticated.
    """

    def verify(self, token: Optional[str]) -> AuthenticatedUser:
        raise AuthError(
            "Authentication is not configured (fail-closed default). "
            "No real token verifier is wired in this phase."
        )
