"""pilot_backend/integration/week_one_release.py — approved goal -> Week 1.

0.6A-2. ONE provider action that composes the EXISTING transitions:

    ClinicalGoal validation    goals.get_goal / goal_anchor / is_activity_mappable
    clinical plan              governance only — clinician-created and
                               ACTIVATED beforehand; never created here
    goal allocation            planning.allocate_goal
    monthly activation         planning.activate_plan
    Week 1 cycle               weekly.create_cycle
    candidates                 activity_bank.candidates_for_goal (static bank)
    allocation                 weekly.allocate_cycle            <- capacity here
    immutable snapshot         weekly.capture_snapshot
    release                    weekly.release_cycle

## IT COMPOSES; IT DOES NOT BYPASS

Every state change above is an existing service method with its own
authorization, its own claim and its own refusals. This module adds no planning
model, no second allocator and no new idempotency subsystem — it orders the
calls and refuses when one of them says no.

The founder's rule was that the low-level transitions must not be exposed
individually to the browser merely to make this possible. They are not: the
route calls this orchestrator, and this orchestrator calls them.

## CAPACITY IS A REQUIRED INPUT AND IS NEVER DEFAULTED

`family_declared_capacity` has no default here, no fallback and no inferred
value. It means what `CapacityLedger` says it means — what the FAMILY reports it
can carry — recorded by the provider for planning. It is NOT a recommended
frequency, a prescribed frequency, a clinical dosage or a required number of
sessions, and nothing in this module treats it as one: it is passed straight to
the existing allocator as capacity and straight to the existing ledger.

Absent or non-positive capacity REFUSES. A default would quietly author a
clinical frequency, which is exactly what the founder forbade.

Provenance uses the narrowest existing mechanism: `allocate_cycle` already emits
an audit event carrying `declared_capacity` together with the acting principal,
and `audit/events.py` already allowlists that field. No model change.

## THE GOAL GATE

A week may be generated only from an APPROVED ClinicalGoal that is
active, carries a current `GoalVersion`, has a `ClinicalGoalAnchor`, and whose
anchor is activity-mappable. A `GoalSuggestion` is never an input: a suggestion
is an offer, and planning from one would mean a family received activities for a
target no clinician accepted.

## NO ACTIVITY CONTENT IS AUTHORED HERE

Content comes only from the reviewed static bank, reached through the goal's own
anchor `family_bindings`. No runtime model, no Parent activity engine, no
generic fallback, and no cross-family substitution — `require_all_families_served`
refuses before anything is written if a bound family has no reviewed content.

## REPLAY

Re-running after release returns the EXISTING released week rather than building
a second one, and a different capacity cannot rewrite it. The underlying
guarantees are the existing ones: `create_cycle` refuses a duplicate sequence,
`allocate_cycle` holds a `ClaimKind.WEEKLY_ALLOCATION` claim and refuses a
second allocation, `capture_snapshot` refuses a second snapshot, and
`release_cycle` refuses a second release.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from ..domain.adaptation_decision import (
    AdaptationDecisionError,
    GoalInvariantProof,
)
from ..domain.clinical_period import local_date_in
from ..domain.goals import GoalKind, GoalRef, GoalStatus
from ..domain.monthly_plan import validate_cycle_month
from ..domain.weekly_cycle import (
    GenerationReason,
    WeeklyCycleError,
    starter_cycle_bounds,
    starter_sequence_for,
)
from ..domain.source_link import SourceSystem
from ..domain.weekly_plan_document import (
    build_document,
    parent_week_view,
)
from ..weekly.errors import (
    InProgressCapacityMismatch,
    InProgressStateMismatch,
    ReleasedPlanImmutable,
    WeeklyConflict,
)
from ..weekly.repeated_signal_policy import (
    RepeatedSignalPolicyError,
    decide as decide_adaptation,
)
from .activity_bank import candidates_for_goal, require_all_families_served
from .adaptive_week import (
    AdaptiveSelectionError,
    select_week_templates,
    verify_reviewed_and_bounded,
)
from .governing_plan import (
    Governance,
    GovernanceOutcome,
    GoverningPlanAmbiguous,
    resolve_governance,
)

def _canonical(document) -> str:
    """A type-stable form for comparing a stored snapshot to a rebuilt one."""
    import json  # noqa: WPS433
    return json.dumps(document, sort_keys=True, default=str)


#: Week 1 of a month. Retained as the FIRST cycle's sequence number, which is
#: what `plan_cycle_bounds` numbers from — not as the sequence this route
#: releases. See `current_sequence_for`.
WEEK_ONE = 1

#: The source system recorded on the snapshot. THERAPIST because the Pilot — the
#: therapist platform — now AUTHORS this week. v1 snapshots captured PARENT's
#: own plan; this is not that, and recording PARENT would misattribute it.
SNAPSHOT_SOURCE_SYSTEM = SourceSystem.THERAPIST


class WeekOneReleaseError(Exception):
    """Week 1 could not be released. PHI-safe: never quotes activity content."""

    PHI_SAFE_MESSAGE = True


class CurrentCycleUnresolved(WeekOneReleaseError):
    """Today does not fall inside any cycle of the plan's month.

    Reachable when a release is attempted for a month that is not the current
    one. Refused rather than defaulted to cycle 1: releasing a week that has
    already ended would put a "this week" in front of a family that is in the
    past.
    """


class ClinicalRenewalRequired(WeekOneReleaseError):
    """P0-C. No clinician-approved plan's clinical period holds this week.

    The child has had a plan, but every plan's period has ended (or cannot
    hold the whole week). Nothing is created, auto-renewed or copied forward:
    continuing requires a clinician to review and activate a plan.
    """


class ClinicalPlanRequired(WeekOneReleaseError):
    """First-plan timezone gate. The child has never had a clinician-ACTIVATED
    plan, so there is nothing to govern Week 1.

    Week 1 is released only under a plan the clinician created — with an
    explicit, validated IANA `timezone_of_record` — and then activated. This
    service never creates, allocates or activates a first plan, so no plan can
    ever acquire a defaulted timezone. A draft is not enough: it has no clinical
    period and governs nothing. Refused before any write.
    """


class GoverningPlanConflict(WeekOneReleaseError):
    """P0-C. A caller-supplied month disagrees with the governing plan, or the
    governing week could not be resolved unambiguously. Fail closed: an
    explicit month may agree with clinical governance but never override it.
    """


class CapacityRequired(WeekOneReleaseError):
    """No family-declared weekly capacity was supplied.

    REFUSED rather than defaulted. The number of activities a week contains is a
    direct function of this value, so inventing one would author a clinical
    frequency that no family declared and no clinician approved.
    """


class GoalNotReleasable(WeekOneReleaseError):
    """The goal cannot drive activity generation.

    Not active, no current version, no canonical anchor, or an anchor whose
    activity families are not reconciled. Fail closed: a week built on one of
    these would give a family activities for a target the system cannot defend.
    """


class NonAdaptiveWeekRefused(WeekOneReleaseError):
    """P0-B1. Week 2 onward may be created ONLY through the adaptive path.

    The pre-P0-B composition created any current cycle with plain
    `create_cycle` and the content-digest allocator, which would release a
    Week N+1 that ignored the family's feedback and — because the allocator
    has no memory of earlier weeks — very likely repeated Week N exactly. That
    path is closed for sequence >= 2; reaching it is a refusal.
    """


class AdaptiveWeekUnavailable(WeekOneReleaseError):
    """Week N+1 has no released Week N to adapt from.

    Fail closed rather than plan from nothing: a gap in the sequence (a missed
    week) is a decision for a clinician, not something to paper over.
    """


class AdaptiveWeekStateMismatch(WeekOneReleaseError):
    """An unreleased Week N+1 exists but is NOT what this request would write.

    P0-B1 hardening. A draft left by an interrupted attempt is RESUMED when
    every persisted piece of it — the cycle's identity and bounds, its
    allocation, its adaptation record and decision, its snapshot — matches the
    deterministic recomputation exactly. Anything else is refused: resuming
    state this request did not produce could release a week nobody approved,
    and repairing it would hide how it arose.
    """


class AdaptiveWeekCapacityMismatch(AdaptiveWeekStateMismatch):
    """A retry named a different family-declared capacity after the first
    allocation of this week was persisted. The recorded capacity stands for
    the in-progress week; a retry may not silently rewrite it."""


class ClinicalBoundaryRefused(WeekOneReleaseError):
    """Week N+1 would move the goal, its version, its anchor or its families.

    P0-B1 may change WHICH reviewed activity is offered and nothing else. Any
    difference in the approved clinical boundary between Week N and now is
    refused before a single write.
    """


@dataclass(frozen=True)
class ReleasableGoal:
    """One approved goal that passed the gate, with everything release needs.

    A named record rather than a tuple: four positional values that all happen
    to be objects is exactly where a later reader swaps two of them.
    """

    goal: Any
    version_id: str
    text: str
    anchor: Any

    @property
    def goal_id(self) -> str:
        return self.goal.clinical_goal_id

    @property
    def ref(self) -> GoalRef:
        return GoalRef(GoalKind.CLINICAL, self.goal_id)


@dataclass(frozen=True)
class WeekOneOutcome:
    """What one release request produced or found already present."""

    child_id: str
    focus_plan_id: str
    cycle_id: str
    goal_ids: Tuple[str, ...]
    activity_count: int
    #: False when Week 1 was already released — the existing week is returned.
    created: bool
    released_at: Optional[datetime]
    snapshot_id: str
    #: The Parent-facing projection, derived from the frozen snapshot.
    parent_week: Dict[str, Any]


class WeekOneReleaseService:
    """Compose the existing transitions into one provider action."""

    def __init__(self, *, repos: Any, goals: Any, plans: Any, weekly: Any,
                 activity_bank: Any, now: Any = None) -> None:
        #: Injectable clock, the same seam every other service takes. "Current"
        #: is a calendar question, so a test must be able to pin the date.
        from ..domain.entities import utc_now  # noqa: WPS433

        self._now = now or utc_now
        self._repos = repos
        self._goals = goals
        self._plans = plans
        self._weekly = weekly
        self._bank = activity_bank

    # -- the goal gate ----------------------------------------------------

    def releasable_goals(self, principal, child_id: str
                         ) -> Tuple["ReleasableGoal", ...]:
        """Every approved ClinicalGoal that may drive activities.

        Ordered by goal id, so the whole operation is deterministic. Side-effect
        free, so a reviewer can inspect the gate without releasing anything.

        The version is read as `current_version_id` plus `current_text`, both
        existing authorized reads — this module never reaches into
        `goal_versions` itself.
        """
        found: List[ReleasableGoal] = []
        for goal in self._goals.list_clinical_goals(principal, child_id):
            if goal.status is not GoalStatus.ACTIVE:
                continue
            version_id = (goal.current_version_id or "").strip()
            if not version_id:
                # A goal with no current version has no approved wording, so
                # there is nothing a family could be shown.
                continue
            ref = GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id)
            anchor = self._goals.goal_anchor(principal, ref)
            if anchor is None or not anchor.is_activity_mappable:
                # Unanchored or unmappable. Skipped here and refused below if
                # NOTHING is releasable — an unmappable goal must not block a
                # mappable sibling, the same rule F-B v2 follows.
                continue
            found.append(ReleasableGoal(
                goal=goal, version_id=version_id,
                text=self._goals.current_text(principal, ref), anchor=anchor))
        return tuple(sorted(found, key=lambda row: row.goal_id))

    # -- the operation ----------------------------------------------------

    def release_week_one(self, principal, child_id: str, *,
                         family_declared_capacity: Optional[int],
                         cycle_month: Optional[str] = None,
                         local_date: Optional[str] = None,
                         request_id: str = "") -> WeekOneOutcome:
        """Create, allocate, snapshot and release Week 1. Or return it.

        Authorization is delegated: every service call below gates on active
        child access and on the planner/managing-clinician rule. The transport
        additionally requires PROVIDER role before this is reached.
        """
        capacity = self._require_capacity(family_declared_capacity)
        goals = self.releasable_goals(principal, child_id)
        if not goals:
            raise GoalNotReleasable(
                "this child has no active, anchored, activity-mappable "
                "clinician-approved goal")

        # P0-C. WHICH plan governs today's week is decided FIRST, from the
        # clinician-approved clinical periods and the existing cycles — never
        # from the calendar month — and before anything is created.
        governance = self.governance(principal, child_id,
                                     local_date=local_date)
        if governance.outcome is GovernanceOutcome.RENEWAL_REQUIRED:
            raise ClinicalRenewalRequired(
                "no clinician-approved plan covers this week; clinician "
                "review or renewal is required")

        if governance.outcome is GovernanceOutcome.BOOTSTRAP:
            # First-plan timezone gate. No plan was ever ACTIVATED for this
            # child (a draft does not count). The pre-gate code created,
            # allocated and activated a plan here with a defaulted UTC zone;
            # that path is removed. The clinician creates the plan with an
            # explicit `timezone_of_record` and activates it first.
            raise ClinicalPlanRequired(
                "the child's first clinical plan must be created and "
                "activated by the clinician before Week 1 is released")

        plan = governance.plan
        if (cycle_month is not None
                and validate_cycle_month(cycle_month) != plan.cycle_month):
            # An explicit month can only AGREE with clinical governance.
            # It can never select another plan, and so can never force a
            # starter week inside a week another plan already governs.
            raise GoverningPlanConflict(
                "the requested month is not the governing plan's")
        if plan.is_active:
            self._ensure_allocated(principal, plan, goals,
                                   request_id=request_id)
        cycle = governance.cycle
        if governance.sequence > WEEK_ONE:
            # P0-B1: Week 2 onward never falls through to the non-adaptive
            # creation path below.
            if cycle is not None and cycle.is_released:
                return self._existing_outcome(principal, plan, cycle,
                                              goals)
            return self._release_adaptive(
                principal, plan, list(governance.plan_cycles), goals,
                first_plan_date=governance.first_plan_date,
                sequence=governance.sequence, existing=cycle,
                capacity=capacity, request_id=request_id)
        if cycle is None:
            cycle = self._weekly.create_cycle(
                principal, plan.focus_plan_id,
                sequence_in_month=WEEK_ONE,
                first_plan_date=governance.local_date,
                request_id=request_id)

        if cycle.is_released:
            # REPLAY. The existing released week is returned unchanged, and the
            # capacity on THIS request is not consulted at all — a released
            # cycle's content is frozen, and a later capacity applies to a
            # future cycle rather than to history.
            return self._existing_outcome(principal, plan, cycle, goals)

        # Week 1 resume. A release interrupted after its allocation committed
        # (snapshot not saved) or after its snapshot committed (release not
        # marked) used to be stuck: re-running `_allocate` hit "already
        # allocated" and every retry was refused. Week 1 now resumes exactly
        # the way Week 2+ does (P0-B1), through the SAME two existing steps:
        #
        #   allocation_state       None -> allocate normally; an allocation
        #                          EXACTLY what this request would write ->
        #                          reuse it; anything else (capacity,
        #                          placements, gaps, ledger) -> refused, and
        #                          nothing is written or repaired.
        #   _resume_snapshot_and_release
        #                          reuse a snapshot only if byte-for-byte the
        #                          document this request builds; otherwise
        #                          capture; release if not released; an
        #                          identical concurrent writer is tolerated.
        #
        # The SNAPSHOT is Week 1's clinical freeze / commitment boundary
        # (founder decision). Before it exists no family-facing commitment
        # exists, so a resumed release uses the LATEST active clinician-approved
        # goal version — provided the persisted allocation still EXACTLY
        # matches what this request expects (else refused, nothing written).
        # Once a snapshot exists, any revision that makes the rebuilt document
        # differ is refused by the byte-equivalence check.
        # (Naming debt: the resume refusal is `AdaptiveWeekStateMismatch` for
        # Week 1 too; kept so P0-B1's error contract does not change.)
        templates_by_id = self._candidate_templates(goals)
        candidates = self._candidates_for(goals, templates_by_id)
        if not candidates:
            raise GoalNotReleasable(
                "no reviewed activity content serves this child's goals")
        resumed = self._weekly.allocation_state(
            principal, cycle.cycle_id, candidates,
            family_declared_capacity=capacity)
        if resumed is None:
            placements = self._allocate(principal, cycle, goals,
                                        templates_by_id, capacity=capacity,
                                        request_id=request_id)
        else:
            placements = resumed.placements
            if not placements:
                raise AdaptiveWeekStateMismatch(
                    "an in-progress week has an empty allocation")
        cycle = self._weekly.get_cycle(principal, cycle.cycle_id)
        return self._resume_snapshot_and_release(
            principal, plan, cycle, goals, placements, templates_by_id,
            request_id=request_id)

    # -- steps -------------------------------------------------------------

    @staticmethod
    def _require_capacity(value: Optional[int]) -> int:
        """The family's declared weekly capacity, or a refusal. No default."""
        if value is None:
            raise CapacityRequired(
                "the family's declared weekly activity capacity is required")
        if isinstance(value, bool) or not isinstance(value, int):
            raise CapacityRequired(
                "the declared weekly capacity must be an integer")
        if value < 1:
            # Zero would release an empty week, which is not a plan. Refused
            # rather than treated as "skip this week".
            raise CapacityRequired(
                "the declared weekly capacity must be at least one activity")
        return value

    def _ensure_allocated(self, principal, plan, goals, *,
                          request_id: str) -> None:
        """Allocate each approved goal, skipping any already on the plan.

        `priority_rank` follows the deterministic goal order. NO second goal is
        manufactured: `default_goal_count=2` is the default OFFER, not a
        requirement, and a child with one approved goal gets a one-goal month.

        `emphasis_weight` and `min_coverage_per_cycle` are left unset so the
        plan's own recorded POLICY supplies them — passing explicit values here
        would freeze today's defaults into a plan that is supposed to carry its
        policy version.
        """
        allocated = {
            allocation.goal_ref.goal_id
            for allocation in self._plans.list_allocations(
                principal, plan.focus_plan_id, include_inactive=True)
        }
        rank = len(allocated)
        for row in goals:
            if row.goal_id in allocated:
                continue
            rank += 1
            self._plans.allocate_goal(
                principal, plan.focus_plan_id, row.ref,
                priority_rank=rank, request_id=request_id)

    @staticmethod
    def first_plan_date_for(cycles) -> Optional[str]:
        """The child's first-plan date, READ BACK from cycle 1's own start.

        No new field and no new storage: the starter cycle's `starts_on` IS the
        first-plan date, because that is how `starter_cycle_bounds` built it. So
        the anchor survives restarts and is impossible to drift away from the
        cycle it describes.
        """
        for cycle in cycles:
            if cycle.sequence_in_month == WEEK_ONE:
                return cycle.starts_on
        return None

    def _resolve_cycle(self, principal, plan, *, request_id: str,
                       local_date: Optional[str] = None):
        """The CURRENT cycle of the active plan, created if it does not exist.

        0.6A-2, restoring the frozen Genex starter-week invariant.

        ## FIRST release for this plan

        No cycle exists yet, so TODAY is the first-plan date and the starter
        cycle runs from today to that week's Sunday. A release on Wednesday
        Oct 7 2026 yields Oct 7-11 — never Oct 5-11, which would date activities
        before the family entered the care loop.

        ## SUBSEQUENT releases

        The anchor is read back from cycle 1's own `starts_on`, and the current
        cycle is the starter-anchored one covering today: week 2 onward is Monday
        to Sunday. An ALREADY-CREATED starter cycle is returned unchanged, so a
        replay can never shift it.
        """
        today = local_date or local_date_in(plan.timezone_of_record,
                                           self._now())
        cycles = list(self._weekly.list_cycles(principal, plan.focus_plan_id))
        anchor = self.first_plan_date_for(cycles)

        if anchor is None:
            # The child's first plan. Today IS the anchor.
            return self._weekly.create_cycle(
                principal, plan.focus_plan_id, sequence_in_month=WEEK_ONE,
                first_plan_date=today, request_id=request_id)

        try:
            sequence = starter_sequence_for(anchor, today)
        except WeeklyCycleError as exc:
            # Today precedes the first plan. Refused rather than resolved to the
            # starter week, which would backdate the family's activities.
            raise CurrentCycleUnresolved(
                "today does not fall inside any cycle of this plan") from exc

        for cycle in cycles:
            if cycle.sequence_in_month == sequence:
                return cycle
        # P0-B1. The pre-P0-B code created Week 2+ right here with plain
        # `create_cycle`, i.e. with no adaptation at all. That path is CLOSED:
        # `release_week_one` routes sequence >= 2 to `_release_adaptive` before
        # reaching this method, so arriving here for a later week means a
        # caller bypassed that routing.
        raise NonAdaptiveWeekRefused(
            "week 2 onward may be created only through the adaptive path")

    # -- P0-B1: adaptive Week N+1 ------------------------------------------

    def _release_adaptive(self, principal, plan, cycles, goals, *,
                          first_plan_date: str, sequence: int, existing,
                          capacity: int, request_id: str) -> "WeekOneOutcome":
        """current observations -> P0-B1 policy -> reviewed selection ->
        allocation -> adaptation record + decision -> snapshot -> release.

        Every clinical-boundary check runs BEFORE the first write. The only
        thing this may change relative to Week N is WHICH reviewed activities
        are scheduled; the goal, its version, its anchor and its families are
        verified identical, and the decision record cannot be constructed if
        they are not.
        """
        previous = next((c for c in cycles
                         if c.sequence_in_month == sequence - 1), None)
        if previous is None or not previous.is_released:
            raise AdaptiveWeekUnavailable(
                "the previous week was never released, so there is nothing "
                "to adapt from")
        snapshots = self._repos.weekly_plan_snapshots.list_for_cycle(
            previous.cycle_id)
        if not snapshots:
            raise AdaptiveWeekUnavailable(
                "the previous week has no frozen snapshot")
        previous_doc = snapshots[0].document()

        # ---- the clinical boundary, BEFORE any write ----------------------
        before_versions = {str(g["goal_id"]): str(g.get("goal_version_id", ""))
                           for g in previous_doc.get("goals") or ()}
        now_by_id = {row.goal_id: row for row in goals}
        if set(before_versions) != set(now_by_id):
            raise ClinicalBoundaryRefused(
                "the approved goals differ from the previous week's")
        previous_activities = list(previous_doc.get("activities") or ())
        previous_template_ids = sorted({str(a["activity_template_id"])
                                        for a in previous_activities})

        allowed_by_goal: Dict[str, Tuple[str, ...]] = {}
        families: List[str] = []
        for row in goals:
            allowed = tuple(sorted({b.family_ref
                                    for b in row.anchor.rung.family_bindings}))
            allowed_by_goal[row.goal_id] = allowed
            families.extend(allowed)
            stored = self._repos.clinical_goal_anchors.find(row.goal_id)
            if stored is None:
                raise ClinicalBoundaryRefused("a goal has lost its anchor")
            last_week_families = {str(a.get("activity_family_ref", ""))
                                  for a in previous_activities
                                  if str(a.get("goal_id")) == row.goal_id}
            if not last_week_families <= set(allowed):
                raise ClinicalBoundaryRefused(
                    "last week already left the anchor's families")
            row_stored_rung = stored.rung.rung_ref
            if row_stored_rung != row.anchor.rung.rung_ref:
                raise ClinicalBoundaryRefused("a goal's anchor rung differs")

        require_all_families_served(self._bank, families)
        pool = tuple(self._bank.templates_for_families(sorted(set(families))))

        try:
            policy = decide_adaptation(
                self._weekly.list_alignments(principal, previous.cycle_id),
                self._weekly.list_observations(principal, previous.cycle_id))
        except RepeatedSignalPolicyError as exc:
            raise ClinicalBoundaryRefused(str(exc)) from exc

        starts_on, ends_on, is_partial, partial_reason = starter_cycle_bounds(
            first_plan_date, sequence)
        day_count = (date.fromisoformat(ends_on)
                     - date.fromisoformat(starts_on)).days + 1
        slots = min(capacity, day_count)
        try:
            selection = select_week_templates(
                decisions=policy.decisions,
                previous_template_ids=previous_template_ids,
                pool=pool, slots=slots)
            verify_reviewed_and_bounded(selection.selected_template_ids, pool,
                                        families)
        except AdaptiveSelectionError as exc:
            raise ClinicalBoundaryRefused(str(exc)) from exc

        by_id = {t.activity_template_id: t for t in pool}

        def _proofs(placed_ids) -> Tuple[GoalInvariantProof, ...]:
            proofs = []
            for row in goals:
                allowed = allowed_by_goal[row.goal_id]
                selected_families = sorted({
                    by_id[ref].activity_family_ref for ref in placed_ids
                    if by_id[ref].activity_family_ref in allowed})
                stored = self._repos.clinical_goal_anchors.find(row.goal_id)
                proofs.append(GoalInvariantProof(
                    goal_id=row.goal_id,
                    goal_version_before=before_versions[row.goal_id],
                    goal_version_after=row.version_id,
                    anchor_rung_ref_before=stored.rung.rung_ref,
                    anchor_rung_ref_after=row.anchor.rung.rung_ref,
                    allowed_family_refs=allowed,
                    selected_family_refs=tuple(selected_families)))
            return tuple(proofs)

        try:
            # Proven against the SELECTION first: nothing is written yet.
            _proofs(selection.selected_template_ids)
        except AdaptationDecisionError as exc:
            raise ClinicalBoundaryRefused(str(exc)) from exc

        # ---- writes: each step is skipped if it already EXACTLY exists -----
        #
        # P0-B1 hardening. Every write below is deterministic given the checks
        # above, so an interrupted attempt leaves a prefix of this sequence.
        # A retry verifies that prefix step by step and continues from the
        # first missing step; any persisted state that differs is refused.
        if existing is None:
            cycle = self._weekly.create_cycle(
                principal, plan.focus_plan_id, sequence_in_month=sequence,
                first_plan_date=first_plan_date,
                predecessor_cycle_id=previous.cycle_id,
                generation_reason=GenerationReason.SEQUENTIAL,
                request_id=request_id)
        else:
            cycle = existing
            self._verify_draft(cycle, plan, sequence=sequence,
                               predecessor_cycle_id=previous.cycle_id,
                               starts_on=starts_on, ends_on=ends_on,
                               is_partial=is_partial,
                               partial_reason=partial_reason)
            # The recorded-capacity rule is enforced in ONE place,
            # `WeeklyService.allocation_state`, below.

        chosen = {ref: by_id[ref] for ref in selection.selected_template_ids}
        candidates = self._candidates_for(goals, chosen)
        try:
            resumed = self._weekly.allocation_state(
                principal, cycle.cycle_id, candidates,
                family_declared_capacity=capacity)
        except InProgressCapacityMismatch as exc:
            raise AdaptiveWeekCapacityMismatch(str(exc)) from exc
        except InProgressStateMismatch as exc:
            raise AdaptiveWeekStateMismatch(str(exc)) from exc
        if resumed is None:
            placements = self._allocate(principal, cycle, goals, chosen,
                                        capacity=capacity,
                                        request_id=request_id)
        else:
            placements = resumed.placements
            if not placements:
                raise AdaptiveWeekStateMismatch(
                    "an in-progress week has an empty allocation")
        placed = sorted({p.activity_identity_ref for p in placements})
        if not set(placed) <= set(selection.selected_template_ids):
            raise ClinicalBoundaryRefused(  # pragma: no cover - allocator bound
                "the allocator placed an activity outside the selection")
        try:
            self._weekly.record_week_adaptation(
                principal, previous.cycle_id, cycle.cycle_id,
                policy_outcome=policy,
                selected_template_ids=placed,
                repeated_template_ids=sorted(set(placed)
                                             & set(previous_template_ids)),
                excluded_themes=selection.excluded_themes,
                goal_proofs=_proofs(placed),
                request_id=request_id)
        except InProgressStateMismatch as exc:
            raise AdaptiveWeekStateMismatch(str(exc)) from exc
        except AdaptationDecisionError as exc:
            # Rendered as the route's constant refusal, never a 500. The week
            # stays unreleased, so the family sees nothing.
            raise ClinicalBoundaryRefused(str(exc)) from exc
        cycle = self._weekly.get_cycle(principal, cycle.cycle_id)
        return self._resume_snapshot_and_release(
            principal, plan, cycle, goals, placements, chosen,
            request_id=request_id)

    @staticmethod
    def _verify_draft(cycle, plan, *, sequence: int, predecessor_cycle_id: str,
                      starts_on: str, ends_on: str, is_partial: bool,
                      partial_reason) -> None:
        """An existing unreleased Week N+1 must be EXACTLY the cycle this
        request would have created. Anything else is not resumed."""
        expected = {
            "child": (cycle.child_id, plan.child_id),
            "focus plan": (cycle.owning_focus_plan_id, plan.focus_plan_id),
            "sequence": (cycle.sequence_in_month, sequence),
            "predecessor": (cycle.predecessor_cycle_id, predecessor_cycle_id),
            "start date": (cycle.starts_on, starts_on),
            "end date": (cycle.ends_on, ends_on),
            "partial flag": (cycle.is_partial, is_partial),
            "partial reason": (cycle.partial_reason, partial_reason),
            "generation reason": (cycle.generation_reason,
                                  GenerationReason.SEQUENTIAL),
        }
        for name, (have, want) in expected.items():
            if have != want:
                raise AdaptiveWeekStateMismatch(
                    f"the in-progress week differs from the expected one: {name}")
        if cycle.is_released:  # pragma: no cover - released is replayed earlier
            raise AdaptiveWeekStateMismatch("the week is already released")

    def _resume_snapshot_and_release(self, principal, plan, cycle, goals,
                                     placements, templates_by_id, *,
                                     request_id: str) -> "WeekOneOutcome":
        """Snapshot (or verify the existing one), then release. P0-B1.

        A snapshot left by an interrupted attempt is reused ONLY if it is
        byte-for-byte the document this request builds; any other snapshot
        refuses, because a released week must show exactly what was decided.
        """
        goal_text = {row.goal_id: row.text for row in goals}
        goal_versions = {row.goal_id: row.version_id for row in goals}
        document = build_document(
            cycle=cycle, placements=placements,
            templates_by_id=templates_by_id,
            goal_text_by_id=goal_text,
            goal_version_by_id=goal_versions)

        existing = self._repos.weekly_plan_snapshots.list_for_cycle(
            cycle.cycle_id)
        if len(existing) > 1:
            raise AdaptiveWeekStateMismatch(
                "the in-progress week has more than one snapshot")
        if existing:
            snapshot = existing[0]
            same = (snapshot.source_system == SNAPSHOT_SOURCE_SYSTEM
                    and snapshot.source_plan_id == cycle.cycle_id
                    and _canonical(snapshot.document()) == _canonical(document))
            if not same:
                raise AdaptiveWeekStateMismatch(
                    "the in-progress week's snapshot differs from the "
                    "expected document")
        else:
            try:
                snapshot = self._weekly.capture_snapshot(
                    principal, cycle.cycle_id, SNAPSHOT_SOURCE_SYSTEM,
                    cycle.cycle_id, document, request_id=request_id)
            except WeeklyConflict:
                # An identical concurrent request froze it first. Accepted
                # only if it is exactly the document this request built.
                raced = self._repos.weekly_plan_snapshots.list_for_cycle(
                    cycle.cycle_id)
                if not (len(raced) == 1 and _canonical(raced[0].document())
                        == _canonical(document)):
                    raise AdaptiveWeekStateMismatch(
                        "a concurrent snapshot differs from the expected "
                        "document") from None
                snapshot = raced[0]
        try:
            released = self._weekly.release_cycle(
                principal, cycle.cycle_id, request_id=request_id)
        except ReleasedPlanImmutable:
            # An identical concurrent request released this same, verified
            # week first. Return it unchanged rather than release it twice.
            return self._existing_outcome(
                principal, plan,
                self._weekly.get_cycle(principal, cycle.cycle_id), goals)

        return WeekOneOutcome(
            child_id=cycle.child_id,
            focus_plan_id=plan.focus_plan_id,
            cycle_id=cycle.cycle_id,
            goal_ids=tuple(row.goal_id for row in goals),
            activity_count=len(placements),
            created=True,
            released_at=released.released_to_parent_at,
            snapshot_id=snapshot.snapshot_id,
            parent_week=parent_week_view(
                snapshot.document(),
                released_at=released.released_to_parent_at))

    def _candidate_templates(self, goals) -> Dict[str, Any]:
        """Reviewed templates for every family the goals' anchors bind.

        `require_all_families_served` runs FIRST, so a goal binding a family
        with no reviewed content refuses before any week is written rather than
        producing a thin week nobody notices.
        """
        families: List[str] = []
        for row in goals:
            families.extend(
                binding.family_ref
                for binding in row.anchor.rung.family_bindings)
        require_all_families_served(self._bank, families)
        return {template.activity_template_id: template
                for template in self._bank.templates_for_families(families)}

    def _allocate(self, principal, cycle, goals, templates_by_id, *,
                  capacity: int, request_id: str):
        """Build candidates per goal and run the EXISTING allocator.

        One `CandidateActivity` per reviewed template per goal, built by
        `candidates_for_goal` — which orders by the template's content digest,
        so the selection is a function of the authored content and of capacity,
        never of dict iteration order.
        """
        candidates = self._candidates_for(goals, templates_by_id)
        if not candidates:
            raise GoalNotReleasable(
                "no reviewed activity content serves this child's goals")

        result = self._weekly.allocate_cycle(
            principal, cycle.cycle_id, candidates,
            family_declared_capacity=capacity, request_id=request_id)
        if not result.placements:
            # Capacity was positive and candidates existed, so an empty result
            # means the allocator refused every placement. Releasing an empty
            # week would tell a family there is nothing to do this week.
            raise GoalNotReleasable(
                "the allocator placed no activity for this cycle")
        return result.placements

    @staticmethod
    def _candidates_for(goals, templates_by_id) -> List[Any]:
        """One candidate per reviewed template per goal it serves."""
        candidates: List[Any] = []
        for row in goals:
            bound = {binding.family_ref
                     for binding in row.anchor.rung.family_bindings}
            templates = [template for template in templates_by_id.values()
                         if template.activity_family_ref in bound]
            candidates.extend(candidates_for_goal(templates, row.ref))
        return candidates

    def _existing_outcome(self, principal, plan, cycle, goals) -> WeekOneOutcome:
        """The already-released week, rebuilt from its FROZEN snapshot."""
        snapshots = self._repos.weekly_plan_snapshots.list_for_cycle(
            cycle.cycle_id)
        if not snapshots:  # pragma: no cover - release requires a snapshot
            raise WeekOneReleaseError(
                "a released cycle has no plan snapshot")
        snapshot = snapshots[0]
        document = snapshot.document()
        return WeekOneOutcome(
            child_id=cycle.child_id,
            focus_plan_id=plan.focus_plan_id,
            cycle_id=cycle.cycle_id,
            goal_ids=tuple(row.goal_id for row in goals),
            activity_count=len(document.get("activities") or ()),
            created=False,
            released_at=cycle.released_to_parent_at,
            snapshot_id=snapshot.snapshot_id,
            parent_week=parent_week_view(
                document, released_at=cycle.released_to_parent_at))

    # -- the Parent read ---------------------------------------------------

    def governance(self, principal, child_id: str, *,
                   local_date: Optional[str] = None) -> Governance:
        """P0-C. Which clinician-approved plan governs today's week.

        The ONE resolution shared by the caregiver `this_week` read, the
        provider `current-cycle` read and the provider release, so the three
        can never disagree about which week is current. Authorized reads only:
        `list_plans` and `list_cycles` both gate on child access.
        """
        plans = list(self._plans.list_plans(principal, child_id))
        cycles_by_plan = {
            plan.focus_plan_id: tuple(self._weekly.list_cycles(
                principal, plan.focus_plan_id))
            for plan in plans}
        try:
            return resolve_governance(plans=plans,
                                      cycles_by_plan=cycles_by_plan,
                                      now=self._now(), local_date=local_date)
        except GoverningPlanAmbiguous as exc:
            raise GoverningPlanConflict(str(exc)) from exc

    # -- the Parent read ---------------------------------------------------

    def this_week(self, principal, child_id: str, *,
                  local_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The released week covering the family's local date, or None.

        Returns None — not a draft — when nothing has been released. A draft
        cycle exists as soon as a provider starts the operation, and showing it
        would put a partially allocated week in front of a family.

        P0-C: the week is the cycle covering today in its OWNING plan's zone of
        record, whatever calendar month today is in — so a week that crosses a
        month boundary is shown on every one of its days.

        Derived from the SNAPSHOT, so a later goal revision cannot change what a
        released week says.
        """
        governance = self.governance(principal, child_id,
                                     local_date=local_date)
        cycle = governance.cycle
        if (governance.outcome is not GovernanceOutcome.COVERED
                or not cycle.is_released):
            return None
        snapshots = self._repos.weekly_plan_snapshots.list_for_cycle(
            cycle.cycle_id)
        if not snapshots:  # pragma: no cover - release requires one
            return None
        return parent_week_view(snapshots[0].document(),
                                released_at=cycle.released_to_parent_at)


__all__ = [
    "AdaptiveWeekCapacityMismatch",
    "ClinicalPlanRequired",
    "ClinicalRenewalRequired",
    "GoverningPlanConflict",
    "AdaptiveWeekStateMismatch",
    "AdaptiveWeekUnavailable",
    "CapacityRequired",
    "ClinicalBoundaryRefused",
    "NonAdaptiveWeekRefused",
    "CurrentCycleUnresolved",
    "ReleasableGoal",
    "GoalNotReleasable",
    "SNAPSHOT_SOURCE_SYSTEM",
    "WEEK_ONE",
    "WeekOneOutcome",
    "WeekOneReleaseError",
    "WeekOneReleaseService",
]
