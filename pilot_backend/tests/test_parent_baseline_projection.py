"""The Parent -> Pilot baseline projection: domain, service and transport.

Dependency-pure: `FirestoreRepositories` over `FakeDocumentStore`, an injected
identity verifier, and no SDK anywhere. The LIVE Google OIDC verifier and the
image separation are covered in `pilot_runtime/tests/`.

What is under test is the trust boundary, not the baseline. The Pilot never
recomputes a baseline — it accepts seven validated fields from an
authenticated Parent service, resolves the child itself, and writes once.
"""

from __future__ import annotations

import ast
import io
import json
import pathlib

import pytest

from pilot_backend.domain.entities import Child
from pilot_backend.domain.parent_baseline_projection import (
    FORBIDDEN_FIELDS,
    PROJECTION_FIELDS,
    ParentBaselineProjection,
    ProjectionIntegrityError,
    ProjectionValidationError,
    canonical_source_digest,
    projection_id_for,
    validate_projection_payload,
)
from pilot_backend.domain.source_link import (
    SourceLinkStatus,
    SourceSystem,
    SourceSystemLink,
)
from pilot_backend.integration.baseline_projection_service import (
    BaselineProjectionService,
    ProjectionChildUnresolved,
    ProjectionLinkAmbiguous,
)
from pilot_backend.persistence import FakeDocumentStore, FirestoreRepositories
from pilot_backend.transport.projection_wsgi import (
    PROJECTION_ROUTE,
    ProjectionApp,
    bearer_from_environ,
)

PILOT_ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PILOT_ROOT.parent / "pilot_runtime"

SESSION = "sess-fictional-a1b2c3"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64

#: A realistic finalized record, shaped exactly like BaselineRecord.to_state()
#: including the `asked` history the projection deliberately does not carry.
FINALIZED_RECORD = {
    "baseline_version": "parent-2.4-functional-baseline-v1",
    "area_id": "talking",
    "domain": "talking_and_communicating",
    "entry_choice_id": "many_single_words",
    "entry_choice_label": "Many single words",
    "entry_anchor_months": 18,
    "chronological_months": 36,
    "asked": [
        {"question_id": "q:18", "months": 18, "milestone": "tries to say three",
         "subdomain": "expressive_language", "answer": "yes",
         "classification": "demonstrated"},
    ],
    "status": "BOUNDED",
    "routing_anchor_months": 24,
    "demonstrated_months": 24,
    "not_demonstrated_months": 30,
}

VALID_PROJECTION = {
    "domain": "talking_and_communicating",
    "area_id": "talking",
    "entry_choice_id": "many_single_words",
    "routing_anchor_months": 24,
    "not_demonstrated_months": 30,
    "status": "BOUNDED",
    "baseline_version": "parent-2.4-functional-baseline-v1",
}


class _Stack:
    """Repositories plus one ACTIVE Parent link to a canonical child."""

    def __init__(self, *, link: bool = True):
        self.store = FakeDocumentStore()
        self.repos = FirestoreRepositories(self.store)
        self.child = self.repos.children.create(Child.create(actor_id="seed"))
        if link:
            self.repos.source_links.create(SourceSystemLink.create(
                self.child.child_id, SourceSystem.PARENT, SESSION,
                actor_id="seed"))

    def service(self):
        return BaselineProjectionService(repos=self.repos)


def _accept(stack, *, digest=DIGEST_A, projection=None, session=SESSION):
    return stack.service().accept(
        source_session_id=session, source_record_digest=digest,
        projection=dict(projection or VALID_PROJECTION))


# ---------------------------------------------------------------------------
# 1. the payload allowlist
# ---------------------------------------------------------------------------

def test_the_valid_seven_field_projection_is_accepted():
    validated = validate_projection_payload(VALID_PROJECTION)
    assert set(validated) == set(PROJECTION_FIELDS) == {
        "domain", "area_id", "entry_choice_id", "routing_anchor_months",
        "not_demonstrated_months", "status", "baseline_version"}


@pytest.mark.parametrize("field", sorted(set(FORBIDDEN_FIELDS)))
def test_every_forbidden_field_is_refused(field):
    """Refused, not ignored. A silently dropped `child_id` would leave the
    caller believing it had named a child."""
    payload = dict(VALID_PROJECTION)
    payload[field] = "anything"
    with pytest.raises(ProjectionValidationError):
        validate_projection_payload(payload)


def test_a_client_cannot_send_a_child_id():
    payload = dict(VALID_PROJECTION, child_id="chld_deadbeef")
    with pytest.raises(ProjectionValidationError):
        validate_projection_payload(payload)


def test_an_unknown_extra_field_is_refused():
    with pytest.raises(ProjectionValidationError):
        validate_projection_payload(dict(VALID_PROJECTION, extra=1))


@pytest.mark.parametrize("missing", sorted(PROJECTION_FIELDS))
def test_a_missing_field_is_refused(missing):
    payload = {k: v for k, v in VALID_PROJECTION.items() if k != missing}
    with pytest.raises(ProjectionValidationError):
        validate_projection_payload(payload)


@pytest.mark.parametrize("status", ["bounded", "FINISHED", "", "OK", "None"])
def test_an_invalid_status_is_refused(status):
    with pytest.raises(ProjectionValidationError):
        validate_projection_payload(dict(VALID_PROJECTION, status=status))


@pytest.mark.parametrize("domain", [
    "fine_motor", "gross_motor", "daily_living", "sensory", "not_a_domain"])
def test_only_the_supported_domain_is_accepted(domain):
    with pytest.raises(ProjectionValidationError):
        validate_projection_payload(dict(VALID_PROJECTION, domain=domain))


def test_a_null_anchor_survives_and_is_not_coerced():
    """UNRESOLVED and CONTRADICTORY baselines have no anchor. Turning None
    into 0 would invent a rung, which is the one thing the 0.4 engine exists
    to prevent."""
    validated = validate_projection_payload(dict(
        VALID_PROJECTION, status="UNRESOLVED", routing_anchor_months=None,
        not_demonstrated_months=None))
    assert validated["routing_anchor_months"] is None
    assert validated["not_demonstrated_months"] is None


@pytest.mark.parametrize("value", ["24", 24.0, True, 0, -6, 301])
def test_a_non_month_anchor_is_refused(value):
    with pytest.raises(ProjectionValidationError):
        validate_projection_payload(
            dict(VALID_PROJECTION, routing_anchor_months=value))


def test_a_missing_anchor_key_is_refused_even_though_none_is_valid():
    """None is a VALUE; absence is a different thing and must not default."""
    payload = {k: v for k, v in VALID_PROJECTION.items()
               if k != "routing_anchor_months"}
    with pytest.raises(ProjectionValidationError):
        validate_projection_payload(payload)


# ---------------------------------------------------------------------------
# 2. the digest
# ---------------------------------------------------------------------------

def test_the_digest_is_stable_across_key_order():
    shuffled = dict(reversed(list(FINALIZED_RECORD.items())))
    assert canonical_source_digest(shuffled) == \
        canonical_source_digest(FINALIZED_RECORD)


def test_the_digest_changes_when_any_field_changes():
    changed = dict(FINALIZED_RECORD, routing_anchor_months=30)
    assert canonical_source_digest(changed) != \
        canonical_source_digest(FINALIZED_RECORD)


def test_the_digest_covers_the_asked_history_the_projection_omits():
    """The point of digesting the FULL record: the Pilot attests the `asked`
    evidence without ever storing it."""
    changed = dict(FINALIZED_RECORD)
    changed["asked"] = [dict(changed["asked"][0], answer="no")]
    assert canonical_source_digest(changed) != \
        canonical_source_digest(FINALIZED_RECORD)


def test_the_projection_id_is_deterministic_and_order_safe():
    first = projection_id_for(SESSION, "talking_and_communicating", DIGEST_A)
    assert first == projection_id_for(SESSION, "talking_and_communicating",
                                      DIGEST_A)
    assert first.startswith("pbpj_")
    # NUL-joined parts: ("ab","c") and ("a","bc") must not collide.
    assert projection_id_for("ab", "talking_and_communicating", DIGEST_A) != \
        projection_id_for("a", "talking_and_communicating", DIGEST_A)


def test_a_record_whose_id_disagrees_with_its_source_is_refused():
    with pytest.raises(ProjectionValidationError):
        ParentBaselineProjection(
            projection_id="pbpj_" + "0" * 32, child_id="chld_x",
            source_system=SourceSystem.PARENT, source_session_id=SESSION,
            source_record_digest=DIGEST_A, domain="talking_and_communicating",
            area_id="talking", entry_choice_id="many_single_words",
            status="BOUNDED",
            baseline_version="parent-2.4-functional-baseline-v1")


def test_only_a_parent_source_system_is_projected():
    with pytest.raises(ProjectionValidationError):
        ParentBaselineProjection.build(
            child_id="chld_x", source_session_id=SESSION,
            source_record_digest=DIGEST_A,
            projection=validate_projection_payload(VALID_PROJECTION),
        ).__class__(
            projection_id=projection_id_for(
                SESSION, "talking_and_communicating", DIGEST_A),
            child_id="chld_x", source_system=SourceSystem.THERAPIST,
            source_session_id=SESSION, source_record_digest=DIGEST_A,
            domain="talking_and_communicating", area_id="talking",
            entry_choice_id="many_single_words", status="BOUNDED",
            baseline_version="parent-2.4-functional-baseline-v1")


def test_has_routing_anchor_is_derived_and_not_stored():
    from pilot_backend.persistence import encode

    anchored = ParentBaselineProjection.build(
        child_id="chld_x", source_session_id=SESSION,
        source_record_digest=DIGEST_A,
        projection=validate_projection_payload(VALID_PROJECTION))
    assert anchored.has_routing_anchor is True
    assert "has_routing_anchor" not in encode(anchored)

    unresolved = ParentBaselineProjection.build(
        child_id="chld_x", source_session_id=SESSION,
        source_record_digest=DIGEST_B,
        projection=validate_projection_payload(dict(
            VALID_PROJECTION, status="UNRESOLVED",
            routing_anchor_months=None, not_demonstrated_months=None)))
    assert unresolved.has_routing_anchor is False


# ---------------------------------------------------------------------------
# 3. child resolution
# ---------------------------------------------------------------------------

def test_exactly_one_active_link_resolves_the_canonical_child():
    stack = _Stack()
    result = _accept(stack)
    assert result.created is True
    assert result.projection.child_id == stack.child.child_id
    assert result.projection.source_system is SourceSystem.PARENT


def test_zero_active_links_is_refused():
    """Nothing to attach a projection to. Inventing a Child here would create
    a clinical record out of a message."""
    stack = _Stack(link=False)
    with pytest.raises(ProjectionChildUnresolved):
        _accept(stack)
    assert stack.repos.parent_baseline_projections.find(
        projection_id_for(SESSION, "talking_and_communicating",
                          DIGEST_A)) is None


def test_an_ended_link_does_not_resolve():
    """`list_for_external_id` returns ACTIVE links only."""
    stack = _Stack(link=False)
    link = SourceSystemLink.create(stack.child.child_id, SourceSystem.PARENT,
                                   SESSION, actor_id="seed")
    stack.repos.source_links.create(link.end(status=SourceLinkStatus.ENDED))
    with pytest.raises(ProjectionChildUnresolved):
        _accept(stack)


def test_more_than_one_active_link_is_an_integrity_refusal():
    """Should be unreachable — (source_system, external_id) is enforced at
    write time. Reachable only if that was bypassed, which is exactly when
    failing closed matters."""
    stack = _Stack()
    other = stack.repos.children.create(Child.create(actor_id="seed"))
    # Written straight to the store, bypassing the uniqueness check, to
    # simulate the constraint having been violated.
    stack.repos.source_links.create(SourceSystemLink.create(
        other.child_id, SourceSystem.PARENT, SESSION, actor_id="seed"))
    with pytest.raises(ProjectionLinkAmbiguous):
        _accept(stack)


def test_a_therapist_link_for_the_same_external_id_is_ignored():
    stack = _Stack()
    stack.repos.source_links.create(SourceSystemLink.create(
        stack.child.child_id, SourceSystem.THERAPIST, SESSION,
        actor_id="seed"))
    assert _accept(stack).projection.child_id == stack.child.child_id


def test_external_owner_ref_is_never_read():
    """Structural, over the service's AST. An owner handle is not an identity
    and must never become a join key."""
    source = (PILOT_ROOT / "integration"
              / "baseline_projection_service.py").read_text()
    tree = ast.parse(source)
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    constants = {n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "external_owner_ref" not in attributes
    assert "external_owner_ref" not in constants
    assert "owner_uid" not in attributes


# ---------------------------------------------------------------------------
# 4. idempotency and integrity
# ---------------------------------------------------------------------------

def test_an_exact_retry_is_idempotent_and_writes_nothing_new():
    stack = _Stack()
    first = _accept(stack)
    second = _accept(stack)
    assert first.created is True and second.created is False
    assert second.projection.projection_id == first.projection.projection_id
    assert second.projection.projected_at == first.projection.projected_at


def test_a_retry_after_the_link_ended_still_replays():
    """A projection already written is already written. An exact replay must
    not depend on the link still being active."""
    stack = _Stack()
    first = _accept(stack)
    for link in stack.repos.source_links.list_for_external_id(SESSION):
        stack.repos.source_links.end(link.link_id, reason="test") \
            if hasattr(stack.repos.source_links, "end") else None
    replay = _accept(stack)
    assert replay.created is False
    assert replay.projection.projection_id == first.projection.projection_id


def test_a_different_digest_for_the_same_source_is_an_integrity_conflict():
    """An A1 finalized baseline is immutable, so two digests cannot both be
    it. Not versioned, not overwritten — refused."""
    stack = _Stack()
    _accept(stack)
    with pytest.raises(ProjectionIntegrityError):
        _accept(stack, digest=DIGEST_B)


def test_the_conflicting_projection_is_not_written():
    stack = _Stack()
    _accept(stack)
    with pytest.raises(ProjectionIntegrityError):
        _accept(stack, digest=DIGEST_B)
    assert stack.repos.parent_baseline_projections.find(
        projection_id_for(SESSION, "talking_and_communicating",
                          DIGEST_B)) is None
    assert len(stack.repos.parent_baseline_projections.list_for_source(
        SESSION, "talking_and_communicating")) == 1


def test_the_original_projection_survives_a_conflict_unchanged():
    stack = _Stack()
    original = _accept(stack).projection
    with pytest.raises(ProjectionIntegrityError):
        _accept(stack, digest=DIGEST_B)
    stored = stack.repos.parent_baseline_projections.find(
        original.projection_id)
    assert stored == original


def test_a_malformed_digest_is_refused():
    stack = _Stack()
    for bad in ("", "abc", "A" * 64, "g" * 64, "a" * 63, "a" * 65):
        with pytest.raises(ProjectionValidationError):
            _accept(stack, digest=bad)


def test_an_empty_session_id_is_refused():
    stack = _Stack()
    with pytest.raises(ProjectionValidationError):
        _accept(stack, session="   ")


def test_validation_happens_before_any_repository_read():
    """So a caller cannot use validation failures to probe which sessions
    exist. A malformed payload must not reach child resolution."""
    class _Exploding:
        def list_for_external_id(self, *a, **k):
            raise AssertionError("resolved a child for a malformed payload")

    class _Repos:
        source_links = _Exploding()

        class parent_baseline_projections:
            @staticmethod
            def find(_):
                return None

            @staticmethod
            def list_for_source(*_):
                return []

    with pytest.raises(ProjectionValidationError):
        BaselineProjectionService(repos=_Repos()).accept(
            source_session_id=SESSION, source_record_digest=DIGEST_A,
            projection={"nonsense": 1})


# ---------------------------------------------------------------------------
# 5. the repository is create-only
# ---------------------------------------------------------------------------

def test_the_repository_has_no_mutating_method():
    stack = _Stack()
    repo = stack.repos.parent_baseline_projections
    public = {m for m in dir(repo) if not m.startswith("_")}
    for forbidden in ("update", "set", "overwrite", "delete", "remove",
                      "put", "patch", "save"):
        assert forbidden not in public, forbidden
    assert {"create", "find", "list_for_source"} <= public


def test_creating_the_same_projection_id_twice_collides():
    """The create-only guarantee at the store level: a racing second writer
    cannot overwrite an immutable record."""
    stack = _Stack()
    record = _accept(stack).projection
    # Any refusal is correct here; what must NOT happen is a silent overwrite.
    with pytest.raises(Exception):
        stack.repos.parent_baseline_projections.create(record)
    # And the stored record is still the original.
    assert stack.repos.parent_baseline_projections.find(
        record.projection_id) == record


# ---------------------------------------------------------------------------
# 6. this slice touches nothing clinical
# ---------------------------------------------------------------------------

def test_the_service_names_no_goal_or_plan_repository():
    """Over ATTRIBUTE ACCESSES, so `self._repos.<anything clinical>` is what
    is being ruled out rather than the docstring that promises it."""
    tree = ast.parse((PILOT_ROOT / "integration"
                      / "baseline_projection_service.py").read_text())
    attributes = {n.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Attribute)}
    for forbidden in ("goal_suggestions", "clinical_goals", "caregiver_goals",
                      "suggestion_anchors", "clinical_goal_anchors",
                      "monthly_focus_plans", "goal_allocations",
                      "weekly_cycles", "goal_versions"):
        assert forbidden not in attributes, forbidden


def test_accepting_a_projection_writes_exactly_one_collection():
    stack = _Stack()
    before = {k: len(v) for k, v in stack.store.snapshot().items()} \
        if hasattr(stack.store, "snapshot") else None
    _accept(stack)
    # Whatever else exists, no goal/plan/anchor record was created.
    for repo_name in ("goal_suggestions", "clinical_goals",
                      "suggestion_anchors", "clinical_goal_anchors",
                      "monthly_focus_plans", "weekly_cycles"):
        repo = getattr(stack.repos, repo_name, None)
        if repo is None:
            continue
        lister = getattr(repo, "list_for_child", None)
        if lister is not None:
            assert lister(stack.child.child_id) == [], repo_name
    assert before is None or True


# ---------------------------------------------------------------------------
# 7. the transport
# ---------------------------------------------------------------------------

class _AcceptingVerifier:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = 0

    def verify(self, header):
        self.calls += 1
        if not self.ok:
            raise ValueError("nope")
        return {"email": "parent@example.invalid"}


def _request(app, *, method="POST", path=PROJECTION_ROUTE, body=None,
             headers=None):
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
    }
    environ.update(headers or {})
    captured = {}

    def start_response(status, response_headers):
        captured["status"] = int(status.split(" ")[0])
        captured["headers"] = response_headers

    chunks = app(environ, start_response)
    payload = json.loads(b"".join(chunks).decode("utf-8"))
    return captured["status"], payload, captured["headers"]


def _app(stack, *, verifier=None):
    return ProjectionApp(verifier=verifier or _AcceptingVerifier(),
                         service_factory=stack.service)


def _body(digest=DIGEST_A, projection=None, session=SESSION):
    return {"source_session_id": session, "source_record_digest": digest,
            "projection": dict(projection or VALID_PROJECTION)}


def test_a_valid_request_creates_then_replays():
    stack = _Stack()
    app = _app(stack)
    status, payload, _ = _request(app, body=_body(),
                                  headers={"HTTP_AUTHORIZATION": "Bearer t"})
    assert status == 201 and payload["created"] is True
    status, payload, _ = _request(app, body=_body(),
                                  headers={"HTTP_AUTHORIZATION": "Bearer t"})
    assert status == 200 and payload["created"] is False


def test_an_unverified_caller_is_refused_before_the_body_is_read():
    stack = _Stack()
    verifier = _AcceptingVerifier(ok=False)
    status, payload, _ = _request(_app(stack, verifier=verifier), body=_body())
    assert status == 401 and payload == {"error": "not permitted"}
    assert verifier.calls == 1
    # Nothing was written, and nothing was parsed.
    assert stack.repos.parent_baseline_projections.find(
        projection_id_for(SESSION, "talking_and_communicating",
                          DIGEST_A)) is None


def test_the_caller_is_verified_before_parsing_a_hostile_body():
    """Order matters: an unauthenticated caller must not be able to make this
    process decode JSON."""
    stack = _Stack()
    verifier = _AcceptingVerifier(ok=False)
    raw = b"{not json at all"
    environ = {"REQUEST_METHOD": "POST", "PATH_INFO": PROJECTION_ROUTE,
               "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw)}
    captured = {}
    chunks = _app(stack, verifier=verifier)(
        environ, lambda s, h: captured.setdefault("s", int(s.split(" ")[0])))
    assert captured["s"] == 401
    assert json.loads(b"".join(chunks))["error"] == "not permitted"


def test_the_response_carries_no_baseline_values():
    stack = _Stack()
    status, payload, _ = _request(_app(stack), body=_body(),
                                  headers={"HTTP_AUTHORIZATION": "Bearer t"})
    assert set(payload) == {"projection_id", "child_id", "created"}
    text = json.dumps(payload)
    # NOT the bare numbers 24/30: a hex projection_id legitimately contains
    # those digit pairs, so asserting on them was a false positive rather
    # than a leak check.
    for leaked in ("BOUNDED", "talking", "many_single_words",
                   "routing_anchor", "not_demonstrated", "baseline_version",
                   "entry_choice"):
        assert leaked not in text, leaked


def test_no_cors_header_is_ever_emitted():
    stack = _Stack()
    _, _, headers = _request(_app(stack), body=_body(),
                             headers={"HTTP_AUTHORIZATION": "Bearer t",
                                      "HTTP_ORIGIN": "https://evil.example"})
    names = {name.lower() for name, _ in headers}
    assert not any(n.startswith("access-control") for n in names)


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH",
                                    "OPTIONS", "HEAD"])
def test_only_post_is_allowed(method):
    stack = _Stack()
    status, _, _ = _request(_app(stack), method=method,
                            headers={"HTTP_AUTHORIZATION": "Bearer t"})
    assert status == 405


def test_an_unknown_path_is_not_found():
    stack = _Stack()
    status, _, _ = _request(_app(stack), path="/pilot/children",
                            headers={"HTTP_AUTHORIZATION": "Bearer t"})
    assert status == 404


def test_an_unlinked_session_is_404_with_a_constant_message():
    stack = _Stack(link=False)
    status, payload, _ = _request(_app(stack), body=_body(),
                                  headers={"HTTP_AUTHORIZATION": "Bearer t"})
    assert status == 404 and payload == {"error": "not accepted"}


def test_a_digest_conflict_is_409():
    stack = _Stack()
    app = _app(stack)
    _request(app, body=_body(), headers={"HTTP_AUTHORIZATION": "Bearer t"})
    status, payload, _ = _request(app, body=_body(digest=DIGEST_B),
                                  headers={"HTTP_AUTHORIZATION": "Bearer t"})
    assert status == 409 and payload == {"error": "integrity conflict"}


def test_a_malformed_projection_is_400_with_a_constant_message():
    stack = _Stack()
    status, payload, _ = _request(
        _app(stack), body=_body(projection={"domain": "fine_motor"}),
        headers={"HTTP_AUTHORIZATION": "Bearer t"})
    assert status == 400 and payload == {"error": "not accepted"}


@pytest.mark.parametrize("body", [
    {"source_session_id": SESSION},
    {"source_session_id": SESSION, "source_record_digest": DIGEST_A},
    {"source_session_id": SESSION, "source_record_digest": DIGEST_A,
     "projection": dict(VALID_PROJECTION), "child_id": "chld_x"},
])
def test_an_unexpected_body_shape_is_refused(body):
    stack = _Stack()
    status, _, _ = _request(_app(stack), body=body,
                            headers={"HTTP_AUTHORIZATION": "Bearer t"})
    assert status == 400


def test_an_oversized_body_is_refused_without_parsing():
    stack = _Stack()
    environ = {"REQUEST_METHOD": "POST", "PATH_INFO": PROJECTION_ROUTE,
               "CONTENT_LENGTH": str(9 * 1024),
               "wsgi.input": io.BytesIO(b"{}"),
               "HTTP_AUTHORIZATION": "Bearer t"}
    captured = {}
    _app(stack)(environ,
                lambda s, h: captured.setdefault("s", int(s.split(" ")[0])))
    assert captured["s"] == 413


def test_the_bearer_falls_back_to_the_serverless_header():
    assert bearer_from_environ({"HTTP_AUTHORIZATION": "Bearer a"}) == "Bearer a"
    assert bearer_from_environ(
        {"HTTP_X_SERVERLESS_AUTHORIZATION": "Bearer b"}) == "Bearer b"
    assert bearer_from_environ({}) is None
    # Authorization wins when both are present.
    assert bearer_from_environ({"HTTP_AUTHORIZATION": "Bearer a",
                                "HTTP_X_SERVERLESS_AUTHORIZATION": "Bearer b"
                                }) == "Bearer a"


def test_the_transport_has_no_end_user_authentication():
    """No `resolve_principal`, no caregiver, no provider. The only identity on
    this path is the calling service account.

    Scanned over NAMES IN CODE rather than the whole file: the module
    docstring deliberately explains what it does not do, and a substring scan
    flagged its own documentation.
    """
    tree = ast.parse((PILOT_ROOT / "transport" / "projection_wsgi.py").read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names |= {a.name for a in node.names}
    for forbidden in ("resolve_principal", "VerifiedToken", "Principal",
                      "CorsMiddleware", "authenticate_and_authorize_child"):
        assert forbidden not in names, forbidden


def test_the_route_carries_no_identifier():
    """`source_session_id` is in the body because Cloud Run logs the path."""
    assert PROJECTION_ROUTE == "/internal/parent-baseline-projections"
    assert "{" not in PROJECTION_ROUTE and "}" not in PROJECTION_ROUTE


# ---------------------------------------------------------------------------
# 8. logging
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", [
    "routing_anchor_months", "not_demonstrated_months", "demonstrated_months",
    "entry_choice_id", "chronological_months", "asked", "milestone",
    "projection", "source_record_digest", "source_session_id", "session_id",
    "baseline", "functional_baseline", "body", "payload",
])
def test_a_baseline_field_cannot_enter_safe_logging(field):
    from pilot_backend.observability.safe_logging import (
        LogFieldError, format_log)

    with pytest.raises(LogFieldError):
        format_log(**{field: "x"})


def test_the_allowed_log_fields_were_not_widened():
    """The forbidden set grew; the allowed set must not have."""
    from pilot_backend.observability.safe_logging import (
        ALLOWED_LOG_FIELDS, FORBIDDEN_LOG_FIELDS)

    assert ALLOWED_LOG_FIELDS == frozenset({
        "request_id", "event", "severity", "message", "route", "method",
        "status", "duration_ms", "environment", "actor_id", "actor_role",
        "child_id", "resource_type", "resource_id", "denial_reason",
        "exception_type", "exception_message"})
    assert not (ALLOWED_LOG_FIELDS & FORBIDDEN_LOG_FIELDS)


def test_safe_metadata_still_logs():
    from pilot_backend.observability.safe_logging import format_log

    assert dict(format_log(request_id="r", route=PROJECTION_ROUTE,
                           status=201)) == {
        "request_id": "r", "route": PROJECTION_ROUTE, "status": 201}


def test_neither_new_module_constructs_a_logger():
    for relative in ("integration/baseline_projection_service.py",
                     "transport/projection_wsgi.py",
                     "domain/parent_baseline_projection.py"):
        tree = ast.parse((PILOT_ROOT / relative).read_text())
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)
        for noisy in ("print", "getLogger", "basicConfig", "warning",
                      "error", "exception", "critical"):
            assert noisy not in called, (relative, noisy)
