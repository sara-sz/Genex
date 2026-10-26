"""A2 v2.1 — the sibling route, its storage, and the outcomes that cannot generate.

The gates this file installs permanently:

  * the router dispatches on the PATH; v2 and v2.1 cannot be reached by shape
  * a deployment without the v2.1 app behaves exactly as before — the route's
    absence IS the activation gate
  * a v2.1 submission produces a projection identical to v2's for the same
    evidence, plus a SEPARATE profile record
  * the profile is stored in its own collection, keyed on the source identity
  * Parent's content evaluation is echoed and NEVER stored
  * NO_TARGET_IDENTIFIED and INSUFFICIENT_EVIDENCE can carry a clinician
    summary and cannot reach the GoalSuggestion chokepoint
"""

from __future__ import annotations

import io
import json

import pytest

from pilot_backend.domain.parent_clinician_profile_v21 import (
    AREA_CONCERN_OBSERVED,
    AREA_NOT_ASSESSED,
    CAUSE_NO_AGE_APPROPRIATE_SKILL,
    MULTI_AREA_INDETERMINATE,
    ClinicianProfileV21,
)
from pilot_backend.persistence.collections import COLLECTIONS
from pilot_backend.persistence.firestore_repos import (
    FirestoreClinicianProfileV21Repository,
)
from pilot_backend.transport.projection_v2_wsgi import (
    PROJECTION_V21_ROUTE,
    PROJECTION_V2_ROUTE,
    InternalProjectionRouter,
)
from pilot_backend.transport.projection_v21_wsgi import (
    PROJECTION_V21_ROUTE as CODEC_ROUTE,
    ProjectionV21App,
)


class _Recorder:
    """A WSGI app that records it was called, and nothing else."""

    def __init__(self, name, verifies=False):
        self.name = name
        self.calls = []
        self.verifies_tokens = verifies

    def __call__(self, environ, start_response):
        self.calls.append((environ.get("PATH_INFO"),
                           environ.get("REQUEST_METHOD")))
        start_response("200 OK", [("Content-Type", "application/json")])
        return [json.dumps({"app": self.name}).encode()]


def _call(app, path, method="POST", body=None):
    payload = b"" if body is None else json.dumps(body).encode("utf-8")
    environ = {"PATH_INFO": path, "REQUEST_METHOD": method,
               "CONTENT_LENGTH": str(len(payload)),
               "wsgi.input": io.BytesIO(payload)}
    captured = {}

    def start_response(status, headers):
        captured["status"] = int(status.split()[0])
        captured["headers"] = headers

    chunks = app(environ, start_response)
    raw = b"".join(chunks)
    return captured["status"], (json.loads(raw) if raw else None)


# ---------------------------------------------------------------------------
# 1. dispatch is by PATH
# ---------------------------------------------------------------------------

def test_the_two_route_constants_do_not_drift():
    assert CODEC_ROUTE == PROJECTION_V21_ROUTE
    assert PROJECTION_V21_ROUTE != PROJECTION_V2_ROUTE
    assert PROJECTION_V21_ROUTE.endswith("-v21")


def test_the_router_sends_each_path_to_its_own_app():
    v1, v2, v21 = _Recorder("v1"), _Recorder("v2"), _Recorder("v21")
    router = InternalProjectionRouter(v1_app=v1, v2_app=v2, v21_app=v21)

    assert _call(router, PROJECTION_V2_ROUTE)[1]["app"] == "v2"
    assert _call(router, PROJECTION_V21_ROUTE)[1]["app"] == "v21"
    assert _call(router, "/internal/anything-else")[1]["app"] == "v1"
    assert v2.calls and v21.calls and v1.calls


def test_a_v21_body_posted_to_the_v2_route_reaches_the_v2_app():
    """The version is the PATH. The v2 app then refuses the body itself."""
    v1, v2, v21 = _Recorder("v1"), _Recorder("v2"), _Recorder("v21")
    router = InternalProjectionRouter(v1_app=v1, v2_app=v2, v21_app=v21)
    _call(router, PROJECTION_V2_ROUTE,
          body={"clinician_portfolio": "anything"})
    assert v2.calls and not v21.calls


def test_without_the_v21_app_the_route_falls_through_to_v1():
    """THE ACTIVATION GATE: the route's absence, not a flag in a handler."""
    v1, v2 = _Recorder("v1"), _Recorder("v2")
    router = InternalProjectionRouter(v1_app=v1, v2_app=v2)
    assert router.v21_app is None
    assert _call(router, PROJECTION_V21_ROUTE)[1]["app"] == "v1"
    assert _call(router, PROJECTION_V2_ROUTE)[1]["app"] == "v2"


def test_the_router_refuses_a_half_applied_auth_posture():
    v1, v2 = _Recorder("v1", verifies=True), _Recorder("v2", verifies=True)
    v21 = _Recorder("v21", verifies=False)
    router = InternalProjectionRouter(v1_app=v1, v2_app=v2, v21_app=v21)
    with pytest.raises(ValueError):
        router.verifies_tokens

    agreeing = InternalProjectionRouter(
        v1_app=v1, v2_app=v2, v21_app=_Recorder("v21", verifies=True))
    assert agreeing.verifies_tokens is True


# ---------------------------------------------------------------------------
# 2. the app: two records, one response
# ---------------------------------------------------------------------------

DIGEST = "a" * 64
PARENT_DIGEST = "b" * 64
CHILD = "chld_" + "c" * 32
SESSION = "sess_fictional"
DOMAIN = "talking_and_communicating"


class _Projection:
    projection_id = "pbpv2_" + "d" * 32
    child_id = CHILD
    domain = DOMAIN


class _Result:
    def __init__(self, created=True):
        self.projection = _Projection()
        self.created = created


class _Service:
    def __init__(self, result=None, error=None):
        self.result = result or _Result()
        self.error = error
        self.calls = []

    def accept(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


class _Profiles:
    def __init__(self, error=None):
        self.created = []
        self.error = error

    def create(self, profile):
        if self.error is not None:
            raise self.error
        self.created.append(profile)
        return profile


def _body(**overrides):
    body = {
        "source_session_id": SESSION,
        "source_record_digest": DIGEST,
        "summary": {"domain": DOMAIN, "area_id": "talking",
                    "entry_choice_id": "many_single_words",
                    "routing_anchor_months": 24,
                    "not_demonstrated_months": None, "status": "BOUNDED",
                    "baseline_version":
                        "parent-2.4-functional-baseline-v2"},
        "skills": [{"domain": DOMAIN, "subdomain": "expressive_language",
                    "months": 24, "milestone": "says two words together",
                    "state": "demonstrated"}],
        "band_totals": [{"months": 24, "total_skills": 1}],
        "clinician_profile": {
            "outcome": "TARGET_SUPPORTED",
            "areas": {"receptive": AREA_CONCERN_OBSERVED,
                      "social": AREA_NOT_ASSESSED},
            "not_assessed_causes": {
                "social": CAUSE_NO_AGE_APPROPRIATE_SKILL},
            "multi_area": MULTI_AREA_INDETERMINATE,
            "multi_area_indeterminate_reasons": ["no_age_appropriate_skill"],
        },
        "parent_content_evaluation": {
            "content_set_id": "talking-served-families-v1",
            "content_digest": PARENT_DIGEST,
            "target_serviceability": "supported",
        },
    }
    body.update(overrides)
    return body


def _app(service=None, profiles=None, verifier=None):
    service = service or _Service()
    profiles = profiles or _Profiles()
    app = ProjectionV21App(service_factory=lambda: service,
                           profile_repository_factory=lambda: profiles,
                           verifier=verifier)
    return app, service, profiles


def test_a_valid_v21_submission_writes_both_records():
    app, service, profiles = _app()
    status, body = _call(app, PROJECTION_V21_ROUTE, body=_body())
    assert status == 201
    assert body["projection_id"] == _Projection.projection_id
    assert body["child_id"] == CHILD
    assert body["clinician_profile_stored"] is True

    # The projection went through the UNCHANGED v2 service, with v2's own args.
    assert set(service.calls[0]) == {"source_session_id",
                                     "source_record_digest", "summary",
                                     "skills", "band_totals"}
    # The profile is a SEPARATE record.
    assert len(profiles.created) == 1
    profile = profiles.created[0]
    assert isinstance(profile, ClinicianProfileV21)
    assert profile.child_id == CHILD
    assert profile.source_record_digest == DIGEST


def test_the_content_evaluation_is_echoed_and_never_stored():
    app, service, profiles = _app()
    _status, body = _call(app, PROJECTION_V21_ROUTE, body=_body())
    assert body["parent_content_digest"] == PARENT_DIGEST
    assert body["parent_target_serviceability"] == "supported"
    # Nothing about it reached the profile record.
    stored = json.dumps(profiles.created[0].as_dict())
    assert PARENT_DIGEST not in stored
    assert "serviceability" not in stored
    assert "content_digest" not in stored


def test_a_profile_write_failure_does_not_fail_the_projection():
    app, _service, _profiles = _app(profiles=_Profiles(error=RuntimeError()))
    status, body = _call(app, PROJECTION_V21_ROUTE, body=_body())
    assert status == 201
    assert body["projection_id"]
    assert body["clinician_profile_stored"] is False


def test_a_duplicate_profile_is_treated_as_stored():
    from pilot_backend.repository.interface import DuplicateRecord

    app, _s, _p = _app(profiles=_Profiles(error=DuplicateRecord("x")))
    status, body = _call(app, PROJECTION_V21_ROUTE, body=_body())
    assert status == 201
    assert body["clinician_profile_stored"] is True


def test_a_replayed_submission_reports_created_false():
    app, _s, _p = _app(service=_Service(result=_Result(created=False)))
    status, body = _call(app, PROJECTION_V21_ROUTE, body=_body())
    assert status == 200
    assert body["created"] is False


def test_a_malformed_body_is_refused_with_a_constant_message():
    app, service, profiles = _app()
    for bad in ({"source_session_id": SESSION},
                _body(clinician_profile={"outcome": "nope"}),
                _body(parent_content_evaluation={"content_set_id": "x"})):
        status, body = _call(app, PROJECTION_V21_ROUTE, body=bad)
        assert status == 400
        assert body == {"error": "not accepted"}
    assert service.calls == [], "nothing reached the service"
    assert profiles.created == []


def test_a_wrong_method_is_refused_before_the_body():
    app, service, _p = _app()
    status, body = _call(app, PROJECTION_V21_ROUTE, method="GET")
    assert status == 405
    assert body == {"error": "method not allowed"}
    assert service.calls == []


def test_a_rejected_caller_never_reaches_the_body():
    class _Deny:
        def verify(self, _token):
            raise ValueError("no")

    app, service, _p = _app(verifier=_Deny())
    status, body = _call(app, PROJECTION_V21_ROUTE, body=_body())
    assert status == 401
    assert body == {"error": "not permitted"}
    assert service.calls == []


def test_the_response_carries_no_clinical_content():
    app, _s, _p = _app()
    _status, body = _call(app, PROJECTION_V21_ROUTE, body=_body())
    blob = json.dumps(body)
    for forbidden in ("milestone", "says", "months", "areas",
                      "concern", "multi_area", "outcome"):
        assert forbidden not in blob, forbidden


# ---------------------------------------------------------------------------
# 3. storage: a separate collection, keyed on the source identity
# ---------------------------------------------------------------------------

def test_the_profile_has_its_own_collection():
    assert "parent_clinician_profile_v21" in COLLECTIONS
    assert COLLECTIONS["parent_clinician_profile_v21"] != \
        COLLECTIONS["parent_baseline_projection_v2"]
    assert COLLECTIONS["parent_clinician_profile_v21"].endswith(
        "parent_clinician_profiles_v21")


def test_the_profile_id_is_a_deterministic_function_of_the_source():
    repo = FirestoreClinicianProfileV21Repository
    first = repo.profile_id_for(SESSION, DOMAIN, DIGEST)
    assert first == repo.profile_id_for(SESSION, DOMAIN, DIGEST)
    assert first.startswith("pcpv21_")
    # A different digest is a different document: evidence is never overwritten.
    assert first != repo.profile_id_for(SESSION, DOMAIN, "e" * 64)
    assert first != repo.profile_id_for("other", DOMAIN, DIGEST)


def test_the_repository_offers_no_update_or_delete():
    """Derived from an immutable baseline: there is no legitimate edit."""
    repo = FirestoreClinicianProfileV21Repository
    for forbidden in ("update", "set", "overwrite", "delete"):
        assert not hasattr(repo, forbidden), forbidden
    for present in ("create", "find", "list_for_child"):
        assert hasattr(repo, present), present


def test_the_projection_repository_cannot_reach_the_profile_collection():
    from pilot_backend.persistence.firestore_repos import (
        FirestoreParentBaselineProjectionV2Repository,
    )

    assert FirestoreParentBaselineProjectionV2Repository.record_type == \
        "parent_baseline_projection_v2"
    assert FirestoreClinicianProfileV21Repository.record_type == \
        "parent_clinician_profile_v21"
    assert FirestoreParentBaselineProjectionV2Repository.record_type != \
        FirestoreClinicianProfileV21Repository.record_type


# ---------------------------------------------------------------------------
# 4. the two outcomes that may be SUMMARISED but may not GENERATE
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("outcome", ["NO_TARGET_IDENTIFIED",
                                     "INSUFFICIENT_EVIDENCE"])
def test_a_no_target_outcome_is_accepted_and_summarised(outcome):
    """They reach the Pilot by design: summarisable, never generatable."""
    app, _service, profiles = _app()
    body = _body()
    body["clinician_profile"]["outcome"] = outcome
    body["parent_content_evaluation"]["target_serviceability"] = \
        "not_applicable"
    status, response = _call(app, PROJECTION_V21_ROUTE, body=body)
    assert status == 201
    assert response["clinician_profile_stored"] is True
    assert profiles.created[0].outcome == outcome


@pytest.mark.parametrize("outcome", ["NO_TARGET_IDENTIFIED",
                                     "INSUFFICIENT_EVIDENCE"])
def test_a_no_target_outcome_cannot_reach_the_chokepoint(outcome):
    """The generation service returns BEFORE calling it. Structural.

    The chokepoint is never invoked for these two, so there is nothing to
    refuse: `generate_for_child` returns `no_unresolved_band` or
    `insufficient_known_evidence` without reaching
    `generate_anchored_suggestion` at all.
    """
    import ast
    import inspect
    import textwrap

    from pilot_backend.integration.baseline_suggestion_generation_v2 import (
        BaselineSuggestionGenerationV2Service,
    )

    source = inspect.getsource(
        BaselineSuggestionGenerationV2Service.generate_for_child)
    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.Module)) and \
                ast.get_docstring(node):
            node.body = node.body[1:]
    code = ast.unparse(tree)
    marker = {"NO_TARGET_IDENTIFIED": "OUTCOME_NO_UNRESOLVED_BAND",
              "INSUFFICIENT_EVIDENCE": "OUTCOME_INSUFFICIENT_KNOWN_EVIDENCE"
              }[outcome]
    assert code.index(marker) < code.index("generate_anchored_suggestion")


def test_the_profile_outcome_is_not_an_input_to_generation():
    """The stored profile's outcome cannot authorise anything.

    `generate_for_child` re-derives the target from Channel-A band evidence and
    never reads a profile record, so a profile claiming TARGET_SUPPORTED on a
    record with no unresolved band changes nothing.
    """
    import ast
    import inspect
    import textwrap

    from pilot_backend.integration.baseline_suggestion_generation_v2 import (
        BaselineSuggestionGenerationV2Service,
    )

    source = inspect.getsource(BaselineSuggestionGenerationV2Service)
    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Module)) and \
                ast.get_docstring(node):
            node.body = node.body[1:]
    code = ast.unparse(tree)
    for forbidden in ("parent_clinician_profiles_v21", "clinician_profile",
                      "ClinicianProfileV21", "TARGET_SUPPORTED"):
        assert forbidden not in code, forbidden
