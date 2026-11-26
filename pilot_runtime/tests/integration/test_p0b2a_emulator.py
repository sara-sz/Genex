"""P0-B2a — constrained developmental recalibration on a REAL emulator.

What only a real server can prove: the P0-B1 explanation transaction now
writes THREE rows (record, decision, developmental movement) plus the cycle
link, READING the cycle first; and eight concurrent Week 2 builds of a placed
plan produce exactly one decision and exactly one movement, which moved the
child exactly one adjacent step.

A NEW fictional child per test (fresh ids in a shared emulator database);
every assertion is scoped to that child.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

WRITERS = 8
DOMAIN = "talking_and_communicating"
CYCLE_MONTH = "2026-10"
TZ = "America/New_York"
CAPACITY = 3
WEEK_TWO_DATE = "2026-10-14"
AT_24 = "says at least two words together like more milk"
BOOK = "name things in a book when you point and ask what is this"
TWO_WORD = "say two or more words together with one action word"
VOCAB = "says about 50 words"
PRONOUNS = "says words like i me or we"
BOTH = ("expressive_vocabulary_growth", "two_word_phrases")
TAXONOMY_VERSION = "activity_family_taxonomy_v1"
GOLD_STANDARD_VERSION = "parent-2.4-functional-baseline-v1"
BASELINE_VERSION_V2 = "parent-2.4-functional-baseline-v2"
TRACK_SUBDOMAINS = ("early_vocalization_and_babbling", "expressive_language")


class Clock:
    def __init__(self, day):
        self.set(day)

    def set(self, day):
        y, m, d = (int(p) for p in day.split("-"))
        self.value = datetime(y, m, d, 15, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


def _rung(milestone, months, families):
    from pilot_backend.domain.canonical_rung import (
        ActivityFamilyBinding,
        CanonicalRung,
    )
    return CanonicalRung.build(
        domain_key=DOMAIN, source_rung_months=months,
        milestone_text=milestone, subdomain="expressive_language",
        family_bindings=tuple(ActivityFamilyBinding(
            family_ref=f, allowed_domains=(DOMAIN,)) for f in families),
        track_subdomains=TRACK_SUBDOMAINS, track_families=(),
        taxonomy_version=TAXONOMY_VERSION,
        baseline_version=GOLD_STANDARD_VERSION)


@pytest.fixture()
def world(repos, unique_suffix):
    from pilot_backend.audit.recorder import AuditRecorder
    from pilot_backend.auth.interface import VerifiedToken
    from pilot_backend.auth.resolver import resolve_principal
    from pilot_backend.domain.canonical_rung import compute_rung_ref
    from pilot_backend.domain.content_authority import (
        ActivityBankContentAuthority,
    )
    from pilot_backend.domain.goals import EditType
    from pilot_backend.domain.managing_clinician import (
        ManagingClinicianAssignment,
    )
    from pilot_backend.domain.parent_baseline_projection_v2 import (
        ParentBaselineProjectionV2,
        ProjectedBandTotal,
        ProjectedSkillEvidence,
    )
    from pilot_backend.domain.roles import ActorRole
    from pilot_backend.domain.source_link import SourceSystem, SourceSystemLink
    from pilot_backend.fixtures.secure_topology import build_secure_topology
    from pilot_backend.goals.service import GoalService
    from pilot_backend.integration.baseline_suggestion_generation_v2 import (
        BaselineSuggestionGenerationV2Service,
    )
    from pilot_backend.integration.developmental_recalibration import (
        DevelopmentalRecalibrationService,
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
    from pilot_runtime.integration.static_developmental_ladder import (
        load_developmental_ladder,
    )

    clock = Clock("2026-10-07")
    topo = build_secure_topology(repos, now=clock(), subject_suffix=unique_suffix)
    child_id = topo.child_alpha.child_id
    session_id = f"sess-p0b2a{unique_suffix}"
    repos.managing_clinicians.create(ManagingClinicianAssignment.create(
        child_id, topo.provider_alpha.provider_id, topo.practice.practice_id,
        provider_connection_id=topo.link_alpha_provider.connection_id,
        actor_id=topo.caregiver_alpha.caregiver_id, now=clock()))
    repos.source_links.create(SourceSystemLink.create(
        child_id, SourceSystem.PARENT, session_id, actor_id="fixture",
        actor_role=ActorRole.CAREGIVER.value, now=clock()))
    source = InMemoryGoldStandardRungSource(
        rungs=(_rung(AT_24, 24, BOTH),
               _rung(BOOK, 30, ("book_object_naming",)),
               _rung(TWO_WORD, 30, ("two_word_phrases",)),
               _rung(VOCAB, 30, ("expressive_vocabulary_growth",))),
        unmappable=(RungTarget(domain_key=DOMAIN, source_rung_months=30,
                               milestone_text=PRONOUNS),))

    def ref(m, months):
        return compute_rung_ref(DOMAIN, months, m)

    # The P0-B1 positive-path shape: 24m EMERGING, the 30m band demonstrated,
    # so F-B v2 targets the 24m milestone the ladder is anchored on.
    states = {BOOK: "demonstrated", TWO_WORD: "demonstrated",
              VOCAB: "demonstrated", PRONOUNS: "demonstrated"}
    evidence = [ProjectedSkillEvidence(rung_ref=ref(AT_24, 24), months=24,
                                       state="emerging")]
    evidence += [ProjectedSkillEvidence(rung_ref=ref(m, 30), months=30, state=s)
                 for m, s in states.items()]
    repos.parent_baseline_projections_v2.create(ParentBaselineProjectionV2.build(
        child_id=child_id, source_session_id=session_id,
        source_record_digest="c" * 64,
        summary={"domain": DOMAIN, "area_id": "talking",
                 "entry_choice_id": "two_three_words",
                 "routing_anchor_months": 24, "not_demonstrated_months": 30,
                 "status": "BOUNDED", "baseline_version": BASELINE_VERSION_V2},
        skill_evidence=tuple(evidence),
        band_totals=(ProjectedBandTotal(months=24, total_skills=1),
                     ProjectedBandTotal(months=30, total_skills=4)),
        now=clock()))

    bank = StaticActivityBank()
    ladder = load_developmental_ladder(activity_bank=bank)
    recorder = AuditRecorder(repos.audit_events, environment="test")
    goals = GoalService(repos=repos, recorder=recorder, now=clock,
                        content_authority=ActivityBankContentAuthority(bank))
    provider = resolve_principal(
        VerifiedToken(subject=topo.provider_alpha.auth_subject), repos)
    caregiver = resolve_principal(
        VerifiedToken(subject=topo.caregiver_alpha.auth_subject), repos)
    generated = BaselineSuggestionGenerationV2Service(
        repos=repos, goals=goals, rung_source=source
    ).generate_for_child(provider, child_id)
    goal = goals.approve_clinical_goal(
        provider, child_id, edit_type=EditType.ACCEPTED_VERBATIM,
        suggestion_id=generated.generated[0].suggestion_ids[0])
    plans = MonthlyPlanService(repos=repos, recorder=recorder, now=clock)
    weekly = WeeklyService(repos=repos, recorder=recorder, now=clock)
    recal = DevelopmentalRecalibrationService(
        repos=repos, goals=goals, plans=plans, weekly=weekly, ladder=ladder,
        now=clock)

    plan = plans.create_plan(provider, child_id, CYCLE_MONTH, TZ)
    plans.allocate_goal(provider, plan.focus_plan_id, goal.ref,
                        priority_rank=1)
    recal.place(provider, plan.focus_plan_id, "step_3")     # Bridge 2
    plans.activate_plan(provider, plan.focus_plan_id)

    class Bundle:
        pass

    w = Bundle()
    w.repos, w.child_id, w.provider, w.caregiver = repos, child_id, provider, caregiver
    w.weekly, w.clock, w.ladder, w.plan = weekly, clock, ladder, plan
    w.service = lambda: WeekOneReleaseService(
        repos=repos, goals=goals, plans=plans, weekly=weekly,
        activity_bank=bank, now=clock, ladder=ladder)
    w.week_one = _release(w)

    # Repeated too_easy on ONE Week 1 activity, two different practice dates.
    from pilot_backend.domain.observation import AttemptOutcome, Difficulty
    refs = {a["title"]: a["activity_ref"]
            for d in w.week_one.parent_week["days"] for a in d["activities"]}
    title = sorted(refs)[0]
    for i, day in enumerate(("2026-10-07", "2026-10-08")):
        clock.set(day)
        instance = weekly.resolve_parent_activity_ref(
            caregiver, w.week_one.cycle_id, refs[title])
        weekly.record_observation(
            caregiver, w.week_one.cycle_id, instance, local_date=day,
            attempt_outcome=AttemptOutcome.DID_IT,
            difficulty=Difficulty.TOO_EASY,
            source_feedback_id=f"fb_p0b2a{unique_suffix}_{i}")
    clock.set(WEEK_TWO_DATE)
    return w


def _release(world, local_date=None):
    return world.service().release_week_one(
        world.provider, world.child_id, family_declared_capacity=CAPACITY,
        cycle_month=CYCLE_MONTH, local_date=local_date)


def _rows(world, collection, **match):
    return [d for _i, d in world.repos.store.list_all(collection)
            if all(d.get(k) == v for k, v in match.items())]


def _assert_one_moved_week_two(world):
    cycles = [c for c in _rows(world, "pilot_weekly_cycles",
                               child_id=world.child_id)
              if int(c["sequence_in_month"]) == 2]
    assert len(cycles) == 1, cycles
    cid = cycles[0]["cycle_id"]
    decisions = _rows(world, "pilot_weekly_adaptation_decisions", to_cycle_id=cid)
    movements = _rows(world, "pilot_developmental_movements",
                      child_id=world.child_id)
    assert len(decisions) == 1 and len(movements) == 1, (decisions, movements)
    m = movements[0]
    assert m["to_cycle_id"] == cid and m["outcome"] == "moved"
    assert (m["node_before_index"], m["node_after_index"]) == (2, 3)
    assert cycles[0]["released_to_parent_at"]
    (snap,) = _rows(world, "pilot_weekly_plan_snapshots", cycle_id=cid)
    pool = set(world.ladder.by_step_key("step_4").pool_template_ids)
    import json
    placed = {a["activity_template_id"] for a in
              json.loads(snap["resolved_plan_document"])["activities"]}
    assert placed and placed <= pool, "only the new node's reviewed pool"
    return m


def test_a_placed_week_two_moves_once_on_a_real_server(world):
    outcome = _release(world, WEEK_TWO_DATE)
    assert outcome.created is True
    m = _assert_one_moved_week_two(world)
    assert len(m["evidence_event_ids"]) == 2
    assert _release(world, WEEK_TWO_DATE).created is False
    _assert_one_moved_week_two(world)


def test_an_interrupted_placed_week_two_resumes_with_one_movement(world):
    original = world.weekly.record_week_adaptation
    armed = {"on": True}

    def interrupted(*args, **kwargs):
        result = original(*args, **kwargs)
        if armed["on"]:
            armed["on"] = False
            raise RuntimeError("injected after the explanation committed")
        return result

    world.weekly.record_week_adaptation = interrupted
    with pytest.raises(RuntimeError):
        _release(world, WEEK_TWO_DATE)
    world.weekly.record_week_adaptation = original
    (before,) = _rows(world, "pilot_developmental_movements",
                      child_id=world.child_id)
    world.clock.set("2026-10-15")          # a later retry stamps differently
    assert _release(world, WEEK_TWO_DATE).created is True
    after = _assert_one_moved_week_two(world)
    assert after == before, "resume reused the committed movement"


def test_eight_concurrent_placed_week_two_builds_move_exactly_once(world):
    barrier = threading.Barrier(WRITERS)
    failures = []

    def release(_i):
        barrier.wait(timeout=30)
        try:
            return _release(world, WEEK_TWO_DATE)
        except Exception as exc:        # recorded, never swallowed
            failures.append(type(exc).__name__)
            return None

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        results = list(pool.map(release, range(WRITERS)))
    assert len(results) == WRITERS, results
    final = _release(world, WEEK_TWO_DATE)
    winners = [r for r in results if r is not None] + [final]
    assert len({r.cycle_id for r in winners}) == 1
    m = _assert_one_moved_week_two(world)
    print(f"\n=== P0-B2a {WRITERS}-way: winners={len(winners) - 1} "
          f"refused={sorted(set(failures))} movement={m['movement_id']} "
          f"{m['node_before_index']}->{m['node_after_index']}")
