"""pilot_backend/domain/rtm.py — RTM episode, monitoring period, technology.

    RTMEpisode           a clinical episode. May span months.
    RTMMonitoringPeriod  ONE calendar month inside an episode.
    RTMTechnology        what the monitoring was performed with.

## The episode and the month are different things

A calendar month ending does NOT close an episode. Only an explicit clinician
action does. Finalizing a `RTMMonitoringPeriod` closes the MONTH; the episode
stays `OPEN` unless the clinician separately closes it.

They are separate records with separate ids and separate terminal actions
precisely so this cannot blur. An episode that auto-closed at month end would
silently end a course of treatment because a calendar page turned.

## A clinical goal is required, and a caregiver goal cannot stand in

An episode requires at least one `ClinicalGoal`. `CaregiverApprovedGoal` is a
real, valid goal that drives planning identically — and it is not a
clinician's treatment goal. `require_clinical_goal_ref` from 0.4B is the gate,
so a caregiver-approved goal cannot open an episode even if a caller passes
its reference: the two are different TYPES, and this is where that matters
most.

## Managing-clinician change does not transfer an open episode

October rule, deliberately simple: an open episode must not silently follow a
change of managing clinician. Continuing one under a different clinician fails
closed. The clinician must explicitly close the episode and open a new one.
Sophisticated transfer semantics are deferred rather than guessed at.

## Technology eligibility is UNRESOLVED and says so

`RegulatoryStatus` has exactly one member for October: `UNDER_REVIEW`. There
is no `FDA_APPROVED`, no `FDA_CLEARED`, no `CLASS_I` and no `ELIGIBLE_DEVICE`
— not as a default, but as an absent value that cannot be written. No object
or report in this slice may imply Genex meets RTM device eligibility, and the
coding layer turns this status into an explicit missing-requirement flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from .entities import SCHEMA_VERSION, utc_now
from .enums import Visibility
from .goals import GoalKind, GoalRef, require_clinical_goal_ref
from .ids import new_rtm_episode_id, new_rtm_period_id, new_rtm_technology_id
from .monthly_plan import validate_cycle_month, validate_timezone


class RTMError(ValueError):
    """Invalid RTM construction or transition. PHI-safe."""

    PHI_SAFE_MESSAGE = True


class EpisodeStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class PeriodStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    FINALIZED = "finalized"


class RegulatoryStatus(str, Enum):
    """Regulatory posture of the monitoring technology.

    ONE member for October. `FDA_APPROVED`, `FDA_CLEARED`, `CLASS_I` and
    `ELIGIBLE_DEVICE` are deliberately ABSENT rather than unused: a value that
    does not exist cannot be set by a caller, a migration or a typo, and
    nothing in this slice may imply device eligibility that has not been
    established.
    """

    UNDER_REVIEW = "under_review"


@dataclass(frozen=True)
class RTMEpisode:
    """A clinical RTM episode. Opened and closed only by a clinician."""

    episode_id: str
    child_id: str
    managing_provider_id: str
    practice_id: str
    #: Clinical goals only. Stored as (kind, id) pairs; the kind is retained
    #: so a reader never has to assume which sort of goal this is.
    clinical_goal_refs: Tuple[Tuple[str, str], ...]
    opened_at: datetime
    opened_by_actor_id: str
    #: The managing-clinician assignment that authorised opening, for audit
    #: and for the no-silent-transfer rule.
    managing_assignment_id: str = ""
    status: EpisodeStatus = EpisodeStatus.OPEN
    closed_at: Optional[datetime] = None
    closed_by_actor_id: Optional[str] = None
    close_reason: str = ""
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.THERAPIST_ONLY

    def __post_init__(self) -> None:
        if not self.clinical_goal_refs:
            raise RTMError("an RTM episode requires at least one clinical goal")
        object.__setattr__(self, "clinical_goal_refs",
                           tuple(self.clinical_goal_refs))
        for kind, goal_id in self.clinical_goal_refs:
            if kind != GoalKind.CLINICAL.value:
                raise RTMError(
                    "an RTM episode accepts clinician-approved goals only")
            if not (goal_id or "").strip():
                raise RTMError("a goal reference requires a goal id")
        for label, value in (("child_id", self.child_id),
                             ("managing_provider_id", self.managing_provider_id),
                             ("practice_id", self.practice_id),
                             ("opened_by_actor_id", self.opened_by_actor_id)):
            if not (value or "").strip():
                raise RTMError(f"an RTM episode requires {label}")

    @property
    def is_open(self) -> bool:
        """Open means the STATUS says so. Not merely "no closed timestamp".

        The 0.4A/0.4C mutation lesson: `close()` always sets both, so a
        timestamp-only check looks sufficient against records this code wrote
        and fails on a stored document carrying a terminal status with a null
        timestamp.
        """
        return self.status is EpisodeStatus.OPEN and self.closed_at is None

    @property
    def goal_refs(self) -> Tuple[GoalRef, ...]:
        return tuple(GoalRef(GoalKind(kind), goal_id)
                     for kind, goal_id in self.clinical_goal_refs)

    @staticmethod
    def open(child_id: str, provider_id: str, practice_id: str,
             goal_refs: Tuple[GoalRef, ...], *, actor_id: str,
             managing_assignment_id: str = "",
             now: Optional[datetime] = None) -> "RTMEpisode":
        """Open an episode. Every goal must be clinician-approved."""
        if not goal_refs:
            raise RTMError("an RTM episode requires at least one clinical goal")
        for ref in goal_refs:
            require_clinical_goal_ref(ref)
        stamp = now or utc_now()
        return RTMEpisode(
            episode_id=new_rtm_episode_id(),
            child_id=child_id,
            managing_provider_id=provider_id,
            practice_id=practice_id,
            clinical_goal_refs=tuple((r.kind.value, r.goal_id)
                                     for r in goal_refs),
            opened_at=stamp,
            opened_by_actor_id=actor_id,
            managing_assignment_id=managing_assignment_id,
            created_at=stamp, updated_at=stamp,
        )

    def close(self, *, actor_id: str, reason: str,
              now: Optional[datetime] = None) -> "RTMEpisode":
        """Close the episode. Explicit clinician action only.

        A reason is required: an episode ending is a clinical event, and one
        with no stated reason is unreviewable.
        """
        if not self.is_open:
            raise RTMError("this episode is already closed")
        if not (reason or "").strip():
            raise RTMError("closing an episode requires a stated reason")
        stamp = now or utc_now()
        return replace(self, status=EpisodeStatus.CLOSED, closed_at=stamp,
                       closed_by_actor_id=actor_id, close_reason=reason,
                       updated_at=stamp)


@dataclass(frozen=True)
class RTMMonitoringPeriod:
    """One calendar month of an episode. Finalizing it does not close it."""

    period_id: str
    episode_id: str
    child_id: str
    focus_plan_id: str
    cycle_month: str
    #: Snapshotted from the MonthlyFocusPlan. Validated, never defaulted.
    timezone_of_record: str
    status: PeriodStatus = PeriodStatus.DRAFT
    started_at: datetime = field(default_factory=utc_now)
    activated_at: Optional[datetime] = None
    finalized_at: Optional[datetime] = None
    finalized_by_actor_id: Optional[str] = None
    #: Deterministic claim proving one active/finalized period per
    #: (episode, cycle_month) — the 0.4A write-time mechanism, reused.
    uniqueness_claim_id: str = ""
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.THERAPIST_ONLY

    def __post_init__(self) -> None:
        object.__setattr__(self, "cycle_month",
                           validate_cycle_month(self.cycle_month))
        object.__setattr__(self, "timezone_of_record",
                           validate_timezone(self.timezone_of_record))
        for label, value in (("episode_id", self.episode_id),
                             ("child_id", self.child_id),
                             ("focus_plan_id", self.focus_plan_id)):
            if not (value or "").strip():
                raise RTMError(f"a monitoring period requires {label}")

    @property
    def is_finalized(self) -> bool:
        return self.status is PeriodStatus.FINALIZED

    @property
    def is_open_for_evidence(self) -> bool:
        """DRAFT and ACTIVE accept evidence; FINALIZED does not."""
        return self.status in (PeriodStatus.DRAFT, PeriodStatus.ACTIVE)

    @staticmethod
    def create(episode_id: str, child_id: str, focus_plan_id: str,
               cycle_month: str, timezone_of_record: str, *,
               now: Optional[datetime] = None) -> "RTMMonitoringPeriod":
        stamp = now or utc_now()
        return RTMMonitoringPeriod(
            period_id=new_rtm_period_id(),
            episode_id=episode_id,
            child_id=child_id,
            focus_plan_id=focus_plan_id,
            cycle_month=cycle_month,
            timezone_of_record=timezone_of_record,
            started_at=stamp, created_at=stamp, updated_at=stamp,
        )

    def activate(self, *, claim_id: str = "",
                 now: Optional[datetime] = None) -> "RTMMonitoringPeriod":
        if self.status is not PeriodStatus.DRAFT:
            raise RTMError("only a draft period can be activated")
        stamp = now or utc_now()
        return replace(self, status=PeriodStatus.ACTIVE, activated_at=stamp,
                       uniqueness_claim_id=claim_id or self.uniqueness_claim_id,
                       updated_at=stamp)

    def finalize(self, *, actor_id: str,
                 now: Optional[datetime] = None) -> "RTMMonitoringPeriod":
        """Close the MONTH. The episode is untouched by design."""
        if self.status is PeriodStatus.FINALIZED:
            raise RTMError("this period is already finalized")
        if self.status is not PeriodStatus.ACTIVE:
            raise RTMError("only an active period can be finalized")
        stamp = now or utc_now()
        return replace(self, status=PeriodStatus.FINALIZED, finalized_at=stamp,
                       finalized_by_actor_id=actor_id, updated_at=stamp)


@dataclass(frozen=True)
class RTMTechnology:
    """What the monitoring was performed with, and its regulatory posture.

    Attached to the EPISODE rather than the period: the technology does not
    change because a month ended, and recording it per month would invite two
    months of one episode to disagree about what was used.
    """

    technology_id: str
    episode_id: str
    child_id: str
    product_descriptor: str
    regulatory_status: RegulatoryStatus = RegulatoryStatus.UNDER_REVIEW
    technology_version: str = ""
    attestation_ref: str = ""
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    #: Append-only revision lineage, so a later attestation does not erase
    #: what the record said when a month was finalized under it.
    supersedes_technology_id: Optional[str] = None
    superseded_by_technology_id: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.THERAPIST_ONLY

    def __post_init__(self) -> None:
        if not isinstance(self.regulatory_status, RegulatoryStatus):
            raise RTMError("regulatory_status must be a RegulatoryStatus")
        if not (self.product_descriptor or "").strip():
            raise RTMError("a technology record requires a product descriptor")

    @property
    def eligibility_established(self) -> bool:
        """Always False in October. There is no status that would make it True.

        A property rather than a literal so the coding layer reads an
        intention rather than hard-coding a comparison that would silently
        start returning True if a status were ever added without review.
        """
        return self.regulatory_status not in (RegulatoryStatus.UNDER_REVIEW,)

    @staticmethod
    def declare(episode_id: str, child_id: str, product_descriptor: str, *,
                technology_version: str = "", attestation_ref: str = "",
                supersedes_technology_id: Optional[str] = None,
                now: Optional[datetime] = None) -> "RTMTechnology":
        stamp = now or utc_now()
        return RTMTechnology(
            technology_id=new_rtm_technology_id(),
            episode_id=episode_id,
            child_id=child_id,
            product_descriptor=product_descriptor,
            regulatory_status=RegulatoryStatus.UNDER_REVIEW,
            technology_version=technology_version,
            attestation_ref=attestation_ref,
            supersedes_technology_id=supersedes_technology_id,
            created_at=stamp, updated_at=stamp,
        )

    def with_successor(self, successor_id: str, *,
                       now: Optional[datetime] = None) -> "RTMTechnology":
        return replace(self, superseded_by_technology_id=successor_id,
                       updated_at=now or utc_now())
