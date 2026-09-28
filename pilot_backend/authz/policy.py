"""pilot_backend/authz/policy.py — may this principal touch this child?

    Caregiver -> active CaregiverChildConnection -> Child
    Provider  -> active ProviderChildConnection  -> Child

## Deny by default

The function returns a denial unless it positively finds an ACTIVE connection
between the principal's application id and the requested child. There is no
`else: allow`, no admin bypass, and no branch that grants access from the
absence of something.

## What is NOT consulted

  * the request body, in any form — uid, email, role, caregiver_id,
    provider_id and practice_id from a caller are ignored entirely; the
    signature takes a `Principal`, which can only come from a verified token;
  * `BETA_ACCESS_CODE` — the Parent 2.3 beta gate is a shared static string and
    is not an identity. Nothing in this module reads it, and a test asserts the
    whole authz package is free of it;
  * frontend state or browser storage, which are not inputs to a server
    decision at all;
  * `Provider.practice_id` at read time — the practice that held the
    relationship is denormalised onto the connection, so a provider changing
    employer cannot retroactively alter who had access.

## Why the child id is re-read

The caller supplies a child id and it is deliberately treated as hostile: it is
used only as a lookup key, and every path that could grant access requires a
connection row naming that exact id. Altering the id in a request therefore
selects a different child for whom the same relationship test must independently
pass.
"""

from __future__ import annotations

from typing import Optional

from ..auth.interface import AuthError, RevokedTokenError, VerifiedToken
from ..auth.resolver import Principal, PrincipalResolutionError, resolve_principal
from ..domain.enums import EntityStatus
from ..domain.roles import ActorRole, CHILD_RELATED_ROLES
from ..repository.interface import RecordNotFound
from .decisions import AccessDecision, Denial


def authorize_child_access(principal: Principal, child_id: str, repos) -> AccessDecision:
    """Deny-by-default child access check for an already-authenticated caller."""
    requested = (child_id or "").strip()
    if not requested:
        return AccessDecision.deny(Denial.UNKNOWN_CHILD, principal=principal)

    if principal.role not in CHILD_RELATED_ROLES:
        return AccessDecision.deny(
            Denial.ROLE_NOT_PERMITTED, principal=principal, child_id=requested)

    # The child must exist and be active before any relationship is considered.
    try:
        child = repos.children.get_by_id(requested)
    except RecordNotFound:
        return AccessDecision.deny(
            Denial.UNKNOWN_CHILD, principal=principal, child_id=requested)
    if child.status is not EntityStatus.ACTIVE:
        return AccessDecision.deny(
            Denial.INACTIVE_CHILD, principal=principal, child_id=requested)

    if principal.role is ActorRole.CAREGIVER:
        connections = repos.caregiver_child.list_caregivers_for_child(
            requested, include_ended=True)
        mine = [c for c in connections if c.caregiver_id == principal.application_id]
    elif principal.role is ActorRole.PROVIDER:
        connections = repos.provider_child.list_providers_for_child(
            requested, include_ended=True)
        mine = [c for c in connections if c.provider_id == principal.application_id]
    else:  # pragma: no cover - CHILD_RELATED_ROLES is checked above
        return AccessDecision.deny(
            Denial.ROLE_NOT_PERMITTED, principal=principal, child_id=requested)

    if not mine:
        # No relationship ever existed. Distinct from one that ended, for audit.
        return AccessDecision.deny(
            Denial.NO_RELATIONSHIP, principal=principal, child_id=requested)

    if not any(c.is_active for c in mine):
        # Ended, revoked or still pending — all refuse, immediately. `is_active`
        # requires status ACTIVE *and* ended_at unset, so a row that was ended
        # in the same request cycle stops granting access on the next read.
        return AccessDecision.deny(
            Denial.INACTIVE_RELATIONSHIP, principal=principal, child_id=requested)

    return AccessDecision.allow(principal, requested)


def authenticate_and_authorize_child(
    bearer: Optional[str], child_id: str, *, verifier, repos,
) -> AccessDecision:
    """Full request path: credential -> identity -> relationship -> decision.

    The single entry point a transport layer should call. Keeping the whole
    chain in one function is what guarantees the 401/403 split is applied
    consistently: an endpoint cannot verify a token and then forget to
    authorize, because it never receives an intermediate result.
    """
    try:
        verified: VerifiedToken = verifier.verify(bearer)
    except RevokedTokenError:
        return AccessDecision.deny(Denial.REVOKED_TOKEN, child_id=child_id)
    except AuthError as exc:
        denial = Denial.NO_TOKEN if "missing bearer token" in str(exc) else Denial.INVALID_TOKEN
        return AccessDecision.deny(denial, child_id=child_id)

    try:
        principal = resolve_principal(verified, repos)
    except PrincipalResolutionError as exc:
        # Authenticated but not provisioned, or provisioned but retired: 403.
        denial = (Denial.INACTIVE_ACTOR if "not active" in str(exc)
                  else Denial.NO_APPLICATION_RECORD)
        return AccessDecision.deny(denial, child_id=child_id)

    return authorize_child_access(principal, child_id, repos)
