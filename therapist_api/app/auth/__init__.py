"""Authentication interface (fail-closed) + test stubs.

No real Firebase verification is wired in this phase. The base verifier denies
everything; only an explicit test stub grants identities.
"""

from .interface import (
    AuthenticatedUser,
    AuthError,
    AuthVerifier,
    FailClosedAuthVerifier,
)
from .stub import StubAuthVerifier

__all__ = [
    "AuthenticatedUser",
    "AuthError",
    "AuthVerifier",
    "FailClosedAuthVerifier",
    "StubAuthVerifier",
]
