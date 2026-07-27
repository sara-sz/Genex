"""Read-access policy for the therapist slice.

Resolves the authenticated principal to a server-side profile and decides the
access level for a therapist→child request. Fail-closed and existence-blind:
an unauthorized or unknown child yields the SAME `ChildNotFound` (404) so we
never reveal whether a child id exists.
"""

from __future__ import annotations

from typing import Optional, Tuple

from ..auth.interface import AuthenticatedUser
from ..domain.enums import (
    ChildAccessLevel,
    ConnectionStatus,
    PrincipalRole,
    RESTRICTED_CONNECTION_STATUSES,
)
from ..domain.roles import UserRole
from ..repository import collections as C
from ..repository.interface import CollaborationRepository


class AccessDenied(Exception):
    """Principal is not permitted to perform this action (-> 403)."""


class ChildNotFound(Exception):
    """Child not accessible to this principal (unknown OR unauthorized) (-> 404)."""


def principal_role(user: AuthenticatedUser) -> PrincipalRole:
    if user.role == UserRole.PARENT:
        return PrincipalRole.PARENT
    return PrincipalRole.THERAPIST


def resolve_therapist(repo: CollaborationRepository, user: AuthenticatedUser) -> dict:
    """Return the therapist profile for a therapist principal, or raise AccessDenied."""
    if principal_role(user) != PrincipalRole.THERAPIST:
        raise AccessDenied("Therapist role required.")
    matches = repo.query(C.THERAPIST_PROFILES, uid=user.uid)
    if not matches:
        raise AccessDenied("No therapist profile for this principal.")
    return matches[0]


def resolve_parent(repo: CollaborationRepository, user: AuthenticatedUser) -> dict:
    if principal_role(user) != PrincipalRole.PARENT:
        raise AccessDenied("Parent role required.")
    matches = repo.query(C.PARENT_PROFILES, uid=user.uid)
    if not matches:
        raise AccessDenied("No parent profile for this principal.")
    return matches[0]


def resolve_child_access(
    repo: CollaborationRepository, therapist_id: str, child_id: str
) -> Tuple[ChildAccessLevel, Optional[dict]]:
    """Resolve (access_level, connection) for a therapist→child pair.

    * active                      -> FULL
    * pending/paused/not-activated -> RESTRICTED (connection summary only)
    * no connection / ended       -> NONE  (caller raises ChildNotFound -> 404)
    """
    conns = repo.query(C.CONNECTIONS, therapist_id=therapist_id, child_id=child_id)
    if not conns:
        return ChildAccessLevel.NONE, None
    conn = conns[0]
    status = ConnectionStatus(conn["status"])
    if status == ConnectionStatus.ACTIVE:
        return ChildAccessLevel.FULL, conn
    if status in RESTRICTED_CONNECTION_STATUSES:
        return ChildAccessLevel.RESTRICTED, conn
    # ended (or anything else) -> existence-blind not-found
    return ChildAccessLevel.NONE, conn


def require_full_access(
    repo: CollaborationRepository, therapist_id: str, child_id: str
) -> dict:
    """Return the connection for FULL access, else raise ChildNotFound (404).

    RESTRICTED and NONE both raise ChildNotFound so restricted children never
    leak full-workspace content and unknown ids are indistinguishable.
    """
    level, conn = resolve_child_access(repo, therapist_id, child_id)
    if level != ChildAccessLevel.FULL or conn is None:
        raise ChildNotFound(child_id)
    return conn
