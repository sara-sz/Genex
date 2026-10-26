"""A2 v2.1 — the versioned sibling codec, and the frozen v2 beside it.

The gates this file installs permanently:

  * the v2 body is STILL exactly its five keys, with its historical validation
  * v2.1 has its own key set, its own decoder and its own route constant
  * neither decoder will accept the other's body, in EITHER direction
  * the v2.1 profile carries derived state only — no month, no prose, no ref
  * the clinician profile and Parent's content evaluation are SEPARATE keys
  * the profile record refuses forbidden content at construction, independently
    of the codec
"""

from __future__ import annotations

import pytest

from pilot_backend.domain.parent_clinician_profile_v21 import (
    AREA_CONCERN_OBSERVED,
    AREA_NOT_ASSESSED,
    AREA_NO_CONCERN_OBSERVED,
    AREA_UNCERTAIN,
    CAUSE_BUDGET_CONSUMED,
    CAUSE_NO_AGE_APPROPRIATE_SKILL,
    MULTI_AREA_CONFIRMED,
    MULTI_AREA_INDETERMINATE,
    MULTI_AREA_NOT_OBSERVED,
    ClinicianProfileV21,
    ClinicianProfileV21Error,
    ParentContentEvaluation,
)
from pilot_backend.transport.projection_v2_request import (
    BODY_KEYS,
    ProjectionV2RequestError,
    decode_body,
)
from pilot_backend.transport.projection_v21_request import (
    BODY_KEYS_V21,
    CONTENT_EVALUATION_KEYS,
    PROFILE_KEYS,
    PROJECTION_V21_PATH,
    ProjectionV21RequestError,
    decode_body_v21,
)

DIGEST = "a" * 64
PARENT_DIGEST = "b" * 64


def _summary():
    return {"domain": "talking_and_communicating", "area_id": "talking",
            "entry_choice_id": "many_single_words",
            "routing_anchor_months": 24, "not_demonstrated_months": None,
            "status": "BOUNDED",
            "baseline_version": "parent-2.4-functional-baseline-v2"}


def _v2_body():
    return {
        "source_session_id": "sess_fictional",
        "source_record_digest": DIGEST,
        "summary": _summary(),
        "skills": [{"domain": "talking_and_communicating",
                    "subdomain": "expressive_language", "months": 24,
                    "milestone": "says at least two words together",
                    "state": "demonstrated"}],
        "band_totals": [{"months": 24, "total_skills": 1}],
    }


def _profile(**overrides):
    body = {
        "outcome": "TARGET_SUPPORTED",
        "areas": {"receptive": AREA_CONCERN_OBSERVED,
                  "functional": AREA_NOT_ASSESSED},
        "not_assessed_causes": {"functional": CAUSE_BUDGET_CONSUMED},
        "multi_area": MULTI_AREA_INDETERMINATE,
        "multi_area_indeterminate_reasons": ["budget_consumed"],
    }
    body.update(overrides)
    return body


def _content_evaluation(**overrides):
    body = {"content_set_id": "talking-served-families-v1",
            "content_digest": PARENT_DIGEST,
            "target_serviceability": "supported"}
    body.update(overrides)
    return body


def _v21_body(**overrides):
    body = dict(_v2_body())
    body["clinician_profile"] = _profile()
    body["parent_content_evaluation"] = _content_evaluation()
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# 1. v2 is FROZEN
# ---------------------------------------------------------------------------

def test_the_v2_body_is_still_exactly_five_keys():
    assert BODY_KEYS == ("source_session_id", "source_record_digest",
                         "summary", "skills", "band_totals")
    assert len(BODY_KEYS) == 5


def test_a_v2_body_still_decodes_unchanged():
    decoded = decode_body(_v2_body())
    assert set(decoded) == set(BODY_KEYS)
    assert decoded["source_record_digest"] == DIGEST
    assert decoded["skills"][0]["months"] == 24


def test_v2_refuses_any_extra_key():
    """The historical behaviour, re-pinned: v2 did not widen."""
    for extra in ("clinician_profile", "parent_content_evaluation",
                  "anything_else"):
        body = dict(_v2_body())
        body[extra] = {}
        with pytest.raises(ProjectionV2RequestError):
            decode_body(body)


# ---------------------------------------------------------------------------
# 2. v2.1 is an explicit, independent version
# ---------------------------------------------------------------------------

def test_the_v21_body_is_exactly_seven_keys():
    assert len(BODY_KEYS_V21) == 7
    assert set(BODY_KEYS) < set(BODY_KEYS_V21)
    assert set(BODY_KEYS_V21) - set(BODY_KEYS) == {
        "clinician_profile", "parent_content_evaluation"}


def test_the_two_additions_are_separate_top_level_keys():
    """Not one nested object: they are different kinds of fact."""
    decoded = decode_body_v21(_v21_body())
    assert "clinician_profile" in decoded
    assert "parent_content_evaluation" in decoded
    # Neither contains the other.
    assert "parent_content_evaluation" not in decoded["clinician_profile"]
    assert "clinician_profile" not in decoded["parent_content_evaluation"]
    assert set(decoded["clinician_profile"]) == set(PROFILE_KEYS)
    assert set(decoded["parent_content_evaluation"]) == set(
        CONTENT_EVALUATION_KEYS)


def test_the_v21_route_is_its_own_path():
    assert PROJECTION_V21_PATH.endswith("-v21")
    assert PROJECTION_V21_PATH != "/internal/parent-baseline-projections-v2"


def test_the_clinical_parts_decode_identically_in_both_versions():
    """v2.1 reuses v2's parsers, so a skill row cannot mean two things."""
    v2_decoded = decode_body(_v2_body())
    v21_decoded = decode_body_v21(_v21_body())
    for part in ("source_session_id", "source_record_digest", "summary",
                 "skills", "band_totals"):
        assert v21_decoded[part] == v2_decoded[part], part


# ---------------------------------------------------------------------------
# 3. MUTUAL REFUSAL, in both directions
# ---------------------------------------------------------------------------

def test_v2_cannot_be_interpreted_as_v21():
    with pytest.raises(ProjectionV2RequestError):
        decode_body_v21(_v2_body())


def test_v21_cannot_be_interpreted_as_v2():
    with pytest.raises(ProjectionV2RequestError):
        decode_body(_v21_body())


def test_a_v21_body_missing_only_the_profile_is_refused():
    """No implicit downgrade: the version is the route, not the shape."""
    body = _v21_body()
    del body["clinician_profile"]
    with pytest.raises(ProjectionV2RequestError):
        decode_body_v21(body)
    with pytest.raises(ProjectionV2RequestError):
        decode_body(body)


# ---------------------------------------------------------------------------
# 4. the profile carries derived state ONLY
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_key", [
    "months", "milestone", "skill_key", "rung_ref", "raw_answers",
    "score", "profile_probes", "reason_codes",
])
def test_the_profile_refuses_a_forbidden_key(bad_key):
    with pytest.raises(ProjectionV2RequestError):
        decode_body_v21(_v21_body(
            clinician_profile=_profile(**{bad_key: "x"})))


def test_the_profile_refuses_an_unknown_area_state():
    with pytest.raises(ProjectionV21RequestError):
        decode_body_v21(_v21_body(clinician_profile=_profile(
            areas={"receptive": "looks_fine"})))


def test_the_profile_refuses_an_unknown_outcome():
    with pytest.raises(ProjectionV21RequestError):
        decode_body_v21(_v21_body(clinician_profile=_profile(
            outcome="TARGET_NEARLY_SUPPORTED")))


def test_the_profile_refuses_a_cause_for_an_assessed_area():
    with pytest.raises(ProjectionV2RequestError):
        decode_body_v21(_v21_body(clinician_profile=_profile(
            areas={"receptive": AREA_CONCERN_OBSERVED},
            not_assessed_causes={"receptive": CAUSE_BUDGET_CONSUMED})))


def test_the_profile_refuses_a_cause_for_an_unknown_area():
    with pytest.raises(ProjectionV21RequestError):
        decode_body_v21(_v21_body(clinician_profile=_profile(
            not_assessed_causes={"prosody": CAUSE_BUDGET_CONSUMED})))


def test_an_indeterminate_verdict_must_say_why():
    with pytest.raises(ProjectionV21RequestError):
        decode_body_v21(_v21_body(clinician_profile=_profile(
            multi_area=MULTI_AREA_INDETERMINATE,
            multi_area_indeterminate_reasons=[])))


def test_a_determinate_verdict_may_not_carry_reasons():
    with pytest.raises(ProjectionV21RequestError):
        decode_body_v21(_v21_body(clinician_profile=_profile(
            multi_area=MULTI_AREA_CONFIRMED,
            multi_area_indeterminate_reasons=["uncertainty"])))


def test_confirmed_and_not_observed_decode_without_reasons():
    for verdict in (MULTI_AREA_CONFIRMED, MULTI_AREA_NOT_OBSERVED):
        decoded = decode_body_v21(_v21_body(clinician_profile=_profile(
            areas={"receptive": AREA_CONCERN_OBSERVED,
                   "functional": AREA_NO_CONCERN_OBSERVED},
            not_assessed_causes={},
            multi_area=verdict, multi_area_indeterminate_reasons=[])))
        assert decoded["clinician_profile"]["multi_area"] == verdict


def test_an_area_key_may_not_be_prose():
    long_key = "the child does not consistently follow directions" * 2
    with pytest.raises(ProjectionV2RequestError):
        decode_body_v21(_v21_body(clinician_profile=_profile(
            areas={long_key: AREA_UNCERTAIN})))


def test_a_repeated_indeterminate_reason_is_refused():
    with pytest.raises(ProjectionV21RequestError):
        decode_body_v21(_v21_body(clinician_profile=_profile(
            multi_area_indeterminate_reasons=["uncertainty", "uncertainty"])))


# ---------------------------------------------------------------------------
# 5. the content evaluation
# ---------------------------------------------------------------------------

def test_the_content_evaluation_carries_a_digest_and_a_verdict():
    decoded = decode_body_v21(_v21_body())["parent_content_evaluation"]
    assert decoded["content_digest"] == PARENT_DIGEST
    assert decoded["target_serviceability"] == "supported"
    assert decoded["content_set_id"]


@pytest.mark.parametrize("state", ["supported", "unsupported",
                                   "not_applicable"])
def test_every_serviceability_state_decodes(state):
    decoded = decode_body_v21(_v21_body(
        parent_content_evaluation=_content_evaluation(
            target_serviceability=state)))
    assert decoded["parent_content_evaluation"][
        "target_serviceability"] == state


def test_an_unknown_serviceability_state_is_refused():
    with pytest.raises(ProjectionV21RequestError):
        decode_body_v21(_v21_body(
            parent_content_evaluation=_content_evaluation(
                target_serviceability="probably")))


def test_a_malformed_content_digest_is_refused():
    with pytest.raises(ProjectionV2RequestError):
        decode_body_v21(_v21_body(
            parent_content_evaluation=_content_evaluation(
                content_digest="short")))


# ---------------------------------------------------------------------------
# 6. the RECORD refuses independently of the codec
# ---------------------------------------------------------------------------

def _record(**overrides):
    kwargs = dict(
        child_id="chld_" + "a" * 32,
        source_session_id="sess_fictional",
        source_record_digest=DIGEST,
        domain="talking_and_communicating",
        outcome="TARGET_SUPPORTED",
        areas={"receptive": AREA_CONCERN_OBSERVED,
               "functional": AREA_NOT_ASSESSED},
        not_assessed_causes={"functional": CAUSE_NO_AGE_APPROPRIATE_SKILL},
        multi_area=MULTI_AREA_INDETERMINATE,
        multi_area_indeterminate_reasons=("no_age_appropriate_skill",),
    )
    kwargs.update(overrides)
    return ClinicianProfileV21(**kwargs)


def test_the_record_accepts_a_valid_profile():
    record = _record()
    assert record.profile_schema == "parent-clinician-profile-v21"
    assert set(record.as_dict()["areas"]) == {"receptive", "functional"}


def test_the_record_refuses_a_developmental_month_anywhere():
    """Defence in depth: the codec refuses it by KEY, this by VALUE."""
    with pytest.raises(ClinicianProfileV21Error):
        _record(areas={"receptive": AREA_CONCERN_OBSERVED, "24": "uncertain",
                       "x": AREA_UNCERTAIN},
                not_assessed_causes={})


def test_the_record_refuses_a_forbidden_area_name():
    with pytest.raises(ClinicianProfileV21Error):
        _record(areas={"months_assessed": AREA_UNCERTAIN},
                not_assessed_causes={})


def test_the_record_refuses_an_empty_profile():
    with pytest.raises(ClinicianProfileV21Error):
        _record(areas={}, not_assessed_causes={})


def test_the_record_refuses_a_bad_source_digest():
    with pytest.raises(ClinicianProfileV21Error):
        _record(source_record_digest="short")


def test_the_record_as_dict_is_phi_safe():
    import json

    blob = json.dumps(_record().as_dict())
    for forbidden in ("milestone", "skill_key", "months", "says", "child's",
                      "caregiver", "score", "probe"):
        assert forbidden not in blob, forbidden


def test_the_content_evaluation_record_validates_its_own_vocabulary():
    evaluation = ParentContentEvaluation(
        content_set_id="talking-served-families-v1",
        content_digest=PARENT_DIGEST, target_serviceability="unsupported")
    assert evaluation.as_dict()["target_serviceability"] == "unsupported"
    with pytest.raises(ClinicianProfileV21Error):
        ParentContentEvaluation(content_set_id="x",
                                content_digest=PARENT_DIGEST,
                                target_serviceability="maybe")
    with pytest.raises(ClinicianProfileV21Error):
        ParentContentEvaluation(content_set_id="x", content_digest="short",
                                target_serviceability="supported")


def test_the_profile_record_is_not_a_field_on_the_projection():
    """THE SEPARATION, restated where the record is defined."""
    from pilot_backend.domain.parent_baseline_projection_v2 import (
        ParentBaselineProjectionV2,
    )

    fields = set(ParentBaselineProjectionV2.__dataclass_fields__)
    assert "clinician_profile" not in fields
    assert "parent_content_evaluation" not in fields
    assert "multi_area" not in fields
    # They share a SOURCE IDENTITY instead, which is how a clinician surface
    # joins them without the generator being able to see the profile.
    for shared in ("child_id", "source_session_id", "source_record_digest"):
        assert shared in fields
        assert shared in ClinicianProfileV21.__dataclass_fields__
