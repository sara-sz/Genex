"""Slice 8 — withholding the goal-generating projection.

Canonical design: `pilot_runtime/docs/parent_baseline_v2_design.md`.

The gates this file installs permanently:

  * `TARGET_UNSUPPORTED_BY_CONTENT` never reaches the A2 v2 projection
  * no other outcome's projection behaviour changes
  * the refusal NEVER converts the outcome, manufactures a target, or drops
    the target band or family names
  * the gate sits in the ONE function every projection passes through
  * the decision is DERIVED, so content landing unblocks the child
"""

from __future__ import annotations

import copy
import itertools
import json

import pytest

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

#: Measured exemplars, one per outcome.
WITHHELD_PATH = ("no_words_yet", 9, ("sometimes", "yes", "yes"))
SUPPORTED_PATH = ("many_single_words", 24, ("yes", "sometimes", "yes"))
NO_TARGET_PATH = ("no_words_yet", 9, ("yes",))
INSUFFICIENT_PATH = ("no_words_yet", 9, ("not_sure", "yes", "yes"))


def _drive(spec):
    choice, age, pattern = spec
    return run_interview(choice, age, _answer_cycle(pattern))


def _all_paths():
    for choice, age in itertools.product(CHOICES, AGES):
        for pattern in itertools.product(ANSWERS, repeat=3):
            record, total, _trace = run_interview(choice, age,
                                                  _answer_cycle(pattern))
            yield choice, age, pattern, record


def _doc_from(record):
    """A finalized session document carrying this record, for the API path."""
    v2.finalize_v2(record)
    return {v2.STATE_KEY_V2: {DOMAIN: v2.record_to_state_v2(record)},
            api.ENVELOPE_STATE_KEY_V2: {DOMAIN: {"finalized": True}},
            "brain_state": {"child": {
                "chronological_months": record.chronological_months}},
            "age_in_months": record.chronological_months}


# ---------------------------------------------------------------------------
# 1. exactly one outcome is withheld
# ---------------------------------------------------------------------------

def test_the_withheld_set_is_exactly_the_unsupported_outcome():
    assert G.WITHHELD_OUTCOMES == (
        O.OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT,)


def test_every_path_is_decided_and_only_unsupported_is_withheld():
    seen = {}
    for choice, age, pattern, record in _all_paths():
        decision = G.projection_decision(record, policy=POLICY)
        assert decision.status in (G.PROJECTION_ALLOWED, G.PROJECTION_WITHHELD)
        expected = (G.PROJECTION_WITHHELD
                    if decision.outcome ==
                    O.OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT
                    else G.PROJECTION_ALLOWED)
        assert decision.status == expected, (choice, age, pattern)
        seen[decision.outcome] = decision.status
    assert seen == {
        O.OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT: G.PROJECTION_WITHHELD,
        O.OUTCOME_TARGET_SUPPORTED: G.PROJECTION_ALLOWED,
        O.OUTCOME_NO_TARGET_IDENTIFIED: G.PROJECTION_ALLOWED,
        O.OUTCOME_INSUFFICIENT_EVIDENCE: G.PROJECTION_ALLOWED,
    }, "every outcome must be exercised, and decided as specified"


def test_a_supported_target_still_projects():
    record, _total, _trace = _drive(SUPPORTED_PATH)
    assert O.derive_outcome(record).outcome == O.OUTCOME_TARGET_SUPPORTED
    decision = G.assert_projectable(record)
    assert decision.allowed is True
    assert decision.withheld_because is None
    payload = api.projection_payload(_doc_from(record), DOMAIN)
    assert set(payload) == {"summary", "skills", "band_totals"}


@pytest.mark.parametrize("spec,expected", [
    (NO_TARGET_PATH, O.OUTCOME_NO_TARGET_IDENTIFIED),
    (INSUFFICIENT_PATH, O.OUTCOME_INSUFFICIENT_EVIDENCE),
])
def test_the_other_two_outcomes_are_unchanged_by_this_slice(spec, expected):
    """Narrow by intent: the existing contract already accepts these and lets
    the Pilot decide, and there is proven coverage that an unresolved baseline
    can still be projected."""
    record, _total, _trace = _drive(spec)
    assert O.derive_outcome(record).outcome == expected
    assert G.assert_projectable(record).allowed is True
    payload = api.projection_payload(_doc_from(record), DOMAIN)
    assert payload["skills"]


# ---------------------------------------------------------------------------
# 2. THE BLOCK, at the real product path
# ---------------------------------------------------------------------------

def test_an_unsupported_target_cannot_build_a_projection_payload():
    record, _total, _trace = _drive(WITHHELD_PATH)
    assert O.derive_outcome(record).outcome == \
        O.OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT
    with pytest.raises(G.ProjectionWithheld):
        api.projection_payload(_doc_from(record), DOMAIN)


def test_the_gate_is_in_the_one_function_every_projection_passes_through():
    """Structural. A check at the route could be bypassed by a new caller."""
    import ast
    import inspect

    src = inspect.getsource(api.projection_payload)
    tree = ast.parse(src.lstrip())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.Module)) and \
                ast.get_docstring(node):
            node.body = node.body[1:]
    code = ast.unparse(tree)
    assert "assert_projectable" in code

    # And `main.py` has no second way to build a body.
    import pathlib
    main_src = pathlib.Path("api/main.py").read_text()
    assert main_src.count("projection_payload(") == 1


def test_the_ungated_builder_has_exactly_one_caller_in_api():
    """The split must not become a bypass.

    `build_projection_payload` exists so the Pilot can be the authority on
    content availability — it cannot exercise that authority on a payload it
    never sees. But exactly one caller inside `api/` may use it: the gated
    wrapper. A second would be a route reaching the Pilot without the slice-8
    refusal, which is the whole thing this gate prevents.
    """
    import pathlib as _pathlib
    import re

    callers = {}
    for path in sorted(_pathlib.Path("api").glob("*.py")):
        hits = re.findall(r"\bbuild_projection_payload\b", path.read_text())
        # Subtract the definition itself in the module that declares it.
        count = len(hits)
        if path.name == "functional_baseline_v2_api.py":
            count -= 1          # the `def`
            count -= 1          # the __all__ entry
        if count > 0:
            callers[path.name] = count
    assert callers == {"functional_baseline_v2_api.py": 1}, callers


def test_main_reaches_the_pilot_only_through_the_gated_function():
    import pathlib as _pathlib

    main_src = _pathlib.Path("api/main.py").read_text()
    assert "build_projection_payload" not in main_src
    assert main_src.count("projection_payload(") == 1


def test_the_gated_wrapper_refuses_before_it_builds():
    """Order matters: a withheld record must not even construct a payload."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(api.projection_payload).lstrip())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.Module)) and \
                ast.get_docstring(node):
            node.body = node.body[1:]
    code = ast.unparse(tree)
    assert code.index("assert_projectable") < \
        code.index("build_projection_payload")


def test_the_ungated_builder_really_is_ungated():
    """Non-vacuity: it builds the payload slice 8 refuses."""
    record, _total, _trace = _drive(WITHHELD_PATH)
    doc = _doc_from(record)
    with pytest.raises(G.ProjectionWithheld):
        api.projection_payload(doc, DOMAIN)
    payload = api.build_projection_payload(doc, DOMAIN)
    assert set(payload) == {"summary", "skills", "band_totals"}
    assert payload["skills"]


def test_every_withheld_path_is_refused_at_the_api():
    refused = 0
    for _choice, _age, _pattern, record in _all_paths():
        if G.projection_decision(record, policy=POLICY).allowed:
            continue
        with pytest.raises(G.ProjectionWithheld):
            api.projection_payload(_doc_from(record), DOMAIN)
        refused += 1
    assert refused > 0


# The HTTP mapping is proved on the SDK-backed surface instead of here —
# `api.main` needs FIREBASE_PROJECT_ID and fastapi, and this suite runs in the
# dependency-pure CI job. See
# `test_parent_24_baseline_projection.py::test_a_withheld_projection_is_a_409`.


# ---------------------------------------------------------------------------
# 3. THE FOUR THINGS IT MUST NEVER DO
# ---------------------------------------------------------------------------

def test_the_outcome_is_never_converted():
    """Not to NO_TARGET_IDENTIFIED, not to INSUFFICIENT_EVIDENCE, not at all."""
    for _choice, _age, _pattern, record in _all_paths():
        before = O.derive_outcome(record).outcome
        decision = G.projection_decision(record, policy=POLICY)
        assert decision.outcome == before
        try:
            G.assert_projectable(record)
        except G.ProjectionWithheld as exc:
            assert exc.decision.outcome == before
            assert exc.decision.outcome != O.OUTCOME_NO_TARGET_IDENTIFIED
            assert exc.decision.outcome != O.OUTCOME_INSUFFICIENT_EVIDENCE
        assert O.derive_outcome(record).outcome == before


def test_no_other_target_is_manufactured():
    from api import baseline_eligibility as E

    for _choice, _age, _pattern, record in _all_paths():
        decision = G.projection_decision(record, policy=POLICY)
        if decision.target_band is None:
            continue
        # Exactly the band the evidence selected, never a nearby served one.
        assert decision.target_band == E.eligible_target_band(record)
        if decision.status == G.PROJECTION_WITHHELD:
            assert decision.target_support in (M.UNSERVED, M.UNMAPPABLE)


def test_the_target_band_and_family_names_survive_the_refusal():
    record, _total, _trace = _drive(WITHHELD_PATH)
    result = O.derive_outcome(record)
    with pytest.raises(G.ProjectionWithheld) as caught:
        G.assert_projectable(record)
    decision = caught.value.decision
    assert decision.target_band == result.target_band
    assert decision.target_band is not None
    assert decision.unsupported_families == result.unsupported_families
    assert decision.unsupported_families or decision.unmappable_skills
    for family in decision.unsupported_families:
        assert family not in M.SERVED_FAMILIES


def test_the_clinician_sentence_is_renderable_from_the_refusal_alone():
    """"A clinically appropriate target was identified, but there is no
    approved pilot content for it." Every element present."""
    record, _total, _trace = _drive(WITHHELD_PATH)
    with pytest.raises(G.ProjectionWithheld) as caught:
        G.assert_projectable(record)
    blob = caught.value.decision.as_dict()
    assert blob["target_identified"] is True
    assert blob["target_band"] is not None
    assert blob["outcome"] == O.OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT
    assert blob["withheld_because"] == G.WITHHELD_UNSUPPORTED_BY_CONTENT
    assert blob["unsupported_families"] or blob["unmappable_skills"]
    assert blob["served_family_set_digest"]


def test_the_summary_still_reports_the_identified_target():
    """The finding must be visible to a clinician surface, not just refused."""
    record, _total, _trace = _drive(WITHHELD_PATH)
    summary = O.baseline_summary(record, policy=POLICY)
    assert summary["outcome"] == O.OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT
    assert summary["target_band"] is not None
    assert summary["target_support"] in (M.UNSERVED, M.UNMAPPABLE)
    assert summary["unsupported_families"] or summary["unmappable_skills"]


# ---------------------------------------------------------------------------
# 4. THE DECISION IS DERIVED, SO CONTENT UNBLOCKS THE CHILD
# ---------------------------------------------------------------------------

def test_nothing_is_written_to_the_record():
    for spec in (WITHHELD_PATH, SUPPORTED_PATH, NO_TARGET_PATH,
                 INSUFFICIENT_PATH):
        record, _total, _trace = _drive(spec)
        before = copy.deepcopy(v2.record_to_state_v2(record))
        try:
            G.assert_projectable(record)
        except G.ProjectionWithheld:
            pass
        G.projection_decision(record, policy=POLICY)
        assert v2.record_to_state_v2(record) == before


def test_no_projection_withheld_flag_is_stored():
    record, _total, _trace = _drive(WITHHELD_PATH)
    stored = v2.record_to_state_v2(record)
    blob = json.dumps(stored)
    for forbidden in ("withheld", "projectable", "unsupported", "support",
                      "outcome"):
        assert forbidden not in blob, forbidden


def test_serving_the_family_unblocks_the_same_record():
    """THE REASON IT IS DERIVED. Track 2 landing a card must reach the child
    with no migration, no re-interview and no backfill."""
    record, _total, _trace = _drive(WITHHELD_PATH)
    decision = G.projection_decision(record, policy=POLICY)
    assert decision.status == G.PROJECTION_WITHHELD
    missing = set(decision.unsupported_families)
    assert missing, "this path must be blocked by a NAMED family"

    original = M.SERVED_FAMILIES
    try:
        M.SERVED_FAMILIES = frozenset(original | missing)
        after = G.projection_decision(record, policy=POLICY)
        assert after.status == G.PROJECTION_ALLOWED
        assert after.outcome == O.OUTCOME_TARGET_SUPPORTED
        assert after.target_band == decision.target_band, (
            "the SAME target, now servable")
        # And the API path now builds a payload for the identical record.
        payload = api.projection_payload(_doc_from(record), DOMAIN)
        assert payload["skills"]
    finally:
        M.SERVED_FAMILIES = original

    assert G.projection_decision(record, policy=POLICY).status == \
        G.PROJECTION_WITHHELD, "the fixture must restore the real content state"


def test_the_decision_pins_the_content_state_that_produced_it():
    record, _total, _trace = _drive(WITHHELD_PATH)
    decision = G.projection_decision(record, policy=POLICY)
    assert decision.served_family_set_id == M.SERVED_FAMILY_SET_ID
    assert decision.served_family_set_digest == M.served_family_set_digest()
    assert len(decision.served_family_set_digest) == 64


# ---------------------------------------------------------------------------
# 5. PHI safety and determinism
# ---------------------------------------------------------------------------

def test_the_decision_leaks_no_child_data():
    for choice, age in itertools.product(CHOICES, (12, 24, 36, 48)):
        for pattern in (("no",), ("sometimes",), ("not_sure",), ("yes",)):
            record, _total, _trace = run_interview(choice, age,
                                                   _answer_cycle(pattern))
            blob = json.dumps(
                G.projection_decision(record, policy=POLICY).as_dict())
            for forbidden in ("milestone", "skill_key", "child", "caregiver",
                              "says", "points", "talks", "tries"):
                assert forbidden not in blob, (choice, age, pattern, forbidden)


def test_the_exception_message_is_phi_safe():
    record, _total, _trace = _drive(WITHHELD_PATH)
    with pytest.raises(G.ProjectionWithheld) as caught:
        G.assert_projectable(record)
    assert getattr(caught.value, "PHI_SAFE_MESSAGE", False) is True
    message = str(caught.value)
    assert G.WITHHELD_UNSUPPORTED_BY_CONTENT in message
    for forbidden in ("milestone", "says", "child", "caregiver"):
        assert forbidden not in message


def test_the_decision_is_deterministic():
    record, _total, _trace = _drive(WITHHELD_PATH)
    first = G.projection_decision(record, policy=POLICY)
    second = G.projection_decision(record, policy=POLICY)
    assert first == second
    assert json.dumps(first.as_dict(), sort_keys=True) == \
        json.dumps(second.as_dict(), sort_keys=True)


def test_the_decision_is_unaffected_by_the_profile():
    """Slice 7's rule, carried through the gate."""
    for _choice, _age, _pattern, record in _all_paths():
        full = G.projection_decision(record, policy=POLICY)
        stripped = v2.record_from_state_v2(v2.record_to_state_v2(record))
        stripped.profile_probes.clear()
        assert G.projection_decision(stripped, policy=POLICY) == full
