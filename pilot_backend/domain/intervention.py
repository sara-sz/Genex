"""pilot_backend/domain/intervention.py — clinician planning actions.

    TherapistIntervention  one clinical decision about the plan. Immutable.

## Released plans are not rewritten

    applies_to = CURRENT_PLAN   records INTENT against a plan the family may
                                already hold. It never mutates the released
                                WeeklyPlanSnapshot.
    applies_to = FUTURE_CYCLE   feeds next-cycle generation directly, with no
                                parent acceptance, because nothing has been
                                shown yet.

The asymmetry is the point. Once `released_to_parent_at` is set and a snapshot
exists, the family has been given something specific; silently changing it
would mean the plan they are working from and the plan of record disagree.
Real proposal-and-acceptance wiring is 0.5. Until then an intervention against
a released cycle is a recorded intention, and `weekly/errors.py` has a
distinct refusal for anything that tries to make it more than that.

## A clinician-directed change is not a failure

Every intervention carries `not_a_failure = True`. When one of these drives a
Week N+1 difference, the adaptation record must say a clinician decided it —
not that the child struggled. The two produce different next weeks and
different conversations, and the signal namespace in `domain/adaptation.py`
keeps them separable by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from .entities import SCHEMA_VERSION, utc_now
from .enums import Visibility
from .ids import new_therapist_intervention_id


class InterventionError(ValueError):
    """Invalid intervention. PHI-safe: names the rule, never the rationale."""

    PHI_SAFE_MESSAGE = True


class InterventionAction(str, Enum):
    """What the clinician decided."""

    ENDORSE = "endorse"
    ADAPT = "adapt"
    REPLACE = "replace"
    ADD_GUIDANCE = "add_guidance"
    DEFER = "defer"
    REMOVE_FROM_SCHEDULING = "remove_from_scheduling"


class InterventionScope(str, Enum):
    """Which plan the decision applies to."""

    CURRENT_PLAN = "current_plan"
    FUTURE_CYCLE = "future_cycle"


#: Actions that place new activity into a cycle and therefore consume family
#: capacity (section 18). ENDORSE and guidance add nothing to do.
CAPACITY_CONSUMING_ACTIONS = frozenset({
    InterventionAction.ADAPT,
    InterventionAction.REPLACE,
})


@dataclass(frozen=True)
class TherapistIntervention:
    """One clinician planning decision. Immutable."""

    intervention_id: str
    child_id: str
    cycle_id: str
    provider_id: str
    action: InterventionAction
    applies_to: InterventionScope
    #: Required. A clinical decision with no stated reason is unreviewable.
    clinical_rationale: str
    #: What the decision is about — an activity instance, a goal, or the
    #: cycle itself. Opaque here; the service validates it belongs to the
    #: child before writing.
    target_ref: str = ""
    guidance_text: str = ""
    #: The managing-clinician assignment that authorised this, for audit.
    managing_assignment_id: str = ""
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.THERAPIST_ONLY

    #: A clinician changing the plan is never evidence the child failed.
    not_a_failure = True

    def __post_init__(self) -> None:
        if not isinstance(self.action, InterventionAction):
            raise InterventionError("action must be an InterventionAction")
        if not isinstance(self.applies_to, InterventionScope):
            raise InterventionError("applies_to must be an InterventionScope")
        if not (self.clinical_rationale or "").strip():
            raise InterventionError("an intervention requires a clinical rationale")
        for label, value in (("child_id", self.child_id),
                             ("cycle_id", self.cycle_id),
                             ("provider_id", self.provider_id)):
            if not (value or "").strip():
                raise InterventionError(f"an intervention requires {label}")
        if (self.action is InterventionAction.ADD_GUIDANCE
                and not (self.guidance_text or "").strip()):
            raise InterventionError("ADD_GUIDANCE requires guidance text")

    @property
    def consumes_capacity(self) -> bool:
        return self.action in CAPACITY_CONSUMING_ACTIONS

    @property
    def is_future(self) -> bool:
        return self.applies_to is InterventionScope.FUTURE_CYCLE

    @staticmethod
    def create(child_id: str, cycle_id: str, provider_id: str, *,
               action: InterventionAction, applies_to: InterventionScope,
               clinical_rationale: str, target_ref: str = "",
               guidance_text: str = "", managing_assignment_id: str = "",
               now: Optional[datetime] = None) -> "TherapistIntervention":
        return TherapistIntervention(
            intervention_id=new_therapist_intervention_id(),
            child_id=child_id,
            cycle_id=cycle_id,
            provider_id=provider_id,
            action=action,
            applies_to=applies_to,
            clinical_rationale=clinical_rationale,
            target_ref=target_ref,
            guidance_text=guidance_text,
            managing_assignment_id=managing_assignment_id,
            created_at=now or utc_now(),
        )
