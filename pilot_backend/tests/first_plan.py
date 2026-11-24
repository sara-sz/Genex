"""Test helper — the clinician's explicit first-plan workflow.

First-plan timezone gate: `WeekOneReleaseService` no longer creates, allocates
or activates a child's first plan (it refuses with `ClinicalPlanRequired`).
Fixtures that used to reach Week 1 through that bootstrap now perform the
supported workflow explicitly, through the SAME service methods the
monthly-plan routes call:

    create_plan(..., timezone_of_record)   explicit, validated IANA zone
    allocate_goal(...)                     each releasable goal, in goal order
    activate_plan(...)                     the clinician's activation

at the moment the bootstrap used to run, so each fixture keeps its clock, its
plan label and its allocation order. Not a test module (no `test_` prefix).
"""

from __future__ import annotations

from typing import Optional

from pilot_backend.domain.clinical_period import local_date_in
from pilot_backend.integration.governing_plan import GovernanceOutcome


def activate_first_plan(service, principal, child_id: str, *,
                        timezone_of_record: str,
                        cycle_month: Optional[str] = None,
                        local_date: Optional[str] = None):
    """Create (explicit zone), allocate and activate the child's first plan."""
    plans = service._plans
    today = local_date or local_date_in(timezone_of_record, service._now())
    plan = plans.create_plan(principal, child_id, cycle_month or today[:7],
                             timezone_of_record)
    for rank, row in enumerate(service.releasable_goals(principal, child_id),
                               start=1):
        plans.allocate_goal(principal, plan.focus_plan_id, row.ref,
                            priority_rank=rank)
    return plans.activate_plan(principal, plan.focus_plan_id)


def release_with_first_plan(service, principal, child_id: str, *,
                            timezone_of_record: str,
                            cycle_month: Optional[str] = None,
                            local_date: Optional[str] = None, **kwargs):
    """`release_week_one`, preceded by the explicit first-plan workflow only
    when the child has never had an activated plan (and has a releasable goal,
    so a no-goal child still gets `GoalNotReleasable` exactly as before)."""
    governance = service.governance(principal, child_id, local_date=local_date)
    if (governance.outcome is GovernanceOutcome.BOOTSTRAP
            and service.releasable_goals(principal, child_id)):
        activate_first_plan(service, principal, child_id,
                            timezone_of_record=timezone_of_record,
                            cycle_month=cycle_month, local_date=local_date)
    return service.release_week_one(principal, child_id,
                                    cycle_month=cycle_month,
                                    local_date=local_date, **kwargs)
