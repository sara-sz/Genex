"""pilot_backend/weekly/service.py — the only writer of the weekly layer.

Every operation begins with the unchanged 0.2 child-access gate and ends with
an audit event, exactly as `identity/`, `goals/` and `planning/` do. No method
takes a role, uid or actor id as a parameter — the principal is the only
source of identity, asserted structurally by the same test that has guarded
every slice since 0.4A.

## Who may do what

    record an observation      a caregiver authorized for the child
    record a plan edit/defer   a caregiver authorized for the child
    plan a weekly cycle        the ACTIVE managing clinician, or a caregiver
                               when the child has none (the 0.4C rule)
    create an intervention     the ACTIVE managing clinician, only

A provider who is connected to the child but is NOT the managing clinician
cannot plan or intervene. Clinical ownership is one recorded assignment so
that "who is responsible?" has one answer.

## Released plans are recorded against, never rewritten

An intervention targeting a cycle with `released_to_parent_at` set and a
snapshot captured is stored as INTENT. The snapshot repository has no update
method and the service never attempts one; `ReleasedPlanImmutable` is raised
for anything that tries to make a current-plan intervention mutate what the
family already holds. Proposal-and-acceptance wiring is 0.5.

## Capacity is finite, including for clinicians

A clinician-added activity consumes family capacity like anything else. If
that pushes a released cycle past the declared capacity, the ledger records
the overage and the reason — the service never silently removes something the
family already received. The NEXT cycle rebalances.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from ..audit.events import AuditAction, AuditResult
from ..authz.decisions import AccessDecision
from ..authz.policy import authorize_child_access
from ..domain.adaptation import AdaptationOrigin, AdaptationRecord
from ..domain.alignment import (
    ActivityGoalAlignment,
    AlignmentSource,
    CapacityLedger,
    CoverageGap,
)
from ..domain.goals import GoalRef
from ..domain.identity_claims import ClaimKind, IdentityClaim, key_digest
from ..domain.intervention import (
    InterventionAction,
    InterventionScope,
    TherapistIntervention,
)
from ..domain.monthly_plan import MonthlyPlanState
from ..domain.observation import (
    AttemptOutcome,
    CustomizationSignalType,
    DeferRecord,
    Difficulty,
    Enjoyment,
    ObservationEvent,
    ParentCustomizationSignal,
    TimezoneSource,
)
from ..domain.roles import ActorRole
from ..domain.source_link import SourceSystem
from ..domain.weekly_cycle import (
    GenerationReason,
    WeeklyCycle,
    WeeklyPlanLink,
    WeeklyPlanSnapshot,
    plan_cycle_bounds,
    starter_cycle_bounds,
)
from ..persistence.document_store import DocumentStoreError
from ..repository.interface import DuplicateRecord, RecordNotFound
from .adaptation import (
    ADAPTATION_RULE_VERSION,
    AdaptationPlan,
    describe_change,
    plan_adaptation,
)
from .allocator import (
    ALLOCATION_ENGINE_VERSION,
    ALLOCATION_RULE_VERSION,
    AllocationResult,
    CandidateActivity,
    allocate,
    effective_allocations,
    suppressed_identities,
)
from .errors import (
    ReleasedPlanImmutable,
    WeeklyAuthorizationError,
    WeeklyConflict,
    WeeklyValidationError,
)

def _default_repos_factory(store):
    from ..persistence.firestore_repos import FirestoreRepositories

    return FirestoreRepositories(store)


RESOURCE_CYCLE = "weekly_cycle"
RESOURCE_SNAPSHOT = "weekly_plan_snapshot"
RESOURCE_OBSERVATION = "observation_event"
RESOURCE_CUSTOMIZATION = "parent_customization_signal"
RESOURCE_DEFER = "defer_record"
RESOURCE_INTERVENTION = "therapist_intervention"
RESOURCE_ADAPTATION = "adaptation_record"


class WeeklyService:
    """Authorized reads and writes for cycles, evidence and adaptation."""

    def __init__(self, *, repos, recorder=None, now=None,
                 repos_factory=None) -> None:
        self._repos = repos
        self._recorder = recorder
        #: Builds a repository set bound to a transaction-scoped store. Kept
        #: injectable so this package imports no persistence module at load
        #: time; the default resolves lazily.
        self._repos_factory = repos_factory or _default_repos_factory
        #: Injectable clock for deterministic tests. No request parameter
        #: reaches it.
        self._now = now

    # -- write-time uniqueness ---------------------------------------------

    def _claim(self, kind: ClaimKind, parts: Tuple[str, ...], *,
               holder_ref: str, child_id: str, actor_id: Optional[str],
               generation: int) -> IdentityClaim:
        return IdentityClaim.build(
            kind, parts, generation, holder_ref=holder_ref, child_id=child_id,
            actor_id=actor_id, now=self._stamp())

    def _next_generation(self, kind: ClaimKind, parts: Tuple[str, ...]) -> int:
        """Read OUTSIDE any transaction — see identity/service.py for why.

        A stale generation cannot produce a second winner, only a collision,
        and a collision is the refusal we want.
        """
        return self._repos.identity_claims.next_generation(
            kind, key_digest(*parts))

    def _stamp(self) -> datetime:
        return self._now() if self._now else datetime.now(timezone.utc)

    # -- gates --------------------------------------------------------------

    def _authorize(self, principal, child_id: str) -> AccessDecision:
        decision = authorize_child_access(principal, child_id, self._repos)
        if not decision.allowed:
            raise WeeklyAuthorizationError(
                f"not permitted for this child ({decision.denial.value})")
        return decision

    def _require_caregiver(self, principal, what: str) -> None:
        if principal.role is not ActorRole.CAREGIVER:
            raise WeeklyAuthorizationError(f"{what} requires a caregiver")

    def _managing_assignment(self, child_id: str):
        active = self._repos.managing_clinicians.list_for_child(child_id)
        if len(active) > 1:
            raise WeeklyConflict("more than one active managing clinician")
        return active[0] if active else None

    def _require_managing_clinician(self, principal, child_id: str, what: str):
        """The caller must BE this child's active managing clinician."""
        if principal.role is not ActorRole.PROVIDER:
            raise WeeklyAuthorizationError(f"{what} requires a provider")
        assignment = self._managing_assignment(child_id)
        if assignment is None:
            raise WeeklyConflict("this child has no active managing clinician")
        if assignment.provider_id != principal.application_id:
            raise WeeklyAuthorizationError(
                f"{what} requires this child's managing clinician")
        return assignment

    def _require_planner(self, principal, child_id: str):
        """The 0.4C single-owner rule, reused unchanged for weekly planning.

        A child WITH a managing clinician is planned by that clinician; a
        child without one is planned by an authorized caregiver. One rule, so
        two parties can never set competing direction.
        """
        assignment = self._managing_assignment(child_id)
        if assignment is not None:
            if (principal.role is not ActorRole.PROVIDER
                    or assignment.provider_id != principal.application_id):
                raise WeeklyAuthorizationError(
                    "only this child's managing clinician may plan the week")
            return assignment
        if principal.role is not ActorRole.CAREGIVER:
            raise WeeklyAuthorizationError(
                "a child with no managing clinician is planned by a caregiver")
        return None

    def _audit(self, action: AuditAction, result: AuditResult,
               resource_type: str, *, principal, child_id: str,
               resource_id: Optional[str], request_id: str, **metadata) -> None:
        if self._recorder is None:
            return
        self._recorder.record_action(
            action, result, resource_type,
            resource_id=resource_id, child_id=child_id, principal=principal,
            request_id=request_id, metadata=metadata,
        )

    # -- loads --------------------------------------------------------------

    def _load_cycle(self, cycle_id: str) -> WeeklyCycle:
        try:
            return self._repos.weekly_cycles.get_by_id(cycle_id)
        except RecordNotFound:
            raise WeeklyConflict("no such weekly cycle") from None

    def _load_plan(self, focus_plan_id: str):
        try:
            return self._repos.focus_plans.get_by_id(focus_plan_id)
        except RecordNotFound:
            raise WeeklyConflict("no such monthly focus plan") from None

    def _require_active_plan(self, focus_plan_id: str):
        plan = self._load_plan(focus_plan_id)
        if plan.state is MonthlyPlanState.DRAFT:
            raise WeeklyConflict(
                "a weekly cycle requires an activated monthly focus plan")
        if plan.state is MonthlyPlanState.CLOSED:
            raise WeeklyConflict("a closed month cannot gain a weekly cycle")
        return plan

    # =====================================================================
    # Weekly cycle
    # =====================================================================

    def create_cycle(self, principal, focus_plan_id: str, *,
                     sequence_in_month: int,
                     predecessor_cycle_id: Optional[str] = None,
                     generation_reason: Optional[GenerationReason] = None,
                     first_plan_date: Optional[str] = None,
                     request_id: str = "") -> WeeklyCycle:
        """Open cycle N of a month. Bounds are DERIVED, never supplied.

        ## `first_plan_date` selects the ANCHOR, not the bounds

        0.6A-2 restores a frozen Genex invariant this layer declared and never
        wired — `PartialReason.PLAN_ACTIVATED_MIDWEEK`, unreferenced since 0.4D.

        Without it, bounds come from `plan_cycle_bounds`: cycle 1 starts on the
        FIRST OF THE MONTH. That is right for a month-anchored sequence and
        wrong for a child's first plan, because a family that onboarded on the
        7th would be handed activities dated the 1st.

        With it, bounds come from `starter_cycle_bounds`: cycle 1 runs from the
        first-plan date to that week's Sunday, and week 2 onward is Monday to
        Sunday. The caller supplies the DATE; the calendar rule stays in the
        domain, so no caller can hand in arbitrary bounds.

        Defaulted to None, so every existing caller keeps the month anchor and
        nothing already released changes.
        """
        plan = self._require_active_plan(focus_plan_id)
        self._authorize(principal, plan.child_id)
        self._require_planner(principal, plan.child_id)

        # Advisory read: a clear refusal in the ordinary case. The claim
        # below is AUTHORITATIVE.
        existing = self._repos.weekly_cycles.list_for_plan(focus_plan_id)
        if any(c.sequence_in_month == sequence_in_month for c in existing):
            raise WeeklyConflict("this cycle already exists for the month")

        if first_plan_date:
            starts_on, ends_on, is_partial, partial_reason = (
                starter_cycle_bounds(first_plan_date, sequence_in_month))
        else:
            starts_on, ends_on, is_partial, partial_reason = plan_cycle_bounds(
                plan.cycle_month, sequence_in_month)
        reason = generation_reason or (
            GenerationReason.FIRST_CYCLE_OF_MONTH if sequence_in_month == 1
            else GenerationReason.SEQUENTIAL)

        cycle = WeeklyCycle.create(
            focus_plan_id, plan.child_id,
            sequence_in_month=sequence_in_month,
            starts_on=starts_on, ends_on=ends_on,
            is_partial=is_partial, partial_reason=partial_reason,
            predecessor_cycle_id=predecessor_cycle_id,
            generation_reason=reason,
            engine_version=ALLOCATION_ENGINE_VERSION,
            now=self._stamp())

        # Claim and record commit together: a crash persists neither, so no
        # generation is consumed and a retry proceeds normally.
        parts = (focus_plan_id, str(sequence_in_month))
        generation = self._next_generation(ClaimKind.WEEKLY_CYCLE, parts)

        def _acquire(store) -> None:
            tx = self._repos_factory(store)
            tx.identity_claims.claim(self._claim(
                ClaimKind.WEEKLY_CYCLE, parts, holder_ref=cycle.cycle_id,
                child_id=plan.child_id, actor_id=principal.application_id,
                generation=generation))
            tx.weekly_cycles.create(cycle)

        try:
            self._repos.store.run_in_transaction(_acquire)
        except (DuplicateRecord, DocumentStoreError):
            self._audit(AuditAction.WEEKLY_CYCLE_CREATED, AuditResult.FAILURE,
                        RESOURCE_CYCLE, principal=principal,
                        child_id=plan.child_id, resource_id=None,
                        request_id=request_id, focus_plan_id=focus_plan_id,
                        cycle_sequence=sequence_in_month)
            raise WeeklyConflict(
                "another writer holds the claim for this cycle") from None

        self._audit(AuditAction.WEEKLY_CYCLE_CREATED, AuditResult.SUCCESS,
                    RESOURCE_CYCLE, principal=principal, child_id=plan.child_id,
                    resource_id=cycle.cycle_id, request_id=request_id,
                    cycle_id=cycle.cycle_id, focus_plan_id=focus_plan_id,
                    cycle_sequence=sequence_in_month,
                    cycle_month=plan.cycle_month,
                    is_partial_week=is_partial,
                    engine_version=ALLOCATION_ENGINE_VERSION)
        return cycle

    def link_external_plan(self, principal, cycle_id: str,
                           source_system: SourceSystem, external_plan_id: str,
                           *, coverage_local_dates: Tuple[str, ...] = (),
                           request_id: str = "") -> WeeklyPlanLink:
        """Bind a source-system plan by EXTERNAL id. Never a canonical key."""
        cycle = self._load_cycle(cycle_id)
        self._authorize(principal, cycle.child_id)
        self._require_planner(principal, cycle.child_id)

        link = WeeklyPlanLink.create(
            cycle_id, source_system, external_plan_id,
            coverage_local_dates=coverage_local_dates, now=self._stamp())
        self._repos.weekly_plan_links.create(link)
        self._audit(AuditAction.WEEKLY_PLAN_LINKED, AuditResult.SUCCESS,
                    RESOURCE_CYCLE, principal=principal, child_id=cycle.child_id,
                    resource_id=link.link_id, request_id=request_id,
                    cycle_id=cycle_id, source_system=source_system.value,
                    link_id=link.link_id)
        return link

    def capture_snapshot(self, principal, cycle_id: str,
                         source_system: SourceSystem, source_plan_id: str,
                         document: Mapping[str, Any], *,
                         source_generated_at: Optional[datetime] = None,
                         request_id: str = "") -> WeeklyPlanSnapshot:
        """Freeze what the parent-facing plan contained. Create-only."""
        cycle = self._load_cycle(cycle_id)
        self._authorize(principal, cycle.child_id)
        self._require_planner(principal, cycle.child_id)

        if self._repos.weekly_plan_snapshots.list_for_cycle(cycle_id):
            raise WeeklyConflict("this cycle already has a plan snapshot")

        snapshot = WeeklyPlanSnapshot.capture(
            cycle_id, source_system, source_plan_id, document,
            source_generated_at=source_generated_at, now=self._stamp())
        self._repos.weekly_plan_snapshots.create(snapshot)
        self._audit(AuditAction.WEEKLY_PLAN_SNAPSHOT_CAPTURED,
                    AuditResult.SUCCESS, RESOURCE_SNAPSHOT, principal=principal,
                    child_id=cycle.child_id, resource_id=snapshot.snapshot_id,
                    request_id=request_id, cycle_id=cycle_id,
                    snapshot_id=snapshot.snapshot_id,
                    source_system=source_system.value)
        return snapshot

    def release_cycle(self, principal, cycle_id: str, *,
                      request_id: str = "") -> WeeklyCycle:
        """Mark the cycle as given to the family.

        Requires a snapshot: releasing a plan whose content was never captured
        would leave nothing to compare a later change against, which is the
        whole reason the snapshot exists.
        """
        cycle = self._load_cycle(cycle_id)
        self._authorize(principal, cycle.child_id)
        self._require_planner(principal, cycle.child_id)
        if not self._repos.weekly_plan_snapshots.list_for_cycle(cycle_id):
            raise WeeklyConflict(
                "a cycle cannot be released before its plan is snapshotted")

        released = cycle.release(now=self._stamp())
        self._repos.weekly_cycles.update(released)
        self._audit(AuditAction.WEEKLY_CYCLE_RELEASED, AuditResult.SUCCESS,
                    RESOURCE_CYCLE, principal=principal, child_id=cycle.child_id,
                    resource_id=cycle_id, request_id=request_id,
                    cycle_id=cycle_id,
                    cycle_sequence=cycle.sequence_in_month)
        return released

    # =====================================================================
    # Allocation
    # =====================================================================

    def allocate_cycle(self, principal, cycle_id: str,
                       candidates: Sequence[CandidateActivity], *,
                       family_declared_capacity: int,
                       request_id: str = "") -> AllocationResult:
        """Place activities, write alignments, gaps and the capacity ledger.

        Uses the allocations EFFECTIVE for this cycle (section 26), so a
        mid-month reprioritisation changes future cycles without rewriting
        what a past cycle did.
        """
        cycle = self._load_cycle(cycle_id)
        self._authorize(principal, cycle.child_id)
        self._require_planner(principal, cycle.child_id)
        if cycle.is_released:
            raise ReleasedPlanImmutable(
                "this cycle has been released; allocate a future cycle instead")
        if self._repos.alignments.list_for_cycle(cycle_id):
            raise WeeklyConflict("this cycle has already been allocated")

        plan = self._load_plan(cycle.owning_focus_plan_id)
        allocations = effective_allocations(
            self._repos.goal_allocations.list_for_plan(
                cycle.owning_focus_plan_id, include_inactive=True),
            cycle.sequence_in_month)
        self._reject_foreign_goals(allocations, cycle)

        defers = self._repos.defer_records.list_for_child(cycle.child_id)
        result = allocate(
            cycle_id, allocations, candidates,
            capacity=family_declared_capacity,
            suppressed_identity_refs=suppressed_identities(
                defers, cycle.sequence_in_month),
            is_partial_week=cycle.is_partial)

        # Claim, alignments, gaps and ledger commit in ONE transaction.
        #
        # Every write here is a `create`, so unlike 0.4C activation there is
        # no `set` and therefore no boundary to recover across: a crash
        # persists nothing at all, and a losing writer leaves nothing behind.
        parts = (cycle_id,)
        generation = self._next_generation(ClaimKind.WEEKLY_ALLOCATION, parts)
        ledger = CapacityLedger.create(
            cycle_id, cycle.child_id, family_declared_capacity,
            allocated_by_planner=result.placed_count, now=self._stamp())

        def _acquire(store) -> None:
            tx = self._repos_factory(store)
            tx.identity_claims.claim(self._claim(
                ClaimKind.WEEKLY_ALLOCATION, parts, holder_ref=cycle_id,
                child_id=cycle.child_id, actor_id=principal.application_id,
                generation=generation))
            self._persist_allocation(tx, cycle, allocations, result)
            tx.capacity_ledgers.create(ledger)

        try:
            self._repos.store.run_in_transaction(_acquire)
        except (DuplicateRecord, DocumentStoreError):
            self._audit(AuditAction.WEEKLY_CYCLE_ALLOCATED, AuditResult.FAILURE,
                        RESOURCE_CYCLE, principal=principal,
                        child_id=cycle.child_id, resource_id=cycle_id,
                        request_id=request_id, cycle_id=cycle_id)
            raise WeeklyConflict(
                "another writer holds the allocation claim for this cycle"
            ) from None

        self._audit(AuditAction.WEEKLY_CYCLE_ALLOCATED, AuditResult.SUCCESS,
                    RESOURCE_CYCLE, principal=principal, child_id=cycle.child_id,
                    resource_id=cycle_id, request_id=request_id,
                    cycle_id=cycle_id, activity_count=result.placed_count,
                    coverage_gap_count=len(result.gaps),
                    declared_capacity=family_declared_capacity,
                    rule_version=ALLOCATION_RULE_VERSION)
        return result

    def _reject_foreign_goals(self, allocations, cycle: WeeklyCycle) -> None:
        """Every allocation must belong to this child AND this focus plan."""
        for allocation in allocations:
            if allocation.child_id != cycle.child_id:
                raise WeeklyValidationError(
                    "allocation belongs to a different child")
            if allocation.focus_plan_id != cycle.owning_focus_plan_id:
                raise WeeklyValidationError(
                    "allocation belongs to a different focus plan")

    def _persist_allocation(self, tx, cycle: WeeklyCycle, allocations,
                            result: AllocationResult) -> None:
        """Write alignments and gaps through the TRANSACTION-scoped repos."""
        by_goal = {a.goal_ref.as_key(): a for a in allocations}
        for placement in result.placements:
            for goal_ref, role in placement.aligned_goals:
                allocation = by_goal.get(goal_ref.as_key())
                tx.alignments.create(ActivityGoalAlignment.create(
                    cycle.cycle_id, cycle.child_id,
                    placement.activity_instance_ref, goal_ref,
                    activity_identity_ref=placement.activity_identity_ref,
                    role=role,
                    alignment_source=placement.alignment_source,
                    milestone_refs=placement.milestone_refs,
                    rule_version=result.rule_version,
                    allocation_id=(allocation.allocation_id
                                   if allocation else None),
                    now=self._stamp()))
        for gap in result.gaps:
            tx.coverage_gaps.create(CoverageGap.create(
                cycle.cycle_id, cycle.child_id, gap.goal_ref, gap.reason,
                capacity_available=gap.capacity_available,
                capacity_required=gap.capacity_required,
                rule_version=result.rule_version, detail=gap.detail,
                now=self._stamp()))

    # =====================================================================
    # Caregiver evidence
    # =====================================================================

    def record_observation(self, principal, cycle_id: str,
                           activity_instance_ref: str, *,
                           local_date: str, attempt_outcome: AttemptOutcome,
                           occurred_at: Optional[datetime] = None,
                           difficulty: Optional[Difficulty] = None,
                           enjoyment: Optional[Enjoyment] = None,
                           assistance: str = "", child_response: str = "",
                           observation_text_ref: str = "",
                           source_feedback_id: str = "",
                           request_id: str = "") -> ObservationEvent:
        """Record one attempt. No clinical inference anywhere in this path."""
        cycle = self._load_cycle(cycle_id)
        self._authorize(principal, cycle.child_id)
        self._require_caregiver(principal, "recording an observation")

        if not cycle.covers(local_date):
            raise WeeklyValidationError(
                "local date is outside the cycle it is recorded against")
        self._require_instance_in_cycle(cycle, activity_instance_ref)

        plan = self._load_plan(cycle.owning_focus_plan_id)
        event = ObservationEvent.record(
            cycle.child_id, cycle_id, activity_instance_ref,
            local_date=local_date,
            occurred_at=occurred_at or self._stamp(),
            # The plan's timezone of record. Validated at plan creation and
            # never defaulted to UTC — attribution depends on it.
            timezone_of_record=plan.timezone_of_record,
            tz_source=TimezoneSource.PLAN_OF_RECORD,
            attempt_outcome=attempt_outcome,
            difficulty=difficulty, enjoyment=enjoyment,
            assistance=assistance, child_response=child_response,
            observation_text_ref=observation_text_ref,
            source_feedback_id=source_feedback_id,
            recorded_by_caregiver_id=principal.application_id,
            now=self._stamp())
        self._repos.observation_events.create(event)

        self._audit(AuditAction.OBSERVATION_RECORDED, AuditResult.SUCCESS,
                    RESOURCE_OBSERVATION, principal=principal,
                    child_id=cycle.child_id, resource_id=event.event_id,
                    request_id=request_id, cycle_id=cycle_id,
                    event_id=event.event_id,
                    attempt_outcome=attempt_outcome.value,
                    attribution_month=event.attribution_month)
        return event

    def _require_instance_in_cycle(self, cycle: WeeklyCycle,
                                   activity_instance_ref: str) -> None:
        """The instance must be aligned in THIS cycle.

        Fails closed on a cross-cycle or cross-child reference. Without this,
        a caregiver authorized for one child could post evidence against
        another child's activity instance by guessing its reference.
        """
        known = {a.activity_instance_ref
                 for a in self._repos.alignments.list_for_cycle(cycle.cycle_id)}
        if activity_instance_ref not in known:
            raise WeeklyValidationError(
                "activity instance does not belong to this cycle")

    def record_customization(self, principal, cycle_id: str,
                             activity_instance_ref: str,
                             signal_type: CustomizationSignalType, *,
                             source_overlay_ref: str = "",
                             request_id: str = "") -> ParentCustomizationSignal:
        """Record a family plan edit. Never child performance."""
        cycle = self._load_cycle(cycle_id)
        self._authorize(principal, cycle.child_id)
        self._require_caregiver(principal, "recording a plan customization")
        self._require_instance_in_cycle(cycle, activity_instance_ref)

        signal = ParentCustomizationSignal.create(
            cycle_id, cycle.child_id, activity_instance_ref, signal_type,
            actor_id=principal.application_id,
            source_overlay_ref=source_overlay_ref, now=self._stamp())
        self._repos.customization_signals.create(signal)
        self._audit(AuditAction.PLAN_CUSTOMIZATION_RECORDED, AuditResult.SUCCESS,
                    RESOURCE_CUSTOMIZATION, principal=principal,
                    child_id=cycle.child_id, resource_id=signal.signal_id,
                    request_id=request_id, cycle_id=cycle_id,
                    signal_id=signal.signal_id,
                    customization_signal_type=signal_type.value)
        return signal

    def defer_activity(self, principal, cycle_id: str,
                       activity_instance_ref: str, *,
                       suppress_for_cycles: int = 1,
                       request_id: str = "") -> DeferRecord:
        """Save for Later. Suppresses the NEXT cycle; never a retirement."""
        cycle = self._load_cycle(cycle_id)
        self._authorize(principal, cycle.child_id)
        self._require_caregiver(principal, "deferring an activity")
        self._require_instance_in_cycle(cycle, activity_instance_ref)

        identity = self._identity_for_instance(cycle_id, activity_instance_ref)
        record = DeferRecord.create(
            cycle.child_id, activity_instance_ref, identity,
            actor_id=principal.application_id, actor_role=principal.role,
            from_cycle_id=cycle_id,
            from_cycle_sequence=cycle.sequence_in_month,
            suppress_for_cycles=suppress_for_cycles, now=self._stamp())
        self._repos.defer_records.create(record)
        # A defer is also a plan customization: both rows exist because they
        # answer different questions, and the signal is what adaptation reads.
        self._repos.customization_signals.create(
            ParentCustomizationSignal.create(
                cycle_id, cycle.child_id, activity_instance_ref,
                CustomizationSignalType.DEFERRED,
                actor_id=principal.application_id, now=self._stamp()))

        self._audit(AuditAction.ACTIVITY_DEFERRED, AuditResult.SUCCESS,
                    RESOURCE_DEFER, principal=principal, child_id=cycle.child_id,
                    resource_id=record.defer_id, request_id=request_id,
                    cycle_id=cycle_id, defer_id=record.defer_id,
                    suppression_until_cycle=record.suppression_until_cycle)
        return record

    def _identity_for_instance(self, cycle_id: str,
                               activity_instance_ref: str) -> str:
        for alignment in self._repos.alignments.list_for_cycle(cycle_id):
            if alignment.activity_instance_ref == activity_instance_ref:
                return alignment.activity_identity_ref
        raise WeeklyValidationError(
            "activity instance does not belong to this cycle")

    def override_defer(self, principal, defer_id: str, *, reason: str,
                       request_id: str = "") -> DeferRecord:
        """Clinician override of a caregiver defer, with full provenance.

        The ONLY route to early reuse of a deferred activity. `reason` is a
        required keyword with no default, and the caller must be the managing
        clinician. The allocator has no access to this method — section 15: a
        system-only coverage floor must never silently defeat a defer.
        """
        try:
            record = self._repos.defer_records.get_by_id(defer_id)
        except RecordNotFound:
            raise WeeklyConflict("no such defer record") from None

        self._authorize(principal, record.child_id)
        self._require_managing_clinician(principal, record.child_id,
                                         "overriding a defer")
        if record.was_overridden:
            raise WeeklyConflict("this defer has already been overridden")

        overridden = record.with_clinician_override(
            actor_id=principal.application_id, reason=reason, now=self._stamp())
        self._repos.defer_records.update(overridden)
        self._audit(AuditAction.DEFER_OVERRIDDEN, AuditResult.SUCCESS,
                    RESOURCE_DEFER, principal=principal,
                    child_id=record.child_id, resource_id=defer_id,
                    request_id=request_id, defer_id=defer_id,
                    provider_id=principal.application_id)
        return overridden

    # =====================================================================
    # Therapist intervention
    # =====================================================================

    def create_intervention(self, principal, cycle_id: str, *,
                            action: InterventionAction,
                            applies_to: InterventionScope,
                            clinical_rationale: str,
                            target_ref: str = "", guidance_text: str = "",
                            request_id: str = "") -> TherapistIntervention:
        """Record a clinician planning decision. Managing clinician only.

        A CURRENT_PLAN intervention against a released cycle is stored as
        INTENT and does not touch the snapshot. Anything that would require
        rewriting released content raises `ReleasedPlanImmutable`.
        """
        cycle = self._load_cycle(cycle_id)
        self._authorize(principal, cycle.child_id)
        assignment = self._require_managing_clinician(
            principal, cycle.child_id, "creating a planning intervention")

        if target_ref:
            self._require_instance_in_cycle(cycle, target_ref)

        if (applies_to is InterventionScope.CURRENT_PLAN
                and cycle.is_released
                and action in (InterventionAction.REPLACE,
                               InterventionAction.REMOVE_FROM_SCHEDULING)):
            self._audit(AuditAction.THERAPIST_INTERVENTION_CREATED,
                        AuditResult.FAILURE, RESOURCE_INTERVENTION,
                        principal=principal, child_id=cycle.child_id,
                        resource_id=None, request_id=request_id,
                        cycle_id=cycle_id, intervention_action=action.value,
                        intervention_scope=applies_to.value)
            raise ReleasedPlanImmutable(
                "this cycle was released; replacing or removing content would "
                "rewrite what the family already holds — target a future cycle")

        intervention = TherapistIntervention.create(
            cycle.child_id, cycle_id, principal.application_id,
            action=action, applies_to=applies_to,
            clinical_rationale=clinical_rationale, target_ref=target_ref,
            guidance_text=guidance_text,
            managing_assignment_id=assignment.assignment_id,
            now=self._stamp())
        self._repos.interventions.create(intervention)

        if intervention.consumes_capacity:
            self._charge_clinician_capacity(cycle_id, action)

        self._audit(AuditAction.THERAPIST_INTERVENTION_CREATED,
                    AuditResult.SUCCESS, RESOURCE_INTERVENTION,
                    principal=principal, child_id=cycle.child_id,
                    resource_id=intervention.intervention_id,
                    request_id=request_id, cycle_id=cycle_id,
                    intervention_id=intervention.intervention_id,
                    intervention_action=action.value,
                    intervention_scope=applies_to.value,
                    provider_id=principal.application_id,
                    assignment_id=assignment.assignment_id)
        return intervention

    def _charge_clinician_capacity(self, cycle_id: str,
                                   action: InterventionAction) -> None:
        """A clinician-added activity consumes family capacity. Section 18.

        Records overage rather than removing anything the family already
        received. Future-cycle generation rebalances.
        """
        ledgers = self._repos.capacity_ledgers.list_for_cycle(cycle_id)
        if not ledgers:
            return
        ledger = ledgers[0]
        self._repos.capacity_ledgers.update(
            ledger.with_clinician_add(
                1, reason=f"clinician_{action.value}", now=self._stamp()))

    # =====================================================================
    # Week N -> Week N+1
    # =====================================================================

    def generate_next_cycle(self, principal, from_cycle_id: str,
                            candidates: Sequence[CandidateActivity], *,
                            family_declared_capacity: int,
                            request_id: str = ""
                            ) -> Tuple[WeeklyCycle, AllocationResult,
                                       AdaptationRecord]:
        """Generate cycle N+1 from cycle N's evidence. General in N.

        Nothing here is specific to cycle 2: the sequence is read from the
        predecessor and incremented.
        """
        previous = self._load_cycle(from_cycle_id)
        self._authorize(principal, previous.child_id)
        self._require_planner(principal, previous.child_id)

        plan = self._require_active_plan(previous.owning_focus_plan_id)
        next_sequence = previous.sequence_in_month + 1

        alignments = self._repos.alignments.list_for_cycle(from_cycle_id)
        events = self._repos.observation_events.list_for_cycle(from_cycle_id)
        signals = self._repos.customization_signals.list_for_cycle(from_cycle_id)
        defers = self._repos.defer_records.list_for_child(previous.child_id)
        gaps = self._repos.coverage_gaps.list_for_cycle(from_cycle_id)
        future = [i for i in self._repos.interventions.list_for_cycle(from_cycle_id)
                  if i.applies_to is InterventionScope.FUTURE_CYCLE]

        adaptation = plan_adaptation(
            alignments, events=events, customization_signals=signals,
            defer_records=defers, interventions=future, coverage_gaps=gaps,
            next_cycle_sequence=next_sequence)

        cycle = self.create_cycle(
            principal, previous.owning_focus_plan_id,
            sequence_in_month=next_sequence,
            predecessor_cycle_id=from_cycle_id,
            generation_reason=GenerationReason.SEQUENTIAL,
            request_id=request_id)

        # Suppressed identities are excluded from the candidate pool rather
        # than filtered afterwards, so a coverage floor can never reach one.
        usable = [c for c in candidates
                  if c.activity_identity_ref
                  not in adaptation.suppressed_identity_refs]
        result = self.allocate_cycle(
            principal, cycle.cycle_id, usable,
            family_declared_capacity=family_declared_capacity,
            request_id=request_id)

        record = AdaptationRecord.create(
            previous.child_id, previous.owning_focus_plan_id,
            from_cycle_id, cycle.cycle_id,
            normalized_signals=adaptation.signals,
            evidence_event_ids=tuple(sorted(e.event_id for e in events)),
            customization_signal_ids=tuple(sorted(s.signal_id for s in signals)),
            defer_record_ids=tuple(sorted(
                d.defer_id for d in defers
                if d.suppresses(next_sequence) and not d.was_overridden)),
            rule_version=ADAPTATION_RULE_VERSION,
            origin=adaptation.origin,
            intervention_id=(future[0].intervention_id if future and
                             adaptation.origin is not AdaptationOrigin.AUTOMATIC
                             else None),
            goal_alignment_before=tuple(sorted(
                a.alignment_id for a in alignments)),
            goal_alignment_after=tuple(sorted(
                a.alignment_id for a in
                self._repos.alignments.list_for_cycle(cycle.cycle_id))),
            coverage_gaps=tuple(sorted(g.gap_id for g in gaps)),
            resulting_change=describe_change(adaptation),
            now=self._stamp())
        self._repos.adaptation_records.create(record)
        self._repos.weekly_cycles.update(
            cycle.with_adaptation(record.record_id, now=self._stamp()))

        self._audit(AuditAction.ADAPTATION_RECORDED, AuditResult.SUCCESS,
                    RESOURCE_ADAPTATION, principal=principal,
                    child_id=previous.child_id, resource_id=record.record_id,
                    request_id=request_id,
                    adaptation_record_id=record.record_id,
                    cycle_id=cycle.cycle_id,
                    adaptation_origin=adaptation.origin.value,
                    signal_count=len(adaptation.signals),
                    rule_version=ADAPTATION_RULE_VERSION)
        return cycle, result, record

    # =====================================================================
    # Reads
    # =====================================================================

    def get_cycle(self, principal, cycle_id: str) -> WeeklyCycle:
        cycle = self._load_cycle(cycle_id)
        self._authorize(principal, cycle.child_id)
        return cycle

    def list_cycles(self, principal, focus_plan_id: str) -> List[WeeklyCycle]:
        plan = self._load_plan(focus_plan_id)
        self._authorize(principal, plan.child_id)
        return self._repos.weekly_cycles.list_for_plan(focus_plan_id)

    def list_alignments(self, principal, cycle_id: str
                        ) -> List[ActivityGoalAlignment]:
        cycle = self._load_cycle(cycle_id)
        self._authorize(principal, cycle.child_id)
        return self._repos.alignments.list_for_cycle(cycle_id)

    def list_coverage_gaps(self, principal, cycle_id: str) -> List[CoverageGap]:
        cycle = self._load_cycle(cycle_id)
        self._authorize(principal, cycle.child_id)
        return self._repos.coverage_gaps.list_for_cycle(cycle_id)

    def list_observations(self, principal, cycle_id: str
                          ) -> List[ObservationEvent]:
        cycle = self._load_cycle(cycle_id)
        self._authorize(principal, cycle.child_id)
        return self._repos.observation_events.list_for_cycle(cycle_id)

    def capacity_for(self, principal, cycle_id: str) -> Optional[CapacityLedger]:
        cycle = self._load_cycle(cycle_id)
        self._authorize(principal, cycle.child_id)
        ledgers = self._repos.capacity_ledgers.list_for_cycle(cycle_id)
        return ledgers[0] if ledgers else None

    def coverage_summary(self, principal, focus_plan_id: str, *,
                         attribution_month: Optional[str] = None):
        """Totals and per-goal attributions across the month's cycles.

        Attribution is by each event's LOCAL month, so a cycle spanning a
        boundary contributes each day to the month that day falls in.
        """
        from .counting import summarize

        plan = self._load_plan(focus_plan_id)
        self._authorize(principal, plan.child_id)
        alignments: List[ActivityGoalAlignment] = []
        events: List[ObservationEvent] = []
        for cycle in self._repos.weekly_cycles.list_for_plan(focus_plan_id):
            alignments.extend(self._repos.alignments.list_for_cycle(cycle.cycle_id))
            events.extend(
                self._repos.observation_events.list_for_cycle(cycle.cycle_id))
        return summarize(alignments, events, attribution_month=attribution_month)
