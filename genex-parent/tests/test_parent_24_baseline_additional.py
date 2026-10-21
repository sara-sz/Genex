"""Slice 6 — the shared additional-question allowance (Q7-Q9).

Canonical design: `pilot_runtime/docs/parent_baseline_v2_design.md`.

The gates this file installs permanently:

  * no path reaches a tenth question, ever
  * Q7-Q9 are accounted in QUESTIONS, not reason events
  * target integrity (R1/R2) outranks profile completeness (R3/R4/R5)
  * there is no completeness reason — the planner stops when no reason applies
  * Q7 is never automatic: a clean interview ends at six
  * an area with no age-appropriate canonical skill is never recovered
  * every question beyond the normal target carries a deterministic reason code
  * the two `not_assessed` causes stay distinguishable
"""

from __future__ import annotations

import copy
import itertools
import json

import pytest

from api import baseline_additional as A
from api import baseline_eligibility as E
from api import baseline_policy as P
from api import baseline_profile as B
from api import baseline_traversal as T
from api import parent_baseline_projection_v2_client as pv2
from genex_core import functional_baseline as v1
from genex_core import functional_baseline_v2 as v2

AREA = "talking"
POLICY = P.TALKING_V2_POLICY_2
ANSWERS = ("yes", "sometimes", "no", "not_sure")
CHOICES = tuple(c.choice_id for c in v1.get_area(AREA).choices)
AGES = (9, 12, 18, 24, 30, 36, 48, 60)

#: Measured exemplars. Channel A alone, driven to its own stop under the joint
#: budget, costs exactly this many questions on these paths. Pinned so the
#: founder's A=5 / A=6 / A=7 / A=9 simulations are run against real
#: trajectories rather than hand-built records.
CHANNEL_A_COST = {
    5: ("no_words_yet", 18, ("yes", "yes", "yes")),
    6: ("no_words_yet", 9, ("sometimes", "yes", "yes")),
    7: ("no_words_yet", 9, ("sometimes", "yes", "sometimes")),
    9: ("no_words_yet", 24, ("yes", "yes", "yes")),
}


# ---------------------------------------------------------------------------
# the driver
# ---------------------------------------------------------------------------

def _answer_cycle(pattern):
    state = {"i": 0}

    def answer_for(_question):
        value = pattern[state["i"] % len(pattern)]
        state["i"] += 1
        return value

    return answer_for


def run_interview(choice, age, answer_for, *, policy=POLICY, limit=32):
    """Drive one whole interview through `plan_interview_step`.

    Returns `(record, asked_total, trace)`. The trace holds one tuple per
    question plus a final `("STOP", reason)`.
    """
    record = v2.start_baseline_v2(AREA, choice, age)
    asked = 0
    trace = []
    for _ in range(limit):
        step = A.plan_interview_step(record, policy=policy, asked_total=asked)
        if step.kind == A.STOP:
            trace.append(("STOP", step.reason))
            return record, asked, trace
        asked += 1
        answer = answer_for(step.question)
        if step.channel == v2.CHANNEL_A:
            if step.enters_band is not None:
                record.bands_entered.append(step.enters_band)
            v2.record_answer_v2(record, step.question, answer)
        else:
            v2.record_profile_probe_v2(record, step.question, answer)
        if step.reason_code:
            v2.record_additional_reason_v2(
                record, question_number=asked,
                reason_code=step.reason_code, channel=step.channel)
        trace.append((asked, step.channel, step.question["months"],
                      step.reason_code, step.profile_area, answer))
    raise AssertionError("the interview did not terminate")


def _asks(trace):
    return [t for t in trace if t[0] != "STOP"]


def _channel_a_only(choice, age, pattern, *, policy=POLICY):
    """Run Channel A to its own stop. Returns `(record, cost)`."""
    record = v2.start_baseline_v2(AREA, choice, age)
    asked = 0
    answer_for = _answer_cycle(pattern)
    for _ in range(32):
        step = A._channel_a_step(record, policy=policy, asked_total=asked)
        if step is None:
            return record, asked
        if step.enters_band is not None:
            record.bands_entered.append(step.enters_band)
        v2.record_answer_v2(record, step.question, answer_for(step.question))
        asked += 1
    raise AssertionError("Channel A did not terminate")


# ---------------------------------------------------------------------------
# 1. THE HARD MAXIMUM
# ---------------------------------------------------------------------------

def test_no_path_reaches_a_tenth_question():
    """Exhaustive over 6 choices x 8 ages x 64 answer patterns."""
    worst = 0
    for choice, age in itertools.product(CHOICES, AGES):
        for pattern in itertools.product(ANSWERS, repeat=3):
            _record, total, _trace = run_interview(
                choice, age, _answer_cycle(pattern))
            assert total <= POLICY.hard_question_max, (choice, age, pattern,
                                                       total)
            worst = max(worst, total)
    assert worst == POLICY.hard_question_max, (
        "non-vacuity: some path must actually reach the maximum")


def test_at_the_hard_maximum_the_planner_stops_regardless_of_uncertainty():
    record, total, trace = run_interview(
        *CHANNEL_A_COST[9][:2], _answer_cycle(CHANNEL_A_COST[9][2]))
    assert total == POLICY.hard_question_max
    assert trace[-1] == ("STOP", A.STOP_HARD_MAXIMUM_REACHED)
    step = A.plan_additional_step(record, policy=POLICY, asked_total=total)
    assert step.kind == A.STOP
    assert step.reason == A.STOP_HARD_MAXIMUM_REACHED
    # Uncertainty genuinely remains, and is preserved rather than collected.
    assert B.recoverable_areas(record, policy=POLICY), (
        "this path must leave a recoverable area unsampled")


@pytest.mark.parametrize("asked_total", [9, 10, 25])
def test_the_planner_refuses_at_or_beyond_the_maximum(asked_total):
    record = v2.start_baseline_v2(AREA, "two_three_words", 24)
    for planner in (A.plan_additional_step, A.plan_interview_step):
        step = planner(record, policy=POLICY, asked_total=asked_total)
        assert step.kind == A.STOP
        assert step.reason == A.STOP_HARD_MAXIMUM_REACHED


# ---------------------------------------------------------------------------
# 2. THE FOUNDER'S CHANNEL-A SIMULATIONS
# ---------------------------------------------------------------------------

def test_channel_a_cost_exemplars_are_still_what_this_suite_assumes():
    for expected_cost, (choice, age, pattern) in CHANNEL_A_COST.items():
        _record, cost = _channel_a_only(choice, age, pattern)
        assert cost == expected_cost, (choice, age, pattern, cost)


def test_channel_a_five_spends_q6_on_profile_and_does_not_reach_q7():
    """A=5 -> Q6 profile normally, NO automatic Q7.

    The whole reason R3 is gated on a difficulty having been found. Without
    that gate this path would recover a second required area at Q7 and Q7
    would be automatic on most A=5 and A=6 trajectories.
    """
    choice, age, pattern = CHANNEL_A_COST[5]
    record, total, trace = run_interview(choice, age, _answer_cycle(pattern))
    asks = _asks(trace)
    assert [t[1] for t in asks[:5]] == [v2.CHANNEL_A] * 5
    assert asks[5][1] == v2.CHANNEL_B
    assert asks[5][3] is None, "Q6 is within the normal target — no reason code"
    assert total == 6
    assert trace[-1] == ("STOP", A.STOP_NO_QUALIFYING_REASON)
    assert record.additional_reasons == []
    assert A.difficulty_found(record, policy=POLICY) is False


def test_channel_a_six_may_begin_recovery_at_q7_only_if_justified():
    """A=6 -> profile recovery at Q7 only when a difficulty exists.

    Both branches proved on the SAME Channel-A cost, so the difference is the
    justification and nothing else.
    """
    choice, age, pattern = CHANNEL_A_COST[6]
    record, cost = _channel_a_only(choice, age, pattern)
    assert cost == 6

    # Branch 1: Channel A found a difficulty on this path -> recovery is live.
    if A.difficulty_found(record, policy=POLICY):
        step = A.plan_additional_step(record, policy=POLICY, asked_total=6)
        assert step.kind == A.ASK
        assert step.reason_code in (A.R1_TARGET_BAND_COMPLETION,
                                    A.R2_TARGET_FLOOR_RESOLUTION,
                                    A.R3_PROFILE_AREA_RECOVERY)
    # Branch 2: an all-demonstrated A=6 record -> no reason applies at all.
    clean, clean_cost = _channel_a_only("no_words_yet", 18, ("yes",))
    while A._channel_a_step(clean, policy=POLICY,
                            asked_total=len(clean.skills)) is not None:
        step = A._channel_a_step(clean, policy=POLICY,
                                 asked_total=len(clean.skills))
        if step.enters_band is not None:
            clean.bands_entered.append(step.enters_band)
        v2.record_answer_v2(clean, step.question, "yes")
    assert A.difficulty_found(clean, policy=POLICY) is False
    assert B.recoverable_areas(clean, policy=POLICY), (
        "an area must genuinely be recoverable, so the refusal is not vacuous")
    refused = A.plan_additional_step(clean, policy=POLICY,
                                     asked_total=POLICY.normal_question_target)
    assert refused.kind == A.STOP
    assert refused.reason == A.STOP_NO_QUALIFYING_REASON


def test_channel_a_seven_leaves_at_most_q8_and_q9():
    choice, age, pattern = CHANNEL_A_COST[7]
    record, total, trace = run_interview(choice, age, _answer_cycle(pattern))
    channel_a = len([t for t in _asks(trace) if t[1] == v2.CHANNEL_A])
    assert channel_a == 7
    assert total <= POLICY.hard_question_max
    beyond = [t for t in _asks(trace) if t[0] > 7]
    assert len(beyond) <= 2, beyond
    assert all(t[3] is not None for t in beyond), "each must carry a reason"


def test_channel_a_nine_stops_immediately():
    choice, age, pattern = CHANNEL_A_COST[9]
    record, total, trace = run_interview(choice, age, _answer_cycle(pattern))
    assert len([t for t in _asks(trace) if t[1] == v2.CHANNEL_A]) == 9
    assert total == 9
    assert trace[-1] == ("STOP", A.STOP_HARD_MAXIMUM_REACHED)
    assert len([t for t in _asks(trace) if t[1] == v2.CHANNEL_B]) == 0, (
        "nine Channel-A questions leave nothing for the profile")


def test_a_channel_a_question_within_the_normal_target_carries_no_reason_code():
    for choice, age in itertools.product(CHOICES, AGES):
        _record, _total, trace = run_interview(choice, age,
                                               _answer_cycle(("no",)))
        for ordinal, _channel, _months, reason_code, _area, _answer in \
                _asks(trace):
            if ordinal <= POLICY.normal_question_target:
                assert reason_code is None, (choice, age, ordinal)
            else:
                assert reason_code in A.REASON_PRIORITY, (choice, age, ordinal)


# ---------------------------------------------------------------------------
# 3. ACCOUNTING IS IN QUESTIONS, NOT REASON EVENTS
# ---------------------------------------------------------------------------

def test_the_ledger_holds_one_entry_per_question_beyond_the_target():
    for choice, age in itertools.product(CHOICES, AGES):
        for pattern in (("no",), ("sometimes",), ("not_sure",),
                        ("no", "not_sure")):
            record, total, trace = run_interview(choice, age,
                                                 _answer_cycle(pattern))
            beyond = max(0, total - POLICY.normal_question_target)
            assert len(record.additional_reasons) == beyond, (choice, age,
                                                              pattern)
            assert [e["question_number"]
                    for e in record.additional_reasons] == \
                list(range(POLICY.normal_question_target + 1, total + 1))


def test_one_reason_repeated_costs_a_question_each_time():
    """A reason buys ONE question, not a phase.

    The all-`no` 36m path finishes a band and descends twice: three questions
    beyond the target, but only two DISTINCT reason codes.
    """
    record, total, trace = run_interview("short_sentences", 36,
                                         _answer_cycle(("no",)))
    assert total == 9
    codes = [e["reason_code"] for e in record.additional_reasons]
    assert len(codes) == 3
    assert len(set(codes)) < len(codes), codes
    summary = A.additional_summary(record, policy=POLICY)
    assert summary["additional_questions_used"] == 3
    assert sum(summary["questions_by_reason"].values()) == 3


def test_the_allowance_is_never_exceeded_by_any_reason_combination():
    for choice, age in itertools.product(CHOICES, AGES):
        for pattern in itertools.product(ANSWERS, repeat=3):
            record, total, _trace = run_interview(choice, age,
                                                  _answer_cycle(pattern))
            used = len(record.additional_reasons)
            assert used <= POLICY.additional_question_allowance, (
                choice, age, pattern, used)
            assert used == max(0, total - POLICY.normal_question_target)


def test_the_ledger_is_recorded_not_derived():
    """Two different reason sequences can reach the same evidence.

    So the ledger cannot be reconstructed from the record afterwards, which is
    why it is stored. The engine refuses an entry without a channel or a code.
    """
    record = v2.start_baseline_v2(AREA, "two_three_words", 24)
    with pytest.raises(v2.BaselineV2Error):
        v2.record_additional_reason_v2(record, question_number=7,
                                       reason_code="R1_X", channel="Z")
    with pytest.raises(v2.BaselineV2Error):
        v2.record_additional_reason_v2(record, question_number=7,
                                       reason_code="  ", channel=v2.CHANNEL_A)
    assert record.additional_reasons == []


# ---------------------------------------------------------------------------
# 4. PRIORITY: target integrity outranks profile completeness
# ---------------------------------------------------------------------------

def test_the_priority_order_is_the_founder_approved_one():
    assert A.REASON_PRIORITY == (
        A.R1_TARGET_BAND_COMPLETION, A.R2_TARGET_FLOOR_RESOLUTION,
        A.R3_PROFILE_AREA_RECOVERY, A.R4_MULTI_AREA_DIFFERENTIATION,
        A.R5_UNCERTAINTY_CLARIFICATION)
    assert set(A.CHANNEL_A_REASONS) | set(A.CHANNEL_B_REASONS) == \
        set(A.REASON_PRIORITY)
    assert not (set(A.CHANNEL_A_REASONS) & set(A.CHANNEL_B_REASONS))


def test_channel_a_takes_the_question_whenever_it_still_has_one_to_ask():
    """R1/R2 pre-empt every Channel-B reason, measured over all paths."""
    checked = 0
    for choice, age in itertools.product(CHOICES, AGES):
        for pattern in itertools.product(ANSWERS, repeat=2):
            record, total, trace = run_interview(choice, age,
                                                 _answer_cycle(pattern))
            for ordinal, channel, _m, reason_code, _a, _ans in _asks(trace):
                if ordinal <= POLICY.normal_question_target:
                    continue
                if reason_code in A.CHANNEL_B_REASONS:
                    # Nothing Channel A wanted may have been outstanding at
                    # that moment. Replay the prefix to check.
                    prefix = v2.start_baseline_v2(AREA, choice, age)
                    replay = _answer_cycle(pattern)
                    for i in range(ordinal - 1):
                        s = A.plan_interview_step(prefix, policy=POLICY,
                                                  asked_total=i)
                        assert s.kind == A.ASK
                        ans = replay(s.question)
                        if s.channel == v2.CHANNEL_A:
                            if s.enters_band is not None:
                                prefix.bands_entered.append(s.enters_band)
                            v2.record_answer_v2(prefix, s.question, ans)
                        else:
                            v2.record_profile_probe_v2(prefix, s.question, ans)
                    assert A._channel_a_step(prefix, policy=POLICY,
                                             asked_total=ordinal - 1) is None
                    checked += 1
    assert checked > 0, "non-vacuity: some Channel-B reason must have fired"


def test_r1_finishes_an_entered_band_and_r2_opens_a_new_one():
    record, total, trace = run_interview("short_sentences", 36,
                                         _answer_cycle(("no",)))
    beyond = [t for t in _asks(trace) if t[3] is not None]
    assert beyond[0][3] == A.R1_TARGET_BAND_COMPLETION
    assert beyond[0][2] == 30, "R1 completes the band already entered"
    assert beyond[1][3] == A.R2_TARGET_FLOOR_RESOLUTION
    assert beyond[1][2] < 30, "R2 descends to a NEW band"


def test_r2_never_opens_a_band_that_does_not_wholly_fit():
    """The frozen rule, re-proved through the Slice-6 planner."""
    for choice, age in itertools.product(CHOICES, AGES):
        for pattern in itertools.product(ANSWERS, repeat=3):
            record, total, trace = run_interview(choice, age,
                                                 _answer_cycle(pattern))
            for months in record.bands_entered:
                assessment = v2.band_assessment(record, months)
                assert assessment.assessment_complete, (
                    choice, age, pattern, months,
                    "an entered band was left half-asked")


def test_target_integrity_outranks_profile_breadth_on_a_long_descent():
    """A=9: the profile gets nothing, and that is the correct trade."""
    record, total, _trace = run_interview(
        *CHANNEL_A_COST[9][:2], _answer_cycle(CHANNEL_A_COST[9][2]))
    assert record.profile_probes == {}
    summary = B.profile_summary(record, policy=POLICY)
    assert set(summary["areas_assessed"]) == set()
    assert summary["areas_recoverable"], (
        "and the gap is reported as recoverable, not as clear")


# ---------------------------------------------------------------------------
# 5. THERE IS NO COMPLETENESS REASON
# ---------------------------------------------------------------------------

def test_no_reason_code_names_completeness():
    forbidden = ("complete", "coverage", "fill", "remaining", "unanswered",
                 "questionnaire", "breadth")
    for code in A.REASON_PRIORITY:
        lowered = code.lower()
        for fragment in forbidden:
            assert fragment not in lowered, (code, fragment)


def test_a_clean_interview_stops_at_the_normal_target():
    """Measured: on every all-`yes` path either Channel A needed the questions
    or the interview ended at six. Nothing is spent on tidiness."""
    stopped_early = 0
    for choice, age in itertools.product(CHOICES, AGES):
        record, total, trace = run_interview(choice, age,
                                             _answer_cycle(("yes",)))
        channel_a = len(record.skills)
        if channel_a <= POLICY.normal_question_target:
            assert total <= POLICY.normal_question_target, (choice, age, total)
            assert record.additional_reasons == []
            stopped_early += 1
    assert stopped_early > 0


def test_the_planner_stops_the_moment_no_reason_applies():
    record, total, trace = run_interview(*CHANNEL_A_COST[5][:2],
                                         _answer_cycle(CHANNEL_A_COST[5][2]))
    assert trace[-1] == ("STOP", A.STOP_NO_QUALIFYING_REASON)
    # And it is a genuine refusal: capacity remained.
    assert total < POLICY.hard_question_max
    assert POLICY.hard_question_max - total == 3


def test_r4_stops_being_material_once_depth_is_characterised():
    """R4 must not become a completeness reason by another name."""
    record = v2.start_baseline_v2(AREA, "short_sentences", 48)
    for area in (B.AREA_RECEPTIVE, B.AREA_FUNCTIONAL):
        probe = B.first_probe(AREA, area, chronological_months=48)
        v2.record_profile_probe_v2(record, B.probe_question(record, probe),
                                   "no")
    assert len(A.concerning_areas(record, policy=POLICY)) >= 2
    step = A._differentiation_step(record, policy=POLICY)
    assert step is not None and step.reason_code == \
        A.R4_MULTI_AREA_DIFFERENTIATION

    # Give every concerning area a second probe: depth is now characterised.
    for area in (B.AREA_RECEPTIVE, B.AREA_FUNCTIONAL):
        evidence = B.area_evidence(record, area)
        lower = B.downward_probe(AREA, area, below_months=evidence[0].months,
                                 asked_months=[e.months for e in evidence])
        v2.record_profile_probe_v2(record, B.probe_question(record, lower),
                                   "no")
    assert len(A.concerning_areas(record, policy=POLICY)) >= 2
    assert A._differentiation_step(record, policy=POLICY) is None


def test_r4_requires_two_genuinely_sampled_concerning_areas():
    record = v2.start_baseline_v2(AREA, "short_sentences", 48)
    probe = B.first_probe(AREA, B.AREA_RECEPTIVE, chronological_months=48)
    v2.record_profile_probe_v2(record, B.probe_question(record, probe), "no")
    assert len(A.concerning_areas(record, policy=POLICY)) == 1
    assert A._differentiation_step(record, policy=POLICY) is None


def test_an_above_age_only_area_is_not_a_concerning_area():
    """It cannot indicate concern, so it cannot count toward R4's threshold."""
    record = v2.start_baseline_v2(AREA, "few_sounds_or_words", 12)
    upward = B.upward_probe(AREA, B.AREA_RECEPTIVE, above_months=12,
                            chronological_months=12, lookahead_months=6)
    assert upward is not None and upward.months > 12
    v2.record_profile_probe_v2(record, B.probe_question(record, upward), "no")
    assert A.concerning_areas(record, policy=POLICY) == ()
    assert A.difficulty_found(record, policy=POLICY) is False


# ---------------------------------------------------------------------------
# 6. R3 NEVER MANUFACTURES COVERAGE
# ---------------------------------------------------------------------------

def test_recovery_never_asks_a_future_only_question():
    """The founder's social example: 36m inventory, younger child, never probed."""
    for choice, age in itertools.product(CHOICES, (12, 18, 24, 30, 35)):
        for pattern in itertools.product(ANSWERS, repeat=3):
            record, _total, trace = run_interview(choice, age,
                                                  _answer_cycle(pattern))
            assert B.area_status(record, B.AREA_SOCIAL) == B.NOT_ASSESSED, (
                choice, age, pattern)
            assert B.not_assessed_cause(record, B.AREA_SOCIAL) == \
                B.NOT_ASSESSED_NO_AGE_APPROPRIATE_SKILL
            # No CHANNEL-B question beyond the normal target is above age.
            #
            # Two deliberate exclusions. Slice 5's optional upward strength
            # probe is legitimately above age but is NORMAL routing (rule 3)
            # and carries no reason code. And Channel A ascends to its own
            # guardrail (chronological + 6), which Slices 3 and 4 already
            # bound — an above-age Channel-A answer is a strength, never a
            # concern, and that is a different rule from this one.
            for _o, channel, months, reason_code, _a, _ans in _asks(trace):
                if channel == v2.CHANNEL_B and reason_code is not None:
                    assert months <= age, (choice, age, reason_code, months)


def test_an_above_age_channel_b_probe_is_only_ever_a_normal_strength_probe():
    """Non-vacuity for the rule above: above-age probes DO occur, and only
    inside the normal target, never as an additional question."""
    seen_above_age = 0
    for choice, age in itertools.product(CHOICES, AGES):
        _record, _total, trace = run_interview(choice, age,
                                               _answer_cycle(("yes",)))
        for ordinal, channel, months, reason_code, _a, _ans in _asks(trace):
            if channel == v2.CHANNEL_B and months > age:
                seen_above_age += 1
                assert reason_code is None
                assert ordinal <= POLICY.normal_question_target
    assert seen_above_age > 0


def test_an_unprobeable_area_is_never_recoverable():
    for age in (12, 18, 24, 30, 35):
        record = v2.start_baseline_v2(AREA, "two_three_words", age)
        assert B.AREA_SOCIAL not in B.recoverable_areas(record, policy=POLICY)
        assert B.not_assessed_cause(record, B.AREA_SOCIAL) == \
            B.NOT_ASSESSED_NO_AGE_APPROPRIATE_SKILL


def test_the_two_not_assessed_causes_are_distinguishable():
    """Slice 5 measured these as different; Slice 6 acts on the difference."""
    young = v2.start_baseline_v2(AREA, "two_three_words", 24)
    assert B.not_assessed_cause(young, B.AREA_SOCIAL) == \
        B.NOT_ASSESSED_NO_AGE_APPROPRIATE_SKILL
    assert B.not_assessed_cause(young, B.AREA_RECEPTIVE) == \
        B.NOT_ASSESSED_BUDGET_CONSUMED

    summary = B.profile_summary(young, policy=POLICY)
    # `intelligibility` begins at 36m too, so it is unprobeable at 24m for the
    # same reason. Both are reported; neither is recoverable.
    assert B.AREA_SOCIAL in summary["areas_unprobeable"]
    assert B.AREA_INTELLIGIBILITY in summary["areas_unprobeable"]
    assert B.AREA_RECEPTIVE not in summary["areas_unprobeable"]
    assert B.AREA_FUNCTIONAL not in summary["areas_unprobeable"]
    assert B.AREA_RECEPTIVE in summary["areas_recoverable"]
    assert B.AREA_FUNCTIONAL in summary["areas_recoverable"]
    assert B.AREA_SOCIAL not in summary["areas_recoverable"]


def test_an_assessed_area_has_no_not_assessed_cause():
    record = v2.start_baseline_v2(AREA, "two_three_words", 24)
    probe = B.first_probe(AREA, B.AREA_RECEPTIVE, chronological_months=24)
    v2.record_profile_probe_v2(record, B.probe_question(record, probe), "yes")
    assert B.not_assessed_cause(record, B.AREA_RECEPTIVE) is None


def test_recovery_prefers_the_policys_required_area_order():
    record = v2.start_baseline_v2(AREA, "short_sentences", 48)
    probe = B.first_probe(AREA, B.AREA_SOCIAL, chronological_months=48)
    v2.record_profile_probe_v2(record, B.probe_question(record, probe), "no")
    assert A.difficulty_found(record, policy=POLICY) is True
    step = A._recovery_step(record, policy=POLICY)
    assert step is not None
    assert step.profile_area == POLICY.required_profile_areas[0]


# ---------------------------------------------------------------------------
# 7. R5
# ---------------------------------------------------------------------------

def test_r5_clarifies_beneath_an_unknown():
    record = v2.start_baseline_v2(AREA, "two_three_words", 24)
    probe = B.first_probe(AREA, B.AREA_RECEPTIVE, chronological_months=24)
    v2.record_profile_probe_v2(record, B.probe_question(record, probe),
                               "not_sure")
    step = A._clarification_step(record, policy=POLICY)
    assert step is not None
    assert step.reason_code == A.R5_UNCERTAINTY_CLARIFICATION
    assert step.question["months"] < 24


def test_r5_does_not_fire_when_the_lowest_probe_is_answered():
    record = v2.start_baseline_v2(AREA, "two_three_words", 24)
    probe = B.first_probe(AREA, B.AREA_RECEPTIVE, chronological_months=24)
    v2.record_profile_probe_v2(record, B.probe_question(record, probe), "no")
    assert A._clarification_step(record, policy=POLICY) is None


def test_r5_does_not_fire_at_the_bottom_of_an_area():
    record = v2.start_baseline_v2(AREA, "no_words_yet", 9)
    bottom = B.area_probes(AREA, B.AREA_RECEPTIVE)[0]
    v2.record_profile_probe_v2(record, B.probe_question(record, bottom),
                               "not_sure")
    assert A._clarification_step(record, policy=POLICY) is None


def test_r5_gives_each_unresolved_area_one_clarification_before_any_second():
    """BREADTH BEFORE DEPTH. The founder's correction, on the 48m all-`not_sure`
    path that previously spent the whole allowance descending receptive."""
    record, total, trace = run_interview("two_three_words", 48,
                                         _answer_cycle(("not_sure",)))
    assert total == 9
    r5 = [(t[4], t[2]) for t in _asks(trace)
          if t[3] == A.R5_UNCERTAINTY_CLARIFICATION]
    assert len(r5) == 3
    areas = [a for a, _m in r5]
    assert len(set(areas)) >= 2, (
        f"all three clarifications went to one area: {r5}")
    # Deepest-first is the thing being forbidden: receptive already held three
    # probes from normal descent, so it must not be clarified again first.
    assert len(B.area_evidence(record, B.AREA_RECEPTIVE)) >= 3
    assert areas[0] != B.AREA_RECEPTIVE


def test_r5_selects_the_shallowest_unresolved_area():
    """The rule, isolated: equal eligibility, unequal depth."""
    record = v2.start_baseline_v2(AREA, "short_sentences", 48)
    # receptive sampled twice, functional once — both unresolved.
    probe = B.first_probe(AREA, B.AREA_RECEPTIVE, chronological_months=48)
    v2.record_profile_probe_v2(record, B.probe_question(record, probe),
                               "not_sure")
    lower = B.downward_probe(AREA, B.AREA_RECEPTIVE, below_months=probe.months,
                             asked_months=[probe.months])
    v2.record_profile_probe_v2(record, B.probe_question(record, lower),
                               "not_sure")
    fprobe = B.first_probe(AREA, B.AREA_FUNCTIONAL, chronological_months=48)
    v2.record_profile_probe_v2(record, B.probe_question(record, fprobe),
                               "not_sure")

    assert set(A.unresolved_sampled_areas(record, policy=POLICY)) == \
        {B.AREA_RECEPTIVE, B.AREA_FUNCTIONAL}
    step = A._clarification_step(record, policy=POLICY)
    assert step is not None
    assert step.profile_area == B.AREA_FUNCTIONAL, "the shallower one first"


def test_r5_breaks_a_depth_tie_by_area_priority():
    record = v2.start_baseline_v2(AREA, "short_sentences", 48)
    for area in (B.AREA_FUNCTIONAL, B.AREA_RECEPTIVE):
        probe = B.first_probe(AREA, area, chronological_months=48)
        v2.record_profile_probe_v2(record, B.probe_question(record, probe),
                                   "not_sure")
    step = A._clarification_step(record, policy=POLICY)
    assert step is not None
    assert step.profile_area == POLICY.required_profile_areas[0]


def test_r5_never_samples_an_unassessed_area():
    """R3 owns unassessed areas; R5 only clarifies what was already sampled."""
    record = v2.start_baseline_v2(AREA, "short_sentences", 48)
    probe = B.first_probe(AREA, B.AREA_RECEPTIVE, chronological_months=48)
    v2.record_profile_probe_v2(record, B.probe_question(record, probe),
                               "not_sure")
    assert B.area_status(record, B.AREA_FUNCTIONAL) == B.NOT_ASSESSED
    for _ in range(4):
        step = A._clarification_step(record, policy=POLICY)
        if step is None:
            break
        assert step.profile_area == B.AREA_RECEPTIVE
        v2.record_profile_probe_v2(record, step.question, "not_sure")
    assert B.area_status(record, B.AREA_FUNCTIONAL) == B.NOT_ASSESSED
    assert A.unresolved_sampled_areas(record, policy=POLICY) == \
        (B.AREA_RECEPTIVE,)


def test_r5_never_touches_an_unprobeable_area():
    for choice, age in itertools.product(CHOICES, (12, 18, 24, 30, 35)):
        record, _total, trace = run_interview(choice, age,
                                              _answer_cycle(("not_sure",)))
        for _o, _c, _m, reason_code, area, _ans in _asks(trace):
            if reason_code == A.R5_UNCERTAINTY_CLARIFICATION:
                assert area != B.AREA_SOCIAL, (choice, age)
        assert B.not_assessed_cause(record, B.AREA_SOCIAL) == \
            B.NOT_ASSESSED_NO_AGE_APPROPRIATE_SKILL


def test_r5_never_deepens_an_area_while_a_shallower_one_is_clarifiable():
    """THE INVARIANT, replayed at every R5 decision on every path.

    Stated this way rather than as "the three clarifications must land in
    different areas", because they legitimately may not. On the 24m
    all-`not_sure` path normal routing leaves receptive with four probes and
    functional with one, so functional takes all three additional questions
    while CATCHING UP and never overtakes receptive. That is breadth, not
    absorption. What must never happen is deepening an area while a shallower
    unresolved area still had a clarifier available.
    """
    checked = 0
    for choice, age in itertools.product(CHOICES, AGES):
        for pattern in (("not_sure",), ("not_sure", "no"),
                        ("not_sure", "yes"), ("no", "not_sure")):
            record, total, trace = run_interview(choice, age,
                                                 _answer_cycle(pattern))
            r5_ordinals = [t[0] for t in _asks(trace)
                           if t[3] == A.R5_UNCERTAINTY_CLARIFICATION]
            if not r5_ordinals:
                continue
            for ordinal in r5_ordinals:
                prefix = v2.start_baseline_v2(AREA, choice, age)
                replay = _answer_cycle(pattern)
                for i in range(ordinal - 1):
                    s = A.plan_interview_step(prefix, policy=POLICY,
                                              asked_total=i)
                    ans = replay(s.question)
                    if s.channel == v2.CHANNEL_A:
                        if s.enters_band is not None:
                            prefix.bands_entered.append(s.enters_band)
                        v2.record_answer_v2(prefix, s.question, ans)
                    else:
                        v2.record_profile_probe_v2(prefix, s.question, ans)
                chosen = A._clarification_step(prefix, policy=POLICY)
                assert chosen is not None
                depths = {}
                for area in A.unresolved_sampled_areas(prefix, policy=POLICY):
                    evidence = B.area_evidence(prefix, area)
                    if B.downward_probe(
                            AREA, area, below_months=evidence[0].months,
                            asked_months=[e.months for e in evidence]):
                        depths[area] = len(evidence)
                assert depths[chosen.profile_area] == min(depths.values()), (
                    choice, age, pattern, ordinal, chosen.profile_area, depths)
                checked += 1
    assert checked > 0, "non-vacuity: R5 must have fired somewhere"


def test_r5_is_channel_b_only():
    """R2 owns Channel-A uncertainty and outranks R5."""
    import inspect

    src = inspect.getsource(A._clarification_step)
    assert "record.skills" not in src
    assert "profile." in src
    for choice, age in itertools.product(CHOICES, AGES):
        record, _total, trace = run_interview(choice, age,
                                              _answer_cycle(("not_sure",)))
        for _o, channel, _m, reason_code, _a, _ans in _asks(trace):
            if reason_code == A.R5_UNCERTAINTY_CLARIFICATION:
                assert channel == v2.CHANNEL_B


# ---------------------------------------------------------------------------
# 8. the frozen boundaries still hold
# ---------------------------------------------------------------------------

def test_an_above_age_answer_never_becomes_a_concern_in_either_channel():
    for choice, age in itertools.product(CHOICES, AGES):
        for pattern in itertools.product(ANSWERS, repeat=3):
            record, _total, _trace = run_interview(choice, age,
                                                   _answer_cycle(pattern))
            for evidence in list(record.skills.values()) + \
                    list(record.profile_probes.values()):
                if evidence.months > age:
                    assert E.counts_as_concern(evidence.state,
                                               evidence.months, age) is False


def test_the_two_evidence_mappings_never_overlap():
    for choice, age in itertools.product(CHOICES, AGES):
        for pattern in itertools.product(ANSWERS, repeat=3):
            record, _total, _trace = run_interview(choice, age,
                                                   _answer_cycle(pattern))
            assert not (set(record.skills) & set(record.profile_probes))


def test_an_unknown_is_never_a_target_and_never_a_floor():
    for choice, age in itertools.product(CHOICES, AGES):
        record, _total, _trace = run_interview(choice, age,
                                               _answer_cycle(("not_sure",)))
        assert T.candidate_target_band(record) is None
        assert T.floor_band(record) is None
        assert E.eligible_target_band(record) is None


def test_the_ledger_is_omitted_from_a_record_that_never_exceeded_the_target():
    record, total, _trace = run_interview(*CHANNEL_A_COST[5][:2],
                                          _answer_cycle(CHANNEL_A_COST[5][2]))
    assert total <= POLICY.normal_question_target
    stored = v2.record_to_state_v2(record)
    assert "additional_reasons" not in stored
    assert json.dumps(
        v2.record_to_state_v2(v2.record_from_state_v2(copy.deepcopy(stored))),
        sort_keys=True) == json.dumps(stored, sort_keys=True)


def test_the_ledger_round_trips_and_preserves_its_order():
    record, total, _trace = run_interview("short_sentences", 36,
                                          _answer_cycle(("no",)))
    stored = v2.record_to_state_v2(record)
    assert stored["additional_reasons"] == record.additional_reasons
    decoded = v2.record_from_state_v2(copy.deepcopy(stored))
    assert decoded.additional_reasons == record.additional_reasons
    assert json.dumps(v2.record_to_state_v2(decoded), sort_keys=True) == \
        json.dumps(stored, sort_keys=True)
    for entry in stored["additional_reasons"]:
        assert set(entry) == set(v2.ADDITIONAL_REASON_KEYS)


def test_the_ledger_never_reaches_the_a2_v2_projection():
    record, _total, _trace = run_interview("short_sentences", 36,
                                           _answer_cycle(("no",)))
    assert record.additional_reasons
    stored = v2.record_to_state_v2(record)
    body = pv2.build_body(source_session_id="sess_fictional", record=stored,
                          payload={"summary": {}, "skills": [],
                                   "band_totals": []})
    assert set(body) == set(pv2.BODY_KEYS)
    blob = json.dumps(body)
    assert "additional_reasons" not in blob
    assert "reason_code" not in blob


def test_the_summaries_leak_no_child_data():
    record, _total, _trace = run_interview("short_sentences", 36,
                                           _answer_cycle(("no",)))
    blob = json.dumps(A.additional_summary(record, policy=POLICY))
    for forbidden in ("milestone", "skill_key", "child", "caregiver", "says",
                      "points", "talks"):
        assert forbidden not in blob, forbidden


# ---------------------------------------------------------------------------
# 9. determinism and policy independence
# ---------------------------------------------------------------------------

def test_the_whole_interview_is_deterministic():
    runs = [run_interview("short_sentences", 36, _answer_cycle(("no",)))[2]
            for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


def test_a_wider_allowance_asks_more_without_changing_what_was_asked():
    """Configuration controls the budget; evidence semantics are invariant."""
    import dataclasses

    wider = dataclasses.replace(POLICY, policy_id="hypothetical",
                                hard_question_max=12)
    assert wider.additional_question_allowance == 6

    base_record, base_total, base_trace = run_interview(
        "short_sentences", 36, _answer_cycle(("no",)))
    wide_record, wide_total, wide_trace = run_interview(
        "short_sentences", 36, _answer_cycle(("no",)), policy=wider)

    assert wide_total > base_total
    assert _asks(wide_trace)[:len(_asks(base_trace))] == _asks(base_trace)
    # Same answers, same states, for every skill both interviews reached.
    for key, evidence in base_record.skills.items():
        assert wide_record.skills[key].state == evidence.state


def test_a_zero_allowance_policy_stops_at_the_normal_target():
    import dataclasses

    tight = dataclasses.replace(POLICY, policy_id="hypothetical",
                                hard_question_max=6)
    assert tight.additional_question_allowance == 0
    for choice, age in itertools.product(CHOICES, AGES):
        record, total, _trace = run_interview(choice, age,
                                              _answer_cycle(("no",)),
                                              policy=tight)
        assert total <= 6, (choice, age, total)
        assert record.additional_reasons == []


def test_phase_for_is_budget_arithmetic_only():
    for spent in range(0, 6):
        assert A.phase_for(spent, policy=POLICY) == A.PHASE_PROFILE_NORMAL
    for spent in range(6, 12):
        assert A.phase_for(spent, policy=POLICY) == A.PHASE_ADDITIONAL


def test_the_additional_planner_refuses_below_the_normal_target():
    record = v2.start_baseline_v2(AREA, "two_three_words", 24)
    step = A.plan_additional_step(record, policy=POLICY, asked_total=3)
    assert step.kind == A.STOP
    assert step.reason == A.STOP_NORMAL_TARGET_NOT_REACHED
