"""Authorization interface (fail-closed) + test stub."""

from .interface import (
    AuthzContext,
    AuthzDecision,
    Authorizer,
    FailClosedAuthorizer,
)
from .stub import StubAuthorizer

__all__ = [
    "AuthzContext",
    "AuthzDecision",
    "Authorizer",
    "FailClosedAuthorizer",
    "StubAuthorizer",
]
