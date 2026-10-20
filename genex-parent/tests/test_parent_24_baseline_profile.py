"""Slice 5 — Channel-B profile routing and fill-to-six.

Canonical design: `pilot_runtime/docs/parent_baseline_v2_design.md`.

The gates this file installs permanently:

  * the FIRST probe in an area is at or below chronological age — never age+6
  * concern or uncertainty descends with NO lower age bound
  * demonstrated age-appropriate evidence may ascend, never past the guardrail
  * an above-age `no` cannot create a concern
  * an area with no age-appropriate canonical skill stays `not_assessed`
  * Channel B adds NO evidence to Channel A
  * the A2 v2 clinical payload is byte-identical with and without profile
    evidence, and the body key set does not grow
  * normal collection stops at six questions, Channel A and B counted together
"""

from __future__ import annotations

import copy
import json

import pytest

from api import baseline_eligibility as E
from api import baseline_policy as P
from api import baseline_profile as B
from api import baseline_traversal as T
from api import functional_baseline_v2_api as api
from api import parent_baseline_projection_v2_client as pv2
from genex_core import functional_baseline as v1
from genex_core import functional_baseline_v2 as v2

AREA = "talking"
DOMAIN = "talking_and_communicating"
POLICY = P.TALKING_V2_POLICY_2

#: Measured from the frozen workbook. Asserted below so a sheet edit that
#: changes the shape of this slice's problem fails here rather than silently
#: changing routing.
EXPECTED_INVENTORY = {
    B.AREA_RECEPTIVE: [2, 4, 12, 15, 15, 18, 24, 24, 60],
    B.AREA_FUNCTIONAL: [9, 12, 15, 24],
    B.AREA_SOCIAL: [36, 48, 48, 60, 60],
    B.AREA_INTELLIGIBILITY: [36],
}

#: The BANDS those skills collapse to. Several areas carry two milestones at one
#: band and Channel B probes a band once — see `band_representative`.
EXPECTED_BANDS = {
    B.AREA_RECEPTIVE: (2, 4, 12, 15, 18, 24, 60),
    B.AREA_FUNCTIONAL: (9, 12, 15, 24),
    B.AREA_SOCIAL: (36, 48, 60),
    B.AREA_INTELLIGIBILITY: (36,),
}


def _record(age, choice="two_three_words"):
    return v2.start_baseline_v2(AREA, choice, age)


def _drive(age, channel_a_spent, answer_for, *, choice="two_three_words",
           future=False, limit=16):
    """Run normal Channel-B collection to its stop. Returns (record, total, trace)."""
    record = _record(age, choice)
    asked = int(channel_a_spent)
    trace = []
    for _ in range(limit):
        step = B.plan_profile_step(record, policy=POLICY, asked_total=asked,
                                   allow_future_only_probe=future)
        if step.kind == B.STOP:
            trace.append(("STOP", step.reason))
            return record, asked, trace
        question = step.question
        answer = answer_for(question)
        trace.append((step.profile_area, question["months"],
                      step.probe_reason, answer))
        v2.record_profile_probe_v2(record, question, answer)
        asked += 1
    raise AssertionError("normal profile collection did not terminate")


def _asks(trace):
    return [t for t in trace if t[0] != "STOP"]


# ---------------------------------------------------------------------------
# 0. the inventory this slice routes over
# ---------------------------------------------------------------------------

def test_the_canonical_profile_inventory_is_what_the_routing_assumes():
    for profile_area, months in EXPECTED_INVENTORY.items():
        found = [p.months for p in B.area_probes(AREA, profile_area)]
        assert found == months, profile_area
        assert B.area_bands(AREA, profile_area) == \
            EXPECTED_BANDS[profile_area], profile_area


def test_a_profile_area_is_never_a_declared_track_subdomain():
    """The structural reason a profile answer cannot complete a Channel-A band."""
    track = set(v1.get_area(AREA).track_subdomains)
    assert not (set(B.AREA_SUBDOMAIN.values()) & track)


def test_an_unknown_profile_area_is_refused():
    with pytest.raises(B.BaselineProfileError):
        B.area_probes(AREA, "pragmatics")


# ---------------------------------------------------------------------------
# 1. THE CORRECTION: the first probe is at or below chronological age
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("age,profile_area,expected", [
    (24, B.AREA_RECEPTIVE, 24),
    (30, B.AREA_RECEPTIVE, 24),     # not 60; and the area has nothing at 30
    (36, B.AREA_FUNCTIONAL, 24),    # gestural tops out at 24
    (12, B.AREA_RECEPTIVE, 12),     # exactly at age is age-appropriate
    (11, B.AREA_RECEPTIVE, 4),
    (36, B.AREA_SOCIAL, 36),
    (60, B.AREA_SOCIAL, 60),
])
def test_the_first_probe_is_the_nearest_skill_at_or_below_age(
        age, profile_area, expected):
    probe = B.first_probe(AREA, profile_area, chronological_months=age)
    assert probe is not None and probe.months == expected


@pytest.mark.parametrize("age,profile_area", [
    (24, B.AREA_SOCIAL),            # conversation_narrative starts at 36
    (30, B.AREA_SOCIAL),            # 30+6 == 36 — the age+6 reading's trap
    (24, B.AREA_INTELLIGIBILITY),
    (8, B.AREA_FUNCTIONAL),         # gestural starts at 9
    (1, B.AREA_RECEPTIVE),
])
def test_no_first_probe_exists_when_the_area_begins_above_age(age,
                                                              profile_area):
    assert B.first_probe(AREA, profile_area, chronological_months=age) is None


def test_the_first_probe_is_not_the_age_plus_six_choice():
    """THE HEADLINE. 30 months: age+6 lands exactly on the 36m social rung.

    An above-age probe is inert on failure by the frozen above-age rule, so
    spending a scarce normal slot there cannot tell us whether the area is
    concerning. The planner must refuse it during normal collection.
    """
    age = 30
    ceiling = POLICY.ascent_ceiling_months(age)
    assert ceiling == 36
    within_guardrail = [p.months for p in B.area_probes(AREA, B.AREA_SOCIAL)
                        if p.months <= ceiling]
    assert within_guardrail == [36], "age+6 really does reach a social rung"

    record, total, trace = _drive(age, 2, lambda q: "yes")
    assert all(months <= age for _, months, _, _ in _asks(trace)), trace
    assert B.area_status(record, B.AREA_SOCIAL) == B.NOT_ASSESSED
    # And it stopped with slots still unspent rather than reaching upward.
    assert total < POLICY.normal_question_target
    assert trace[-1] == ("STOP", B.STOP_NO_COVERAGE_AVAILABLE)


def test_normal_routing_issues_no_above_age_first_probe_at_any_age():
    """Swept across every age the entry choices can produce."""
    for age in range(1, 73):
        record, _total, trace = _drive(age, 0, lambda q: "not_sure")
        for profile_area, months, probe_reason, _answer in _asks(trace):
            if probe_reason == B.PROBE_FIRST:
                assert months <= age, (age, profile_area, months)


# ---------------------------------------------------------------------------
# 2. downward clarification has no lower age bound
# ---------------------------------------------------------------------------

def test_concern_descends_below_the_childs_age_without_a_floor():
    record, total, trace = _drive(24, 0, lambda q: "no")
    receptive = [months for area, months, _, _ in _asks(trace)
                 if area == B.AREA_RECEPTIVE]
    assert receptive[0] == 24
    assert receptive[1:], "a concern must be able to descend"
    assert all(b < a for a, b in zip(receptive, receptive[1:])), receptive


def test_uncertainty_descends_exactly_as_concern_does():
    """`not_sure` is not evidence of absence, but a lower question may be answerable."""
    record, _total, trace = _drive(48, 0, lambda q: "not_sure")
    reasons = {r for _a, _m, r, _ans in _asks(trace)}
    assert B.PROBE_DOWNWARD in reasons
    for area, months, probe_reason, _ in _asks(trace):
        if probe_reason == B.PROBE_DOWNWARD:
            assert months < 48


def test_the_descent_is_unbounded_all_the_way_to_the_bottom_of_the_area():
    """No chronological floor: with budget, receptive descends 24 -> 2."""
    record = _record(24)
    months = 24
    seen = []
    for _ in range(12):
        probe = B.downward_probe(AREA, B.AREA_RECEPTIVE, below_months=months,
                                 asked_months=seen)
        if probe is None:
            break
        seen.append(probe.months)
        months = probe.months
    assert seen == [18, 15, 12, 4, 2], seen


def test_downward_probe_takes_no_chronological_argument():
    """Structural: it has no parameter a floor could be derived from."""
    import inspect

    params = set(inspect.signature(B.downward_probe).parameters)
    assert "chronological_months" not in params
    assert "lookahead_months" not in params


def test_an_above_age_failure_does_not_trigger_a_descent():
    """Direction must be driven by age-appropriate evidence only."""
    record = _record(12)
    probe = B.first_probe(AREA, B.AREA_RECEPTIVE, chronological_months=12)
    v2.record_profile_probe_v2(record, B.probe_question(record, probe), "yes")
    upward = B.upward_probe(AREA, B.AREA_RECEPTIVE, above_months=12,
                            chronological_months=12, lookahead_months=6)
    assert upward is not None and upward.months == 15
    v2.record_profile_probe_v2(record, B.probe_question(record, upward), "no")
    assert B.area_direction(record, B.AREA_RECEPTIVE,
                            chronological_months=12) == B.ASCEND


# ---------------------------------------------------------------------------
# 3. the optional upward probe, and the guardrail that bounds it
# ---------------------------------------------------------------------------

def test_demonstrated_age_appropriate_evidence_may_probe_upward():
    """Non-vacuity: an ascent really does happen on the real inventory."""
    record, _total, trace = _drive(12, 1, lambda q: "yes")
    upward = [(a, m) for a, m, r, _ in _asks(trace) if r == B.PROBE_UPWARD]
    assert upward, trace
    assert all(m > 12 for _a, m in upward)


def test_no_probe_ever_exceeds_the_guardrail():
    for age in range(1, 73):
        ceiling = POLICY.ascent_ceiling_months(age)
        record, _total, trace = _drive(age, 0, lambda q: "yes", future=True)
        for area, months, probe_reason, _ in _asks(trace):
            assert months <= ceiling, (age, area, months, ceiling)


def test_the_guardrail_is_inclusive_and_refuses_one_month_past():
    """12m child: 18 is reachable, and nothing beyond it is."""
    probe = B.upward_probe(AREA, B.AREA_RECEPTIVE, above_months=15,
                           chronological_months=12, lookahead_months=6)
    assert probe is not None and probe.months == 18
    assert B.upward_probe(AREA, B.AREA_RECEPTIVE, above_months=18,
                          chronological_months=12, lookahead_months=6) is None
    # 60 exists in the area but sits far past 12+6.
    assert 60 in EXPECTED_INVENTORY[B.AREA_RECEPTIVE]


def test_an_area_with_concern_never_ascends():
    record = _record(24)
    probe = B.first_probe(AREA, B.AREA_RECEPTIVE, chronological_months=24)
    v2.record_profile_probe_v2(record, B.probe_question(record, probe), "no")
    assert B.area_direction(record, B.AREA_RECEPTIVE,
                            chronological_months=24) == B.DESCEND


# ---------------------------------------------------------------------------
# 4. an above-age `no` cannot create a concern
# ---------------------------------------------------------------------------

def test_an_above_age_no_creates_no_concern_in_the_profile():
    """The frozen above-age rule, applied to Channel B.

    A 12-month-old who demonstrates at 12 and fails every strength probe above
    age must come out of the profile with zero concerns.
    """
    record, _total, trace = _drive(
        12, 1, lambda q: "yes" if q["months"] <= 12 else "no")
    above = [e for e in record.profile_probes.values() if e.months > 12]
    assert above, "this path must actually probe above age"
    assert all(e.state == v2.STATE_NOT_DEMONSTRATED for e in above)

    summary = B.profile_summary(record, policy=POLICY)
    assert summary["areas_with_concern"] == []
    for area in summary["areas"].values():
        assert area["concern"] is False
        assert area["concern_bands"] == []


def test_the_profile_uses_the_one_eligibility_predicate():
    """It must consume `baseline_eligibility`, not restate the age rule."""
    import ast
    import pathlib

    src = pathlib.Path(B.__file__).read_text()
    modules = {n.module for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.ImportFrom)}
    assert "api" in modules or any("eligibility" in (m or "")
                                   for m in modules)
    assert "baseline_eligibility" in src
    for needle in ("def _classify", '"yes":', "'yes':", "def is_above_age"):
        assert needle not in src, needle


def test_an_above_age_strength_is_still_reported_as_a_strength():
    record, _total, _trace = _drive(12, 1, lambda q: "yes")
    summary = B.profile_summary(record, policy=POLICY)
    strengths = [b for a in summary["areas"].values()
                 for b in a["strength_bands"]]
    assert strengths and all(b > 12 for b in strengths)


# ---------------------------------------------------------------------------
# 5. no age-appropriate skill -> the area stays not_assessed
# ---------------------------------------------------------------------------

def test_social_stays_not_assessed_for_a_child_below_the_inventorys_start():
    """The founder's example, on real data: conversation_narrative starts at 36."""
    for age in (18, 24, 30, 35):
        record, _total, _trace = _drive(age, 0, lambda q: "yes")
        assert B.area_status(record, B.AREA_SOCIAL) == B.NOT_ASSESSED, age
        summary = B.profile_summary(record, policy=POLICY)
        assert B.AREA_SOCIAL in summary["areas_not_assessed"]
        assert summary["areas"][B.AREA_SOCIAL]["age_appropriate_inventory"] == 0


def test_not_assessed_is_never_reported_as_clear():
    """An unprobed area must not be readable as reassurance."""
    record, _total, _trace = _drive(24, 0, lambda q: "yes")
    summary = B.profile_summary(record, policy=POLICY)
    social = summary["areas"][B.AREA_SOCIAL]
    assert social["status"] == B.NOT_ASSESSED
    assert social["assessed"] is False
    assert social["probes"] == 0
    assert social["lowest_probe_months"] is None
    # The status vocabulary admits no third "clear" value.
    assert set(a["status"] for a in summary["areas"].values()) <= \
        {B.ASSESSED, B.NOT_ASSESSED}
    blob = json.dumps(summary)
    for forbidden in ("clear", "no_concern", "normal", "typical", "passed"):
        assert forbidden not in blob, forbidden


def test_coverage_is_never_manufactured_during_normal_collection():
    """A future-only area is skipped even with slots to spare."""
    record, total, trace = _drive(24, 0, lambda q: "yes")
    assert B.area_status(record, B.AREA_SOCIAL) == B.NOT_ASSESSED
    assert B.area_status(record, B.AREA_INTELLIGIBILITY) == B.NOT_ASSESSED
    assert all(m <= 24 for _a, m, _r, _ in _asks(trace))


def test_a_future_only_probe_requires_an_explicit_opt_in():
    """Rule 4's escape hatch exists, is off by default, and is Slice 6's call.

    Driven to the point where normal routing has genuinely run out of
    age-appropriate coverage but still holds unspent slots — the only situation
    in which the question is live.
    """
    record, total, trace = _drive(30, 2, lambda q: "yes")
    assert trace[-1] == ("STOP", B.STOP_NO_COVERAGE_AVAILABLE)
    assert POLICY.profile_slots_for(total) > 0, "slots must remain unspent"

    assert B.plan_profile_step(record, policy=POLICY,
                               asked_total=total).kind == B.STOP
    step = B.plan_profile_step(record, policy=POLICY, asked_total=total,
                               allow_future_only_probe=True)
    assert step.kind == B.ASK
    assert step.profile_area == B.AREA_SOCIAL
    assert step.question["months"] == 36
    assert step.probe_reason == B.PROBE_UPWARD


# ---------------------------------------------------------------------------
# 6. Channel B adds no evidence to Channel A
# ---------------------------------------------------------------------------

def test_profile_probes_never_enter_the_skills_mapping():
    record, _total, trace = _drive(36, 0, lambda q: "no")
    assert _asks(trace)
    assert record.skills == {}
    assert record.profile_probes


def test_a_profile_answer_cannot_complete_or_master_a_channel_a_band():
    record = _record(24)
    record.bands_entered.append(24)
    before = v2.band_assessment(record, 24)
    for _ in range(4):
        step = B.plan_profile_step(record, policy=POLICY, asked_total=0)
        if step.kind == B.STOP:
            break
        v2.record_profile_probe_v2(record, step.question, "yes")
    after = v2.band_assessment(record, 24)
    assert (after.assessment_complete, after.band_mastered) == \
        (before.assessment_complete, before.band_mastered)
    assert before.assessment_complete is False


def test_profile_evidence_cannot_produce_a_channel_a_target():
    record = _record(24)
    for _ in range(6):
        step = B.plan_profile_step(record, policy=POLICY, asked_total=0)
        if step.kind == B.STOP:
            break
        v2.record_profile_probe_v2(record, step.question, "no")
    assert T.candidate_target_band(record) is None
    assert E.eligible_target_band(record) is None


def test_the_two_recorders_write_to_two_different_mappings():
    """Structural: there is no flag that could promote a profile answer."""
    import inspect

    for fn in (v2.record_answer_v2, v2.record_profile_probe_v2):
        params = set(inspect.signature(fn).parameters)
        assert params == {"record", "question", "answer"}, fn
    assert "record.skills[" in inspect.getsource(v2.record_answer_v2)
    assert "record.skills[" not in inspect.getsource(v2.record_profile_probe_v2)


def test_a_profile_question_carries_no_activity_family():
    """A profile probe must not look like something a goal could be built from."""
    record = _record(36)
    step = B.plan_profile_step(record, policy=POLICY, asked_total=0)
    assert step.kind == B.ASK
    assert "activity_family" not in step.question
    assert step.question["channel"] == "profile"


# ---------------------------------------------------------------------------
# 7. the A2 v2 payload
# ---------------------------------------------------------------------------

def _finalized_doc(age=36):
    """A real finalized Channel-A session, through the public API."""
    doc = {"brain_state": {"child": {"chronological_months": age}},
           "age_in_months": age}
    api.start(doc, DOMAIN, "many_single_words")
    for _ in range(12):
        view = api.view(doc, DOMAIN)
        question = view.get("current_question")
        if not question:
            break
        api.answer(doc, DOMAIN, question["skill_key"], "yes")
    if not api.view(doc, DOMAIN).get("finalized"):
        api.finalize_baseline(doc, DOMAIN)
    return doc


def test_the_clinical_projection_payload_is_byte_identical():
    """THE GATE. Channel-B evidence must not reach A2 v2.

    `summary`, `skills` and `band_totals` are the whole clinical content of the
    body, and all three must be unchanged by any amount of profile evidence.
    """
    doc = _finalized_doc()
    before = copy.deepcopy(api.projection_payload(doc, DOMAIN))

    record = v2.record_from_state_v2(doc[v2.STATE_KEY_V2][DOMAIN])
    for _ in range(9):
        step = B.plan_profile_step(record, policy=POLICY, asked_total=0,
                                   allow_future_only_probe=True)
        if step.kind == B.STOP:
            break
        v2.record_profile_probe_v2(record, step.question, "no")
    assert record.profile_probes, "the comparison must not be vacuous"
    doc[v2.STATE_KEY_V2][DOMAIN] = v2.record_to_state_v2(record)

    after = api.projection_payload(doc, DOMAIN)
    for part in ("summary", "skills", "band_totals"):
        assert json.dumps(after[part], sort_keys=True) == \
            json.dumps(before[part], sort_keys=True), part
    assert json.dumps(after, sort_keys=True) == \
        json.dumps(before, sort_keys=True)


def test_the_projection_body_key_set_does_not_grow():
    doc = _finalized_doc()
    record = doc[v2.STATE_KEY_V2][DOMAIN]
    body = pv2.build_body(source_session_id="sess_fictional",
                          record=record,
                          payload=api.projection_payload(doc, DOMAIN))
    assert set(body) == set(pv2.BODY_KEYS)
    assert "profile_probes" not in json.dumps(body)
    assert "profile" not in json.dumps(body)


def test_a_record_without_profile_evidence_keeps_its_exact_source_digest():
    """Historical stability: the key is OMITTED when empty, so no digest moves."""
    doc = _finalized_doc()
    stored = doc[v2.STATE_KEY_V2][DOMAIN]
    assert "profile_probes" not in stored
    round_tripped = v2.record_to_state_v2(v2.record_from_state_v2(
        copy.deepcopy(stored)))
    assert json.dumps(round_tripped, sort_keys=True) == \
        json.dumps(stored, sort_keys=True)
    assert pv2.canonical_source_digest(round_tripped) == \
        pv2.canonical_source_digest(stored)


def test_profile_evidence_does_change_the_source_record_digest():
    """Deliberate, and documented: the digest is PROVENANCE over the record.

    A record carrying profile evidence genuinely is a different record, and a
    digest that ignored part of it would be a weaker integrity claim. This is
    safe because `/projection` is gated on the FINALIZED record — Channel B is
    always already recorded when the one projection is built, so a replay of
    the same session recomputes the same digest.
    """
    doc = _finalized_doc()
    before = pv2.canonical_source_digest(doc[v2.STATE_KEY_V2][DOMAIN])

    record = v2.record_from_state_v2(doc[v2.STATE_KEY_V2][DOMAIN])
    step = B.plan_profile_step(record, policy=POLICY, asked_total=0)
    assert step.kind == B.ASK
    v2.record_profile_probe_v2(record, step.question, "no")
    stored = v2.record_to_state_v2(record)

    assert pv2.canonical_source_digest(stored) != before
    # Stable thereafter — replay of the same finalized record is idempotent.
    assert pv2.canonical_source_digest(stored) == \
        pv2.canonical_source_digest(v2.record_to_state_v2(
            v2.record_from_state_v2(copy.deepcopy(stored))))


def test_the_profile_summary_leaks_no_child_data():
    record, _total, _trace = _drive(36, 0, lambda q: "no")
    blob = json.dumps(B.profile_summary(record, policy=POLICY))
    for forbidden in ("milestone", "skill_key", "child", "caregiver",
                      "says", "points", "follows"):
        assert forbidden not in blob, forbidden


# ---------------------------------------------------------------------------
# 8. normal collection stops at six, Channel A and B counted together
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("channel_a_spent,expected_profile_slots", [
    (0, 6), (1, 5), (2, 4), (3, 3), (4, 2), (5, 1), (6, 0), (7, 0), (9, 0)])
def test_the_founders_slot_arithmetic(channel_a_spent,
                                      expected_profile_slots):
    assert POLICY.profile_slots_for(channel_a_spent) == expected_profile_slots


def test_normal_collection_never_exceeds_six_total_questions():
    """Swept over every age and every Channel-A cost."""
    for age in range(1, 73):
        for spent in range(0, 10):
            _record_, total, trace = _drive(age, spent, lambda q: "no")
            if spent >= POLICY.normal_question_target:
                assert _asks(trace) == [], (age, spent)
                assert trace[-1] == ("STOP", B.STOP_NORMAL_BUDGET_SPENT)
            assert total <= max(spent, POLICY.normal_question_target), \
                (age, spent, total)


def test_channel_b_never_reaches_for_an_additional_question():
    """Q7-Q9 belong to Slice 6. This module must stop at the normal target."""
    for age in (12, 24, 36, 48, 60):
        _record_, total, trace = _drive(age, 0, lambda q: "no")
        assert total <= POLICY.normal_question_target, (age, total)
        assert total < POLICY.hard_question_max


def test_a_channel_a_traversal_at_the_normal_target_leaves_no_profile_slot():
    record = _record(24)
    step = B.plan_profile_step(record, policy=POLICY,
                               asked_total=POLICY.normal_question_target)
    assert step.kind == B.STOP
    assert step.reason == B.STOP_NORMAL_BUDGET_SPENT
    assert record.profile_probes == {}


def test_breadth_precedes_depth():
    """Every required, probeable area is opened before any gets a second question."""
    record, _total, trace = _drive(48, 0, lambda q: "no")
    asks = _asks(trace)
    first_depth = next((i for i, a in enumerate(asks)
                        if a[2] != B.PROBE_FIRST), len(asks))
    opened = {a[0] for a in asks[:first_depth]}
    probeable = {a for a in POLICY.required_profile_areas
                 if B.first_probe(AREA, a, chronological_months=48)}
    assert probeable <= opened, (probeable, opened)


def test_the_joint_budget_is_exercised_against_a_real_channel_a_traversal():
    """End to end: Channel A runs first, Channel B spends only what is left."""
    record = _record(24, "two_three_words")
    budget = T.InterviewBudget.from_policy(POLICY)
    asked = 0
    for _ in range(16):
        step = T.plan_next(
            record, lookahead_months=POLICY.normal_upper_lookahead_months,
            budget=budget.with_asked(asked))
        if step.kind == T.STOP:
            break
        if step.enters_band is not None:
            record.bands_entered.append(step.enters_band)
        v2.record_answer_v2(record, step.question, "yes")
        asked += 1
    channel_a = asked
    assert channel_a > 0

    for _ in range(16):
        step = B.plan_profile_step(record, policy=POLICY, asked_total=asked)
        if step.kind == B.STOP:
            break
        v2.record_profile_probe_v2(record, step.question, "yes")
        asked += 1

    assert asked <= POLICY.normal_question_target, (channel_a, asked)
    assert len(record.skills) == channel_a
    assert len(record.profile_probes) == asked - channel_a


# ---------------------------------------------------------------------------
# 9. determinism and policy independence
# ---------------------------------------------------------------------------

def test_routing_is_deterministic():
    runs = [_drive(36, 1, lambda q: "no")[2] for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


def test_changing_the_budget_changes_how_many_probes_not_what_they_mean():
    """The governing principle, on Channel B."""
    import dataclasses

    wider = dataclasses.replace(POLICY, policy_id="hypothetical",
                                normal_question_target=8, hard_question_max=9)
    narrow = _drive(48, 0, lambda q: "no")[2]

    record = _record(48)
    asked, trace = 0, []
    for _ in range(16):
        step = B.plan_profile_step(record, policy=wider, asked_total=asked)
        if step.kind == B.STOP:
            break
        trace.append((step.profile_area, step.question["months"],
                      step.probe_reason, "no"))
        v2.record_profile_probe_v2(record, step.question, "no")
        asked += 1

    assert len(trace) > len(_asks(narrow)), "a wider budget asks more"
    assert trace[:len(_asks(narrow))] == _asks(narrow), \
        "and asks the same questions in the same order first"


def test_an_unknown_fallback_tier_is_refused():
    import dataclasses

    broken = dataclasses.replace(POLICY, policy_id="hypothetical",
                                 profile_fallback_tiers=("prosody_plus",))
    with pytest.raises(B.BaselineProfileError):
        B.plan_profile_step(_record(36), policy=broken, asked_total=0)
