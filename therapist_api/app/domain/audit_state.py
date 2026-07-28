"""Canonical structured before/after state for audit events (Phase 1B.2A.2).

An audit event must let a reader reconstruct exactly what changed on a plan
assignment WITHOUT re-reading the mutated record. A bare status string cannot do
that: it cannot show that the assignment stayed current, kept its activity
version, and moved forward exactly one version.

Every write operation therefore records the SAME assignment-state shape on both
sides of the change, so `before_state` / `after_state` are directly comparable
key-by-key. Operation-specific facts (e.g. the proposal a modify attached) are
added as extra keys on the side where they exist.

The output is plain JSON-compatible data — enums are coerced to their values —
because audit records are written verbatim to the store and must survive a
Firestore round-trip unchanged.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional

# The assignment fields captured on BOTH sides of every plan-assignment write.
ASSIGNMENT_STATE_KEYS = (
    "assignment_version",
    "pending_proposal_id",
    "plan_approval_status",
    "assignment_status",
    "current_activity_version_id",
)


def plain(value: Any) -> Any:
    """Coerce enum members to their JSON-safe primitive value."""
    return value.value if isinstance(value, Enum) else value


def assignment_state(assignment: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    """Structured audit state for one plan assignment.

    `assignment` is the raw stored record. Pass `extra` for operation-specific
    facts (e.g. `proposal_id`, `proposed_activity_version_id`). Call it BEFORE
    mutating the record for `before_state`, and again AFTER for `after_state`.
    """
    state: Dict[str, Any] = {
        "assignment_version": int(assignment["version"]),
        "pending_proposal_id": plain(assignment.get("pending_proposal_id")),
        "plan_approval_status": plain(assignment["plan_approval_status"]),
        "assignment_status": plain(assignment["assignment_status"]),
        "current_activity_version_id": plain(assignment["activity_version_id"]),
    }
    state.update({k: plain(v) for k, v in extra.items()})
    return state


def assignment_unchanged_except_version(
    before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]
) -> bool:
    """True when the assignment kept its status and activity version, +1 version.

    Expresses the Phase 1B.2A invariant for a modify proposal: the original
    assignment remains active and current, nothing was replaced, and the version
    advanced exactly once.
    """
    if not before or not after:
        return False
    return (
        after["assignment_version"] == before["assignment_version"] + 1
        and after["plan_approval_status"] == before["plan_approval_status"]
        and after["assignment_status"] == before["assignment_status"]
        and after["current_activity_version_id"] == before["current_activity_version_id"]
    )
