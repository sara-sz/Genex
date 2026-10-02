"""Build a SECOND fictional child, fully staged, purely to verify 0.5D reads.

## Why a second child

0.5D's two enrichments can only be observed when a goal and a released weekly
cycle exist. Both require a managing clinician, which requires an accepted
provider connection — and the primary fictional child must keep NO connection,
because the Parent UI inviting and the Therapist UI accepting is the flow
being tested.

So this stages a throwaway child instead. The primary child is left pristine.

Everything here runs through the frozen services, including the parts that
have no HTTP route (cycle creation, snapshot capture, allocation, release).
The 0.5D reads are then verified over real HTTP against this child.

## The snapshot document is a FICTIONAL stand-in

Staging has no Parent system — `PILOT_PARENT_SESSION_BUCKET` is deliberately
unset. So the document below plays the part Parent would play. It is shaped
the way a plan document plausibly would be, which makes it useful for wiring a
UI, and it is NOT a schema the pilot owns, validates or promises. That is
exactly what `is_canonical: false` in the response is there to say.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth.resolver import resolve_principal
from pilot_backend.auth.verifiers import VerifiedToken
from pilot_backend.config import PilotSettings
from pilot_backend.connections import ProviderConnectionService
from pilot_backend.domain.connections import CaregiverChildConnection
from pilot_backend.domain.entities import Child
from pilot_backend.domain.enums import CaregiverRelationship
from pilot_backend.domain.goals import EditType, GoalKind, GoalRef
from pilot_backend.domain.source_link import SourceSystem
from pilot_backend.goals.service import GoalService
from pilot_backend.persistence import FirestoreRepositories
from pilot_backend.planning.service import MonthlyPlanService
from pilot_backend.repository.interface import RecordNotFound
from pilot_backend.weekly.allocator import CandidateActivity
from pilot_backend.weekly.service import WeeklyService
from pilot_runtime.composition import build_store

VERIFICATION_CHILD_ID = "child_fictionalverificationonlydonotuse"
GOAL_TEMPLATE = "Fictional verification target for {child}. Not clinical text."

#: A fictional stand-in for Parent's own plan document. Opaque to the pilot.
FICTIONAL_PLAN_DOCUMENT = {
    "plan_label": "Fictional week (verification only)",
    "activities": [
        {"ref": "fictional-activity-1",
         "title": "Fictional activity one",
         "parent_instructions": "Fictional instructions. Not clinical advice.",
         "domain": "talking_and_communicating",
         "routine": "fictional-routine-mealtime",
         "materials": ["fictional-material-a"],
         "local_date": "fictional-day-1"},
        {"ref": "fictional-activity-2",
         "title": "Fictional activity two",
         "parent_instructions": "Fictional instructions. Not clinical advice.",
         "domain": "social_and_emotional",
         "routine": "fictional-routine-play",
         "materials": [],
         "local_date": "fictional-day-2"},
    ],
}


class SeedRefused(Exception):
    PHI_SAFE_MESSAGE = True


def main(argv) -> int:
    if len(argv) != 3:
        print(__doc__)
        print("usage: ... <caregiver_auth_subject> <provider_auth_subject>")
        return 64
    caregiver_subject, provider_subject = argv[1].strip(), argv[2].strip()

    settings = PilotSettings.from_env(os.environ)
    if settings.environment.is_prod:
        raise SeedRefused("the verification seed must never run against prod")

    repos = FirestoreRepositories(build_store(settings,
                                              process_env=os.environ))
    recorder = AuditRecorder(repos.audit_events,
                             environment=settings.environment.value)

    caregiver = resolve_principal(
        VerifiedToken(subject=caregiver_subject), repos)
    provider = resolve_principal(
        VerifiedToken(subject=provider_subject), repos)
    if caregiver is None or provider is None:
        raise SeedRefused("caregiver or provider subject resolves to nothing")

    # --- the child and its ownership edge ------------------------------
    try:
        child = repos.children.get_by_id(VERIFICATION_CHILD_ID)
        print(f"child      : {child.child_id} (existing)")
    except RecordNotFound:
        child = repos.children.create(Child(
            child_id=VERIFICATION_CHILD_ID,
            created_by_actor_id=caregiver.application_id))
        repos.caregiver_child.connect(CaregiverChildConnection.create(
            caregiver.application_id, child.child_id,
            CaregiverRelationship.PARENT,
            actor_id=caregiver.application_id))
        print(f"child      : {child.child_id} (created)")

    connections = ProviderConnectionService(repos=repos, recorder=recorder)
    goals = GoalService(repos=repos, recorder=recorder)
    plans = MonthlyPlanService(repos=repos, recorder=recorder)
    weekly = WeeklyService(repos=repos, recorder=recorder)

    # --- connection + managing clinician -------------------------------
    if not connections.current_managing_clinician(caregiver, child.child_id):
        pending = connections.invite_provider(
            caregiver, child.child_id, provider.application_id)
        connections.accept_invitation(provider, pending.connection_id)
        connections.assign_managing_clinician(
            caregiver, child.child_id, provider.application_id)
        print("connection : invited, accepted, managing assigned")
    else:
        print("connection : already active with a managing clinician")

    # --- a goal, so the 0.5D wording read has something to resolve -----
    existing = goals.list_clinical_goals(provider, child.child_id)
    if existing:
        goal = existing[0]
        print(f"goal       : {goal.clinical_goal_id} (existing)")
    else:
        goal = goals.approve_clinical_goal(
            provider, child.child_id, edit_type=EditType.AUTHORED_FRESH,
            text=GOAL_TEMPLATE, reason="Fictional verification rationale")
        print(f"goal       : {goal.clinical_goal_id} (created)")

    # --- an ACTIVE monthly plan ----------------------------------------
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    plan = plans.active_plan(provider, child.child_id, month)
    if plan is None:
        plan = plans.create_plan(provider, child.child_id, month, "UTC")
        plans.allocate_goal(
            provider, plan.focus_plan_id,
            GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id),
            priority_rank=1, emphasis_weight=2)
        plan = plans.activate_plan(provider, plan.focus_plan_id)
        print(f"plan       : {plan.focus_plan_id} (created, allocated, active)")
    else:
        print(f"plan       : {plan.focus_plan_id} (existing)")

    # --- a RELEASED weekly cycle with the fictional snapshot -----------
    cycles = weekly.list_cycles(provider, plan.focus_plan_id)
    if cycles:
        print(f"cycle      : {cycles[-1].cycle_id} (existing)")
    else:
        cycle = weekly.create_cycle(provider, plan.focus_plan_id,
                                    sequence_in_month=1)
        # Snapshot BEFORE release: a cycle cannot be released before its plan
        # is snapshotted.
        weekly.capture_snapshot(
            provider, cycle.cycle_id, SourceSystem.PARENT,
            "fictional-parent-plan-verification", FICTIONAL_PLAN_DOCUMENT)
        ref = GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id)
        weekly.allocate_cycle(
            provider, cycle.cycle_id,
            [CandidateActivity(activity_identity_ref="fictional-activity-1",
                               supports=(ref,), primary_for=ref),
             CandidateActivity(activity_identity_ref="fictional-activity-2",
                               supports=(ref,))],
            family_declared_capacity=4)
        released = weekly.release_cycle(provider, cycle.cycle_id)
        print(f"cycle      : {released.cycle_id} (snapshotted, allocated, released)")

    print(f"\nverification child ready: {child.child_id}")
    print("the PRIMARY fictional child is untouched and still has no connection")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
