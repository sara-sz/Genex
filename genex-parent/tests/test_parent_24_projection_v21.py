"""A2 v2.1 — the Parent-side sibling client.

The gates this file installs permanently:

  * the frozen v2 body builder is UNCHANGED, five keys, same digest
  * v2.1 is seven keys: v2's five plus two SEPARATE additions
  * the clinician profile carries derived state only — the Pilot's own codec
    refuses the same things again, and both refusals are exercised
  * Parent's content evaluation is PROVENANCE, carries its digest, and is
    never folded into the clinician profile
  * adding content changes the evaluation without touching the baseline
  * all four outcomes produce a sendable v2.1 body, including the two with no
    target
"""

from __future__ import annotations

import copy
import itertools
import json

import pytest

from api import baseline_contract as C
from api import baseline_micro_tracks as M
from api import baseline_outcome as O
from api import baseline_policy as P
from api import functional_baseline_v2_api as api
from api import parent_baseline_projection_v2_client as pv2
from api import parent_baseline_projection_v21_client as pv21
from genex_core import functional_baseline_v2 as v2

from test_parent_24_baseline_additional import (  # noqa: E402
    AGES, ANSWERS, CHOICES, POLICY, _answer_cycle, run_interview)

DOMAIN = "talking_and_communicating"
AREA = "talking"

SUPPORTED_PATH = ("many_single_words", 24, ("yes", "sometimes", "yes"))
WITHHELD_PATH = ("no_words_yet", 9, ("sometimes", "yes", "yes"))
NO_TARGET_PATH = ("no_words_yet", 9, ("yes",))
INSUFFICIENT_PATH = ("no_words_yet", 9, ("not_sure", "yes", "yes"))


def _record_for(spec):
    choice, age, pattern = spec
    record, _total, _trace = run_interview(choice, age,
                                           _answer_cycle(pattern))
    v2.finalize_v2(record)
    return record


def _doc_for(record):
    return {v2.STATE_KEY_V2: {DOMAIN: v2.record_to_state_v2(record)},
            api.ENVELOPE_STATE_KEY_V2: {DOMAIN: {"finalized": True}},
            "brain_state": {"child": {
                "chronological_months": record.chronological_months}},
            "age_in_months": record.chronological_months}


def _bodies(spec):
    """The v2 and v2.1 bodies for the same finalized record."""
    record = _record_for(spec)
    doc = _doc_for(record)
    stored = api.finalized_record(doc, DOMAIN)
    payload = api.build_projection_payload(doc, DOMAIN)
    v2_body = pv2.build_body(source_session_id="sess_fictional",
                             record=stored, payload=payload)
    v21_body = pv21.build_body_v21(source_session_id="sess_fictional",
                                   record=stored, payload=payload,
                                   decoded_record=record)
    return record, v2_body, v21_body


# ---------------------------------------------------------------------------
# 1. v2 is unchanged
# ---------------------------------------------------------------------------

def test_the_v2_builder_still_produces_exactly_five_keys():
    _record, v2_body, _v21 = _bodies(SUPPORTED_PATH)
    assert set(v2_body) == set(pv2.BODY_KEYS)
    assert len(pv2.BODY_KEYS) == 5


def test_both_versions_agree_about_the_source_identity():
    """A v2 and a v2.1 projection of one record came from the same thing."""
    _record, v2_body, v21_body = _bodies(SUPPORTED_PATH)
    assert v21_body["source_session_id"] == v2_body["source_session_id"]
    assert v21_body["source_record_digest"] == v2_body["source_record_digest"]


def test_the_clinical_parts_are_byte_identical_across_versions():
    _record, v2_body, v21_body = _bodies(SUPPORTED_PATH)
    for part in ("summary", "skills", "band_totals"):
        assert json.dumps(v21_body[part], sort_keys=True) == \
            json.dumps(v2_body[part], sort_keys=True), part


# ---------------------------------------------------------------------------
# 2. v2.1 shape
# ---------------------------------------------------------------------------

def test_the_v21_body_is_seven_keys_with_two_separate_additions():
    _record, _v2, v21_body = _bodies(SUPPORTED_PATH)
    assert set(v21_body) == set(pv21.BODY_KEYS_V21)
    assert len(pv21.BODY_KEYS_V21) == 7
    assert set(pv21.BODY_KEYS_V21) - set(pv2.BODY_KEYS) == {
        "clinician_profile", "parent_content_evaluation"}
    # Neither addition is nested inside the other.
    assert "content_digest" not in v21_body["clinician_profile"]
    assert "areas" not in v21_body["parent_content_evaluation"]


def test_the_profile_key_set_is_exactly_the_approved_one():
    _record, _v2, v21_body = _bodies(SUPPORTED_PATH)
    assert set(v21_body["clinician_profile"]) == set(pv21.PROFILE_KEYS)
    assert set(v21_body["parent_content_evaluation"]) == set(
        pv21.CONTENT_EVALUATION_KEYS)


def test_every_outcome_produces_a_sendable_body():
    seen = set()
    for spec in (SUPPORTED_PATH, WITHHELD_PATH, NO_TARGET_PATH,
                 INSUFFICIENT_PATH):
        record, _v2, v21_body = _bodies(spec)
        outcome = O.derive_outcome(record).outcome
        seen.add(outcome)
        assert v21_body["clinician_profile"]["outcome"] == outcome
    assert seen == set(O.OUTCOME_STATES)


def test_the_two_no_target_outcomes_report_not_applicable_serviceability():
    """There is nothing whose serviceability could be asserted."""
    for spec in (NO_TARGET_PATH, INSUFFICIENT_PATH):
        record, _v2, v21_body = _bodies(spec)
        assert O.derive_outcome(record).target_band is None
        assert v21_body["parent_content_evaluation"][
            "target_serviceability"] == pv21.SERVICEABILITY_NOT_APPLICABLE


def test_a_supported_target_reports_supported_and_an_unservable_one_does_not():
    _record, _v2, supported = _bodies(SUPPORTED_PATH)
    assert supported["parent_content_evaluation"][
        "target_serviceability"] == pv21.SERVICEABILITY_SUPPORTED
    _record2, _v2b, withheld = _bodies(WITHHELD_PATH)
    assert withheld["parent_content_evaluation"][
        "target_serviceability"] == pv21.SERVICEABILITY_UNSUPPORTED


# ---------------------------------------------------------------------------
# 3. nothing raw travels, and the Pilot refuses it again
# ---------------------------------------------------------------------------

def test_the_profile_carries_no_month_prose_ref_or_reason_code():
    for choice, age in itertools.product(CHOICES, (12, 24, 36, 48)):
        for pattern in (("no",), ("yes",), ("not_sure",), ("sometimes",)):
            record, _total, _trace = run_interview(choice, age,
                                                   _answer_cycle(pattern))
            v2.finalize_v2(record)
            profile = pv21.build_clinician_profile(record, policy=POLICY)
            blob = json.dumps(profile)
            for forbidden in ("months", "milestone", "skill_key", "rung",
                              "score", "probe", "R1_", "R2_", "R3_", "R4_",
                              "R5_", "says", "points", "talks"):
                assert forbidden not in blob, (choice, age, forbidden)


def test_no_profile_value_is_a_developmental_month():
    months = {12, 15, 18, 24, 30, 36, 48, 60}

    def integers(node):
        if isinstance(node, dict):
            for value in node.values():
                yield from integers(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                yield from integers(value)
        elif isinstance(node, int) and not isinstance(node, bool):
            yield node

    for spec in (SUPPORTED_PATH, WITHHELD_PATH, NO_TARGET_PATH,
                 INSUFFICIENT_PATH):
        record, _v2, v21_body = _bodies(spec)
        for value in integers(v21_body["clinician_profile"]):
            assert value not in months, value


def test_the_pilot_codec_accepts_what_parent_builds():
    """Both halves of the boundary, exercised against each other."""
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from pilot_backend.transport.projection_v21_request import decode_body_v21

    for spec in (SUPPORTED_PATH, WITHHELD_PATH, NO_TARGET_PATH,
                 INSUFFICIENT_PATH):
        _record, _v2, v21_body = _bodies(spec)
        decoded = decode_body_v21(v21_body)
        assert decoded["clinician_profile"]["areas"]
        assert decoded["parent_content_evaluation"]["content_digest"]


def test_the_pilot_v2_codec_refuses_a_v21_body():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from pilot_backend.transport.projection_v2_request import (
        ProjectionV2RequestError, decode_body)

    _record, v2_body, v21_body = _bodies(SUPPORTED_PATH)
    assert decode_body(v2_body)            # the v2 body still decodes
    with pytest.raises(ProjectionV2RequestError):
        decode_body(v21_body)


def test_the_area_states_use_the_wire_vocabulary_not_the_contract_one():
    """The contract view folds the cause INTO the state; the wire separates
    them, so a consumer can act on the state without parsing the cause out."""
    record = _record_for(NO_TARGET_PATH)
    profile = pv21.build_clinician_profile(record, policy=POLICY)
    for area, state in profile["areas"].items():
        assert state in ("concern_observed", "uncertain",
                         "no_concern_observed", "not_assessed"), (area, state)
    for area, cause in profile["not_assessed_causes"].items():
        assert profile["areas"][area] == "not_assessed"
        assert cause in ("budget_consumed", "no_age_appropriate_skill")


# ---------------------------------------------------------------------------
# 4. the content evaluation is current, the baseline is historical
# ---------------------------------------------------------------------------

def test_adding_content_changes_the_evaluation_not_the_baseline():
    record = _record_for(WITHHELD_PATH)
    stored_before = copy.deepcopy(v2.record_to_state_v2(record))
    before = pv21.build_parent_content_evaluation(record)
    assert before["target_serviceability"] == pv21.SERVICEABILITY_UNSUPPORTED

    missing = set(O.derive_outcome(record).unsupported_families)
    assert missing
    original = M.SERVED_FAMILIES
    try:
        M.SERVED_FAMILIES = frozenset(original | missing)
        after = pv21.build_parent_content_evaluation(record)
        assert after["target_serviceability"] == pv21.SERVICEABILITY_SUPPORTED
        assert after["content_digest"] != before["content_digest"]
        # The finalized baseline did not move.
        assert v2.record_to_state_v2(record) == stored_before
    finally:
        M.SERVED_FAMILIES = original
    assert pv21.build_parent_content_evaluation(record) == before


def test_the_evaluation_always_carries_the_content_state_it_used():
    for spec in (SUPPORTED_PATH, WITHHELD_PATH, NO_TARGET_PATH):
        record = _record_for(spec)
        evaluation = pv21.build_parent_content_evaluation(record)
        assert evaluation["content_set_id"] == M.SERVED_FAMILY_SET_ID
        assert evaluation["content_digest"] == M.served_family_set_digest()
        assert len(evaluation["content_digest"]) == 64


def test_the_profile_does_not_depend_on_the_content_state():
    """HISTORICAL vs CURRENT, at the wire format.

    The clinician profile is derived clinical context. Content changing must
    move the EVALUATION and leave the profile's area states alone.
    """
    record = _record_for(WITHHELD_PATH)
    before = pv21.build_clinician_profile(record, policy=POLICY)
    original = M.SERVED_FAMILIES
    try:
        M.SERVED_FAMILIES = frozenset()
        after = pv21.build_clinician_profile(record, policy=POLICY)
    finally:
        M.SERVED_FAMILIES = original
    assert after["areas"] == before["areas"]
    assert after["multi_area"] == before["multi_area"]
    assert after["not_assessed_causes"] == before["not_assessed_causes"]
    # Only the OUTCOME may move, and only between the two target states.
    if after["outcome"] != before["outcome"]:
        assert {after["outcome"], before["outcome"]} <= set(
            O.TARGET_OUTCOMES)


# ---------------------------------------------------------------------------
# 5. determinism and the builder's discipline
# ---------------------------------------------------------------------------

def test_the_body_is_deterministic():
    record = _record_for(SUPPORTED_PATH)
    doc = _doc_for(record)
    stored = api.finalized_record(doc, DOMAIN)
    payload = api.build_projection_payload(doc, DOMAIN)
    first = pv21.build_body_v21(source_session_id="s", record=stored,
                                payload=payload, decoded_record=record)
    second = pv21.build_body_v21(source_session_id="s", record=stored,
                                 payload=payload, decoded_record=record)
    assert json.dumps(first, sort_keys=True) == json.dumps(second,
                                                           sort_keys=True)


def test_the_builder_constructs_the_profile_by_name():
    """So a field added to the contract view cannot start travelling."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(pv21.build_clinician_profile).lstrip())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.Module)) and \
                ast.get_docstring(node):
            node.body = node.body[1:]
    code = ast.unparse(tree)
    # No wholesale copy of the summary into the body.
    assert "dict(summary)" not in code
    assert "**summary" not in code
    for key in pv21.PROFILE_KEYS:
        assert f"'{key}'" in code or f'"{key}"' in code, key


def test_building_a_body_mutates_nothing():
    record = _record_for(SUPPORTED_PATH)
    before = copy.deepcopy(v2.record_to_state_v2(record))
    doc = _doc_for(record)
    pv21.build_body_v21(
        source_session_id="s", record=api.finalized_record(doc, DOMAIN),
        payload=api.build_projection_payload(doc, DOMAIN),
        decoded_record=record)
    assert v2.record_to_state_v2(record) == before


def test_an_incomplete_payload_is_refused():
    record = _record_for(SUPPORTED_PATH)
    doc = _doc_for(record)
    with pytest.raises(pv2.ProjectionRejected):
        pv21.build_body_v21(source_session_id="s",
                            record=api.finalized_record(doc, DOMAIN),
                            payload={"summary": {}}, decoded_record=record)
