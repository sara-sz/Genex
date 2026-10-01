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
