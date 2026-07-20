"""Test-only stub auth verifier.

Grants identities from an in-memory token->user map. Enforces environment
isolation: a user whose environment differs from the verifier's environment is
rejected (models the dev/prod Firebase issuer boundary).

NOT for production use. Real verification (Firebase ID tokens) is a later,
explicitly-approved step.
"""

from __future__ import annotations

from typing import Dict, Optional

from .interface import AuthenticatedUser, AuthError, AuthVerifier


class StubAuthVerifier(AuthVerifier):
    def __init__(self, environment: str, tokens: Optional[Dict[str, AuthenticatedUser]] = None):
        super().__init__(environment)
        self._tokens: Dict[str, AuthenticatedUser] = dict(tokens or {})

    def add(self, token: str, user: AuthenticatedUser) -> None:
        self._tokens[token] = user

    def verify(self, token: Optional[str]) -> AuthenticatedUser:
        if not token:
            raise AuthError("Missing bearer token.")
        user = self._tokens.get(token)
        if user is None:
            raise AuthError("Unknown or invalid token.")
        if user.environment != self.environment:
            # Cross-environment token: a dev identity presented to prod (or vice
            # versa) must be rejected — separate Firebase issuers per environment.
            raise AuthError(
                f"Token environment '{user.environment}' does not match service "
                f"environment '{self.environment}'."
            )
        return user
