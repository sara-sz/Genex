"""Pre-deploy cleanup — developmental months must not reach the browser.

Canonical design: `pilot_runtime/docs/parent_baseline_v2_design.md`.

Chronological age is legitimate routing context. A canonical BAND MONTH is a
developmental-age interpretation, and a caregiver reading `18` beside a progress
bar reads it as a verdict about their child however it is labelled. Two fields
used to carry one: `bands[].months` and `current_question.months`. Both are
gone; the canonical months are unchanged server-side.

The gates this file installs permanently:

  * no Parent Baseline v2 response contains a developmental band month
  * the current-question payload exposes no month
  * ordering and progress still work, from an ordinal position
  * internal traversal and target evidence are unchanged
  * projection identity and historical records are unaffected

## THE ONE KNOWN RESIDUE, ASSERTED SO IT CANNOT BE FORGOTTEN

`skill_key` is `skill_key(domain, subdomain, months, milestone)` joined by a
unit separator, so a client that splits it recovers the month AND the subdomain
that `_client_question` deliberately withholds. It cannot be removed here: the
client must echo it back on `/answer` and that is the entire retry guard, so
changing it would alter a frozen external contract rather than a browser
display. `test_the_skill_key_residue_is_known_and_unchanged` pins it as a
DELIBERATE, REPORTED exception — not an oversight, and not permission to add a
second one.
"""

from __future__ import annotations

import copy
import itertools
import json

import pytest

from api import baseline_contract as C
from api import functional_baseline_v2_api as api
from api import parent_baseline_projection_v2_client as pv2
from genex_core import functional_baseline as v1
from genex_core import functional_baseline_v2 as v2

DOMAIN = "talking_and_communicating"
AREA = "talking"

#: Every canonical band month on the declared track, plus the profile areas.
#: A response value equal to any of these is the leak this file exists to stop.
CANONICAL_MONTHS = frozenset({2, 4, 6, 9, 12, 15, 18, 24, 30, 36, 48, 60})

CHOICES = tuple(c.choice_id for c in v1.get_area(AREA).choices)


def _live(age=30, choice="two_three_words", answer_for=None, finalize=True):
    """A session driven through the real API surface."""
    answer_for = answer_for or (lambda q: "yes")
    doc = {"brain_state": {"child": {"chronological_months": age}},
           "age_in_months": age}
    api.start(doc, DOMAIN, choice)
    views = [api.view(doc, DOMAIN)]
    for _ in range(20):
        question = views[-1].get("current_question")
        if not question:
            break
        api.answer(doc, DOMAIN, question["skill_key"], answer_for(question))
        views.append(api.view(doc, DOMAIN))
    if finalize and not views[-1]["finalized"]:
        api.finalize_baseline(doc, DOMAIN)
        views.append(api.view(doc, DOMAIN))
    return doc, views


def _months_of(question):
    return int(question["skill_key"].split("\x1f")[2])


def _integers(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from _integers(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _integers(value)
    elif isinstance(node, int) and not isinstance(node, bool):
        yield node


# ---------------------------------------------------------------------------
# 1. no Parent v2 response contains a developmental band month
# ---------------------------------------------------------------------------

def test_no_view_carries_a_band_month_field():
    for age, choice in itertools.product((9, 18, 24, 30, 36, 48, 60), CHOICES):
        _doc, views = _live(age=age, choice=choice)
        for view in views:
            for band in view["bands"]:
                assert "months" not in band, (age, choice, band)
                assert set(band) == {"position", "assessed", "total",
                                     "complete", "mastered"}


def test_no_view_value_is_a_canonical_month():
    """Stronger than a key check: no INTEGER in the payload may be a band.

    `chronological_months` is deliberately absent from the view, so there is no
    legitimate month-valued integer left to collide with. Counts are small and
    `position` is 1-based, both of which can coincide with 2, 4, 6 or 9 — so
    those four are checked by NAME above and excluded here.
    """
    ambiguous = {2, 4, 6, 9}
    for age, choice in itertools.product((9, 18, 24, 30, 36, 48, 60), CHOICES):
        _doc, views = _live(age=age, choice=choice)
        for view in views:
            for value in _integers(view):
                assert value not in (CANONICAL_MONTHS - ambiguous), (
                    age, choice, value, view)


def test_no_view_key_is_named_like_a_developmental_month():
    _doc, views = _live()

    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                yield from keys(value)

    for view in views:
        for key in keys(view):
            lowered = str(key).lower()
            for fragment in ("month", "band", "dev_age", "anchor"):
                assert fragment not in lowered or key == "bands", (key,
                                                                   fragment)


def test_the_whole_month_sweep_across_every_answer_shape():
    answers = ("yes", "sometimes", "no", "not_sure")
    for choice in CHOICES:
        for age in (12, 24, 36, 48):
            for pattern in itertools.product(answers, repeat=2):
                state = {"i": 0}

                def answer_for(_q, pattern=pattern, state=state):
                    value = pattern[state["i"] % len(pattern)]
                    state["i"] += 1
                    return value

                _doc, views = _live(age=age, choice=choice,
                                    answer_for=answer_for)
                for view in views:
                    for band in view["bands"]:
                        assert "months" not in band
                    question = view.get("current_question")
                    if question:
                        assert "months" not in question


# ---------------------------------------------------------------------------
# 2. the current-question payload exposes no month
# ---------------------------------------------------------------------------

def test_the_current_question_payload_is_exactly_three_fields():
    _doc, views = _live(finalize=False)
    questions = [v["current_question"] for v in views
                 if v.get("current_question")]
    assert questions
    for question in questions:
        assert set(question) == {"skill_key", "milestone",
                                 "parent_explanation"}
        assert "months" not in question
        assert "subdomain" not in question
        assert "activity_family" not in question


def test_the_client_question_builder_withholds_months_structurally():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(api._client_question).lstrip())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.Module)) and \
                ast.get_docstring(node):
            node.body = node.body[1:]
    code = ast.unparse(tree)
    assert '"months"' not in code and "'months'" not in code


def test_the_engine_question_still_carries_months_for_the_server():
    """The month is withheld from the CLIENT, not removed from the engine."""
    record = v2.start_baseline_v2(AREA, "two_three_words", 30)
    question = v2.next_question_v2(record)
    assert question["months"] == 24
    assert api._client_question(question)["skill_key"] == \
        question["skill_key"]
    assert "months" not in api._client_question(question)


def test_the_skill_key_residue_is_known_and_unchanged():
    """DELIBERATE, REPORTED EXCEPTION — see this module's docstring.

    `skill_key` must keep its exact shape: the client echoes it on `/answer`
    and the engine validates it, so this is a frozen external contract rather
    than a display. Pinned here so the residue is a recorded decision and a
    change to the key shape is a visible, reviewed event.
    """
    _doc, views = _live(finalize=False)
    question = views[0]["current_question"]
    fields = question["skill_key"].split("\x1f")
    assert len(fields) == 4
    assert fields[0] == DOMAIN
    assert fields[2].isdigit()
    assert int(fields[2]) in CANONICAL_MONTHS
    # It is NOT a second display field: nothing else in the payload repeats it.
    assert "months" not in question


# ---------------------------------------------------------------------------
# 3. ordering and progress still work
# ---------------------------------------------------------------------------

def test_position_is_a_dense_one_based_sequence():
    for age, choice in itertools.product((9, 24, 36, 60), CHOICES):
        _doc, views = _live(age=age, choice=choice)
        for view in views:
            positions = [b["position"] for b in view["bands"]]
            assert positions == list(range(1, len(positions) + 1)), (age,
                                                                     choice)


def test_position_matches_the_order_the_traversal_entered_bands():
    doc, views = _live(age=30, choice="two_three_words",
                       answer_for=lambda q: "yes" if _months_of(q) <= 24
                       else "no")
    entered = doc[api.CANONICAL_STATE_KEY_V2][DOMAIN]["bands_entered"]
    bands = views[-1]["bands"]
    assert len(bands) == len(entered)
    for index, band in enumerate(bands):
        assert band["position"] == index + 1
    # The position is an INDEX INTO the internal order, which is what makes it
    # a usable ordering key without being a developmental figure.
    assert entered == [24, 30]


def test_progress_counts_are_still_reported_per_band():
    doc, views = _live(age=30, choice="two_three_words",
                       answer_for=lambda q: "yes" if _months_of(q) <= 24
                       else "no")
    final = views[-1]
    assert len(final["bands"]) == 2
    assert final["bands"][0] == {"position": 1, "assessed": 1, "total": 1,
                                 "complete": True, "mastered": True}
    assert final["bands"][1] == {"position": 2, "assessed": 4, "total": 4,
                                 "complete": True, "mastered": False}
    assert final["skills_assessed"] == 5


def test_progress_is_monotonic_as_the_interview_advances():
    _doc, views = _live(age=30, choice="two_three_words")
    totals = [sum(b["assessed"] for b in v["bands"]) for v in views]
    assert totals == sorted(totals)
    assert totals[-1] > totals[0]
    counts = [len(v["bands"]) for v in views]
    assert counts == sorted(counts), "band count never decreases"


# ---------------------------------------------------------------------------
# 4. internal traversal and target evidence are unchanged
# ---------------------------------------------------------------------------

def test_the_stored_record_still_carries_the_canonical_months():
    doc, _views = _live(age=30, choice="two_three_words",
                        answer_for=lambda q: "yes" if _months_of(q) <= 24
                        else "no")
    stored = doc[api.CANONICAL_STATE_KEY_V2][DOMAIN]
    assert stored["bands_entered"] == [24, 30]
    assert {s["months"] for s in stored["skills"]} == {24, 30}
    assert stored["routing_anchor_months"] is not None


def test_the_outcome_and_target_evidence_are_unchanged():
    from api import baseline_outcome as O

    doc, _views = _live(age=30, choice="two_three_words",
                        answer_for=lambda q: "yes" if _months_of(q) <= 24
                        else "no")
    record = v2.record_from_state_v2(doc[api.CANONICAL_STATE_KEY_V2][DOMAIN])
    result = O.derive_outcome(record)
    assert result.target_band == 30
    assert result.floor_band == 24
    assert result.floor_established is True
    assert result.outcome in O.TARGET_OUTCOMES


def test_the_traversal_is_unaffected_by_the_display_change():
    """The questions asked, and their order, are identical."""
    doc, views = _live(age=30, choice="two_three_words")
    asked = [v["current_question"]["skill_key"] for v in views
             if v.get("current_question")]
    assert asked == sorted(set(asked), key=asked.index), "no repeats"
    months = [int(k.split("\x1f")[2]) for k in asked]
    assert months == sorted(months) or months[0] == 24
    assert len(asked) == len(
        doc[api.CANONICAL_STATE_KEY_V2][DOMAIN]["skills"])


# ---------------------------------------------------------------------------
# 5. projection identity and historical records are unaffected
# ---------------------------------------------------------------------------

def test_the_projection_payload_still_carries_the_canonical_months():
    """A2 v2 resolves the canonical rung from them. This is a SERVER boundary.

    Driven on a CONTENT-SUPPORTED path, because slice 8 withholds the payload
    entirely for an unservable target — the 30-month target used elsewhere in
    this file is unserved today, so it has no payload to inspect.
    """
    doc, _views = _live(age=24, choice="many_single_words",
                        answer_for=lambda q: "yes" if _months_of(q) <= 18
                        else "no")
    payload = api.projection_payload(doc, DOMAIN)
    assert {s["months"] for s in payload["skills"]} == {18, 24}
    assert [b["months"] for b in payload["band_totals"]] == [18, 24]


def test_the_source_record_digest_is_unaffected_by_the_display_change():
    """The digest is over the STORED dict; nothing stored changed."""
    doc, _views = _live(age=30, choice="two_three_words")
    stored = doc[api.CANONICAL_STATE_KEY_V2][DOMAIN]
    first = pv2.canonical_source_digest(stored)
    round_tripped = v2.record_to_state_v2(
        v2.record_from_state_v2(copy.deepcopy(stored)))
    assert json.dumps(round_tripped, sort_keys=True) == \
        json.dumps(stored, sort_keys=True)
    assert pv2.canonical_source_digest(round_tripped) == first


def test_the_stored_key_set_is_unchanged_by_the_cleanup():
    doc, _views = _live(age=30, choice="two_three_words")
    stored = doc[api.CANONICAL_STATE_KEY_V2][DOMAIN]
    assert set(stored) == {
        "record_schema", "baseline_version", "area_id", "domain",
        "entry_choice_id", "entry_anchor_months", "chronological_months",
        "skills", "bands_entered", "status", "routing_anchor_months",
        "demonstrated_months", "not_demonstrated_months",
        "policy_id", "policy_digest",
    }


def test_the_parent_outcome_block_is_still_parent_safe():
    doc, views = _live()
    C.assert_parent_safe(views[-1]["outcome"])
    C.assert_parent_safe(views[-1]["bands"])
    C.assert_parent_safe(views[-1]["current_question"])


def test_the_whole_view_passes_the_parent_month_rule_except_the_skill_key():
    """The skill key is the one reported exception; everything else is clean."""
    _doc, views = _live(finalize=False)
    view = dict(views[0])
    question = dict(view["current_question"])
    question.pop("skill_key")
    view["current_question"] = question
    blob = json.dumps(view)
    assert "months" not in blob
    for month in sorted(CANONICAL_MONTHS - {2, 4, 6, 9}):
        assert f": {month}" not in blob, month
