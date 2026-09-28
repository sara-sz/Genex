"""pilot_backend/authz/decisions.py — the vocabulary of a refusal.

## 401 versus 403, decided once

    401  we do not know who you are
         missing token, malformed header, invalid signature, expired, revoked

    403  we know who you are, and you may not have this
         no application record, inactive account, no relationship to the child,
         ended or revoked relationship, role not permitted

Mapping each denial reason to its status here — rather than at each call site —
is what keeps the distinction honest. The common failure is returning 403 for
an expired token (telling a client to re-authenticate is impossible) or 401 for
a permission failure (inviting a credential retry loop that will never work).

## Denial reasons are internal

`Denial` is for audit and tests. It is deliberately NOT the response body: a
caller learns "403", not "that child exists but you are not connected to them".
`AccessDecision.public_reason` is the only caller-facing text, and it is
constant per status code, so responses cannot become an existence oracle for
child ids.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..auth.resolver import Principal

HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403


class Denial(str, Enum):
    """Why access was refused. Internal — audit and tests only."""

    # -- authentication (401) --
    NO_TOKEN = "no_token"
    INVALID_TOKEN = "invalid_token"
    REVOKED_TOKEN = "revoked_token"

    # -- authorization (403) --
    NO_APPLICATION_RECORD = "no_application_record"
    INACTIVE_ACTOR = "inactive_actor"
    UNKNOWN_CHILD = "unknown_child"
    INACTIVE_CHILD = "inactive_child"
    NO_RELATIONSHIP = "no_relationship"
    INACTIVE_RELATIONSHIP = "inactive_relationship"
    ROLE_NOT_PERMITTED = "role_not_permitted"


#: The single source of truth for which refusals are 401 and which are 403.
_AUTHENTICATION_DENIALS = frozenset({
    Denial.NO_TOKEN, Denial.INVALID_TOKEN, Denial.REVOKED_TOKEN,
})


def status_for(denial: Denial) -> int:
    return HTTP_UNAUTHORIZED if denial in _AUTHENTICATION_DENIALS else HTTP_FORBIDDEN


@dataclass(frozen=True)
class AccessDecision:
    """The outcome of one access check. Deny-by-default by construction.

    There is no public constructor that produces an allow without a principal
    and a child id, so an uninitialised or partially built decision cannot read
    as permission.
    """

    allowed: bool
    status_code: int
    denial: Optional[Denial] = None
    principal: Optional[Principal] = None
    child_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.allowed:
            if self.denial is not None:
                raise ValueError("an allowed decision cannot carry a denial reason")
            if self.status_code != HTTP_OK:
                raise ValueError("an allowed decision must be 200")
            if self.principal is None:
                raise ValueError("an allowed decision must name the principal it allows")
        else:
            if self.denial is None:
                raise ValueError("a denied decision must state why")
            if self.status_code != status_for(self.denial):
                raise ValueError(
                    f"{self.denial.value} must be {status_for(self.denial)}, "
                    f"got {self.status_code}")

    @property
    def public_reason(self) -> str:
        """Caller-facing text. Constant per status — never resource-specific."""
        if self.allowed:
            return "ok"
        if self.status_code == HTTP_UNAUTHORIZED:
            return "authentication required"
        return "not permitted"

    # -- constructors -------------------------------------------------------

    @staticmethod
    def allow(principal: Principal, child_id: str) -> "AccessDecision":
        return AccessDecision(
            allowed=True, status_code=HTTP_OK, principal=principal, child_id=child_id)

    @staticmethod
    def deny(denial: Denial, *, principal: Optional[Principal] = None,
             child_id: Optional[str] = None) -> "AccessDecision":
        return AccessDecision(
            allowed=False,
            status_code=status_for(denial),
            denial=denial,
            principal=principal,
            child_id=child_id,
        )
