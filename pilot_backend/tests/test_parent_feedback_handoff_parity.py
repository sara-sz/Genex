"""P0-A2 — CROSS-SYSTEM parity between the Parent mapping and the Pilot reader.

The Parent side has one authoritative function that turns a stored feedback
record into a Pilot observation body. Its correctness is not a property of
either system alone: it is a claim that the words Parent emits are the words
the Pilot's enums and its WSGI body allowlist actually accept.

A Parent-only test of that mapping can only prove the mapping agrees with
itself. So this suite imports BOTH packages and compares the real vocabularies
and the real allowlist — no fakes, no restated constants. It is the test that
fails if either side renames a value.

It is also the only place the full round trip is exercised: the body Parent
would emit is fed to the Pilot's own `record_observation` service against a
real released week, so "the client posts this" is proven to mean "the Pilot
accepts this".
"""

from __future__ import annotations

import pathlib
import sys

import pytest

# The Parent package is a sibling namespace, not a dependency. Resolved from
# this file rather than from the cwd so the suite behaves the same under
# `pytest pilot_backend/tests` and under a direct file run.
_PARENT = pathlib.Path(__file__).resolve().parents[2] / "genex-parent"
if _PARENT.is_dir() and str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

parent_progress = pytest.importorskip(
    "api.progress", reason="the Parent package is not present on this branch")
parent_schemas = pytest.importorskip("api.schemas")

from pilot_backend.domain.weekly_plan_document import (  # noqa: E402
    parent_activity_ref,
)
from pilot_backend.domain.observation import (  # noqa: E402
    AttemptOutcome,
    Difficulty,
    Enjoyment,
)
from pilot_backend.transport import wsgi_app as pilot_wsgi  # noqa: E402

# The REAL released-week world and HTTP helper, reused rather than rebuilt:
# a parity suite that built its own week could agree with itself about a
# handle neither system ever minted.
from pilot_backend.tests.test_workflow_e2e import (  # noqa: E402,F401
    TODAY,
    e2e,
    post,
    world,
)


def _literal_values(annotation):
    """The allowed strings of a `Literal[...]` field annotation."""
    import typing
    args = typing.get_args(annotation)
    # Optional[Literal[...]] and bare Literal[...] both flatten to strings.
    out = [a for a in args if isinstance(a, str)]
    if out:
        return frozenset(out)
    for arg in args:
        inner = _literal_values(arg)
        if inner:
            return inner
    return frozenset()


def _field_values(name, model=None):
    """The vocabulary of one field, read from BOTH Parent request models.

    `FeedbackRequest` (standalone) and `PilotFeedbackRequest` (provider-
    connected) are disjoint shapes that nonetheless share the three outcome
    vocabularies. Asserting they agree is the point: a parent's "loved it"
    must mean the same thing whichever planning system scheduled the activity,
    and the mapping below is keyed on one set of words.
    """
    if model is not None:
        return _literal_values(model.model_fields[name].annotation)
    standalone = _literal_values(
        parent_schemas.FeedbackRequest.model_fields[name].annotation)
    pilot_linked = _literal_values(
        parent_schemas.PilotFeedbackRequest.model_fields[name].annotation)
    assert standalone == pilot_linked, (name, standalone, pilot_linked)
    return standalone


# ── the three vocabularies ────────────────────────────────────────────────

def test_enjoyment_map_is_a_bijection_onto_the_pilot_enum():
    """Every Parent word maps to a real Pilot word, and all three are reached.

    Onto in both directions on purpose. A missing Parent key would make some
    parent's answer unmappable at delivery time — after the feedback was
    already accepted — and a Pilot value no Parent word reaches would be a
    state the Pilot can represent but this path can never produce, which is
    how a silent default gets introduced later.
    """
    parent_words = _field_values("enjoyment")
    assert set(parent_progress.ENJOYMENT_TO_PILOT) == set(parent_words)
    assert (set(parent_progress.ENJOYMENT_TO_PILOT.values())
            == {e.value for e in Enjoyment})


def test_completion_map_is_a_bijection_onto_the_pilot_outcome_enum():
    parent_words = _field_values("completion")
    assert set(parent_progress.COMPLETION_TO_PILOT) == set(parent_words)
    assert (set(parent_progress.COMPLETION_TO_PILOT.values())
            == {o.value for o in AttemptOutcome})


def test_difficulty_needs_no_translation_because_the_words_already_agree():
    """Asserted rather than assumed. The mapping passes difficulty through, so
    a rename on either side has to break a test here rather than produce a
    rejected observation in a parent's hands."""
    parent_words = _field_values("difficulty")
    assert parent_words == {d.value for d in Difficulty}


# ── the wire shape ────────────────────────────────────────────────────────

def _observation_spec():
    """The Pilot's own allowlist for the record-observation body."""
    return pilot_wsgi.OBSERVATION_BODY_SPEC


def test_the_parent_body_is_exactly_what_the_pilot_route_allows():
    allowed, required = _observation_spec()
    body = parent_progress.pilot_observation_payload({
        "feedback_id": "fb_1", "supersedes_feedback_id": "fb_0",
        "pilot_cycle_id": "wcyc_1",
        "pilot_activity_ref": parent_activity_ref("actinst_abc"),
        "activity_date": "2026-10-13", "local_submission_date": "2026-10-15",
        "completion": "did_it", "difficulty": "just_right",
        "enjoyment": "loved_it",
    })["body"]
    assert set(body) <= set(allowed), set(body) - set(allowed)
    assert set(required) <= set(body), set(required) - set(body)
    # The revision field is the only optional one, so a full body reaches the
    # whole allowlist. If that stops being true the allowlist grew and this
    # mapping has not been told about it.
    assert set(body) == set(allowed)


def test_a_first_observation_omits_the_supersedes_field_entirely():
    """Absent, not empty-string. An empty `supersedes_source_feedback_id`
    would be a revision of nothing, and the Pilot's own `__post_init__`
    refuses that — correctly."""
    body = parent_progress.pilot_observation_payload({
        "feedback_id": "fb_1", "pilot_cycle_id": "wcyc_1",
        "pilot_activity_ref": parent_activity_ref("actinst_abc"),
        "activity_date": "2026-10-13", "completion": "did_it",
        "difficulty": "just_right", "enjoyment": "loved_it",
    })["body"]
    assert "supersedes_source_feedback_id" not in body


def test_the_parent_ref_pattern_accepts_exactly_what_the_pilot_mints():
    """Parent validates the ref's SHAPE so a half-typed value never reaches the
    ledger — but it must validate the shape the Pilot actually produces, not a
    guess at it. Checked against `parent_activity_ref` itself."""
    # On the PILOT-LINKED model: the standalone shape has no Pilot fields at
    # all, which is itself asserted in the Parent suite.
    assert "pilot_activity_ref" not in parent_schemas.FeedbackRequest.model_fields
    pattern = parent_schemas.PilotFeedbackRequest.model_fields[
        "pilot_activity_ref"].metadata
    regex = next((getattr(m, "pattern", None) for m in pattern
                  if getattr(m, "pattern", None)), None)
    assert regex, "the Parent field no longer constrains the ref shape"
    import re
    minted = parent_activity_ref("actinst_whatever")
    assert re.match(regex, minted), f"{regex!r} rejects {minted!r}"
    # And it refuses the internal identifier the Parent surface must never see.
    assert not re.match(regex, "actinst_whatever")


# ── the round trip, against the real Pilot service ────────────────────────

def test_the_mapped_body_is_accepted_VERBATIM_over_the_real_pilot_route(world):
    """End to end over HTTP, with no Parent-side fake and no reshaping.

    The dict the Parent mapping produces is posted AS THE BODY to the real
    route, against a REAL released week whose handle the Pilot actually
    minted. That is the only way to prove the Parent body resolves rather than
    merely validates — and it would fail if the mapping emitted a field the
    allowlist refuses, in which case no amount of Parent-side green would have
    told us.
    """
    w = world
    payload = parent_progress.pilot_observation_payload({
        "feedback_id": "fb_round_trip",
        "pilot_cycle_id": w.cycle_id,
        "pilot_activity_ref": w.parent_activity_refs[0],
        "activity_date": TODAY,
        "local_submission_date": "2099-01-01",   # must never be used
        "completion": "did_it", "difficulty": "just_right",
        "enjoyment": "it_was_okay",
        "note": "a private note that must not travel",
        "owner_uid": "parent-uid-a",
    })
    assert payload["cycle_id"] == w.cycle_id

    status, body = post(w, f"/pilot/cycles/{w.cycle_id}/observations", "cg",
                        payload["body"])[:2]
    assert status == 200, body

    event_id = body["observation"]["event_id"]
    stored = [e for e in w.weekly.list_observations(
        w.principal("hannah"), w.cycle_id) if e.event_id == event_id][0]
    # The practice date won, not the submission date.
    assert stored.local_date == TODAY != "2099-01-01"
    assert stored.source_feedback_id == "fb_round_trip"
    # The translated word landed on the canonical member.
    assert stored.enjoyment.value == "neutral"
    assert stored.attempt_outcome.value == "did_it"
    # Nothing Parent-only crossed, and no clinical field was inferred.
    assert stored.recorded_by_caregiver_id and "parent-uid-a" not in repr(
        stored)
    assert stored.assistance == stored.child_response == ""
    assert stored.observation_text_ref == ""


def test_a_revision_supersedes_rather_than_duplicating_over_the_real_route(
        world):
    """Parent's append-only revision becomes the Pilot's supersession.

    Proven on the Pilot's own `current_observations()` view, so the claim is
    about what a reader of the evidence sees — one current reading, not two
    independent signals from one practice.
    """
    w = world
    first = parent_progress.pilot_observation_payload({
        "feedback_id": "fb_rev_1", "pilot_cycle_id": w.cycle_id,
        "pilot_activity_ref": w.parent_activity_refs[0],
        "activity_date": TODAY, "completion": "did_it",
        "difficulty": "just_right", "enjoyment": "loved_it"})
    assert post(w, f"/pilot/cycles/{w.cycle_id}/observations", "cg",
                first["body"])[0] == 200

    revised = parent_progress.pilot_observation_payload({
        "feedback_id": "fb_rev_2", "supersedes_feedback_id": "fb_rev_1",
        "pilot_cycle_id": w.cycle_id,
        "pilot_activity_ref": w.parent_activity_refs[0],
        "activity_date": TODAY, "completion": "wasnt_ready_yet",
        "difficulty": "too_hard", "enjoyment": "not_really"})
    assert revised["body"]["supersedes_source_feedback_id"] == "fb_rev_1"
    status, body = post(w, f"/pilot/cycles/{w.cycle_id}/observations", "cg",
                        revised["body"])[:2]
    assert status == 200, body

    from pilot_backend.domain.observation import current_observations
    stored = w.weekly.list_observations(w.principal("hannah"), w.cycle_id)
    ids = {e.source_feedback_id for e in stored}
    assert {"fb_rev_1", "fb_rev_2"} <= ids          # history is kept
    current = {e.source_feedback_id for e in current_observations(stored)}
    assert "fb_rev_2" in current and "fb_rev_1" not in current
