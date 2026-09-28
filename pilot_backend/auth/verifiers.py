"""pilot_backend/auth/verifiers.py — the three verifiers and how one is chosen.

    FailClosedAuthVerifier    rejects everything. The default.
    DevAuthVerifier           fictional local tokens. dev/test ONLY.
    IdentityPlatformVerifier  production, revocation-checked, decoder-backed.

## Dev auth cannot reach production

Enforced three times, deliberately redundantly, because this is the single
control whose failure would silently open every protected operation:

  1. `PilotSettings` refuses to construct a prod settings object with
     `dev_auth_enabled` true — the service will not start;
  2. `DevAuthVerifier.__init__` raises if handed a prod environment, so it
     cannot be constructed even by code bypassing settings;
  3. `build_verifier` only selects it for dev/test AND when explicitly enabled.

Any one of these alone would be adequate on a good day. Three means a refactor
has to defeat all three before production accepts a fictional token, and each
is tested independently.

## Production always checks revocation

`IdentityPlatformVerifier` passes `check_revoked=True` when the environment is
prod, and the caller cannot override it. Revocation is how account compromise
and staff offboarding actually take effect; making it a per-call argument means
one endpoint eventually forgets.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Mapping, Optional

from .interface import (
    AuthError,
    AuthVerifier,
    RevokedTokenError,
    TokenDecoder,
    VerifiedToken,
    extract_bearer,
)

#: Substrings a provider raises for a revoked credential. Matched
#: case-insensitively against the decoder's exception text, since each SDK
#: words it differently and we must not silently treat revocation as a generic
#: failure — both are 401, but only one is countable as a revocation.
_REVOCATION_MARKERS = ("revoked", "token has been revoked", "session has been revoked")


class FailClosedAuthVerifier(AuthVerifier):
    """Rejects every credential. The correct verifier when none is configured.

    This is what production uses today: no Identity Platform project has been
    approved or provisioned, so there is no decoder to bind, and the service
    must refuse traffic rather than invent an identity.
    """

    def verify(self, bearer: Optional[str]) -> VerifiedToken:
        raise AuthError("authentication is not configured for this environment")


class DevAuthVerifier(AuthVerifier):
    """In-memory fictional tokens for local development and tests."""

    def __init__(self, environment: str,
                 tokens: Optional[Mapping[str, VerifiedToken]] = None) -> None:
        if (environment or "").strip().lower() == "prod":
            raise AuthError("DevAuthVerifier cannot be constructed in prod")
        super().__init__(environment)
        self._tokens: Dict[str, VerifiedToken] = dict(tokens or {})

    def add(self, token: str, verified: VerifiedToken) -> None:
        self._tokens[token] = verified

    def verify(self, bearer: Optional[str]) -> VerifiedToken:
        token = extract_bearer(bearer)
        found = self._tokens.get(token)
        if found is None:
            raise AuthError("unknown or invalid token")
        return found


class IdentityPlatformVerifier(AuthVerifier):
    """Production verifier over an injected `TokenDecoder`.

    Holds no credentials and opens no connections; the decoder does both. That
    keeps this class fully testable with a fake decoder while still expressing
    the real production rules — revocation checking, verified-email policy, and
    translation of provider claims into a `VerifiedToken`.
    """

    def __init__(self, environment: str, decoder: TokenDecoder, *,
                 require_verified_email: bool = False) -> None:
        super().__init__(environment)
        if decoder is None:
            raise AuthError("IdentityPlatformVerifier requires a token decoder")
        self._decoder = decoder
        self._require_verified_email = require_verified_email

    @property
    def _check_revoked(self) -> bool:
        """Non-prod may skip the provider round-trip; prod never does."""
        return (self.environment or "").strip().lower() == "prod"

    def verify(self, bearer: Optional[str]) -> VerifiedToken:
        token = extract_bearer(bearer)
        check_revoked = self._check_revoked
        try:
            claims = self._decoder(token, check_revoked=check_revoked)
        except AuthError:
            raise
        except Exception as exc:
            # The provider SDK's exception text is third-party and may echo
            # request content. Classify it, then discard it: only our own
            # wording is ever raised onward or logged.
            if any(marker in str(exc).lower() for marker in _REVOCATION_MARKERS):
                raise RevokedTokenError("token has been revoked") from None
            raise AuthError("invalid token") from None

        if not isinstance(claims, Mapping):
            raise AuthError("invalid token")

        subject = str(claims.get("uid") or claims.get("sub") or "").strip()
        if not subject:
            raise AuthError("invalid token")

        email_verified = bool(claims.get("email_verified", False))
        if self._require_verified_email and not email_verified:
            raise AuthError("email is not verified")

        return VerifiedToken(
            subject=subject,
            issuer=str(claims.get("iss") or ""),
            audience=str(claims.get("aud") or ""),
            email=(str(claims["email"]) if claims.get("email") else None),
            email_verified=email_verified,
            issued_at=_as_utc(claims.get("iat")),
            expires_at=_as_utc(claims.get("exp")),
            revocation_checked=check_revoked,
        )


def _as_utc(value: object) -> Optional[datetime]:
    """Accept a POSIX timestamp or a datetime; normalise to aware UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


def build_verifier(settings, decoder: Optional[TokenDecoder] = None) -> AuthVerifier:
    """Select the verifier for a configuration. Fail-closed by default.

    prod + decoder        -> IdentityPlatformVerifier (revocation checked)
    prod, no decoder      -> FailClosedAuthVerifier (today's real state)
    dev/test + dev auth   -> DevAuthVerifier
    dev/test + decoder    -> IdentityPlatformVerifier
    anything else         -> FailClosedAuthVerifier
    """
    environment = settings.environment.value

    if settings.environment.is_prod:
        # Belt and braces: settings already refuses this combination.
        if settings.dev_auth_enabled:
            raise AuthError("dev auth is not permitted in prod")
        if decoder is None:
            return FailClosedAuthVerifier(environment)
        return IdentityPlatformVerifier(environment, decoder)

    if settings.dev_auth_enabled:
        return DevAuthVerifier(environment)
    if decoder is not None:
        return IdentityPlatformVerifier(environment, decoder)
    return FailClosedAuthVerifier(environment)
