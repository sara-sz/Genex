"""pilot_backend.auth — server-side token verification and identity resolution."""

from .interface import (
    AuthError,
    AuthVerifier,
    RevokedTokenError,
    TokenDecoder,
    VerifiedToken,
)
from .verifiers import (
    DevAuthVerifier,
    FailClosedAuthVerifier,
    IdentityPlatformVerifier,
    build_verifier,
)
from .resolver import Principal, PrincipalResolutionError, resolve_principal

__all__ = [
    "AuthError",
    "AuthVerifier",
    "RevokedTokenError",
    "TokenDecoder",
    "VerifiedToken",
    "DevAuthVerifier",
    "FailClosedAuthVerifier",
    "IdentityPlatformVerifier",
    "build_verifier",
    "Principal",
    "PrincipalResolutionError",
    "resolve_principal",
]
