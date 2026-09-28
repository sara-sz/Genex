"""pilot_backend/auth/interface.py — the authentication contract.

## The SDK is a port, not a dependency

Production verification targets Identity Platform / Firebase Admin ID tokens.
This package does not import `firebase_admin`, and neither does anything else
in `pilot_backend`. Instead, `TokenDecoder` describes the one call we need —
"decode and validate this ID token, optionally checking revocation" — with the
exact semantics `firebase_admin.auth.verify_id_token(token, check_revoked=True)`
provides.

Three reasons this is the right shape rather than a convenience:

  * the pilot test suite runs on a dependency-pure CI job with no network and
    no credentials, and security behaviour must be provable there;
  * BACKEND 0.1 asserts structurally that no auth SDK is imported anywhere in
    this package, and that invariant is preserved here, not weakened;
  * binding the real decoder is a deployment decision owned by the HIPAA
    workstream, which has not yet approved an Identity Platform project.

The composition root supplies the decoder when provisioning is approved. Until
then `build_verifier` returns a fail-closed verifier in production — the
service cannot authenticate anyone, which is the correct posture for a backend
whose identity provider does not exist yet.

## Fail closed, always

Every failure path raises `AuthError`. There is no return value meaning
"couldn't tell" — an exception is the only way out other than a fully verified
token, so a caller cannot forget to check a boolean.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional, Protocol, runtime_checkable


class AuthError(Exception):
    """Authentication failed. Always maps to HTTP 401, never 403.

    PHI-safe by declaration: messages here name the failure mode only
    ("missing bearer token"), never the token, the subject, or any payload.
    """

    PHI_SAFE_MESSAGE = True


class RevokedTokenError(AuthError):
    """The token was structurally valid but has been revoked or the session ended.

    A distinct type so revocation is provable in tests and countable in audit,
    but it is still a 401: the credential is no longer a credential.
    """


@dataclass(frozen=True)
class VerifiedToken:
    """The ONLY trustworthy statement about who is calling.

    Everything downstream — application identity, role, child access — derives
    from this object. A uid, email, role, caregiver id or provider id appearing
    anywhere in a request body or header is untrusted input and is never read
    as identity.
    """

    #: The identity provider's stable subject (Firebase `uid`). Matched against
    #: `auth_subject` on the application record. Never used as a display value.
    subject: str
    issuer: str = ""
    audience: str = ""
    #: Present for contact purposes only. NEVER an identity key and never an
    #: authorization input — see `resolver` and `authz`.
    email: Optional[str] = None
    email_verified: bool = False
    issued_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    #: True only when revocation was actually checked against the provider.
    #: Production verification refuses to produce a token without it.
    revocation_checked: bool = False
    claims: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (self.subject or "").strip():
            raise AuthError("verified token has no subject")


@runtime_checkable
class TokenDecoder(Protocol):
    """Port for the real Identity Platform / Firebase Admin verification call.

    Implementations must:
      * verify signature, issuer, audience and expiry;
      * when `check_revoked` is true, consult the provider for revocation and
        raise (not return) if the token or session has been revoked;
      * raise on ANY failure. Returning a partial result is a security bug.

    The return mapping carries provider claims; `IdentityPlatformVerifier`
    translates it into a `VerifiedToken`.
    """

    def __call__(self, token: str, *, check_revoked: bool) -> Mapping[str, object]: ...


class AuthVerifier(ABC):
    """Turns a bearer credential into a `VerifiedToken`, or raises."""

    def __init__(self, environment: str) -> None:
        self.environment = environment

    @abstractmethod
    def verify(self, bearer: Optional[str]) -> VerifiedToken:
        """Verify a raw `Authorization` header value or bare token.

        Raises `AuthError` on missing, malformed, invalid, expired or revoked
        credentials. Never returns None.
        """


def extract_bearer(raw: Optional[str]) -> str:
    """Pull the token out of an `Authorization` header, strictly.

    Accepts `Bearer <token>` (scheme case-insensitive) or a bare token, and
    rejects anything else. Deliberately strict: a header like `Basic abc`
    should be a 401, not a token of "Basic abc" that fails verification later
    for a confusing reason.
    """
    value = (raw or "").strip()
    if not value:
        raise AuthError("missing bearer token")
    parts = value.split()
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2 and parts[0].lower() == "bearer":
        if not parts[1].strip():
            raise AuthError("missing bearer token")
        return parts[1]
    raise AuthError("malformed Authorization header")
