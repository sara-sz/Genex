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


class EpisodeClosedForAccrual(RTMConflict):
    """The episode is closed, so no further RTM evidence or time may accrue.

    Track 3 slice 3A. Before this existed, `open_period` refused a closed
    episode a NEW month but nothing refused further writes into a month that
    was already open — so a closed episode kept accruing clinician minutes,
    reviews, actions and real-time contacts. The episode was loaded on that
    path and its status discarded.

    A distinct type because the correct response differs from every other
    conflict here. `FinalizedRecordImmutable` says "this month is done, amend
    instead"; this says "this course of treatment is over, and there is no
    amendment that makes new evidence belong to it". Reopening is not a thing:
    the clinician opens a NEW episode, which is the same shape as the
    no-silent-transfer rule above.

    ## WHAT IT DELIBERATELY DOES NOT STOP

    Closure ends RTM ACCRUAL, not the family's use of Genex. It does not touch
    account access, caregiver activity or observation recording, reading
    already-recorded evidence, or generating, finalizing and amending the final
    month's report. An episode that could not be reported on after closing
    would make the last month of every course of treatment undocumentable.
    """


class RTMPrerequisitesIncomplete(RTMConflict):
    """Monitoring cannot begin: enrollment, consent or setup is missing.

    Track 3 slice 3C. The pilot order is enrollment metadata -> caregiver
    consent -> setup/education -> monitoring, and this is what refuses the
    last step when the first three are not all in place.

    Distinct from `EpisodeClosedForAccrual`, which says a course of treatment
    has ENDED. This one says it has not properly BEGUN, and the remedy is the
    opposite: supply the missing evidence and open the month again.

    ## IT REFUSES RTM MONITORING, NOT THE PRODUCT

    An episode that is not RTM-ready blocks RTM months and nothing else. The
    family keeps using Genex — plans, activities, observations, reads and
    account access are all untouched, because none of them consults readiness.
    RTM readiness and Genex access are separate states by design.

    The message names WHICH prerequisites are missing, as short PHI-free
    reason codes, because a refusal a clinician cannot act on is a support
    ticket rather than a gate.
    """
