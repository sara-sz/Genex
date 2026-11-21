"""pilot_backend/goals/errors.py — goal and planning failure types.

All PHI-safe by declaration: messages name the rule that failed and, at most,
an opaque application id. None carries goal text, an edit reason, a domain, or
anything a clinician typed.

Split the same way `identity/errors.py` is, and for the same reason: a
permission failure must never be reportable as a data problem, or the reverse.
`GoalConflict` maps to 409, `GoalAuthorizationError` to 403,
`GoalValidationError` to 400.
"""

from __future__ import annotations


class GoalConflict(Exception):
    """A uniqueness, lifecycle or ambiguity rule refused the write."""

    PHI_SAFE_MESSAGE = True


class SuccessorActivationPremature(GoalConflict):
    """P0-C renewal. Another active clinician-approved plan can still hold the
    child's current or upcoming week, so a successor may not be activated yet.

    Activation STARTS a plan's 28-day clinical period; activating early would
    burn it while the incumbent still governs. Prepare the successor as a
    draft and activate it once the incumbent can no longer hold the week.
    """


class ClinicalTimezoneMismatch(GoalConflict):
    """RTM billing-evidence v2. Every clinician-approved (activated) plan of a
    child must share one `timezone_of_record`.

    RTM periods take their timezone from a plan; that is only sound because no
    two approved plans of the child can disagree. A plan in another zone is
    refused at activation rather than converted, picked between, or discovered
    at month-end reporting time.
    """


class GoalAuthorizationError(Exception):
    """Authenticated, but not permitted this goal or planning operation."""

    PHI_SAFE_MESSAGE = True


class GoalValidationError(Exception):
    """Inputs are internally inconsistent — a goal for the wrong child, a
    version chain out of order, an allocation naming a closed goal."""

    PHI_SAFE_MESSAGE = True
