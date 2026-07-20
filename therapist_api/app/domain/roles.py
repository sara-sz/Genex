"""User roles.

A role is resolved SERVER-SIDE from a verified identity + a backend allowlist /
role record. The front-end role-selection screen is NEVER authorization — it is
a UI convenience only. See app/authz for enforcement.
"""

from __future__ import annotations

from enum import Enum


class UserRole(str, Enum):
    PARENT = "parent"
    SLP = "slp"  # SLP-first; OT/PT/MD/educator roles added later.


VALID_ROLES = frozenset(r.value for r in UserRole)


def is_valid_role(value: str) -> bool:
    return value in VALID_ROLES
