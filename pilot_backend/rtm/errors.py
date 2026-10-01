"""pilot_backend/rtm/errors.py — RTM-layer failure types.

All PHI-safe by declaration: messages name the rule that failed and, at most,
an opaque application id. None carries clinical interpretation, an action
narrative, an activity description, observation text or goal text.

Split the way every prior slice's errors are, so a permission failure can
never be reported as a data problem or the reverse. `RTMConflict` maps to 409,
`RTMAuthorizationError` to 403, `RTMValidationError` to 400.
"""

from __future__ import annotations


class RTMConflict(Exception):
    """A uniqueness, lifecycle or ambiguity rule refused the write."""

    PHI_SAFE_MESSAGE = True


class RTMAuthorizationError(Exception):
    """Authenticated, but not permitted this RTM operation."""

    PHI_SAFE_MESSAGE = True


class RTMValidationError(Exception):
    """Inputs are internally inconsistent — a period naming another child's
    focus plan, evidence from outside the month, a goal that is not clinical."""

    PHI_SAFE_MESSAGE = True


class FinalizedRecordImmutable(RTMConflict):
    """An attempt to change a finalized period or report in place.

    A distinct type because the correct response is specific: amend, with a
    reason and lineage. A finalized report is a clinical document someone may
    have read or exported, and editing it in place would make two readers of
    "the October report" disagree with no way to tell which was which.
    """


class EpisodeTransferRefused(RTMConflict):
    """An open episode cannot follow a change of managing clinician.

    October rule, deliberately simple: close the episode explicitly and open a
    new one under the new clinician. Sophisticated transfer semantics are
    deferred rather than guessed at, and guessing here would silently move a
    course of treatment between clinicians.
    """
