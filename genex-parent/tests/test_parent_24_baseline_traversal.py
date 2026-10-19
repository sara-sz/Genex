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
POLICY = P.TALKING_V2_POLICY_1
BUDGET = POLICY.target_finding_budget
LOOKAHEAD = POLICY.normal_upper_lookahead_months

CHOICES = ("no_words_yet", "few_sounds_or_words", "many_single_words",
           "two_three_words", "short_sentences", "not_sure")
AGES = (2, 4, 6, 9, 12, 18, 24, 30, 36, 48, 60)
ANSWERS = ("yes", "no", "sometimes", "with_help", "not_sure")


def drive(choice, age, answer_for, budget=BUDGET, lookahead=LOOKAHEAD):
    """Run Channel A to its terminal step. Returns (record, reason, asked)."""
    record = v2.start_baseline_v2(AREA, choice, age)
    asked = []
    for _ in range(64):
        step = T.plan_next(record, lookahead_months=lookahead, budget=budget)
        if step.kind == T.STOP:
            return record, step.reason, asked
        if step.enters_band is not None:
            record.bands_entered.append(step.enters_band)
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
        lambda q: "yes" if q["months"] <= 24 else "no", budget=9)
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

def test_channel_a_never_exceeds_the_target_finding_budget():
    over = [(c, a, n, len(asked))
            for c, a, n, f in MATRIX
            for _r, _reason, asked in [drive(c, a, f)]
            if len(asked) > BUDGET]
    assert over == [], over


@pytest.mark.parametrize("budget", [1, 2, 3, 4, 5, 6, 7])
def test_the_budget_is_respected_at_every_value(budget):
    for choice, age, name, fn in MATRIX[::37]:
        record, reason, asked = drive(choice, age, fn, budget=budget)
        assert len(asked) <= budget
        for months in record.bands_entered:
            assert v2.band_assessment(record, months).assessment_complete


def test_a_band_wider_than_the_budget_is_never_entered():
    """Rule 5 at the first step. Band 6 is three skills wide."""
    record, reason, asked = drive("not_sure", 6, lambda q: "yes", budget=2)
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
    step = T.plan_next(record, lookahead_months=LOOKAHEAD, budget=BUDGET)
    assert step.is_ask and step.question["months"] == 9


def test_the_anchor_is_honoured_even_above_the_guardrail():
    """Rule 1: an explicitly reported advanced ability gets verified."""
    record = v2.start_baseline_v2(AREA, "short_sentences", 12)
    assert T.start_band(record) == 36
    assert T.ascent_ceiling_band(record, lookahead_months=LOOKAHEAD) == 36
    step = T.plan_next(record, lookahead_months=LOOKAHEAD, budget=BUDGET)
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
    record, reason, asked = drive("no_words_yet", 9, lambda q: "no", budget=9)
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
        T.plan_next(record, lookahead_months=LOOKAHEAD, budget=BUDGET)
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
                                  budget=BUDGET)
    assert summary["every_entered_band_complete"] is True
    assert summary["questions_spent"] == len(asked)
    assert summary["terminal_reason"] == reason
    blob = json.dumps(summary)
    for forbidden in ("milestone", "child", "caregiver", "skill_key"):
        assert forbidden not in blob
