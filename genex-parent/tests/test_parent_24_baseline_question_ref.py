"""The opaque browser-visible question reference.

Canonical design: `pilot_runtime/docs/parent_baseline_v2_design.md`.

`skill_key` is `domain · subdomain · months · milestone` joined by `U+001F`.
Splitting it recovered the developmental month and the subdomain the question
payload deliberately withholds, so instructing clients to treat it as opaque
did not make it opaque. The browser now gets a digest.

The gates this file installs permanently:

  * the reference is a fixed-width digest, not a parseable identity
  * it is SALTED per session, so it is not a reversible lookup table over the
    small canonical inventory
  * it is DETERMINISTIC within a session, so replay and idempotency behave
    exactly as they did when the key was echoed
  * `/answer` still proves the answer belongs to the expected canonical
    question, and still refuses a stale submission
  * the salt lives in the lifecycle envelope, never the canonical record, so
    no projection id and no finalized evidence moves
  * a missing salt fails closed rather than falling back to the canonical key
"""

from __future__ import annotations

import copy
import itertools
import json

import pytest

from api import baseline_question_ref as QR
from api import functional_baseline_v2_api as api
from api import parent_baseline_projection_v2_client as pv2
from genex_core import functional_baseline as v1
from genex_core import functional_baseline_v2 as v2

DOMAIN = "talking_and_communicating"
AREA = "talking"
CHOICES = tuple(c.choice_id for c in v1.get_area(AREA).choices)


def _started(age=30, choice="two_three_words"):
    doc = {"brain_state": {"child": {"chronological_months": age}},
           "age_in_months": age}
    api.start(doc, DOMAIN, choice)
    return doc


def _engine_question(doc):
    stored = doc[api.CANONICAL_STATE_KEY_V2][DOMAIN]
    return v2.next_question_v2(v2.record_from_state_v2(stored))


# ---------------------------------------------------------------------------
# 1. shape, and non-invertibility
# ---------------------------------------------------------------------------

def test_the_reference_is_a_fixed_width_hex_digest():
    ref = QR.question_ref("salt", "a\x1fb\x1f24\x1fc")
    assert len(ref) == QR.REF_HEX_LENGTH == 32
    assert all(c in "0123456789abcdef" for c in ref)


def test_the_reference_contains_no_field_of_the_canonical_key():
    doc = _started()
    engine = _engine_question(doc)
    ref = api.view(doc, DOMAIN)["current_question"]["question_ref"]
    assert "\x1f" not in ref
    assert DOMAIN not in ref
    assert engine["subdomain"] not in ref
    assert str(engine["months"]) != ref
    for word in engine["milestone"].split():
        assert word not in ref


def test_the_reference_is_injective_over_the_whole_canonical_inventory():
    """Collision would make the retry guard weaker than comparing the keys.

    Checked over every declared-track skill in every band, under one salt.
    """
    salt = QR.new_salt()
    record = v2.start_baseline_v2(AREA, "no_words_yet", 60)
    keys = set()
    for months in sorted({int(r["months"]) for r in
                          v1._rows_for_domain(record.domain,
                                              v1.get_area(AREA).track_subdomains,
                                              ())}):
        for row in v2.band_skill_rows(record, months):
            keys.add(v2.skill_key(record.domain, row["subdomain"], months,
                                  row["milestone"]))
    assert len(keys) > 10
    refs = {QR.question_ref(salt, key) for key in keys}
    assert len(refs) == len(keys), "a reference collision would be a wrong rung"


def test_an_unsalted_digest_would_have_been_a_lookup_table():
    """WHY the salt exists, stated as a test.

    The canonical inventory is small and public. Without a salt the mapping
    reference -> skill is a table anyone can build, so the month would still
    be recoverable — just one step later.
    """
    record = v2.start_baseline_v2(AREA, "two_three_words", 30)
    rows = v2.band_skill_rows(record, 30)
    keys = [v2.skill_key(record.domain, r["subdomain"], 30, r["milestone"])
            for r in rows]

    fixed = "constant"
    table = {QR.question_ref(fixed, k): k for k in keys}
    assert len(table) == len(keys)
    # Under a FIXED salt an attacker inverts it. Under a per-session salt the
    # same table is worthless for any other session.
    other = QR.new_salt()
    assert not (set(table) & {QR.question_ref(other, k) for k in keys})


# ---------------------------------------------------------------------------
# 2. salted per session, stable within one
# ---------------------------------------------------------------------------

def test_two_sessions_asking_the_same_question_get_different_references():
    first, second = _started(), _started()
    a = api.view(first, DOMAIN)["current_question"]
    b = api.view(second, DOMAIN)["current_question"]
    assert a["milestone"] == b["milestone"]
    assert a["question_ref"] != b["question_ref"]


def test_the_reference_is_stable_across_repeated_reads():
    doc = _started()
    refs = {api.view(doc, DOMAIN)["current_question"]["question_ref"]
            for _ in range(5)}
    assert len(refs) == 1


def test_the_salt_is_minted_once_and_reused():
    doc = _started()
    salt = doc[api.ENVELOPE_STATE_KEY_V2][DOMAIN][QR.ENVELOPE_SALT_FIELD]
    for _ in range(3):
        api.view(doc, DOMAIN)
    assert doc[api.ENVELOPE_STATE_KEY_V2][DOMAIN][
        QR.ENVELOPE_SALT_FIELD] == salt


def test_a_session_with_no_salt_mints_one_rather_than_failing():
    doc = _started()
    del doc[api.ENVELOPE_STATE_KEY_V2][DOMAIN][QR.ENVELOPE_SALT_FIELD]
    view = api.view(doc, DOMAIN)
    assert view["current_question"]["question_ref"]
    assert doc[api.ENVELOPE_STATE_KEY_V2][DOMAIN][QR.ENVELOPE_SALT_FIELD]


def test_building_a_client_question_without_a_salt_fails_closed():
    doc = _started()
    engine = _engine_question(doc)
    for bad in (None, "", "   "):
        with pytest.raises(QR.QuestionRefError):
            api._client_question(engine, bad)


def test_the_helpers_refuse_an_empty_operand():
    with pytest.raises(QR.QuestionRefError):
        QR.question_ref("", "key")
    with pytest.raises(QR.QuestionRefError):
        QR.question_ref("salt", "")


# ---------------------------------------------------------------------------
# 3. /answer still validates, and replay still behaves
# ---------------------------------------------------------------------------

def test_the_right_reference_is_accepted():
    doc = _started()
    ref = api.view(doc, DOMAIN)["current_question"]["question_ref"]
    view = api.answer(doc, DOMAIN, ref, "yes")
    assert view["skills_assessed"] == 1


def test_a_wrong_but_well_formed_reference_is_refused():
    doc = _started()
    with pytest.raises(api.BaselineV2SkillMismatch):
        api.answer(doc, DOMAIN, "0" * 32, "yes")
    assert api.view(doc, DOMAIN)["skills_assessed"] == 0


def test_the_canonical_key_itself_is_refused_as_a_reference():
    """Belt and braces: the old token no longer works."""
    doc = _started()
    engine = _engine_question(doc)
    with pytest.raises(api.BaselineV2SkillMismatch):
        api.answer(doc, DOMAIN, engine["skill_key"], "yes")


def test_a_replayed_reference_is_refused_exactly_as_a_replayed_key_was():
    doc = _started()
    ref = api.view(doc, DOMAIN)["current_question"]["question_ref"]
    api.answer(doc, DOMAIN, ref, "yes")
    with pytest.raises(api.BaselineV2SkillMismatch):
        api.answer(doc, DOMAIN, ref, "no")
    # The first answer stands; the replay did not overwrite it.
    stored = doc[api.CANONICAL_STATE_KEY_V2][DOMAIN]
    assert len(stored["skills"]) == 1
    assert stored["skills"][0]["state"] == "demonstrated"


def test_a_mismatch_names_references_and_never_the_canonical_key():
    doc = _started()
    expected = api.view(doc, DOMAIN)["current_question"]["question_ref"]
    with pytest.raises(api.BaselineV2SkillMismatch) as caught:
        api.answer(doc, DOMAIN, "1" * 32, "yes")
    assert caught.value.expected == expected
    assert caught.value.received == "1" * 32
    for value in (caught.value.expected, caught.value.received):
        assert "\x1f" not in value
        assert len(value) == 32


def test_a_whole_interview_runs_on_references_alone():
    for age, choice in itertools.product((9, 24, 36, 60), CHOICES):
        doc = _started(age=age, choice=choice)
        asked = 0
        for _ in range(20):
            question = api.view(doc, DOMAIN).get("current_question")
            if not question:
                break
            api.answer(doc, DOMAIN, question["question_ref"], "yes")
            asked += 1
        assert asked > 0, (age, choice)
        assert len(doc[api.CANONICAL_STATE_KEY_V2][DOMAIN]["skills"]) == asked


# ---------------------------------------------------------------------------
# 4. the record, the digest and historical evidence are untouched
# ---------------------------------------------------------------------------

def test_the_salt_is_never_written_to_the_canonical_record():
    doc = _started()
    api.answer(doc, DOMAIN,
               api.view(doc, DOMAIN)["current_question"]["question_ref"],
               "yes")
    stored = doc[api.CANONICAL_STATE_KEY_V2][DOMAIN]
    blob = json.dumps(stored)
    assert QR.ENVELOPE_SALT_FIELD not in blob
    assert "question_ref" not in blob
    assert doc[api.ENVELOPE_STATE_KEY_V2][DOMAIN][QR.ENVELOPE_SALT_FIELD]


def test_the_stored_key_set_is_unchanged():
    doc = _started()
    while True:
        question = api.view(doc, DOMAIN).get("current_question")
        if not question:
            break
        api.answer(doc, DOMAIN, question["question_ref"], "yes")
    api.finalize_baseline(doc, DOMAIN)
    assert set(doc[api.CANONICAL_STATE_KEY_V2][DOMAIN]) == {
        "record_schema", "baseline_version", "area_id", "domain",
        "entry_choice_id", "entry_anchor_months", "chronological_months",
        "skills", "bands_entered", "status", "routing_anchor_months",
        "demonstrated_months", "not_demonstrated_months",
        "policy_id", "policy_digest",
    }


def test_the_projection_digest_does_not_depend_on_the_salt():
    """Two sessions with identical evidence and different salts agree."""
    digests = []
    for _ in range(2):
        doc = _started(age=24, choice="many_single_words")
        while True:
            question = api.view(doc, DOMAIN).get("current_question")
            if not question:
                break
            api.answer(doc, DOMAIN, question["question_ref"], "yes")
        api.finalize_baseline(doc, DOMAIN)
        digests.append(
            pv2.canonical_source_digest(
                doc[api.CANONICAL_STATE_KEY_V2][DOMAIN]))
    assert digests[0] == digests[1], (
        "a projection id must not depend on a browser display concern")


def test_the_projection_payload_still_carries_the_canonical_identity():
    """The SERVER boundary is unchanged: A2 still resolves the rung."""
    doc = _started(age=24, choice="many_single_words")
    while True:
        question = api.view(doc, DOMAIN).get("current_question")
        if not question:
            break
        api.answer(doc, DOMAIN, question["question_ref"], "yes")
    api.finalize_baseline(doc, DOMAIN)
    payload = api.build_projection_payload(doc, DOMAIN)
    assert payload["skills"]
    for skill in payload["skills"]:
        assert skill["months"] and skill["milestone"] and skill["subdomain"]
