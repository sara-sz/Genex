"""pilot_backend/weekly/errors.py — weekly-layer failure types.

All PHI-safe by declaration: messages name the rule that failed and, at most,
an opaque application id. None carries observation text, activity
instructions, clinical rationale or goal text.

Split the way `identity/errors.py` and `goals/errors.py` are, so a permission
failure can never be reported as a data problem or the reverse.
`WeeklyConflict` maps to 409, `WeeklyAuthorizationError` to 403,
`WeeklyValidationError` to 400.
"""

from __future__ import annotations


class WeeklyConflict(Exception):
    """A uniqueness, lifecycle or ambiguity rule refused the write."""

    PHI_SAFE_MESSAGE = True


class WeeklyAuthorizationError(Exception):
    """Authenticated, but not permitted this weekly-layer operation."""

    PHI_SAFE_MESSAGE = True


class WeeklyValidationError(Exception):
    """Inputs are internally inconsistent — an observation for a cycle that
    belongs to another child, a goal outside the focus plan, a local date the
    cycle does not cover."""

    PHI_SAFE_MESSAGE = True


class ReleasedPlanImmutable(WeeklyConflict):
    """An attempt to change a plan the family has already been given.

    A distinct type because the correct response is specific and must not be
    confused with an ordinary conflict: record the intervention as INTENT
    against the released cycle, or target a future cycle instead. Rewriting a
    released `WeeklyPlanSnapshot` would make the plan the family is working
    from and the plan of record disagree, with nothing saying which is real.

    Proposal-and-acceptance wiring is 0.5.
    """


class InProgressStateMismatch(WeeklyConflict):
    """P0-B1 resume. Persisted partial state is not what THIS request would
    have written, so it is not resumed.

    Raised when an earlier attempt left an allocation, an adaptation record or
    decision, a cycle link or a snapshot that differs from the deterministic
    recomputation. Fail closed: resuming state we did not produce could release
    a week nobody approved, and repairing it would hide how it arose.
    """


class InProgressCapacityMismatch(InProgressStateMismatch):
    """A retry named a different family-declared capacity after the first
    allocation was persisted. The recorded capacity is the in-progress week's
    truth; a later request may not silently rewrite it."""


class OverlappingCycle(WeeklyConflict):
    """P0-C. The new cycle would share a local date with another cycle of the
    same child — under ANY plan. Two weeks covering one day would give a
    family two plans for that day and make "this week" ambiguous."""


class OutsideClinicalPeriod(WeeklyValidationError):
    """P0-C. A starter-anchored cycle does not fit inside its plan's clinical
    period (clinician activation + 28 local days). Renewal is a clinician
    decision; this layer refuses rather than extending an approval."""
