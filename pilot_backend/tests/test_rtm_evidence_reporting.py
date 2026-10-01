"""0.4F/G — RTM evidence, month-end reporting and CPT coding assistance.

Same harness as every prior slice: `FirestoreRepositories` over
`FakeDocumentStore`, so these exercise the production repository code rather
than a parallel in-memory implementation that could drift. No concurrency
claim is made here — the fake is a plain dict and not thread-safe; racing
writers are proven in `pilot_runtime/tests/integration/`.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import fields as dataclass_fields
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pilot_backend.audit.events import ALLOWED_METADATA_KEYS, AuditAction
from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth import VerifiedToken, resolve_principal
from pilot_backend.coding.rules import (
    CODING_RULE_SET_ID,
    CODING_RULE_VERSION,
    SUPPORTED_CODES,
    CodeCandidate,
    CodingInputs,
    CodingRuleError,
    ConfirmationStatus,
    MissingRequirement,
    evaluate,
    known_rule_sets,
    rule_set_for,
)
from pilot_backend.domain.enums import ConnectionStatus
from pilot_backend.domain.goals import EditType, EvidenceSource, GoalError, GoalKind, GoalRef
from pilot_backend.domain.month_end import (
    MonthEndReport,
    ReportError,
    ReportSection,
    ReportState,
    SectionContent,
)
from pilot_backend.domain.observation import AttemptOutcome, Difficulty
from pilot_backend.domain.roles import ActorRole
from pilot_backend.domain.rtm import (
    EpisodeStatus,
    PeriodStatus,
    RegulatoryStatus,
    RTMEpisode,
    RTMError,
    RTMMonitoringPeriod,
    RTMTechnology,
)
from pilot_backend.domain.rtm_documentation import (
    ClinicalAction,
    ClinicalActionType,
    DocumentationError,
    InteractionModality,
    ParticipantType,
    SynchronousInteraction,
    TherapistReview,
    TimeEntry,
    TimeEntryMethod,
    documented_minutes,
)
from pilot_backend.domain.rtm_summary import (
    CodingAssistanceSummary,
    GoalStatusRecommendation,
    RTMEvidenceSummary,
    SummaryError,
)
from pilot_backend.fixtures.secure_topology import (
    CAREGIVER_ALPHA_SUBJECT,
    CAREGIVER_BETA_SUBJECT,
    PROVIDER_ALPHA_SUBJECT,
    PROVIDER_BETA_SUBJECT,
    PROVIDER_GAMMA_SUBJECT,
    build_secure_topology,
)
from pilot_backend.goals.service import GoalService
from pilot_backend.goals.suggestion_engine import ObservationSnapshot, ObservedDomain
from pilot_backend.identity import LongitudinalIdentityService
from pilot_backend.persistence import FakeDocumentStore, FirestoreRepositories, encode
from pilot_backend.persistence.codecs import decode
from pilot_backend.persistence.collections import COLLECTIONS, PILOT_COLLECTION_PREFIX
from pilot_backend.planning.service import MonthlyPlanService
from pilot_backend.rtm.errors import (
    EpisodeTransferRefused,
    FinalizedRecordImmutable,
    RTMAuthorizationError,
    RTMConflict,
    RTMValidationError,
)
from pilot_backend.rtm.service import RTMService
from pilot_backend.weekly.allocator import CandidateActivity
from pilot_backend.weekly.service import WeeklyService

from .test_secure_foundation import ALL_SENTINELS, SENTINEL_CONCERN, SENTINEL_NOTE

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
PILOT_ROOT = Path(__file__).resolve().parents[1]
CYCLE = "2026-10"
ZONE = "America/New_York"


class _AdvancingClock:
    def __init__(self, start): self._t, self._n = start, 0

    def __call__(self):
        self._n += 1
        return self._t.replace(microsecond=0) + timedelta(seconds=self._n)


class Stack:
    def __init__(self):
        self.store = FakeDocumentStore()
        self.repos = FirestoreRepositories(self.store)
        self.topo = build_secure_topology(self.repos, now=T0)
        self.recorder = AuditRecorder(self.repos.audit_events, environment="test")
        self.clock = _AdvancingClock(T0)
        kw = dict(repos=self.repos, recorder=self.recorder, now=self.clock)
        self.identity = LongitudinalIdentityService(**kw)
        self.goals = GoalService(**kw)
        self.plans = MonthlyPlanService(**kw)
        self.weekly = WeeklyService(**kw)
        self.rtm = RTMService(**kw)

    def principal(self, subject):
        return resolve_principal(VerifiedToken(subject=subject), self.repos)

    @property
    def caregiver_alpha(self): return self.principal(CAREGIVER_ALPHA_SUBJECT)
    @property
    def caregiver_beta(self): return self.principal(CAREGIVER_BETA_SUBJECT)
    @property
    def provider_alpha(self): return self.principal(PROVIDER_ALPHA_SUBJECT)
    @property
    def provider_beta(self): return self.principal(PROVIDER_BETA_SUBJECT)
    @property
    def child(self): return self.topo.child_alpha.child_id

    def connected_provider_gamma(self):
        """Provider-Gamma, ACTIVE on Child-Alpha but never the owner."""
        self.repos.provider_child.activate(
            self.topo.link_gamma_provider_pending.connection_id, now=T0)
        return self.principal(PROVIDER_GAMMA_SUBJECT)


@pytest.fixture()
def s():
    return Stack()


# ---------------------------------------------------------------------------
# scenario builders — neutral fictional aliases only
# ---------------------------------------------------------------------------

def _snapshot(child_id):
    return ObservationSnapshot(child_id, CYCLE, (
        ObservedDomain("talking_and_communicating", True,
                       EvidenceSource.CLINICIAN_OBSERVATION,
                       milestone_refs=("mv1:cdc:comm:24m:two-word",),
                       functional_baseline_area="requesting"),
        ObservedDomain("social_and_emotional", True,
                       EvidenceSource.CAREGIVER_REPORTED_MILESTONE,
                       milestone_refs=("mv1:cdc:social:24m:turn-taking",)),
    ))


def _planning(s, *, release=True):
    """Identity, goals, active month, cycle 1 allocated. Returns context."""
    s.identity.assign_managing_clinician(
        s.provider_alpha, s.child, s.topo.provider_alpha.provider_id)
    offered = s.goals.generate_suggestions(s.provider_alpha, s.child,
                                           _snapshot(s.child))
    goals = [s.goals.approve_clinical_goal(
        s.provider_alpha, s.child, edit_type=EditType.ACCEPTED_VERBATIM,
        suggestion_id=x.suggestion_id) for x in offered]
    plan = s.plans.create_plan(s.provider_alpha, s.child, CYCLE, ZONE)
    for rank, goal in enumerate(goals, start=1):
        s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id, goal.ref,
                              priority_rank=rank)
    s.plans.activate_plan(s.provider_alpha, plan.focus_plan_id)

    cycle = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                  sequence_in_month=1)
    g1, g2 = goals[0].ref, goals[1].ref
    candidates = (
        CandidateActivity("activity-a", (g1,), primary_for=g1),
        CandidateActivity("activity-b", (g2,), primary_for=g2),
        # C supports BOTH goals — one opportunity, two attributions.
        CandidateActivity("activity-c", (g1, g2), primary_for=g1),
    )
    result = s.weekly.allocate_cycle(s.provider_alpha, cycle.cycle_id,
                                     candidates, family_declared_capacity=3)
    if release:
        s.weekly.capture_snapshot(
            s.provider_alpha, cycle.cycle_id,
            __import__("pilot_backend.domain.source_link", fromlist=["x"]
                       ).SourceSystem.PARENT,
            "fictional-parent-plan-001", {"week": 1, "items": []})
        s.weekly.release_cycle(s.provider_alpha, cycle.cycle_id)
    return {"plan": plan, "goals": goals, "cycle": cycle, "result": result,
            "candidates": candidates}


def _instances(result):
    return {p.activity_identity_ref: p.activity_instance_ref
            for p in result.placements}


def _rtm_open(s, ctx):
    episode = s.rtm.open_episode(s.provider_alpha, s.child,
                                 [g.ref for g in ctx["goals"]])
    s.rtm.declare_technology(s.provider_alpha, episode.episode_id,
                             "Genex RTM pilot")
    period = s.rtm.open_period(s.provider_alpha, episode.episode_id,
                               ctx["plan"].focus_plan_id)
    return episode, period


# ===========================================================================
# the coding rule set — §16-21
# ===========================================================================

@pytest.mark.parametrize("minutes,expected", [
    (0, []), (1, []), (9, []),
    (10, [("98979", 1)]), (13, [("98979", 1)]), (19, [("98979", 1)]),
    (20, [("98980", 1)]), (21, [("98980", 1)]), (39, [("98980", 1)]),
    (40, [("98980", 1), ("98981", 1)]),
    (42, [("98980", 1), ("98981", 1)]),
    (59, [("98980", 1), ("98981", 1)]),
    (60, [("98980", 1), ("98981", 2)]),
    (79, [("98980", 1), ("98981", 2)]),
    (80, [("98980", 1), ("98981", 3)]),
])
def test_the_2026_time_pattern_table(minutes, expected):
    """§21 verbatim. Every boundary, with documented real-time contact."""
    outcome = evaluate(CodingInputs(minutes, True))
    assert [(c.code, c.units) for c in outcome.candidates] == expected


@pytest.mark.parametrize("minutes", [10, 13, 19, 20, 39, 40, 42, 59, 60, 79])
def test_no_candidate_without_real_time_interactive_communication(minutes):
    """The same times with no documented real-time contact. No candidate."""
    outcome = evaluate(CodingInputs(minutes, False))
    assert outcome.candidates == ()
    assert (MissingRequirement.NO_REAL_TIME_INTERACTIVE_COMMUNICATION
            in outcome.missing_requirements)


def test_98979_is_never_suggested_with_98980_or_98981():
    for minutes in range(0, 200):
        codes = {c.code for c in evaluate(CodingInputs(minutes, True)).candidates}
        assert not ("98979" in codes and (codes & {"98980", "98981"})), minutes


def test_98981_is_never_suggested_without_98980():
    for minutes in range(0, 200):
        codes = {c.code for c in evaluate(CodingInputs(minutes, True)).candidates}
        assert not ("98981" in codes and "98980" not in codes), minutes


def test_incomplete_additional_increments_never_round_up():
    """39 is not 40; 59 is not 60. Integer division, no rounding step."""
    assert evaluate(CodingInputs(39, True)).units_for("98981") == 0
    assert evaluate(CodingInputs(59, True)).units_for("98981") == 1
    assert evaluate(CodingInputs(79, True)).units_for("98981") == 2


def test_only_treatment_management_codes_are_supported():
    """Device-supply codes have no entry and cannot be produced."""
    assert SUPPORTED_CODES == ("98979", "98980", "98981")
    with pytest.raises(CodingRuleError):
        CodeCandidate("98975", 1)
    for minutes in range(0, 200):
        for candidate in evaluate(CodingInputs(minutes, True)).candidates:
            assert candidate.code in SUPPORTED_CODES


def test_technology_status_is_flagged_even_when_the_pattern_matches():
    """§19. A time-pattern match is NOT an eligibility conclusion."""
    outcome = evaluate(CodingInputs(42, True))
    assert outcome.has_candidates
    assert (MissingRequirement.TECHNOLOGY_STATUS_UNRESOLVED
            in outcome.missing_requirements)
    assert (MissingRequirement.CLINICIAN_CONFIRMATION_REQUIRED
            in outcome.missing_requirements)


def test_clinician_confirmation_is_always_required():
    for minutes in (0, 10, 42, 79):
        for interaction in (True, False):
            outcome = evaluate(CodingInputs(minutes, interaction))
            assert (MissingRequirement.CLINICIAN_CONFIRMATION_REQUIRED
                    in outcome.missing_requirements)


def test_the_rules_never_use_billing_language():
    """No output may claim billability, eligibility or reimbursement."""
    banned = ("billable", "reimburs", "claim", "eligible for", "approved code",
              "claim-ready", "payer", "guarantee")
    for minutes in (0, 9, 10, 19, 20, 42, 79):
        for interaction in (True, False):
            outcome = evaluate(CodingInputs(minutes, interaction))
            blob = " ".join(list(outcome.rule_explanations)
                            + [c.rationale for c in outcome.candidates]).lower()
            for word in banned:
                assert word not in blob, (minutes, interaction, word)


def test_the_rule_set_is_versioned_and_resolvable():
    assert known_rule_sets() == ((CODING_RULE_SET_ID, CODING_RULE_VERSION),)
    assert rule_set_for(CODING_RULE_SET_ID, CODING_RULE_VERSION) is evaluate
    with pytest.raises(CodingRuleError):
        rule_set_for(CODING_RULE_SET_ID, "v99")


def test_the_coding_package_imports_nothing_from_the_app():
    """§15: isolated from clinical/product logic, with no I/O at all."""
    tree = ast.parse((PILOT_ROOT / "coding/rules.py").read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
    banned = {"requests", "httpx", "urllib", "http", "socket", "openai",
              "anthropic", "google", "vertexai", "firebase_admin", "grpc"}
    assert not (modules & banned), modules & banned
    # No service, repository or store reaches the rules.
    assert not any(node.level for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom) and node.level)


def test_negative_minutes_are_refused():
    with pytest.raises(CodingRuleError):
        CodingInputs(-1, True)


# ===========================================================================
# mutation-sweep follow-ups
#
# Seven mutations survived the first sweep. Five were REDUNDANT DEFENCES —
# the weakened guard was backed by a second one, so behaviour held and no
# test failed. Redundancy is deliberate, but an untested guard is still an
# untested guard: these exercise each one directly. Two were real gaps.
# ===========================================================================

def test_the_coding_invariant_check_refuses_a_contradictory_set():
    """Mutation 6/7. The branches cannot emit these; the guard still must.

    `_validate_invariants` is deliberately redundant with the branch
    structure — branches are easy to edit and hard to review. Calling it
    directly is what makes the redundancy a tested guarantee rather than
    dead code.
    """
    from pilot_backend.coding.rules import _validate_invariants

    with pytest.raises(CodingRuleError):
        _validate_invariants((CodeCandidate("98979", 1),
                              CodeCandidate("98980", 1)))
    with pytest.raises(CodingRuleError):
        _validate_invariants((CodeCandidate("98981", 1),))
    # The legitimate pairing is accepted.
    _validate_invariants((CodeCandidate("98980", 1), CodeCandidate("98981", 2)))


def test_a_caregiver_is_refused_before_any_clinician_lookup(s):
    """Mutation 14. The ROLE check must fire on its own.

    With no managing clinician assigned, the ownership check would raise
    RTMConflict. A caregiver must still get RTMAuthorizationError, which is
    only true if the role check runs FIRST and independently.
    """
    plan = s.plans.create_plan(s.caregiver_alpha, s.child, CYCLE, ZONE)
    assert s.repos.managing_clinicians.list_for_child(s.child) == []
    with pytest.raises(RTMAuthorizationError):
        s.rtm.open_episode(s.caregiver_alpha, s.child,
                           [GoalRef(GoalKind.CLINICAL, "clgl_absent")])


def test_the_episode_record_itself_refuses_a_caregiver_goal():
    """Mutation 16. The dataclass guard, independent of the service gate."""
    with pytest.raises(RTMError):
        RTMEpisode(
            episode_id="repi_1", child_id="chld_x",
            managing_provider_id="prov_1", practice_id="prac_1",
            clinical_goal_refs=((GoalKind.CAREGIVER_APPROVED.value, "cagl_1"),),
            opened_at=T0, opened_by_actor_id="prov_1")


def test_an_unaffirmed_contact_does_not_count_as_real_time():
    """Mutation 30. The reachable half of the real-time check.

    Every `ParticipantType` member is a family member, so the participant
    half of `counts_as_real_time_communication` is currently unreachable and
    exists to hold if the enum ever grows. `real_time_affirmed` is the half
    that can fail today, and it must.
    """
    interaction = SynchronousInteraction.record(
        "rper_1", "chld_x", "prov_1", occurred_at_utc=T0,
        local_date="2026-10-05", timezone_of_record=ZONE,
        modality=InteractionModality.PHONE,
        participant_type=ParticipantType.CAREGIVER,
        real_time_affirmed=False)
    assert not interaction.counts_as_real_time_communication
    assert {m.name for m in ParticipantType} == {"PATIENT", "CAREGIVER", "BOTH"}


def test_the_report_record_itself_refuses_an_unexplained_amendment():
    """Mutation 36. `MonthEndReport.amend`'s own reason check."""
    report = MonthEndReport.create("rper_1", "chld_x", "mfpl_1", CYCLE)
    finalized = report.finalize(actor_id="prov_1")
    with pytest.raises(ReportError):
        finalized.amend(actor_id="prov_1", reason="   ")
    with pytest.raises(ReportError):
        finalized.amend(actor_id="  ", reason="a real reason")


def test_evidence_excludes_events_attributed_to_another_month(s):
    """Mutation 42 — a REAL gap. Attribution is by LOCAL month, not cycle.

    A cycle spanning Oct 26 to Nov 1 owns its Nov 1 attempt, but that attempt
    belongs to November. October's summary must not count it, or a two-month
    report counts the same attempt twice.
    """
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    instances = _instances(ctx["result"])

    # Cycle 4 of October runs 2026-10-19..25; cycle 5 runs 26 Oct - 1 Nov.
    for sequence in (2, 3, 4, 5):
        s.weekly.create_cycle(s.provider_alpha, ctx["plan"].focus_plan_id,
                              sequence_in_month=sequence)
    spanning = [c for c in s.weekly.list_cycles(
        s.provider_alpha, ctx["plan"].focus_plan_id)
        if c.spans_month_boundary]
    assert spanning, "no cycle crosses the month boundary in this fixture"
    crossing = spanning[0]
    result = s.weekly.allocate_cycle(
        s.provider_alpha, crossing.cycle_id, ctx["candidates"],
        family_declared_capacity=2)
    crossing_ref = result.placements[0].activity_instance_ref

    s.weekly.record_observation(
        s.caregiver_alpha, ctx["cycle"].cycle_id, instances["activity-a"],
        local_date="2026-10-02", attempt_outcome=AttemptOutcome.DID_IT)
    november = s.weekly.record_observation(
        s.caregiver_alpha, crossing.cycle_id, crossing_ref,
        local_date="2026-11-01", attempt_outcome=AttemptOutcome.DID_IT)
    assert november.attribution_month == "2026-11"

    summary = s.rtm.generate_evidence_summary(s.provider_alpha,
                                              period.period_id)
    assert summary.total_distinct_observation_events == 1, \
        "a November attempt was counted in October"
    assert summary.distinct_observed_local_dates == 1


# ===========================================================================
# episode — §2, §3
# ===========================================================================

def test_an_episode_requires_a_clinician_approved_goal(s):
    ctx = _planning(s)
    caregiver_goal = s.goals.approve_caregiver_goal(
        s.caregiver_alpha, s.child, edit_type=EditType.AUTHORED_FRESH,
        text="Family-chosen focus.", reason="family priority")
    with pytest.raises(GoalError):
        s.rtm.open_episode(s.provider_alpha, s.child, [caregiver_goal.ref])
    with pytest.raises(RTMValidationError):
        s.rtm.open_episode(s.provider_alpha, s.child, [])


def test_an_episode_requires_goals_belonging_to_this_child(s):
    ctx = _planning(s)
    s.identity.assign_managing_clinician(
        s.caregiver_beta if False else s.provider_beta,
        s.topo.child_beta.child_id, s.topo.provider_beta.provider_id)
    other = s.goals.approve_clinical_goal(
        s.provider_beta, s.topo.child_beta.child_id,
        edit_type=EditType.AUTHORED_FRESH, text="Other child.", reason="r")
    with pytest.raises(RTMValidationError):
        s.rtm.open_episode(s.provider_alpha, s.child, [other.ref])


def test_a_month_ending_does_not_close_the_episode(s):
    """§2. Only explicit clinician action closes an episode."""
    ctx = _planning(s)
    episode, period = _rtm_open(s, ctx)
    s.rtm.finalize_period(s.provider_alpha, period.period_id)

    assert s.rtm.get_period(s.provider_alpha, period.period_id).is_finalized
    assert s.rtm.get_episode(s.provider_alpha, episode.episode_id).is_open, \
        "finalizing a month closed the episode"


def test_closing_an_episode_requires_a_reason(s):
    ctx = _planning(s)
    episode, _ = _rtm_open(s, ctx)
    assert "reason" in inspect.signature(s.rtm.close_episode).parameters
    assert (inspect.signature(s.rtm.close_episode).parameters["reason"].default
            is inspect.Parameter.empty)
    with pytest.raises(RTMError):
        s.rtm.close_episode(s.provider_alpha, episode.episode_id, reason="  ")
    closed = s.rtm.close_episode(s.provider_alpha, episode.episode_id,
                                 reason="course of treatment complete")
    assert not closed.is_open and closed.close_reason


def test_an_episode_is_inactive_on_status_even_without_a_timestamp():
    """The recurring mutation lesson, applied to episodes."""
    episode = RTMEpisode.open("chld_x", "prov_1", "prac_1",
                              (GoalRef(GoalKind.CLINICAL, "clgl_1"),),
                              actor_id="prov_1")
    assert episode.is_open
    assert not replace(episode, status=EpisodeStatus.CLOSED,
                       closed_at=None).is_open


def test_an_open_episode_does_not_silently_transfer(s):
    """§3. A changed managing clinician cannot continue someone else's episode."""
    ctx = _planning(s)
    episode, period = _rtm_open(s, ctx)

    # Transfer clinical ownership to Provider-Gamma.
    gamma = s.connected_provider_gamma()
    s.identity.transfer_managing_clinician(
        s.provider_alpha, s.child, s.topo.provider_gamma.provider_id,
        reason="caseload change")

    for call in (
        lambda: s.rtm.record_time(gamma, period.period_id,
                                  local_date="2026-10-05", minutes=10,
                                  activity_description="d"),
        lambda: s.rtm.finalize_period(gamma, period.period_id),
        lambda: s.rtm.generate_evidence_summary(gamma, period.period_id),
        lambda: s.rtm.open_period(gamma, episode.episode_id,
                                  ctx["plan"].focus_plan_id),
    ):
        with pytest.raises(EpisodeTransferRefused):
            call()

    # The sanctioned route out: close it explicitly.
    closed = s.rtm.close_episode(gamma, episode.episode_id,
                                 reason="transferred; opening a new episode")
    assert not closed.is_open


def test_only_one_open_episode_per_child(s):
    ctx = _planning(s)
    episode, _ = _rtm_open(s, ctx)
    with pytest.raises(RTMConflict):
        s.rtm.open_episode(s.provider_alpha, s.child,
                           [g.ref for g in ctx["goals"]])


# ===========================================================================
# monitoring period — §4, §26
# ===========================================================================

def test_the_period_takes_its_month_and_timezone_from_the_focus_plan(s):
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    assert period.cycle_month == CYCLE
    assert period.timezone_of_record == ZONE
    assert period.status is PeriodStatus.ACTIVE


def test_a_period_cannot_reference_another_childs_focus_plan(s):
    ctx = _planning(s)
    episode, _ = _rtm_open(s, ctx)
    s.identity.assign_managing_clinician(
        s.provider_beta, s.topo.child_beta.child_id,
        s.topo.provider_beta.provider_id)
    other_plan = s.plans.create_plan(s.provider_beta,
                                     s.topo.child_beta.child_id, CYCLE, ZONE)
    with pytest.raises(RTMValidationError):
        s.rtm.open_period(s.provider_alpha, episode.episode_id,
                          other_plan.focus_plan_id)


def test_one_period_per_episode_month(s):
    ctx = _planning(s)
    episode, _ = _rtm_open(s, ctx)
    with pytest.raises(RTMConflict):
        s.rtm.open_period(s.provider_alpha, episode.episode_id,
                          ctx["plan"].focus_plan_id)


def test_a_period_requires_a_valid_timezone():
    from pilot_backend.domain.monthly_plan import TimezoneError

    with pytest.raises(TimezoneError):
        RTMMonitoringPeriod.create("repi_1", "chld_x", "mfpl_1", CYCLE, "")
    with pytest.raises(TimezoneError):
        RTMMonitoringPeriod.create("repi_1", "chld_x", "mfpl_1", CYCLE,
                                   "Mars/Olympus")


def test_a_finalized_period_accepts_no_further_evidence(s):
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    s.rtm.finalize_period(s.provider_alpha, period.period_id)
    with pytest.raises(FinalizedRecordImmutable):
        s.rtm.record_time(s.provider_alpha, period.period_id,
                          local_date="2026-10-06", minutes=10,
                          activity_description="late")


# ===========================================================================
# time — §8, §24
# ===========================================================================

def test_time_is_manual_and_there_is_no_other_method():
    assert [m.name for m in TimeEntryMethod] == ["MANUAL"]
    assert not any(n in {m.name for m in TimeEntryMethod}
                   for n in ("AUTOMATIC", "TIMER", "INFERRED", "ESTIMATED"))


def test_no_module_infers_time_from_activity():
    """No timer, no page-view, no click-time, no estimate."""
    banned = {"page_view", "pageview", "click_time", "session_duration",
              "auto_timer", "start_timer", "stop_timer", "estimated_minutes",
              "inferred_minutes"}
    for relative in ("domain/rtm_documentation.py", "rtm/service.py",
                     "rtm/evidence.py", "coding/rules.py"):
        tree = ast.parse((PILOT_ROOT / relative).read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
        assert not (banned & {n.lower() for n in names}), relative


@pytest.mark.parametrize("minutes", [0, -5])
def test_non_positive_minutes_are_refused(minutes):
    with pytest.raises(DocumentationError):
        TimeEntry.record("rper_1", "chld_x", "prov_1", local_date="2026-10-05",
                         timezone_of_record=ZONE, minutes=minutes,
                         activity_description="d")


def test_two_entries_add_and_a_correction_does_not_double_count(s):
    """§24. The monthly total is the CURRENT effective entries."""
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    s.rtm.record_time(s.provider_alpha, period.period_id,
                      local_date="2026-10-05", minutes=25,
                      activity_description="review and plan adjustment")
    second = s.rtm.record_time(s.provider_alpha, period.period_id,
                               local_date="2026-10-12", minutes=30,
                               activity_description="caregiver coaching")
    assert s.rtm.documented_minutes_for(s.provider_alpha, period.period_id) == 55

    s.rtm.correct_time(s.provider_alpha, second.time_entry_id, minutes=17,
                       reason="mis-keyed; actual was 17 minutes")
    assert s.rtm.documented_minutes_for(s.provider_alpha, period.period_id) == 42

    stored = s.rtm.list_time_entries(s.provider_alpha, period.period_id)
    assert len(stored) == 3, "the superseded row must be retained"
    assert sum(e.minutes for e in stored) == 72, "raw sum would double-count"


def test_a_correction_requires_a_reason():
    with pytest.raises(DocumentationError):
        TimeEntry.record("rper_1", "chld_x", "prov_1", local_date="2026-10-05",
                         timezone_of_record=ZONE, minutes=10,
                         activity_description="d",
                         supersedes_time_entry_id="tent_1")


def test_an_already_corrected_entry_cannot_be_corrected_again(s):
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    first = s.rtm.record_time(s.provider_alpha, period.period_id,
                              local_date="2026-10-05", minutes=20,
                              activity_description="d")
    s.rtm.correct_time(s.provider_alpha, first.time_entry_id, minutes=15,
                       reason="r")
    with pytest.raises(RTMConflict):
        s.rtm.correct_time(s.provider_alpha, first.time_entry_id, minutes=10,
                           reason="again")


def test_time_outside_the_period_month_is_refused(s):
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    with pytest.raises(RTMValidationError):
        s.rtm.record_time(s.provider_alpha, period.period_id,
                          local_date="2026-11-02", minutes=10,
                          activity_description="next month")


def test_the_monthly_total_is_deterministic_and_reconstructible(s):
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    for day, mins in (("2026-10-05", 12), ("2026-10-09", 18),
                      ("2026-10-20", 12)):
        s.rtm.record_time(s.provider_alpha, period.period_id, local_date=day,
                          minutes=mins, activity_description="d")
    entries = s.rtm.list_time_entries(s.provider_alpha, period.period_id)
    assert documented_minutes(entries) == 42
    assert documented_minutes(entries) == documented_minutes(list(reversed(entries)))


# ===========================================================================
# synchronous interaction — §9, §25
# ===========================================================================

@pytest.mark.parametrize("modality", [
    InteractionModality.PHONE, InteractionModality.VIDEO,
    InteractionModality.IN_PERSON, InteractionModality.OTHER_SYNCHRONOUS])
def test_every_modality_counts_as_real_time_with_the_family(s, modality):
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    interaction = s.rtm.record_synchronous_interaction(
        s.provider_alpha, period.period_id, local_date="2026-10-07",
        modality=modality, participant_type=ParticipantType.CAREGIVER)
    assert interaction.counts_as_real_time_communication


def test_asynchronous_exchange_has_no_representable_modality():
    """§9/§25. A message, note or email cannot be recorded as synchronous."""
    members = {m.name for m in InteractionModality}
    for absent in ("MESSAGE", "EMAIL", "TEXT", "NOTE", "ASYNC",
                   "ASYNCHRONOUS", "APP_ACTIVITY", "PLAN_REVIEW"):
        assert absent not in members
    with pytest.raises(ValueError):
        InteractionModality("message")


def test_other_synchronous_requires_explicit_real_time_affirmation():
    with pytest.raises(DocumentationError):
        SynchronousInteraction.record(
            "rper_1", "chld_x", "prov_1", occurred_at_utc=T0,
            local_date="2026-10-07", timezone_of_record=ZONE,
            modality=InteractionModality.OTHER_SYNCHRONOUS,
            participant_type=ParticipantType.CAREGIVER,
            real_time_affirmed=False)


def test_a_therapist_review_alone_does_not_satisfy_the_interaction_rule(s):
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    s.rtm.record_time(s.provider_alpha, period.period_id,
                      local_date="2026-10-05", minutes=42,
                      activity_description="d")
    s.rtm.record_review(s.provider_alpha, period.period_id,
                        clinical_interpretation="Reviewed the month.")

    coding = s.rtm.generate_coding_assistance(s.provider_alpha,
                                              period.period_id)
    assert coding.potential_code_candidates == ()
    assert (MissingRequirement.NO_REAL_TIME_INTERACTIVE_COMMUNICATION
            in coding.missing_requirement_flags)


def test_duration_is_never_inferred(s):
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    interaction = s.rtm.record_synchronous_interaction(
        s.provider_alpha, period.period_id, local_date="2026-10-07",
        modality=InteractionModality.PHONE,
        participant_type=ParticipantType.CAREGIVER)
    assert interaction.duration_minutes is None, "a duration was invented"


# ===========================================================================
# review and action — §6, §7
# ===========================================================================

def test_a_review_may_exist_with_no_intervention(s):
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    review = s.rtm.record_review(
        s.provider_alpha, period.period_id,
        clinical_interpretation="Steady engagement; continue as planned.")
    assert review.changes_planning is False
    assert s.repos.clinical_actions.list_for_period(period.period_id) == []


def test_a_review_requires_clinician_authored_interpretation():
    with pytest.raises(DocumentationError):
        TherapistReview.create("rper_1", "chld_x", "prov_1",
                               clinical_interpretation="   ")


def test_a_review_cannot_reference_another_childs_events(s):
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    with pytest.raises(RTMValidationError):
        s.rtm.record_review(s.provider_alpha, period.period_id,
                            clinical_interpretation="x",
                            reviewed_event_ids=("obsv_not_in_this_period",))


def test_an_action_type_is_not_a_medical_necessity_claim():
    action = ClinicalAction.create("trev_1", "rper_1", "chld_x", "prov_1",
                                   action_type=ClinicalActionType.CONTINUE_PLAN,
                                   narrative="n")
    assert action.establishes_medical_necessity is False


# ===========================================================================
# evidence summary — §5, §11, §13
# ===========================================================================

def test_distinct_observed_local_dates_is_a_fact_not_a_qualification(s):
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    instances = _instances(ctx["result"])
    for day, ref in (("2026-10-01", instances["activity-c"]),
                     ("2026-10-02", instances["activity-a"]),
                     ("2026-10-02", instances["activity-b"])):
        s.weekly.record_observation(
            s.caregiver_alpha, ctx["cycle"].cycle_id, ref, local_date=day,
            attempt_outcome=AttemptOutcome.DID_IT)

    summary = s.rtm.generate_evidence_summary(s.provider_alpha,
                                              period.period_id)
    assert summary.total_distinct_observation_events == 3
    assert summary.distinct_observed_local_dates == 2

    names = {f.name for f in dataclass_fields(RTMEvidenceSummary)}
    for banned in ("qualifying_days", "billable_days", "eligible_days",
                   "monitoring_days"):
        assert banned not in names


def test_per_goal_attribution_overlaps_and_is_not_the_total(s):
    """§6 carried into RTM reporting: never sum per-goal streams."""
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    # One attempt of the MULTI-GOAL activity.
    s.weekly.record_observation(
        s.caregiver_alpha, ctx["cycle"].cycle_id,
        _instances(ctx["result"])["activity-c"], local_date="2026-10-01",
        attempt_outcome=AttemptOutcome.DID_IT)

    summary = s.rtm.generate_evidence_summary(s.provider_alpha,
                                              period.period_id)
    assert summary.total_distinct_observation_events == 1
    assert summary.multi_goal_overlap_count == 1
    assert summary.sum_of_per_goal_attempts == 2
    assert summary.sum_of_per_goal_attempts != \
        summary.total_distinct_observation_events


def test_did_it_never_becomes_achieved(s):
    """§13. Activity completion does not establish goal achievement."""
    assert "ACHIEVED" not in {m.name for m in GoalStatusRecommendation}
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    for ref in _instances(ctx["result"]).values():
        s.weekly.record_observation(
            s.caregiver_alpha, ctx["cycle"].cycle_id, ref,
            local_date="2026-10-01", attempt_outcome=AttemptOutcome.DID_IT,
            difficulty=Difficulty.TOO_EASY)
    summary = s.rtm.generate_evidence_summary(s.provider_alpha,
                                              period.period_id)
    for line in summary.per_goal_evidence:
        assert line.status_recommendation is GoalStatusRecommendation.CONTINUE


def test_evidence_is_referenced_not_duplicated(s):
    """§5. No RTM-specific observation stream exists."""
    assert "rtm_observation" not in COLLECTIONS
    assert not any("rtm" in key and "observation" in key for key in COLLECTIONS)


def test_a_summary_is_regenerated_not_overwritten(s):
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    first = s.rtm.generate_evidence_summary(s.provider_alpha, period.period_id)
    second = s.rtm.generate_evidence_summary(s.provider_alpha, period.period_id)
    assert first.summary_id != second.summary_id
    assert s.repos.evidence_summaries.get_by_id(first.summary_id) == first
    assert not hasattr(s.repos.evidence_summaries, "update")


# ===========================================================================
# coding assistance — §14, §22
# ===========================================================================

def _document_42_minutes_with_contact(s, period):
    """42 CURRENT minutes, reached through a correction (§24 x §28)."""
    s.rtm.record_time(s.provider_alpha, period.period_id,
                      local_date="2026-10-05", minutes=25,
                      activity_description="review and plan adjustment")
    second = s.rtm.record_time(s.provider_alpha, period.period_id,
                               local_date="2026-10-12", minutes=30,
                               activity_description="caregiver coaching")
    s.rtm.correct_time(s.provider_alpha, second.time_entry_id, minutes=17,
                       reason="mis-keyed; actual was 17 minutes")
    return s.rtm.record_synchronous_interaction(
        s.provider_alpha, period.period_id, local_date="2026-10-14",
        modality=InteractionModality.PHONE,
        participant_type=ParticipantType.CAREGIVER, duration_minutes=15)


def test_coding_assistance_reads_current_minutes_only(s):
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    _document_42_minutes_with_contact(s, period)

    coding = s.rtm.generate_coding_assistance(s.provider_alpha,
                                              period.period_id)
    assert coding.documented_management_minutes == 42
    assert coding.potential_code_candidates == (("98980", 1), ("98981", 1))


def test_a_clinician_decision_never_rewrites_what_was_generated(s):
    """§22."""
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    _document_42_minutes_with_contact(s, period)
    generated = s.rtm.generate_coding_assistance(s.provider_alpha,
                                                 period.period_id)

    decided = s.rtm.decide_coding_assistance(
        s.provider_alpha, generated.coding_summary_id,
        ConfirmationStatus.CONFIRMED, note="agreed")

    assert decided.potential_code_candidates == generated.potential_code_candidates
    assert decided.rule_explanations == generated.rule_explanations
    assert decided.missing_requirement_flags == generated.missing_requirement_flags
    assert decided.coding_rule_version == generated.coding_rule_version
    assert decided.documented_management_minutes == \
        generated.documented_management_minutes
    assert decided.clinician_confirmed_by == s.topo.provider_alpha.provider_id
    assert decided.clinician_confirmation_status is ConfirmationStatus.CONFIRMED


def test_a_summary_cannot_be_decided_twice(s):
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    _document_42_minutes_with_contact(s, period)
    generated = s.rtm.generate_coding_assistance(s.provider_alpha,
                                                 period.period_id)
    s.rtm.decide_coding_assistance(s.provider_alpha,
                                   generated.coding_summary_id,
                                   ConfirmationStatus.REJECTED)
    with pytest.raises(SummaryError):
        s.rtm.decide_coding_assistance(s.provider_alpha,
                                       generated.coding_summary_id,
                                       ConfirmationStatus.CONFIRMED)


def test_coding_assistance_is_never_authoritative(s):
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    _document_42_minutes_with_contact(s, period)
    coding = s.rtm.generate_coding_assistance(s.provider_alpha,
                                              period.period_id)
    assert coding.is_authoritative is False
    assert coding.requires_clinician_confirmation is True


def test_the_summary_refuses_a_contradictory_candidate_set():
    with pytest.raises(SummaryError):
        CodingAssistanceSummary.create(
            "rper_1", "chld_x",
            potential_code_candidates=(("98979", 1), ("98980", 1)))
    with pytest.raises(SummaryError):
        CodingAssistanceSummary.create(
            "rper_1", "chld_x", potential_code_candidates=(("98981", 1),))


# ===========================================================================
# month-end report — §12, §27
# ===========================================================================

def _full_month(s):
    ctx = _planning(s)
    episode, period = _rtm_open(s, ctx)
    instances = _instances(ctx["result"])
    s.weekly.record_observation(
        s.caregiver_alpha, ctx["cycle"].cycle_id, instances["activity-c"],
        local_date="2026-10-01", attempt_outcome=AttemptOutcome.DID_IT)
    s.weekly.record_observation(
        s.caregiver_alpha, ctx["cycle"].cycle_id, instances["activity-a"],
        local_date="2026-10-02",
        attempt_outcome=AttemptOutcome.WASNT_READY_YET)
    _document_42_minutes_with_contact(s, period)
    review = s.rtm.record_review(
        s.provider_alpha, period.period_id,
        clinical_interpretation="Engagement steady; continue current focus.")
    s.rtm.record_clinical_action(
        s.provider_alpha, review.review_id,
        action_type=ClinicalActionType.CONTINUE_PLAN,
        narrative="Continue; revisit turn-taking next month.")
    s.rtm.finalize_period(s.provider_alpha, period.period_id)
    s.rtm.generate_evidence_summary(s.provider_alpha, period.period_id)
    s.rtm.generate_coding_assistance(s.provider_alpha, period.period_id)
    return ctx, episode, period


def test_the_report_has_all_eleven_sections(s):
    ctx, episode, period = _full_month(s)
    report = s.rtm.generate_report(s.provider_alpha, period.period_id)
    assert {c.section for c in report.sections} == set(ReportSection)
    assert len(report.sections) == 11


def test_section_c_declares_its_overlap(s):
    ctx, episode, period = _full_month(s)
    report = s.rtm.generate_report(s.provider_alpha, period.period_id)
    section = report.section_for(ReportSection.GOAL_ATTRIBUTION)
    assert section.overlap_declared is True
    assert section.count("multi_goal_overlap") == 1
    assert section.count("distinct_total") == 2


def test_section_d_is_attributed_to_the_caregiver(s):
    ctx, episode, period = _full_month(s)
    report = s.rtm.generate_report(s.provider_alpha, period.period_id)
    assert report.section_for(
        ReportSection.PARENT_OBSERVATIONS).attributed_to_role == "caregiver"


def test_section_k_carries_the_missing_requirement_flags(s):
    ctx, episode, period = _full_month(s)
    report = s.rtm.generate_report(s.provider_alpha, period.period_id)
    labels = dict(report.section_for(ReportSection.CODING_ASSISTANCE).labels)
    flags = [v for k, v in report.section_for(
        ReportSection.CODING_ASSISTANCE).labels if k == "missing_requirement"]
    assert MissingRequirement.TECHNOLOGY_STATUS_UNRESOLVED.value in flags
    assert MissingRequirement.CLINICIAN_CONFIRMATION_REQUIRED.value in flags
    assert labels["clinician_confirmation_status"] == "not_reviewed"


def test_a_finalized_report_cannot_be_rewritten(s):
    ctx, episode, period = _full_month(s)
    report = s.rtm.generate_report(s.provider_alpha, period.period_id)
    finalized = s.rtm.finalize_report(s.provider_alpha, report.report_id)
    with pytest.raises(FinalizedRecordImmutable):
        s.rtm.finalize_report(s.provider_alpha, report.report_id)
    with pytest.raises(ReportError):
        finalized.finalize(actor_id="prov_1")


def test_amendment_writes_a_successor_and_keeps_the_predecessor(s):
    ctx, episode, period = _full_month(s)
    report = s.rtm.generate_report(s.provider_alpha, period.period_id)
    finalized = s.rtm.finalize_report(s.provider_alpha, report.report_id)

    amended = s.rtm.amend_report(s.provider_alpha, finalized.report_id,
                                 reason="late time entry reconciled")
    assert amended.version == 2
    assert amended.state is ReportState.AMENDED
    assert amended.supersedes_report_id == finalized.report_id
    assert amended.amendment_reason

    predecessor = s.repos.month_end_reports.get_by_id(finalized.report_id)
    assert predecessor.superseded_by_report_id == amended.report_id
    assert predecessor.sections == finalized.sections, "predecessor was edited"
    assert s.repos.month_end_reports.current_for_period(
        period.period_id).report_id == amended.report_id


def test_a_refused_amendment_leaves_no_orphan_draft(s):
    """Found while analysing a surviving mutation.

    `amend_report` rebuilds the report before `MonthEndReport.amend`
    validates the reason, so an empty reason used to persist a draft row and
    then fail. The service now validates first.
    """
    ctx, episode, period = _full_month(s)
    report = s.rtm.generate_report(s.provider_alpha, period.period_id)
    finalized = s.rtm.finalize_report(s.provider_alpha, report.report_id)
    before = len(s.repos.month_end_reports.list_for_period(period.period_id))

    with pytest.raises(RTMValidationError):
        s.rtm.amend_report(s.provider_alpha, finalized.report_id, reason="  ")

    after = len(s.repos.month_end_reports.list_for_period(period.period_id))
    assert after == before, "a refused amendment persisted an orphan draft"


def test_the_amend_reason_guard_is_redundant_but_present(s):
    """Documents why one mutation survives the sweep.

    Removing the reason check from `MonthEndReport.amend` changes no
    behaviour, because `__post_init__` refuses an AMENDED report with no
    reason as well. The guard is defence in depth, kept deliberately, and
    this test pins the second layer so the redundancy is a tested fact
    rather than an assumption.
    """
    report = MonthEndReport.create("rper_1", "chld_x", "mfpl_1", CYCLE)
    with pytest.raises(ReportError):
        replace(MonthEndReport.create("rper_1", "chld_x", "mfpl_1", CYCLE,
                                      version=2, amendment_reason="   "),
                state=ReportState.AMENDED)


def test_amendment_requires_a_reason(s):
    ctx, episode, period = _full_month(s)
    report = s.rtm.generate_report(s.provider_alpha, period.period_id)
    finalized = s.rtm.finalize_report(s.provider_alpha, report.report_id)
    signature = inspect.signature(RTMService.amend_report)
    assert signature.parameters["reason"].default is inspect.Parameter.empty
    # The SERVICE refuses first, before any rebuild — see
    # test_a_refused_amendment_leaves_no_orphan_draft. The domain guard is
    # exercised directly in
    # test_the_report_record_itself_refuses_an_unexplained_amendment.
    with pytest.raises(RTMValidationError):
        s.rtm.amend_report(s.provider_alpha, finalized.report_id, reason="  ")


def test_a_draft_report_cannot_be_amended(s):
    ctx, episode, period = _full_month(s)
    report = s.rtm.generate_report(s.provider_alpha, period.period_id)
    with pytest.raises(RTMConflict):
        s.rtm.amend_report(s.provider_alpha, report.report_id, reason="r")


def test_the_report_carries_no_clinical_prose(s):
    """Sections index evidence; they never copy text."""
    ctx, episode, period = _full_month(s)
    report = s.rtm.generate_report(s.provider_alpha, period.period_id)
    blob = " ".join(
        [r for c in report.sections for r in c.record_refs]
        + [f"{k}{v}" for c in report.sections for k, v in c.labels]
        + [f"{k}{v}" for c in report.sections for k, v in c.counts])
    for fragment in ("Engagement steady", "revisit turn-taking",
                     "caregiver coaching", "mis-keyed"):
        assert fragment not in blob


# ===========================================================================
# FULL FICTIONAL OCTOBER E2E — §28
# ===========================================================================

def test_full_october_end_to_end(s):
    """One fictional October across every frozen layer, 0.4A through 0.4G.

    Neutral aliases throughout: Child-Alpha, Caregiver-Alpha, Provider-Alpha.
    """
    # --- identity (0.4A) ------------------------------------------------
    assignment = s.identity.assign_managing_clinician(
        s.provider_alpha, s.child, s.topo.provider_alpha.provider_id)
    assert assignment.is_active

    # --- goals and the month (0.4B/C) -----------------------------------
    offered = s.goals.generate_suggestions(s.provider_alpha, s.child,
                                           _snapshot(s.child))
    assert len(offered) == 2
    primary = s.goals.approve_clinical_goal(
        s.provider_alpha, s.child, edit_type=EditType.ACCEPTED_VERBATIM,
        suggestion_id=offered[0].suggestion_id)
    secondary = s.goals.approve_clinical_goal(
        s.provider_alpha, s.child, edit_type=EditType.MODIFIED,
        suggestion_id=offered[1].suggestion_id,
        text="Take short back-and-forth turns during a familiar game.",
        reason="narrowed to the routine the family practises")
    plan = s.plans.create_plan(s.provider_alpha, s.child, CYCLE, ZONE,
                               monitoring_focus="requesting at mealtimes")
    a1 = s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id,
                               primary.ref, priority_rank=1)
    a2 = s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id,
                               secondary.ref, priority_rank=2)
    assert (a1.emphasis_weight, a2.emphasis_weight) == (3, 2)
    s.plans.activate_plan(s.provider_alpha, plan.focus_plan_id)

    # --- weekly cycle 1: Oct 1-4 partial (0.4D) --------------------------
    cycle1 = s.weekly.create_cycle(s.provider_alpha, plan.focus_plan_id,
                                   sequence_in_month=1)
    assert (cycle1.starts_on, cycle1.ends_on) == ("2026-10-01", "2026-10-04")
    assert cycle1.is_partial

    g1, g2 = primary.ref, secondary.ref
    candidates = (
        CandidateActivity("activity-a", (g1,), primary_for=g1),
        CandidateActivity("activity-b", (g2,), primary_for=g2),
        CandidateActivity("activity-c", (g1, g2), primary_for=g1),
    )
    result = s.weekly.allocate_cycle(s.provider_alpha, cycle1.cycle_id,
                                     candidates, family_declared_capacity=3)
    instances = _instances(result)

    # C is ONE opportunity serving both goals.
    c_placement = next(p for p in result.placements
                       if p.activity_identity_ref == "activity-c")
    assert c_placement.goal_count == 2
    assert result.placed_count == 3
    assert result.attributions_for(g1) + result.attributions_for(g2) > \
        result.placed_count, "overlap must exist for the no-double-count proof"
    assert s.weekly.list_coverage_gaps(s.provider_alpha, cycle1.cycle_id) == []

    from pilot_backend.domain.source_link import SourceSystem
    s.weekly.capture_snapshot(s.provider_alpha, cycle1.cycle_id,
                              SourceSystem.PARENT, "fictional-parent-plan-001",
                              {"week": 1, "items": ["a", "b", "c"]})
    s.weekly.release_cycle(s.provider_alpha, cycle1.cycle_id)

    # --- parent evidence (0.4E) ------------------------------------------
    s.weekly.record_observation(
        s.caregiver_alpha, cycle1.cycle_id, instances["activity-a"],
        local_date="2026-10-01", attempt_outcome=AttemptOutcome.DID_IT)
    s.weekly.record_observation(
        s.caregiver_alpha, cycle1.cycle_id, instances["activity-b"],
        local_date="2026-10-02",
        attempt_outcome=AttemptOutcome.WASNT_READY_YET)
    defer = s.weekly.defer_activity(s.caregiver_alpha, cycle1.cycle_id,
                                    instances["activity-c"])

    # --- therapist future-cycle guidance ---------------------------------
    from pilot_backend.domain.intervention import (
        InterventionAction, InterventionScope)
    s.weekly.create_intervention(
        s.provider_alpha, cycle1.cycle_id,
        action=InterventionAction.ADD_GUIDANCE,
        applies_to=InterventionScope.FUTURE_CYCLE,
        clinical_rationale="Model the target twice before expecting a turn.",
        guidance_text="Offer two models first.")

    # --- cycle 2: adaptation (0.4E) --------------------------------------
    cycle2, result2, adaptation = s.weekly.generate_next_cycle(
        s.provider_alpha, cycle1.cycle_id, candidates,
        family_declared_capacity=3)
    assert (cycle2.starts_on, cycle2.ends_on) == ("2026-10-05", "2026-10-11")
    placed2 = {p.activity_identity_ref for p in result2.placements}
    assert "activity-c" not in placed2, "the defer was not honoured"
    assert adaptation.not_a_failure
    assert adaptation.resulting_change

    # --- RTM (0.4F) -------------------------------------------------------
    episode = s.rtm.open_episode(s.provider_alpha, s.child,
                                 [primary.ref, secondary.ref])
    technology = s.rtm.declare_technology(s.provider_alpha,
                                          episode.episode_id, "Genex RTM pilot")
    assert technology.regulatory_status is RegulatoryStatus.UNDER_REVIEW
    period = s.rtm.open_period(s.provider_alpha, episode.episode_id,
                               plan.focus_plan_id)
    assert period.focus_plan_id == plan.focus_plan_id

    review = s.rtm.record_review(
        s.provider_alpha, period.period_id,
        clinical_interpretation="Requesting emerging; turn-taking needs more "
                                "adult modelling.",
        reviewed_cycle_ids=(cycle1.cycle_id, cycle2.cycle_id))
    s.rtm.record_clinical_action(
        s.provider_alpha, review.review_id,
        action_type=ClinicalActionType.EDUCATION_OR_COACHING,
        narrative="Coached caregiver on modelling before expectation.")
    _document_42_minutes_with_contact(s, period)

    # --- month end (0.4G) -------------------------------------------------
    finalized_period = s.rtm.finalize_period(s.provider_alpha,
                                             period.period_id)
    assert finalized_period.is_finalized
    assert s.rtm.get_episode(s.provider_alpha,
                             episode.episode_id).is_open, "episode auto-closed"

    evidence = s.rtm.generate_evidence_summary(s.provider_alpha,
                                               period.period_id)
    assert evidence.documented_management_minutes == 42
    assert evidence.real_time_interactive_communication_present
    assert evidence.technology_regulatory_status is RegulatoryStatus.UNDER_REVIEW

    coding = s.rtm.generate_coding_assistance(s.provider_alpha,
                                              period.period_id)
    assert coding.potential_code_candidates == (("98980", 1), ("98981", 1))
    assert (MissingRequirement.TECHNOLOGY_STATUS_UNRESOLVED
            in coding.missing_requirement_flags)
    assert (MissingRequirement.CLINICIAN_CONFIRMATION_REQUIRED
            in coding.missing_requirement_flags)

    blob = " ".join(coding.rule_explanations).lower()
    for word in ("billable", "reimburs", "claim", "approved code"):
        assert word not in blob

    report = s.rtm.generate_report(s.provider_alpha, period.period_id)
    issued = s.rtm.finalize_report(s.provider_alpha, report.report_id)
    assert issued.state is ReportState.FINALIZED
    assert len(issued.sections) == 11


# ===========================================================================
# NEGATIVE E2E — §29
# ===========================================================================

def test_42_minutes_without_real_time_contact_yields_no_candidate(s):
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    s.rtm.record_time(s.provider_alpha, period.period_id,
                      local_date="2026-10-05", minutes=42,
                      activity_description="documentation and planning")
    coding = s.rtm.generate_coding_assistance(s.provider_alpha,
                                              period.period_id)
    assert coding.documented_management_minutes == 42
    assert coding.potential_code_candidates == ()
    assert (MissingRequirement.NO_REAL_TIME_INTERACTIVE_COMMUNICATION
            in coding.missing_requirement_flags)


def test_a_caregiver_cannot_write_clinical_records(s):
    ctx = _planning(s)
    episode, period = _rtm_open(s, ctx)
    for call in (
        lambda: s.rtm.open_episode(s.caregiver_alpha, s.child,
                                   [g.ref for g in ctx["goals"]]),
        lambda: s.rtm.record_review(s.caregiver_alpha, period.period_id,
                                    clinical_interpretation="x"),
        lambda: s.rtm.record_time(s.caregiver_alpha, period.period_id,
                                  local_date="2026-10-05", minutes=10,
                                  activity_description="d"),
        lambda: s.rtm.record_synchronous_interaction(
            s.caregiver_alpha, period.period_id, local_date="2026-10-05",
            modality=InteractionModality.PHONE,
            participant_type=ParticipantType.CAREGIVER),
        lambda: s.rtm.finalize_period(s.caregiver_alpha, period.period_id),
        lambda: s.rtm.generate_coding_assistance(s.caregiver_alpha,
                                                 period.period_id),
    ):
        with pytest.raises(RTMAuthorizationError):
            call()


def test_a_connected_non_managing_provider_is_refused(s):
    ctx = _planning(s)
    episode, period = _rtm_open(s, ctx)
    gamma = s.connected_provider_gamma()
    for call in (
        lambda: s.rtm.record_review(gamma, period.period_id,
                                    clinical_interpretation="x"),
        lambda: s.rtm.record_time(gamma, period.period_id,
                                  local_date="2026-10-05", minutes=10,
                                  activity_description="d"),
        lambda: s.rtm.finalize_period(gamma, period.period_id),
    ):
        with pytest.raises(RTMAuthorizationError):
            call()
    # Reads are permitted for a connected provider.
    assert s.rtm.get_period(gamma, period.period_id) == period


def test_an_unrelated_provider_is_denied_entirely(s):
    ctx = _planning(s)
    episode, period = _rtm_open(s, ctx)
    for call in (
        lambda: s.rtm.get_episode(s.provider_beta, episode.episode_id),
        lambda: s.rtm.get_period(s.provider_beta, period.period_id),
        lambda: s.rtm.record_review(s.provider_beta, period.period_id,
                                    clinical_interpretation="x"),
    ):
        with pytest.raises(RTMAuthorizationError):
            call()


def test_a_revoked_connection_fails_closed(s):
    ctx = _planning(s)
    episode, period = _rtm_open(s, ctx)
    s.repos.provider_child.end_connection(
        s.topo.link_alpha_provider.connection_id,
        status=ConnectionStatus.REVOKED)
    with pytest.raises(RTMAuthorizationError):
        s.rtm.record_time(s.provider_alpha, period.period_id,
                          local_date="2026-10-05", minutes=10,
                          activity_description="d")


def test_no_client_supplied_role_reaches_the_rtm_service(s):
    for name, member in inspect.getmembers(RTMService,
                                           predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        params = set(inspect.signature(member).parameters)
        assert not (params & {"role", "actor_role", "uid", "is_admin",
                              "provider_id", "caregiver_id", "actor_id"}), name


def test_every_public_rtm_method_requires_a_principal():
    for name, member in inspect.getmembers(RTMService,
                                           predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        params = list(inspect.signature(member).parameters)
        assert params[:2] == ["self", "principal"], (name, params)


# ===========================================================================
# payer / insurance absence — §23
# ===========================================================================

#: Insurance/billing vocabulary that must never appear.
#:
#: Bare `claim` and `claim_id` are deliberately NOT here. They collide with
#: the 0.4A write-time uniqueness CLAIM — a mutex primitive, a completely
#: different sense of the word — which the RTM period legitimately uses for
#: its one-period-per-episode-month constraint. Banning the bare token would
#: have forced the uniqueness mechanism to be renamed to satisfy a test,
#: which is the test bending the system rather than describing it.
#:
#: So the ban is on BILLING-claim tokens, and
#: `test_every_claim_token_is_the_identity_primitive` closes the gap from the
#: other side by asserting positively that every claim identifier in the RTM
#: layer is the 0.4A primitive.
_PAYER_VOCABULARY = {
    "member_id", "policy_number", "group_number", "insurance_plan",
    "insurance", "payer_id", "payer", "eligibility_status", "benefit_status",
    "benefits", "allowed_amount", "reimbursement_amount", "reimbursement",
    "claim_status", "claim_number", "claim_amount", "insurance_claim",
    "claim_submission", "clearinghouse", "copay", "deductible",
    "coinsurance", "remittance",
}

#: Every permitted `claim`-containing identifier, each the 0.4A mutex.
_IDENTITY_CLAIM_TOKENS = {
    "claim", "claim_id", "claim_kind", "claims", "identity_claims",
    "uniqueness_claim_id", "next_generation", "claimkind", "identityclaim",
}

_RTM_MODULES = ("domain/rtm.py", "domain/rtm_documentation.py",
                "domain/rtm_summary.py", "domain/month_end.py",
                "coding/rules.py", "rtm/service.py", "rtm/evidence.py",
                "rtm/reporting.py")


@pytest.mark.parametrize("relative", _RTM_MODULES)
def test_no_payer_vocabulary_in_the_rtm_layer(relative):
    """Token-level, so prose cannot trip it and an identifier cannot hide."""
    tree = ast.parse((PILOT_ROOT / relative).read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
    offenders = _PAYER_VOCABULARY & {n.lower() for n in names}
    assert not offenders, (relative, offenders)


def test_payer_verification_does_not_exist_anywhere():
    for path in sorted(PILOT_ROOT.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        classes = {n.name for n in ast.walk(ast.parse(path.read_text()))
                   if isinstance(n, ast.ClassDef)}
        assert "PayerVerification" not in classes, path.name


def test_no_rtm_record_has_a_payer_field():
    for model in (RTMEpisode, RTMMonitoringPeriod, RTMTechnology,
                  TherapistReview, ClinicalAction, TimeEntry,
                  SynchronousInteraction, RTMEvidenceSummary,
                  CodingAssistanceSummary, MonthEndReport):
        names = {f.name.lower() for f in dataclass_fields(model)}
        assert not (_PAYER_VOCABULARY & names), model.__name__


def test_every_claim_token_is_the_identity_primitive():
    """Closes the gap left by not banning the bare word `claim`.

    Every claim-containing identifier in the RTM layer must be the 0.4A
    uniqueness mutex. A billing claim would show up here as an unrecognised
    token rather than slipping through a vocabulary list that had to exclude
    the word.
    """
    for relative in _RTM_MODULES:
        tree = ast.parse((PILOT_ROOT / relative).read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
        claimish = {n for n in names if "claim" in n.lower()}
        unexpected = {n for n in claimish
                      if n.lower() not in _IDENTITY_CLAIM_TOKENS}
        assert not unexpected, (relative, unexpected)


def test_no_payer_collection_is_registered():
    """`pilot_identity_claims` is the 0.4A mutex store, not a billing store."""
    for key, name in COLLECTIONS.items():
        blob = f"{key} {name}".lower()
        for word in ("payer", "insurance", "eligibility", "benefit",
                     "claim_status", "clearinghouse", "remittance"):
            assert word not in blob, (key, name)


# ===========================================================================
# audit — §30
# ===========================================================================

def test_material_rtm_actions_emit_audit_events(s):
    ctx, episode, period = _full_month(s)
    report = s.rtm.generate_report(s.provider_alpha, period.period_id)
    s.rtm.finalize_report(s.provider_alpha, report.report_id)
    s.rtm.close_episode(s.provider_alpha, episode.episode_id, reason="done")

    actions = {e.action for e in s.repos.audit_events.list_all()}
    assert {AuditAction.RTM_EPISODE_OPENED,
            AuditAction.RTM_EPISODE_CLOSED,
            AuditAction.RTM_TECHNOLOGY_DECLARED,
            AuditAction.RTM_PERIOD_OPENED,
            AuditAction.RTM_PERIOD_FINALIZED,
            AuditAction.THERAPIST_REVIEW_RECORDED,
            AuditAction.CLINICAL_ACTION_RECORDED,
            AuditAction.TIME_ENTRY_RECORDED,
            AuditAction.TIME_ENTRY_CORRECTED,
            AuditAction.SYNCHRONOUS_INTERACTION_RECORDED,
            AuditAction.RTM_EVIDENCE_SUMMARY_GENERATED,
            AuditAction.CODING_ASSISTANCE_GENERATED,
            AuditAction.MONTH_END_REPORT_FINALIZED} <= actions


def test_the_audit_trail_carries_no_clinical_text(s):
    ctx = _planning(s)
    _, period = _rtm_open(s, ctx)
    s.rtm.record_time(s.provider_alpha, period.period_id,
                      local_date="2026-10-05", minutes=20,
                      activity_description=SENTINEL_NOTE)
    review = s.rtm.record_review(s.provider_alpha, period.period_id,
                                 clinical_interpretation=SENTINEL_CONCERN)
    s.rtm.record_clinical_action(
        s.provider_alpha, review.review_id,
        action_type=ClinicalActionType.OTHER_CLINICAL_ACTION,
        narrative=SENTINEL_NOTE)

    for event in s.repos.audit_events.list_all():
        blob = " ".join([event.resource_type, event.resource_id or "",
                         *event.metadata.keys(), *event.metadata.values()])
        for sentinel in ALL_SENTINELS:
            assert sentinel not in blob, event.action


def test_no_rtm_metadata_key_escapes_the_allowlist(s):
    ctx, episode, period = _full_month(s)
    for event in s.repos.audit_events.list_all():
        assert set(event.metadata) <= ALLOWED_METADATA_KEYS


def test_rtm_errors_are_phi_safe_by_declaration():
    for error in (RTMConflict, RTMAuthorizationError, RTMValidationError,
                  FinalizedRecordImmutable, EpisodeTransferRefused,
                  RTMError, DocumentationError, SummaryError, ReportError,
                  CodingRuleError):
        assert getattr(error, "PHI_SAFE_MESSAGE", False), error.__name__


# ===========================================================================
# persistence
# ===========================================================================

def test_every_rtm_record_round_trips(s):
    ctx, episode, period = _full_month(s)
    report = s.rtm.generate_report(s.provider_alpha, period.period_id)
    records = [
        episode,
        s.repos.rtm_periods.get_by_id(period.period_id),
        *s.repos.rtm_technologies.list_for_episode(episode.episode_id),
        *s.repos.therapist_reviews.list_for_period(period.period_id),
        *s.repos.clinical_actions.list_for_period(period.period_id),
        *s.repos.time_entries.list_for_period(period.period_id),
        *s.repos.synchronous_interactions.list_for_period(period.period_id),
        *s.repos.evidence_summaries.list_for_period(period.period_id),
        *s.repos.coding_summaries.list_for_period(period.period_id),
        report,
    ]
    assert len(records) >= 10
    for record in records:
        assert decode(type(record), encode(record)) == record


def test_no_encoded_document_contains_a_nested_array(s):
    """Firestore does not support nested arrays. The fake does.

    `_PairTuple` originally encoded pairs as `[[k, v], ...]`; every unit test
    passed and the real client rejected the write, affecting seven of the
    eleven 0.4F/G record types. This asserts the property the fake cannot:
    no encoded value is a list containing a list.
    """
    ctx, episode, period = _full_month(s)
    report = s.rtm.generate_report(s.provider_alpha, period.period_id)

    def assert_no_nested_array(value, path):
        if isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                assert not isinstance(item, (list, tuple)), \
                    f"nested array at {path}[{index}] — Firestore refuses this"
                assert_no_nested_array(item, f"{path}[{index}]")
        elif isinstance(value, dict):
            for key, item in value.items():
                assert_no_nested_array(item, f"{path}.{key}")

    records = [
        episode, s.repos.rtm_periods.get_by_id(period.period_id), report,
        *s.repos.evidence_summaries.list_for_period(period.period_id),
        *s.repos.coding_summaries.list_for_period(period.period_id),
        *s.repos.time_entries.list_for_period(period.period_id),
    ]
    assert len(records) >= 6
    for record in records:
        assert_no_nested_array(encode(record), type(record).__name__)


def test_only_pilot_prefixed_collections_are_used(s):
    _full_month(s)
    for name in s.store.collections():
        assert name.startswith(PILOT_COLLECTION_PREFIX), name


@pytest.mark.parametrize("repo_name", [
    "rtm_episodes", "rtm_periods", "rtm_technologies", "therapist_reviews",
    "clinical_actions", "time_entries", "synchronous_interactions",
    "evidence_summaries", "coding_summaries", "month_end_reports"])
def test_no_rtm_repository_exposes_a_delete(repo_name):
    repos = FirestoreRepositories(FakeDocumentStore())
    repo = getattr(repos, repo_name)
    banned = ("delete", "remove", "purge", "drop", "destroy", "erase",
              "truncate")
    for attribute in dir(repo):
        if attribute.startswith("_"):
            continue
        assert not any(w in attribute.lower() for w in banned), \
            (repo_name, attribute)


def test_immutable_rtm_records_have_no_update_path():
    repos = FirestoreRepositories(FakeDocumentStore())
    for repo in (repos.therapist_reviews, repos.clinical_actions,
                 repos.synchronous_interactions, repos.evidence_summaries):
        assert not hasattr(repo, "update"), repo.record_type


def test_rtm_modules_never_call_a_delete():
    for relative in _RTM_MODULES + ("rtm/errors.py",):
        tree = ast.parse((PILOT_ROOT / relative).read_text())
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not ({"delete", "remove", "purge", "drop"} & called), relative


def test_listings_do_not_tie_on_a_foreign_key(s):
    """The recurring ordering trap, checked for the 0.4F/G own-ids."""
    ctx, episode, period = _full_month(s)
    s.rtm.generate_report(s.provider_alpha, period.period_id)
    for listing in (s.repos.time_entries.list_for_period(period.period_id),
                    s.repos.therapist_reviews.list_for_period(period.period_id),
                    s.repos.month_end_reports.list_for_period(period.period_id)):
        ids = [getattr(r, f.name) for r in listing
               for f in dataclass_fields(r)[:1]]
        assert len(set(ids)) == len(ids)


# ===========================================================================
# scope guard — 0.4F/G is the last fictional slice
# ===========================================================================

def test_no_out_of_scope_object_was_implemented():
    banned = {"PayerVerification", "MonitoringDay", "ClearingHouseSubmission",
              "ClaimSubmission", "EligibilityCheck", "RemittanceAdvice",
              "EMRIntegration", "ReimbursementEstimate"}
    for path in sorted(PILOT_ROOT.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        names = {n.name for n in ast.walk(ast.parse(path.read_text()))
                 if isinstance(n, ast.ClassDef)}
        assert not (banned & names), (path.name, banned & names)


def test_monitoring_day_qualification_is_not_computed():
    """§5. MonitoringDay stays deferred; only distinct dates are counted."""
    for relative in _RTM_MODULES:
        source = (PILOT_ROOT / relative).read_text()
        tree = ast.parse(source)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.add(node.name)
        lowered = {n.lower() for n in names}
        for banned in ("monitoring_day", "monitoringday", "qualifying_day",
                       "qualifying_days", "billable_day", "billable_days"):
            assert banned not in lowered, (relative, banned)
