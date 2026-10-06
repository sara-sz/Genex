"""Parent's outbound session-claim mint (0.5F-A3).

Covers the pairing guard, the token properties, the three-field registration
payload, the mint route, and the property that matters most: the raw token goes
to the authenticated browser and NOWHERE else — not to storage, not to a log,
and not to the Pilot.

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
# The bootstrap variables are deliberately ABSENT by default: an unconfigured
# deployment must not be able to mint a capability.
for _var in ("PILOT_BOOTSTRAP_URL", "PILOT_BOOTSTRAP_AUDIENCE",
             "GENEX_PILOT_PROJECTION_PAIRING"):
    os.environ.pop(_var, None)

shutil.rmtree("/tmp/genex_api_sessions", ignore_errors=True)

import firebase_admin  # noqa: E402

firebase_admin._apps["[DEFAULT]"] = object()
from firebase_admin import auth as firebase_auth  # noqa: E402

_TOKENS = {
    "token-claim-a": {"uid": "uid-claim-a", "email": "a@example.invalid"},
    "token-claim-b": {"uid": "uid-claim-b", "email": "b@example.invalid"},
}
firebase_auth.verify_id_token = (
    lambda t, *a, **k: _TOKENS[t] if t in _TOKENS
    else (_ for _ in ()).throw(firebase_auth.InvalidIdTokenError("bad")))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api import parent_session_handoff_client as claim_client  # noqa: E402
from api import session_store  # noqa: E402
from api.main import app  # noqa: E402

client = TestClient(app)
REPO = pathlib.Path(__file__).resolve().parents[1]
AUTH_A = {"Authorization": "Bearer token-claim-a"}
AUTH_B = {"Authorization": "Bearer token-claim-b"}

AUDIENCE = "https://pilot-bootstrap-staging-abc-uc.a.run.app"
GOOD_ENV = {
    "GENEX_PILOT_PROJECTION_PAIRING": "parent-staging->pilot-staging",
    "PILOT_BOOTSTRAP_AUDIENCE": AUDIENCE,
    "PILOT_BOOTSTRAP_URL": AUDIENCE + claim_client.BOOTSTRAP_PATH,
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


# ---------------------------------------------------------------------------
# 1. the pairing guard
# ---------------------------------------------------------------------------

def test_an_unconfigured_deployment_cannot_mint():
    """Fail closed by ABSENCE. Parent prod, with nothing set, cannot mint."""
    with pytest.raises(claim_client.ClaimNotConfigured):
        claim_client.claim_config({})


def test_a_missing_pairing_token_is_refused_before_the_url_is_read():
    with pytest.raises(claim_client.ClaimNotConfigured):
        claim_client.claim_config({
            "PILOT_BOOTSTRAP_URL": GOOD_ENV["PILOT_BOOTSTRAP_URL"],
            "PILOT_BOOTSTRAP_AUDIENCE": AUDIENCE})


@pytest.mark.parametrize("pairing", [
    "parent-prod->pilot-staging",
    "parent-prod->pilot-prod",
    "parent-staging->pilot-prod",
    "PARENT-STAGING->PILOT-STAGING",
    "anything",
])
def test_only_the_one_permitted_pairing_is_allowed(pairing):
    env = dict(GOOD_ENV, GENEX_PILOT_PROJECTION_PAIRING=pairing)
    with pytest.raises((claim_client.ClaimPairingForbidden,
                        claim_client.ClaimNotConfigured)):
        claim_client.claim_config(env)


def test_the_permitted_pairing_set_contains_no_production_pairing():
    assert claim_client.PERMITTED_PAIRINGS == ("parent-staging->pilot-staging",)
    for pairing in claim_client.PERMITTED_PAIRINGS:
        assert "prod" not in pairing


def test_the_pairing_variable_is_shared_with_the_a2_projection_client():
    """One variable governs whether THIS Parent may talk to THAT Pilot, so the
    two boundaries cannot drift into disagreeing about which pairing is live."""
    from api import parent_baseline_projection_client as projection_client

    assert claim_client.PAIRING_ENV_VAR == projection_client.PAIRING_ENV_VAR
    assert claim_client.PERMITTED_PAIRINGS == projection_client.PERMITTED_PAIRINGS


def test_the_configured_pairing_is_accepted():
    url, audience = claim_client.claim_config(GOOD_ENV)
    assert url == GOOD_ENV["PILOT_BOOTSTRAP_URL"]
    assert audience == AUDIENCE


@pytest.mark.parametrize("url", [
    "http://pilot-bootstrap-staging-abc-uc.a.run.app"
    "/internal/parent-session-claims",                       # plaintext
    AUDIENCE,                                                # not the endpoint
    AUDIENCE + "/internal/other",                            # wrong endpoint
    "https://evil.example/internal/parent-session-claims",   # not the audience
])
def test_a_bad_target_url_is_refused(url):
    with pytest.raises(claim_client.ClaimNotConfigured):
        claim_client.claim_config(dict(GOOD_ENV, PILOT_BOOTSTRAP_URL=url))


def test_the_audience_must_be_the_service_the_url_addresses():
    with pytest.raises(claim_client.ClaimNotConfigured):
        claim_client.claim_config(dict(
            GOOD_ENV, PILOT_BOOTSTRAP_AUDIENCE="https://other.run.app"))


def test_a_plaintext_target_is_refused_not_upgraded():
    """ISOLATED from the audience-match check: a plaintext URL with a matching
    plaintext audience satisfies `url.startswith(audience)`, so only the https
    requirement itself can refuse this case."""
    with pytest.raises(claim_client.ClaimNotConfigured):
        claim_client.claim_config(dict(
            GOOD_ENV,
            PILOT_BOOTSTRAP_URL="http://plain.example"
                                + claim_client.BOOTSTRAP_PATH,
            PILOT_BOOTSTRAP_AUDIENCE="http://plain.example"))


def test_the_bootstrap_target_is_not_the_projection_target():
    """Two separate private services; one URL must not address the other."""
    from api import parent_baseline_projection_client as projection_client

    assert claim_client.BOOTSTRAP_PATH != projection_client.PROJECTION_PATH
    assert claim_client.URL_ENV_VAR != projection_client.URL_ENV_VAR
    assert claim_client.AUDIENCE_ENV_VAR != projection_client.AUDIENCE_ENV_VAR


# ---------------------------------------------------------------------------
# 2. token and digest properties
# ---------------------------------------------------------------------------

def test_generated_tokens_are_unique_and_long():
    tokens = {claim_client.generate_claim_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) >= 43 for t in tokens)


def test_the_token_generator_uses_the_csprng():
    source = (REPO / "api" / "parent_session_handoff_client.py").read_text()
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    assert "secrets" in names
    assert "random" not in names


def test_the_digest_matches_the_pilots_definition_byte_for_byte():
    """A CROSS-SYSTEM invariant. If the two sides ever disagree, every token
    becomes unredeemable — loud, but only after a deployment."""
    import sys

    sys.path.insert(0, str(REPO.parent))
    from pilot_backend.domain.parent_session_claim import (
        CLAIM_DIGEST_DOMAIN as PILOT_DOMAIN,
        claim_digest as pilot_digest,
    )

    assert claim_client.CLAIM_DIGEST_DOMAIN == PILOT_DOMAIN
    for token in [claim_client.generate_claim_token() for _ in range(25)]:
        assert claim_client.claim_digest(token) == pilot_digest(token)


def test_the_ttl_matches_the_pilots_ceiling():
    import sys

    sys.path.insert(0, str(REPO.parent))
    from pilot_backend.domain.parent_session_claim import CLAIM_TTL_SECONDS

    assert claim_client.CLAIM_TTL_SECONDS == CLAIM_TTL_SECONDS
    assert 300 <= claim_client.CLAIM_TTL_SECONDS <= 900


def test_a_short_token_cannot_be_digested():
    with pytest.raises(claim_client.ClaimRejected):
        claim_client.claim_digest("tiny")


# ---------------------------------------------------------------------------
# 3. what is sent, and what is not
# ---------------------------------------------------------------------------

def _mint(session_id="sess-a", env=None, status=201, payload=None,
          captured=None):
    def fetcher(audience):
        return "fake-identity-token"

    def poster(url, body, token, timeout):
        if captured is not None:
            captured["url"] = url
            captured["body"] = body
            captured["token"] = token
        return status, (payload if payload is not None
                        else {"registered": True, "created": True,
                              "expires_at": "2026-10-05T12:10:00+00:00"})

    return claim_client.mint_session_claim(
        source_session_id=session_id, env=env or GOOD_ENV,
        fetcher=fetcher, poster=poster)


def test_the_registration_payload_is_exactly_three_fields():
    captured = {}
    _mint(captured=captured)
    assert set(captured["body"]) == {"claim_digest", "source_session_id",
                                     "ttl_seconds"}


def test_the_raw_token_is_never_sent_to_the_pilot():
    """THE central guarantee. Only a preimage can be redeemed, and the Pilot
    never holds one."""
    captured = {}
    result = _mint(captured=captured)
    rendered = json.dumps(captured["body"])
    assert result["claim_token"] not in rendered
    assert captured["body"]["claim_digest"] == claim_client.claim_digest(
        result["claim_token"])


def test_no_uid_or_clinical_field_is_sent():
    captured = {}
    _mint(captured=captured)
    for forbidden in ("parent_uid", "owner_uid", "uid", "auth_subject",
                      "email", "child_id", "caregiver_id",
                      "external_owner_ref", "child_name", "diagnosis",
                      "concern", "qna", "asked", "activities", "schedules",
                      "baseline", "routing_anchor_months"):
        assert forbidden not in captured["body"]


def test_the_identity_token_is_audience_bound_and_sent_as_a_bearer():
    captured = {}
    _mint(captured=captured)
    assert captured["token"] == "fake-identity-token"
    assert captured["url"] == GOOD_ENV["PILOT_BOOTSTRAP_URL"]


def test_an_unreachable_pilot_is_retryable_and_leaks_no_url():
    def fetcher(audience):
        return "t"

    def poster(url, body, token, timeout):
        raise RuntimeError(f"connection refused to {url} with {body}")

    with pytest.raises(claim_client.ClaimUnavailable) as exc:
        claim_client.mint_session_claim(source_session_id="s", env=GOOD_ENV,
                                        fetcher=fetcher, poster=poster)
    assert "run.app" not in str(exc.value)
    assert "claim_digest" not in str(exc.value)


def test_a_missing_identity_token_is_retryable():
    def fetcher(audience):
        raise RuntimeError("not running on Google infrastructure")

    with pytest.raises(claim_client.ClaimUnavailable):
        claim_client.mint_session_claim(source_session_id="s", env=GOOD_ENV,
                                        fetcher=fetcher,
                                        poster=lambda *a: (201, {}))


@pytest.mark.parametrize("status", [400, 404, 409])
def test_a_refused_registration_is_not_retryable(status):
    with pytest.raises(claim_client.ClaimRejected):
        _mint(status=status, payload={})


@pytest.mark.parametrize("status", [500, 502, 503, 429])
def test_a_transient_failure_is_retryable(status):
    with pytest.raises(claim_client.ClaimUnavailable):
        _mint(status=status, payload={})


def test_a_retry_mints_a_new_token_rather_than_reusing_one():
    """Re-registering a token Parent may already have handed out would be the
    dangerous choice; the unregistered one simply becomes unredeemable."""
    first, second = {}, {}
    _mint(captured=first)
    _mint(captured=second)
    assert first["body"]["claim_digest"] != second["body"]["claim_digest"]


def test_this_module_never_logs():
    source = (REPO / "api" / "parent_session_handoff_client.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            assert name not in ("print", "getLogger", "warning", "info",
                                "debug", "error", "exception", "log")
    names = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)}
    assert "logging" not in names


# ---------------------------------------------------------------------------
# 4. the mint route
# ---------------------------------------------------------------------------

def test_the_route_requires_authentication():
    _make_session("uid-claim-a", "sess-route-1")
    response = client.post("/api/v1/session/sess-route-1/pilot-claim")
    assert response.status_code in (401, 403)


def test_another_caregivers_session_cannot_be_claimed():
    """`_require_session` is the ownership proof the whole capability rests on."""
    _make_session("uid-claim-a", "sess-route-2")
    response = client.post("/api/v1/session/sess-route-2/pilot-claim",
                           headers=AUTH_B)
    assert response.status_code in (403, 404)


def test_a_missing_session_is_refused():
    response = client.post("/api/v1/session/sess-does-not-exist/pilot-claim",
                           headers=AUTH_A)
    assert response.status_code in (403, 404)


def test_an_unconfigured_deployment_returns_501_not_500():
    """A capability gap, not a fault — an operator needs to tell them apart."""
    _make_session("uid-claim-a", "sess-route-3")
    response = client.post("/api/v1/session/sess-route-3/pilot-claim",
                           headers=AUTH_A)
    assert response.status_code == 501
    assert response.json()["detail"] == "claim_not_configured"


def test_the_route_takes_no_body_fields_that_could_name_a_session():
    """The session is a PATH segment, already proven owned. A body naming a
    different session would be a second, unauthorized way to choose one."""
    _make_session("uid-claim-a", "sess-route-4")
    response = client.post("/api/v1/session/sess-route-4/pilot-claim",
                           json={"source_session_id": "somebody-elses"},
                           headers=AUTH_A)
    # Still the configuration refusal: the body is simply never consulted.
    assert response.status_code == 501


def test_the_parent_session_is_not_modified_by_minting():
    """No token, digest or Pilot identifier is written back into the session."""
    doc = _make_session("uid-claim-a", "sess-route-5")
    before = json.dumps(doc, sort_keys=True, default=str)
    client.post("/api/v1/session/sess-route-5/pilot-claim", headers=AUTH_A)
    session_store._cache.clear()
    after_doc = session_store.load("uid-claim-a", "sess-route-5",
                                   force_remote=True)
    rendered = json.dumps(after_doc, sort_keys=True, default=str)
    for forbidden in ("claim_token", "claim_digest", "pilot_child_id",
                      "capability"):
        assert forbidden not in rendered
    assert before == rendered


def test_the_route_is_declared_and_distinct_from_the_projection_route():
    paths = {r.path for r in app.routes}
    assert "/api/v1/session/{session_id}/pilot-claim" in paths
    assert "/api/v1/session/{session_id}/baseline/{domain}/projection" in paths


def test_the_a2_projection_client_is_unchanged_by_this_slice():
    """Byte-identity against the frozen A2 tag, not a substring scan."""
    import subprocess

    path = "genex-parent/api/parent_baseline_projection_client.py"
    want = subprocess.run(
        ["git", "rev-parse",
         f"october-pilot-0.5f-a2-parent-baseline-projection:{path}"],
        capture_output=True, text=True, cwd=REPO.parent)
    if want.returncode != 0:
        pytest.skip("the A2 tag is not present in this checkout")
    got = subprocess.run(["git", "hash-object", path], capture_output=True,
                         text=True, check=True, cwd=REPO.parent)
    assert got.stdout.strip() == want.stdout.strip()
