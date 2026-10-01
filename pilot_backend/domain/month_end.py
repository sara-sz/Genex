"""pilot_backend/domain/month_end.py — the monthly report, with lineage.

    MonthEndReport  DRAFT -> FINALIZED -> (AMENDED successor)

## No silent rewrite after finalization

A finalized report is a clinical document someone may have read, exported or
acted on. Changing it in place would make two readers of "the October report"
disagree with no way to tell which was which.

So amendment writes a SUCCESSOR with a stated reason, an actor, a timestamp
and lineage both ways. The predecessor keeps its content and gains a forward
pointer. This is the same append-only shape as the 0.4A assignment chain and
the 0.4B goal-version chain, for the same reason.

## Sections are references, not copies of clinical text

A section holds counts and REFERENCES — event ids, review ids, action ids,
summary ids. It does not copy observation prose, clinical interpretation,
action narrative or activity instructions into the report record. The report
is an index over evidence that already exists; rendering it for a human is a
presentation concern, and keeping the text out of this record keeps the
report safe to count, query and audit.

Section C carries the overlap explicitly, because per-goal attributed counts
must never be summed into a total.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from .entities import SCHEMA_VERSION, utc_now
from .enums import Visibility
from .ids import new_month_end_report_id


class ReportError(ValueError):
    """Invalid report construction or transition. PHI-safe."""

    PHI_SAFE_MESSAGE = True


class ReportState(str, Enum):
    DRAFT = "draft"
    FINALIZED = "finalized"
    AMENDED = "amended"


class ReportSection(str, Enum):
    """The sections of the month-end report, A through K."""

    MONTHLY_GOALS = "a_monthly_goals"
    ENGAGEMENT = "b_engagement_home_practice"
    GOAL_ATTRIBUTION = "c_goal_attribution"
    PARENT_OBSERVATIONS = "d_parent_observations"
    WEEKLY_ADAPTATIONS = "e_weekly_adaptations"
    THERAPIST_REVIEW = "f_therapist_review"
    CLINICAL_ACTIONS = "g_clinical_actions"
    DOCUMENTED_TIME = "h_documented_therapist_time"
    SYNCHRONOUS_INTERACTIONS = "i_synchronous_interactions"
    RTM_EVIDENCE = "j_rtm_evidence_summary"
    CODING_ASSISTANCE = "k_coding_assistance"


@dataclass(frozen=True)
class SectionContent:
    """One section: counts and references, never clinical prose.

    `attributed_to_role` exists for section D. A parent observation is the
    caregiver's, and a report that restated it as a clinician finding would
    misattribute the only part of the month the family contributed.
    """

    section: ReportSection
    #: Opaque ids of the records this section indexes.
    record_refs: Tuple[str, ...] = ()
    #: Small, non-clinical integers: counts, minutes, units.
    counts: Tuple[Tuple[str, int], ...] = ()
    #: Short enum-valued facts, e.g. a goal's status recommendation.
    labels: Tuple[Tuple[str, str], ...] = ()
    attributed_to_role: str = ""
    #: Set on section C. Per-goal counts overlap and must not be summed.
    overlap_declared: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.section, ReportSection):
            raise ReportError("section must be a ReportSection")
        for name in ("record_refs", "counts", "labels"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    def count(self, key: str) -> Optional[int]:
        for name, value in self.counts:
            if name == key:
                return value
        return None


@dataclass(frozen=True)
class MonthEndReport:
    """One month's report. Finalized reports are never edited in place."""

    report_id: str
    period_id: str
    child_id: str
    focus_plan_id: str
    cycle_month: str
    sections: Tuple[SectionContent, ...] = ()
    state: ReportState = ReportState.DRAFT
    version: int = 1
    generated_at: datetime = field(default_factory=utc_now)
    finalized_at: Optional[datetime] = None
    finalized_by_actor_id: Optional[str] = None
    #: Amendment lineage, both directions.
    supersedes_report_id: Optional[str] = None
    superseded_by_report_id: Optional[str] = None
    amendment_reason: str = ""
    amended_by_actor_id: Optional[str] = None
    evidence_summary_id: str = ""
    coding_summary_id: str = ""
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.THERAPIST_ONLY

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ReportError("report versions start at 1")
        object.__setattr__(self, "sections", tuple(self.sections))
        seen = set()
        for content in self.sections:
            if content.section in seen:
                raise ReportError(f"duplicate section: {content.section.value}")
            seen.add(content.section)
        if (self.state is ReportState.AMENDED
                and not (self.amendment_reason or "").strip()):
            raise ReportError("an amended report requires a stated reason")

    @property
    def is_finalized(self) -> bool:
        """FINALIZED or AMENDED — both are issued documents, not drafts."""
        return self.state in (ReportState.FINALIZED, ReportState.AMENDED)

    @property
    def is_current(self) -> bool:
        return self.superseded_by_report_id is None

    def section_for(self, section: ReportSection) -> Optional[SectionContent]:
        for content in self.sections:
            if content.section is section:
                return content
        return None

    @staticmethod
    def create(period_id: str, child_id: str, focus_plan_id: str,
               cycle_month: str, *, sections: Tuple[SectionContent, ...] = (),
               evidence_summary_id: str = "", coding_summary_id: str = "",
               version: int = 1,
               supersedes_report_id: Optional[str] = None,
               amendment_reason: str = "",
               now: Optional[datetime] = None) -> "MonthEndReport":
        stamp = now or utc_now()
        return MonthEndReport(
            report_id=new_month_end_report_id(),
            period_id=period_id,
            child_id=child_id,
            focus_plan_id=focus_plan_id,
            cycle_month=cycle_month,
            sections=tuple(sections),
            version=version,
            generated_at=stamp,
            supersedes_report_id=supersedes_report_id,
            amendment_reason=amendment_reason,
            evidence_summary_id=evidence_summary_id,
            coding_summary_id=coding_summary_id,
            created_at=stamp, updated_at=stamp,
        )

    def finalize(self, *, actor_id: str,
                 now: Optional[datetime] = None) -> "MonthEndReport":
        if self.state is not ReportState.DRAFT:
            raise ReportError("only a draft report can be finalized")
        if not (actor_id or "").strip():
            raise ReportError("finalizing requires an actor")
        stamp = now or utc_now()
        return replace(self, state=ReportState.FINALIZED, finalized_at=stamp,
                       finalized_by_actor_id=actor_id, updated_at=stamp)

    def with_successor(self, successor_id: str, *,
                       now: Optional[datetime] = None) -> "MonthEndReport":
        """Stamp the forward pointer. The content is untouched."""
        if not self.is_finalized:
            raise ReportError("only a finalized report is superseded")
        return replace(self, superseded_by_report_id=successor_id,
                       updated_at=now or utc_now())

    def amend(self, *, actor_id: str, reason: str,
              sections: Optional[Tuple[SectionContent, ...]] = None,
              evidence_summary_id: str = "", coding_summary_id: str = "",
              now: Optional[datetime] = None) -> "MonthEndReport":
        """Build the AMENDED successor. This record is not modified.

        Returns a new report at `version + 1` pointing back at this one. The
        caller persists both and stamps the forward pointer; nothing here
        edits a finalized document.
        """
        if not self.is_finalized:
            raise ReportError("only a finalized report can be amended")
        if not (reason or "").strip():
            raise ReportError("an amendment requires a stated reason")
        if not (actor_id or "").strip():
            raise ReportError("an amendment requires an actor")
        stamp = now or utc_now()
        successor = MonthEndReport.create(
            self.period_id, self.child_id, self.focus_plan_id,
            self.cycle_month,
            sections=sections if sections is not None else self.sections,
            evidence_summary_id=evidence_summary_id or self.evidence_summary_id,
            coding_summary_id=coding_summary_id or self.coding_summary_id,
            version=self.version + 1,
            supersedes_report_id=self.report_id,
            amendment_reason=reason,
            now=stamp)
        return replace(successor, state=ReportState.AMENDED,
                       amended_by_actor_id=actor_id,
                       finalized_at=stamp, finalized_by_actor_id=actor_id)
