"""0.6A-1F — the WHOLE A2 v2 product path, against a REAL Firestore emulator.

This is the only place the chain runs end to end with nothing faked but the
network hop and the Firebase token decoder:

    authenticated fictional Parent HTTP  (FastAPI TestClient, real routes)
      -> Parent baseline v2 engine, band-complete questioning
      -> finalized BaselineRecordV2 in durable Parent session storage
      -> the real Parent A2 v2 client, building the real request body
      -> the real private v2 WSGI route            (in-process transport)
      -> the real canonicalisation boundary, against the FROZEN rung table
      -> the real v2 Firestore repository          (emulator)
      -> readback

## What is real, and what is not

REAL: both engines, both API layers, the request codec, the canonicalisation
boundary, the frozen rung-table artifact, the Firestore adapter, the codec and
the repository.

NOT REAL, and deliberately so: the Firebase token decoder (mocked, as every
Parent API test does) and the OIDC token fetch plus the HTTPS hop. A `poster`
seam hands the body straight to the private WSGI app.

That seam is the honest limit of this test and is stated rather than papered
over: it proves the PAYLOAD and the PERSISTENCE, not the transport security.
The transport security is proven elsewhere — `projection_v2_config`'s refusals in
the Parent suite, the verifier's refusals in `pilot_backend`, and the deployed
single-`roles/run.invoker` boundary by the staging auth probes. No cloud IAM is
created here, and nothing is deployed.

## Why the Parent half must be driven over HTTP

v1's defect was invisible at the engine level: the engine was correct about what
it was asked to do, and the product still asked one skill per band. So the
evidence that reaches Firestore has to originate from a real request sequence, or
this test would prove the projection works on a record no product path produces.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PARENT_ROOT = REPO_ROOT / "genex-parent"

DOMAIN = "talking_and_communicating"
BOOK = "name things in a book"
TWO_WORD = "say two or more words together"
VOCAB = "says about 50 words"
PRONOUNS = "I me or we"

#: The three declared-track rungs the taxonomy has not reconciled to an activity
#: family. They must survive transport as canonical evidence anyway.
WH = "ask who or what or where or why"
OWN_NAME = "says first name when asked"


@pytest.fixture(scope="module")
def parent_app():
    """The real Parent FastAPI app, with Firebase mocked and a local store.

    Module-scoped because importing `api.main` has import-time side effects and
    mutates module-level taxonomy state; building it once keeps this file to a
    single Parent app per process, which is the constraint the Parent suite
    already works under.
    """
    os.environ["FIREBASE_PROJECT_ID"] = "genex-test"
    os.environ["LOCAL_SESSION_FALLBACK"] = "1"
    os.environ["REQUIRE_BETA_CODE"] = "false"
    os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:3000")
    os.environ["ACTIVITY_MODEL"] = ""
    for name in ("GCS_BUCKET", "OPENAI_API_KEY", "CONCERN_ROUTER_MODEL"):
        os.environ.pop(name, None)
    # `session_store` hardcodes its local fallback directory, so the dir is
    # cleared rather than redirected.
    shutil.rmtree("/tmp/genex_api_sessions", ignore_errors=True)

    if str(PARENT_ROOT) not in sys.path:
        sys.path.insert(0, str(PARENT_ROOT))
    sys.modules.setdefault("openai", None)

    import firebase_admin

    firebase_admin._apps.setdefault("[DEFAULT]", object())
    from firebase_admin import auth as firebase_auth

    firebase_auth.verify_id_token = lambda t, *a, **k: {
        "uid": "uid-a2v2-emulator", "email": "fictional@example.invalid"}

    from fastapi.testclient import TestClient

    from api import functional_baseline_v2_api as baseline_v2_api
    from api import parent_baseline_projection_v2_client as v2_client
    from api import session_store
    from api.main import app

    return {
        "client": TestClient(app),
        "store": session_store,
        "api": baseline_v2_api,
        "v2_client": v2_client,
        "auth": {"Authorization": "Bearer fictional-token"},
        "uid": "uid-a2v2-emulator",
    }


def _make_session(parent, session_id, *, months=30):
    brain_state = {
        "child": {"chronological_months": months, "diagnosis": "",
                  "concern": ""},
        "qna": {}, "dev_age": {},
    }
    doc = parent["store"].new_session_doc(
        session_id=session_id, owner_uid=parent["uid"], age_in_months=months,
        daily_time_minutes=20, diagnosis_or_condition="",
        brain_state=brain_state, interview={}, timezone="UTC",
        beta_authorized=True)
    doc["brain_state"] = brain_state
    parent["store"].save(parent["uid"], session_id, doc)


def _drive_baseline(parent, session_id, overrides, *,
                    choice="two_three_words"):
    """Drive the whole v2 baseline over the REAL Parent HTTP routes.

    Returns `(trace, finalized_record, projection_payload)`.
    """
    client, auth = parent["client"], parent["auth"]
    base = f"/api/v2/session/{session_id}/baseline/{DOMAIN}"

    started = client.post(base + "/start",
                          json={"entry_choice_id": choice}, headers=auth)
    assert started.status_code == 200, started.text

    trace, view = [], started.json()
    while view.get("current_question"):
        question = view["current_question"]
        answer = "yes"
        for fragment, value in overrides.items():
            if fragment in question["milestone"]:
                answer = value
        response = client.post(
            base + "/answer",
            json={"skill_key": question["skill_key"], "answer": answer},
            headers=auth)
        assert response.status_code == 200, response.text
        trace.append((question["months"], question["milestone"], answer))
        view = response.json()

    finalized = client.post(base + "/finalize", headers=auth)
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["finalized"] is True

    parent["store"]._cache.clear()
    doc = parent["store"].load(parent["uid"], session_id, force_remote=True)
    return (trace,
            parent["api"].finalized_record(doc, DOMAIN),
            parent["api"].projection_payload(doc, DOMAIN))


@pytest.fixture()
def pilot(repos):
    """The private v2 route over the emulator, with the FROZEN rung table."""
    from pilot_backend.integration.baseline_projection_v2_service import (
        BaselineProjectionV2Service,
    )
    from pilot_backend.transport.projection_v2_wsgi import (
        PROJECTION_V2_ROUTE,
        InternalProjectionRouter,
        ProjectionV2App,
    )
    from pilot_backend.transport.projection_wsgi import ProjectionApp
    from pilot_runtime.integration.static_rung_source import (
        build_static_rung_source,
    )

    rung_source = build_static_rung_source()
    app = InternalProjectionRouter(
        v1_app=ProjectionApp(verifier=None, service_factory=lambda: None),
        v2_app=ProjectionV2App(
            verifier=None,
            service_factory=lambda: BaselineProjectionV2Service(
                repos=repos, rung_source=rung_source)))
    return {"app": app, "repos": repos, "route": PROJECTION_V2_ROUTE,
            "rungs": rung_source}


def _poster(pilot):
    """The seam that replaces the OIDC-authenticated HTTPS hop.

    Hands the body the REAL client built to the REAL private WSGI app. The token
    is accepted and ignored: this service runs in the declared `iam_only`
    posture, where Cloud Run IAM is the authoritative gate and the app inspects
    no token.
    """
    import io

    def post(url, body, token, timeout):
        assert url.endswith("/internal/parent-baseline-projections-v2")
        assert token, "the client must still mint an identity token"
        raw = json.dumps(body).encode("utf-8")
        captured = {}
        environ = {"PATH_INFO": pilot["route"], "REQUEST_METHOD": "POST",
                   "CONTENT_LENGTH": str(len(raw)),
                   "wsgi.input": io.BytesIO(raw)}
        chunks = pilot["app"](
            environ, lambda status, headers: captured.setdefault(
                "status", status))
        return int(captured["status"].split()[0]), json.loads(
            b"".join(chunks))

    return post


def _link_child(repos, session_id):
    from pilot_backend.domain.entities import Child
    from pilot_backend.domain.source_link import SourceSystem, SourceSystemLink

    child = repos.children.create(Child.create(actor_id="a2v2-seed"))
    repos.source_links.create(SourceSystemLink.create(
        child.child_id, SourceSystem.PARENT, session_id,
        actor_id="a2v2-seed"))
    return child


def _project(parent, pilot, session_id, record, payload):
    env = {
        parent["v2_client"].PAIRING_ENV_VAR: "parent-staging->pilot-staging",
        parent["v2_client"].AUDIENCE_ENV_VAR: "https://pilot-v2.example",
        parent["v2_client"].URL_ENV_VAR: (
            "https://pilot-v2.example"
            + parent["v2_client"].PROJECTION_V2_PATH),
    }
    return parent["v2_client"].project_baseline_v2(
        source_session_id=session_id, record=record, payload=payload,
        env=env, fetcher=lambda audience: "fictional-oidc-token",
        poster=_poster(pilot))


# ===========================================================================
# ITEM 13 — the mixed 30-month band, all the way to Firestore and back
# ===========================================================================


def test_the_mixed_30m_band_reaches_firestore_with_four_distinct_refs(
        parent_app, pilot, unique_suffix):
    session_id = f"sess-a2v2-mixed-{unique_suffix}"
    _make_session(parent_app, session_id)
    child = _link_child(pilot["repos"], session_id)

    trace, record, payload = _drive_baseline(
        parent_app, session_id, {BOOK: "no", PRONOUNS: "not_sure"})

    # The Parent half asked every 30-month sibling, over HTTP.
    at_30 = [m for months, m, _ in trace if months == 30]
    assert len(at_30) == 4 and len(set(at_30)) == 4
    assert {b["months"]: b["total_skills"]
            for b in payload["band_totals"]}[30] == 4

    result = _project(parent_app, pilot, session_id, record, payload)
    assert result["created"] is True
    assert result["projection_id"].startswith("pbp2_")
    assert result["child_id"] == child.child_id

    # --- readback, from the emulator -----------------------------------
    stored = pilot["repos"].parent_baseline_projections_v2.find(
        result["projection_id"])
    assert stored is not None
    assert stored.projection_schema == "parent-baseline-projection-v2"
    assert stored.child_id == child.child_id

    band_30 = stored.assessed_in_band(30)
    assert len(band_30) == 4
    assert len({e.rung_ref for e in band_30}) == 4
    assert all(e.rung_ref.startswith("rung1:") for e in band_30)

    # The canonical denominator and Parent's projected total agree, and both
    # agree with the frozen roster.
    roster = pilot["rungs"].declared_band_roster(DOMAIN, 30)
    assert len(roster) == 4
    assert next(b.total_skills for b in stored.band_totals
                if b.months == 30) == 4
    assert {e.rung_ref for e in band_30} == set(roster)

    assert stored.assessment_complete(30) is True
    assert stored.band_mastered(30) is False

    # Each of the four states survived independently, against the ref the frozen
    # table resolves for that milestone.
    def ref(fragment, months=30):
        milestone = next(m for mo, m, _ in trace
                         if mo == months and fragment in m)
        return pilot["rungs"].canonical_identity(DOMAIN, months, milestone)[0]

    states = {e.rung_ref: e.state for e in band_30}
    assert states[ref(BOOK)] == "not_demonstrated"
    assert states[ref(TWO_WORD)] == "demonstrated"
    assert states[ref(VOCAB)] == "demonstrated"
    assert states[ref(PRONOUNS)] == "unknown"

    # The unresolved target is the book-naming deficit, and the unknown is NOT a
    # target: an unanswerable question is not evidence a skill is absent.
    unresolved = stored.unresolved_skills()
    assert [e.rung_ref for e in unresolved] == [ref(BOOK)]
    assert unresolved[0].state == "not_demonstrated"

    # The pronouns skill is present DESPITE having no activity family.
    artifact = json.loads(
        (REPO_ROOT / "pilot_runtime/data/rung_table_talking_v1.json")
        .read_text(encoding="utf-8"))
    assert artifact["rungs"][ref(PRONOUNS)]["mappable"] is False
    assert ref(PRONOUNS) in states


def test_the_persisted_document_in_firestore_holds_no_clinical_prose(
        parent_app, pilot, unique_suffix, store):
    """Read back through the RAW document store, not the decoded object.

    The decoded object cannot carry a milestone — the dataclass has no field for
    one. This checks the actual stored bytes, which is what an export or a
    console view would show.
    """
    session_id = f"sess-a2v2-noprose-{unique_suffix}"
    _make_session(parent_app, session_id)
    _link_child(pilot["repos"], session_id)
    _trace, record, payload = _drive_baseline(
        parent_app, session_id, {BOOK: "no", PRONOUNS: "not_sure"})
    result = _project(parent_app, pilot, session_id, record, payload)

    raw = store.get("pilot_parent_baseline_projections_v2",
                    result["projection_id"])
    blob = json.dumps(raw, default=str)
    assert sorted(raw) == [
        "area_id", "band_totals", "baseline_version", "child_id", "domain",
        "entry_choice_id", "not_demonstrated_months", "projected_at",
        "projection_id", "projection_schema", "routing_anchor_months",
        "schema_version", "skill_evidence", "source_record_digest",
        "source_session_id", "source_system", "status"]
    for forbidden in (BOOK, VOCAB, PRONOUNS, "milestone", "subdomain",
                      "skill_key", "question_id", "raw_answer", "asked",
                      "chronological_months", parent_app["uid"]):
        assert forbidden not in blob, forbidden
    for row in raw["skill_evidence"]:
        assert sorted(row) == ["months", "rung_ref", "state"]


# ===========================================================================
# ITEM 9 — unmapped activity families survive the REAL transport
# ===========================================================================


def test_unmapped_skills_reach_firestore_as_canonical_evidence(
        parent_app, pilot, unique_suffix):
    """`pronouns`, `wh_question_asking` and `expressive_name_response`.

    All three are canonicalisable and NONE is activity-mappable. A2 is not
    allowed to discard them because the taxonomy has not caught up: baseline
    evidence is about the child, mappability is about our content.
    """
    session_id = f"sess-a2v2-unmapped-{unique_suffix}"
    _make_session(parent_app, session_id, months=40)
    _link_child(pilot["repos"], session_id)

    # `short_sentences` enters higher up the ladder, so the 36-month band — which
    # holds the other two unmapped rungs — is assessed.
    trace, record, payload = _drive_baseline(
        parent_app, session_id, {WH: "no", OWN_NAME: "not_sure"},
        choice="short_sentences")
    result = _project(parent_app, pilot, session_id, record, payload)
    stored = pilot["repos"].parent_baseline_projections_v2.find(
        result["projection_id"])

    artifact = json.loads(
        (REPO_ROOT / "pilot_runtime/data/rung_table_talking_v1.json")
        .read_text(encoding="utf-8"))
    present = {e.rung_ref: e.state for e in stored.skill_evidence}

    found = 0
    for fragment in (WH, OWN_NAME):
        matches = [(mo, m) for mo, m, _ in trace if fragment in m]
        if not matches:
            continue
        months, milestone = matches[0]
        ref, _subdomain = pilot["rungs"].canonical_identity(
            DOMAIN, months, milestone)
        # The test is not vacuous: the rung really has no activity family.
        assert artifact["rungs"][ref]["mappable"] is False, fragment
        assert ref in present, fragment
        found += 1
    assert found == 2, "neither unmapped 36-month skill was asked"

    # Every assessed skill arrived; nothing was filtered on mappability.
    assert len(stored.skill_evidence) == len(payload["skills"])
    unmappable = [r for r in present
                  if artifact["rungs"][r]["mappable"] is False]
    assert unmappable, "the test proved nothing about unmappable rungs"


# ===========================================================================
# ITEM 10 / 11 — replay, immutability, and the changed source record
# ===========================================================================


def test_a_replay_across_a_fresh_client_returns_the_same_document(
        parent_app, pilot, unique_suffix):
    """Idempotent through the REAL store, with nothing cached in between."""
    session_id = f"sess-a2v2-replay-{unique_suffix}"
    _make_session(parent_app, session_id)
    _link_child(pilot["repos"], session_id)
    _trace, record, payload = _drive_baseline(
        parent_app, session_id, {BOOK: "no", PRONOUNS: "not_sure"})

    first = _project(parent_app, pilot, session_id, record, payload)
    second = _project(parent_app, pilot, session_id, record, payload)

    assert first["created"] is True
    assert second["created"] is False
    assert first["projection_id"] == second["projection_id"]
    assert len(pilot["repos"].parent_baseline_projections_v2.list_for_source(
        session_id, DOMAIN)) == 1


def test_a_changed_source_record_is_refused_and_never_overwrites(
        parent_app, pilot, unique_suffix):
    """Different evidence -> different digest -> a conflict, not an overwrite.

    The stored evidence is what a later clinical decision appeals to, so
    overwriting it would destroy the only record of what was actually asked.
    """
    session_id = f"sess-a2v2-conflict-{unique_suffix}"
    _make_session(parent_app, session_id)
    _link_child(pilot["repos"], session_id)
    _trace, record, payload = _drive_baseline(
        parent_app, session_id, {BOOK: "no", PRONOUNS: "not_sure"})
    original = _project(parent_app, pilot, session_id, record, payload)

    # A DIFFERENT finalized record for the same session: the pronouns skill
    # answered rather than unknown. Parent's own route would refuse to produce
    # this (the baseline is immutable), so it is constructed here deliberately to
    # attack the Pilot's integrity check directly.
    tampered = json.loads(json.dumps(record))
    for row in tampered["skills"]:
        if PRONOUNS in row["milestone"]:
            row["state"] = "demonstrated"
    tampered_payload = json.loads(json.dumps(payload))
    for row in tampered_payload["skills"]:
        if PRONOUNS in row["milestone"]:
            row["state"] = "demonstrated"

    with pytest.raises(parent_app["v2_client"].ProjectionRejected):
        _project(parent_app, pilot, session_id, tampered, tampered_payload)

    # The original is intact and unchanged.
    stored = pilot["repos"].parent_baseline_projections_v2.find(
        original["projection_id"])
    assert stored is not None
    states = [e.state for e in stored.assessed_in_band(30)]
    assert states.count("unknown") == 1
    assert len(pilot["repos"].parent_baseline_projections_v2.list_for_source(
        session_id, DOMAIN)) == 1


def test_an_unlinked_session_is_refused_against_the_real_store(
        parent_app, pilot, unique_suffix):
    session_id = f"sess-a2v2-unlinked-{unique_suffix}"
    _make_session(parent_app, session_id)
    # No source link is created.
    _trace, record, payload = _drive_baseline(
        parent_app, session_id, {BOOK: "no"})

    with pytest.raises(parent_app["v2_client"].ProjectionRejected):
        _project(parent_app, pilot, session_id, record, payload)
    assert pilot["repos"].parent_baseline_projections_v2.list_for_source(
        session_id, DOMAIN) == []


# ===========================================================================
# ITEM 12 — the Parent baseline survives a Firestore-side failure
# ===========================================================================


def test_a_failed_pilot_write_leaves_the_parent_baseline_finalized(
        parent_app, pilot, unique_suffix):
    """The projection is downstream and the Parent record does not depend on it.

    The Pilot write is made to fail at the repository, which is as deep as a
    failure can go while still being the Pilot's problem. Afterwards the Parent
    baseline is still finalized and byte-identical, and the retry succeeds — so
    there is no distributed transaction and nothing to roll back.
    """
    session_id = f"sess-a2v2-pilotfail-{unique_suffix}"
    _make_session(parent_app, session_id)
    _link_child(pilot["repos"], session_id)
    _trace, record, payload = _drive_baseline(
        parent_app, session_id, {BOOK: "no", PRONOUNS: "not_sure"})

    before = json.dumps(record, sort_keys=True)
    repo = pilot["repos"].parent_baseline_projections_v2
    original_create = repo.create

    def failing_create(projection):
        raise RuntimeError("firestore unavailable")

    repo.create = failing_create
    try:
        with pytest.raises(parent_app["v2_client"].ProjectionUnavailable):
            _project(parent_app, pilot, session_id, record, payload)
    finally:
        repo.create = original_create

    # Nothing was written on the Pilot side.
    assert repo.list_for_source(session_id, DOMAIN) == []

    # The Parent baseline is untouched and still finalized, read back durably.
    parent_app["store"]._cache.clear()
    doc = parent_app["store"].load(parent_app["uid"], session_id,
                                   force_remote=True)
    assert parent_app["api"].is_finalized(doc, DOMAIN) is True
    assert json.dumps(parent_app["api"].finalized_record(doc, DOMAIN),
                      sort_keys=True) == before
    view = parent_app["client"].get(
        f"/api/v2/session/{session_id}/baseline/{DOMAIN}",
        headers=parent_app["auth"])
    assert view.json()["finalized"] is True

    # And the identical retry now succeeds.
    retried = _project(parent_app, pilot, session_id, record, payload)
    assert retried["created"] is True
    assert len(repo.list_for_source(session_id, DOMAIN)) == 1


# ===========================================================================
# ITEM 11 — the logging audit, against the real adapter
# ===========================================================================


def test_no_milestone_prose_reaches_a_log_on_the_real_path(
        parent_app, pilot, unique_suffix, caplog, capsys):
    """The planted string here is a REAL milestone from the frozen source.

    A synthetic sentinel would prove only that the sentinel was not logged. The
    thing that must not be logged is the actual clinical wording the request
    carries, so the check uses it.
    """
    import logging

    session_id = f"sess-a2v2-logs-{unique_suffix}"
    _make_session(parent_app, session_id)
    _link_child(pilot["repos"], session_id)

    caplog.clear()
    capsys.readouterr()
    with caplog.at_level(logging.DEBUG):
        trace, record, payload = _drive_baseline(
            parent_app, session_id, {BOOK: "no", PRONOUNS: "not_sure"})
        result = _project(parent_app, pilot, session_id, record, payload)
    assert result["created"] is True

    captured = capsys.readouterr()
    haystack = "\n".join([
        caplog.text,
        "".join(record_.getMessage() for record_ in caplog.records),
        captured.out, captured.err,
    ])

    milestones = {milestone for _m, milestone, _a in trace}
    assert milestones
    for milestone in milestones:
        assert milestone not in haystack, milestone
    for fragment in (BOOK, VOCAB, PRONOUNS, "skill_key"):
        assert fragment not in haystack, fragment
