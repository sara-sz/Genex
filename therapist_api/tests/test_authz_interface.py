"""Authorization interface fails closed by default; stub exercises the gate."""

from __future__ import annotations

from app.auth import AuthenticatedUser
from app.authz import AuthzContext, FailClosedAuthorizer, StubAuthorizer
from app.domain.roles import UserRole


def _slp(env="dev"):
    return AuthenticatedUser(uid="slp1", environment=env, role=UserRole.SLP)


def _parent(env="dev"):
    return AuthenticatedUser(uid="par1", environment=env, role=UserRole.PARENT)


def test_default_authorizer_denies_everything():
    d = FailClosedAuthorizer("dev").authorize(
        _slp(), AuthzContext(action="view_plan", environment="dev")
    )
    assert d.allowed is False


def test_active_connection_and_scope_allows():
    ctx = AuthzContext(
        action="view_plan",
        environment="dev",
        required_role=UserRole.SLP,
        child_ref="child_a",
        connection_status="active",
        granted_scopes=("view_plan", "recommend"),
    )
    assert StubAuthorizer("dev").authorize(_slp(), ctx).allowed is True


def test_no_connection_denied():
    ctx = AuthzContext(
        action="view_plan",
        environment="dev",
        required_role=UserRole.SLP,
        child_ref="child_a",
        connection_status=None,
        granted_scopes=("view_plan",),
    )
    assert StubAuthorizer("dev").authorize(_slp(), ctx).allowed is False


def test_paused_connection_denied():
    ctx = AuthzContext(
        action="view_plan",
        environment="dev",
        required_role=UserRole.SLP,
        child_ref="child_a",
        connection_status="paused",
        granted_scopes=("view_plan",),
    )
    assert StubAuthorizer("dev").authorize(_slp(), ctx).allowed is False


def test_missing_scope_denied():
    ctx = AuthzContext(
        action="recommend",
        environment="dev",
        required_role=UserRole.SLP,
        child_ref="child_a",
        connection_status="active",
        granted_scopes=("view_plan",),  # no 'recommend'
    )
    assert StubAuthorizer("dev").authorize(_slp(), ctx).allowed is False


def test_wrong_role_denied():
    ctx = AuthzContext(
        action="view_plan",
        environment="dev",
        required_role=UserRole.SLP,
        child_ref="child_a",
        connection_status="active",
        granted_scopes=("view_plan",),
    )
    assert StubAuthorizer("dev").authorize(_parent(), ctx).allowed is False


def test_parent_not_owning_child_denied():
    ctx = AuthzContext(
        action="respond_recommendation",
        environment="dev",
        required_role=UserRole.PARENT,
        parent_owns_child=False,
    )
    assert StubAuthorizer("dev").authorize(_parent(), ctx).allowed is False


def test_cross_environment_denied():
    ctx = AuthzContext(action="view_plan", environment="dev", required_role=UserRole.SLP)
    # prod user hitting a dev-scoped context
    assert StubAuthorizer("dev").authorize(_slp("prod"), ctx).allowed is False


def test_invalid_recommendation_state_denied():
    ctx = AuthzContext(
        action="respond_recommendation",
        environment="dev",
        required_role=UserRole.PARENT,
        parent_owns_child=True,
        recommendation_state="applied",
        valid_recommendation_states=("pending_parent_acceptance",),
    )
    assert StubAuthorizer("dev").authorize(_parent(), ctx).allowed is False
