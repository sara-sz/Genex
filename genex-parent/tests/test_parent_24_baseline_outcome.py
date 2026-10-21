"""Slice 7 — outcome states, profile summary, multi-area derivation.

Canonical design: `pilot_runtime/docs/parent_baseline_v2_design.md`.

The gates this file installs permanently:

  * exactly one of the four outcomes holds for every record
  * a SPARSE OR ABSENT PROFILE NEVER CHANGES THE OUTCOME
  * an unsupported target is never reported as no target, and never substituted
  * `unknown` never becomes `NO_TARGET_IDENTIFIED`
  * an above-age failure never produces a target
  * `multi_area` is a TRI-STATE; `NOT_OBSERVED` requires every required area
    to have been sampled
  * `not_assessed` never reads as reassurance, and its cause stays legible
  * the outcome cannot be changed by a policy/budget change
"""

from __future__ import annotations

import copy
import itertools
import json

import pytest

from api import baseline_additional as A
from api import baseline_eligibility as E
from api import baseline_micro_tracks as M
from api import baseline_outcome as O
from api import baseline_policy as P
from api import baseline_profile as B
from api import baseline_traversal as T
from genex_core import functional_baseline as v1
from genex_core import functional_baseline_v2 as v2

from test_parent_24_baseline_additional import (  # noqa: E402
    AGES, ANSWERS, CHOICES, POLICY, _answer_cycle, run_interview)

AREA = "talking"

#: Measured exemplar paths, one per outcome. Pinned so each outcome is proved
#: on a real trajectory rather than a hand-built record.
OUTCOME_PATHS = {
    O.OUTCOME_TARGET_SUPPORTED: ("many_single_words", 24,
                                 ("yes", "sometimes", "yes")),
    O.OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT: ("no_words_yet", 9,
                                              ("sometimes", "yes", "yes")),
    O.OUTCOME_NO_TARGET_IDENTIFIED: ("no_words_yet", 9, ("yes",)),
    O.OUTCOME_INSUFFICIENT_EVIDENCE: ("no_words_yet", 9,
                                      ("not_sure", "yes", "yes")),
}


def _drive(choice, age, pattern):
    return run_interview(choice, age, _answer_cycle(pattern))


def _all_paths():
    for choice, age in itertools.product(CHOICES, AGES):
        for pattern in itertools.product(ANSWERS, repeat=3):
            record, total, trace = _drive(choice, age, pattern)
            yield choice, age, pattern, record, total


# ---------------------------------------------------------------------------
# 1. the four states are exclusive, total, and all reachable
# ---------------------------------------------------------------------------

def test_every_record_gets_exactly_one_of_the_four_outcomes():
    seen = set()
    for choice, age, pattern, record, _total in _all_paths():
        result = O.derive_outcome(record)
        assert result.outcome in O.OUTCOME_STATES, (choice, age, pattern)
        seen.add(result.outcome)
    assert seen == set(O.OUTCOME_STATES), (
        f"an outcome is unreachable: {set(O.OUTCOME_STATES) - seen}")


@pytest.mark.parametrize("outcome", list(OUTCOME_PATHS))
def test_each_outcome_exemplar_still_produces_that_outcome(outcome):
    choice, age, pattern = OUTCOME_PATHS[outcome]
    record, _total, _trace = _drive(choice, age, pattern)
    assert O.derive_outcome(record).outcome == outcome


def test_only_the_two_target_outcomes_carry_a_target_band():
    for _choice, _age, _pattern, record, _total in _all_paths():
        result = O.derive_outcome(record)
        if result.outcome in O.TARGET_OUTCOMES:
            assert result.target_band is not None
            assert result.support in M.SUPPORT_STATES
            assert result.has_target is True
        else:
            assert result.target_band is None
            assert result.support is None
            assert result.has_target is False


def test_an_insufficient_outcome_always_says_why():
    for _choice, _age, _pattern, record, _total in _all_paths():
        result = O.derive_outcome(record)
        if result.outcome == O.OUTCOME_INSUFFICIENT_EVIDENCE:
            assert result.insufficient_because is not None
        else:
            assert result.insufficient_because is None


# ---------------------------------------------------------------------------
# 2. THE HEADLINE RULE: a sparse profile never changes the outcome
# ---------------------------------------------------------------------------

def test_stripping_the_entire_profile_changes_no_outcome():
    """The founder's rule, proved by construction over every path.

    Same Channel-A evidence, zero Channel-B evidence: identical outcome,
    identical target, identical support.
    """
    compared = 0
    for _choice, _age, _pattern, record, _total in _all_paths():
        full = O.derive_outcome(record)
        stripped = v2.record_from_state_v2(v2.record_to_state_v2(record))
        stripped.profile_probes.clear()
        assert O.derive_outcome(stripped) == full
        compared += 1
    assert compared > 0


def test_a_supported_target_with_zero_profile_questions_is_still_supported():
    """The founder's worked example, on a real A=9 trajectory."""
    found = None
    for choice, age, pattern, record, _total in _all_paths():
        if record.profile_probes:
            continue
        if O.derive_outcome(record).outcome == O.OUTCOME_TARGET_SUPPORTED:
            found = (choice, age, pattern, record)
            break
    assert found is not None, (
        "no path produced a supported target with an empty profile")
    _choice, _age, _pattern, record = found
    summary = O.baseline_summary(record, policy=POLICY)
    assert summary["outcome"] == O.OUTCOME_TARGET_SUPPORTED
    assert summary["target_band"] is not None
    assert summary["areas_assessed"] == []
    assert set(summary["areas_not_assessed"]) == set(B.AREA_SUBDOMAIN)
    assert summary["multi_area"] == O.MULTI_AREA_INDETERMINATE


def test_the_outcome_function_never_reads_the_profile():
    """Structural, not behavioural: there is no code path to consult."""
    import inspect

    import ast

    for fn in (O.derive_outcome, O.target_band_support,
               O._age_appropriate_evidence):
        tree = ast.parse(inspect.getsource(fn).lstrip())
        # Strip docstrings: this module DISCUSSES the profile at length and the
        # gate is about executable references, not prose.
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Module)) and \
                    ast.get_docstring(node):
                node.body = node.body[1:]
        code = ast.unparse(tree)
        assert "profile_probes" not in code, fn
        assert "profile." not in code, fn


def test_the_outcome_is_unchanged_by_a_budget_change():
    """Configuration controls the interview; it cannot control the finding."""
    import dataclasses

    wider = dataclasses.replace(POLICY, policy_id="hypothetical",
                                hard_question_max=12)
    for choice, age in itertools.product(CHOICES, (12, 24, 36, 48)):
        base_record, _t, _tr = _drive(choice, age, ("no",))
        base = O.derive_outcome(base_record)
        # The SAME record, asked about under a different policy.
        assert O.derive_outcome(base_record, policy=wider) == base


def test_derive_outcome_takes_policy_only_for_symmetry():
    import inspect

    signature = inspect.signature(O.derive_outcome)
    assert "policy" in signature.parameters
    src = inspect.getsource(O.derive_outcome)
    body = src.split('"""')[2] if src.count('"""') >= 2 else src
    assert "policy." not in body, (
        "the outcome must not consult the policy for anything")


# ---------------------------------------------------------------------------
# 3. an unsupported target is NOT no target
# ---------------------------------------------------------------------------

def test_an_unsupported_target_keeps_its_band_and_names_the_gap():
    choice, age, pattern = OUTCOME_PATHS[
        O.OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT]
    record, _total, _trace = _drive(choice, age, pattern)
    result = O.derive_outcome(record)
    assert result.outcome == O.OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT
    assert result.target_band is not None
    assert result.support in (M.UNSERVED, M.UNMAPPABLE)
    assert result.unsupported_families or result.unmappable_skills
    for family in result.unsupported_families:
        assert family not in M.SERVED_FAMILIES


def test_an_unsupported_target_is_never_substituted_for_a_served_one():
    """The frozen anti-substitution rule, at the outcome layer."""
    substituted = []
    for _choice, _age, _pattern, record, _total in _all_paths():
        result = O.derive_outcome(record)
        if result.outcome != O.OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT:
            continue
        # The band reported must be the one the evidence selected, not a
        # nearby served band.
        assert result.target_band == E.eligible_target_band(record)
        assert result.target_band == T.candidate_target_band(record)
        if result.support == M.SUPPORTED:
            substituted.append(result.target_band)
    assert substituted == []


def test_an_unsupported_target_never_reports_as_no_target():
    for _choice, _age, _pattern, record, _total in _all_paths():
        result = O.derive_outcome(record)
        if E.eligible_target_band(record) is not None:
            assert result.outcome in O.TARGET_OUTCOMES, result.outcome
            assert result.outcome != O.OUTCOME_NO_TARGET_IDENTIFIED


def test_a_band_is_supported_only_when_every_eligible_skill_is():
    """Partial support is UNSERVED, not SUPPORTED."""
    checked = 0
    for _choice, _age, _pattern, record, _total in _all_paths():
        band = E.eligible_target_band(record)
        if band is None:
            continue
        support = O.target_band_support(record, band)
        counts = support["by_support"]
        if support["support"] == M.SUPPORTED:
            assert counts[M.SUPPORTED] == support["eligible_unresolved_skills"]
        else:
            assert counts[M.UNSERVED] or counts[M.UNMAPPABLE]
        checked += 1
    assert checked > 0


def test_support_is_assessed_only_over_eligible_unresolved_skills():
    """A demonstrated sibling in the target band needs no content."""
    record, _total, _trace = _drive("many_single_words", 36,
                                    ("yes", "no", "yes"))
    band = E.eligible_target_band(record)
    if band is None:
        pytest.skip("this path produced no eligible target")
    support = O.target_band_support(record, band)
    rows = v2.band_skill_rows(record, band)
    assert support["eligible_unresolved_skills"] <= len(rows)
    assert support["eligible_unresolved_skills"] >= 1


def test_support_refuses_a_band_with_no_eligible_unresolved_skill():
    record, _total, _trace = _drive("no_words_yet", 9, ("yes",))
    mastered = T.floor_band(record)
    assert mastered is not None
    with pytest.raises(O.BaselineOutcomeError):
        O.target_band_support(record, mastered)


# ---------------------------------------------------------------------------
# 4. unknown and above-age never manufacture an outcome
# ---------------------------------------------------------------------------

def test_an_all_unknown_interview_is_insufficient_never_no_target():
    """The most misleading thing this module could say."""
    for choice, age in itertools.product(CHOICES, AGES):
        record, _total, _trace = _drive(choice, age, ("not_sure",))
        result = O.derive_outcome(record)
        assert result.outcome == O.OUTCOME_INSUFFICIENT_EVIDENCE, (choice, age)
        assert result.outcome != O.OUTCOME_NO_TARGET_IDENTIFIED
        assert result.target_band is None


def test_an_unknown_at_or_below_age_blocks_no_target_identified():
    for _choice, _age, _pattern, record, _total in _all_paths():
        result = O.derive_outcome(record)
        if result.outcome != O.OUTCOME_NO_TARGET_IDENTIFIED:
            continue
        chronological = int(record.chronological_months)
        for evidence in record.skills.values():
            if not E.is_above_age(evidence.months, chronological):
                assert evidence.state != v2.STATE_UNKNOWN


def test_an_above_age_failure_never_produces_a_target():
    for choice, age in itertools.product(CHOICES, AGES):
        record, _total, _trace = _drive(
            choice, age, ("yes",))  # demonstrates below, may fail above
        result = O.derive_outcome(record)
        if result.target_band is not None:
            assert result.target_band <= age, (choice, age, result.target_band)


def test_no_target_identified_requires_an_age_appropriate_assessment():
    for _choice, _age, _pattern, record, _total in _all_paths():
        result = O.derive_outcome(record)
        if result.outcome == O.OUTCOME_NO_TARGET_IDENTIFIED:
            assert O._age_appropriate_evidence(record)


def test_every_entered_band_is_complete_on_every_path():
    """So `band_incomplete` stays a guard rather than an outcome in practice."""
    for choice, age, pattern, record, _total in _all_paths():
        for months in record.bands_entered:
            assert v2.band_assessment(record, months).assessment_complete, (
                choice, age, pattern, months)
        assert O.derive_outcome(record).insufficient_because != \
            O.INSUFFICIENT_BAND_INCOMPLETE


# ---------------------------------------------------------------------------
# 5. multi-area is a TRI-STATE
# ---------------------------------------------------------------------------

def test_multi_area_is_never_a_boolean():
    for _choice, _age, _pattern, record, _total in _all_paths():
        state = O.multi_area_state(record, policy=POLICY)
        assert state in O.MULTI_AREA_STATES
        assert not isinstance(state, bool)


def test_all_three_multi_area_states_are_reachable():
    seen = {O.multi_area_state(r, policy=POLICY)
            for _c, _a, _p, r, _t in _all_paths()}
    assert seen == set(O.MULTI_AREA_STATES)


def test_not_observed_requires_every_required_area_to_be_sampled():
    """THE RULE. `NOT_OBSERVED` is a claim about the child, so it may only be
    made when the question was actually put."""
    checked = 0
    for _choice, _age, _pattern, record, _total in _all_paths():
        if O.multi_area_state(record, policy=POLICY) != \
                O.MULTI_AREA_NOT_OBSERVED:
            continue
        for area in POLICY.required_profile_areas:
            assert B.area_status(record, area) == B.ASSESSED, area
        assert len(A.concerning_areas(record, policy=POLICY)) <= 1
        checked += 1
    assert checked > 0


def test_an_unsampled_required_area_forces_indeterminate():
    for _choice, _age, _pattern, record, _total in _all_paths():
        unsampled = [a for a in POLICY.required_profile_areas
                     if B.area_status(record, a) == B.NOT_ASSESSED]
        state = O.multi_area_state(record, policy=POLICY)
        if unsampled and state != O.MULTI_AREA_CONFIRMED:
            assert state == O.MULTI_AREA_INDETERMINATE, (unsampled, state)


def test_indeterminate_always_names_at_least_one_gap():
    for _choice, _age, _pattern, record, _total in _all_paths():
        state = O.multi_area_state(record, policy=POLICY)
        reasons = O.multi_area_indeterminate_reasons(record, policy=POLICY)
        assert set(reasons) <= set(O.INDETERMINATE_REASONS)
        assert list(reasons) == sorted(
            reasons, key=O.INDETERMINATE_REASONS.index), "stable order"
        if state == O.MULTI_AREA_INDETERMINATE:
            assert reasons, "indeterminate must say why"
        else:
            assert reasons == ()


def test_all_three_indeterminacy_reasons_are_reachable():
    seen = set()
    for _choice, _age, _pattern, record, _total in _all_paths():
        seen.update(O.multi_area_indeterminate_reasons(record, policy=POLICY))
    assert seen == set(O.INDETERMINATE_REASONS), (
        f"unreachable: {set(O.INDETERMINATE_REASONS) - seen}")


def test_a_young_child_with_one_concern_is_indeterminate_by_inventory():
    """Social begins at 36 months, so one concern cannot support a negative."""
    for age in (18, 24, 30):
        record, _total, _trace = _drive("two_three_words", age, ("yes",))
        assert O.multi_area_state(record, policy=POLICY) == \
            O.MULTI_AREA_INDETERMINATE
        reasons = O.multi_area_indeterminate_reasons(record, policy=POLICY)
        assert O.INDETERMINATE_NO_AGE_APPROPRIATE_SKILL in reasons
        summary = O.baseline_summary(record, policy=POLICY)
        assert B.AREA_SOCIAL in summary["areas_not_assessed_unprobeable"]


# ---------------------------------------------------------------------------
# 5b. THE PRECEDENCE CORRECTION — the founder's three cases, all under 36m
#     where `conversation_narrative` cannot be sampled at all.
# ---------------------------------------------------------------------------

def _under_36m_record(age=24):
    record = v2.start_baseline_v2(AREA, "two_three_words", age)
    assert B.first_probe(AREA, B.AREA_SOCIAL, chronological_months=age) is None
    assert B.not_assessed_cause(record, B.AREA_SOCIAL) == \
        B.NOT_ASSESSED_NO_AGE_APPROPRIATE_SKILL
    return record


def _probe(record, area, answer, age=24):
    probe = B.first_probe(AREA, area, chronological_months=age)
    assert probe is not None
    v2.record_profile_probe_v2(record, B.probe_question(record, probe), answer)


def test_two_observed_concerns_confirm_even_when_social_is_unassessable():
    """CASE 1. Positive evidence is never erased by a missing area.

    Receptive concern + functional concern, child aged 24 months so the social
    area can never be sampled for them. CONFIRMED regardless.
    """
    record = _under_36m_record()
    _probe(record, B.AREA_RECEPTIVE, "no")
    _probe(record, B.AREA_FUNCTIONAL, "no")

    assert B.area_status(record, B.AREA_SOCIAL) == B.NOT_ASSESSED
    assert len(A.concerning_areas(record, policy=POLICY)) == 2
    assert O.multi_area_state(record, policy=POLICY) == O.MULTI_AREA_CONFIRMED
    assert O.multi_area_indeterminate_reasons(record, policy=POLICY) == ()

    summary = O.baseline_summary(record, policy=POLICY)
    assert summary["multi_area"] == O.MULTI_AREA_CONFIRMED
    assert summary["multi_area_is_determinate"] is True
    assert summary["multi_area_indeterminate_reasons"] == []
    # And the missing area is still reported honestly alongside it.
    assert B.AREA_SOCIAL in summary["areas_not_assessed_unprobeable"]


def test_confirmed_survives_every_kind_of_missing_evidence():
    """CASE 1, generalised: unprobeable, budget-consumed and uncertain all at
    once must not downgrade two observed concerns."""
    record = _under_36m_record()
    _probe(record, B.AREA_RECEPTIVE, "no")
    _probe(record, B.AREA_FUNCTIONAL, "no")
    # Add an uncertain answer in a concerning area, and leave intelligibility
    # (a non-required fallback area) unsampled.
    lower = B.downward_probe(AREA, B.AREA_RECEPTIVE, below_months=24,
                             asked_months=[24])
    v2.record_profile_probe_v2(record, B.probe_question(record, lower),
                               "not_sure")
    assert B.area_status(record, B.AREA_INTELLIGIBILITY) == B.NOT_ASSESSED
    assert O.uncertain_areas(record) == (B.AREA_RECEPTIVE,)
    assert O.multi_area_state(record, policy=POLICY) == O.MULTI_AREA_CONFIRMED


def test_one_concern_with_social_unassessable_is_indeterminate():
    """CASE 2. One observed concern cannot support a reassuring negative."""
    record = _under_36m_record()
    _probe(record, B.AREA_RECEPTIVE, "no")
    _probe(record, B.AREA_FUNCTIONAL, "yes")

    assert A.concerning_areas(record, policy=POLICY) == (B.AREA_RECEPTIVE,)
    assert O.multi_area_state(record, policy=POLICY) == \
        O.MULTI_AREA_INDETERMINATE
    reasons = O.multi_area_indeterminate_reasons(record, policy=POLICY)
    assert reasons == (O.INDETERMINATE_NO_AGE_APPROPRIATE_SKILL,)

    summary = O.baseline_summary(record, policy=POLICY)
    assert summary["areas_with_concern"] == [B.AREA_RECEPTIVE]
    assert summary["multi_area_is_determinate"] is False


def test_no_concerns_with_social_unassessable_is_indeterminate():
    """CASE 3. `not_assessed` is never interpreted as non-concerning."""
    record = _under_36m_record()
    _probe(record, B.AREA_RECEPTIVE, "yes")
    _probe(record, B.AREA_FUNCTIONAL, "yes")

    assert A.concerning_areas(record, policy=POLICY) == ()
    assert O.multi_area_state(record, policy=POLICY) == \
        O.MULTI_AREA_INDETERMINATE
    assert O.multi_area_indeterminate_reasons(record, policy=POLICY) == \
        (O.INDETERMINATE_NO_AGE_APPROPRIATE_SKILL,)
    summary = O.baseline_summary(record, policy=POLICY)
    assert summary["multi_area"] != O.MULTI_AREA_NOT_OBSERVED


def test_a_sampled_but_unknown_area_blocks_not_observed():
    """The gap the sampled-or-not test missed.

    All three required areas sampled, one concern, and the non-concerning
    functional area holds only an `unknown`. That area could still turn out
    concerning, so NOT_OBSERVED is not available.
    """
    record = v2.start_baseline_v2(AREA, "short_sentences", 48)
    for area, answer in ((B.AREA_RECEPTIVE, "no"),
                         (B.AREA_FUNCTIONAL, "not_sure"),
                         (B.AREA_SOCIAL, "yes")):
        _probe(record, area, answer, age=48)

    for area in POLICY.required_profile_areas:
        assert B.area_status(record, area) == B.ASSESSED
    assert A.concerning_areas(record, policy=POLICY) == (B.AREA_RECEPTIVE,)
    assert O.multi_area_state(record, policy=POLICY) == \
        O.MULTI_AREA_INDETERMINATE
    assert O.multi_area_indeterminate_reasons(record, policy=POLICY) == \
        (O.INDETERMINATE_UNCERTAINTY,)


def test_an_unknown_inside_an_already_concerning_area_is_not_a_reason():
    """It is already counted; more certainty there cannot make a SECOND area
    concerning."""
    record = v2.start_baseline_v2(AREA, "short_sentences", 48)
    for area, answer in ((B.AREA_RECEPTIVE, "no"),
                         (B.AREA_FUNCTIONAL, "yes"),
                         (B.AREA_SOCIAL, "yes")):
        _probe(record, area, answer, age=48)
    lower = B.downward_probe(AREA, B.AREA_RECEPTIVE, below_months=24,
                             asked_months=[24])
    v2.record_profile_probe_v2(record, B.probe_question(record, lower),
                               "not_sure")
    assert B.AREA_RECEPTIVE in O.uncertain_areas(record)
    assert O.multi_area_state(record, policy=POLICY) == \
        O.MULTI_AREA_NOT_OBSERVED


def test_a_non_required_fallback_area_never_blocks_not_observed():
    """Deliberate asymmetry: CONFIRMED counts any area, the negative is gated
    on the required set, or NOT_OBSERVED would be unreachable."""
    record = v2.start_baseline_v2(AREA, "short_sentences", 48)
    for area, answer in ((B.AREA_RECEPTIVE, "yes"),
                         (B.AREA_FUNCTIONAL, "yes"),
                         (B.AREA_SOCIAL, "yes")):
        _probe(record, area, answer, age=48)
    assert B.area_status(record, B.AREA_INTELLIGIBILITY) == B.NOT_ASSESSED
    assert O.multi_area_state(record, policy=POLICY) == \
        O.MULTI_AREA_NOT_OBSERVED


def test_a_concern_in_a_non_required_area_still_counts_toward_confirmed():
    """The other half of the asymmetry: positive evidence counts anywhere."""
    record = v2.start_baseline_v2(AREA, "short_sentences", 48)
    _probe(record, B.AREA_RECEPTIVE, "no", age=48)
    _probe(record, B.AREA_INTELLIGIBILITY, "no", age=48)
    assert set(A.concerning_areas(record, policy=POLICY)) == \
        {B.AREA_RECEPTIVE, B.AREA_INTELLIGIBILITY}
    assert O.multi_area_state(record, policy=POLICY) == O.MULTI_AREA_CONFIRMED


def test_confirmed_is_never_downgraded_on_any_real_path():
    """Swept: two observed concerns always CONFIRM, whatever is missing."""
    checked = 0
    for _choice, _age, _pattern, record, _total in _all_paths():
        if len(A.concerning_areas(record, policy=POLICY)) >= 2:
            assert O.multi_area_state(record, policy=POLICY) == \
                O.MULTI_AREA_CONFIRMED
            checked += 1
    assert checked > 0


def test_confirmed_requires_two_genuinely_sampled_concerning_areas():
    checked = 0
    for _choice, _age, _pattern, record, _total in _all_paths():
        if O.multi_area_state(record, policy=POLICY) != \
                O.MULTI_AREA_CONFIRMED:
            continue
        concerning = A.concerning_areas(record, policy=POLICY)
        assert len(concerning) >= 2
        chronological = int(record.chronological_months)
        for area in concerning:
            evidence = B.area_evidence(record, area)
            assert evidence
            assert any(E.counts_as_concern(e.state, e.months, chronological)
                       for e in evidence)
        checked += 1
    assert checked > 0


# ---------------------------------------------------------------------------
# 6. not_assessed never reads as reassurance
# ---------------------------------------------------------------------------

def test_the_three_distinctions_stay_separable():
    """Concern found / never sampled / sampled but uncertain."""
    for _choice, _age, _pattern, record, _total in _all_paths():
        summary = O.baseline_summary(record, policy=POLICY)
        concern = set(summary["areas_with_concern"])
        not_assessed = set(summary["areas_not_assessed"])
        uncertain = set(summary["areas_uncertain"])
        assessed = set(summary["areas_assessed"])

        assert not (not_assessed & assessed)
        assert not_assessed | assessed == set(B.AREA_SUBDOMAIN)
        # An unsampled area can be neither concerning nor uncertain: both are
        # statements about evidence it does not have.
        assert not (not_assessed & concern)
        assert not (not_assessed & uncertain)
        # And the two causes partition the unsampled areas exactly.
        assert set(summary["areas_not_assessed_recoverable"]) | \
            set(summary["areas_not_assessed_unprobeable"]) == not_assessed
        assert not (set(summary["areas_not_assessed_recoverable"]) &
                    set(summary["areas_not_assessed_unprobeable"]))
        # The R3-eligible subset is narrower, and never names an area the
        # inventory cannot reach.
        assert set(summary["areas_recoverable_by_question"]) <= \
            set(summary["areas_not_assessed_recoverable"])
        assert set(summary["areas_recoverable_by_question"]) <= \
            set(POLICY.required_profile_areas)


def test_no_summary_field_reads_as_reassurance_about_an_unsampled_area():
    for _choice, _age, _pattern, record, _total in _all_paths():
        summary = O.baseline_summary(record, policy=POLICY)
        for area in summary["areas_not_assessed"]:
            report = summary["profile"][area]
            assert report["status"] == B.NOT_ASSESSED
            assert report["assessed"] is False
            assert report["probes"] == 0
            assert report["concern"] is False      # absence, not clearance
            assert report["concern_bands"] == []
            assert report["lowest_probe_months"] is None
            assert report["not_assessed_cause"] is not None


def test_no_reported_value_is_a_reassuring_word():
    """Scanned over VALUES, not keys. `normal_question_target` is a budget
    field name; what must never appear is a reassuring STATE."""
    def values(node):
        if isinstance(node, dict):
            for v in node.values():
                yield from values(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                yield from values(v)
        elif isinstance(node, str):
            yield node

    for choice, age in itertools.product(CHOICES, (12, 24, 36, 48)):
        for pattern in (("yes",), ("no",), ("not_sure",)):
            record, _total, _trace = _drive(choice, age, pattern)
            for value in values(O.baseline_summary(record, policy=POLICY)):
                lowered = value.lower()
                for forbidden in ("clear", "normal", "typical", "passed",
                                  "healthy", "no_concern", "fine",
                                  "on_track", "all_good"):
                    assert forbidden not in lowered, (value, forbidden)


def test_uncertain_is_distinct_from_not_assessed():
    record, _total, _trace = _drive("two_three_words", 48, ("not_sure",))
    summary = O.baseline_summary(record, policy=POLICY)
    assert summary["areas_uncertain"], "this path must produce uncertainty"
    for area in summary["areas_uncertain"]:
        assert area in summary["areas_assessed"]
        assert area not in summary["areas_not_assessed"]


# ---------------------------------------------------------------------------
# 7. the ledger, and PHI safety
# ---------------------------------------------------------------------------

def test_the_summary_carries_the_additional_question_ledger():
    record, total, _trace = _drive("short_sentences", 36, ("no",))
    summary = O.baseline_summary(record, policy=POLICY)
    ledger = summary["additional_questions"]
    assert ledger["questions_total"] == total
    assert ledger["additional_questions_used"] == \
        max(0, total - POLICY.normal_question_target)
    assert ledger["reasons_in_order"] == [e["reason_code"]
                                          for e in record.additional_reasons]
    assert ledger["exceeded_normal_target"] is (
        total > POLICY.normal_question_target)
    assert ledger["channel_a_questions"] + ledger["channel_b_questions"] == \
        total


def test_the_summary_leaks_no_child_data():
    for choice, age in itertools.product(CHOICES, (12, 24, 36, 48)):
        for pattern in (("no",), ("not_sure",), ("yes",), ("sometimes",)):
            record, _total, _trace = _drive(choice, age, pattern)
            blob = json.dumps(O.baseline_summary(record, policy=POLICY))
            for forbidden in ("milestone", "skill_key", "child", "caregiver",
                              "says", "points", "talks", "waves", "follows",
                              "tries"):
                assert forbidden not in blob, (choice, age, pattern, forbidden)


def test_the_summary_is_json_serialisable_and_deterministic():
    record, _total, _trace = _drive("short_sentences", 36, ("no",))
    first = json.dumps(O.baseline_summary(record, policy=POLICY),
                       sort_keys=True)
    second = json.dumps(O.baseline_summary(record, policy=POLICY),
                        sort_keys=True)
    assert first == second


def test_the_summary_is_derived_and_mutates_nothing():
    record, _total, _trace = _drive("short_sentences", 36, ("no",))
    before = copy.deepcopy(v2.record_to_state_v2(record))
    O.baseline_summary(record, policy=POLICY)
    O.derive_outcome(record)
    O.multi_area_state(record, policy=POLICY)
    assert v2.record_to_state_v2(record) == before


def test_the_summary_pins_the_served_family_set():
    """A content change must be visible in anything derived from it."""
    record, _total, _trace = _drive("short_sentences", 36, ("no",))
    summary = O.baseline_summary(record, policy=POLICY)
    assert summary["served_family_set_id"] == M.SERVED_FAMILY_SET_ID
    assert summary["served_family_set_digest"] == \
        M.served_family_set_digest()
    assert len(summary["served_family_set_digest"]) == 64
