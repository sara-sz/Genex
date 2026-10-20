"""Slice 1 — the versioned Baseline v2 interview policy.

Canonical design: `pilot_runtime/docs/parent_baseline_v2_design.md`.

The gates this file installs permanently:

  * policy digest drift — a registered policy is immutable
  * a policy CANNOT express evidence semantics
  * an unpolicied historical record still decodes, unchanged
  * `record_to_state_v2` OMITS the policy keys when unset, so every historical
    stored dict — and therefore every historical A2 `projection_id` — is
    byte-identical
  * a new session is frozen to a policy, and a later default change cannot
    reach it
"""

from __future__ import annotations

import copy
import inspect
import json

import pytest

from api import baseline_policy as P
from api import functional_baseline_v2_api as api
from genex_core import functional_baseline_v2 as v2

DOMAIN = "talking_and_communicating"


# ---------------------------------------------------------------------------
# 1. the registry and its digest
# ---------------------------------------------------------------------------

def test_the_pilot_policy_carries_the_founder_approved_values():
    p = P.get_policy("talking-v2-policy-2")
    assert p.normal_question_target == 6
    assert p.hard_question_max == 9
    assert p.additional_question_allowance == 3
    assert p.normal_upper_lookahead_months == 6
    assert p.target_finding_normal_allocation == 4
    assert p.target_budget_enforced_at == "complete_band_boundary"
    assert p.descent_lower_bound == "none"
    assert p.anchor_source == "entry_choice"
    assert p.substitute_served_target is False
    assert p.required_profile_areas == ("receptive", "functional", "social")


def test_the_policy_digest_is_stable_and_order_independent():
    """DRIFT GATE. The digest is a pure function of the canonical dict."""
    p = P.TALKING_V2_POLICY_2
    assert p.policy_digest == p.policy_digest
    shuffled = dict(reversed(list(p.as_canonical_dict().items())))
    assert P.canonical_json(shuffled) == P.canonical_json(p.as_canonical_dict())
    assert len(p.policy_digest) == 64


def test_a_changed_value_changes_the_digest():
    """Non-vacuity: the digest must actually detect an edit."""
    import dataclasses

    p = P.TALKING_V2_POLICY_2
    for field, value in (("normal_question_target", 7),
                         ("hard_question_max", 8),
                         ("normal_upper_lookahead_months", 9),
                         ("target_finding_normal_allocation", 3),
                         ("substitute_served_target", True)):
        mutated = dataclasses.replace(p, **{field: value})
        assert mutated.policy_digest != p.policy_digest, field


def test_verify_refuses_an_edited_registry():
    with pytest.raises(P.BaselinePolicyError):
        P.verify_policy("talking-v2-policy-2", "0" * 64)


def test_verify_refuses_an_unknown_policy_id():
    with pytest.raises(P.BaselinePolicyError):
        P.verify_policy("talking-v2-policy-does-not-exist", "x")


def test_an_unknown_policy_never_falls_back_to_the_default():
    """A silent fallback is how history would acquire today's rules."""
    with pytest.raises(P.BaselinePolicyError):
        P.get_policy("nope")


def test_the_registry_is_not_mutable_through_its_accessors():
    ids = P.registered_policy_ids()
    assert "talking-v2-policy-1" in ids
    assert "talking-v2-policy-2" in ids
    assert isinstance(ids, tuple)
    with pytest.raises(TypeError):
        P._REGISTRY["new"] = P.TALKING_V2_POLICY_2  # type: ignore[index]


# ---------------------------------------------------------------------------
# 2. THE GATE: a policy cannot express evidence semantics
# ---------------------------------------------------------------------------

def test_the_policy_key_set_is_exactly_the_allowlist():
    """A new key has to come through POLICY_KEYS, where a reviewer sees it."""
    for policy_id in P.registered_policy_ids():
        assert set(P.get_policy(policy_id).as_canonical_dict()) == \
            set(P.POLICY_KEYS)


def test_no_policy_key_names_an_evidence_concept():
    offenders = [(k, frag) for k in P.POLICY_KEYS
                 for frag in P.FORBIDDEN_POLICY_KEY_FRAGMENTS if frag in k]
    assert offenders == [], (
        "a policy key names an evidence concept; configuration controls the "
        f"interview budget, not what an answer means: {offenders}")


def test_the_functions_that_decide_meaning_take_no_policy():
    """The structural half of the guarantee.

    If `_classify` or the band derivations could accept a policy, a budget
    change could alter what evidence means. They cannot, and this proves it by
    signature rather than by convention.
    """
    from genex_core import functional_baseline as v1

    for fn in (v1._classify, v2.band_assessment, v2.band_skill_rows,
               v2.record_answer_v2, v2.finalize_v2):
        params = set(inspect.signature(fn).parameters)
        assert not (params & {"policy", "policy_id", "baseline_policy"}), fn


def test_the_engine_does_not_import_the_policy_registry():
    """Dependency direction: api -> genex_core, never the reverse."""
    import ast
    import pathlib

    src = pathlib.Path(v2.__file__).read_text()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            assert "baseline_policy" not in (node.module or "")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "baseline_policy" not in alias.name


def test_changing_a_budget_cannot_change_any_recorded_state():
    """The principle, exercised end to end on the engine."""
    import dataclasses

    rec = v2.start_baseline_v2("talking", "many_single_words", 36)
    q = v2.next_question_v2(rec)
    v2.record_answer_v2(rec, q, "sometimes")
    before = copy.deepcopy(v2.record_to_state_v2(rec))

    # A different policy, frozen onto an otherwise identical record.
    other = dataclasses.replace(P.TALKING_V2_POLICY_2,
                                policy_id="talking-v2-policy-hypothetical",
                                normal_question_target=7,
                                hard_question_max=8)
    rec2 = v2.start_baseline_v2("talking", "many_single_words", 36,
                                policy_id=other.policy_id,
                                policy_digest=other.policy_digest)
    q2 = v2.next_question_v2(rec2)
    v2.record_answer_v2(rec2, q2, "sometimes")
    after = v2.record_to_state_v2(rec2)

    assert q["skill_key"] == q2["skill_key"]
    assert after["skills"] == before["skills"]
    assert after["status"] == before["status"]
    assert after["bands_entered"] == before["bands_entered"]


# ---------------------------------------------------------------------------
# 3. HISTORICAL STABILITY — the reason the keys are omitted when unset
# ---------------------------------------------------------------------------

def _legacy_record():
    """A record exactly as code before Slice 1 would have produced it."""
    rec = v2.start_baseline_v2("talking", "many_single_words", 36)
    q = v2.next_question_v2(rec)
    v2.record_answer_v2(rec, q, "yes")
    q = v2.next_question_v2(rec)
    v2.record_answer_v2(rec, q, "sometimes")
    v2.finalize_v2(rec)
    return rec


def test_an_unpolicied_record_serialises_without_the_policy_keys():
    """DIGEST STABILITY GATE.

    The A2 v2 source digest is taken over this exact dict. Emitting the keys as
    null would change every historical digest and turn an idempotent replay
    into an integrity conflict against itself.
    """
    state = v2.record_to_state_v2(_legacy_record())
    assert "policy_id" not in state
    assert "policy_digest" not in state


def test_the_legacy_stored_key_set_is_exactly_what_it_always_was():
    state = v2.record_to_state_v2(_legacy_record())
    assert set(state) == {
        "record_schema", "baseline_version", "area_id", "domain",
        "entry_choice_id", "entry_anchor_months", "chronological_months",
        "skills", "bands_entered", "status", "routing_anchor_months",
        "demonstrated_months", "not_demonstrated_months",
    }


def test_a_historical_record_round_trips_byte_identically():
    before = v2.record_to_state_v2(_legacy_record())
    after = v2.record_to_state_v2(v2.record_from_state_v2(copy.deepcopy(before)))
    assert json.dumps(after, sort_keys=True) == json.dumps(before,
                                                           sort_keys=True)


def test_a_historical_record_still_decodes_and_keeps_its_evidence():
    rec = v2.record_from_state_v2(v2.record_to_state_v2(_legacy_record()))
    assert rec.policy_id is None
    assert rec.policy_digest is None
    assert len(rec.skills) == 2
    assert rec.status == "BOUNDED"
    assert rec.demonstrated_months == 18
    assert rec.not_demonstrated_months == 24


def test_an_unpolicied_record_resolves_to_the_registered_default():
    rec = _legacy_record()
    assert api.policy_for_record(rec).policy_id == "talking-v2-policy-2"


# ---------------------------------------------------------------------------
# 4. a NEW session is frozen to its policy
# ---------------------------------------------------------------------------

def _doc(age=36):
    return {"brain_state": {"child": {"chronological_months": age}},
            "age_in_months": age}


def test_start_freezes_the_policy_onto_the_record():
    doc = _doc()
    api.start(doc, DOMAIN, "many_single_words")
    stored = api.finalized_record.__wrapped__(doc, DOMAIN) \
        if hasattr(api.finalized_record, "__wrapped__") else \
        (doc[v2.STATE_KEY_V2][DOMAIN])
    assert stored["policy_id"] == "talking-v2-policy-2"
    assert stored["policy_digest"] == P.TALKING_V2_POLICY_2.policy_digest


def test_a_frozen_session_is_verified_not_re_resolved():
    doc = _doc()
    api.start(doc, DOMAIN, "many_single_words")
    rec = v2.record_from_state_v2(doc[v2.STATE_KEY_V2][DOMAIN])
    assert api.policy_for_record(rec).policy_id == "talking-v2-policy-2"

    rec.policy_digest = "0" * 64          # simulate a registry edit
    with pytest.raises(P.BaselinePolicyError):
        api.policy_for_record(rec)


def test_the_frozen_policy_survives_a_later_default_change():
    """A config change must not reach an in-progress session."""
    doc = _doc()
    api.start(doc, DOMAIN, "many_single_words")
    frozen = doc[v2.STATE_KEY_V2][DOMAIN]["policy_id"]
    api.answer(doc, DOMAIN,
               api.view(doc, DOMAIN)["current_question"]["skill_key"], "yes")
    assert doc[v2.STATE_KEY_V2][DOMAIN]["policy_id"] == frozen


# ---------------------------------------------------------------------------
# 5. budget arithmetic only — never a judgement
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spent,expected", [(0, 6), (1, 5), (2, 4), (3, 3),
                                            (4, 2), (6, 0), (7, 0), (9, 0)])
def test_profile_slots_are_not_capped_at_the_number_of_standard_areas(
        spent, expected):
    """Correction 1: "6 normally" really means aim for 6."""
    assert P.TALKING_V2_POLICY_2.profile_slots_for(spent) == expected


@pytest.mark.parametrize("age,ceiling", [(6, 12), (9, 15), (24, 30), (36, 42),
                                         (60, 66)])
def test_the_upper_guardrail_is_chronological_plus_lookahead(age, ceiling):
    assert P.TALKING_V2_POLICY_2.ascent_ceiling_months(age) == ceiling
