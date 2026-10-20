"""Slice 4 — above-age interpretation and target eligibility.

Canonical design: `pilot_runtime/docs/parent_baseline_v2_design.md`.

The gates this file installs permanently:

  * failure on a genuinely above-age skill NEVER becomes a deficit or a concern
  * `yes` above age is reported as a STRENGTH
  * `not_sure` remains uncertainty everywhere — never target, never concern
  * eligibility changes NO recorded state
  * the predicate keys on chronological age, never on the policy lookahead
  * a rung AT the child's age stays fully eligible
"""

from __future__ import annotations

import itertools

import pytest

from api import baseline_eligibility as E
from api import baseline_policy as P
from api import baseline_traversal as T
from genex_core import functional_baseline as v1
from genex_core import functional_baseline_v2 as v2

AREA = "talking"
ANSWERS = ("yes", "sometimes", "with_help", "no", "not_sure")
STATES = {a: v1._classify(a) for a in ANSWERS}


# ---------------------------------------------------------------------------
# 1. the answer vocabulary is imported, never forked
# ---------------------------------------------------------------------------

def test_the_answer_vocabulary_is_unchanged():
    E.assert_answer_vocabulary_unchanged()


def test_the_five_answers_still_map_to_the_four_states():
    assert STATES == {"yes": "demonstrated", "sometimes": "emerging",
                      "with_help": "emerging", "no": "not_demonstrated",
                      "not_sure": "unknown"}


# ---------------------------------------------------------------------------
# 2. THE RULE: above age may discover strength, never manufacture deficit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer", ANSWERS)
def test_no_above_age_answer_is_ever_target_eligible(answer):
    """The headline. 24m child asked a 30m rung."""
    assert E.target_eligible(STATES[answer], months=30,
                             chronological_months=24) is False


@pytest.mark.parametrize("answer", ANSWERS)
def test_no_above_age_answer_ever_counts_as_concern(answer):
    assert E.counts_as_concern(STATES[answer], months=30,
                               chronological_months=24) is False


def test_a_no_above_age_is_recorded_but_not_a_deficit():
    """`no` on a future skill: state preserved, eligibility denied."""
    state = STATES["no"]
    assert state == "not_demonstrated"
    assert E.target_eligible(state, 30, 24) is False
    assert E.counts_as_concern(state, 30, 24) is False
    assert E.above_age_label(state, 30, 24) == E.ABOVE_AGE_NOT_YET


def test_sometimes_and_with_help_above_age_are_emerging_advanced_not_concern():
    for answer in ("sometimes", "with_help"):
        state = STATES[answer]
        assert state == "emerging"
        assert E.counts_as_concern(state, 30, 24) is False
        assert E.above_age_label(state, 30, 24) == E.ABOVE_AGE_EMERGING


def test_yes_above_age_is_a_strength():
    assert E.is_strength(STATES["yes"], 30, 24) is True
    assert E.above_age_label(STATES["yes"], 30, 24) == E.ABOVE_AGE_STRENGTH


def test_not_sure_above_age_stays_uncertainty():
    assert E.above_age_label(STATES["not_sure"], 30, 24) == E.ABOVE_AGE_UNCERTAIN
    assert E.target_eligible(STATES["not_sure"], 30, 24) is False


# ---------------------------------------------------------------------------
# 3. at or below age is unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer,eligible", [
    ("yes", False), ("sometimes", True), ("with_help", True),
    ("no", True), ("not_sure", False)])
def test_at_or_below_age_eligibility_is_the_ordinary_rule(answer, eligible):
    for months, chrono in ((24, 24), (18, 24), (2, 60)):
        assert E.target_eligible(STATES[answer], months, chrono) is eligible


def test_a_rung_at_the_childs_exact_age_is_still_eligible():
    """Strictly greater, not >=. An age-appropriate expectation is ordinary."""
    assert E.is_above_age(24, 24) is False
    assert E.target_eligible(STATES["no"], 24, 24) is True


def test_not_sure_is_never_a_target_at_any_age():
    for months, chrono in ((2, 60), (24, 24), (30, 24), (60, 6)):
        assert E.target_eligible(STATES["not_sure"], months, chrono) is False
        assert E.counts_as_concern(STATES["not_sure"], months, chrono) is False


def test_yes_below_age_is_not_reported_as_a_strength():
    assert E.is_strength(STATES["yes"], 18, 24) is False
    assert E.above_age_label(STATES["yes"], 18, 24) is None


# ---------------------------------------------------------------------------
# 4. eligibility changes no state, and keys on age not policy
# ---------------------------------------------------------------------------

def test_eligibility_never_changes_a_recorded_state():
    record = v2.start_baseline_v2(AREA, "two_three_words", 24)
    record.bands_entered.append(24)
    rows = v2.band_skill_rows(record, 24)
    q = {"domain": record.domain, "subdomain": rows[0]["subdomain"],
         "months": 24, "milestone": rows[0]["milestone"]}
    v2.record_answer_v2(record, q, "no")
    before = {k: e.state for k, e in record.skills.items()}
    E.eligible_unresolved(record)
    E.strengths(record)
    E.eligibility_summary(record)
    assert {k: e.state for k, e in record.skills.items()} == before


def test_the_predicate_does_not_depend_on_the_policy_lookahead():
    """Changing +6 to +9 changes ROUTING, never meaning."""
    import dataclasses

    base = P.TALKING_V2_POLICY_1
    wider = dataclasses.replace(base, policy_id="hypothetical",
                                normal_upper_lookahead_months=9)
    assert wider.normal_upper_lookahead_months != \
        base.normal_upper_lookahead_months
    # The predicate takes chronological age only — there is no policy argument
    # it could consult.
    import inspect
    for fn in (E.target_eligible, E.counts_as_concern, E.is_strength,
               E.is_above_age, E.above_age_label):
        params = set(inspect.signature(fn).parameters)
        assert not (params & {"policy", "lookahead_months", "policy_id"}), fn


def test_the_module_defines_no_answer_to_state_mapping():
    """It must consume `_classify`, never restate it."""
    import ast
    import pathlib

    src = pathlib.Path(E.__file__).read_text()
    for needle in ("def _classify", '"yes":', "'yes':"):
        assert needle not in src, needle
    tree = ast.parse(src)
    imported = {n.module for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom)}
    assert "genex_core.functional_baseline" in imported


# ---------------------------------------------------------------------------
# 5. applied to real traversals
# ---------------------------------------------------------------------------

def _drive(choice, age, answer_for, budget=None):
    policy = P.TALKING_V2_POLICY_1
    budget = policy.target_finding_budget if budget is None else budget
    record = v2.start_baseline_v2(AREA, choice, age)
    for _ in range(64):
        step = T.plan_next(record,
                           lookahead_months=policy.normal_upper_lookahead_months,
                           budget=budget)
        if step.kind == T.STOP:
            return record, step.reason
        if step.enters_band is not None:
            record.bands_entered.append(step.enters_band)
        v2.record_answer_v2(record, step.question, answer_for(step.question))
    raise AssertionError("did not terminate")


def test_an_average_child_probed_above_age_gains_no_deficit():
    """The failure mode this rule exists to prevent.

    A 24-month-old is probed up to the guardrail. Answering `no` to the
    above-age rungs must not produce an eligible target.
    """
    record, reason = _drive("two_three_words", 24,
                            lambda q: "yes" if q["months"] <= 24 else "no",
                            budget=9)
    above = [e for e in record.skills.values() if e.months > 24]
    assert above, "this path should have probed above age"
    assert all(e.state == "not_demonstrated" for e in above)
    assert E.eligible_target_band(record) is None, (
        "an above-age no must not become the child's target")


def test_a_genuine_below_age_deficit_remains_eligible():
    record, reason = _drive("many_single_words", 36,
                            lambda q: "yes" if q["months"] <= 18 else "no",
                            budget=9)
    band = E.eligible_target_band(record)
    assert band is not None and band <= 36


def test_the_raw_and_eligible_target_bands_may_differ_and_both_are_kept():
    """A difference is a finding: the traversal bracketed something inert."""
    record, reason = _drive("two_three_words", 24,
                            lambda q: "yes" if q["months"] <= 24 else "no",
                            budget=9)
    raw = T.candidate_target_band(record)
    eligible = E.eligible_target_band(record)
    if raw is not None:
        assert raw > 24
    assert eligible is None


def test_strengths_are_collected_from_above_age_demonstrations():
    record, reason = _drive("two_three_words", 24, lambda q: "yes", budget=9)
    found = E.strengths(record)
    assert found, "all-yes above the guardrail should yield strengths"
    assert all(e.months > 24 for e in found)


def test_eligibility_summary_leaks_no_child_data():
    import json

    record, reason = _drive("many_single_words", 36,
                            lambda q: "yes" if q["months"] <= 18 else "no",
                            budget=9)
    summary = E.eligibility_summary(record)
    blob = json.dumps(summary)
    for forbidden in ("milestone", "skill_key", "child", "caregiver",
                      "says", "tries"):
        assert forbidden not in blob


def test_exhaustive_age_by_state_matrix_is_internally_consistent():
    """Eligibility and concern must never diverge."""
    for answer, months, chrono in itertools.product(
            ANSWERS, (2, 12, 24, 36, 60), (2, 12, 24, 36, 60)):
        state = STATES[answer]
        assert E.target_eligible(state, months, chrono) == \
            E.counts_as_concern(state, months, chrono)
        if E.is_strength(state, months, chrono):
            assert state == "demonstrated" and months > chrono
