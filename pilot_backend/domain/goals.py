"""pilot_backend/domain/goals.py — suggestions, approved goals, and versions.

Three concepts that are routinely collapsed into one field called "goal", kept
apart here as separate TYPES:

    GoalSuggestion          Genex-authored candidate. NEVER an approved goal.
    ClinicalGoal            clinician-approved. RTM-eligible.
    CaregiverApprovedGoal   caregiver-approved. NEVER RTM-eligible.

## Two types, not one type with a flag

A single `Goal` carrying `approved_by_role` would be shorter and would work
until the day one function forgets to check the flag — at which point a
caregiver-approved goal becomes clinical evidence, silently. Distinct types
make that a `TypeError` rather than a missed branch.

The codebase already uses this pattern deliberately: the therapist service
keeps `ParentNote` and `PrivateTherapistNote` as separate models in separate
collections for exactly the same reason.

`GoalRef` exists so allocations can point at either kind without erasing which
kind it is. It is a discriminated reference, not a merged type — and
`require_clinical_goal_ref` is the structural gate for anything that may only
accept a clinician-approved goal.

## A milestone is evidence, not a goal

`GoalSuggestionEvidence` carries the milestone ids, the baseline area and the
domain that produced a suggestion. A CDC milestone is an observation; a goal
is a human-approved functional target. Nothing here promotes one to the other
without a recorded human action.

## Versions are immutable

`GoalVersion` is never edited. Modifying an approved goal appends a new
version with a reason and a `supersedes_version_id`; the goal's
`current_version_id` moves forward and the prior text stays readable. Nothing
in this module deletes.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from .entities import SCHEMA_VERSION, utc_now
from .enums import Visibility
from .goal_vocabulary import require_canonical_domain
from .ids import (
    new_caregiver_goal_id,
    new_clinical_goal_id,
    new_goal_suggestion_id,
    new_goal_version_id,
)
from .roles import ActorRole

#: The token a family-facing template uses in place of a child's name.
#:
#: Nothing in `pilot_backend` stores a child's name — `Child` has carried no
#: name since 0.1 and the Parent service is name-blind at rest. Writing
#: "Help Maya ..." into a suggestion would quietly end that property, so the
#: stored text keeps a placeholder and the label is substituted at
#: presentation time by whichever system actually holds it.
CHILD_PLACEHOLDER = "{child}"


class GoalKind(str, Enum):
    """Which approved-goal type a reference points at.

    A discriminator on a REFERENCE, not on a goal. The goals themselves remain
    separate types; this exists so an allocation can name one without
    flattening the distinction.
    """

    CLINICAL = "clinical"
    CAREGIVER_APPROVED = "caregiver_approved"


class GoalStatus(str, Enum):
    """Lifecycle of an approved goal. There is no DELETED state."""

    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


class SuggestionStatus(str, Enum):
    """What became of a Genex-authored candidate."""

    OFFERED = "offered"
    ACCEPTED = "accepted"
    MODIFIED = "modified"
    REPLACED = "replaced"
    DECLINED = "declined"
    SUPERSEDED = "superseded"


class EvidenceSource(str, Enum):
    """Where the observation behind a suggestion came from.

    An enum rather than a free string for the reason `SourceSystem` is one in
    0.4A: an unrecognised source name would silently create a category nobody
    validates, and evidence provenance is exactly the thing that must not be
    approximate.

    DIAGNOSIS IS NOT A MEMBER, and that is the point. A diagnosis is context,
    never a starting level — the Parent invariant "observed ability is the
    starting level; diagnosis does not override observation" is enforced here
    by leaving diagnosis with no representable slot to occupy.
    """

    #: A caregiver answered a milestone question.
    CAREGIVER_REPORTED_MILESTONE = "caregiver_reported_milestone"
    #: A caregiver or clinician explicitly named this focus area.
    EXPLICIT_SELECTION = "explicit_selection"
    #: A clinician recorded a direct observation.
    CLINICIAN_OBSERVATION = "clinician_observation"
    #: A functional-baseline area assessment.
    FUNCTIONAL_BASELINE = "functional_baseline"
    #: Carried forward from a completed prior cycle.
    PRIOR_CYCLE_SUMMARY = "prior_cycle_summary"


class EditType(str, Enum):
    """How an approved goal version came to exist."""

    ACCEPTED_VERBATIM = "accepted_verbatim"
    MODIFIED = "modified"
    REPLACED = "replaced"
    AUTHORED_FRESH = "authored_fresh"


#: Edits that change the wording a human approved, and therefore require a
#: stated reason. Accepting verbatim needs none: the suggestion IS the reason.
EDIT_TYPES_REQUIRING_REASON = frozenset({
    EditType.MODIFIED, EditType.REPLACED, EditType.AUTHORED_FRESH,
})


class GoalError(ValueError):
    """Invalid goal construction or transition.

    PHI-safe: names the rule that failed, never goal text or a person.
    """

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class GoalRef:
    """A typed pointer to one approved goal."""

    kind: GoalKind
    goal_id: str

    def __post_init__(self) -> None:
        if not (self.goal_id or "").strip():
            raise GoalError("a goal reference requires a goal id")

    @property
    def is_rtm_eligible(self) -> bool:
        """Only a clinician-approved goal may ever participate in RTM."""
        return self.kind is GoalKind.CLINICAL

    def as_key(self) -> str:
        """Stable string form for claim keys and comparisons."""
        return f"{self.kind.value}:{self.goal_id}"


def require_clinical_goal_ref(ref: GoalRef) -> GoalRef:
    """Structural gate for anything that may only accept a clinical goal.

    The point of two types is that this check exists in ONE place and refuses
    rather than coercing. A caregiver-approved goal is a real, valid goal —
    it is simply not a clinician's treatment goal, and no amount of
    downstream convenience should blur that.
    """
    if not ref.is_rtm_eligible:
        raise GoalError("this operation requires a clinician-approved goal")
    return ref


@dataclass(frozen=True)
class GoalSuggestionEvidence:
    """Why Genex proposed this. Traceable back to structured observation."""

    #: Canonical seven-domain key the suggestion addresses.
    domain_key: str
    #: What kind of observation this was. Required — see `EvidenceSource`.
    evidence_source: EvidenceSource
    #: Milestone identifiers that support it, e.g. `mv1:cdc:...`. May be empty
    #: only when an explicit observed functional area carries the support.
    milestone_refs: Tuple[str, ...] = ()
    #: Which functional-baseline area produced the observation.
    functional_baseline_area: str = ""
    #: The observed starting level, in the caller's structured vocabulary.
    observed_level: str = ""
    #: Set when a caregiver or clinician explicitly named this focus.
    explicitly_selected: bool = False
    #: Prior-month evidence, when a later cycle supplies it.
    prior_month_summary_id: Optional[str] = None
    #: Version of the ranking + wording rules that produced the suggestion.
    rule_version: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain_key",
                           require_canonical_domain(self.domain_key))
        if not isinstance(self.evidence_source, EvidenceSource):
            raise GoalError("evidence_source must be an EvidenceSource")
        if not (self.milestone_refs or self.explicitly_selected
                or self.functional_baseline_area):
            raise GoalError(
                "evidence must carry a milestone, a baseline area, or an "
                "explicit selection — a suggestion with no support is a guess")


@dataclass(frozen=True)
class GoalSuggestion:
    """A Genex-authored candidate. Never itself an approved goal."""

    suggestion_id: str
    child_id: str
    cycle_month: str                      # "YYYY-MM"
    #: Family-facing wording containing `CHILD_PLACEHOLDER`, never a name.
    family_facing_text_template: str
    evidence: GoalSuggestionEvidence
    suggested_priority_rank: int
    suggested_emphasis_weight: int
    generator_version: str
    #: Deterministic in 0.4B/C. The value exists so a later LLM-assisted
    #: WORDING mode is a visible, auditable change rather than a silent one.
    generation_mode: str = "deterministic"
    status: SuggestionStatus = SuggestionStatus.OFFERED
    created_at: datetime = field(default_factory=utc_now)
    created_by_actor_id: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    #: A suggestion is Genex's proposal to the family and their clinician.
    VISIBILITY = Visibility.PARENT_VISIBLE

    def __post_init__(self) -> None:
        if CHILD_PLACEHOLDER not in self.family_facing_text_template:
            raise GoalError(
                "family-facing text must use the child placeholder; a stored "
                "name would break the name-blind property")
        if self.suggested_priority_rank < 1:
            raise GoalError("priority rank starts at 1")
        if self.suggested_emphasis_weight <= 0:
            raise GoalError("emphasis weight must be positive")

    def render(self, child_label: str) -> str:
        """Substitute a display name at presentation time. Never stored."""
        return self.family_facing_text_template.replace(
            CHILD_PLACEHOLDER, (child_label or "your child").strip())

    def with_status(self, status: SuggestionStatus) -> "GoalSuggestion":
        return replace(self, status=status)

    @staticmethod
    def create(child_id: str, cycle_month: str, template: str,
               evidence: GoalSuggestionEvidence, *, priority_rank: int,
               emphasis_weight: int, generator_version: str,
               actor_id: Optional[str] = None,
               now: Optional[datetime] = None) -> "GoalSuggestion":
        return GoalSuggestion(
            suggestion_id=new_goal_suggestion_id(),
            child_id=child_id,
            cycle_month=cycle_month,
            family_facing_text_template=template,
            evidence=evidence,
            suggested_priority_rank=priority_rank,
            suggested_emphasis_weight=emphasis_weight,
            generator_version=generator_version,
            created_at=now or utc_now(),
            created_by_actor_id=actor_id,
        )


@dataclass(frozen=True)
class GoalVersion:
    """One immutable wording of an approved goal, with full provenance."""

    version_id: str
    goal_kind: GoalKind
    goal_id: str
    version_number: int
    text: str
    edit_type: EditType
    actor_id: str
    actor_role: ActorRole
    derived_from_suggestion_id: Optional[str] = None
    reason: str = ""
    supersedes_version_id: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    #: Goal text is shared with the family by design.
    VISIBILITY = Visibility.PARENT_VISIBLE

    def __post_init__(self) -> None:
        if self.version_number < 1:
            raise GoalError("goal versions start at 1")
        if not (self.text or "").strip():
            raise GoalError("a goal version requires text")
        if not (self.actor_id or "").strip():
            raise GoalError("a goal version requires an actor")
        if (self.edit_type in EDIT_TYPES_REQUIRING_REASON
                and not (self.reason or "").strip()):
            raise GoalError(f"{self.edit_type.value} requires a stated reason")
        if (self.edit_type is EditType.ACCEPTED_VERBATIM
                and not self.derived_from_suggestion_id):
            raise GoalError(
                "accepting verbatim requires the suggestion it accepted")

    @property
    def ref(self) -> GoalRef:
        return GoalRef(self.goal_kind, self.goal_id)

    def render(self, child_label: str) -> str:
        """Substitute a display name at presentation time, if one is present.

        Text accepted verbatim from a suggestion always carries
        `CHILD_PLACEHOLDER`. Text a human authored may not, and that is fine —
        substitution is a no-op then.

        What this canNOT do is guarantee that human-authored text contains no
        name. Genex-GENERATED wording never does, and that is enforced by
        `GoalSuggestion.__post_init__`; a clinician typing a child's name into
        a goal they wrote is clinical free text, recorded as carried debt
        rather than pretended away.
        """
        return self.text.replace(CHILD_PLACEHOLDER,
                                 (child_label or "your child").strip())

    @staticmethod
    def create(ref: GoalRef, version_number: int, text: str,
               edit_type: EditType, *, actor_id: str, actor_role: ActorRole,
               derived_from_suggestion_id: Optional[str] = None,
               reason: str = "",
               supersedes_version_id: Optional[str] = None,
               now: Optional[datetime] = None) -> "GoalVersion":
        return GoalVersion(
            version_id=new_goal_version_id(),
            goal_kind=ref.kind,
            goal_id=ref.goal_id,
            version_number=version_number,
            text=text.strip(),
            edit_type=edit_type,
            actor_id=actor_id,
            actor_role=actor_role,
            derived_from_suggestion_id=derived_from_suggestion_id,
            reason=reason.strip(),
            supersedes_version_id=supersedes_version_id,
            created_at=now or utc_now(),
        )


@dataclass(frozen=True)
class ClinicalGoal:
    """A clinician-approved treatment goal. RTM-eligible.

    Owned by the ACTIVE managing clinician. Genex may suggest; only a
    clinician action creates one.
    """

    clinical_goal_id: str
    child_id: str
    managing_provider_id: str
    practice_id: str
    #: The managing-clinician assignment that authorised creation, for audit.
    managing_assignment_id: str
    current_version_id: str
    status: GoalStatus = GoalStatus.ACTIVE
    opened_at: datetime = field(default_factory=utc_now)
    closed_at: Optional[datetime] = None
    created_by_actor_id: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.PARENT_VISIBLE

    @property
    def ref(self) -> GoalRef:
        return GoalRef(GoalKind.CLINICAL, self.clinical_goal_id)

    @property
    def is_active(self) -> bool:
        return self.status is GoalStatus.ACTIVE and self.closed_at is None

    @staticmethod
    def create(child_id: str, provider_id: str, practice_id: str, *,
               managing_assignment_id: str, current_version_id: str,
               actor_id: Optional[str] = None,
               now: Optional[datetime] = None) -> "ClinicalGoal":
        stamp = now or utc_now()
        for label, value in (("child_id", child_id), ("provider_id", provider_id),
                             ("practice_id", practice_id),
                             ("managing_assignment_id", managing_assignment_id)):
            if not (value or "").strip():
                raise GoalError(f"a clinical goal requires {label}")
        return ClinicalGoal(
            clinical_goal_id=new_clinical_goal_id(),
            child_id=child_id,
            managing_provider_id=provider_id,
            practice_id=practice_id,
            managing_assignment_id=managing_assignment_id,
            current_version_id=current_version_id,
            opened_at=stamp, created_at=stamp, updated_at=stamp,
            created_by_actor_id=actor_id,
        )

    def with_current_version(self, version_id: str, *,
                             now: Optional[datetime] = None) -> "ClinicalGoal":
        return replace(self, current_version_id=version_id,
                       updated_at=now or utc_now())

    def with_status(self, status: GoalStatus, *,
                    now: Optional[datetime] = None) -> "ClinicalGoal":
        stamp = now or utc_now()
        closed = stamp if status is GoalStatus.RETIRED else self.closed_at
        return replace(self, status=status, closed_at=closed, updated_at=stamp)


@dataclass(frozen=True)
class CaregiverApprovedGoal:
    """A caregiver-approved developmental goal. NEVER RTM-eligible.

    A real goal that drives planning identically. It is simply not a
    clinician's treatment goal, and no code path converts one into the other:
    a clinician joining later must author a `ClinicalGoal` explicitly, with
    its own provenance.
    """

    caregiver_goal_id: str
    child_id: str
    approved_by_caregiver_id: str
    current_version_id: str
    status: GoalStatus = GoalStatus.ACTIVE
    opened_at: datetime = field(default_factory=utc_now)
    closed_at: Optional[datetime] = None
    created_by_actor_id: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.PARENT_VISIBLE

    @property
    def ref(self) -> GoalRef:
        return GoalRef(GoalKind.CAREGIVER_APPROVED, self.caregiver_goal_id)

    @property
    def is_active(self) -> bool:
        return self.status is GoalStatus.ACTIVE and self.closed_at is None

    @staticmethod
    def create(child_id: str, caregiver_id: str, *, current_version_id: str,
               actor_id: Optional[str] = None,
               now: Optional[datetime] = None) -> "CaregiverApprovedGoal":
        stamp = now or utc_now()
        for label, value in (("child_id", child_id), ("caregiver_id", caregiver_id)):
            if not (value or "").strip():
                raise GoalError(f"a caregiver-approved goal requires {label}")
        return CaregiverApprovedGoal(
            caregiver_goal_id=new_caregiver_goal_id(),
            child_id=child_id,
            approved_by_caregiver_id=caregiver_id,
            current_version_id=current_version_id,
            opened_at=stamp, created_at=stamp, updated_at=stamp,
            created_by_actor_id=actor_id,
        )

    def with_current_version(self, version_id: str, *,
                             now: Optional[datetime] = None) -> "CaregiverApprovedGoal":
        return replace(self, current_version_id=version_id,
                       updated_at=now or utc_now())

    def with_status(self, status: GoalStatus, *,
                    now: Optional[datetime] = None) -> "CaregiverApprovedGoal":
        stamp = now or utc_now()
        closed = stamp if status is GoalStatus.RETIRED else self.closed_at
        return replace(self, status=status, closed_at=closed, updated_at=stamp)
