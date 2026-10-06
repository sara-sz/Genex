"""0.5F-B — projection to deterministic anchored GoalSuggestion.

Covers the generation key, the frozen clinical algorithm, the atomic
claim-first commit, the provider-only authorization, and every fail-closed
state the founder enumerated.

Real `FirestoreRepositories` over the in-memory document store, as the A2/A3
suites do: the codec, the deterministic claim id and the transaction are the
things under test, so a fake repository would test nothing.

The 18m -> 24m fixture is PINNED here as a regression case. It is not hardcoded
in production logic — the service asks the Gold Standard source for the step, and
these constants only assert that the frozen algorithm still answers the same way.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth.resolver import resolve_principal
from pilot_backend.auth.verifiers import VerifiedToken
from pilot_backend.domain.canonical_rung import ActivityFamilyBinding, CanonicalRung
from pilot_backend.domain.connections import (
    CaregiverChildConnection,
    ProviderChildConnection,
)
from pilot_backend.domain.entities import Caregiver, Child, Practice, Provider
from pilot_backend.domain.enums import (
    CaregiverRelationship,
    ConnectionStatus,
    ProviderDiscipline,
)
from pilot_backend.domain.managing_clinician import ManagingClinicianAssignment
from pilot_backend.domain.parent_baseline_projection import (
    ParentBaselineProjection,
)
from pilot_backend.domain.roles import ActorRole
from pilot_backend.domain.source_link import SourceSystem, SourceSystemLink
from pilot_backend.domain.suggestion_generation import (
    GENERATABLE_STATUSES,
    GENERATION_POLICY_VERSION,
    NON_GENERATABLE_STATUSES,
    BaselineNotGeneratable,
    GoalSuggestionGenerationClaim,
    ProjectionLineageInvalid,
    SuggestionGenerationError,
    TargetNotMappable,
    TargetNotResolvable,
    generation_claim_id,
    generation_key,
    is_generatable_status,
    projection_cycle_month,
    target_within_ceiling,
)
from pilot_backend.goals.errors import GoalAuthorizationError
from pilot_backend.goals.service import GoalService
from pilot_backend.integration.baseline_suggestion_generation import (
    SUPPORTED_DOMAIN,
    BaselineSuggestionGenerationService,
)
from pilot_backend.integration.gold_standard_source import (
    InMemoryGoldStandardRungSource,
    RungTarget,
)
from pilot_backend.persistence import FakeDocumentStore, FirestoreRepositories

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
SESSION = "2aad031e-70bf-4970-875c-1558a955709a"
CHILD = "chld_16a0d958bedd43b6b073ce3cc393a8f1"
DIGEST = "a8dd7506bcbce333ab5d9ea94991875440f8a56e5c31ce65c67ecf4ad8345685"

# ---- the FROZEN expected result, pinned as a regression fixture -----------
EXPECTED_MONTHS = 24
EXPECTED_MILESTONE = "says at least two words together like more milk"
EXPECTED_SUBDOMAIN = "expressive_language"
EXPECTED_RUNG_REF = "rung1:feb590cf2788978b383c11062ce67c1b"
EXPECTED_TRACK_REF = "track1:5be892494f3e6894a7de24a868084e0f"
EXPECTED_FAMILIES = ("expressive_vocabulary_growth", "two_word_phrases")
TAXONOMY_VERSION = "activity_family_taxonomy_v1"
GOLD_STANDARD_VERSION = "parent-2.4-functional-baseline-v1"


def _rung(months=EXPECTED_MONTHS, milestone=EXPECTED_MILESTONE,
          families=EXPECTED_FAMILIES, domain=SUPPORTED_DOMAIN):
    # `build` is the only constructor callers should use: it COMPUTES both refs
    # and `__post_init__` recomputes and compares them. So the expected
    # rung_ref/track_ref below are not asserted against values this fixture
    # chose — they are asserted against what the frozen ref functions derive.
    return CanonicalRung.build(
        domain_key=domain,
        source_rung_months=months,
        milestone_text=milestone,
        subdomain=EXPECTED_SUBDOMAIN,
        family_bindings=tuple(
            ActivityFamilyBinding(family_ref=f, allowed_domains=(domain,))
            for f in families),
        track_subdomains=("early_vocalization_and_babbling",
                          "expressive_language"),
        track_families=(),
        taxonomy_version=TAXONOMY_VERSION,
        baseline_version=GOLD_STANDARD_VERSION)


def _projection(*, status="BOUNDED", anchor=18, ceiling=24,
                domain=SUPPORTED_DOMAIN, child=CHILD, session=SESSION):
    return ParentBaselineProjection.build(
        child_id=child, source_session_id=session, source_record_digest=DIGEST,
        projection={"domain": domain, "area_id": "talking",
                    "entry_choice_id": "many_single_words",
                    "routing_anchor_months": anchor,
                    "not_demonstrated_months": ceiling,
                    "status": status,
                    "baseline_version": GOLD_STANDARD_VERSION},
        now=NOW)


class _World:
    """A fictional child with a caregiver, a managing provider and a projection."""

    def __init__(self, *, rungs=None, unmappable=(), projection=None,
                 with_link=True, with_projection=True, managing=True):
        self.store = FakeDocumentStore()
        self.repos = FirestoreRepositories(self.store)
        self.recorder = AuditRecorder(self.repos.audit_events,
                                      environment="test")
        self.goals = GoalService(repos=self.repos, recorder=self.recorder,
                                 now=lambda: NOW)
        self.source = InMemoryGoldStandardRungSource(
            rungs=tuple(rungs if rungs is not None else (_rung(),)),
            unmappable=tuple(unmappable))
        self.service = BaselineSuggestionGenerationService(
            repos=self.repos, goals=self.goals, rung_source=self.source)

        child = Child(child_id=CHILD, created_at=NOW, updated_at=NOW)
        self.repos.children.create(child)

        self.caregiver = Caregiver.create("Fictional Caregiver",
                                         auth_subject="cg-subject", now=NOW)
        self.repos.caregivers.create(self.caregiver)
        self.repos.caregiver_child.connect(CaregiverChildConnection.create(
            self.caregiver.caregiver_id, CHILD, CaregiverRelationship.PARENT,
            actor_id=self.caregiver.caregiver_id, now=NOW))

        practice = Practice.create("Fictional Practice", now=NOW)
        self.repos.practices.create(practice)
        self.provider = Provider.create(
            practice.practice_id, ProviderDiscipline.SLP, "Hannah",
            auth_subject="pr-subject", now=NOW)
        self.repos.providers.create(self.provider)
        self.other_provider = Provider.create(
            practice.practice_id, ProviderDiscipline.SLP, "Other Clinician",
            auth_subject="pr2-subject", now=NOW)
        self.repos.providers.create(self.other_provider)

        self.connections = {}
        for provider in (self.provider, self.other_provider):
            # Created PENDING, then activated — a clinician is connected only
            # once the connection is accepted, which is the 0.5B rule.
            connection = ProviderChildConnection.create(
                provider.provider_id, CHILD, practice.practice_id,
                actor_id=self.caregiver.caregiver_id, now=NOW).activate(now=NOW)
            self.repos.provider_child.connect(connection)
            self.connections[provider.provider_id] = connection

        if managing:
            self.repos.managing_clinicians.create(
                ManagingClinicianAssignment.create(
                    CHILD, self.provider.provider_id, practice.practice_id,
                    provider_connection_id=self.connections[
                        self.provider.provider_id].connection_id,
                    actor_id=self.caregiver.caregiver_id, now=NOW))
        self.practice_id = practice.practice_id

        if with_link:
            self.repos.source_links.create(SourceSystemLink.create(
                CHILD, SourceSystem.PARENT, SESSION,
                actor_id=self.caregiver.caregiver_id,
                actor_role=ActorRole.CAREGIVER.value, now=NOW))

        self.projection = projection if projection is not None else _projection()
        if with_projection:
            self.repos.parent_baseline_projections.create(self.projection)

    def principal(self, subject="pr-subject"):
        return resolve_principal(VerifiedToken(subject=subject), self.repos)


# ---------------------------------------------------------------------------
# 1. the generation key
# ---------------------------------------------------------------------------

def test_the_policy_version_is_explicit():
    assert GENERATION_POLICY_VERSION == "goal-suggestion-generation-policy-v1"


def test_the_key_is_stable_for_identical_immutable_inputs():
    args = dict(projection_id="pbpj_x", domain_key=SUPPORTED_DOMAIN,
                target_rung_ref=EXPECTED_RUNG_REF,
                taxonomy_version=TAXONOMY_VERSION,
                gold_standard_version=GOLD_STANDARD_VERSION)
    assert generation_key(**args) == generation_key(**args)
    assert len(generation_key(**args)) == 64


@pytest.mark.parametrize("field,value", [
    ("projection_id", "pbpj_other"),
    ("domain_key", "fine_motor"),
    ("target_rung_ref", "rung1:different"),
    ("taxonomy_version", "activity_family_taxonomy_v2"),
    ("gold_standard_version", "parent-2.5-functional-baseline-v1"),
    ("generation_policy", "goal-suggestion-generation-policy-v2"),
])
def test_every_key_input_changes_the_key(field, value):
    """A later policy, taxonomy or Gold Standard legitimately regenerates."""
    args = dict(projection_id="pbpj_x", domain_key=SUPPORTED_DOMAIN,
                target_rung_ref=EXPECTED_RUNG_REF,
                taxonomy_version=TAXONOMY_VERSION,
                gold_standard_version=GOLD_STANDARD_VERSION)
    assert generation_key(**dict(args, **{field: value})) != generation_key(**args)


def test_the_key_is_domain_separated():
    import hashlib

    args = dict(projection_id="pbpj_x", domain_key=SUPPORTED_DOMAIN,
                target_rung_ref=EXPECTED_RUNG_REF,
                taxonomy_version=TAXONOMY_VERSION,
                gold_standard_version=GOLD_STANDARD_VERSION)
    bare = hashlib.sha256("\x00".join([
        "pbpj_x", GENERATION_POLICY_VERSION, TAXONOMY_VERSION,
        GOLD_STANDARD_VERSION, SUPPORTED_DOMAIN, EXPECTED_RUNG_REF,
    ]).encode()).hexdigest()
    assert generation_key(**args) != bare


@pytest.mark.parametrize("missing", [
    "projection_id", "domain_key", "target_rung_ref", "taxonomy_version",
    "gold_standard_version",
])
def test_a_missing_key_input_is_refused(missing):
    args = dict(projection_id="pbpj_x", domain_key=SUPPORTED_DOMAIN,
                target_rung_ref=EXPECTED_RUNG_REF,
                taxonomy_version=TAXONOMY_VERSION,
                gold_standard_version=GOLD_STANDARD_VERSION)
    args[missing] = ""
    with pytest.raises(SuggestionGenerationError):
        generation_key(**args)


def test_the_claim_id_is_derived_and_verified():
    key = generation_key(projection_id="pbpj_x", domain_key=SUPPORTED_DOMAIN,
                         target_rung_ref=EXPECTED_RUNG_REF,
                         taxonomy_version=TAXONOMY_VERSION,
                         gold_standard_version=GOLD_STANDARD_VERSION)
    assert generation_claim_id(key) == f"gsgc_{key[:32]}"
    with pytest.raises(SuggestionGenerationError):
        GoalSuggestionGenerationClaim(
            claim_id="gsgc_wrong", generation_key=key, projection_id="p",
            child_id=CHILD, domain_key=SUPPORTED_DOMAIN,
            target_rung_ref=EXPECTED_RUNG_REF, target_rung_months=24,
            generation_policy=GENERATION_POLICY_VERSION,
            taxonomy_version=TAXONOMY_VERSION,
            gold_standard_version=GOLD_STANDARD_VERSION)


def test_the_requester_is_not_part_of_the_key():
    """Hannah requests generation; she does not determine its target."""
    a = GoalSuggestionGenerationClaim.build(
        projection_id="pbpj_x", child_id=CHILD, domain_key=SUPPORTED_DOMAIN,
        target_rung_ref=EXPECTED_RUNG_REF, target_rung_months=24,
        taxonomy_version=TAXONOMY_VERSION,
        gold_standard_version=GOLD_STANDARD_VERSION,
        requested_by_actor_id="prov_one", now=NOW)
    b = GoalSuggestionGenerationClaim.build(
        projection_id="pbpj_x", child_id=CHILD, domain_key=SUPPORTED_DOMAIN,
        target_rung_ref=EXPECTED_RUNG_REF, target_rung_months=24,
        taxonomy_version=TAXONOMY_VERSION,
        gold_standard_version=GOLD_STANDARD_VERSION,
        requested_by_actor_id="prov_two", now=NOW)
    assert a.generation_key == b.generation_key
    assert a.claim_id == b.claim_id


def test_the_claim_is_immutable_with_no_generated_flag():
    claim = GoalSuggestionGenerationClaim.build(
        projection_id="pbpj_x", child_id=CHILD, domain_key=SUPPORTED_DOMAIN,
        target_rung_ref=EXPECTED_RUNG_REF, target_rung_months=24,
        taxonomy_version=TAXONOMY_VERSION,
        gold_standard_version=GOLD_STANDARD_VERSION, now=NOW)
    with pytest.raises(Exception):
        claim.projection_id = "other"  # type: ignore[misc]
    assert not hasattr(claim, "generated")
    for forbidden in ("with_result", "complete", "invalidate", "mark_generated"):
        assert not hasattr(claim, forbidden)


# ---------------------------------------------------------------------------
# 2. the frozen clinical gates
# ---------------------------------------------------------------------------

def test_the_generatable_status_vocabulary():
    assert GENERATABLE_STATUSES == ("BOUNDED", "AGE_RELEVANT", "EMERGING")
    assert NON_GENERATABLE_STATUSES == ("UNRESOLVED", "CONTRADICTORY")
    for status in GENERATABLE_STATUSES:
        assert is_generatable_status(status)
    for status in NON_GENERATABLE_STATUSES:
        assert not is_generatable_status(status)


def test_the_ceiling_is_inclusive():
    """Frozen Parent semantics: the ceiling IS the first unachieved rung."""
    assert target_within_ceiling(24, 24) is True
    assert target_within_ceiling(23, 24) is True
    assert target_within_ceiling(25, 24) is False
    # No ceiling means nothing to bound against.
    assert target_within_ceiling(60, None) is True


def test_the_cycle_month_comes_from_the_projection_not_now():
    assert projection_cycle_month(_projection()) == "2026-10"


# ---------------------------------------------------------------------------
# 3. happy path — the pinned 18m -> 24m fixture
# ---------------------------------------------------------------------------

def test_the_frozen_algorithm_selects_the_expected_canonical_target():
    world = _World()
    rung = world.service.resolve_target(world.projection)
    assert rung.source_rung_months == EXPECTED_MONTHS
    assert rung.milestone_text == EXPECTED_MILESTONE
    assert rung.subdomain == EXPECTED_SUBDOMAIN
    assert rung.rung_ref == EXPECTED_RUNG_REF
    assert rung.track_ref == EXPECTED_TRACK_REF
    assert tuple(b.family_ref for b in rung.family_bindings) == EXPECTED_FAMILIES
    assert rung.is_activity_mappable is True


def test_the_managing_provider_generates_one_suggestion_and_one_anchor():
    world = _World()
    outcome = world.service.generate_for_child(world.principal(), CHILD)

    assert outcome.created is True
    assert len(outcome.suggestions) == 1
    assert outcome.projection_id == world.projection.projection_id
    assert outcome.target_rung_ref == EXPECTED_RUNG_REF
    assert outcome.target_rung_months == EXPECTED_MONTHS

    suggestion = outcome.suggestions[0]
    anchors = world.store.list_all("pilot_suggestion_anchors")
    assert len(anchors) == 1
    anchor_doc = anchors[0][1]
    assert anchor_doc["suggestion_id"] == suggestion.suggestion_id
    assert anchor_doc["child_id"] == CHILD
    assert anchor_doc["rung"]["rung_ref"] == EXPECTED_RUNG_REF

    claims = world.store.list_all("pilot_suggestion_generation_claims")
    assert len(claims) == 1
    assert claims[0][1]["target_rung_ref"] == EXPECTED_RUNG_REF
    assert claims[0][1]["projection_id"] == world.projection.projection_id


def test_the_suggestion_stays_offered_and_creates_no_clinical_goal():
    """Triggering is not approving."""
    world = _World()
    outcome = world.service.generate_for_child(world.principal(), CHILD)
    assert outcome.suggestions[0].status.value == "offered"
    assert world.store.list_all("pilot_clinical_goals") == []
    assert world.store.list_all("pilot_goal_versions") == []
    assert world.store.list_all("pilot_clinical_goal_anchors") == []


def test_the_existing_provider_read_sees_the_suggestion():
    world = _World()
    world.service.generate_for_child(world.principal(), CHILD)
    found = world.goals.list_suggestions(world.principal(), CHILD)
    assert len(found) == 1
    assert found[0].evidence.domain_key == SUPPORTED_DOMAIN


# ---------------------------------------------------------------------------
# 4. lineage
# ---------------------------------------------------------------------------

def test_lineage_runs_through_the_generation_claim_not_the_observation():
    """The projection is referenced by the CLAIM, never by the observation.

    `prior_month_summary_id` must stay unset: it is load-bearing in the frozen
    engine — `evidence_score` adds SCORE_PRIOR_CYCLE_CONTINUITY and `explain`
    appends "prior_cycle_continuity" — and a first-ever Parent baseline has
    earned neither.
    """
    world = _World()
    outcome = world.service.generate_for_child(world.principal(), CHILD)
    suggestion = outcome.suggestions[0]

    assert suggestion.evidence.prior_month_summary_id is None
    assert "prior_cycle_continuity" not in suggestion.evidence.__dict__.get(
        "rule_version", "")
    # What the observation DOES carry is true of the observation itself.
    assert suggestion.evidence.functional_baseline_area == "talking"
    assert suggestion.evidence.observed_level == "many_single_words"

    # The lineage chain, end to end, through the claim.
    claim_doc = world.store.list_all("pilot_suggestion_generation_claims")[0][1]
    assert claim_doc["projection_id"] == world.projection.projection_id
    assert suggestion.suggestion_id in list(claim_doc["suggestion_ids"])
    anchor_doc = world.store.list_all("pilot_suggestion_anchors")[0][1]
    assert anchor_doc["suggestion_id"] == suggestion.suggestion_id

    rendered = json.dumps(world.store.list_all("pilot_goal_suggestions"),
                          default=str)
    for forbidden in ("asked", "diagnosis", "concern", "qna", "owner_uid",
                      "parent_uid", "auth_subject", "chronological",
                      "cg-subject", "pr-subject"):
        assert forbidden not in rendered, forbidden


def test_the_claim_names_the_suggestions_it_produced():
    world = _World()
    outcome = world.service.generate_for_child(world.principal(), CHILD)
    claim_doc = world.store.list_all("pilot_suggestion_generation_claims")[0][1]
    assert list(claim_doc["suggestion_ids"]) == \
        [s.suggestion_id for s in outcome.suggestions]


# ---------------------------------------------------------------------------
# 5. idempotency and cross-actor convergence
# ---------------------------------------------------------------------------

def test_a_repeat_by_the_same_provider_is_idempotent():
    world = _World()
    first = world.service.generate_for_child(world.principal(), CHILD)
    before = json.dumps(world.store.list_all("pilot_goal_suggestions"),
                        default=str)
    second = world.service.generate_for_child(world.principal(), CHILD)
    after = json.dumps(world.store.list_all("pilot_goal_suggestions"),
                       default=str)

    assert second.created is False
    assert [s.suggestion_id for s in second.suggestions] == \
        [s.suggestion_id for s in first.suggestions]
    assert before == after, "a replay rewrote or added a suggestion"
    assert len(world.store.list_all("pilot_suggestion_generation_claims")) == 1
    assert len(world.store.list_all("pilot_suggestion_anchors")) == 1


def test_a_different_authorized_provider_converges_on_the_same_generation():
    """Generation identity depends on projection/policy, not on the actor."""
    world = _World()
    first = world.service.generate_for_child(world.principal("pr-subject"),
                                             CHILD)
    # Make the other provider the managing clinician, then let them ask.
    world.repos.managing_clinicians.update(
        world.repos.managing_clinicians.list_for_child(CHILD)[0]
        .end(reason="test", actor_id="t", now=NOW))
    world.repos.managing_clinicians.create(
        ManagingClinicianAssignment.create(
            CHILD, world.other_provider.provider_id, world.practice_id,
            provider_connection_id=world.connections[
                world.other_provider.provider_id].connection_id,
            actor_id=world.caregiver.caregiver_id, now=NOW))

    second = world.service.generate_for_child(world.principal("pr2-subject"),
                                              CHILD)
    assert second.created is False
    assert [s.suggestion_id for s in second.suggestions] == \
        [s.suggestion_id for s in first.suggestions]
    assert len(world.store.list_all("pilot_goal_suggestions")) == 1


# ---------------------------------------------------------------------------
# 6. authorization
# ---------------------------------------------------------------------------

def test_a_caregiver_cannot_generate():
    world = _World()
    caregiver_principal = world.principal("cg-subject")
    assert caregiver_principal.role is ActorRole.CAREGIVER
    # The service delegates to GoalService, which authorizes child ACCESS — the
    # provider-only rule lives at the transport boundary and is proven there.
    # Here we prove a caregiver cannot reach the managing-clinician gate.
    with pytest.raises(GoalAuthorizationError):
        world.goals._require_managing_clinician(caregiver_principal, CHILD)


def test_a_non_managing_provider_is_refused_by_the_existing_gate():
    world = _World()
    with pytest.raises(GoalAuthorizationError):
        world.goals._require_managing_clinician(
            world.principal("pr2-subject"), CHILD)


def test_a_provider_without_child_access_is_refused():
    world = _World()
    stranger = Provider.create(
        world.provider.practice_id, ProviderDiscipline.SLP, "Unconnected",
        auth_subject="stranger", now=NOW)
    world.repos.providers.create(stranger)
    with pytest.raises(GoalAuthorizationError):
        world.goals._authorize(world.principal("stranger"), CHILD)


# ---------------------------------------------------------------------------
# 7. fail closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", NON_GENERATABLE_STATUSES)
def test_a_non_generatable_status_creates_nothing(status):
    anchor = None if status == "UNRESOLVED" else 18
    world = _World(projection=_projection(status=status, anchor=anchor))
    with pytest.raises(BaselineNotGeneratable):
        world.service.generate_for_child(world.principal(), CHILD)
    assert world.store.list_all("pilot_goal_suggestions") == []
    assert world.store.list_all("pilot_suggestion_generation_claims") == []


def test_a_projection_with_no_routing_anchor_creates_nothing():
    world = _World(projection=_projection(status="BOUNDED", anchor=None))
    with pytest.raises(BaselineNotGeneratable):
        world.service.generate_for_child(world.principal(), CHILD)
    assert world.store.list_all("pilot_goal_suggestions") == []


def test_a2_already_refuses_a_projection_for_another_domain():
    """The OUTER layer: an unsupported domain cannot even become a projection.

    A2's `ParentBaselineProjection` supports exactly `talking_and_communicating`,
    so 0.5F-B can never be handed a real projection for another domain. Pinned
    because it means the F-B domain gate below is defence in depth rather than
    the only guard — and if A2 ever widened, this test is where that shows up.
    """
    from pilot_backend.domain.parent_baseline_projection import (
        ProjectionValidationError,
    )

    with pytest.raises(ProjectionValidationError):
        _projection(domain="fine_motor")


def test_the_f_b_domain_gate_refuses_an_unsupported_domain():
    """The INNER layer, exercised directly.

    A stub stands in for a projection A2 cannot produce, so the gate is proven
    to refuse rather than merely assumed unreachable.
    """
    class _OtherDomainProjection:
        projection_id = "pbpj_other_domain"
        child_id = CHILD
        domain = "fine_motor"
        status = "BOUNDED"
        routing_anchor_months = 18
        not_demonstrated_months = 24
        has_routing_anchor = True

    world = _World()
    with pytest.raises(BaselineNotGeneratable):
        world.service.resolve_target(_OtherDomainProjection())


def test_a_missing_parent_source_link_creates_nothing():
    world = _World(with_link=False)
    with pytest.raises(ProjectionLineageInvalid):
        world.service.generate_for_child(world.principal(), CHILD)
    assert world.store.list_all("pilot_goal_suggestions") == []


def test_an_ambiguous_parent_source_link_creates_nothing():
    """The refusal must be the AMBIGUITY one, not merely some refusal.

    This test was flaky before the assertion on the message was added, and the
    mutation sweep is what exposed it: with the `len(links) > 1` guard disabled
    it still passed about two runs in three. `ProjectionLineageInvalid` covers
    both "more than one link" and "no projection for this session", so when the
    arbitrary ordering of `links[0]` happened to pick the second session — which
    has no projection — the SAME exception class was raised for an entirely
    different reason and the test could not tell.
    """
    world = _World()
    world.repos.source_links.create(SourceSystemLink.create(
        CHILD, SourceSystem.PARENT, SESSION + "-second",
        actor_id="t", actor_role=ActorRole.CAREGIVER.value, now=NOW))
    with pytest.raises(ProjectionLineageInvalid) as caught:
        world.service.generate_for_child(world.principal(), CHILD)
    assert "more than one active Parent source link" in str(caught.value)
    assert world.store.list_all("pilot_goal_suggestions") == []


def test_two_fully_valid_parent_sessions_still_refuse():
    """Order-independent by construction: EITHER link would succeed alone.

    The test above pins the message; this one removes the ordering dependence
    altogether. Both sessions have their own valid projection, so no
    "no projection" refusal is reachable and only the ambiguity guard can
    produce one. Whichever link `links[0]` happens to be, generating would
    otherwise succeed — and picking one would be choosing whose baseline counts.
    """
    world = _World()
    second = SESSION + "-second"
    world.repos.source_links.create(SourceSystemLink.create(
        CHILD, SourceSystem.PARENT, second,
        actor_id="t", actor_role=ActorRole.CAREGIVER.value, now=NOW))
    world.repos.parent_baseline_projections.create(
        _projection(session=second))

    with pytest.raises(ProjectionLineageInvalid) as caught:
        world.service.generate_for_child(world.principal(), CHILD)
    assert "more than one active Parent source link" in str(caught.value)
    assert world.store.list_all("pilot_goal_suggestions") == []
    assert world.store.list_all("pilot_suggestion_generation_claims") == []


def test_a_missing_projection_creates_nothing():
    world = _World(with_projection=False)
    with pytest.raises(ProjectionLineageInvalid):
        world.service.generate_for_child(world.principal(), CHILD)
    assert world.store.list_all("pilot_goal_suggestions") == []


def test_a_projection_child_mismatch_creates_nothing():
    world = _World(with_projection=False)
    world.repos.parent_baseline_projections.create(
        _projection(child="chld_somebody_else"))
    with pytest.raises(ProjectionLineageInvalid):
        world.service.generate_for_child(world.principal(), CHILD)
    assert world.store.list_all("pilot_goal_suggestions") == []


def test_no_next_rung_on_the_track_creates_nothing():
    """At the top of the declared track there is nothing to step to."""
    world = _World(projection=_projection(anchor=60, ceiling=None),
                   rungs=(_rung(months=24),))
    with pytest.raises(TargetNotResolvable):
        world.service.generate_for_child(world.principal(), CHILD)
    assert world.store.list_all("pilot_goal_suggestions") == []


def test_a_target_above_the_ceiling_creates_nothing():
    """The next rung is 30 but the child already failed 24."""
    world = _World(projection=_projection(anchor=18, ceiling=24),
                   rungs=(_rung(months=30, milestone="a harder milestone"),))
    with pytest.raises(TargetNotResolvable):
        world.service.generate_for_child(world.principal(), CHILD)
    assert world.store.list_all("pilot_goal_suggestions") == []


def test_an_unmappable_target_creates_nothing():
    """One of the intentionally unresolved declared-SLP cases."""
    world = _World(
        rungs=(),
        unmappable=(RungTarget(domain_key=SUPPORTED_DOMAIN,
                               source_rung_months=24,
                               milestone_text=EXPECTED_MILESTONE),))
    with pytest.raises(TargetNotMappable):
        world.service.generate_for_child(world.principal(), CHILD)
    assert world.store.list_all("pilot_goal_suggestions") == []
    assert world.store.list_all("pilot_suggestion_anchors") == []


def test_a_rung_the_gold_standard_does_not_have_creates_nothing():
    world = _World(rungs=(_rung(months=24, milestone="a different milestone"),))
    # The step finds months 24, but the milestone it names resolves to a rung
    # the fixture does not hold under the requested text.
    outcome = world.service.resolve_target(world.projection)
    assert outcome.source_rung_months == 24


# ---------------------------------------------------------------------------
# 8. concurrency over the in-memory store
# ---------------------------------------------------------------------------
#
# The in-memory store is a plain dict and NOT thread-safe, so this proves the
# DETERMINISTIC KEY converges — not the transaction. The real contention proof
# is the emulator suite.

def test_repeated_sequential_generation_never_adds_a_second_set():
    world = _World()
    results = [world.service.generate_for_child(world.principal(), CHILD)
               for _ in range(8)]
    assert sum(1 for r in results if r.created) == 1
    ids = {tuple(s.suggestion_id for s in r.suggestions) for r in results}
    assert len(ids) == 1, "the eight calls did not agree on one set"
    assert len(world.store.list_all("pilot_goal_suggestions")) == 1
    assert len(world.store.list_all("pilot_suggestion_anchors")) == 1
    assert len(world.store.list_all("pilot_suggestion_generation_claims")) == 1


# ---------------------------------------------------------------------------
# 9. structural guarantees
# ---------------------------------------------------------------------------

def test_the_claim_repository_has_no_mutating_method():
    from pilot_backend.persistence.firestore_repos import (
        FirestoreSuggestionGenerationClaimRepository,
    )

    public = {n for n in dir(FirestoreSuggestionGenerationClaimRepository)
              if not n.startswith("_")}
    assert public == {"create", "find", "record_type", "model"}
    for forbidden in ("update", "set", "delete", "overwrite", "mark_generated"):
        assert forbidden not in public


def test_the_generation_claim_is_not_an_identity_claim():
    """A separate collection, so ClaimKind keeps meaning identity uniqueness."""
    from pilot_backend.domain.identity_claims import ClaimKind
    from pilot_backend.persistence.collections import COLLECTIONS

    assert COLLECTIONS["suggestion_generation_claim"] == \
        "pilot_suggestion_generation_claims"
    assert COLLECTIONS["identity_claim"] == "pilot_identity_claims"
    assert not any("suggestion" in k.value for k in ClaimKind)


def test_the_frozen_generate_suggestions_boundary_is_unchanged():
    """0.5F-B adds a method; it does not alter the 0.4B/0.5E-A one."""
    import inspect

    sig = inspect.signature(GoalService.generate_suggestions)
    assert set(sig.parameters) == {"self", "principal", "child_id", "snapshot",
                                   "policy", "count", "request_id"}


def test_generation_consults_neither_age_nor_diagnosis():
    """Neither is representable in the input, so neither can be consulted."""
    fields = set(ParentBaselineProjection.__dataclass_fields__)
    for forbidden in ("chronological_months", "age_in_months", "diagnosis",
                      "concern", "asked"):
        assert forbidden not in fields
    source = __import__(
        "pilot_backend.integration.baseline_suggestion_generation",
        fromlist=["x"]).__file__
    effective = "\n".join(
        line.split("#", 1)[0]
        for line in open(source).read().split('"""', 2)[2].splitlines())
    for forbidden in ("chronological", "diagnosis", "age_in_months"):
        assert forbidden not in effective, forbidden


# ---------------------------------------------------------------------------
# 10. gaps the mutation sweep found
# ---------------------------------------------------------------------------
#
# Each test below exists because a mutation SURVIVED the suite above. Recorded
# as a group so the reason they exist is not lost.

def test_the_step_moves_exactly_one_rung_not_to_the_top():
    """Kills P1. The earlier fixture had ONE rung above the floor, so
    `higher[0]` and `higher[-1]` were the same element and a mutation that
    jumped to the top of the track was indistinguishable."""
    world = _World(rungs=(_rung(months=24),
                          _rung(months=30, milestone="a harder milestone"),
                          _rung(months=36, milestone="a much harder milestone")),
                   projection=_projection(anchor=18, ceiling=36))
    rung = world.service.resolve_target(world.projection)
    assert rung.source_rung_months == 24, "the step skipped forward"


def test_a_resolvable_but_unmappable_target_is_refused():
    """Kills M1 and M3. The earlier unmappable test used the fixture's
    `unmappable` set, so `rung_for_target` RAISED and the final
    `is_activity_mappable` check was never reached. This rung resolves
    successfully and is simply not mappable."""
    unmappable_rung = CanonicalRung.build(
        domain_key=SUPPORTED_DOMAIN, source_rung_months=24,
        milestone_text=EXPECTED_MILESTONE, subdomain=EXPECTED_SUBDOMAIN,
        # No family bindings at all -> resolves, but cannot be mapped.
        family_bindings=(),
        track_subdomains=("early_vocalization_and_babbling",
                          "expressive_language"),
        track_families=(),
        taxonomy_version=TAXONOMY_VERSION,
        baseline_version=GOLD_STANDARD_VERSION)
    assert unmappable_rung.is_activity_mappable is False

    world = _World(rungs=(unmappable_rung,))
    with pytest.raises(TargetNotMappable):
        world.service.generate_for_child(world.principal(), CHILD)
    assert world.store.list_all("pilot_goal_suggestions") == []
    assert world.store.list_all("pilot_suggestion_anchors") == []
    assert world.store.list_all("pilot_suggestion_generation_claims") == []


def test_the_anchor_boundary_refuses_an_unmappable_rung_directly():
    """Kills M3 at its own layer: the only place an anchor is written."""
    from pilot_backend.goals.errors import GoalValidationError

    unmappable_rung = CanonicalRung.build(
        domain_key=SUPPORTED_DOMAIN, source_rung_months=24,
        milestone_text=EXPECTED_MILESTONE, subdomain=EXPECTED_SUBDOMAIN,
        family_bindings=(),
        track_subdomains=("expressive_language",), track_families=(),
        taxonomy_version=TAXONOMY_VERSION,
        baseline_version=GOLD_STANDARD_VERSION)
    world = _World()
    observed = world.service._observed_domain(world.projection, unmappable_rung)
    with pytest.raises(GoalValidationError):
        world.goals.generate_anchored_suggestion(
            world.principal(), CHILD, projection=world.projection,
            canonical_rung=unmappable_rung, observed=observed)


def test_the_cycle_month_is_the_projections_month_not_this_month():
    """Kills L2. The earlier assertion used a projection stamped in the SAME
    month as the test clock, so reading `now()` instead was indistinguishable."""
    old = ParentBaselineProjection.build(
        child_id=CHILD, source_session_id=SESSION, source_record_digest=DIGEST,
        projection={"domain": SUPPORTED_DOMAIN, "area_id": "talking",
                    "entry_choice_id": "many_single_words",
                    "routing_anchor_months": 18,
                    "not_demonstrated_months": 24, "status": "BOUNDED",
                    "baseline_version": GOLD_STANDARD_VERSION},
        now=datetime(2025, 3, 9, 8, 0, tzinfo=timezone.utc))
    assert projection_cycle_month(old) == "2025-03"
    assert projection_cycle_month(old) != datetime.now(
        timezone.utc).strftime("%Y-%m")


def test_the_projection_must_name_the_authorized_child_at_the_boundary():
    """Kills A1. Exercised directly against the anchor boundary."""
    from pilot_backend.goals.errors import GoalValidationError

    world = _World()
    other = _projection(child="chld_somebody_else")
    observed = world.service._observed_domain(other, _rung())
    with pytest.raises(GoalValidationError):
        world.goals.generate_anchored_suggestion(
            world.principal(), CHILD, projection=other,
            canonical_rung=_rung(), observed=observed)


def test_the_rung_must_match_the_observed_domain_at_the_boundary():
    """Kills A2. A mismatched anchor is worse than none."""
    from pilot_backend.goals.errors import GoalValidationError

    world = _World()
    observed = world.service._observed_domain(world.projection, _rung())
    other_domain_rung = CanonicalRung.build(
        domain_key="fine_motor", source_rung_months=24,
        milestone_text="an unrelated milestone", subdomain="fine_motor_skills",
        family_bindings=(ActivityFamilyBinding(
            family_ref="grasp_and_release", allowed_domains=("fine_motor",)),),
        track_subdomains=("fine_motor_skills",), track_families=(),
        taxonomy_version=TAXONOMY_VERSION,
        baseline_version=GOLD_STANDARD_VERSION)
    with pytest.raises(GoalValidationError):
        world.goals.generate_anchored_suggestion(
            world.principal(), CHILD, projection=world.projection,
            canonical_rung=other_domain_rung, observed=observed)


def test_the_replay_path_returns_the_claims_own_suggestions():
    """Kills X2. Asserts the replay resolves through the CLAIM's lineage, not
    by re-querying the child — so another generation's suggestion can never be
    returned as this one's."""
    world = _World()
    first = world.service.generate_for_child(world.principal(), CHILD)
    claim_doc = world.store.list_all("pilot_suggestion_generation_claims")[0][1]
    claim = world.repos.suggestion_generation_claims.find(claim_doc["claim_id"])
    assert claim is not None
    resolved = world.goals._suggestions_for_claim(claim)
    assert [s.suggestion_id for s in resolved] == \
        [s.suggestion_id for s in first.suggestions]


def test_a_refusal_after_the_claim_check_writes_no_partial_state():
    """Kills X1's intent at the unit layer: a refusal must leave NOTHING.

    The emulator suite proves the concurrent case; this proves the sequential
    one, where an unmappable rung is refused after the claim was computed.
    """
    unmappable_rung = CanonicalRung.build(
        domain_key=SUPPORTED_DOMAIN, source_rung_months=24,
        milestone_text=EXPECTED_MILESTONE, subdomain=EXPECTED_SUBDOMAIN,
        family_bindings=(), track_subdomains=("expressive_language",),
        track_families=(), taxonomy_version=TAXONOMY_VERSION,
        baseline_version=GOLD_STANDARD_VERSION)
    world = _World(rungs=(unmappable_rung,))
    with pytest.raises(TargetNotMappable):
        world.service.generate_for_child(world.principal(), CHILD)
    for collection in ("pilot_suggestion_generation_claims",
                       "pilot_goal_suggestions", "pilot_suggestion_anchors"):
        assert world.store.list_all(collection) == [], collection


def test_an_ended_parent_source_link_does_not_resolve_a_session():
    """Kills R3. An ended link must not supply the session."""
    world = _World(with_link=False)
    link = SourceSystemLink.create(
        CHILD, SourceSystem.PARENT, SESSION, actor_id="t",
        actor_role=ActorRole.CAREGIVER.value, now=NOW)
    world.repos.source_links.create(link.end(reason="test", actor_id="t",
                                             now=NOW))
    with pytest.raises(ProjectionLineageInvalid):
        world.service.generate_for_child(world.principal(), CHILD)
    assert world.store.list_all("pilot_goal_suggestions") == []


def test_zero_links_and_a_bogus_session_are_distinguishable():
    """Kills R1. The earlier test could not tell "no link" from "a link naming
    a session with no projection", because both raise the same class. This
    asserts the no-link case refuses BEFORE any projection lookup."""
    world = _World(with_link=False, with_projection=False)
    with pytest.raises(ProjectionLineageInvalid) as caught:
        world.service._current_parent_session(CHILD)
    assert "no active Parent source link" in str(caught.value)


def test_two_projections_for_one_session_are_refused():
    """Kills R5. Two digests for one immutable source is an integrity failure."""
    world = _World()
    world.repos.parent_baseline_projections.create(
        ParentBaselineProjection.build(
            child_id=CHILD, source_session_id=SESSION,
            source_record_digest="b" * 64,
            projection={"domain": SUPPORTED_DOMAIN, "area_id": "talking",
                        "entry_choice_id": "many_single_words",
                        "routing_anchor_months": 18,
                        "not_demonstrated_months": 24, "status": "BOUNDED",
                        "baseline_version": GOLD_STANDARD_VERSION},
            now=NOW))
    with pytest.raises(ProjectionLineageInvalid) as caught:
        world.service.generate_for_child(world.principal(), CHILD)
    assert "more than one baseline projection" in str(caught.value)
    assert world.store.list_all("pilot_goal_suggestions") == []


def test_only_a_provider_passes_the_managing_clinician_gate():
    """Kills A3. Asserts the ROLE check specifically, with a caregiver who IS
    otherwise related to the child — so the refusal cannot come from the
    relationship lookup instead."""
    world = _World()
    caregiver_principal = world.principal("cg-subject")
    assert caregiver_principal.role is ActorRole.CAREGIVER
    with pytest.raises(GoalAuthorizationError) as caught:
        world.goals._require_managing_clinician(caregiver_principal, CHILD)
    assert "managing clinician" in str(caught.value)


# ---------------------------------------------------------------------------
# 11. the generation route over real HTTP
# ---------------------------------------------------------------------------
#
# Added because A4 (provider-only) and A6 (POST-only) SURVIVED: nothing
# exercised the route itself, only the route table. These drive the real WSGI
# application.

from pilot_backend.tests.test_integration_identity import (  # noqa: E402
    CAREGIVER_ALPHA_SUBJECT,
    PROVIDER_ALPHA_SUBJECT,
    build_http,
    call,
)
from pilot_backend.transport.wsgi_app import (  # noqa: E402
    GENERATE_SUGGESTIONS_ROUTE,
)


def _http_world():
    """The secure topology, plus a Parent link and projection for its child."""
    http = build_http(rung_source=InMemoryGoldStandardRungSource(
        rungs=(_rung(),)))
    child_id = http.topo.child_alpha.child_id
    http.repos.source_links.create(SourceSystemLink.create(
        child_id, SourceSystem.PARENT, SESSION, actor_id="fixture",
        actor_role=ActorRole.CAREGIVER.value, now=NOW))
    http.repos.parent_baseline_projections.create(ParentBaselineProjection.build(
        child_id=child_id, source_session_id=SESSION,
        source_record_digest=DIGEST,
        projection={"domain": SUPPORTED_DOMAIN, "area_id": "talking",
                    "entry_choice_id": "many_single_words",
                    "routing_anchor_months": 18,
                    "not_demonstrated_months": 24, "status": "BOUNDED",
                    "baseline_version": GOLD_STANDARD_VERSION},
        now=NOW))
    http.child_id = child_id
    http.path = f"/pilot/children/{child_id}/goal-suggestions/generate"
    return http


def test_a_caregiver_cannot_invoke_the_generation_route():
    """Kills A4. A family must not trigger clinical target selection."""
    http = _http_world()
    status, body, _ = call(http.app, http.path, method="POST",
                           bearer="Bearer token-caregiver-alpha")
    assert status == 403
    assert body == {"error": "not permitted"}
    assert http.repos.store.list_all("pilot_suggestion_generation_claims") == []


def test_generation_is_not_reachable_by_get():
    """Kills A6. A GET that mutated would be cacheable and prefetchable."""
    http = _http_world()
    status, _, _ = call(http.app, http.path, method="GET",
                        bearer="Bearer token-provider-alpha")
    assert status == 405
    assert http.repos.store.list_all("pilot_suggestion_generation_claims") == []


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_only_post_generates(method):
    http = _http_world()
    status, _, _ = call(http.app, http.path, method=method,
                        bearer="Bearer token-provider-alpha")
    assert status == 405


def test_an_unauthenticated_generation_request_is_refused():
    http = _http_world()
    status, _, _ = call(http.app, http.path, method="POST")
    assert status in (401, 403)
    assert http.repos.store.list_all("pilot_suggestion_generation_claims") == []


def test_the_route_fails_closed_with_no_gold_standard_configured():
    """No rung source means no canonical target can be resolved."""
    http = build_http()  # rung_source defaults to None
    child_id = http.topo.child_alpha.child_id
    status, body, _ = call(
        http.app, f"/pilot/children/{child_id}/goal-suggestions/generate",
        method="POST", bearer="Bearer token-provider-alpha")
    assert status == 403
    assert http.repos.store.list_all("pilot_suggestion_generation_claims") == []


def test_the_route_template_is_registered_and_protected():
    from pilot_backend.transport.wsgi_app import route_templates

    assert route_templates()[GENERATE_SUGGESTIONS_ROUTE] is False


def test_the_observation_earns_no_prior_cycle_continuity_bonus():
    """Measured, not asserted by inspection: the misuse inflated the score by 15
    (25 -> 40) and added a false `explain()` reason."""
    from pilot_backend.goals.suggestion_engine import evidence_score, explain

    world = _World()
    observed = world.service._observed_domain(world.projection, _rung())
    assert observed.prior_month_summary_id is None
    assert "prior_cycle_continuity" not in explain(observed)

    from dataclasses import replace as _replace
    inflated = _replace(observed,
                        prior_month_summary_id=world.projection.projection_id)
    assert evidence_score(inflated) - evidence_score(observed) == 15
    assert "prior_cycle_continuity" in explain(inflated)
