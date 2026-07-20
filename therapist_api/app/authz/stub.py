"""Test-only stub authorizer implementing the ordered gate.

This exercises the intended policy shape so the gate can be tested, but it is
NOT wired to any real datastore. Every check that is not satisfied denies with
an explicit reason (fail-closed at every step).
"""

from __future__ import annotations

from .interface import AuthenticatedUser, AuthzContext, AuthzDecision, Authorizer


def _deny(reason: str) -> AuthzDecision:
    return AuthzDecision(allowed=False, reason=reason)


class StubAuthorizer(Authorizer):
    def authorize(self, user: AuthenticatedUser, ctx: AuthzContext) -> AuthzDecision:
        # 1. environment coherence
        if user.environment != self.environment or ctx.environment != self.environment:
            return _deny("environment mismatch")

        # 2. role
        if ctx.required_role is not None and user.role != ctx.required_role:
            return _deny(f"role {user.role} != required {ctx.required_role}")

        # 3/4. therapist -> child connection must be active (not paused/ended)
        if ctx.child_ref is not None and ctx.required_role is not None:
            from ..domain.roles import UserRole

            if ctx.required_role == UserRole.SLP:
                if ctx.connection_status is None:
                    return _deny("no connection to child")
                if ctx.connection_status != "active":
                    return _deny(f"connection not active ({ctx.connection_status})")

        # 5. scope
        if ctx.action and ctx.child_ref is not None and ctx.granted_scopes:
            if ctx.action not in ctx.granted_scopes:
                return _deny(f"scope '{ctx.action}' not granted")

        # 6. parent owns child (for parent actions)
        if ctx.parent_owns_child is False:
            return _deny("parent does not own child")

        # 7. recommendation state
        if ctx.valid_recommendation_states:
            if ctx.recommendation_state not in ctx.valid_recommendation_states:
                return _deny(
                    f"recommendation state '{ctx.recommendation_state}' not in "
                    f"{ctx.valid_recommendation_states}"
                )

        return AuthzDecision(allowed=True, reason="ok")
