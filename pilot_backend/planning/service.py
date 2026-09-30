"""pilot_backend/planning/service.py — the only writer of monthly focus plans.

Same shape as `identity/service.py` and `goals/service.py`: the 0.2 child-access
gate first, an audit event last, `principal` as the only source of identity.

## Who owns a month

    child HAS an active managing clinician   only that clinician may write
    child has NONE                           an authorized caregiver may

One rule, not two overlapping ones. A family with no clinician still needs a
month of direction — that is what `CaregiverApprovedGoal` is for — but once a
clinician holds the assignment, direction has a single owner and a caregiver
cannot set a competing one. `_require_plan_author` is the single place that
decides this.

## Activation wins a claim, exactly as 0.4A does

`ClaimKind.MONTHLY_FOCUS_PLAN` on (child_id, cycle_month). Two clinicians
activating October in the same second must not both succeed, or the child has
two months of direction and nothing in the record says which one the weekly
plans followed.

## Why activation is not one transaction, and why that is still safe

The `DocumentStore` port REFUSES `set` inside a transaction — a deliberate
0.4A constraint, because a transactional `set` that could create a document
would silently weaken the port's existence guarantee. Activation needs a
read-modify-write of an existing plan, so it cannot all live in one
transaction, and the 0.4A note is explicit that a slice hitting this must
RESTRUCTURE rather than relax the port.

So it is restructured into two steps with a forward-recoverable boundary:

    1. transaction: create the uniqueness claim AND every goal snapshot
    2. outside:     set the plan ACTIVE, stamping the claim id

Step 1 is all-or-nothing. A crash between 1 and 2 leaves a claim whose
`holder_ref` is THIS plan's id and a DRAFT plan. A retry recognises that it
already holds its own claim, skips step 1 entirely — so no duplicate snapshots
— and completes step 2. The key is never stranded, and no cleanup job exists
to hide the problem.

A DIFFERENT plan retrying collides on the claim and is refused, which is the
outcome we want. Recovery is idempotent for the rightful holder and a hard
refusal for everyone else.

## Snapshots are taken at activation, not read later

Each active allocation is frozen with the goal's text AS IT THEN READ. An
edit in November cannot reach October's record, because October's record is a
copy rather than a pointer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Tuple

from ..audit.events import AuditAction, AuditResult
from ..authz.decisions import AccessDecision
from ..authz.policy import authorize_child_access
from ..domain.goals import GoalKind, GoalRef
from ..domain.identity_claims import (
    ClaimKind,
    IdentityClaim,
    claim_document_id,
    key_digest,
)
from ..domain.monthly_plan import (
    AllocationStatus,
    MonthlyFocusPlan,
    MonthlyGoalAllocation,
    MonthlyGoalSnapshot,
    MonthlyPlanState,
    validate_cycle_month,
)
from ..domain.planning_policy import CURRENT_PLANNING_POLICY, PlanningPolicyVersion
from ..domain.roles import ActorRole
from ..goals.errors import GoalAuthorizationError, GoalConflict, GoalValidationError
from ..persistence.document_store import DocumentStoreError
from ..repository.interface import DuplicateRecord, RecordNotFound

RESOURCE_FOCUS_PLAN = "monthly_focus_plan"
RESOURCE_ALLOCATION = "monthly_goal_allocation"


def _default_repos_factory(store):
    from ..persistence.firestore_repos import FirestoreRepositories

    return FirestoreRepositories(store)


class MonthlyPlanService:
    """Authorized reads and writes for monthly direction."""

    def __init__(self, *, repos, recorder=None, now=None,
                 repos_factory=None) -> None:
        self._repos = repos
        self._recorder = recorder
        self._repos_factory = repos_factory or _default_repos_factory
        self._now = now

    def _stamp(self) -> datetime:
        return self._now() if self._now else datetime.now(timezone.utc)

    # -- gates --------------------------------------------------------------

    def _authorize(self, principal, child_id: str) -> AccessDecision:
        decision = authorize_child_access(principal, child_id, self._repos)
        if not decision.allowed:
            raise GoalAuthorizationError(
                f"not permitted for this child ({decision.denial.value})")
        return decision

    def _require_plan_author(self, principal, child_id: str):
        """Enforce the single-owner rule. Returns the assignment, or None."""
        active = self._repos.managing_clinicians.list_for_child(child_id)
        if len(active) > 1:
            raise GoalConflict("more than one active managing clinician")
        if active:
            assignment = active[0]
            if (principal.role is not ActorRole.PROVIDER
                    or assignment.provider_id != principal.application_id):
                raise GoalAuthorizationError(
                    "only this child's managing clinician may set the month")
            return assignment
        if principal.role is not ActorRole.CAREGIVER:
            raise GoalAuthorizationError(
                "a child with no managing clinician is planned by a caregiver")
        return None

    def _audit(self, action: AuditAction, result: AuditResult, resource_type: str,
               *, principal, child_id: str, resource_id: Optional[str],
               request_id: str, **metadata) -> None:
        if self._recorder is None:
            return
        self._recorder.record_action(
            action, result, resource_type,
            resource_id=resource_id, child_id=child_id, principal=principal,
            request_id=request_id, metadata=metadata,
        )

    def _load_plan(self, focus_plan_id: str) -> MonthlyFocusPlan:
        try:
            return self._repos.focus_plans.get_by_id(focus_plan_id)
        except RecordNotFound:
            raise GoalConflict("no such monthly focus plan") from None

    # =====================================================================
    # Plan lifecycle
    # =====================================================================

    def create_plan(self, principal, child_id: str, cycle_month: str,
                    timezone_of_record: str, *,
                    policy: PlanningPolicyVersion = CURRENT_PLANNING_POLICY,
                    monitoring_focus: str = "",
                    routines_context: Tuple[str, ...] = (),
                    interests_motivators: Tuple[str, ...] = (),
                    support_considerations: str = "",
                    expected_practice_cadence: str = "",
                    monitoring_dimensions: Tuple[str, ...] = (),
                    clinician_guidance: str = "",
                    request_id: str = "") -> MonthlyFocusPlan:
        """Draft a month of direction. Activating it is a separate action.

        `timezone_of_record` is required and validated — there is no UTC
        fallback. A silently wrong zone shifts a day across a month boundary,
        and the monthly layer is the thing that counts days into months.
        """
        self._authorize(principal, child_id)
        self._require_plan_author(principal, child_id)
        month = validate_cycle_month(cycle_month)

        if self._repos.focus_plans.active_for_cycle(child_id, month) is not None:
            raise GoalConflict("this child already has an active plan for the month")

        plan = MonthlyFocusPlan.create(
            child_id, month, timezone_of_record,
            monitoring_focus=monitoring_focus,
            routines_context=routines_context,
            interests_motivators=interests_motivators,
            support_considerations=support_considerations,
            expected_practice_cadence=expected_practice_cadence,
            monitoring_dimensions=monitoring_dimensions,
            clinician_guidance=clinician_guidance,
            policy_version=policy.policy_version,
            actor_id=principal.application_id,
            actor_role=principal.role,
            now=self._stamp())
        self._repos.focus_plans.create(plan)

        self._audit(AuditAction.MONTHLY_PLAN_CREATED, AuditResult.SUCCESS,
                    RESOURCE_FOCUS_PLAN, principal=principal, child_id=child_id,
                    resource_id=plan.focus_plan_id, request_id=request_id,
                    focus_plan_id=plan.focus_plan_id, cycle_month=month,
                    policy_version=policy.policy_version,
                    plan_state=plan.state.value)
        return plan

    def activate_plan(self, principal, focus_plan_id: str, *,
                      request_id: str = "") -> MonthlyFocusPlan:
        """Win the child-month claim, snapshot the allocations, go ACTIVE.

        Refuses a plan with no active allocation: an active month that is
        about nothing is a state the weekly layer cannot act on, and letting
        it exist would push the failure into a layer that has less context to
        report it.
        """
        plan = self._load_plan(focus_plan_id)
        self._authorize(principal, plan.child_id)
        self._require_plan_author(principal, plan.child_id)
        if plan.state is not MonthlyPlanState.DRAFT:
            raise GoalConflict("only a draft plan can be activated")

        allocations = self._repos.goal_allocations.list_for_plan(focus_plan_id)
        if not allocations:
            raise GoalValidationError(
                "a plan cannot be activated with no allocated goal")

        # Read outside the transaction — see identity/service.py for the
        # measured reason (read locks turned an 8-way race into 194 seconds).
        parts = (plan.child_id, plan.cycle_month)
        digest = key_digest(*parts)
        generation = self._repos.identity_claims.next_generation(
            ClaimKind.MONTHLY_FOCUS_PLAN, digest)
        claim_id = claim_document_id(ClaimKind.MONTHLY_FOCUS_PLAN, digest, generation)

        if not self._already_holds(claim_id, focus_plan_id):
            snapshots = self._build_snapshots(plan, allocations)

            def _acquire(store) -> None:
                tx = self._repos_factory(store)
                tx.identity_claims.claim(IdentityClaim.build(
                    ClaimKind.MONTHLY_FOCUS_PLAN, parts, generation,
                    holder_ref=focus_plan_id, child_id=plan.child_id,
                    actor_id=principal.application_id, now=self._stamp()))
                for snapshot in snapshots:
                    tx.goal_snapshots.create(snapshot)

            try:
                self._repos.store.run_in_transaction(_acquire)
            except (DuplicateRecord, DocumentStoreError):
                self._audit(AuditAction.MONTHLY_PLAN_ACTIVATED, AuditResult.FAILURE,
                            RESOURCE_FOCUS_PLAN, principal=principal,
                            child_id=plan.child_id, resource_id=focus_plan_id,
                            request_id=request_id, focus_plan_id=focus_plan_id,
                            cycle_month=plan.cycle_month)
                raise GoalConflict(
                    "another writer holds the plan claim for this child-month"
                ) from None

        activated = plan.activate(claim_id=claim_id, now=self._stamp())
        self._repos.focus_plans.update(activated)

        self._audit(AuditAction.MONTHLY_PLAN_ACTIVATED, AuditResult.SUCCESS,
                    RESOURCE_FOCUS_PLAN, principal=principal,
                    child_id=plan.child_id, resource_id=focus_plan_id,
                    request_id=request_id, focus_plan_id=focus_plan_id,
                    cycle_month=plan.cycle_month, claim_id=claim_id,
                    claim_kind=ClaimKind.MONTHLY_FOCUS_PLAN.value,
                    plan_state=activated.state.value)
        return activated

    def _already_holds(self, claim_id: str, focus_plan_id: str) -> bool:
        """Whether THIS plan already won this claim — the recovery path.

        True only when the claim exists AND names this plan as its holder. A
        claim held by another plan returns False, so the caller proceeds into
        the transaction and collides there, which is the refusal we want.
        """
        try:
            existing = self._repos.identity_claims.get_by_id(claim_id)
        except RecordNotFound:
            return False
        return existing.holder_ref == focus_plan_id

    def _build_snapshots(self, plan: MonthlyFocusPlan,
                         allocations) -> List[MonthlyGoalSnapshot]:
        """Freeze each allocated goal's current wording."""
        stamp = self._stamp()
        snapshots = []
        for allocation in allocations:
            goal = self._load_allocated_goal(allocation.goal_ref)
            version = self._repos.goal_versions.get_by_id(goal.current_version_id)
            snapshots.append(MonthlyGoalSnapshot.create(
                plan.focus_plan_id, plan.child_id, allocation.goal_ref,
                goal_version_id=version.version_id,
                goal_text=version.text,
                priority_rank=allocation.priority_rank,
                emphasis_weight=allocation.emphasis_weight,
                min_coverage_per_cycle=allocation.min_coverage_per_cycle,
                approved_by_role=version.actor_role,
                allocation_id=allocation.allocation_id,
                effective_from=stamp, now=stamp))
        return snapshots

    def close_plan(self, principal, focus_plan_id: str, *,
                   request_id: str = "") -> MonthlyFocusPlan:
        """Close an active month. The plan, allocations and snapshots remain.

        The claim is deliberately NOT released. A closed month is finished,
        not undone: re-activating October after it ended would let a later
        edit rewrite what the month was measured against, which is the exact
        thing snapshots exist to prevent.
        """
        plan = self._load_plan(focus_plan_id)
        self._authorize(principal, plan.child_id)
        self._require_plan_author(principal, plan.child_id)

        closed = plan.close(now=self._stamp())
        self._repos.focus_plans.update(closed)
        self._audit(AuditAction.MONTHLY_PLAN_CLOSED, AuditResult.SUCCESS,
                    RESOURCE_FOCUS_PLAN, principal=principal,
                    child_id=plan.child_id, resource_id=focus_plan_id,
                    request_id=request_id, focus_plan_id=focus_plan_id,
                    cycle_month=plan.cycle_month, plan_state=closed.state.value)
        return closed

    # =====================================================================
    # Allocation
    # =====================================================================

    def _load_allocated_goal(self, ref: GoalRef):
        repo = (self._repos.clinical_goals if ref.kind is GoalKind.CLINICAL
                else self._repos.caregiver_goals)
        try:
            return repo.get_by_id(ref.goal_id)
        except RecordNotFound:
            raise GoalValidationError("allocation names a goal that does not exist") from None

    def allocate_goal(self, principal, focus_plan_id: str, ref: GoalRef, *,
                      priority_rank: int,
                      emphasis_weight: Optional[int] = None,
                      min_coverage_per_cycle: Optional[int] = None,
                      effective_from_cycle: int = 1,
                      reason: str = "",
                      request_id: str = "") -> MonthlyGoalAllocation:
        """Put a goal on the month at a priority.

        `emphasis_weight` defaults from the plan's recorded POLICY version, not
        from whatever the current defaults happen to be — a plan built under
        planning-policy-2026.10 keeps that weighting even after the product
        decision moves. An explicit weight always wins: the policy supplies a
        default, it does not impose a cap, and nothing here limits how many
        goals a month may carry.
        """
        plan = self._load_plan(focus_plan_id)
        self._authorize(principal, plan.child_id)
        self._require_plan_author(principal, plan.child_id)
        if plan.state is MonthlyPlanState.CLOSED:
            raise GoalConflict("a closed plan cannot be changed")

        goal = self._load_allocated_goal(ref)
        if goal.child_id != plan.child_id:
            raise GoalValidationError("goal belongs to a different child")
        if not goal.is_active:
            raise GoalValidationError("a closed goal cannot be allocated")

        for existing in self._repos.goal_allocations.list_for_plan(focus_plan_id):
            if existing.goal_ref == ref:
                raise GoalConflict("this goal is already allocated to the month")

        policy = self._policy_for_plan(plan)
        allocation = MonthlyGoalAllocation.create(
            focus_plan_id, plan.child_id, ref,
            priority_rank=priority_rank,
            emphasis_weight=(policy.emphasis_for_rank(priority_rank)
                             if emphasis_weight is None else emphasis_weight),
            min_coverage_per_cycle=(policy.min_coverage_per_cycle
                                    if min_coverage_per_cycle is None
                                    else min_coverage_per_cycle),
            actor_id=principal.application_id, actor_role=principal.role,
            effective_from_cycle=effective_from_cycle, reason=reason,
            now=self._stamp())
        self._repos.goal_allocations.create(allocation)

        self._audit(AuditAction.GOAL_ALLOCATED, AuditResult.SUCCESS,
                    RESOURCE_ALLOCATION, principal=principal,
                    child_id=plan.child_id, resource_id=allocation.allocation_id,
                    request_id=request_id, focus_plan_id=focus_plan_id,
                    allocation_id=allocation.allocation_id,
                    goal_kind=ref.kind.value, goal_id=ref.goal_id,
                    priority_rank=allocation.priority_rank,
                    emphasis_weight=allocation.emphasis_weight,
                    cycle_month=plan.cycle_month)
        return allocation

    def reprioritize(self, principal, allocation_id: str, *,
                     priority_rank: int,
                     emphasis_weight: Optional[int] = None,
                     min_coverage_per_cycle: Optional[int] = None,
                     effective_from_cycle: int,
                     reason: str,
                     request_id: str = "") -> MonthlyGoalAllocation:
        """Write a SUCCESSOR allocation. The prior row is retained.

        `effective_from_cycle` and `reason` are required keywords with no
        default. Mid-month reprioritisation is only interpretable if the
        record says from WHEN and WHY — without both, week 2's weighting
        becomes unanswerable the moment week 3's decision lands.
        """
        try:
            previous = self._repos.goal_allocations.get_by_id(allocation_id)
        except RecordNotFound:
            raise GoalConflict("no such allocation") from None

        plan = self._load_plan(previous.focus_plan_id)
        self._authorize(principal, plan.child_id)
        self._require_plan_author(principal, plan.child_id)
        if plan.state is MonthlyPlanState.CLOSED:
            raise GoalConflict("a closed plan cannot be changed")
        if not previous.is_active:
            raise GoalConflict("only an active allocation can be reprioritised")
        if effective_from_cycle < previous.effective_from_cycle:
            raise GoalValidationError(
                "a successor cannot take effect before its predecessor")

        policy = self._policy_for_plan(plan)
        successor = MonthlyGoalAllocation.create(
            previous.focus_plan_id, previous.child_id, previous.goal_ref,
            priority_rank=priority_rank,
            emphasis_weight=(policy.emphasis_for_rank(priority_rank)
                             if emphasis_weight is None else emphasis_weight),
            min_coverage_per_cycle=(previous.min_coverage_per_cycle
                                    if min_coverage_per_cycle is None
                                    else min_coverage_per_cycle),
            actor_id=principal.application_id, actor_role=principal.role,
            effective_from_cycle=effective_from_cycle,
            supersedes_allocation_id=previous.allocation_id,
            reason=reason, now=self._stamp())
        self._repos.goal_allocations.create(successor)
        self._repos.goal_allocations.update(
            previous.supersede(successor.allocation_id, now=self._stamp()))

        self._audit(AuditAction.GOAL_ALLOCATION_REPRIORITIZED, AuditResult.SUCCESS,
                    RESOURCE_ALLOCATION, principal=principal,
                    child_id=plan.child_id, resource_id=successor.allocation_id,
                    request_id=request_id,
                    focus_plan_id=previous.focus_plan_id,
                    allocation_id=successor.allocation_id,
                    goal_kind=previous.goal_kind.value,
                    goal_id=previous.goal_id,
                    priority_rank=successor.priority_rank,
                    emphasis_weight=successor.emphasis_weight,
                    cycle_month=plan.cycle_month)
        return successor

    def set_allocation_status(self, principal, allocation_id: str,
                              status: AllocationStatus, *,
                              request_id: str = "") -> MonthlyGoalAllocation:
        """Pause or retire an allocation without deleting it.

        SUPERSEDED is refused: that status is set by `reprioritize` alongside
        the successor pointer, and allowing it here would produce a superseded
        row with nothing superseding it.
        """
        if status is AllocationStatus.SUPERSEDED:
            raise GoalValidationError(
                "superseding is done by reprioritize, which writes a successor")
        try:
            allocation = self._repos.goal_allocations.get_by_id(allocation_id)
        except RecordNotFound:
            raise GoalConflict("no such allocation") from None

        plan = self._load_plan(allocation.focus_plan_id)
        self._authorize(principal, plan.child_id)
        self._require_plan_author(principal, plan.child_id)
        if plan.state is MonthlyPlanState.CLOSED:
            raise GoalConflict("a closed plan cannot be changed")

        updated = self._repos.goal_allocations.update(
            allocation.with_status(status, now=self._stamp()))
        self._audit(AuditAction.GOAL_ALLOCATION_REPRIORITIZED, AuditResult.SUCCESS,
                    RESOURCE_ALLOCATION, principal=principal,
                    child_id=plan.child_id, resource_id=allocation_id,
                    request_id=request_id, allocation_id=allocation_id,
                    focus_plan_id=allocation.focus_plan_id,
                    goal_kind=allocation.goal_kind.value,
                    goal_id=allocation.goal_id,
                    priority_rank=allocation.priority_rank,
                    cycle_month=plan.cycle_month)
        return updated

    def _policy_for_plan(self, plan: MonthlyFocusPlan) -> PlanningPolicyVersion:
        """The policy the PLAN was built with, resolved from its record.

        Falls back to the current policy only when the plan predates the
        field. Resolving an unknown version raises rather than substituting
        today's defaults — a plan stamped with a policy nobody can produce
        cannot be explained, and quietly using the current numbers would
        misreport what it was actually built with.
        """
        from ..domain.planning_policy import policy_for

        if not plan.policy_version:
            return CURRENT_PLANNING_POLICY
        return policy_for(plan.policy_version)

    # =====================================================================
    # Reads
    # =====================================================================

    def get_plan(self, principal, focus_plan_id: str) -> MonthlyFocusPlan:
        plan = self._load_plan(focus_plan_id)
        self._authorize(principal, plan.child_id)
        return plan

    def active_plan(self, principal, child_id: str,
                    cycle_month: str) -> Optional[MonthlyFocusPlan]:
        self._authorize(principal, child_id)
        return self._repos.focus_plans.active_for_cycle(
            child_id, validate_cycle_month(cycle_month))

    def list_plans(self, principal, child_id: str) -> List[MonthlyFocusPlan]:
        self._authorize(principal, child_id)
        return self._repos.focus_plans.list_for_child(child_id)

    def list_allocations(self, principal, focus_plan_id: str, *,
                         include_inactive: bool = False
                         ) -> List[MonthlyGoalAllocation]:
        plan = self._load_plan(focus_plan_id)
        self._authorize(principal, plan.child_id)
        return self._repos.goal_allocations.list_for_plan(
            focus_plan_id, include_inactive=include_inactive)

    def list_snapshots(self, principal, focus_plan_id: str
                       ) -> List[MonthlyGoalSnapshot]:
        """What the month was actually working toward."""
        plan = self._load_plan(focus_plan_id)
        self._authorize(principal, plan.child_id)
        return self._repos.goal_snapshots.list_for_plan(focus_plan_id)
