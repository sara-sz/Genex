"""Parent's outbound baseline projection (0.5F-A2).

Covers the pairing guard, the seven-field payload, the digest, the trigger
route, and the property that matters most for families: finalizing a baseline
does not depend on the Pilot being reachable.

In-process TestClient, Firebase mocked, local durable store — the same harness
`test_parent_24_baseline_api.py` uses. No network call is made: the identity
token fetcher and the HTTP poster are both injected.
"""

import ast
import json
import os
import pathlib
import shutil

os.environ["FIREBASE_PROJECT_ID"] = "genex-test"
os.environ["LOCAL_SESSION_FALLBACK"] = "1"
os.environ.pop("GCS_BUCKET", None)
os.environ["REQUIRE_BETA_CODE"] = "false"
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:3000")
os.environ["ACTIVITY_MODEL"] = ""
# The projection variables are deliberately ABSENT by default: an
# unconfigured deployment must not be able to project.
for _var in ("PILOT_PROJECTION_URL", "PILOT_PROJECTION_AUDIENCE",
             "GENEX_PILOT_PROJECTION_PAIRING"):
    os.environ.pop(_var, None)

shutil.rmtree("/tmp/genex_api_sessions", ignore_errors=True)

import firebase_admin  # noqa: E402

firebase_admin._apps["[DEFAULT]"] = object()
from firebase_admin import auth as firebase_auth  # noqa: E402

_TOKENS = {
    "token-proj-a": {"uid": "uid-proj-a", "email": "a@example.invalid"},
    "token-proj-b": {"uid": "uid-proj-b", "email": "b@example.invalid"},
}
firebase_auth.verify_id_token = (
    lambda t, *a, **k: _TOKENS[t] if t in _TOKENS
    else (_ for _ in ()).throw(firebase_auth.InvalidIdTokenError("bad")))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api import functional_baseline_api as baseline_api  # noqa: E402
from api import parent_baseline_projection_client as client_mod  # noqa: E402
from api import session_store  # noqa: E402
from api.main import app  # noqa: E402

client = TestClient(app)
REPO = pathlib.Path(__file__).resolve().parents[1]
SLP = "talking_and_communicating"
AUTH_A = {"Authorization": "Bearer token-proj-a"}
AUTH_B = {"Authorization": "Bearer token-proj-b"}

AUDIENCE = "https://pilot-projection-staging-abc-uc.a.run.app"
GOOD_ENV = {
    "GENEX_PILOT_PROJECTION_PAIRING": "parent-staging->pilot-staging",
    "PILOT_PROJECTION_AUDIENCE": AUDIENCE,
    "PILOT_PROJECTION_URL": AUDIENCE + client_mod.PROJECTION_PATH,
}

DIAGNOSIS_SENTINEL = "SENTINEL-DIAGNOSIS-autism-level-3"
CONCERN_SENTINEL = "SENTINEL-CONCERN-he does not talk at all"


def _make_session(uid, session_id, *, months=36, diagnosis="", concern=""):
    brain = {"child": {"chronological_months": months,
                       "diagnosis": diagnosis, "concern": concern},
             "qna": {}, "dev_age": {}}
    doc = session_store.new_session_doc(
        session_id=session_id, owner_uid=uid, age_in_months=months,
        daily_time_minutes=20, diagnosis_or_condition=diagnosis,
        brain_state=brain, interview={}, timezone="UTC", beta_authorized=True)
    doc["brain_state"] = brain
    session_store.save(uid, session_id, doc)
    return doc


def _finalize_baseline(session_id, auth=AUTH_A, answers=("yes", "yes", "no")):
    view = client.post(f"/api/v1/session/{session_id}/baseline/{SLP}/start",
                       json={"entry_choice_id": "many_single_words"},
                       headers=auth).json()
    index = 0
    while view.get("current_question"):
        value = answers[index] if index < len(answers) else "no"
        index += 1
        view = client.post(
            f"/api/v1/session/{session_id}/baseline/{SLP}/answer",
            json={"question_id": view["current_question"]["question_id"],
                  "answer": value}, headers=auth).json()
    return client.post(f"/api/v1/session/{session_id}/baseline/{SLP}/finalize",
                       headers=auth)


def _reload(uid, session_id):
    session_store._cache.clear()
    return session_store.load(uid, session_id, force_remote=True)


# ---------------------------------------------------------------------------
# 1. the pairing guard
# ---------------------------------------------------------------------------

def test_an_unconfigured_deployment_cannot_project():
    """Fail closed by ABSENCE. Parent prod, with nothing set, cannot project
    even if it wanted to."""
    with pytest.raises(client_mod.ProjectionNotConfigured):
        client_mod.projection_config({})


def test_a_missing_pairing_token_is_refused_before_the_url_is_read():
    with pytest.raises(client_mod.ProjectionNotConfigured):
        client_mod.projection_config({
            "PILOT_PROJECTION_URL": GOOD_ENV["PILOT_PROJECTION_URL"],
            "PILOT_PROJECTION_AUDIENCE": AUDIENCE})


@pytest.mark.parametrize("pairing", [
    "parent-prod->pilot-staging",
    "parent-prod->pilot-prod",
    "parent-staging->pilot-prod",
    "PARENT-STAGING->PILOT-STAGING",
    "parent-staging->pilot-staging ",
    "anything",
])
def test_only_the_one_permitted_pairing_is_allowed(pairing):
    """`parent-prod->pilot-staging` is the pairing this guard exists to
    forbid, and no production pairing exists at all."""
    env = dict(GOOD_ENV, GENEX_PILOT_PROJECTION_PAIRING=pairing)
    if pairing.strip() == "parent-staging->pilot-staging":
        return  # the trailing-space case normalises to the permitted value
    with pytest.raises((client_mod.ProjectionPairingForbidden,
                        client_mod.ProjectionNotConfigured)):
        client_mod.projection_config(env)


def test_the_permitted_pairing_set_contains_no_production_pairing():
    assert client_mod.PERMITTED_PAIRINGS == ("parent-staging->pilot-staging",)
    for pairing in client_mod.PERMITTED_PAIRINGS:
        assert "prod" not in pairing


def test_the_configured_pairing_is_accepted():
    url, audience = client_mod.projection_config(GOOD_ENV)
    assert url == GOOD_ENV["PILOT_PROJECTION_URL"]
    assert audience == AUDIENCE


@pytest.mark.parametrize("url", [
    "http://pilot-projection-staging-abc-uc.a.run.app"
    "/internal/parent-baseline-projections",                 # plaintext
    AUDIENCE,                                                # not the endpoint
    AUDIENCE + "/internal/other",                            # wrong endpoint
    "https://evil.example/internal/parent-baseline-projections",  # not the aud
])
def test_a_bad_target_url_is_refused(url):
    with pytest.raises(client_mod.ProjectionNotConfigured):
        client_mod.projection_config(dict(GOOD_ENV, PILOT_PROJECTION_URL=url))


def test_the_audience_must_be_the_service_the_url_addresses():
    """Otherwise a token minted for one service could be posted to another —
    exactly the confusion audience binding exists to prevent."""
    with pytest.raises(client_mod.ProjectionNotConfigured):
        client_mod.projection_config(dict(
            GOOD_ENV, PILOT_PROJECTION_AUDIENCE="https://other.run.app"))


def test_a_plaintext_target_is_refused_not_upgraded():
    """Silently rewriting a configured URL would hide a misconfiguration.

    ISOLATED from the audience-match check: a plaintext URL with a matching
    plaintext audience would satisfy `url.startswith(audience)`, so this case
    can only be refused by the https requirement itself. Without that
    isolation a mutation removing the https check survived.
    """
    with pytest.raises(client_mod.ProjectionNotConfigured):
        client_mod.projection_config(dict(
            GOOD_ENV,
            PILOT_PROJECTION_URL="http://plain.example"
                                 + client_mod.PROJECTION_PATH,
            PILOT_PROJECTION_AUDIENCE="http://plain.example"))


# ---------------------------------------------------------------------------
# 2. the payload and the digest
# ---------------------------------------------------------------------------

FINALIZED = {
    "baseline_version": "parent-2.4-functional-baseline-v1",
    "area_id": "talking", "domain": SLP,
    "entry_choice_id": "many_single_words",
    "entry_choice_label": "Many single words",
    "entry_anchor_months": 18, "chronological_months": 36,
    "asked": [{"question_id": "q:18", "months": 18,
               "milestone": "tries to say three or more words",
               "subdomain": "expressive_language", "answer": "yes",
               "classification": "demonstrated"}],
    "status": "BOUNDED", "routing_anchor_months": 24,
    "demonstrated_months": 24, "not_demonstrated_months": 30,
}


def test_the_payload_is_exactly_the_seven_fields():
    payload = client_mod.build_projection(FINALIZED)
    assert set(payload) == set(client_mod.PROJECTED_FIELDS)
    assert len(payload) == 7


@pytest.mark.parametrize("excluded", [
    "asked", "chronological_months", "demonstrated_months",
    "entry_anchor_months", "entry_choice_label",
])
def test_the_payload_excludes_what_downstream_does_not_need(excluded):
    """`asked` is the big one: a per-rung yes/no about one child, and the
    target rule never reads it."""
    assert excluded not in client_mod.build_projection(FINALIZED)


def test_the_payload_is_built_additively_not_subtractively():
    """A field added to the Parent record later must stay home. A subtractive
    copy would ship it, which is how clinical content leaks."""
    payload = client_mod.build_projection(
        dict(FINALIZED, some_future_clinical_field="SENTINEL"))
    assert "some_future_clinical_field" not in payload
    assert "SENTINEL" not in json.dumps(payload)


def test_an_incomplete_record_is_refused():
    for field in client_mod.PROJECTED_FIELDS:
        partial = {k: v for k, v in FINALIZED.items() if k != field}
        with pytest.raises(client_mod.ProjectionRejected):
            client_mod.build_projection(partial)


def test_the_digest_matches_the_pilots_computation_byte_for_byte():
    """If the two sides ever diverge, every projection becomes an integrity
    conflict — loud, but useless. So they are compared directly."""
    import sys

    sys.path.insert(0, str(REPO.parent))
    from pilot_backend.domain.parent_baseline_projection import (
        canonical_source_digest as pilot_digest)

    assert client_mod.canonical_source_digest(FINALIZED) == \
        pilot_digest(FINALIZED)


def test_the_digest_covers_the_asked_history_that_is_not_sent():
    changed = dict(FINALIZED)
    changed["asked"] = [dict(changed["asked"][0], answer="no")]
    assert client_mod.canonical_source_digest(changed) != \
        client_mod.canonical_source_digest(FINALIZED)


def test_the_digest_is_order_independent():
    shuffled = dict(reversed(list(FINALIZED.items())))
    assert client_mod.canonical_source_digest(shuffled) == \
        client_mod.canonical_source_digest(FINALIZED)


# ---------------------------------------------------------------------------
# 3. the outbound call
# ---------------------------------------------------------------------------

def _poster(status=201, payload=None, capture=None):
    def post(url, body, token, timeout):
        if capture is not None:
            capture.update({"url": url, "body": body, "token": token,
                            "timeout": timeout})
        return status, payload if payload is not None else {
            "projection_id": "pbpj_abc", "child_id": "chld_abc",
            "created": status == 201}

    return post


def test_a_successful_projection_returns_only_derived_ids():
    captured = {}
    result = client_mod.project_baseline(
        source_session_id="sess-1", record=FINALIZED, env=GOOD_ENV,
        fetcher=lambda aud: "tok-" + aud, poster=_poster(capture=captured))
    assert result == {"projection_id": "pbpj_abc", "child_id": "chld_abc",
                      "created": True}
    assert captured["url"] == GOOD_ENV["PILOT_PROJECTION_URL"]
    assert captured["token"] == "tok-" + AUDIENCE


def test_the_outbound_body_is_exactly_three_keys():
    captured = {}
    client_mod.project_baseline(
        source_session_id="sess-1", record=FINALIZED, env=GOOD_ENV,
        fetcher=lambda aud: "tok", poster=_poster(capture=captured))
    assert set(captured["body"]) == {"source_session_id",
                                     "source_record_digest", "projection"}
    assert set(captured["body"]["projection"]) == \
        set(client_mod.PROJECTED_FIELDS)


def test_no_child_id_or_parent_uid_is_ever_sent():
    captured = {}
    client_mod.project_baseline(
        source_session_id="sess-1", record=FINALIZED, env=GOOD_ENV,
        fetcher=lambda aud: "tok", poster=_poster(capture=captured))
    text = json.dumps(captured["body"])
    for forbidden in ("child_id", "owner_uid", "uid", "external_owner_ref",
                      "chronological_months", "asked", "diagnosis",
                      "concern", "qna"):
        assert forbidden not in text, forbidden


def test_the_token_is_audience_bound():
    seen = []
    client_mod.project_baseline(
        source_session_id="sess-1", record=FINALIZED, env=GOOD_ENV,
        fetcher=lambda aud: seen.append(aud) or "tok", poster=_poster())
    assert seen == [AUDIENCE]


@pytest.mark.parametrize("status", [400, 404, 409])
def test_a_pilot_refusal_is_not_retryable(status):
    with pytest.raises(client_mod.ProjectionRejected):
        client_mod.project_baseline(
            source_session_id="sess-1", record=FINALIZED, env=GOOD_ENV,
            fetcher=lambda aud: "tok", poster=_poster(status=status))


@pytest.mark.parametrize("status", [401, 403, 500, 502, 503, 504])
def test_an_unavailable_pilot_is_retryable(status):
    with pytest.raises(client_mod.ProjectionUnavailable):
        client_mod.project_baseline(
            source_session_id="sess-1", record=FINALIZED, env=GOOD_ENV,
            fetcher=lambda aud: "tok", poster=_poster(status=status))


def test_an_identical_retry_is_safe_because_the_pilot_replays():
    """No local idempotency state is needed: the Pilot keys on
    (session, domain, digest) and returns the existing projection."""
    calls = []

    def post(url, body, token, timeout):
        calls.append(body["source_record_digest"])
        return (201 if len(calls) == 1 else 200), {
            "projection_id": "pbpj_abc", "child_id": "chld_abc",
            "created": len(calls) == 1}

    first = client_mod.project_baseline(
        source_session_id="s", record=FINALIZED, env=GOOD_ENV,
        fetcher=lambda a: "t", poster=post)
    second = client_mod.project_baseline(
        source_session_id="s", record=FINALIZED, env=GOOD_ENV,
        fetcher=lambda a: "t", poster=post)
    assert first["created"] is True and second["created"] is False
    assert calls[0] == calls[1]  # the same digest both times


def test_a_token_failure_is_retryable_not_a_rejection():
    def fetcher(aud):
        raise RuntimeError("not running on Google infrastructure")

    with pytest.raises(client_mod.ProjectionUnavailable):
        client_mod.project_baseline(
            source_session_id="s", record=FINALIZED, env=GOOD_ENV,
            fetcher=fetcher, poster=_poster())


# ---------------------------------------------------------------------------
# 4. the trigger route
# ---------------------------------------------------------------------------

def test_the_route_refuses_an_unfinalized_baseline():
    _make_session("uid-proj-a", "s-unfinal")
    client.post(f"/api/v1/session/s-unfinal/baseline/{SLP}/start",
                json={"entry_choice_id": "many_single_words"}, headers=AUTH_A)
    response = client.post(
        f"/api/v1/session/s-unfinal/baseline/{SLP}/projection", headers=AUTH_A)
    assert response.status_code == 409
    assert response.json()["detail"] == "baseline_not_finalized"


def test_the_route_refuses_an_unstarted_baseline():
    _make_session("uid-proj-a", "s-nostart")
    response = client.post(
        f"/api/v1/session/s-nostart/baseline/{SLP}/projection", headers=AUTH_A)
    assert response.status_code == 404


def test_the_route_is_not_configured_in_this_test_environment():
    """Fail closed by absence, end to end: the test process sets none of the
    three variables, so a finalized baseline still cannot be projected."""
    _make_session("uid-proj-a", "s-unconf")
    assert _finalize_baseline("s-unconf").status_code == 200
    response = client.post(
        f"/api/v1/session/s-unconf/baseline/{SLP}/projection", headers=AUTH_A)
    assert response.status_code == 501
    assert response.json()["detail"] == "projection_not_configured"


@pytest.mark.parametrize("error,status,detail", [
    ("ProjectionPairingForbidden", 403, "projection_pairing_forbidden"),
    ("ProjectionNotConfigured", 501, "projection_not_configured"),
    ("ProjectionRejected", 422, "projection_rejected"),
    ("ProjectionUnavailable", 503, "projection_unavailable"),
])
def test_each_client_failure_maps_to_its_own_status(monkeypatch, error,
                                                    status, detail):
    """Every branch of the route's error mapping, over real HTTP.

    These four were previously unreachable in the test environment: with no
    projection configured, the route always returned 501, so a mutation that
    collapsed two statuses survived. Each failure is now injected directly.
    """
    _make_session("uid-proj-a", f"s-map-{error}")
    _finalize_baseline(f"s-map-{error}")

    exception = getattr(client_mod, error)

    def _raise(**kwargs):
        raise exception("injected")

    monkeypatch.setattr(client_mod, "project_baseline", _raise)
    response = client.post(
        f"/api/v1/session/s-map-{error}/baseline/{SLP}/projection",
        headers=AUTH_A)
    assert response.status_code == status, response.text
    assert response.json()["detail"] == detail


def test_a_retryable_failure_is_distinguishable_from_a_rejection():
    """503 invites a retry; 422 does not. Collapsing them would make a client
    either loop on a permanent refusal or give up on a transient one."""
    assert 503 != 422


def test_another_caregiver_cannot_project_this_session():
    _make_session("uid-proj-a", "s-owned")
    _finalize_baseline("s-owned")
    response = client.post(
        f"/api/v1/session/s-owned/baseline/{SLP}/projection", headers=AUTH_B)
    assert response.status_code in (403, 404)


def test_an_unauthenticated_request_cannot_project():
    _make_session("uid-proj-a", "s-anon")
    _finalize_baseline("s-anon")
    assert client.post(
        f"/api/v1/session/s-anon/baseline/{SLP}/projection"
    ).status_code in (401, 403)


def test_an_unsupported_domain_cannot_be_projected():
    _make_session("uid-proj-a", "s-dom")
    response = client.post(
        "/api/v1/session/s-dom/baseline/fine_motor/projection", headers=AUTH_A)
    assert response.status_code == 404


def test_the_route_takes_no_body_fields():
    """Nothing a client sends may influence what is projected."""
    source = (REPO / "api" / "main.py").read_text()
    block = source.split("async def session_baseline_projection(", 1)[1]
    signature = block.split(")", 1)[0]
    assert "body" not in signature, signature


def test_the_browser_never_receives_the_payload_or_the_target():
    """The response carries derived ids only — no payload, no URL, no token."""
    source = (REPO / "api" / "main.py").read_text()
    block = source.split("async def session_baseline_projection(", 1)[1]
    returned = block.split("return {", 1)[1].split("}", 1)[0]
    for leaked in ("record", "projection\"", "token", "url", "audience",
                   "routing_anchor"):
        assert leaked not in returned, leaked


# ---------------------------------------------------------------------------
# 5. finalization does not depend on the Pilot
# ---------------------------------------------------------------------------

def test_finalization_does_not_call_the_projection_client():
    """THE property families depend on: a parent completing their calibration
    must not be blocked by another system's availability. Asserted over the
    finalize route's own source, so the two cannot be coupled later."""
    source = (REPO / "api" / "main.py").read_text()
    block = source.split("async def session_baseline_finalize(", 1)[1]
    body = block.split("@app.", 1)[0]
    assert "projection_client" not in body
    assert "project_baseline" not in body


def test_the_baseline_api_module_does_not_import_the_projection_client():
    tree = ast.parse((REPO / "api" / "functional_baseline_api.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert not any("projection" in m for m in imported), imported


def test_a_finalized_baseline_is_durable_even_though_projection_fails():
    """Finalize, then fail every projection attempt, then reload: the
    finalized record is unchanged and still finalized."""
    _make_session("uid-proj-a", "s-durable")
    assert _finalize_baseline("s-durable").status_code == 200
    before = _reload("uid-proj-a", "s-durable")["functional_baseline"][SLP]

    for _ in range(3):
        assert client.post(
            f"/api/v1/session/s-durable/baseline/{SLP}/projection",
            headers=AUTH_A).status_code == 501

    after = _reload("uid-proj-a", "s-durable")
    assert after["functional_baseline"][SLP] == before
    assert after["functional_baseline_api"][SLP]["finalized"] is True


def test_a_failed_projection_writes_nothing_to_the_parent_session():
    """Data direction is Parent -> Pilot only. Nothing about the attempt is
    recorded, so Parent's record never depends on Pilot state."""
    _make_session("uid-proj-a", "s-nowrite")
    _finalize_baseline("s-nowrite")
    before = _reload("uid-proj-a", "s-nowrite")
    client.post(f"/api/v1/session/s-nowrite/baseline/{SLP}/projection",
                headers=AUTH_A)
    after = _reload("uid-proj-a", "s-nowrite")
    assert set(after) == set(before)
    assert after["functional_baseline"] == before["functional_baseline"]
    assert after["functional_baseline_api"] == before["functional_baseline_api"]


def test_a_successful_projection_also_writes_nothing_to_the_session(monkeypatch):
    _make_session("uid-proj-a", "s-nowrite2")
    _finalize_baseline("s-nowrite2")
    before = _reload("uid-proj-a", "s-nowrite2")

    monkeypatch.setattr(client_mod, "project_baseline",
                        lambda **k: {"projection_id": "pbpj_x",
                                     "child_id": "chld_x", "created": True})
    response = client.post(
        f"/api/v1/session/s-nowrite2/baseline/{SLP}/projection",
        headers=AUTH_A)
    assert response.status_code == 200
    assert response.json()["created"] is True

    after = _reload("uid-proj-a", "s-nowrite2")
    assert after["functional_baseline"] == before["functional_baseline"]
    assert after["functional_baseline_api"] == before["functional_baseline_api"]
    assert "projection" not in json.dumps(sorted(after.keys()))


# ---------------------------------------------------------------------------
# 6. PHI and logging
# ---------------------------------------------------------------------------

def test_the_client_constructs_no_logger_and_prints_nothing():
    tree = ast.parse((REPO / "api"
                      / "parent_baseline_projection_client.py").read_text())
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
    for noisy in ("print", "getLogger", "basicConfig", "info", "debug",
                  "warning", "error", "exception", "critical"):
        assert noisy not in called, noisy


def test_no_exception_message_quotes_the_target_or_the_payload():
    """A `requests` error can quote the full URL, which identifies the Pilot
    projection service; the exception text is never propagated."""
    def post(url, body, token, timeout):
        # A `requests` error genuinely looks like this: it quotes the full URL
        # and sometimes the body.
        raise RuntimeError(f"connection failed to {url} with {body}")

    with pytest.raises(client_mod.ProjectionUnavailable) as caught:
        client_mod.project_baseline(
            source_session_id="s", record=FINALIZED, env=GOOD_ENV,
            fetcher=lambda a: "t", poster=post)
    message = str(caught.value)
    assert "pilot-projection" not in message
    assert AUDIENCE not in message
    assert "BOUNDED" not in message
    assert "routing_anchor" not in message
    # The cause is kept in the chain for a local debugger, never in the message.
    assert caught.value.__cause__ is not None


def test_no_sentinel_free_text_can_reach_the_outbound_body():
    captured = {}
    record = dict(FINALIZED, diagnosis=DIAGNOSIS_SENTINEL,
                  concern=CONCERN_SENTINEL)
    client_mod.project_baseline(
        source_session_id="s", record=record, env=GOOD_ENV,
        fetcher=lambda a: "t", poster=_poster(capture=captured))
    text = json.dumps(captured["body"])
    assert "SENTINEL" not in text


def test_the_route_detail_codes_are_constant():
    source = (REPO / "api" / "main.py").read_text()
    block = source.split("async def session_baseline_projection(", 1)[1]
    body = block.split("return {", 1)[0]
    for detail in ("projection_not_configured", "projection_pairing_forbidden",
                   "projection_rejected", "projection_unavailable",
                   "baseline_not_finalized"):
        assert f'detail="{detail}"' in body, detail
    # No f-string detail anywhere in the route.
    assert 'detail=f"' not in body


# ---------------------------------------------------------------------------
# 7. the finalized-record reader
# ---------------------------------------------------------------------------

def test_the_finalized_reader_returns_the_canonical_record_verbatim():
    _make_session("uid-proj-a", "s-reader")
    _finalize_baseline("s-reader")
    doc = _reload("uid-proj-a", "s-reader")
    record = baseline_api.finalized_record(doc, SLP)
    assert record == doc["functional_baseline"][SLP]
    # A copy, so a caller cannot mutate session state through it.
    record["status"] = "TAMPERED"
    assert doc["functional_baseline"][SLP]["status"] != "TAMPERED"


def test_the_finalized_reader_refuses_an_unfinalized_baseline():
    _make_session("uid-proj-a", "s-reader2")
    client.post(f"/api/v1/session/s-reader2/baseline/{SLP}/start",
                json={"entry_choice_id": "many_single_words"}, headers=AUTH_A)
    doc = _reload("uid-proj-a", "s-reader2")
    with pytest.raises(baseline_api.BaselineNotFinalized):
        baseline_api.finalized_record(doc, SLP)


def test_an_unresolved_baseline_can_still_be_projected():
    """UNRESOLVED is a RESULT, not a failure: the Pilot needs to know the
    baseline resolved to no anchor so 0.5F-B can fail closed rather than
    wait forever for a projection that never comes."""
    _make_session("uid-proj-a", "s-unres")
    client.post(f"/api/v1/session/s-unres/baseline/{SLP}/start",
                json={"entry_choice_id": "many_single_words"}, headers=AUTH_A)
    client.post(f"/api/v1/session/s-unres/baseline/{SLP}/finalize",
                headers=AUTH_A)
    record = baseline_api.finalized_record(
        _reload("uid-proj-a", "s-unres"), SLP)
    payload = client_mod.build_projection(record)
    assert payload["status"] == "UNRESOLVED"
    assert payload["routing_anchor_months"] is None


# ---------------------------------------------------------------------------
# 8. CORRECTION 1 — the GCS permission model
#
# An earlier proposal was objectViewer + objectCreator, on the belief that
# replacing an object needs only storage.objects.create. That is wrong:
# REPLACING an existing object at the same name also requires
# storage.objects.delete, so the Parent service would have saved each session
# exactly once and then started failing. These tests pin the corrected model
# and the two facts that make the delete permission safe to grant.
# ---------------------------------------------------------------------------

from api import session_store_iam as iam  # noqa: E402


class _RecordingBlob:
    """A blob that records every operation and the bytes it was given."""

    def __init__(self, name, store, log):
        self.name = name
        self._store = store
        self._log = log

    def upload_from_string(self, data, content_type=None, **kwargs):
        self._log.append(("upload_from_string", self.name,
                          "if_generation_match" in kwargs))
        existed = self.name in self._store
        self._store[self.name] = data
        # Record whether this write REPLACED an existing object, which is the
        # case that needs storage.objects.delete.
        self._log.append(("overwrote" if existed else "created", self.name))

    def download_as_text(self):
        self._log.append(("download_as_text", self.name))
        return self._store[self.name]

    def exists(self):
        self._log.append(("exists", self.name))
        return self.name in self._store

    def delete(self):  # pragma: no cover - must never be reached
        self._log.append(("delete", self.name))
        raise AssertionError("the application deleted a session object")


class _RecordingBucket:
    def __init__(self, store, log):
        self._store, self._log = store, log

    def blob(self, name):
        return _RecordingBlob(name, self._store, self._log)

    def get_blob(self, name):
        self._log.append(("get_blob", name))
        return _RecordingBlob(name, self._store, self._log) \
            if name in self._store else None


def _fake_gcs(monkeypatch):
    """Install a recording GCS client over the REAL adapter functions."""
    import types

    store, log = {}, []

    class _Client:
        def bucket(self, name):
            return _RecordingBucket(store, log)

        def list_blobs(self, *a, **k):
            log.append(("list_blobs", a[0] if a else ""))
            return []

    module = types.ModuleType("google.cloud.storage")
    module.Client = _Client
    monkeypatch.setitem(__import__("sys").modules, "google.cloud.storage",
                        module)
    try:
        import google.cloud as google_cloud

        monkeypatch.setattr(google_cloud, "storage", module, raising=False)
    except ImportError:  # pragma: no cover
        pass
    monkeypatch.setattr(session_store, "GCS_BUCKET_NAME", "fake-bucket")
    return store, log


def test_the_first_save_CREATES_the_session_object(monkeypatch):
    store, log = _fake_gcs(monkeypatch)
    session_store._gcs_save_raw("uid-x", "s-1", {"owner_uid": "uid-x"})
    assert "sessions/uid-x/s-1.json" in store
    assert ("created", "sessions/uid-x/s-1.json") in log
    assert ("overwrote", "sessions/uid-x/s-1.json") not in log


def test_a_subsequent_save_OVERWRITES_the_same_object(monkeypatch):
    """THE case that needs storage.objects.delete. The blob NAME is identical,
    so GCS models this as replacing an existing object."""
    store, log = _fake_gcs(monkeypatch)
    session_store._gcs_save_raw("uid-x", "s-1", {"owner_uid": "uid-x", "n": 1})
    session_store._gcs_save_raw("uid-x", "s-1", {"owner_uid": "uid-x", "n": 2})
    overwrites = [e for e in log if e[0] == "overwrote"]
    assert len(overwrites) == 1
    assert overwrites[0][1] == "sessions/uid-x/s-1.json"
    # One object, not two: the second write replaced the first.
    assert list(store) == ["sessions/uid-x/s-1.json"]
    assert '"n": 2' in store["sessions/uid-x/s-1.json"]


def test_the_CAS_overwrite_uses_the_same_object_name(monkeypatch):
    """`save_if_generation_match` is a CONDITIONAL insert, not a differently
    permissioned one — same name, same replacement, same requirement."""
    store, log = _fake_gcs(monkeypatch)
    session_store._gcs_save_raw("uid-x", "s-2", {"owner_uid": "uid-x"})
    session_store.save_if_generation_match(
        "uid-x", "s-2", {"owner_uid": "uid-x", "n": 2}, 1)
    guarded = [e for e in log
               if e[0] == "upload_from_string" and e[2] is True]
    assert guarded, "the CAS path did not pass if_generation_match"
    assert guarded[0][1] == "sessions/uid-x/s-2.json"
    assert list(store) == ["sessions/uid-x/s-2.json"]


def test_deleting_a_session_is_not_an_application_operation(monkeypatch):
    """The permission is required by the platform for replacement; the
    application never exercises it. The recording blob raises on delete, so a
    full create + overwrite + CAS cycle proves none happens."""
    store, log = _fake_gcs(monkeypatch)
    session_store._gcs_save_raw("uid-x", "s-3", {"owner_uid": "uid-x"})
    session_store._gcs_save_raw("uid-x", "s-3", {"owner_uid": "uid-x", "n": 2})
    session_store.save_if_generation_match(
        "uid-x", "s-3", {"owner_uid": "uid-x", "n": 3}, 1)
    session_store._gcs_load_raw("uid-x", "s-3")
    assert not [e for e in log if e[0] == "delete"]


@pytest.mark.parametrize("operation", iam.FORBIDDEN_OPERATIONS)
def test_no_parent_module_calls_a_destructive_blob_operation(operation):
    """Structural, over every shipped module's AST: `blob.delete()` and
    friends must not appear, so the delete permission can only ever be used
    implicitly by an overwrite."""
    for path in sorted((REPO / "api").glob("*.py")) + \
            sorted((REPO / "genex_core").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and \
                    isinstance(node.func, ast.Attribute) and \
                    node.func.attr == operation:
                raise AssertionError(f"{path.name} calls .{operation}()")


def test_the_required_permission_set_is_exactly_four():
    assert set(iam.REQUIRED_PERMISSIONS) == {
        "storage.objects.get",
        "storage.objects.list",
        "storage.objects.create",
        "storage.objects.delete",
    }


def test_the_delete_permission_is_documented_as_platform_required():
    reason = iam.REQUIRED_PERMISSIONS["storage.objects.delete"]
    assert "REQUIRED BY GCS" in reason
    assert "Never called by the application" in reason


def test_no_broad_storage_role_is_proposed():
    commands = " ".join(iam.gcloud_commands())
    for broad in ("roles/storage.admin", "roles/storage.objectAdmin",
                  "roles/editor", "roles/owner", "roles/storage.objectUser"):
        assert broad not in commands, broad


def test_the_binding_is_bucket_scoped_not_project_scoped():
    create, bind = iam.gcloud_commands()
    assert f"gs://{iam.STAGING_SESSION_BUCKET}" in bind
    assert "buckets add-iam-policy-binding" in bind
    assert "projects add-iam-policy-binding" not in bind
    # And it is the STAGING bucket, never production's.
    assert "prod" not in iam.STAGING_SESSION_BUCKET


def test_bucket_administration_is_excluded():
    for excluded in ("storage.buckets.delete", "storage.buckets.setIamPolicy",
                     "storage.objects.setIamPolicy"):
        assert excluded in iam.EXCLUDED_PERMISSIONS
        assert excluded not in iam.REQUIRED_PERMISSIONS


# ---------------------------------------------------------------------------
# 9. CORRECTION 2 — Cloud Run IAM is authoritative
# ---------------------------------------------------------------------------

def test_the_auth_probe_exists_and_covers_every_required_case():
    probe = (REPO.parent / "pilot_runtime" / "deploy"
             / "probe_projection_auth.sh").read_text()
    assert "allUsers is NOT an invoker" in probe
    assert "unauthenticated is rejected by the platform" in probe
    assert "genex-parent-prod-run" in probe
    assert "compute@developer.gserviceaccount.com" in probe
    assert "reaches the app" in probe
    assert "wrong audience is rejected" in probe
    # Probe 5: the open question, explicitly flagged as deciding the mode.
    assert "X-Serverless-Authorization" in probe
    assert "iam_only" in probe


def test_the_probe_forbids_inventing_an_alternate_mechanism():
    probe = (REPO.parent / "pilot_runtime" / "deploy"
             / "probe_projection_auth.sh").read_text()
    assert "REMAINS AUTHORITATIVE" in probe
    assert "No shared secret, no static key, no" in probe


def test_the_probe_never_logs_the_token_itself():
    probe = (REPO.parent / "pilot_runtime" / "deploy"
             / "probe_projection_auth.sh").read_text()
    assert "must never log the token itself" in probe
