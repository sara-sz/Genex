"""pilot_backend/domain/roles.py — who an authenticated actor is, application-side.

A role is DERIVED, never submitted. It is the answer to "which application
record did this verified auth subject resolve to?", so it cannot be chosen by
the caller. Nothing accepts a role from a request body, header or token claim.

Kept in the domain layer (not in `auth`) because authorization, audit events and
later clinical records all speak this vocabulary, and none of them should have
to import the authentication package to name a role.
"""

from __future__ import annotations

from enum import Enum


class ActorRole(str, Enum):
    """The application roles the pilot recognises.

    There is no ADMIN role in 0.2. Administrative capability is a real
    requirement, but granting it needs an approved break-glass and audit story;
    an unused ADMIN constant is an invitation to check it in somewhere first.
    """

    CAREGIVER = "caregiver"
    PROVIDER = "provider"


#: Roles that may hold a relationship to a child. Both current roles qualify;
#: the constant exists so a future non-relational role (billing, support) is a
#: deliberate addition here rather than an accidental grant in `authz`.
CHILD_RELATED_ROLES = frozenset({ActorRole.CAREGIVER, ActorRole.PROVIDER})
