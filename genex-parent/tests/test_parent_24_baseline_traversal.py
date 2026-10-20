"""Slice 3 — anchored Channel-A traversal, guardrail, floor, band budget.

Canonical design: `pilot_runtime/docs/parent_baseline_v2_design.md`.

The gates this file installs permanently:

  * EVERY ENTERED BAND IS COMPLETE, over the whole path matrix. This is the
    audited failure the budget rule exists to prevent.
  * ADVERSARIAL: a truncated record really does make the Pilot's
    `select_target_band` raise — so the gate above is not vacuous.
  * L never exceeds the target-finding budget.
  * ascent respects the guardrail; the anchor may exceed it; descent is
    unbounded by age.
  * `unknown` never establishes a floor.
  * determinism.
"""

from __future__ import annotations

import itertools

import pytest

from api import baseline_policy as P
from api import baseline_traversal as T
from genex_core import functional_baseline_v2 as v2

AREA = "talking"
POLICY = P.TALKING_V2_POLICY_2
BUDGET = POLICY.target_finding_normal_allocation
LOOKAHEAD = POLICY.normal_upper_lookahead_months

CHOICES = ("no_words_yet", "few_sounds_or_words", "many_single_words",
           "two_three_words", "short_sentences", "not_sure")
AGES = (2, 4, 6, 9, 12, 18, 24, 30, 36, 48, 60)
ANSWERS = ("yes", "no", "sometimes", "with_help", "not_sure")


def budget_for(allocation=None, asked_total=0, hard=None, normal=None):
    """One budget object; the planner never sees loose integers."""
    return T.InterviewBudget(
        normal_allocation=(POLICY.target_finding_normal_allocation
                           if allocation is None else allocation),
        normal_total=(POLICY.normal_question_target if normal is None
                      else normal),
        hard_total=(POLICY.hard_question_max if hard is None else hard),
        asked_total=asked_total)


def drive(choice, age, answer_for, budget=None, lookahead=LOOKAHEAD,
          hard=None, normal=None):
    """Run Channel A to its terminal step. Returns (record, reason, asked).

    `asked_total` is refreshed every iteration, because the hard total binds
    the WHOLE interview and Channel A is all of it during target finding.
    """
    allocation = budget
    record = v2.start_baseline_v2(AREA, choice, age)
    asked = []
    tiers = []
    for _ in range(64):
        step = T.plan_next(
            record, lookahead_months=lookahead,
            budget=budget_for(allocation, len(asked), hard, normal))
        if step.kind == T.STOP:
            record._tiers = tiers          # test-only breadcrumb
            return record, step.reason, asked
        if step.enters_band is not None:
            record.bands_entered.append(step.enters_band)
            tiers.append((step.enters_band, step.admitted_tier))
        q = step.question
        asked.append(q)
        v2.record_answer_v2(record, q, answer_for(q))
    raise AssertionError("traversal did not terminate")


def _patterns():
    yield "all_yes", lambda q: "yes"
    yield "all_no", lambda q: "no"
    yield "all_sometimes", lambda q: "sometimes"
    yield "all_with_help", lambda q: "with_help"
    yield "all_not_sure", lambda q: "not_sure"
    yield "first_no", lambda q, s=[]: "no" if not s and s.append(1) is None else "yes"
    yield "yes_to_18", lambda q: "yes" if q["months"] <= 18 else "no"
    yield "yes_to_24", lambda q: "yes" if q["months"] <= 24 else "no"
    yield "yes_to_30", lambda q: "yes" if q["months"] <= 30 else "no"
    yield "unknown_low_no_high", lambda q: ("not_sure" if q["months"] <= 12
                                            else "no")


MATRIX = [(c, a, n, f) for c, a, (n, f)
          in itertools.product(CHOICES, AGES, list(_patterns()))]


# ---------------------------------------------------------------------------
# 1. THE GATE: every entered band is complete, always
# ---------------------------------------------------------------------------

def test_every_entered_band_is_complete_across_the_whole_path_matrix():
    """The audited failure this rule exists to prevent.

    An incomplete entered band PROJECTS fine and then makes
    `select_target_band` raise `BandAssessmentIncomplete` at the first
    incomplete band, which the transport renders as a constant 403. Enforcing
    the budget at band boundaries makes that unreachable.
    """
    bad = []
    for choice, age, name, fn in MATRIX:
        record, reason, asked = drive(choice, age, fn)
        for months in record.bands_entered:
            if not v2.band_assessment(record, months).assessment_complete:
                bad.append((choice, age, name, months))
    assert bad == [], bad


def test_the_matrix_is_large_enough_to_be_meaningful():
    assert len(MATRIX) == len(CHOICES) * len(AGES) * 10 == 660


def test_a_truncated_band_really_does_break_the_pilot():
    """ADVERSARIAL non-vacuity for the gate above.

    Without this, "every entered band is complete" could be true of a planner
    that simply never enters a band.
    """
    classify = pytest.importorskip(
        "pilot_backend.domain.suggestion_generation_v2")
    # budget=9 so the four-wide band 30 is actually entered: at the pilot's
    # budget of 4 it cannot be (1 + 4 > 4), which is its own finding.
    record, reason, asked = drive(
        "two_three_words", 36,
        lambda q: "yes" if q["months"] <= 24 else "no", budget=9, normal=9)
    band30 = [r for r in v2.band_skill_rows(record, 30)]
    assert 30 in record.bands_entered and len(band30) == 4
    victim = v2.skill_key(record.domain, band30[-1]["subdomain"], 30,
                          band30[-1]["milestone"])
    record.skills.pop(victim, None)
    roster = [v2.skill_key(record.domain, r["subdomain"], 30, r["milestone"])
              for r in band30]
    view = classify.classify_band(
        months=30, roster=roster,
        states_by_ref={k: e.state for k, e in record.skills.items()
                       if e.months == 30})
    assert view.classification == classify.BAND_INCOMPLETE
    with pytest.raises(classify.BandAssessmentIncomplete):
        classify.select_target_band([view])


# ---------------------------------------------------------------------------
# 2. the budget
# ---------------------------------------------------------------------------

def test_channel_a_never_exceeds_the_hard_total():
    """The only absolute bound. NINE IS A TOTAL, never a per-channel figure.

    `target_finding_normal_allocation` is the PREFERRED allocation, not a
    ceiling: when target or floor integrity needs another complete band,
    Channel A may pass it via tier 2 (within the normal total) or tier 3
    (within the hard total). An earlier version of this test asserted
    `L <= 4` and that assertion was itself the bug.
    """
    over = [(c, a, n, len(asked))
            for c, a, n, f in MATRIX
            for _r, _reason, asked in [drive(c, a, f)]
            if len(asked) > POLICY.hard_question_max]
    assert over == [], over


def test_expansion_beyond_the_preferred_allocation_is_always_tier_justified():
    """Every band past tier 1 must name the bound that admitted it."""
    for choice, age, name, fn in MATRIX:
        record, reason, asked = drive(choice, age, fn)
        if len(asked) <= POLICY.target_finding_normal_allocation:
            continue
        tiers = {t for _band, t in record._tiers}
        assert tiers & {T.TIER_NORMAL, T.TIER_ADDITIONAL}, (
            choice, age, name, record._tiers)
        for _band, tier in record._tiers:
            assert tier in T.BAND_ADMISSION_TIERS


@pytest.mark.parametrize("allocation", [1, 2, 3, 4, 5, 6])
def test_every_entered_band_stays_complete_at_every_allocation(allocation):
    for choice, age, name, fn in MATRIX[::37]:
        record, reason, asked = drive(choice, age, fn, budget=allocation)
        assert len(asked) <= POLICY.hard_question_max
        for months in record.bands_entered:
            assert v2.band_assessment(record, months).assessment_complete


# ---------------------------------------------------------------------------
# 2b. THE THREE FOUNDER-PINNED BUDGET CASES
# ---------------------------------------------------------------------------

def test_case_24m_mastered_then_whole_band_30_costs_five_inside_normal():
    """1 + 4 = 5. Tier 2. Still a normal interview, zero additional questions.

    Band 24 is one skill wide and band 30 is four. Under the superseded
    reading (4 as an absolute ceiling) this band could never be entered, so a
    child who had mastered 24m got no target at all — even though 30m is
    exactly where their target lies.
    """
    record, reason, asked = drive(
        "two_three_words", 36, lambda q: "yes" if q["months"] <= 24 else "no")
    assert [q["months"] for q in asked] == [24, 30, 30, 30, 30]
    assert len(asked) == 5
    assert len(asked) <= POLICY.normal_question_target
    assert record.bands_entered == [24, 30]
    # Entered at tier 2: beyond the preferred 4, inside the normal 6.
    assert dict(record._tiers)[30] == T.TIER_NORMAL
    # Zero additional-question usage: 5 <= 6.
    additional_used = max(0, len(asked) - POLICY.normal_question_target)
    assert additional_used == 0
    # One normal profile slot remains before six.
    assert POLICY.profile_slots_for(len(asked)) == 1
    for months in record.bands_entered:
        assert v2.band_assessment(record, months).assessment_complete


def test_case_36m_anchor_establishing_the_30m_floor_costs_seven():
    """3 + 4 = 7. Tier 3. Q7 is the first beyond the normal interview."""
    record, reason, asked = drive(
        "short_sentences", 36, lambda q: "yes" if q["months"] < 36 else "no")
    assert len(asked) == 7
    assert record.bands_entered == [36, 30]
    assert dict(record._tiers)[36] == T.TIER_PREFERRED
    assert dict(record._tiers)[30] == T.TIER_ADDITIONAL
    assert reason == T.TARGET_FOUND
    assert T.floor_band(record) == 30
    assert T.candidate_target_band(record) == 36
    # Q7 is the first additional question; two slots remain, never past nine.
    additional_used = len(asked) - POLICY.normal_question_target
    assert additional_used == 1
    assert POLICY.additional_question_allowance - additional_used == 2
    assert len(asked) <= POLICY.hard_question_max
    assert POLICY.profile_slots_for(len(asked)) == 0
    for months in record.bands_entered:
        assert v2.band_assessment(record, months).assessment_complete


def test_case_a_whole_band_that_would_exceed_nine_is_not_entered():
    """6 already asked + a four-wide band = 10. Refuse; never half-ask it."""
    record = v2.start_baseline_v2(AREA, "two_three_words", 36)
    record.bands_entered.append(24)
    row = v2.band_skill_rows(record, 24)[0]
    v2.record_answer_v2(record, {"domain": record.domain,
                                 "subdomain": row["subdomain"],
                                 "months": 24,
                                 "milestone": row["milestone"]}, "yes")
    # Channel B has already spent five, so the interview total stands at six.
    budget = T.InterviewBudget(
        normal_allocation=POLICY.target_finding_normal_allocation,
        normal_total=POLICY.normal_question_target,
        hard_total=POLICY.hard_question_max,
        asked_total=6)
    assert T.band_width(record, 30) == 4
    assert budget.admits_band(channel_a_spent=1, width=4) is None
    step = T.plan_next(record, lookahead_months=LOOKAHEAD, budget=budget)
    assert step.kind == T.STOP
    assert step.reason == T.CEILING_BUDGET_EXHAUSTED
    assert 30 not in record.bands_entered
    assert v2.band_assessment(record, 24).assessment_complete


def test_the_hard_total_binds_channel_a_and_channel_b_together():
    """Nine is a total. A tier-1 decision cannot overrun it."""
    budget = T.InterviewBudget(normal_allocation=4, normal_total=6,
                               hard_total=9, asked_total=8)
    # Width 1 fits (9), width 2 does not (10) — even though Channel A has
    # spent nothing and tier 1 would otherwise admit it.
    assert budget.admits_band(channel_a_spent=0, width=1) is not None
    assert budget.admits_band(channel_a_spent=0, width=2) is None


def test_target_integrity_outranks_profile_breadth():
    """A band needed for integrity is entered even though it costs profile."""
    record, reason, asked = drive(
        "two_three_words", 36, lambda q: "yes" if q["months"] <= 24 else "no")
    assert POLICY.profile_slots_for(len(asked)) < 3, (
        "entering band 30 must cost profile breadth")
    assert 30 in record.bands_entered, (
        "and integrity must still win: the band is entered")


def test_the_three_tiers_are_admitted_in_order():
    budget = T.InterviewBudget(normal_allocation=4, normal_total=6,
                               hard_total=9, asked_total=0)
    assert budget.admits_band(channel_a_spent=0, width=4) == T.TIER_PREFERRED
    assert budget.admits_band(channel_a_spent=3, width=3) == T.TIER_NORMAL
    assert budget.with_asked(5).admits_band(channel_a_spent=5,
                                            width=4) == T.TIER_ADDITIONAL
    assert budget.with_asked(6).admits_band(channel_a_spent=6, width=4) is None


def test_the_additional_allowance_cannot_contradict_the_two_bounds():
    for policy_id in P.registered_policy_ids():
        policy = P.get_policy(policy_id)
        budget = T.InterviewBudget.from_policy(policy)
        assert budget.additional_allowance == (
            policy.hard_question_max - policy.normal_question_target)
        assert budget.additional_allowance == \
            policy.additional_question_allowance


def test_changing_the_allocation_changes_no_recorded_state():
    """Budget is routing. It never touches meaning."""
    answers = {}

    def pattern(q):
        answers.setdefault(q["skill_key"], "yes" if q["months"] <= 24 else "no")
        return answers[q["skill_key"]]

    a = drive("two_three_words", 36, pattern, budget=4)
    b = drive("two_three_words", 36, pattern, budget=6)
    for record in (a[0], b[0]):
        for key, evidence in record.skills.items():
            assert evidence.state == (
                "demonstrated" if evidence.months <= 24 else "not_demonstrated")


def test_a_band_wider_than_the_budget_is_never_entered():
    """Rule 5 at the first step. Band 6 is three skills wide."""
    record, reason, asked = drive("not_sure", 6, lambda q: "yes", budget=2,
                                  normal=2, hard=2)
    assert asked == []
    assert reason == T.START_BAND_DOES_NOT_FIT
    assert record.bands_entered == []


# ---------------------------------------------------------------------------
# 3. routing: anchor, guardrail, descent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("choice,expected", [
    ("no_words_yet", 9), ("few_sounds_or_words", 12),
    ("many_single_words", 18), ("two_three_words", 24),
    ("short_sentences", 36)])
def test_the_entry_choice_sets_the_start_band_independently_of_age(choice,
                                                                   expected):
    for age in (6, 24, 36, 60):
        record = v2.start_baseline_v2(AREA, choice, age)
        assert T.start_band(record) == expected


def test_a_delayed_older_child_starts_low_not_at_their_age():
    """The founder's example: 36m child reported as having no words."""
    record = v2.start_baseline_v2(AREA, "no_words_yet", 36)
    assert T.start_band(record) == 9
    step = T.plan_next(record, lookahead_months=LOOKAHEAD, budget=budget_for())
    assert step.is_ask and step.question["months"] == 9


def test_the_anchor_is_honoured_even_above_the_guardrail():
    """Rule 1: an explicitly reported advanced ability gets verified."""
    record = v2.start_baseline_v2(AREA, "short_sentences", 12)
    assert T.start_band(record) == 36
    assert T.ascent_ceiling_band(record, lookahead_months=LOOKAHEAD) == 36
    step = T.plan_next(record, lookahead_months=LOOKAHEAD, budget=budget_for())
    assert step.is_ask and step.question["months"] == 36


def test_ascent_stops_at_the_guardrail():
    record, reason, asked = drive("many_single_words", 18, lambda q: "yes")
    assert max(q["months"] for q in asked) <= T.ascent_ceiling_band(
        record, lookahead_months=LOOKAHEAD)
    assert reason == T.NO_TARGET_WITHIN_GUARDRAIL


def test_descent_is_not_clamped_to_chronological_age():
    """Rule 3. A 60-month-old may legitimately be asked 2-month skills."""
    record, reason, asked = drive("few_sounds_or_words", 60, lambda q: "no")
    assert min(q["months"] for q in asked) < 60
    assert min(q["months"] for q in asked) <= 9


def test_the_entry_choice_never_marks_anything_demonstrated():
    record = v2.start_baseline_v2(AREA, "short_sentences", 60)
    assert record.skills == {}
    assert T.floor_band(record) is None


# ---------------------------------------------------------------------------
# 4. the floor
# ---------------------------------------------------------------------------

def test_unknown_never_establishes_a_floor():
    """Rule 4. An unanswerable question is not evidence a skill is present."""
    record, reason, asked = drive("many_single_words", 24,
                                  lambda q: "not_sure")
    assert T.floor_band(record) is None
    assert reason in (T.ONLY_UNKNOWN, T.FLOOR_UNESTABLISHED)


def test_a_target_requires_a_mastered_floor_below_it():
    record, reason, asked = drive("two_three_words", 36,
                                  lambda q: "yes" if q["months"] < 24 else "no")
    if reason == T.TARGET_FOUND:
        floor = T.floor_band(record)
        target = T.candidate_target_band(record)
        assert floor is not None and target is not None and floor < target


def test_the_candidate_target_is_the_lowest_unresolved_entered_band():
    """Matches `select_target_band`, so the reported state is predictive."""
    for choice, age, name, fn in MATRIX[::53]:
        record, reason, asked = drive(choice, age, fn)
        target = T.candidate_target_band(record)
        if target is None:
            continue
        lower_unresolved = [
            m for m in record.bands_entered
            if m < target and v2.band_assessment(record, m).unresolved
            and v2.band_assessment(record, m).assessment_complete]
        assert lower_unresolved == []


def test_a_deficit_at_the_ladder_bottom_is_still_a_finding():
    record, reason, asked = drive("no_words_yet", 9, lambda q: "no", budget=9,
                                  normal=9)
    assert reason in (T.TARGET_AT_LADDER_BOTTOM, T.FLOOR_UNESTABLISHED)


# ---------------------------------------------------------------------------
# 5. terminal reasons and determinism
# ---------------------------------------------------------------------------

def test_every_path_terminates_with_a_declared_reason():
    for choice, age, name, fn in MATRIX:
        record, reason, asked = drive(choice, age, fn)
        assert reason in T.TERMINAL_REASONS, (choice, age, name, reason)


def test_the_traversal_is_deterministic():
    for choice, age, name, fn in MATRIX[::41]:
        a = drive(choice, age, fn)
        b = drive(choice, age, fn)
        assert [q["skill_key"] for q in a[2]] == [q["skill_key"] for q in b[2]]
        assert a[1] == b[1]


def test_plan_next_mutates_nothing():
    record = v2.start_baseline_v2(AREA, "many_single_words", 36)
    before = (dict(record.skills), list(record.bands_entered))
    for _ in range(3):
        T.plan_next(record, lookahead_months=LOOKAHEAD, budget=budget_for())
    assert (dict(record.skills), list(record.bands_entered)) == before


def test_the_frozen_engine_traversal_is_untouched():
    """`next_question_v2` keeps its own behaviour; this is a second planner."""
    record = v2.start_baseline_v2(AREA, "many_single_words", 36)
    q = v2.next_question_v2(record)
    assert q is not None and q["months"] == 18
    assert v2.MAX_BANDS == 3


def test_channel_a_summary_reports_completeness_and_no_child_data():
    import json

    record, reason, asked = drive("two_three_words", 36,
                                  lambda q: "yes" if q["months"] <= 24 else "no")
    summary = T.channel_a_summary(record, lookahead_months=LOOKAHEAD,
                                  budget=budget_for())
    assert summary["every_entered_band_complete"] is True
    assert summary["questions_spent"] == len(asked)
    assert summary["terminal_reason"] == reason
    blob = json.dumps(summary)
    for forbidden in ("milestone", "child", "caregiver", "skill_key"):
        assert forbidden not in blob
