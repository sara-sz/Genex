"""Authorization interface.

Authorization is enforced SERVER-SIDE and is the source of truth (never the
front-end role screen, never a shared code). The ordered gate is:

  1. authenticated + token issuer == this environment
  2. server-side role resolved and permitted for the action
  3. (therapist->child) an ACTIVE TherapistChildConnection exists
  4. the connection is not paused/ended
  5. the required permission scope is granted
  6. (parent action) the parent owns the child (ParentChildRelationship)
  7. (recommendation action) the recommendation is in a valid state
  8. environment checks pass

The DEFAULT authorizer denies everything (fail-closed). A real policy authorizer
is a later, explicitly-approved step; a StubAuthorizer exists for tests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from pydantic import BaseModel

from ..auth.interface import AuthenticatedUser
from ..domain.roles import UserRole


class AuthzContext(BaseModel):
    """Everything a policy needs to decide a single request."""

    action: str  # e.g. "view_plan", "recommend", "respond_recommendation"
    environment: str
    required_role: Optional[UserRole] = None
    child_ref: Optional[str] = None
    connection_status: Optional[str] = None  # active|paused|ended|...
    granted_scopes: tuple[str, ...] = ()
    parent_owns_child: Optional[bool] = None
    recommendation_state: Optional[str] = None
    valid_recommendation_states: tuple[str, ...] = ()


class AuthzDecision(BaseModel):
    allowed: bool
    reason: str = ""


class Authorizer(ABC):
    def __init__(self, environment: str) -> None:
        self.environment = environment

    @abstractmethod
    def authorize(self, user: AuthenticatedUser, ctx: AuthzContext) -> AuthzDecision:
        raise NotImplementedError


class FailClosedAuthorizer(Authorizer):
    """Default authorizer: denies everything."""

    def authorize(self, user: AuthenticatedUser, ctx: AuthzContext) -> AuthzDecision:
        return AuthzDecision(
            allowed=False,
            reason="Authorization not configured (fail-closed default).",
        )
