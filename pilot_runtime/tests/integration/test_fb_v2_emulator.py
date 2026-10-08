"""F-B v2 under CONTENTION, against a REAL Firestore emulator. 0.6A-1G.

The `pilot_backend` suite runs this same service over `FakeDocumentStore`, which
is a plain dict and NOT thread-safe. Every claim about concurrency is made here
and only here.

## What a real server is needed to prove

**One claim, one suggestion, one anchor PER CANONICAL TARGET, under contention.**
Eight threads generate for the same child at the same moment, against a band with
two mappable deficits. Afterwards exactly two claims, two suggestions and two
anchors exist — not sixteen, not two-plus-orphans, and not one because a race
collapsed a target.

**Per-target atomicity is what lets the targets be independent.** F-B v2 does NOT
wrap its targets in one transaction: if it did, one contended target would roll
back its siblings, which is the "an unmappable or failing target must not block
mappable siblings" rule violated by the transaction boundary instead of by the
logic. Each target therefore commits claim-first on its own, and the loser of a
race writes nothing at all.

**Replay across the real store, with nothing cached.** A second request returns
the same suggestion ids and writes nothing.

The 0.4A emulator lesson applies throughout: a stalled harness must report itself
rather than return a short result list that reads as a uniqueness win.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth.interface import VerifiedToken
from pilot_backend.auth.resolver import resolve_principal
from pilot_backend.domain.canonical_rung import (
    ActivityFamilyBinding,
    CanonicalRung,
    compute_rung_ref,
)
from pilot_backend.domain.parent_baseline_projection_v2 import (
    ParentBaselineProjectionV2,
    ProjectedBandTotal,
    ProjectedSkillEvidence,
)
from pilot_backend.domain.roles import ActorRole
from pilot_backend.domain.source_link import SourceSystem, SourceSystemLink
from pilot_backend.domain.suggestion_generation_v2 import (
    GENERATION_POLICY_VERSION_V2,
    OUTCOME_GENERATED,
)
from pilot_backend.goals.service import GoalService
from pilot_backend.integration.baseline_suggestion_generation_v2 import (
    BaselineSuggestionGenerationV2Service,
)
from pilot_backend.integration.gold_standard_source import (
    InMemoryGoldStandardRungSource,
    RungTarget,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
DOMAIN = "talking_and_communicating"
TAXONOMY_VERSION = "activity_family_taxonomy_v1"
GOLD_STANDARD_VERSION = "parent-2.4-functional-baseline-v1"
BASELINE_VERSION_V2 = "parent-2.4-functional-baseline-v2"
DIGEST = "f" * 64

AT_24 = "says at least two words together like more milk"
BOOK = "name things in a book when you point and ask what is this"
TWO_WORD = "say two or more words together with one action word"
VOCAB = "says about 50 words"
PRONOUNS = "says words like i me or we"
MAPPABLE_30 = (BOOK, TWO_WORD, VOCAB)

#: Eight writers, the same count the 0.5B/0.5F-A3 contention suites use.
WRITERS = 8


def _rung(milestone, months):
    return CanonicalRung.build(
        domain_key=DOMAIN, source_rung_months=months,
        milestone_text=milestone, subdomain="expressive_language",
        family_bindings=(ActivityFamilyBinding(
            family_ref="expressive_vocabulary_growth",
            allowed_domains=(DOMAIN,)),),
        track_subdomains=("early_vocalization_and_babbling",
                          "expressive_language"),
        track_families=(), taxonomy_version=TAXONOMY_VERSION,
        baseline_version=GOLD_STANDARD_VERSION)


def _ref(milestone, months):
    return compute_rung_ref(DOMAIN, months, milestone)


def _source():
    return InMemoryGoldStandardRungSource(
        rungs=tuple([_rung(AT_24, 24)]
                    + [_rung(m, 30) for m in MAPPABLE_30]),
        unmappable=(RungTarget(domain_key=DOMAIN, source_rung_months=30,
                               milestone_text=PRONOUNS),))


@pytest.fixture()
def world(repos, unique_suffix):
    """A fictional child with a managing clinician and a v2 projection.

    Two MAPPABLE deficits plus one unmappable one, so the contention test is
    about several independent targets rather than a single mutex.
    """
    from pilot_backend.domain.managing_clinician import (
        ManagingClinicianAssignment,
    )
    from pilot_backend.fixtures.secure_topology import build_secure_topology

    topo = build_secure_topology(repos, now=NOW,
                                subject_suffix=unique_suffix)
    child_id = topo.child_alpha.child_id
    session_id = f"sess-fbv2-emulator{unique_suffix}"

    repos.managing_clinicians.create(ManagingClinicianAssignment.create(
        child_id, topo.provider_alpha.provider_id, topo.practice.practice_id,
        provider_connection_id=topo.link_alpha_provider.connection_id,
        actor_id=topo.caregiver_alpha.caregiver_id, now=NOW))
    repos.source_links.create(SourceSystemLink.create(
        child_id, SourceSystem.PARENT, session_id, actor_id="fixture",
        actor_role=ActorRole.CAREGIVER.value, now=NOW))

    states = {BOOK: "not_demonstrated", TWO_WORD: "emerging",
              VOCAB: "demonstrated", PRONOUNS: "not_demonstrated"}
    evidence = [ProjectedSkillEvidence(rung_ref=_ref(AT_24, 24), months=24,
                                       state="demonstrated")]
    evidence += [ProjectedSkillEvidence(rung_ref=_ref(m, 30), months=30,
                                        state=s) for m, s in states.items()]
    projection = ParentBaselineProjectionV2.build(
        child_id=child_id, source_session_id=session_id,
        source_record_digest=DIGEST,
        summary={"domain": DOMAIN, "area_id": "talking",
                 "entry_choice_id": "two_three_words",
                 "routing_anchor_months": 24,
                 "not_demonstrated_months": 30, "status": "BOUNDED",
                 "baseline_version": BASELINE_VERSION_V2},
        skill_evidence=tuple(evidence),
        band_totals=(ProjectedBandTotal(months=24, total_skills=1),
                     ProjectedBandTotal(months=30, total_skills=4)), now=NOW)
    repos.parent_baseline_projections_v2.create(projection)

    recorder = AuditRecorder(repos.audit_events, environment="test")
    goals = GoalService(repos=repos, recorder=recorder, now=lambda: NOW)

    class Bundle:
        pass

    bundle = Bundle()
    bundle.repos, bundle.goals, bundle.child_id = repos, goals, child_id
    bundle.session_id, bundle.projection = session_id, projection
    bundle.source = _source()
    bundle.principal = resolve_principal(
        VerifiedToken(subject=topo.provider_alpha.auth_subject), repos)
    bundle.service = lambda: BaselineSuggestionGenerationV2Service(
        repos=repos, goals=goals, rung_source=bundle.source)
    bundle.expected_targets = {_ref(BOOK, 30), _ref(TWO_WORD, 30)}
    bundle.unsupported = _ref(PRONOUNS, 30)
    return bundle


def _claims_for(world):
    return [claim for _id, claim in world.repos.store.list_all(
        "pilot_suggestion_generation_claims")
        if claim.get("child_id") == world.child_id]


def _suggestions_for(world):
    return [s for _id, s in world.repos.store.list_all(
        "pilot_goal_suggestions") if s.get("child_id") == world.child_id]


def _anchors_for(world):
    return [a for _id, a in world.repos.store.list_all(
        "pilot_suggestion_anchors") if a.get("child_id") == world.child_id]


def test_eight_concurrent_generations_produce_one_claim_per_target(world):
    """The whole contention claim, against a real server.

    Two mappable targets and eight writers. Exactly two claims, two suggestions
    and two anchors — and the anchors are for the two refs the EVIDENCE named.
    """
    barrier = threading.Barrier(WRITERS)
    results, failures = [], []

    def generate(_index):
        barrier.wait(timeout=30)
        try:
            return world.service().generate_for_child(
                world.principal, world.child_id)
        except Exception as exc:  # recorded, never swallowed
            failures.append(exc)
            return None

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        for outcome in pool.map(generate, range(WRITERS)):
            results.append(outcome)

    # A stalled harness must report itself rather than return a short list that
    # reads as a uniqueness win.
    assert len(results) == WRITERS, results
    assert not failures, failures
    assert all(outcome is not None for outcome in results)

    claims = _claims_for(world)
    suggestions = _suggestions_for(world)
    anchors = _anchors_for(world)
    assert len(claims) == 2, [c["target_rung_ref"] for c in claims]
    assert len(suggestions) == 2
    assert len(anchors) == 2

    # One claim per canonical target, each under the v2 policy.
    assert {c["target_rung_ref"] for c in claims} == world.expected_targets
    assert {c["generation_policy"] for c in claims} == {
        GENERATION_POLICY_VERSION_V2}
    # No orphans: every suggestion has an anchor, and every anchor's rung is one
    # of the two targets.
    assert ({a["suggestion_id"] for a in anchors}
            == {s["suggestion_id"] for s in suggestions})
    assert {a["rung"]["rung_ref"] for a in anchors} == world.expected_targets

    # Nothing was created for the unmappable deficit or the demonstrated skill.
    assert world.unsupported not in {a["rung"]["rung_ref"] for a in anchors}
    assert _ref(VOCAB, 30) not in {a["rung"]["rung_ref"] for a in anchors}

    # Every writer converged on the SAME two suggestion ids, and exactly one
    # writer per target reports having created it.
    stored_ids = {s["suggestion_id"] for s in suggestions}
    for outcome in results:
        assert set(outcome.suggestion_ids) == stored_ids
        assert outcome.outcome == OUTCOME_GENERATED
        assert outcome.unsupported_target_refs == (world.unsupported,)
    created_counts = {}
    for outcome in results:
        for target in outcome.generated:
            created_counts.setdefault(target.rung_ref, 0)
            created_counts[target.rung_ref] += int(target.created)
    assert created_counts == {ref: 1 for ref in world.expected_targets}, (
        created_counts)


def test_a_replay_against_the_real_store_writes_nothing_new(world):
    first = world.service().generate_for_child(world.principal,
                                              world.child_id)
    assert first.outcome == OUTCOME_GENERATED
    assert len(_claims_for(world)) == 2

    # A FRESH service instance, so nothing is cached between the two calls.
    second = world.service().generate_for_child(world.principal,
                                               world.child_id)
    assert set(second.suggestion_ids) == set(first.suggestion_ids)
    assert all(target.created is False for target in second.generated)
    assert second.unsupported_target_refs == first.unsupported_target_refs
    assert len(_claims_for(world)) == 2
    assert len(_suggestions_for(world)) == 2
    assert len(_anchors_for(world)) == 2


def test_generation_creates_no_clinical_goal_against_the_real_store(world):
    """Item 14, through the real adapter. Approval is still the only path."""
    world.service().generate_for_child(world.principal, world.child_id)
    for collection in ("pilot_clinical_goals", "pilot_goal_versions",
                       "pilot_clinical_goal_anchors", "pilot_caregiver_goals",
                       "pilot_monthly_focus_plans",
                       "pilot_monthly_goal_allocations",
                       "pilot_weekly_cycles", "pilot_weekly_plan_snapshots"):
        rows = [r for _i, r in world.repos.store.list_all(collection)
                if r.get("child_id") == world.child_id]
        assert rows == [], collection
    assert {s["status"] for s in _suggestions_for(world)} == {"offered"}


def test_a_concurrent_replay_after_a_first_generation_adds_nothing(world):
    """The common production shape: one generation, then eight opens of the child."""
    world.service().generate_for_child(world.principal, world.child_id)
    baseline_claims = len(_claims_for(world))

    barrier = threading.Barrier(WRITERS)

    def generate(_index):
        barrier.wait(timeout=30)
        return world.service().generate_for_child(world.principal,
                                                 world.child_id)

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        outcomes = list(pool.map(generate, range(WRITERS)))

    assert len(outcomes) == WRITERS
    assert len(_claims_for(world)) == baseline_claims == 2
    assert len(_suggestions_for(world)) == 2
    for outcome in outcomes:
        assert all(target.created is False for target in outcome.generated)
