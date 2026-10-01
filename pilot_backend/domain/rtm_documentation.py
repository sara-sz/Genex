"""pilot_backend/domain/rtm_documentation.py — what the clinician documented.

    TherapistReview         clinician interpretation of evidence. Immutable.
    ClinicalAction          what the clinician decided to do. Immutable.
    TimeEntry               MANUALLY entered minutes. Immutable + superseding.
    SynchronousInteraction  a real-time contact that actually happened.

## Review is not intervention

`TherapistIntervention` (0.4D/E) CHANGES planning. `TherapistReview`
DOCUMENTS that a clinician looked at evidence and what they made of it. A
review may exist with no intervention at all — "I reviewed this month and the
plan should continue" is a complete clinical act, and a model that required an
intervention to record a review would push clinicians into making changes they
did not intend.

Genex does not generate `clinical_interpretation`. It is clinician-authored,
required, and never produced by a rule or a model.

## Time is typed by a human, never measured

`entry_method` has exactly one member: `MANUAL`. There is no timer, no
page-view inference, no click-time inference and no estimate. The enum is
single-valued on purpose — an automatic source cannot be recorded because
there is no value that would represent one, and a test asserts the enum stays
that way.

Corrections never destroy history. A correction is a NEW entry that supersedes
its predecessor with a stated reason; the original row remains. Monthly totals
count only entries nothing supersedes, so a correction cannot double-count.

## Synchronous is a category, not an adjective

`SynchronousInteraction` records real-time contact that actually happened:
phone, video, in person, or another modality explicitly recorded as real-time.

A message is not one. A parent note is not one. A private therapist note is
not one. A plan review is not one. None of those has a constructor path into
this type, and `counts_as_real_time_communication` requires both a
synchronous modality AND a participant who is the patient or caregiver — so a
real-time call with a colleague does not satisfy a requirement about
communicating with the family.

Duration is optional and manual. It is never inferred.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import Enum
from typing import Optional, Tuple

from .entities import SCHEMA_VERSION, utc_now
from .enums import Visibility
from .ids import (
    new_clinical_action_id,
    new_synchronous_interaction_id,
    new_therapist_review_id,
    new_time_entry_id,
)
from .monthly_plan import validate_timezone
from .observation import attribution_month_for


class DocumentationError(ValueError):
    """Invalid clinical documentation. PHI-safe: names the rule, never text."""

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class TherapistReview:
    """A clinician reviewed this month's evidence. Immutable."""

    review_id: str
    period_id: str
    child_id: str
    provider_id: str
    #: Clinician-authored. Genex never generates this.
    clinical_interpretation: str
    reviewed_event_ids: Tuple[str, ...] = ()
    reviewed_cycle_ids: Tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.THERAPIST_ONLY

    #: A review documents; it does not change the plan. Read by reporting so
    #: a review is never presented as a planning change.
    changes_planning = False

    def __post_init__(self) -> None:
        if not (self.clinical_interpretation or "").strip():
            raise DocumentationError(
                "a therapist review requires clinician-authored interpretation")
        for label, value in (("period_id", self.period_id),
                             ("child_id", self.child_id),
                             ("provider_id", self.provider_id)):
            if not (value or "").strip():
                raise DocumentationError(f"a review requires {label}")
        object.__setattr__(self, "reviewed_event_ids",
                           tuple(self.reviewed_event_ids))
        object.__setattr__(self, "reviewed_cycle_ids",
                           tuple(self.reviewed_cycle_ids))

    @staticmethod
    def create(period_id: str, child_id: str, provider_id: str, *,
               clinical_interpretation: str,
               reviewed_event_ids: Tuple[str, ...] = (),
               reviewed_cycle_ids: Tuple[str, ...] = (),
               now: Optional[datetime] = None) -> "TherapistReview":
        return TherapistReview(
            review_id=new_therapist_review_id(),
            period_id=period_id,
            child_id=child_id,
            provider_id=provider_id,
            clinical_interpretation=clinical_interpretation,
            reviewed_event_ids=tuple(sorted(reviewed_event_ids)),
            reviewed_cycle_ids=tuple(sorted(reviewed_cycle_ids)),
            created_at=now or utc_now(),
        )


class ClinicalActionType(str, Enum):
    """What the clinician decided to do.

    A narrow documentation vocabulary. The enum is NOT evidence of medical
    necessity and nothing downstream may treat it as such — it records a
    decision, and whether that decision was necessary is a clinical judgement
    this system does not make.
    """

    CONTINUE_PLAN = "continue_plan"
    MODIFY_PLAN = "modify_plan"
    PROGRESS_PLAN = "progress_plan"
    CHANGE_GOAL = "change_goal"
    CONTACT_CAREGIVER = "contact_caregiver"
    EDUCATION_OR_COACHING = "education_or_coaching"
    OTHER_CLINICAL_ACTION = "other_clinical_action"


@dataclass(frozen=True)
class ClinicalAction:
    """One documented clinician decision. Immutable."""

    action_id: str
    review_id: str
    period_id: str
    child_id: str
    provider_id: str
    action_type: ClinicalActionType
    #: Clinician-authored clinical content. Never enters audit metadata.
    narrative: str
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.THERAPIST_ONLY

    #: An action type is a documentation category, never a necessity claim.
    establishes_medical_necessity = False

    def __post_init__(self) -> None:
        if not isinstance(self.action_type, ClinicalActionType):
            raise DocumentationError("action_type must be a ClinicalActionType")
        if not (self.narrative or "").strip():
            raise DocumentationError("a clinical action requires a narrative")

    @staticmethod
    def create(review_id: str, period_id: str, child_id: str,
               provider_id: str, *, action_type: ClinicalActionType,
               narrative: str,
               now: Optional[datetime] = None) -> "ClinicalAction":
        return ClinicalAction(
            action_id=new_clinical_action_id(),
            review_id=review_id,
            period_id=period_id,
            child_id=child_id,
            provider_id=provider_id,
            action_type=action_type,
            narrative=narrative,
            created_at=now or utc_now(),
        )


class TimeEntryMethod(str, Enum):
    """How the minutes were captured.

    EXACTLY ONE member, permanently. There is no AUTOMATIC, no TIMER, no
    INFERRED and no ESTIMATED — an automatically measured time source cannot
    be recorded because no value exists to represent one. A test asserts the
    enum has not grown.
    """

    MANUAL = "manual"


@dataclass(frozen=True)
class TimeEntry:
    """Treatment-management minutes, typed by a clinician. Immutable."""

    time_entry_id: str
    period_id: str
    child_id: str
    provider_id: str
    local_date: str
    timezone_of_record: str
    minutes: int
    #: Clinician-authored. Never enters audit metadata.
    activity_description: str
    entered_at: datetime = field(default_factory=utc_now)
    entry_method: TimeEntryMethod = TimeEntryMethod.MANUAL
    source_review_id: Optional[str] = None
    source_action_id: Optional[str] = None
    #: Correction lineage. The predecessor row is RETAINED; totals skip it.
    supersedes_time_entry_id: Optional[str] = None
    superseded_by_time_entry_id: Optional[str] = None
    correction_reason: str = ""
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.THERAPIST_ONLY

    def __post_init__(self) -> None:
        if isinstance(self.minutes, bool) or not isinstance(self.minutes, int):
            raise DocumentationError("minutes must be a whole number")
        if self.minutes <= 0:
            raise DocumentationError("minutes must be positive")
        if self.entry_method is not TimeEntryMethod.MANUAL:
            raise DocumentationError("time must be entered manually")
        try:
            date.fromisoformat(self.local_date)
        except ValueError:
            raise DocumentationError("local_date must be an ISO date") from None
        object.__setattr__(self, "timezone_of_record",
                           validate_timezone(self.timezone_of_record))
        if (self.supersedes_time_entry_id
                and not (self.correction_reason or "").strip()):
            raise DocumentationError(
                "a correcting time entry requires a stated reason")

    @property
    def attribution_month(self) -> str:
        return attribution_month_for(self.local_date)

    @property
    def is_current(self) -> bool:
        """Whether this entry still counts. Superseded entries do not."""
        return self.superseded_by_time_entry_id is None

    @staticmethod
    def record(period_id: str, child_id: str, provider_id: str, *,
               local_date: str, timezone_of_record: str, minutes: int,
               activity_description: str,
               source_review_id: Optional[str] = None,
               source_action_id: Optional[str] = None,
               supersedes_time_entry_id: Optional[str] = None,
               correction_reason: str = "",
               now: Optional[datetime] = None) -> "TimeEntry":
        return TimeEntry(
            time_entry_id=new_time_entry_id(),
            period_id=period_id,
            child_id=child_id,
            provider_id=provider_id,
            local_date=local_date,
            timezone_of_record=timezone_of_record,
            minutes=minutes,
            activity_description=activity_description,
            entered_at=now or utc_now(),
            entry_method=TimeEntryMethod.MANUAL,
            source_review_id=source_review_id,
            source_action_id=source_action_id,
            supersedes_time_entry_id=supersedes_time_entry_id,
            correction_reason=correction_reason,
        )

    def with_successor(self, successor_id: str) -> "TimeEntry":
        """Mark this entry corrected. The row is retained, never edited away."""
        return replace(self, superseded_by_time_entry_id=successor_id)


class InteractionModality(str, Enum):
    """How the real-time contact happened.

    Every member is synchronous by definition. There is no MESSAGE, no EMAIL
    and no NOTE member — an asynchronous exchange has no representable value
    here, so it cannot be recorded as a synchronous interaction by mistake.
    """

    PHONE = "phone"
    VIDEO = "video"
    IN_PERSON = "in_person"
    #: Another genuinely real-time modality. Requires explicit affirmation
    #: that it was real-time — see `__post_init__`.
    OTHER_SYNCHRONOUS = "other_synchronous"


class ParticipantType(str, Enum):
    PATIENT = "patient"
    CAREGIVER = "caregiver"
    BOTH = "both"


@dataclass(frozen=True)
class SynchronousInteraction:
    """A real-time contact that actually happened. Immutable."""

    interaction_id: str
    period_id: str
    child_id: str
    provider_id: str
    occurred_at_utc: datetime
    local_date: str
    timezone_of_record: str
    modality: InteractionModality
    participant_type: ParticipantType
    #: Manual and OPTIONAL. Never inferred from anything.
    duration_minutes: Optional[int] = None
    #: An explicit human affirmation that this was real-time communication.
    #: Required for OTHER_SYNCHRONOUS, where the modality alone does not say.
    real_time_affirmed: bool = True
    note_ref: str = ""
    entered_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.THERAPIST_ONLY

    def __post_init__(self) -> None:
        if not isinstance(self.modality, InteractionModality):
            raise DocumentationError("modality must be an InteractionModality")
        if not isinstance(self.participant_type, ParticipantType):
            raise DocumentationError("participant_type must be a ParticipantType")
        if self.occurred_at_utc.tzinfo is None:
            raise DocumentationError("refusing a naive interaction timestamp")
        if self.duration_minutes is not None:
            if (isinstance(self.duration_minutes, bool)
                    or not isinstance(self.duration_minutes, int)):
                raise DocumentationError("duration must be a whole number")
            if self.duration_minutes <= 0:
                raise DocumentationError("duration must be positive when given")
        if (self.modality is InteractionModality.OTHER_SYNCHRONOUS
                and not self.real_time_affirmed):
            raise DocumentationError(
                "OTHER_SYNCHRONOUS requires explicit real-time affirmation")
        object.__setattr__(self, "timezone_of_record",
                           validate_timezone(self.timezone_of_record))
        try:
            date.fromisoformat(self.local_date)
        except ValueError:
            raise DocumentationError("local_date must be an ISO date") from None

    @property
    def attribution_month(self) -> str:
        return attribution_month_for(self.local_date)

    @property
    def counts_as_real_time_communication(self) -> bool:
        """Whether this satisfies "real-time communication with the family".

        Requires a synchronous modality, an explicit real-time affirmation,
        AND a participant who is the patient or caregiver. A real-time call
        with a colleague is a real event and does not satisfy a requirement
        about communicating with the family.
        """
        return (self.real_time_affirmed
                and self.participant_type in (ParticipantType.PATIENT,
                                              ParticipantType.CAREGIVER,
                                              ParticipantType.BOTH))

    @staticmethod
    def record(period_id: str, child_id: str, provider_id: str, *,
               occurred_at_utc: datetime, local_date: str,
               timezone_of_record: str, modality: InteractionModality,
               participant_type: ParticipantType,
               duration_minutes: Optional[int] = None,
               real_time_affirmed: bool = True, note_ref: str = "",
               now: Optional[datetime] = None) -> "SynchronousInteraction":
        return SynchronousInteraction(
            interaction_id=new_synchronous_interaction_id(),
            period_id=period_id,
            child_id=child_id,
            provider_id=provider_id,
            occurred_at_utc=occurred_at_utc,
            local_date=local_date,
            timezone_of_record=timezone_of_record,
            modality=modality,
            participant_type=participant_type,
            duration_minutes=duration_minutes,
            real_time_affirmed=real_time_affirmed,
            note_ref=note_ref,
            entered_at=now or utc_now(),
        )


def documented_minutes(entries) -> int:
    """Monthly treatment-management minutes from the CURRENT entries.

    Counts only entries nothing supersedes. A correction adds a row and marks
    its predecessor, so summing everything would double-count exactly the
    minutes a clinician was trying to fix — and that is the number the coding
    rules read.
    """
    return sum(entry.minutes for entry in entries if entry.is_current)
