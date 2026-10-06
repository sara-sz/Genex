"""0.5F-B — deterministic generation against a REAL Firestore emulator.

The `pilot_backend` suite runs this same service code over a plain dict, which is
NOT thread-safe. Every claim about CONTENTION is made here and only here.

What a real server is needed to prove:

**Eight simultaneous generation requests produce ONE effective set.** One claim,
one suggestion, one anchor per suggestion, zero orphans. A read-then-write guard
— which is what 0.5E-B's `list_suggestions` pre-check is — would let several
through, and the result would be parallel candidate sets for one baseline with
nothing saying which is Genex's recommendation.

**The claim is the mutex, not a flag.** The deterministic document id is what
makes the eight writers collide on one document.

**Losers write nothing.** The claim is created FIRST inside the transaction, so a
writer that loses has not already committed a suggestion or an anchor.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth.interface import VerifiedToken
from pilot_backend.auth.resolver import resolve_principal
from pilot_backend.domain.canonical_rung import ActivityFamilyBinding, CanonicalRung
from pilot_backend.domain.connections import (
    CaregiverChildConnection,
    ProviderChildConnection,
)
from pilot_backend.domain.entities import Caregiver, Child, Practice, Provider
from pilot_backend.domain.enums import CaregiverRelationship, ProviderDiscipline
from pilot_backend.domain.managing_clinician import ManagingClinicianAssignment
from pilot_backend.domain.parent_baseline_projection import (
    ParentBaselineProjection,
)
from pilot_backend.domain.roles import ActorRole
from pilot_backend.domain.source_link import SourceSystem, SourceSystemLink
from pilot_backend.goals.service import GoalService
from pilot_backend.integration.baseline_suggestion_generation import (
    SUPPORTED_DOMAIN,
    BaselineSuggestionGenerationService,
)
from pilot_backend.integration.gold_standard_source import (
    InMemoryGoldStandardRungSource,
)

T0 = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
RACE_WIDTH = 8
DIGEST = "a8dd7506bcbce333ab5d9ea94991875440f8a56e5c31ce65c67ecf4ad8345685"
MILESTONE = "says at least two words together like more milk"


class _AdvancingClock:
    """Deterministic, strictly increasing and thread-safe — see 0.4A."""

    def __init__(self, start: datetime) -> None:
        self._start = start.replace(microsecond=0)
        self._tick = 0
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            self._tick += 1
            tick = self._tick
        return self._start + timedelta(seconds=tick)


def _rung():
    return CanonicalRung.build(
        domain_key=SUPPORTED_DOMAIN, source_rung_months=24,
        milestone_text=MILESTONE, subdomain="expressive_language",
        family_bindings=(
            ActivityFamilyBinding(family_ref="expressive_vocabulary_growth",
                                  allowed_domains=(SUPPORTED_DOMAIN,)),
            ActivityFamilyBinding(family_ref="two_word_phrases",
                                  allowed_domains=(SUPPORTED_DOMAIN,))),
        track_subdomains=("early_vocalization_and_babbling",
                          "expressive_language"),
        track_families=(),
        taxonomy_version="activity_family_taxonomy_v1",
        baseline_version="parent-2.4-functional-baseline-v1")


@pytest.fixture()
def generation(repos, unique_suffix):
    """A fictional child with a managing provider and one projection."""
    recorder = AuditRecorder(repos.audit_events, environment="test")

    class Bundle:
        pass

    bundle = Bundle()
    bundle.repos = repos
    bundle.goals = GoalService(repos=repos, recorder=recorder,
                              now=_AdvancingClock(T0))
    bundle.source = InMemoryGoldStandardRungSource(rungs=(_rung(),))
    bundle.service = BaselineSuggestionGenerationService(
        repos=repos, goals=bundle.goals, rung_source=bundle.source)

    # Unique per test AND per run: the emulator database is never reset and
    # these tests assert ABSOLUTE counts, so a second run would otherwise see
    # the first run's records. The lesson the 0.5A bridge suite learned twice.
    nonce = "-" + uuid.uuid4().hex[:10]
    session = "fictional-parent-session-fb" + unique_suffix + nonce
    subject = "fictional-provider-fb" + unique_suffix + nonce
    cg_subject = "fictional-caregiver-fb" + unique_suffix + nonce

    child = Child.create(actor_id="fixture", now=T0)
    repos.children.create(child)
    bundle.child_id = child.child_id

    caregiver = Caregiver.create("Fictional Caregiver",
                                 auth_subject=cg_subject, now=T0)
    repos.caregivers.create(caregiver)
    repos.caregiver_child.connect(CaregiverChildConnection.create(
        caregiver.caregiver_id, child.child_id, CaregiverRelationship.PARENT,
        actor_id=caregiver.caregiver_id, now=T0))

    practice = Practice.create("Fictional Practice", now=T0)
    repos.practices.create(practice)
    provider = Provider.create(practice.practice_id, ProviderDiscipline.SLP,
                               "Hannah", auth_subject=subject, now=T0)
    repos.providers.create(provider)
    connection = ProviderChildConnection.create(
        provider.provider_id, child.child_id, practice.practice_id,
        actor_id=caregiver.caregiver_id, now=T0).activate(now=T0)
    repos.provider_child.connect(connection)
    repos.managing_clinicians.create(ManagingClinicianAssignment.create(
        child.child_id, provider.provider_id, practice.practice_id,
        provider_connection_id=connection.connection_id,
        actor_id=caregiver.caregiver_id, now=T0))

    repos.source_links.create(SourceSystemLink.create(
        child.child_id, SourceSystem.PARENT, session,
        actor_id=caregiver.caregiver_id,
        actor_role=ActorRole.CAREGIVER.value, now=T0))

    bundle.projection = ParentBaselineProjection.build(
        child_id=child.child_id, source_session_id=session,
        source_record_digest=DIGEST,
        projection={"domain": SUPPORTED_DOMAIN, "area_id": "talking",
                    "entry_choice_id": "many_single_words",
                    "routing_anchor_months": 18,
                    "not_demonstrated_months": 24,
                    "status": "BOUNDED",
                    "baseline_version": "parent-2.4-functional-baseline-v1"},
        now=T0)
    repos.parent_baseline_projections.create(bundle.projection)
    bundle.subject = subject
    bundle.principal = resolve_principal(VerifiedToken(subject=subject), repos)
    return bundle


def _race(call, width: int = RACE_WIDTH):
    """Run `call(i)` on `width` threads released together by a barrier.

    Without the barrier the first thread usually finishes before the last
    starts, and the test would pass against a service with no mutex at all.
    """
    barrier = threading.Barrier(width)
    results, errors = [], []
    lock = threading.Lock()

    def run(i):
        barrier.wait(timeout=30)
        try:
            value = call(i)
        except Exception as exc:  # noqa: BLE001 - classified by the caller
            with lock:
                errors.append(exc)
            return
        with lock:
            results.append(value)

    with ThreadPoolExecutor(max_workers=width) as pool:
        list(pool.map(run, range(width)))

    assert len(results) + len(errors) == width, (
        f"the harness stalled: {len(results)} results + {len(errors)} errors "
        f"for {width} threads")
    return results, errors


def _mine(generation, collection, field, value):
    """Only the records belonging to THIS test's child or projection."""
    return [doc for _id, doc
            in generation.repos.store.query_equals(collection, field, value)]


# ---------------------------------------------------------------------------
# the property that needs a real server
# ---------------------------------------------------------------------------

def test_eight_concurrent_generations_produce_one_effective_set(generation):
    """THE test. A read-then-write guard would let several through."""
    results, errors = _race(
        lambda i: generation.service.generate_for_child(
            generation.principal, generation.child_id,
            request_id=f"req-gen-{i}"))

    assert errors == [], [type(e).__name__ for e in errors]
    assert len(results) == RACE_WIDTH
    assert sum(1 for r in results if r.created) == 1, (
        "more than one writer believed it generated")

    sets = {tuple(s.suggestion_id for s in r.suggestions) for r in results}
    assert len(sets) == 1, f"the eight calls disagreed: {sets}"

    claims = _mine(generation, "pilot_suggestion_generation_claims",
                   "projection_id", generation.projection.projection_id)
    assert len(claims) == 1, f"{len(claims)} generation claims"

    suggestions = _mine(generation, "pilot_goal_suggestions",
                        "child_id", generation.child_id)
    assert len(suggestions) == 1, f"{len(suggestions)} suggestions"

    anchors = _mine(generation, "pilot_suggestion_anchors",
                    "child_id", generation.child_id)
    assert len(anchors) == 1, f"{len(anchors)} anchors"

    # One anchor per suggestion, and no orphan of either kind.
    assert anchors[0]["suggestion_id"] == suggestions[0]["suggestion_id"]
    assert list(claims[0]["suggestion_ids"]) == [suggestions[0]["suggestion_id"]]


def test_the_claim_document_id_is_the_mutex(generation):
    from pilot_backend.domain.suggestion_generation import (
        generation_claim_id,
        generation_key,
    )

    generation.service.generate_for_child(generation.principal,
                                          generation.child_id)
    rung = _rung()
    key = generation_key(
        projection_id=generation.projection.projection_id,
        domain_key=SUPPORTED_DOMAIN, target_rung_ref=rung.rung_ref,
        taxonomy_version=rung.taxonomy_version,
        gold_standard_version=rung.baseline_version)
    assert generation.repos.suggestion_generation_claims.find(
        generation_claim_id(key)) is not None


def test_a_sequential_retry_after_the_race_adds_nothing(generation):
    results, _ = _race(
        lambda i: generation.service.generate_for_child(
            generation.principal, generation.child_id))
    winner = next(r for r in results if r.created)

    retried = generation.service.generate_for_child(generation.principal,
                                                   generation.child_id)
    assert retried.created is False
    assert [s.suggestion_id for s in retried.suggestions] == \
        [s.suggestion_id for s in winner.suggestions]
    assert len(_mine(generation, "pilot_goal_suggestions", "child_id",
                     generation.child_id)) == 1


def test_the_stored_claim_round_trips_through_firestore(generation):
    outcome = generation.service.generate_for_child(generation.principal,
                                                    generation.child_id)
    claims = _mine(generation, "pilot_suggestion_generation_claims",
                   "projection_id", generation.projection.projection_id)
    stored = claims[0]
    assert stored["target_rung_months"] == 24
    assert stored["domain_key"] == SUPPORTED_DOMAIN
    assert stored["generation_policy"] == "goal-suggestion-generation-policy-v1"
    assert stored["target_rung_ref"] == outcome.target_rung_ref
    # The requester is recorded for AUDIT, and is not part of the key.
    assert stored["requested_by_actor_id"] == \
        generation.principal.application_id


def test_the_provider_read_path_sees_exactly_one_suggestion(generation):
    _race(lambda i: generation.service.generate_for_child(
        generation.principal, generation.child_id))
    found = generation.goals.list_suggestions(generation.principal,
                                              generation.child_id)
    assert len(found) == 1
    assert found[0].status.value == "offered"


def test_no_clinical_goal_is_created_by_generation(generation):
    _race(lambda i: generation.service.generate_for_child(
        generation.principal, generation.child_id))
    assert _mine(generation, "pilot_clinical_goals", "child_id",
                 generation.child_id) == []
