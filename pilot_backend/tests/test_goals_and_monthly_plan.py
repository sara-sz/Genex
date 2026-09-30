"""0.4B/C — goal layer and monthly focus-plan layer.

Same harness as 0.4A: `FirestoreRepositories` over `FakeDocumentStore`, so
these are the production repository code paths rather than a parallel
in-memory implementation that could drift. The concurrency claims are NOT made
here — `FakeDocumentStore` is not thread-safe and the 0.4A freeze recorded
that as carried debt. Racing writers are proven only in
`pilot_runtime/tests/integration/`.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pilot_backend.audit.events import ALLOWED_METADATA_KEYS, AuditAction, AuditResult
from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth import VerifiedToken, resolve_principal
from pilot_backend.domain.enums import ConnectionStatus
from pilot_backend.domain.goal_vocabulary import (
    CANONICAL_DOMAIN_KEYS,
    UnknownDomainError,
    domain_rank,
    require_canonical_domain,
)
from pilot_backend.domain.goals import (
    CHILD_PLACEHOLDER,
    CaregiverApprovedGoal,
    ClinicalGoal,
    EditType,
    EvidenceSource,
    GoalError,
    GoalKind,
    GoalRef,
    GoalStatus,
    GoalSuggestion,
    GoalSuggestionEvidence,
    GoalVersion,
    SuggestionStatus,
    require_clinical_goal_ref,
)
from pilot_backend.domain.identity_claims import ClaimKind, key_digest
from pilot_backend.domain.monthly_plan import (
    AllocationStatus,
    MonthlyFocusPlan,
    MonthlyGoalAllocation,
    MonthlyGoalSnapshot,
    MonthlyPlanError,
    MonthlyPlanState,
    TimezoneError,
    month_bounds,
    validate_cycle_month,
    validate_timezone,
)
from pilot_backend.domain.planning_policy import (
    CURRENT_PLANNING_POLICY,
    POLICY_2026_10,
    PlanningPolicyError,
    PlanningPolicyVersion,
    known_policy_versions,
    policy_for,
)
from pilot_backend.domain.roles import ActorRole
from pilot_backend.fixtures.secure_topology import (
    CAREGIVER_ALPHA_SUBJECT,
    CAREGIVER_BETA_SUBJECT,
    CAREGIVER_GAMMA_SUBJECT,
    PROVIDER_ALPHA_SUBJECT,
    PROVIDER_BETA_SUBJECT,
    PROVIDER_GAMMA_SUBJECT,
    build_secure_topology,
)
from pilot_backend.goals.errors import (
    GoalAuthorizationError,
    GoalConflict,
    GoalValidationError,
)
from pilot_backend.goals.service import GoalService
from pilot_backend.goals.suggestion_engine import (
    DOMAIN_TEMPLATES,
    GENERATOR_VERSION,
    SUGGESTION_RULE_VERSION,
    ObservationSnapshot,
    ObservedDomain,
    SuggestionEngineError,
    eligible_domains,
    evidence_score,
    explain,
    generate_suggestions,
)
from pilot_backend.identity import LongitudinalIdentityService
from pilot_backend.persistence import FakeDocumentStore, FirestoreRepositories, encode
from pilot_backend.persistence.codecs import decode
from pilot_backend.persistence.collections import COLLECTIONS, PILOT_COLLECTION_PREFIX
from pilot_backend.planning.service import MonthlyPlanService

from .test_secure_foundation import ALL_SENTINELS, SENTINEL_CONCERN, SENTINEL_NOTE

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
PILOT_ROOT = Path(__file__).resolve().parents[1]
CYCLE = "2026-10"
ZONE = "America/New_York"


class _AdvancingClock:
    """Deterministic but strictly increasing — see 0.4A for why frozen fails."""

    def __init__(self, start: datetime) -> None:
        self._t = start
        self._tick = 0

    def __call__(self) -> datetime:
        self._tick += 1
        return self._t.replace(microsecond=0) + timedelta(seconds=self._tick)


class Stack:
    def __init__(self):
        self.store = FakeDocumentStore()
        self.repos = FirestoreRepositories(self.store)
        self.topo = build_secure_topology(self.repos, now=T0)
        self.recorder = AuditRecorder(self.repos.audit_events, environment="test")
        self.clock = _AdvancingClock(T0)
        self.identity = LongitudinalIdentityService(
            repos=self.repos, recorder=self.recorder, now=self.clock)
        self.goals = GoalService(
            repos=self.repos, recorder=self.recorder, now=self.clock)
        self.plans = MonthlyPlanService(
            repos=self.repos, recorder=self.recorder, now=self.clock)

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
        """Provider-Gamma, ACTIVE on Child-Alpha but never the owner.

        The topology ships Gamma as PENDING, which the 0.2 gate refuses before
        any ownership rule runs. Activating the connection is what makes the
        ownership check the thing under test rather than the access check —
        a mutation dropping the managing-clinician comparison survived until
        this existed.
        """
        self.repos.provider_child.activate(
            self.topo.link_gamma_provider_pending.connection_id, now=T0)
        return self.principal(PROVIDER_GAMMA_SUBJECT)

    def connected_caregiver_gamma(self):
        """Caregiver-Gamma, re-connected to Child-Alpha and ACTIVE.

        Same reasoning: Gamma's shipped connection is REVOKED, so a test using
        it never reaches the "only the approving caregiver" rule.
        """
        from pilot_backend.domain.connections import CaregiverChildConnection
        from pilot_backend.domain.enums import CaregiverRelationship

        self.repos.caregiver_child.connect(CaregiverChildConnection.create(
            self.topo.caregiver_gamma.caregiver_id, self.child,
            CaregiverRelationship.PARENT,
            actor_id=self.topo.caregiver_gamma.caregiver_id, now=T0))
        return self.principal(CAREGIVER_GAMMA_SUBJECT)

    def make_managing_clinician(self):
        """Give Provider-Alpha clinical ownership of Child-Alpha."""
        return self.identity.assign_managing_clinician(
            self.provider_alpha, self.child, self.topo.provider_alpha.provider_id)

    def offer(self, *, count=None, child=None):
        target = child or self.child
        return self.goals.generate_suggestions(
            self.provider_alpha if self._has_clinician(target) else self.caregiver_alpha,
            target, snapshot(target), count=count)

    def _has_clinician(self, child_id):
        return bool(self.repos.managing_clinicians.list_for_child(child_id))


def snapshot(child_id, *, cycle=CYCLE, domains=None) -> ObservationSnapshot:
    return ObservationSnapshot(child_id, cycle, domains if domains is not None else (
        ObservedDomain("talking_and_communicating", True,
                       EvidenceSource.CLINICIAN_OBSERVATION,
                       milestone_refs=("mv1:cdc:comm:24m:two-word",),
                       functional_baseline_area="requesting",
                       observed_level="emerging"),
        ObservedDomain("fine_motor", True,
                       EvidenceSource.CAREGIVER_REPORTED_MILESTONE,
                       milestone_refs=("mv1:cdc:fine:24m:scribble",)),
        ObservedDomain("gross_motor", True,
                       EvidenceSource.CAREGIVER_REPORTED_MILESTONE,
                       functional_baseline_area="transitions"),
    ))


@pytest.fixture()
def s():
    return Stack()


@pytest.fixture()
def clinical(s):
    """Stack with Provider-Alpha as managing clinician and one approved goal."""
    s.make_managing_clinician()
    offered = s.goals.generate_suggestions(s.provider_alpha, s.child, snapshot(s.child))
    goal = s.goals.approve_clinical_goal(
        s.provider_alpha, s.child, edit_type=EditType.ACCEPTED_VERBATIM,
        suggestion_id=offered[0].suggestion_id)
    return s, offered, goal


# ===========================================================================
# vocabulary — mirrored, not invented
# ===========================================================================

def test_canonical_domains_mirror_parent_taxonomy():
    """The pinning test the mirror exists for.

    `pilot_backend` cannot import `parent_taxonomy`: they are separate
    top-level namespaces and the pilot CI job runs from the repository root,
    where `genex-parent/parent_taxonomy` is not importable. So the seven keys
    are restated, and this is where a divergence surfaces — deliberately and
    loudly — if Parent's taxonomy ever changes.
    """
    assert CANONICAL_DOMAIN_KEYS == (
        "talking_and_communicating", "social_and_emotional",
        "learning_and_thinking", "fine_motor", "gross_motor",
        "daily_living", "sensory",
    )
    assert len(set(CANONICAL_DOMAIN_KEYS)) == 7


def test_an_unknown_domain_is_refused_not_passed_through():
    with pytest.raises(UnknownDomainError):
        require_canonical_domain("executive_function")
    with pytest.raises(UnknownDomainError):
        require_canonical_domain("")
    assert require_canonical_domain("  Fine_Motor ") == "fine_motor"


def test_domain_rank_is_a_total_order_over_the_seven():
    ranks = [domain_rank(k) for k in CANONICAL_DOMAIN_KEYS]
    assert ranks == sorted(ranks) == list(range(7))


# ===========================================================================
# the suggestion engine — deterministic, offline, explainable
# ===========================================================================

def test_the_engine_makes_no_network_call_and_imports_no_model_client():
    """Proven structurally, not by a comment promising it.

    An AST scan of the imports, so a docstring mentioning "LLM" cannot pass or
    fail the check and an added `import requests` cannot hide in a helper.
    """
    tree = ast.parse((PILOT_ROOT / "goals/suggestion_engine.py").read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    banned = {"requests", "httpx", "urllib", "urllib3", "http", "socket",
              "openai", "anthropic", "google", "vertexai", "aiohttp"}
    assert not (modules & banned), modules & banned


def test_the_same_snapshot_always_produces_the_same_ranking_and_wording(s):
    first = generate_suggestions(snapshot(s.child))
    second = generate_suggestions(snapshot(s.child))
    assert [x.evidence.domain_key for x in first] == \
           [x.evidence.domain_key for x in second]
    assert [x.family_facing_text_template for x in first] == \
           [x.family_facing_text_template for x in second]
    assert [x.suggested_priority_rank for x in first] == [1, 2]


def test_input_order_does_not_change_the_ranking(s):
    forward = snapshot(s.child)
    reversed_domains = ObservationSnapshot(
        s.child, CYCLE, tuple(reversed(forward.domains)))
    assert [x.evidence.domain_key for x in generate_suggestions(forward)] == \
           [x.evidence.domain_key for x in generate_suggestions(reversed_domains)]


def test_explicit_selection_outranks_stronger_milestone_evidence(s):
    domains = (
        ObservedDomain("learning_and_thinking", True,
                       EvidenceSource.CLINICIAN_OBSERVATION,
                       milestone_refs=("a", "b", "c", "d"),
                       functional_baseline_area="following-directions"),
        ObservedDomain("daily_living", True, EvidenceSource.EXPLICIT_SELECTION,
                       explicitly_selected=True),
    )
    ranked = generate_suggestions(ObservationSnapshot(s.child, CYCLE, domains))
    assert ranked[0].evidence.domain_key == "daily_living"


def test_ties_break_on_canonical_order_not_insertion_order(s):
    domains = (
        ObservedDomain("gross_motor", True,
                       EvidenceSource.CAREGIVER_REPORTED_MILESTONE,
                       milestone_refs=("x",)),
        ObservedDomain("fine_motor", True,
                       EvidenceSource.CAREGIVER_REPORTED_MILESTONE,
                       milestone_refs=("y",)),
    )
    ranked = generate_suggestions(ObservationSnapshot(s.child, CYCLE, domains))
    assert evidence_score(domains[0]) == evidence_score(domains[1])
    assert [x.evidence.domain_key for x in ranked] == ["fine_motor", "gross_motor"]


def test_a_no_answer_is_never_treated_as_a_level_or_an_age(s):
    """The Parent invariant, as a test.

    An unanswered domain is UNKNOWN. It is not defaulted to six months, to a
    zero score, or to anything else that would let it compete — it is simply
    not a candidate.
    """
    domains = (
        ObservedDomain("social_and_emotional", False,
                       EvidenceSource.CAREGIVER_REPORTED_MILESTONE,
                       milestone_refs=("m",)),
        ObservedDomain("fine_motor", True,
                       EvidenceSource.CAREGIVER_REPORTED_MILESTONE,
                       milestone_refs=("m2",)),
    )
    ranked = generate_suggestions(ObservationSnapshot(s.child, CYCLE, domains), count=7)
    assert [x.evidence.domain_key for x in ranked] == ["fine_motor"]


def test_an_unsupported_domain_is_not_padded_into_the_offer(s):
    """Returning fewer than asked for is correct, not a bug to paper over."""
    domains = (
        ObservedDomain("fine_motor", True,
                       EvidenceSource.CAREGIVER_REPORTED_MILESTONE,
                       milestone_refs=("m",)),
        ObservedDomain("gross_motor", True,
                       EvidenceSource.CAREGIVER_REPORTED_MILESTONE),
    )
    assert len(generate_suggestions(
        ObservationSnapshot(s.child, CYCLE, domains), count=5)) == 1


def test_sensory_evidence_is_never_invented(s):
    """Parent 2.4 shipped `sensory` with no curated milestone content.

    The engine consumes supplied observations and fabricates nothing, so a
    sensory suggestion appears only where a human actually observed or
    selected something — never from milestone evidence that does not exist.
    """
    nothing_observed = ObservationSnapshot(s.child, CYCLE, ())
    assert generate_suggestions(nothing_observed, count=7) == ()

    observed = ObservationSnapshot(s.child, CYCLE, (
        ObservedDomain("sensory", True, EvidenceSource.CLINICIAN_OBSERVATION,
                       functional_baseline_area="mealtime-tolerance"),))
    assert [x.evidence.domain_key
            for x in generate_suggestions(observed)] == ["sensory"]


def test_diagnosis_has_no_representable_slot_in_evidence():
    """"Diagnosis does not override observation", enforced by absence."""
    members = {m.name for m in EvidenceSource}
    assert not any("DIAGNOS" in name for name in members), members
    with pytest.raises(ValueError):
        EvidenceSource("diagnosis")


def test_chronological_age_is_not_a_parameter_of_the_engine():
    """Age is context. There is nothing here to misuse it with."""
    for fn in (generate_suggestions, eligible_domains, evidence_score):
        params = set(inspect.signature(fn).parameters)
        assert not any("age" in p for p in params), (fn.__name__, params)
    fields = set(ObservedDomain.__dataclass_fields__) | \
        set(ObservationSnapshot.__dataclass_fields__)
    assert not any("age" in f or "diagnos" in f for f in fields), fields


def test_wording_is_chosen_from_a_catalogue_never_composed_from_input(s):
    """No observed value can reach the stored text."""
    domains = (ObservedDomain(
        "fine_motor", True, EvidenceSource.CLINICIAN_OBSERVATION,
        milestone_refs=(SENTINEL_NOTE,), functional_baseline_area=SENTINEL_CONCERN,
        observed_level=SENTINEL_NOTE),)
    offered = generate_suggestions(ObservationSnapshot(s.child, CYCLE, domains))
    assert offered[0].family_facing_text_template in DOMAIN_TEMPLATES.values()
    for sentinel in ALL_SENTINELS:
        assert sentinel not in offered[0].family_facing_text_template


def test_every_canonical_domain_has_wording_and_none_carries_a_name():
    assert set(DOMAIN_TEMPLATES) == set(CANONICAL_DOMAIN_KEYS)
    for key, template in DOMAIN_TEMPLATES.items():
        assert CHILD_PLACEHOLDER in template, key


def test_a_stored_suggestion_never_contains_a_child_name(s):
    for suggestion in generate_suggestions(snapshot(s.child)):
        assert CHILD_PLACEHOLDER in suggestion.family_facing_text_template
        assert "Maya" not in suggestion.family_facing_text_template
        rendered = suggestion.render("Fictional-Child")
        assert "Fictional-Child" in rendered
        # Rendering is a pure function: nothing was written back.
        assert CHILD_PLACEHOLDER in suggestion.family_facing_text_template


def test_a_suggestion_without_the_placeholder_is_refused():
    evidence = GoalSuggestionEvidence(
        domain_key="fine_motor", evidence_source=EvidenceSource.EXPLICIT_SELECTION,
        explicitly_selected=True)
    with pytest.raises(GoalError):
        GoalSuggestion.create("chld_x", CYCLE, "Help Maya scribble.", evidence,
                              priority_rank=1, emphasis_weight=3,
                              generator_version="v")


def test_evidence_with_no_support_is_refused():
    with pytest.raises(GoalError):
        GoalSuggestionEvidence(domain_key="fine_motor",
                               evidence_source=EvidenceSource.FUNCTIONAL_BASELINE)


def test_explanations_name_rules_not_values():
    observed = ObservedDomain("fine_motor", True,
                              EvidenceSource.CLINICIAN_OBSERVATION,
                              milestone_refs=(SENTINEL_NOTE,),
                              functional_baseline_area=SENTINEL_CONCERN)
    reasons = explain(observed)
    assert reasons == ("clinician_observation", "functional_baseline_area",
                       "milestone_support")
    for sentinel in ALL_SENTINELS:
        assert all(sentinel not in reason for reason in reasons)


def test_the_snapshot_refuses_a_duplicate_domain(s):
    with pytest.raises(SuggestionEngineError):
        ObservationSnapshot(s.child, CYCLE, (
            ObservedDomain("fine_motor", True, EvidenceSource.EXPLICIT_SELECTION,
                           explicitly_selected=True),
            ObservedDomain("fine_motor", True, EvidenceSource.EXPLICIT_SELECTION,
                           explicitly_selected=True)))


def test_suggestions_record_the_rule_and_generator_versions(s):
    offered = generate_suggestions(snapshot(s.child))
    assert offered[0].generator_version == GENERATOR_VERSION
    assert offered[0].evidence.rule_version == SUGGESTION_RULE_VERSION
    assert offered[0].generation_mode == "deterministic"


# ===========================================================================
# planning policy — versioned defaults, not invariants
# ===========================================================================

def test_the_default_offer_is_two_but_nothing_caps_the_count():
    assert CURRENT_PLANNING_POLICY.default_goal_count == 2
    assert CURRENT_PLANNING_POLICY.emphasis_by_rank == (3, 2)
    assert CURRENT_PLANNING_POLICY.min_coverage_per_cycle == 1
    # A fourth goal is weighted, not silently dropped or zeroed.
    assert CURRENT_PLANNING_POLICY.emphasis_for_rank(4) == 1


def test_weights_are_relative_and_do_not_sum_to_a_percentage():
    weights = CURRENT_PLANNING_POLICY.emphasis_by_rank
    assert sum(weights) != 100
    assert weights[0] / weights[1] == 1.5


def test_an_alternative_policy_needs_no_schema_change():
    """Three equally-weighted goals is a product decision, not a violation."""
    alternative = PlanningPolicyVersion(
        policy_version="planning-policy-test", default_goal_count=3,
        emphasis_by_rank=(2, 2, 2), default_emphasis_beyond_ranks=2,
        min_coverage_per_cycle=2, domain_tie_break_order=CANONICAL_DOMAIN_KEYS)
    assert alternative.emphasis_for_rank(3) == 2


def test_an_unknown_policy_version_is_refused_not_substituted():
    assert policy_for(POLICY_2026_10.policy_version) is POLICY_2026_10
    assert known_policy_versions() == (POLICY_2026_10.policy_version,)
    with pytest.raises(PlanningPolicyError):
        policy_for("planning-policy-1999.01")


@pytest.mark.parametrize("kwargs", [
    {"policy_version": ""},
    {"default_goal_count": 0},
    {"emphasis_by_rank": ()},
    {"emphasis_by_rank": (3, 0)},
    {"default_emphasis_beyond_ranks": 0},
    {"min_coverage_per_cycle": -1},
])
def test_malformed_policies_are_refused(kwargs):
    base = dict(policy_version="p", default_goal_count=2,
                emphasis_by_rank=(3, 2), default_emphasis_beyond_ranks=1,
                min_coverage_per_cycle=1,
                domain_tie_break_order=CANONICAL_DOMAIN_KEYS)
    base.update(kwargs)
    with pytest.raises(PlanningPolicyError):
        PlanningPolicyVersion(**base)


# ===========================================================================
# goal types — two types, not one type with a flag
# ===========================================================================

def test_only_a_clinical_goal_is_rtm_eligible():
    clinical = GoalRef(GoalKind.CLINICAL, "clgl_1")
    caregiver = GoalRef(GoalKind.CAREGIVER_APPROVED, "cagl_1")
    assert clinical.is_rtm_eligible and not caregiver.is_rtm_eligible
    assert require_clinical_goal_ref(clinical) is clinical
    with pytest.raises(GoalError):
        require_clinical_goal_ref(caregiver)


def test_the_two_goal_types_are_distinct_classes_in_distinct_collections():
    assert not issubclass(ClinicalGoal, CaregiverApprovedGoal)
    assert not issubclass(CaregiverApprovedGoal, ClinicalGoal)
    assert COLLECTIONS["clinical_goal"] != COLLECTIONS["caregiver_goal"]


@pytest.mark.parametrize("edit_type", [
    EditType.MODIFIED, EditType.REPLACED, EditType.AUTHORED_FRESH])
def test_changing_approved_wording_requires_a_reason(edit_type):
    with pytest.raises(GoalError):
        GoalVersion.create(GoalRef(GoalKind.CLINICAL, "clgl_1"), 1, "text",
                           edit_type, actor_id="prov_1",
                           actor_role=ActorRole.PROVIDER)


def test_accepting_verbatim_requires_the_suggestion_it_accepted():
    with pytest.raises(GoalError):
        GoalVersion.create(GoalRef(GoalKind.CLINICAL, "clgl_1"), 1, "text",
                           EditType.ACCEPTED_VERBATIM, actor_id="prov_1",
                           actor_role=ActorRole.PROVIDER)


def test_a_goal_version_is_frozen():
    version = GoalVersion.create(
        GoalRef(GoalKind.CLINICAL, "clgl_1"), 1, "text",
        EditType.ACCEPTED_VERBATIM, actor_id="prov_1",
        actor_role=ActorRole.PROVIDER, derived_from_suggestion_id="gsug_1")
    with pytest.raises(Exception):
        version.text = "rewritten"  # type: ignore[misc]


def test_a_retired_goal_is_inactive_even_without_a_closed_timestamp():
    """The 0.4A mutation lesson, applied to goals.

    `with_status` always sets `closed_at` alongside RETIRED, so a status-only
    check looks redundant against records this code wrote. A stored document
    can carry a terminal status with a null timestamp, and then it is the only
    thing standing between a retired goal and being treated as active.
    """
    goal = ClinicalGoal.create("chld_x", "prov_1", "prac_1",
                               managing_assignment_id="mcas_1",
                               current_version_id="gver_1")
    assert goal.is_active
    assert not ClinicalGoal(**{**goal.__dict__, "status": GoalStatus.RETIRED,
                               "closed_at": None}).is_active
    assert not ClinicalGoal(**{**goal.__dict__,
                               "status": GoalStatus.PAUSED}).is_active


# ===========================================================================
# approval — Genex suggests, a human approves
# ===========================================================================

def test_generating_suggestions_creates_no_goal(s):
    s.make_managing_clinician()
    offered = s.goals.generate_suggestions(s.provider_alpha, s.child,
                                           snapshot(s.child))
    assert len(offered) == 2
    assert s.goals.list_goals(s.provider_alpha, s.child) == ()
    assert all(x.status is SuggestionStatus.OFFERED for x in offered)


def test_accepting_verbatim_stores_the_suggestions_own_template(clinical):
    s, offered, goal = clinical
    version = s.repos.goal_versions.get_by_id(goal.current_version_id)
    assert version.text == offered[0].family_facing_text_template
    assert version.edit_type is EditType.ACCEPTED_VERBATIM
    assert version.derived_from_suggestion_id == offered[0].suggestion_id
    assert version.version_number == 1
    reloaded = s.repos.goal_suggestions.get_by_id(offered[0].suggestion_id)
    assert reloaded.status is SuggestionStatus.ACCEPTED


def test_accepting_verbatim_ignores_text_the_caller_echoed_back(s):
    """Otherwise "accepted verbatim" is a claim the record cannot support."""
    s.make_managing_clinician()
    offered = s.goals.generate_suggestions(s.provider_alpha, s.child,
                                           snapshot(s.child))
    goal = s.goals.approve_clinical_goal(
        s.provider_alpha, s.child, edit_type=EditType.ACCEPTED_VERBATIM,
        suggestion_id=offered[0].suggestion_id, text="something else entirely")
    version = s.repos.goal_versions.get_by_id(goal.current_version_id)
    assert version.text == offered[0].family_facing_text_template


def test_a_suggestion_can_only_be_acted_on_once(clinical):
    s, offered, _ = clinical
    with pytest.raises(GoalConflict):
        s.goals.approve_clinical_goal(
            s.provider_alpha, s.child, edit_type=EditType.ACCEPTED_VERBATIM,
            suggestion_id=offered[0].suggestion_id)
    with pytest.raises(GoalConflict):
        s.goals.decline_suggestion(s.provider_alpha, offered[0].suggestion_id)


def test_declining_keeps_the_row(s):
    s.make_managing_clinician()
    offered = s.goals.generate_suggestions(s.provider_alpha, s.child,
                                           snapshot(s.child))
    declined = s.goals.decline_suggestion(s.provider_alpha,
                                          offered[1].suggestion_id)
    assert declined.status is SuggestionStatus.DECLINED
    assert s.repos.goal_suggestions.get_by_id(offered[1].suggestion_id).status \
        is SuggestionStatus.DECLINED


def test_authoring_fresh_consumes_no_suggestion(s):
    s.make_managing_clinician()
    goal = s.goals.approve_clinical_goal(
        s.provider_alpha, s.child, edit_type=EditType.AUTHORED_FRESH,
        text="Use a picture exchange to request a snack.",
        reason="family priority raised in session")
    version = s.repos.goal_versions.get_by_id(goal.current_version_id)
    assert version.derived_from_suggestion_id is None


def test_authoring_fresh_must_not_name_a_suggestion(s):
    s.make_managing_clinician()
    offered = s.goals.generate_suggestions(s.provider_alpha, s.child,
                                           snapshot(s.child))
    with pytest.raises(GoalValidationError):
        s.goals.approve_clinical_goal(
            s.provider_alpha, s.child, edit_type=EditType.AUTHORED_FRESH,
            suggestion_id=offered[0].suggestion_id, text="t", reason="r")


def test_modifying_a_suggestion_requires_naming_it(s):
    s.make_managing_clinician()
    with pytest.raises(GoalValidationError):
        s.goals.approve_clinical_goal(
            s.provider_alpha, s.child, edit_type=EditType.MODIFIED,
            text="t", reason="r")


def test_a_suggestion_for_another_child_is_refused(s):
    s.make_managing_clinician()
    beta_offer = s.goals.generate_suggestions(
        s.caregiver_beta, s.topo.child_beta.child_id,
        snapshot(s.topo.child_beta.child_id))
    with pytest.raises(GoalValidationError):
        s.goals.approve_clinical_goal(
            s.provider_alpha, s.child, edit_type=EditType.MODIFIED,
            suggestion_id=beta_offer[0].suggestion_id, text="t", reason="r")


def test_a_snapshot_for_another_child_is_refused(s):
    s.make_managing_clinician()
    with pytest.raises(GoalValidationError):
        s.goals.generate_suggestions(
            s.provider_alpha, s.child, snapshot(s.topo.child_beta.child_id))


def test_a_caregiver_goal_is_approved_by_the_caregiver(s):
    offered = s.goals.generate_suggestions(s.caregiver_alpha, s.child,
                                           snapshot(s.child))
    goal = s.goals.approve_caregiver_goal(
        s.caregiver_alpha, s.child, edit_type=EditType.ACCEPTED_VERBATIM,
        suggestion_id=offered[0].suggestion_id)
    assert isinstance(goal, CaregiverApprovedGoal)
    assert not goal.ref.is_rtm_eligible
    assert goal.approved_by_caregiver_id == s.topo.caregiver_alpha.caregiver_id


# ===========================================================================
# revision — append-only wording history
# ===========================================================================

def test_revising_appends_a_version_and_keeps_the_previous_text(clinical):
    s, _, goal = clinical
    original = s.repos.goal_versions.get_by_id(goal.current_version_id)
    version = s.goals.revise_goal(
        s.provider_alpha, goal.ref, "Use two-word requests at mealtimes.",
        reason="narrowed to the routine the family practises")

    chain = s.goals.goal_history(s.provider_alpha, goal.ref)
    assert [v.version_number for v in chain] == [1, 2]
    assert chain[0].text == original.text
    assert version.supersedes_version_id == original.version_id
    assert s.goals.current_text(s.provider_alpha, goal.ref) == \
        "Use two-word requests at mealtimes."


def test_revision_reason_has_no_default(clinical):
    s, _, goal = clinical
    signature = inspect.signature(s.goals.revise_goal)
    assert signature.parameters["reason"].default is inspect.Parameter.empty
    assert signature.parameters["reason"].kind is inspect.Parameter.KEYWORD_ONLY


def test_accepting_verbatim_cannot_be_used_to_revise(clinical):
    s, _, goal = clinical
    with pytest.raises(GoalValidationError):
        s.goals.revise_goal(s.provider_alpha, goal.ref, "t",
                            edit_type=EditType.ACCEPTED_VERBATIM, reason="r")


def test_a_retired_goal_cannot_be_revised(clinical):
    s, _, goal = clinical
    s.goals.set_goal_status(s.provider_alpha, goal.ref, GoalStatus.RETIRED)
    with pytest.raises(GoalConflict):
        s.goals.revise_goal(s.provider_alpha, goal.ref, "t", reason="r")


def test_retiring_keeps_the_goal_and_its_history(clinical):
    s, _, goal = clinical
    s.goals.set_goal_status(s.provider_alpha, goal.ref, GoalStatus.RETIRED)
    assert s.goals.list_goals(s.provider_alpha, s.child) == ()
    assert goal.ref in s.goals.list_goals(s.provider_alpha, s.child,
                                          include_closed=True)
    assert len(s.goals.goal_history(s.provider_alpha, goal.ref)) == 1


# ===========================================================================
# authorization
# ===========================================================================

def test_only_the_managing_clinician_may_author_a_clinical_goal(s):
    """A CONNECTED provider is not automatically the clinical owner.

    Provider-Gamma passes the 0.2 child-access gate here — an unconnected
    provider would be refused before the ownership rule ever ran, which is
    what made an earlier version of this test vacuous.
    """
    s.make_managing_clinician()
    gamma = s.connected_provider_gamma()
    assert s.goals.list_goals(gamma, s.child) == (), "gamma is genuinely authorized"

    with pytest.raises(GoalAuthorizationError):
        s.goals.approve_clinical_goal(
            gamma, s.child, edit_type=EditType.AUTHORED_FRESH,
            text="t", reason="r")


def test_a_connected_non_owner_provider_cannot_revise_or_retire(s):
    s.make_managing_clinician()
    offered = s.goals.generate_suggestions(s.provider_alpha, s.child,
                                           snapshot(s.child))
    goal = s.goals.approve_clinical_goal(
        s.provider_alpha, s.child, edit_type=EditType.ACCEPTED_VERBATIM,
        suggestion_id=offered[0].suggestion_id)
    gamma = s.connected_provider_gamma()

    assert s.goals.get_goal(gamma, goal.ref) == goal, "reads are permitted"
    with pytest.raises(GoalAuthorizationError):
        s.goals.revise_goal(gamma, goal.ref, "t", reason="r")
    with pytest.raises(GoalAuthorizationError):
        s.goals.set_goal_status(gamma, goal.ref, GoalStatus.RETIRED)


def test_a_connected_non_owner_provider_cannot_set_the_month(s):
    s.make_managing_clinician()
    gamma = s.connected_provider_gamma()
    with pytest.raises(GoalAuthorizationError):
        s.plans.create_plan(gamma, s.child, CYCLE, ZONE)


def test_an_unconnected_provider_is_refused_before_the_ownership_rule(s):
    """Provider-Beta belongs to another family entirely."""
    s.make_managing_clinician()
    with pytest.raises(GoalAuthorizationError):
        s.goals.approve_clinical_goal(
            s.provider_beta, s.child, edit_type=EditType.AUTHORED_FRESH,
            text="t", reason="r")


def test_a_provider_without_the_assignment_is_refused(s):
    """No managing clinician at all: a provider still cannot author one."""
    with pytest.raises(GoalConflict):
        s.goals.approve_clinical_goal(
            s.provider_alpha, s.child, edit_type=EditType.AUTHORED_FRESH,
            text="t", reason="r")


def test_a_caregiver_cannot_author_or_edit_a_clinical_goal(clinical):
    s, _, goal = clinical
    with pytest.raises(GoalAuthorizationError):
        s.goals.approve_clinical_goal(
            s.caregiver_alpha, s.child, edit_type=EditType.AUTHORED_FRESH,
            text="t", reason="r")
    with pytest.raises(GoalAuthorizationError):
        s.goals.revise_goal(s.caregiver_alpha, goal.ref, "t", reason="r")
    with pytest.raises(GoalAuthorizationError):
        s.goals.set_goal_status(s.caregiver_alpha, goal.ref, GoalStatus.RETIRED)


def test_a_provider_cannot_edit_a_caregiver_approved_goal(s):
    goal = s.goals.approve_caregiver_goal(
        s.caregiver_alpha, s.child, edit_type=EditType.AUTHORED_FRESH,
        text="Pour water at the sink with one hand steadying the cup.",
        reason="family chose this")
    s.make_managing_clinician()
    with pytest.raises(GoalAuthorizationError):
        s.goals.revise_goal(s.provider_alpha, goal.ref, "t", reason="r")


def test_an_authorized_second_caregiver_cannot_edit_the_approvers_goal(s):
    """Two caregivers, one child, one goal. Only its approver may edit it.

    Caregiver-Gamma holds a real ACTIVE connection to Child-Alpha here, so
    the 0.2 gate passes and the ownership rule is genuinely what refuses. An
    unconnected caregiver would have been stopped a layer earlier.
    """
    goal = s.goals.approve_caregiver_goal(
        s.caregiver_alpha, s.child, edit_type=EditType.AUTHORED_FRESH,
        text="Pour water at the sink.", reason="family chose this")
    gamma = s.connected_caregiver_gamma()

    assert goal.ref in s.goals.list_goals(gamma, s.child), "gamma is authorized"
    with pytest.raises(GoalAuthorizationError):
        s.goals.revise_goal(gamma, goal.ref, "t2", reason="r")
    with pytest.raises(GoalAuthorizationError):
        s.goals.set_goal_status(gamma, goal.ref, GoalStatus.RETIRED)


def test_an_unrelated_caregiver_cannot_edit_a_goal_at_all(s):
    goal = s.goals.approve_caregiver_goal(
        s.caregiver_alpha, s.child, edit_type=EditType.AUTHORED_FRESH,
        text="t", reason="r")
    # Caregiver-Beta has no relationship to Child-Alpha at all.
    with pytest.raises(GoalAuthorizationError):
        s.goals.revise_goal(s.caregiver_beta, goal.ref, "t2", reason="r")


def test_an_unrelated_family_cannot_read_or_write_goals(clinical):
    s, offered, goal = clinical
    for call in (
        lambda: s.goals.list_suggestions(s.caregiver_beta, s.child),
        lambda: s.goals.list_goals(s.caregiver_beta, s.child),
        lambda: s.goals.get_goal(s.caregiver_beta, goal.ref),
        lambda: s.goals.goal_history(s.caregiver_beta, goal.ref),
        lambda: s.goals.decline_suggestion(s.caregiver_beta,
                                           offered[1].suggestion_id),
        lambda: s.goals.generate_suggestions(s.caregiver_beta, s.child,
                                             snapshot(s.child)),
    ):
        with pytest.raises(GoalAuthorizationError):
            call()


def test_an_ended_relationship_revokes_goal_access(clinical):
    s, _, goal = clinical
    s.repos.provider_child.end_connection(
        s.topo.link_alpha_provider.connection_id, status=ConnectionStatus.REVOKED)
    with pytest.raises(GoalAuthorizationError):
        s.goals.revise_goal(s.provider_alpha, goal.ref, "t", reason="r")


def test_no_client_supplied_role_can_reach_the_goal_services(s):
    for fn in (s.goals.approve_clinical_goal, s.goals.approve_caregiver_goal,
               s.goals.revise_goal, s.goals.set_goal_status,
               s.plans.create_plan, s.plans.allocate_goal,
               s.plans.activate_plan, s.plans.reprioritize):
        params = set(inspect.signature(fn).parameters)
        assert not (params & {"role", "actor_role", "uid", "is_admin",
                              "caregiver_id", "provider_id", "actor_id"}), fn.__name__


@pytest.mark.parametrize("service", [GoalService, MonthlyPlanService])
def test_every_public_service_method_requires_a_principal(service):
    for name, member in inspect.getmembers(service, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        params = list(inspect.signature(member).parameters)
        assert params[:2] == ["self", "principal"], (service.__name__, name, params)


# ===========================================================================
# monthly focus plan
# ===========================================================================

def test_the_month_is_derived_from_the_cycle_not_supplied():
    assert month_bounds("2026-10") == (datetime(2026, 10, 1).date(),
                                       datetime(2026, 10, 31).date())
    assert month_bounds("2028-02")[1].day == 29  # leap year, computed


@pytest.mark.parametrize("bad", ["2026-13", "2026-00", "202610", "2026-1", "", "oct"])
def test_a_malformed_cycle_month_is_refused(bad):
    with pytest.raises(MonthlyPlanError):
        validate_cycle_month(bad)


def test_a_timezone_is_required_and_never_defaulted_to_utc():
    """Parent falls back to UTC on a bad zone. Here that would be a bug.

    A silent hour shift moves a day across a month boundary, and the monthly
    layer is the thing that counts days into months.
    """
    assert validate_timezone(" America/New_York ") == "America/New_York"
    for bad in ("", "   ", "Mars/Olympus", "EST5EDT-nope"):
        with pytest.raises(TimezoneError):
            validate_timezone(bad)
    with pytest.raises(TimezoneError):
        MonthlyFocusPlan.create("chld_x", CYCLE, "")


def test_a_plan_starts_as_a_draft_and_carries_no_activities(s):
    plan = s.plans.create_plan(s.caregiver_alpha, s.child, CYCLE, ZONE,
                               monitoring_focus="requesting at mealtimes")
    assert plan.state is MonthlyPlanState.DRAFT
    assert plan.starts_on == "2026-10-01" and plan.ends_on == "2026-10-31"
    assert plan.timezone_of_record == ZONE
    assert not any("activit" in f for f in MonthlyFocusPlan.__dataclass_fields__)


def test_a_draft_plan_is_not_active(s):
    """`is_active` must read the STATE, not merely the absence of a timestamp.

    A mutation sweep caught this: dropping the state check from `is_active`
    left every unit test passing, because a draft and an active plan both
    carry `closed_at = None`. A draft that reads as active makes
    `active_for_cycle` return it, and the advisory uniqueness check in
    `create_plan` then refuses a second plan on the strength of a draft
    nobody activated.
    """
    plan = s.plans.create_plan(s.caregiver_alpha, s.child, CYCLE, ZONE)
    assert not plan.is_active
    assert s.repos.focus_plans.active_for_cycle(s.child, CYCLE) is None


def test_a_closed_plan_is_inactive_even_without_a_closed_timestamp(s):
    """The 0.4A mutation lesson, restated for plans.

    `close()` always sets `state` and `closed_at` together, so a timestamp
    check looks sufficient against records this code wrote. A stored document
    can carry a terminal state with a null timestamp — and then the state
    check is the only thing keeping a finished month out of the active
    lookup.
    """
    plan = s.plans.create_plan(s.caregiver_alpha, s.child, CYCLE, ZONE)
    for terminal in (MonthlyPlanState.CLOSED, MonthlyPlanState.DRAFT):
        assert not replace(plan, state=terminal, closed_at=None).is_active
    assert replace(plan, state=MonthlyPlanState.ACTIVE).is_active


def test_a_child_with_a_managing_clinician_is_planned_by_that_clinician(s):
    s.make_managing_clinician()
    with pytest.raises(GoalAuthorizationError):
        s.plans.create_plan(s.caregiver_alpha, s.child, CYCLE, ZONE)
    assert s.plans.create_plan(s.provider_alpha, s.child, CYCLE, ZONE)


def test_a_child_with_no_clinician_is_planned_by_a_caregiver(s):
    with pytest.raises(GoalAuthorizationError):
        s.plans.create_plan(s.provider_alpha, s.child, CYCLE, ZONE)
    assert s.plans.create_plan(s.caregiver_alpha, s.child, CYCLE, ZONE)


# ===========================================================================
# allocation
# ===========================================================================

def _plan_with_goal(s):
    s.make_managing_clinician()
    offered = s.goals.generate_suggestions(s.provider_alpha, s.child,
                                           snapshot(s.child))
    goals = [s.goals.approve_clinical_goal(
        s.provider_alpha, s.child, edit_type=EditType.ACCEPTED_VERBATIM,
        suggestion_id=x.suggestion_id) for x in offered]
    plan = s.plans.create_plan(s.provider_alpha, s.child, CYCLE, ZONE)
    return plan, goals


def test_emphasis_defaults_come_from_the_plans_recorded_policy(s):
    plan, goals = _plan_with_goal(s)
    primary = s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id,
                                    goals[0].ref, priority_rank=1)
    secondary = s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id,
                                      goals[1].ref, priority_rank=2)
    assert (primary.emphasis_weight, secondary.emphasis_weight) == (3, 2)
    assert primary.min_coverage_per_cycle == 1
    assert plan.policy_version == POLICY_2026_10.policy_version


def test_an_explicit_weight_overrides_the_default(s):
    plan, goals = _plan_with_goal(s)
    allocation = s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id,
                                       goals[0].ref, priority_rank=1,
                                       emphasis_weight=7,
                                       min_coverage_per_cycle=3)
    assert (allocation.emphasis_weight, allocation.min_coverage_per_cycle) == (7, 3)


def test_nothing_caps_the_number_of_allocated_goals(s):
    plan, goals = _plan_with_goal(s)
    third = s.goals.approve_clinical_goal(
        s.provider_alpha, s.child, edit_type=EditType.AUTHORED_FRESH,
        text="A third focus.", reason="clinician judgement")
    for rank, goal in enumerate([goals[0], goals[1], third], start=1):
        s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id, goal.ref,
                              priority_rank=rank)
    allocations = s.plans.list_allocations(s.provider_alpha, plan.focus_plan_id)
    assert [a.emphasis_weight for a in allocations] == [3, 2, 1]


@pytest.mark.parametrize("kwargs", [
    # An explicit weight is supplied so the call reaches the DOMAIN invariant
    # rather than stopping at the policy's own rank check — the point is that
    # the record refuses these whatever policy is in force.
    {"priority_rank": 0, "emphasis_weight": 3},
    {"priority_rank": 1, "emphasis_weight": 0},
    {"priority_rank": 1, "emphasis_weight": -2},
    {"priority_rank": 1, "emphasis_weight": 3, "min_coverage_per_cycle": -1},
    {"priority_rank": 1, "emphasis_weight": 3, "effective_from_cycle": 0},
])
def test_invariants_hold_for_every_policy(s, kwargs):
    plan, goals = _plan_with_goal(s)
    with pytest.raises(MonthlyPlanError):
        s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id,
                              goals[0].ref, **kwargs)


def test_the_policy_also_refuses_a_rank_below_one(s):
    with pytest.raises(PlanningPolicyError):
        CURRENT_PLANNING_POLICY.emphasis_for_rank(0)


def test_a_goal_from_another_child_cannot_be_allocated(s):
    plan, _ = _plan_with_goal(s)
    other = s.goals.approve_caregiver_goal(
        s.caregiver_beta, s.topo.child_beta.child_id,
        edit_type=EditType.AUTHORED_FRESH, text="t", reason="r")
    with pytest.raises(GoalValidationError):
        s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id, other.ref,
                              priority_rank=1)


def test_a_retired_goal_cannot_be_allocated(s):
    plan, goals = _plan_with_goal(s)
    s.goals.set_goal_status(s.provider_alpha, goals[0].ref, GoalStatus.RETIRED)
    with pytest.raises(GoalValidationError):
        s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id,
                              goals[0].ref, priority_rank=1)


def test_the_same_goal_cannot_be_allocated_twice(s):
    plan, goals = _plan_with_goal(s)
    s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id, goals[0].ref,
                          priority_rank=1)
    with pytest.raises(GoalConflict):
        s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id,
                              goals[0].ref, priority_rank=2)


def test_reprioritising_writes_a_successor_and_keeps_the_predecessor(s):
    plan, goals = _plan_with_goal(s)
    first = s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id,
                                  goals[0].ref, priority_rank=1)
    second = s.plans.reprioritize(
        s.provider_alpha, first.allocation_id, priority_rank=2,
        effective_from_cycle=3, reason="family priority shifted mid-month")

    all_rows = s.plans.list_allocations(s.provider_alpha, plan.focus_plan_id,
                                        include_inactive=True)
    assert {r.allocation_id for r in all_rows} == {first.allocation_id,
                                                   second.allocation_id}
    predecessor = s.repos.goal_allocations.get_by_id(first.allocation_id)
    assert predecessor.status is AllocationStatus.SUPERSEDED
    assert predecessor.superseded_by_allocation_id == second.allocation_id
    assert predecessor.priority_rank == 1, "week 2's weighting is still readable"
    assert second.supersedes_allocation_id == first.allocation_id
    assert second.effective_from_cycle == 3
    assert second.emphasis_weight == 2


def test_reprioritise_requires_when_and_why(s):
    signature = inspect.signature(MonthlyPlanService.reprioritize)
    for name in ("effective_from_cycle", "reason"):
        assert signature.parameters[name].default is inspect.Parameter.empty


def test_a_successor_cannot_take_effect_before_its_predecessor(s):
    plan, goals = _plan_with_goal(s)
    first = s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id,
                                  goals[0].ref, priority_rank=1,
                                  effective_from_cycle=3)
    with pytest.raises(GoalValidationError):
        s.plans.reprioritize(s.provider_alpha, first.allocation_id,
                             priority_rank=2, effective_from_cycle=2,
                             reason="r")


def test_superseding_by_hand_is_refused(s):
    plan, goals = _plan_with_goal(s)
    first = s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id,
                                  goals[0].ref, priority_rank=1)
    with pytest.raises(GoalValidationError):
        s.plans.set_allocation_status(s.provider_alpha, first.allocation_id,
                                      AllocationStatus.SUPERSEDED)
    paused = s.plans.set_allocation_status(s.provider_alpha,
                                           first.allocation_id,
                                           AllocationStatus.PAUSED)
    assert paused.status is AllocationStatus.PAUSED


# ===========================================================================
# activation, claims and snapshots
# ===========================================================================

def _activated(s):
    plan, goals = _plan_with_goal(s)
    s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id, goals[0].ref,
                          priority_rank=1)
    s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id, goals[1].ref,
                          priority_rank=2)
    return s.plans.activate_plan(s.provider_alpha, plan.focus_plan_id), goals


def test_activation_wins_a_child_month_claim(s):
    activated, _ = _activated(s)
    digest = key_digest(s.child, CYCLE)
    claim = s.repos.identity_claims.get_by_id(activated.activation_claim_id)
    assert claim.kind is ClaimKind.MONTHLY_FOCUS_PLAN
    assert claim.key_digest == digest
    assert claim.holder_ref == activated.focus_plan_id
    assert activated.state is MonthlyPlanState.ACTIVE


def test_a_second_plan_for_the_same_child_month_is_refused(s):
    activated, _ = _activated(s)
    # The advisory check catches the ordinary case...
    with pytest.raises(GoalConflict):
        s.plans.create_plan(s.provider_alpha, s.child, CYCLE, ZONE)
    # ...and the claim catches a plan drafted before the first activated.
    second = MonthlyFocusPlan.create(s.child, CYCLE, ZONE,
                                     policy_version=POLICY_2026_10.policy_version)
    s.repos.focus_plans.create(second)
    goal = s.goals.approve_clinical_goal(
        s.provider_alpha, s.child, edit_type=EditType.AUTHORED_FRESH,
        text="Another focus.", reason="r")
    s.plans.allocate_goal(s.provider_alpha, second.focus_plan_id, goal.ref,
                          priority_rank=1)
    with pytest.raises(GoalConflict):
        s.plans.activate_plan(s.provider_alpha, second.focus_plan_id)
    assert s.repos.focus_plans.get_by_id(second.focus_plan_id).state \
        is MonthlyPlanState.DRAFT


def test_a_plan_with_no_allocated_goal_cannot_be_activated(s):
    plan, _ = _plan_with_goal(s)
    with pytest.raises(GoalValidationError):
        s.plans.activate_plan(s.provider_alpha, plan.focus_plan_id)


def test_activation_snapshots_the_wording_as_it_then_read(s):
    activated, goals = _activated(s)
    snapshots = s.plans.list_snapshots(s.provider_alpha, activated.focus_plan_id)
    original = [x.goal_text_at_snapshot for x in snapshots]
    assert len(snapshots) == 2
    assert snapshots[0].approved_by_role is ActorRole.PROVIDER

    s.goals.revise_goal(s.provider_alpha, goals[0].ref,
                        "Completely rewritten in November.",
                        reason="new evidence")

    after = [x.goal_text_at_snapshot for x in
             s.plans.list_snapshots(s.provider_alpha, activated.focus_plan_id)]
    assert after == original, "a later edit reached back into October"
    assert s.goals.current_text(s.provider_alpha, goals[0].ref) == \
        "Completely rewritten in November."


def test_a_crash_between_claim_and_activation_is_forward_recoverable(s):
    """Step 1 committed, step 2 did not. A retry completes, it does not strand.

    The port refuses `set` inside a transaction, so activation cannot be one
    atomic write. The boundary is made safe by making recovery idempotent for
    the rightful holder rather than by a cleanup job.
    """
    plan, goals = _plan_with_goal(s)
    s.plans.allocate_goal(s.provider_alpha, plan.focus_plan_id, goals[0].ref,
                          priority_rank=1)

    real_update = s.repos.focus_plans.update
    s.repos.focus_plans.update = lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("process died before the state write"))
    with pytest.raises(RuntimeError):
        s.plans.activate_plan(s.provider_alpha, plan.focus_plan_id)
    s.repos.focus_plans.update = real_update

    assert s.repos.focus_plans.get_by_id(plan.focus_plan_id).state \
        is MonthlyPlanState.DRAFT
    snapshots_after_crash = s.repos.goal_snapshots.list_for_plan(plan.focus_plan_id)
    assert len(snapshots_after_crash) == 1

    recovered = s.plans.activate_plan(s.provider_alpha, plan.focus_plan_id)
    assert recovered.state is MonthlyPlanState.ACTIVE
    assert len(s.repos.goal_snapshots.list_for_plan(plan.focus_plan_id)) == 1, \
        "recovery duplicated the snapshots"


def test_a_failed_activation_persists_no_snapshot(s):
    """The transaction is all-or-nothing: claim and snapshots commit together."""
    activated, _ = _activated(s)
    second = MonthlyFocusPlan.create(s.child, CYCLE, ZONE,
                                     policy_version=POLICY_2026_10.policy_version)
    s.repos.focus_plans.create(second)
    goal = s.goals.approve_clinical_goal(
        s.provider_alpha, s.child, edit_type=EditType.AUTHORED_FRESH,
        text="Another focus.", reason="r")
    s.plans.allocate_goal(s.provider_alpha, second.focus_plan_id, goal.ref,
                          priority_rank=1)
    with pytest.raises(GoalConflict):
        s.plans.activate_plan(s.provider_alpha, second.focus_plan_id)
    assert s.repos.goal_snapshots.list_for_plan(second.focus_plan_id) == []


def test_a_closed_plan_cannot_be_changed(s):
    activated, goals = _activated(s)
    closed = s.plans.close_plan(s.provider_alpha, activated.focus_plan_id)
    assert closed.state is MonthlyPlanState.CLOSED

    third = s.goals.approve_clinical_goal(
        s.provider_alpha, s.child, edit_type=EditType.AUTHORED_FRESH,
        text="Too late.", reason="r")
    with pytest.raises(GoalConflict):
        s.plans.allocate_goal(s.provider_alpha, activated.focus_plan_id,
                              third.ref, priority_rank=3)
    # The record survives closure intact.
    assert len(s.plans.list_snapshots(s.provider_alpha,
                                      activated.focus_plan_id)) == 2


def test_closing_does_not_release_the_month(s):
    """October cannot be reopened and rewritten after it ended."""
    activated, _ = _activated(s)
    s.plans.close_plan(s.provider_alpha, activated.focus_plan_id)
    digest = key_digest(s.child, CYCLE)
    assert s.repos.identity_claims.next_generation(
        ClaimKind.MONTHLY_FOCUS_PLAN, digest) == 0


def test_a_different_month_is_a_different_claim(s):
    _activated(s)
    november = s.plans.create_plan(s.provider_alpha, s.child, "2026-11", ZONE)
    goal = s.goals.approve_clinical_goal(
        s.provider_alpha, s.child, edit_type=EditType.AUTHORED_FRESH,
        text="November focus.", reason="r")
    s.plans.allocate_goal(s.provider_alpha, november.focus_plan_id, goal.ref,
                          priority_rank=1)
    assert s.plans.activate_plan(
        s.provider_alpha, november.focus_plan_id).state is MonthlyPlanState.ACTIVE


def test_active_plan_lookup_fails_closed_on_two_active_plans(s):
    from pilot_backend.repository.interface import AmbiguousRecordState

    activated, _ = _activated(s)
    smuggled = MonthlyFocusPlan.create(
        s.child, CYCLE, ZONE, policy_version=POLICY_2026_10.policy_version)
    s.repos.focus_plans.create(smuggled.activate(claim_id="forged"))
    with pytest.raises(AmbiguousRecordState):
        s.repos.focus_plans.active_for_cycle(s.child, CYCLE)


# ===========================================================================
# audit
# ===========================================================================

def test_material_actions_emit_audit_events(s):
    activated, goals = _activated(s)
    s.goals.revise_goal(s.provider_alpha, goals[0].ref, "revised", reason="r")
    s.plans.close_plan(s.provider_alpha, activated.focus_plan_id)

    actions = {e.action for e in s.repos.audit_events.list_all()}
    assert {AuditAction.GOAL_SUGGESTIONS_GENERATED,
            AuditAction.CLINICAL_GOAL_APPROVED,
            AuditAction.GOAL_VERSION_ADDED,
            AuditAction.MONTHLY_PLAN_CREATED,
            AuditAction.GOAL_ALLOCATED,
            AuditAction.MONTHLY_PLAN_ACTIVATED,
            AuditAction.MONTHLY_PLAN_CLOSED} <= actions


def test_a_refused_activation_is_audited_as_a_failure(s):
    _activated(s)
    second = MonthlyFocusPlan.create(s.child, CYCLE, ZONE,
                                     policy_version=POLICY_2026_10.policy_version)
    s.repos.focus_plans.create(second)
    goal = s.goals.approve_clinical_goal(
        s.provider_alpha, s.child, edit_type=EditType.AUTHORED_FRESH,
        text="t", reason="r")
    s.plans.allocate_goal(s.provider_alpha, second.focus_plan_id, goal.ref,
                          priority_rank=1)
    with pytest.raises(GoalConflict):
        s.plans.activate_plan(s.provider_alpha, second.focus_plan_id)

    failures = [e for e in s.repos.audit_events.list_all()
                if e.result is AuditResult.FAILURE
                and e.action is AuditAction.MONTHLY_PLAN_ACTIVATED]
    assert len(failures) == 1


def test_the_audit_trail_never_records_goal_text_or_a_domain(s):
    """It records THAT a goal was approved and by whom, never what it said."""
    activated, goals = _activated(s)
    s.goals.revise_goal(s.provider_alpha, goals[0].ref, SENTINEL_NOTE,
                        reason=SENTINEL_CONCERN)

    for event in s.repos.audit_events.list_all():
        blob = " ".join([event.resource_type, event.resource_id or "",
                         *event.metadata.keys(), *event.metadata.values()])
        for sentinel in ALL_SENTINELS:
            assert sentinel not in blob, event.action
        assert "domain_key" not in event.metadata
        for template in DOMAIN_TEMPLATES.values():
            assert template not in blob


def test_no_goal_metadata_key_escapes_the_allowlist(s):
    activated, _ = _activated(s)
    for event in s.repos.audit_events.list_all():
        assert set(event.metadata) <= ALLOWED_METADATA_KEYS


def test_goal_errors_are_phi_safe_by_declaration():
    for error in (GoalConflict, GoalAuthorizationError, GoalValidationError,
                  GoalError, MonthlyPlanError, TimezoneError,
                  PlanningPolicyError, SuggestionEngineError,
                  UnknownDomainError):
        assert getattr(error, "PHI_SAFE_MESSAGE", False), error.__name__


# ===========================================================================
# persistence
# ===========================================================================

def test_every_new_record_round_trips_through_the_codecs(s):
    activated, goals = _activated(s)
    records = [
        *s.repos.goal_suggestions.list_for_child(s.child),
        *s.repos.goal_versions.list_chain(goals[0].ref.goal_id),
        *s.repos.clinical_goals.list_for_child(s.child),
        activated,
        *s.repos.goal_allocations.list_for_plan(activated.focus_plan_id),
        *s.repos.goal_snapshots.list_for_plan(activated.focus_plan_id),
    ]
    assert len(records) >= 7
    for record in records:
        assert decode(type(record), encode(record)) == record


def test_an_unknown_document_field_is_refused_not_ignored(s):
    plan = MonthlyFocusPlan.create(s.child, CYCLE, ZONE)
    document = dict(encode(plan))
    document["smuggled"] = "x"
    with pytest.raises(Exception):
        decode(MonthlyFocusPlan, document)


def test_nested_evidence_inherits_the_same_strictness(s):
    suggestion = generate_suggestions(snapshot(s.child))[0]
    document = encode(suggestion)
    document["evidence"] = dict(document["evidence"])
    del document["evidence"]["rule_version"]
    with pytest.raises(Exception):
        decode(GoalSuggestion, document)


def test_ordered_string_lists_keep_their_order(s):
    plan = MonthlyFocusPlan.create(s.child, CYCLE, ZONE,
                                   routines_context=("bedtime", "bath", "meals"))
    assert decode(MonthlyFocusPlan, encode(plan)).routines_context == \
        ("bedtime", "bath", "meals")


def test_only_pilot_prefixed_collections_are_used(s):
    _activated(s)
    for name in s.store.collections():
        assert name.startswith(PILOT_COLLECTION_PREFIX), name


#: Each new record type and the field that IS its identity.
OWN_ID_FIELDS = {
    GoalSuggestion: "suggestion_id",
    GoalVersion: "version_id",
    ClinicalGoal: "clinical_goal_id",
    CaregiverApprovedGoal: "caregiver_goal_id",
    MonthlyFocusPlan: "focus_plan_id",
    MonthlyGoalAllocation: "allocation_id",
    MonthlyGoalSnapshot: "snapshot_id",
}


def test_the_sort_tiebreak_uses_each_records_own_id(s):
    """The 0.4B/C form of the BACKEND 0.1 ordering flake.

    `_own_id` picks the tiebreak by scanning a fixed attribute order. Every
    new record type also carries `child_id`, and allocations and snapshots
    also carry `focus_plan_id` — if either matched first, a whole child's
    records would tie on one key and ordering would fall back to whatever the
    store returned. Asserted per type, because a single "the list is stable"
    check passes even when the tiebreak is a foreign key.
    """
    from pilot_backend.persistence.firestore_repos import _own_id

    activated, goals = _activated(s)
    caregiver_goal = s.goals.approve_caregiver_goal(
        s.caregiver_alpha, s.child, edit_type=EditType.AUTHORED_FRESH,
        text="A caregiver goal.", reason="family chose this")
    records = [
        *s.repos.goal_suggestions.list_for_child(s.child),
        *s.repos.goal_versions.list_chain(goals[0].ref.goal_id),
        *s.repos.clinical_goals.list_for_child(s.child),
        caregiver_goal,
        activated,
        *s.repos.goal_allocations.list_for_plan(activated.focus_plan_id),
        *s.repos.goal_snapshots.list_for_plan(activated.focus_plan_id),
    ]
    seen = set()
    for record in records:
        field = OWN_ID_FIELDS[type(record)]
        assert _own_id(record) == getattr(record, field), type(record).__name__
        for foreign in ("child_id", "focus_plan_id", "goal_id"):
            if foreign != field and hasattr(record, foreign):
                assert _own_id(record) != getattr(record, foreign), \
                    (type(record).__name__, foreign)
        seen.add(type(record))
    assert seen == set(OWN_ID_FIELDS), "a new record type is untested here"


def test_listings_are_stable_across_repeated_reads(s):
    activated, _ = _activated(s)
    allocations = s.repos.goal_allocations.list_for_plan(activated.focus_plan_id)
    assert len({a.allocation_id for a in allocations}) == len(allocations)
    assert [a.priority_rank for a in allocations] == [1, 2]
    suggestions = s.repos.goal_suggestions.list_for_child(s.child)
    assert suggestions == s.repos.goal_suggestions.list_for_child(s.child)


@pytest.mark.parametrize("repo_name", [
    "goal_suggestions", "goal_versions", "clinical_goals", "caregiver_goals",
    "focus_plans", "goal_allocations", "goal_snapshots"])
def test_no_goal_repository_exposes_a_delete(repo_name):
    repos = FirestoreRepositories(FakeDocumentStore())
    repo = getattr(repos, repo_name)
    banned = ("delete", "remove", "purge", "drop", "destroy", "erase", "truncate")
    for attribute in dir(repo):
        if attribute.startswith("_"):
            continue
        assert not any(w in attribute.lower() for w in banned), (repo_name, attribute)


def test_goal_modules_never_call_a_delete():
    for relative in ("domain/goals.py", "domain/monthly_plan.py",
                     "domain/planning_policy.py", "domain/goal_vocabulary.py",
                     "goals/service.py", "goals/suggestion_engine.py",
                     "planning/service.py"):
        tree = ast.parse((PILOT_ROOT / relative).read_text())
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not ({"delete", "remove", "purge", "drop"} & called), relative


def test_immutable_records_have_no_update_path():
    repos = FirestoreRepositories(FakeDocumentStore())
    for repo in (repos.goal_versions, repos.goal_snapshots):
        assert not hasattr(repo, "update")
        assert not hasattr(repo, "set")


# ===========================================================================
# scope guard — 0.4B/C only
# ===========================================================================

def test_no_weekly_observation_or_rtm_object_was_implemented():
    banned = {
        "WeeklyCycle", "WeeklyPlan", "WeeklyActivityAllocation",
        "ActivityGoalAlignment", "CoverageGap", "ObservationEvent",
        "RTMEpisode", "RTMMonitoringPeriod", "TherapistReview", "TimeEntry",
        "SynchronousInteraction", "RTMTechnology", "PayerVerification",
        "MonitoringDay", "MonthEndReport", "AdaptationRecord",
        "CodingAssistanceSummary",
    }
    for path in sorted(PILOT_ROOT.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        names = {n.name for n in ast.walk(ast.parse(path.read_text()))
                 if isinstance(n, ast.ClassDef)}
        assert not (banned & names), (path.name, banned & names)


def test_no_billing_or_payer_vocabulary_entered_the_goal_layer():
    """October RTM scope: no payer, member, plan-benefit or claim concept.

    Token-level, so a docstring saying "never billable" cannot trip it and an
    identifier called `reimbursement_amount` cannot hide behind one.
    """
    banned = {"payer", "member_id", "eligibility", "reimbursement",
              "clearinghouse", "copay", "deductible", "cpt_code", "billable"}
    for relative in ("domain/goals.py", "domain/monthly_plan.py",
                     "domain/planning_policy.py", "goals/service.py",
                     "goals/suggestion_engine.py", "planning/service.py"):
        tree = ast.parse((PILOT_ROOT / relative).read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.add(node.name)
        assert not (banned & {n.lower() for n in names}), relative
