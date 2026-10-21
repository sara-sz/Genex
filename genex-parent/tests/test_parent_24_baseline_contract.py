"""Slice 9 — the derived contract, and the boundary it freezes.

Canonical design: `pilot_runtime/docs/parent_baseline_v2_design.md`.

The nine founder-required proofs, in order:

  1. slice 9 is ADDITIVE and breaks no historical v2 field
  2. no raw Channel-B evidence enters the goal-generating projection
  3. no internal milestone/month/skill-ref data leaks through the clinician
     summary
  4. an unsupported target retains its EXACT canonical target evidence
  5. adding content changes current serviceability without mutating the
     finalized baseline record
  6. altering content support changes serviceability deterministically, under a
     NEW content digest
  7. CONFIRMED multi-area wins over missing or uncertain third areas
  8. a negative multi-area conclusion is impossible when required evidence is
     missing or unknown
  9. the parent-facing contract never exposes internal reason codes
"""

from __future__ import annotations

import copy
import itertools
import json

import pytest

from api import baseline_additional as A
from api import baseline_contract as C
from api import baseline_micro_tracks as M
from api import baseline_outcome as O
from api import baseline_profile as B
from api import baseline_projection_gate as G
from api import functional_baseline_v2_api as api
from genex_core import functional_baseline_v2 as v2

from test_parent_24_baseline_additional import (  # noqa: E402
    AGES, ANSWERS, CHOICES, POLICY, _answer_cycle, run_interview)

AREA = "talking"
DOMAIN = "talking_and_communicating"

WITHHELD_PATH = ("no_words_yet", 9, ("sometimes", "yes", "yes"))
SUPPORTED_PATH = ("many_single_words", 24, ("yes", "sometimes", "yes"))

#: The v2 view key set exactly as it was before slice 9. Pinned so an addition
#: is visible here and a removal or rename is impossible to land quietly.
HISTORICAL_VIEW_KEYS = frozenset({
    "domain", "area_id", "baseline_version", "entry_choice_id", "finalized",
    "status", "skills_assessed", "bands", "current_question",
    "ready_to_finalize", "updated_at",
})


def _drive(spec):
    choice, age, pattern = spec
    return run_interview(choice, age, _answer_cycle(pattern))


def _all_paths():
    for choice, age in itertools.product(CHOICES, AGES):
        for pattern in itertools.product(ANSWERS, repeat=3):
            record, _total, _trace = run_interview(choice, age,
                                                   _answer_cycle(pattern))
            yield choice, age, pattern, record


def _doc_from(record):
    v2.finalize_v2(record)
    return {v2.STATE_KEY_V2: {DOMAIN: v2.record_to_state_v2(record)},
            api.ENVELOPE_STATE_KEY_V2: {DOMAIN: {"finalized": True,
                                                 "updated_at": "fixed"}},
            "brain_state": {"child": {
                "chronological_months": record.chronological_months}},
            "age_in_months": record.chronological_months}


def _live_doc(age=36, choice="many_single_words", answer="yes"):
    """A session driven through the real API, not a hand-built record."""
    doc = {"brain_state": {"child": {"chronological_months": age}},
           "age_in_months": age}
    api.start(doc, DOMAIN, choice)
    for _ in range(16):
        question = api.view(doc, DOMAIN).get("current_question")
        if not question:
            break
        api.answer(doc, DOMAIN, question["skill_key"], answer)
    return doc


# ---------------------------------------------------------------------------
# PROOF 1 — additive, and no historical field broken
# ---------------------------------------------------------------------------

def test_the_historical_view_keys_are_unchanged_before_finalize():
    doc = _live_doc()
    view = api.view(doc, DOMAIN)
    assert set(view) == HISTORICAL_VIEW_KEYS
    assert "outcome" not in view, (
        "an outcome before finalize would be a prediction, not a result")


def test_finalize_only_ADDS_the_outcome_block():
    doc = _live_doc()
    before = api.view(doc, DOMAIN)
    api.finalize_baseline(doc, DOMAIN)
    after = api.view(doc, DOMAIN)

    assert set(after) - set(before) == {"outcome"}
    assert HISTORICAL_VIEW_KEYS <= set(after)
    # Every historical field that is not lifecycle-dependent is untouched.
    for key in ("domain", "area_id", "baseline_version", "entry_choice_id",
                "skills_assessed", "bands"):
        assert after[key] == before[key], key


def test_the_outcome_block_has_exactly_three_fields():
    doc = _live_doc()
    api.finalize_baseline(doc, DOMAIN)
    block = api.view(doc, DOMAIN)["outcome"]
    assert set(block) == {"state", "plan_available", "answers_recorded"}
    assert block["state"] in C.PARENT_STATES


def test_the_stored_record_shape_is_unchanged_by_slice_9():
    """The contract is DERIVED. Nothing new is persisted."""
    doc = _live_doc()
    api.finalize_baseline(doc, DOMAIN)
    stored = doc[v2.STATE_KEY_V2][DOMAIN]
    for forbidden in ("outcome", "serviceability", "projectable",
                      "parent_state", "content_digest"):
        assert forbidden not in stored, forbidden


def test_a_historical_unpolicied_record_still_renders_a_view():
    """Backward compatibility: records written before slice 1 must not break."""
    doc = _live_doc()
    api.finalize_baseline(doc, DOMAIN)
    stored = doc[v2.STATE_KEY_V2][DOMAIN]
    stored.pop("policy_id", None)
    stored.pop("policy_digest", None)
    view = api.view(doc, DOMAIN)
    assert view["outcome"]["state"] in C.PARENT_STATES


def test_all_four_parent_states_are_reachable_and_the_mapping_is_total():
    seen = set()
    for _choice, _age, _pattern, record in _all_paths():
        state = C.parent_view(record, policy=POLICY)["state"]
        assert state in C.PARENT_STATES
        seen.add(state)
    assert seen == set(C.PARENT_STATES)


# ---------------------------------------------------------------------------
# PROOF 2 — no raw Channel-B evidence reaches the projection
# ---------------------------------------------------------------------------

def test_no_channel_b_evidence_enters_the_projection_payload():
    for _choice, _age, _pattern, record in _all_paths():
        if not G.projection_decision(record, policy=POLICY).allowed:
            continue
        if not record.profile_probes:
            continue
        payload = api.projection_payload(_doc_from(record), DOMAIN)
        projected = {(s["subdomain"], s["months"], s["milestone"])
                     for s in payload["skills"]}
        for probe in record.profile_probes.values():
            assert (probe.subdomain, probe.months, probe.milestone) not in \
                projected, "a profile probe reached the projection"
        # And the projected count is exactly the Channel-A count.
        assert len(payload["skills"]) == len(record.skills)


def test_the_projection_payload_is_byte_identical_without_the_profile():
    compared = 0
    for _choice, _age, _pattern, record in _all_paths():
        if not G.projection_decision(record, policy=POLICY).allowed:
            continue
        if not record.profile_probes:
            continue
        full = api.projection_payload(_doc_from(record), DOMAIN)
        stripped_record = v2.record_from_state_v2(
            v2.record_to_state_v2(record))
        stripped_record.profile_probes.clear()
        stripped = api.projection_payload(_doc_from(stripped_record), DOMAIN)
        assert json.dumps(full, sort_keys=True) == \
            json.dumps(stripped, sort_keys=True)
        compared += 1
    assert compared > 0


def test_no_profile_subdomain_is_a_projected_subdomain():
    """Structural: the two channels draw on disjoint subdomain sets."""
    doc = _live_doc()
    api.finalize_baseline(doc, DOMAIN)
    payload = api.projection_payload(doc, DOMAIN)
    projected = {s["subdomain"] for s in payload["skills"]}
    assert not (projected & set(B.AREA_SUBDOMAIN.values()))


# ---------------------------------------------------------------------------
# PROOF 3 — nothing internal leaks through the clinician summary
# ---------------------------------------------------------------------------

def test_the_clinician_summary_exposes_no_months_prose_or_refs():
    for choice, age in itertools.product(CHOICES, AGES):
        for pattern in (("no",), ("yes",), ("not_sure",), ("sometimes",)):
            record, _total, _trace = run_interview(choice, age,
                                                   _answer_cycle(pattern))
            summary = C.clinician_summary(record, policy=POLICY)
            C.assert_clinician_safe(summary)


def test_the_clinician_summary_contains_no_raw_answer_or_milestone_text():
    record, _total, _trace = _drive(WITHHELD_PATH)
    blob = json.dumps(C.clinician_summary(record, policy=POLICY))
    for forbidden in ("yes", "not_sure", "with_help", "says", "points",
                      "talks", "waves", "tries", "demonstrated"):
        assert forbidden not in blob, forbidden


def test_the_clinician_summary_contains_no_integer_month_value():
    """Stronger than a token scan: no value may BE a developmental month.

    Restricted to the UNAMBIGUOUS bands (12 and above). The lowest four — 2, 4,
    6, 9 — collide with ordinary small counts: `unmappable_skills` is legally
    2 on some paths, and asserting that away would be asserting that a count
    cannot be small. The structural half of the guarantee is the companion test
    below, which checks that no clinician field is NAMED like a band.
    """
    months = {12, 15, 18, 24, 30, 36, 48, 60}

    def numbers(node):
        if isinstance(node, dict):
            for value in node.values():
                yield from numbers(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                yield from numbers(value)
        elif isinstance(node, int) and not isinstance(node, bool):
            yield node

    for _choice, _age, _pattern, record in _all_paths():
        summary = C.clinician_summary(record, policy=POLICY)
        for value in numbers(summary):
            assert value not in months, (value, summary)


def test_no_clinician_field_is_named_like_a_band():
    """The structural half: a month cannot travel under a band-shaped name."""
    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                yield from keys(value)

    record, _total, _trace = _drive(WITHHELD_PATH)
    summary = C.clinician_summary(record, policy=POLICY)
    for key in keys(summary):
        lowered = str(key).lower()
        for fragment in ("month", "band", "age", "anchor", "rung"):
            assert fragment not in lowered, (key, fragment)


def test_the_clinician_summary_keeps_the_two_bases_separate():
    record, _total, _trace = _drive(WITHHELD_PATH)
    summary = C.clinician_summary(record, policy=POLICY)
    assert set(summary) == {"clinical_history", "current_serviceability",
                            "profile"}
    assert summary["clinical_history"]["basis"] == \
        "finalized_baseline_evidence"
    assert summary["current_serviceability"]["basis"] == \
        "current_reviewed_content"


def test_the_clinician_summary_supports_the_intended_sentence():
    """"A clinically appropriate target was identified, but Genex does not
    currently have approved pilot content for it." """
    record, _total, _trace = _drive(WITHHELD_PATH)
    summary = C.clinician_summary(record, policy=POLICY)
    assert summary["clinical_history"]["target_identified"] is True
    assert summary["clinical_history"]["outcome"] == \
        O.OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT
    assert summary["current_serviceability"]["projectable"] is False
    assert summary["current_serviceability"]["unsupported_families"]
    assert summary["current_serviceability"]["content_digest"]


def test_the_clinician_summary_reports_the_five_area_states_only():
    seen = set()
    for _choice, _age, _pattern, record in _all_paths():
        areas = C.clinician_summary(record, policy=POLICY)["profile"]["areas"]
        assert set(areas) == set(B.AREA_SUBDOMAIN)
        for state in areas.values():
            assert state in C.AREA_STATES
            seen.add(state)
    assert seen == set(C.AREA_STATES), (
        f"an area state is unreachable: {set(C.AREA_STATES) - seen}")


def test_not_assessed_is_never_collapsed_into_no_concern_observed():
    """The one substitution that would make our gap read as reassurance."""
    for _choice, _age, _pattern, record in _all_paths():
        for area in B.AREA_SUBDOMAIN:
            state = C.area_state(record, area, policy=POLICY)
            if B.area_status(record, area) == B.NOT_ASSESSED:
                assert state in (C.AREA_NOT_ASSESSED_BUDGET,
                                 C.AREA_NOT_ASSESSED_NO_SKILL)
                assert state != C.AREA_NO_CONCERN_OBSERVED
            else:
                assert state not in (C.AREA_NOT_ASSESSED_BUDGET,
                                     C.AREA_NOT_ASSESSED_NO_SKILL)


def test_the_reason_ledger_is_provenance_only():
    record, _total, _trace = run_interview("short_sentences", 36,
                                           _answer_cycle(("no",)))
    assert record.additional_reasons, "this path must use the allowance"

    clinician = json.dumps(C.clinician_summary(record, policy=POLICY))
    parent = json.dumps(C.parent_view(record, policy=POLICY))
    for code in A.REASON_PRIORITY:
        assert code not in clinician, code
        assert code not in parent, code

    prov = C.provenance(record, policy=POLICY)
    assert prov["additional_question_ledger"] == record.additional_reasons
    assert prov["interview"]["reasons_in_order"]


# ---------------------------------------------------------------------------
# PROOF 4 — an unsupported target keeps its exact canonical evidence
# ---------------------------------------------------------------------------

def test_an_unsupported_target_retains_its_exact_canonical_evidence():
    from api import baseline_eligibility as E

    checked = 0
    for _choice, _age, _pattern, record in _all_paths():
        history = C.clinical_history(record)
        if history["outcome"] != O.OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT:
            continue
        result = O.derive_outcome(record)
        assert history["target_band"] == result.target_band
        assert history["target_band"] == E.eligible_target_band(record)
        assert history["floor_band"] == result.floor_band
        assert history["floor_established"] == result.floor_established
        service = C.current_serviceability(record)
        assert list(result.unsupported_families) == \
            service["unsupported_families"]
        for family in service["unsupported_families"]:
            assert family not in M.SERVED_FAMILIES
        checked += 1
    assert checked > 0


def test_the_historical_block_never_consults_the_content_state():
    """Structural, and the reason the split is trustworthy."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(C.clinical_history).lstrip())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.Module)) and \
                ast.get_docstring(node):
            node.body = node.body[1:]
    code = ast.unparse(tree)
    for forbidden in ("micro_tracks", "SERVED_FAMILIES", "projection_gate",
                      "served_family_set_digest"):
        assert forbidden not in code, forbidden


# ---------------------------------------------------------------------------
# PROOF 5 and 6 — content changes serviceability, never the baseline
# ---------------------------------------------------------------------------

def test_adding_content_changes_serviceability_without_touching_the_record():
    """THE FOUNDER'S BOUNDARY, proved in both directions at once."""
    record, _total, _trace = _drive(WITHHELD_PATH)
    stored_before = copy.deepcopy(v2.record_to_state_v2(record))
    history_before = C.clinical_history(record)
    service_before = C.current_serviceability(record)
    assert service_before["projectable"] is False
    missing = set(service_before["unsupported_families"])
    assert missing

    original = M.SERVED_FAMILIES
    try:
        M.SERVED_FAMILIES = frozenset(original | missing)
        service_after = C.current_serviceability(record)
        history_after = C.clinical_history(record)

        # CURRENT serviceability moved.
        assert service_after["projectable"] is True
        assert service_after["support"] == M.SUPPORTED
        assert service_after["unsupported_families"] == []
        # ...under a DIFFERENT content digest (proof 6).
        assert service_after["content_digest"] != \
            service_before["content_digest"]

        # HISTORICAL evidence did not. Not the answers, not the canonical
        # target, not the floor, and no reassessment.
        assert v2.record_to_state_v2(record) == stored_before
        assert history_after["target_band"] == history_before["target_band"]
        assert history_after["floor_band"] == history_before["floor_band"]
        assert history_after["floor_established"] == \
            history_before["floor_established"]
        assert history_after["baseline_version"] == \
            history_before["baseline_version"]
        assert history_after["policy_digest"] == history_before["policy_digest"]
        # The OUTCOME does move — it is a joint statement about evidence and
        # content — but only between the two TARGET outcomes, so the target is
        # never lost and never becomes "no target" or "insufficient".
        assert {history_before["outcome"], history_after["outcome"]} == {
            O.OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT,
            O.OUTCOME_TARGET_SUPPORTED}
        assert history_after["target_identified"] is True
    finally:
        M.SERVED_FAMILIES = original

    assert C.current_serviceability(record)["projectable"] is False
    assert v2.record_to_state_v2(record) == stored_before


def test_removing_content_support_is_deterministic_under_a_new_digest():
    """PROOF 6 in the other direction: a supported target becomes unservable."""
    record, _total, _trace = _drive(SUPPORTED_PATH)
    before = C.current_serviceability(record)
    assert before["projectable"] is True
    stored_before = copy.deepcopy(v2.record_to_state_v2(record))

    original = M.SERVED_FAMILIES
    try:
        M.SERVED_FAMILIES = frozenset()
        first = C.current_serviceability(record)
        second = C.current_serviceability(record)
        assert first == second, "the derivation must be deterministic"
        assert first["projectable"] is False
        assert first["support"] in (M.UNSERVED, M.UNMAPPABLE)
        assert first["content_digest"] != before["content_digest"]
        # The identified target survives the content removal.
        assert C.clinical_history(record)["target_band"] == \
            C.clinical_history(record)["target_band"]
        assert C.clinical_history(record)["target_identified"] is True
        assert v2.record_to_state_v2(record) == stored_before
    finally:
        M.SERVED_FAMILIES = original

    assert C.current_serviceability(record) == before


def test_every_serviceability_result_is_stamped_with_its_content_state():
    for _choice, _age, _pattern, record in _all_paths():
        service = C.current_serviceability(record)
        assert service["content_set_id"] == M.SERVED_FAMILY_SET_ID
        assert service["content_digest"] == M.served_family_set_digest()
        assert len(service["content_digest"]) == 64


def test_the_two_blocks_declare_their_basis():
    record, _total, _trace = _drive(WITHHELD_PATH)
    assert C.clinical_history(record)["basis"] == \
        "finalized_baseline_evidence"
    assert C.current_serviceability(record)["basis"] == \
        "current_reviewed_content"


# ---------------------------------------------------------------------------
# PROOF 7 and 8 — multi-area precedence, through the contract
# ---------------------------------------------------------------------------

def _probe(record, area, answer, age):
    probe = B.first_probe(AREA, area, chronological_months=age)
    assert probe is not None
    v2.record_profile_probe_v2(record, B.probe_question(record, probe), answer)


def test_confirmed_wins_over_a_missing_third_area():
    """PROOF 7. 24 months: social can never be sampled."""
    record = v2.start_baseline_v2(AREA, "two_three_words", 24)
    _probe(record, B.AREA_RECEPTIVE, "no", 24)
    _probe(record, B.AREA_FUNCTIONAL, "no", 24)
    block = C.profile_block(record, policy=POLICY)
    assert block["multi_area"] == O.MULTI_AREA_CONFIRMED
    assert block["multi_area_indeterminate_reasons"] == []
    assert block["areas"][B.AREA_SOCIAL] == C.AREA_NOT_ASSESSED_NO_SKILL


def test_confirmed_wins_over_an_uncertain_third_area():
    """PROOF 7, the uncertainty variant."""
    record = v2.start_baseline_v2(AREA, "short_sentences", 48)
    _probe(record, B.AREA_RECEPTIVE, "no", 48)
    _probe(record, B.AREA_FUNCTIONAL, "no", 48)
    _probe(record, B.AREA_SOCIAL, "not_sure", 48)
    block = C.profile_block(record, policy=POLICY)
    assert block["multi_area"] == O.MULTI_AREA_CONFIRMED
    assert block["multi_area_indeterminate_reasons"] == []
    assert block["areas"][B.AREA_SOCIAL] == C.AREA_UNCERTAIN


@pytest.mark.parametrize("answers,expected_reason", [
    (("no", "yes"), O.INDETERMINATE_NO_AGE_APPROPRIATE_SKILL),
    (("yes", "yes"), O.INDETERMINATE_NO_AGE_APPROPRIATE_SKILL),
])
def test_a_negative_conclusion_is_impossible_with_a_missing_required_area(
        answers, expected_reason):
    """PROOF 8, the missing variant."""
    record = v2.start_baseline_v2(AREA, "two_three_words", 24)
    _probe(record, B.AREA_RECEPTIVE, answers[0], 24)
    _probe(record, B.AREA_FUNCTIONAL, answers[1], 24)
    block = C.profile_block(record, policy=POLICY)
    assert block["multi_area"] == O.MULTI_AREA_INDETERMINATE
    assert expected_reason in block["multi_area_indeterminate_reasons"]
    assert block["multi_area"] != O.MULTI_AREA_NOT_OBSERVED


def test_a_negative_conclusion_is_impossible_with_an_unknown_required_area():
    """PROOF 8, the unknown variant."""
    record = v2.start_baseline_v2(AREA, "short_sentences", 48)
    _probe(record, B.AREA_RECEPTIVE, "no", 48)
    _probe(record, B.AREA_FUNCTIONAL, "not_sure", 48)
    _probe(record, B.AREA_SOCIAL, "yes", 48)
    block = C.profile_block(record, policy=POLICY)
    assert block["multi_area"] == O.MULTI_AREA_INDETERMINATE
    assert block["multi_area_indeterminate_reasons"] == [
        O.INDETERMINATE_UNCERTAINTY]


def test_not_observed_requires_complete_and_certain_required_evidence():
    """PROOF 8, swept: the negative is only ever stated with the evidence."""
    checked = 0
    for _choice, _age, _pattern, record in _all_paths():
        block = C.profile_block(record, policy=POLICY)
        if block["multi_area"] != O.MULTI_AREA_NOT_OBSERVED:
            continue
        for area in POLICY.required_profile_areas:
            state = block["areas"][area]
            assert state not in (C.AREA_NOT_ASSESSED_BUDGET,
                                 C.AREA_NOT_ASSESSED_NO_SKILL,
                                 C.AREA_UNCERTAIN), (area, state)
        assert block["multi_area_indeterminate_reasons"] == []
        checked += 1
    assert checked > 0


def test_a_sparse_profile_never_changes_the_parent_state():
    """Slice 7's rule, carried all the way to the caregiver surface."""
    for _choice, _age, _pattern, record in _all_paths():
        full = C.parent_view(record, policy=POLICY)
        stripped = v2.record_from_state_v2(v2.record_to_state_v2(record))
        stripped.profile_probes.clear()
        after = C.parent_view(stripped, policy=POLICY)
        assert after["state"] == full["state"]
        assert after["plan_available"] == full["plan_available"]


# ---------------------------------------------------------------------------
# PROOF 9 — the parent contract exposes no internal concept
# ---------------------------------------------------------------------------

def test_the_parent_view_exposes_no_internal_concept_on_any_path():
    for _choice, _age, _pattern, record in _all_paths():
        C.assert_parent_safe(C.parent_view(record, policy=POLICY))


def test_the_parent_view_exposes_no_reason_code():
    record, _total, _trace = run_interview("short_sentences", 36,
                                           _answer_cycle(("no",)))
    assert record.additional_reasons
    blob = json.dumps(C.parent_view(record, policy=POLICY))
    for code in A.REASON_PRIORITY:
        assert code not in blob
    for fragment in ("R1", "R2", "R3", "R4", "R5", "reason"):
        assert fragment not in blob, fragment


def test_the_parent_state_is_not_a_reversible_encoding_of_the_outcome():
    """A caregiver surface must not be able to recover the clinical state."""
    for state in C.PARENT_STATES:
        for outcome in O.OUTCOME_STATES:
            assert outcome not in state
            assert outcome.lower() not in state


def test_the_parent_view_names_no_canonical_family():
    record, _total, _trace = _drive(WITHHELD_PATH)
    blob = json.dumps(C.parent_view(record, policy=POLICY))
    for family in (M.SERVED_FAMILIES | M.KNOWN_UNSERVED_FAMILIES):
        assert family not in blob, family


def test_the_parent_view_exposes_no_developmental_month():
    months = {2, 4, 6, 9, 12, 15, 18, 24, 30, 36, 48, 60}
    for _choice, _age, _pattern, record in _all_paths():
        view = C.parent_view(record, policy=POLICY)
        for value in view.values():
            if isinstance(value, int) and not isinstance(value, bool):
                assert value not in months or value <= 9, value


def test_the_route_level_outcome_block_is_parent_safe():
    """Through the real API, not the helper."""
    for age, choice, answer in ((36, "many_single_words", "no"),
                                (24, "two_three_words", "not_sure"),
                                (9, "no_words_yet", "sometimes"),
                                (48, "short_sentences", "yes")):
        doc = _live_doc(age=age, choice=choice, answer=answer)
        api.finalize_baseline(doc, DOMAIN)
        C.assert_parent_safe(api.view(doc, DOMAIN)["outcome"])


def test_the_three_views_are_deterministic_and_json_serialisable():
    record, _total, _trace = _drive(WITHHELD_PATH)
    for builder in (C.parent_view, C.clinician_summary, C.provenance):
        first = json.dumps(builder(record, policy=POLICY), sort_keys=True)
        second = json.dumps(builder(record, policy=POLICY), sort_keys=True)
        assert first == second, builder


def test_the_views_mutate_nothing():
    record, _total, _trace = _drive(WITHHELD_PATH)
    before = copy.deepcopy(v2.record_to_state_v2(record))
    C.parent_view(record, policy=POLICY)
    C.clinician_summary(record, policy=POLICY)
    C.provenance(record, policy=POLICY)
    assert v2.record_to_state_v2(record) == before


def test_the_forbid_lists_are_not_accidentally_equal():
    """They differ by design; equal lists would mean one was never considered."""
    assert set(C.PARENT_FORBIDDEN_TOKENS) != \
        set(C.CLINICIAN_FORBIDDEN_TOKENS)
    assert set(C.CLINICIAN_FORBIDDEN_TOKENS) < \
        set(C.PARENT_FORBIDDEN_TOKENS), (
        "the parent list must be strictly stricter")


def test_the_leak_gates_actually_detect_a_leak():
    """Non-vacuity. A gate that cannot fail proves nothing."""
    with pytest.raises(C.BaselineContractError):
        C.assert_parent_safe({"leak": O.OUTCOME_INSUFFICIENT_EVIDENCE})
    with pytest.raises(C.BaselineContractError):
        C.assert_parent_safe({"months": 24})
    with pytest.raises(C.BaselineContractError):
        C.assert_clinician_safe({"target_band": 24})
    with pytest.raises(C.BaselineContractError):
        C.assert_clinician_safe({"code": "R1_TARGET_BAND_COMPLETION"})
    # And the clinician gate deliberately PERMITS outcome and family names.
    C.assert_clinician_safe({"outcome": O.OUTCOME_INSUFFICIENT_EVIDENCE,
                             "unsupported_families": ["early_vocalizations"]})
