"""0.6A-2 — the current-week release under CONTENTION, on a real emulator.

The `pilot_backend` suite runs this same orchestrator over `FakeDocumentStore`,
which is a plain dict and NOT thread-safe. Every claim about contention is made
here and only here.

## What a real server is needed to prove

**Eight concurrent releases produce ONE week.** One weekly cycle, one canonical
allocation, one `CapacityLedger`, one immutable snapshot, one released cycle —
and every caller that succeeds converges on the same released week.

**No partial or orphan writes.** The release composes several existing
transitions, each with its own claim or create-only refusal. A loser must leave
nothing behind: no second cycle at the same sequence, no second snapshot, no
alignments without a ledger, no ledger without alignments.

The 0.4A emulator lesson applies: a stalled harness must report itself rather
than return a short result list that reads as a uniqueness win.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

#: Eight writers, the same count every other contention suite here uses.
WRITERS = 8

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
DOMAIN = "talking_and_communicating"
CYCLE_MONTH = "2026-10"
TZ = "America/New_York"
CAPACITY = 3

AT_24 = "says at least two words together like more milk"
BOOK = "name things in a book when you point and ask what is this"
TWO_WORD = "say two or more words together with one action word"
VOCAB = "says about 50 words"
PRONOUNS = "says words like i me or we"
TAXONOMY_VERSION = "activity_family_taxonomy_v1"
GOLD_STANDARD_VERSION = "parent-2.4-functional-baseline-v1"
BASELINE_VERSION_V2 = "parent-2.4-functional-baseline-v2"
TRACK_SUBDOMAINS = ("early_vocalization_and_babbling", "expressive_language")
FAMILY_BY_MILESTONE = {
    AT_24: "expressive_vocabulary_growth",
    BOOK: "book_object_naming",
    TWO_WORD: "two_word_phrases",
    VOCAB: "expressive_vocabulary_growth",
}


def _rung(milestone, months, families):
    from pilot_backend.domain.canonical_rung import (
        ActivityFamilyBinding,
        CanonicalRung,
    )

    return CanonicalRung.build(
        domain_key=DOMAIN, source_rung_months=months,
        milestone_text=milestone, subdomain="expressive_language",
        family_bindings=tuple(
            ActivityFamilyBinding(family_ref=f, allowed_domains=(DOMAIN,))
            for f in families),
        track_subdomains=TRACK_SUBDOMAINS, track_families=(),
        taxonomy_version=TAXONOMY_VERSION,
        baseline_version=GOLD_STANDARD_VERSION)


@pytest.fixture()
def world(repos, unique_suffix):
    """A fictional child with an APPROVED, anchored, mappable goal.

    The goal comes from the real F-B v2 generation plus the real approval path,
    so the anchor and its `family_bindings` are genuine rather than seeded.
    """
    from pilot_backend.audit.recorder import AuditRecorder
    from pilot_backend.auth.interface import VerifiedToken
    from pilot_backend.auth.resolver import resolve_principal
    from pilot_backend.domain.goals import EditType
    from pilot_backend.domain.managing_clinician import (
        ManagingClinicianAssignment,
    )
    from pilot_backend.domain.parent_baseline_projection_v2 import (
        ParentBaselineProjectionV2,
        ProjectedBandTotal,
        ProjectedSkillEvidence,
    )
    from pilot_backend.domain.canonical_rung import compute_rung_ref
    from pilot_backend.domain.roles import ActorRole
    from pilot_backend.domain.source_link import SourceSystem, SourceSystemLink
    from pilot_backend.fixtures.secure_topology import build_secure_topology
    from pilot_backend.goals.service import GoalService
    from pilot_backend.integration.baseline_suggestion_generation_v2 import (
        BaselineSuggestionGenerationV2Service,
    )
    from pilot_backend.integration.gold_standard_source import (
        InMemoryGoldStandardRungSource,
        RungTarget,
    )
    from pilot_backend.integration.week_one_release import (
        WeekOneReleaseService,
    )
    from pilot_backend.planning.service import MonthlyPlanService
    from pilot_backend.weekly.service import WeeklyService
    from pilot_runtime.integration.static_activity_bank import (
        StaticActivityBank,
    )

    topo = build_secure_topology(repos, now=NOW, subject_suffix=unique_suffix)
    child_id = topo.child_alpha.child_id
    session_id = f"sess-week1{unique_suffix}"

    repos.managing_clinicians.create(ManagingClinicianAssignment.create(
        child_id, topo.provider_alpha.provider_id, topo.practice.practice_id,
        provider_connection_id=topo.link_alpha_provider.connection_id,
        actor_id=topo.caregiver_alpha.caregiver_id, now=NOW))
    repos.source_links.create(SourceSystemLink.create(
        child_id, SourceSystem.PARENT, session_id, actor_id="fixture",
        actor_role=ActorRole.CAREGIVER.value, now=NOW))

    rung_source = InMemoryGoldStandardRungSource(
        rungs=(_rung(AT_24, 24, (FAMILY_BY_MILESTONE[AT_24],)),
               _rung(BOOK, 30, (FAMILY_BY_MILESTONE[BOOK],)),
               _rung(TWO_WORD, 30, (FAMILY_BY_MILESTONE[TWO_WORD],)),
               _rung(VOCAB, 30, (FAMILY_BY_MILESTONE[VOCAB],))),
        unmappable=(RungTarget(domain_key=DOMAIN, source_rung_months=30,
                               milestone_text=PRONOUNS),))

    def ref(milestone, months):
        return compute_rung_ref(DOMAIN, months, milestone)

    states = {BOOK: "not_demonstrated", TWO_WORD: "demonstrated",
              VOCAB: "demonstrated", PRONOUNS: "unknown"}
    evidence = [ProjectedSkillEvidence(rung_ref=ref(AT_24, 24), months=24,
                                       state="demonstrated")]
    evidence += [ProjectedSkillEvidence(rung_ref=ref(m, 30), months=30,
                                        state=s) for m, s in states.items()]
    repos.parent_baseline_projections_v2.create(
        ParentBaselineProjectionV2.build(
            child_id=child_id, source_session_id=session_id,
            source_record_digest="b" * 64,
            summary={"domain": DOMAIN, "area_id": "talking",
                     "entry_choice_id": "two_three_words",
                     "routing_anchor_months": 24,
                     "not_demonstrated_months": 30, "status": "BOUNDED",
                     "baseline_version": BASELINE_VERSION_V2},
            skill_evidence=tuple(evidence),
            band_totals=(ProjectedBandTotal(months=24, total_skills=1),
                         ProjectedBandTotal(months=30, total_skills=4)),
            now=NOW))

    recorder = AuditRecorder(repos.audit_events, environment="test")
    goals = GoalService(repos=repos, recorder=recorder, now=lambda: NOW)
    principal = resolve_principal(
        VerifiedToken(subject=topo.provider_alpha.auth_subject), repos)

    # The REAL chain: F-B v2 generates, Hannah approves verbatim.
    generated = BaselineSuggestionGenerationV2Service(
        repos=repos, goals=goals, rung_source=rung_source
    ).generate_for_child(principal, child_id)
    assert len(generated.generated) == 1, generated
    goal = goals.approve_clinical_goal(
        principal, child_id, edit_type=EditType.ACCEPTED_VERBATIM,
        suggestion_id=generated.generated[0].suggestion_ids[0])

    plans = MonthlyPlanService(repos=repos, recorder=recorder,
                               now=lambda: NOW)
    weekly = WeeklyService(repos=repos, recorder=recorder, now=lambda: NOW)

    class Bundle:
        pass

    bundle = Bundle()
    bundle.repos, bundle.child_id, bundle.principal = repos, child_id, principal
    bundle.goal, bundle.goals, bundle.weekly = goal, goals, weekly
    bundle.service = lambda: WeekOneReleaseService(
        repos=repos, goals=goals, plans=plans, weekly=weekly,
        activity_bank=StaticActivityBank(), now=lambda: NOW)
    return bundle


def _rows(world, collection, **match):
    out = []
    for _doc_id, data in world.repos.store.list_all(collection):
        if all(data.get(key) == value for key, value in match.items()):
            out.append(data)
    return out


def test_eight_concurrent_releases_produce_exactly_one_released_week(world):
    """The whole contention claim, against a real server."""
    barrier = threading.Barrier(WRITERS)
    results, failures = [], []

    def release(_index):
        barrier.wait(timeout=30)
        try:
            return world.service().release_week_one(
                world.principal, world.child_id,
                family_declared_capacity=CAPACITY,
                timezone_of_record=TZ, cycle_month=CYCLE_MONTH)
        except Exception as exc:        # recorded, never swallowed
            failures.append(f"{type(exc).__name__}: {exc}")
            return None

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        results = list(pool.map(release, range(WRITERS)))

    # A stalled harness must report itself rather than return a short list.
    assert len(results) == WRITERS, results
    winners = [r for r in results if r is not None]
    assert winners, f"every writer failed: {failures}"

    cycles = _rows(world, "pilot_weekly_cycles", child_id=world.child_id)
    ledgers = _rows(world, "pilot_capacity_ledgers", child_id=world.child_id)
    snapshots = [d for _i, d in world.repos.store.list_all(
        "pilot_weekly_plan_snapshots")
        if d.get("cycle_id") == cycles[0]["cycle_id"]]
    alignments = _rows(world, "pilot_activity_goal_alignments",
                       child_id=world.child_id)

    print(f"\n=== after {WRITERS}-way contention ===")
    print(f"  writers succeeded / refused : {len(winners)} / "
          f"{len(failures)}")
    print(f"  weekly cycles               : {len(cycles)}  "
          f"{cycles[0]['cycle_id']}")
    print(f"  sequence / bounds           : {cycles[0]['sequence_in_month']}  "
          f"{cycles[0]['starts_on']} -> {cycles[0]['ends_on']}")
    print(f"  capacity ledgers            : {len(ledgers)}  "
          f"declared={ledgers[0]['family_declared_capacity']}")
    print(f"  weekly plan snapshots       : {len(snapshots)}  "
          f"{snapshots[0]['snapshot_id']}")
    print(f"  activity goal alignments    : {len(alignments)}")
    print(f"  released cycles             : "
          f"{sum(1 for c in cycles if c.get('released_to_parent_at'))}")
    if failures:
        print(f"  refusals (expected, losers) : {sorted(set(failures))[:3]}")

    # ONE of everything.
    assert len(cycles) == 1, [c["cycle_id"] for c in cycles]
    assert len(ledgers) == 1
    assert len(snapshots) == 1
    assert len(alignments) == CAPACITY
    assert sum(1 for c in cycles if c.get("released_to_parent_at")) == 1
    assert ledgers[0]["family_declared_capacity"] == CAPACITY
    assert ledgers[0]["allocated_by_planner"] == CAPACITY

    # No orphans: alignments and the ledger name the one cycle, and every
    # alignment belongs to the approved goal.
    assert {a["cycle_id"] for a in alignments} == {cycles[0]["cycle_id"]}
    assert ledgers[0]["cycle_id"] == cycles[0]["cycle_id"]
    assert {a["goal_id"] for a in alignments} == {
        world.goal.clinical_goal_id}

    # Every successful caller converges on the SAME released week, and exactly
    # one reports having created it.
    assert len({w.cycle_id for w in winners}) == 1
    assert len({w.snapshot_id for w in winners}) == 1
    assert all(w.activity_count == CAPACITY for w in winners)
    first = winners[0].parent_week
    assert all(w.parent_week == first for w in winners)
    assert sum(1 for w in winners if w.created) <= 1

    # The current cycle, not a past one.
    assert cycles[0]["sequence_in_month"] == 2
    assert cycles[0]["starts_on"] == "2026-10-05"
    assert cycles[0]["ends_on"] == "2026-10-11"


def test_a_concurrent_replay_after_release_adds_nothing(world):
    """The production shape: one release, then eight opens of the child."""
    world.service().release_week_one(
        world.principal, world.child_id, family_declared_capacity=CAPACITY,
        timezone_of_record=TZ, cycle_month=CYCLE_MONTH)
    baseline = (len(_rows(world, "pilot_weekly_cycles",
                          child_id=world.child_id)),
                len(_rows(world, "pilot_capacity_ledgers",
                          child_id=world.child_id)))

    barrier = threading.Barrier(WRITERS)

    def replay(_index):
        barrier.wait(timeout=30)
        return world.service().release_week_one(
            world.principal, world.child_id,
            family_declared_capacity=CAPACITY,
            timezone_of_record=TZ, cycle_month=CYCLE_MONTH)

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        outcomes = list(pool.map(replay, range(WRITERS)))

    assert len(outcomes) == WRITERS
    assert all(o.created is False for o in outcomes)
    assert len({o.snapshot_id for o in outcomes}) == 1
    assert (len(_rows(world, "pilot_weekly_cycles",
                      child_id=world.child_id)),
            len(_rows(world, "pilot_capacity_ledgers",
                      child_id=world.child_id))) == baseline


def test_a_concurrent_replay_with_a_different_capacity_cannot_rewrite_it(world):
    """A released week is frozen; a later capacity belongs to a future cycle."""
    first = world.service().release_week_one(
        world.principal, world.child_id, family_declared_capacity=CAPACITY,
        timezone_of_record=TZ, cycle_month=CYCLE_MONTH)

    barrier = threading.Barrier(WRITERS)

    def replay(index):
        barrier.wait(timeout=30)
        return world.service().release_week_one(
            world.principal, world.child_id,
            family_declared_capacity=1 + (index % 5),
            timezone_of_record=TZ, cycle_month=CYCLE_MONTH)

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        outcomes = list(pool.map(replay, range(WRITERS)))

    assert all(o.created is False for o in outcomes)
    assert all(o.activity_count == CAPACITY for o in outcomes)
    assert all(o.parent_week == first.parent_week for o in outcomes)
    ledgers = _rows(world, "pilot_capacity_ledgers", child_id=world.child_id)
    assert len(ledgers) == 1
    assert ledgers[0]["family_declared_capacity"] == CAPACITY
