"""Runs INSIDE the built pilot-api image. 0.5F-B Option C container proof.

Not a pytest module — it is piped into the container's own interpreter:

    docker run -i --network host --entrypoint python pilot-api:ci - \\
        < pilot_runtime/tests/container_generation_proof.py

## Why it is piped in rather than baked in

The image deliberately deletes `pilot_backend/tests`, `pilot_runtime/tests` and
`pilot_backend/fixtures`, so nothing here can live in the artifact under test.
Piping the harness to the image's interpreter keeps the IMAGE unmodified while
still executing inside it — the distinction the founder asked for, since the
0.5F-B inspection showed that source-tree proofs can pass while the image
cannot serve a single request.

## What is real and what is a harness

REAL, from the image: the composition root, `StaticRungTableSource` over the
committed artifact, the WSGI application, the route table, the authorization
chain, `GoalService`, the generation transaction, the Firestore adapter.

HARNESS, supplied here: the fictional records, and one fictional bearer token
registered on the image's OWN `DevAuthVerifier` (the mechanism dev auth exists
for). No clinical component is substituted — in particular NO in-memory rung
source, which is the thing this proof exists to rule out.

## Persistence

Against the real Firestore emulator when `FIRESTORE_EMULATOR_HOST` is set,
which is the hosted-CI path. Falls back to in-memory only when explicitly asked
via `PROOF_IN_MEMORY=1`, and says which it used in its output, so a weaker run
can never be mistaken for the stronger one.
"""

from __future__ import annotations

import io
import json
import os
import sys
import uuid

EXPECTED = {
    "target_months": 24,
    "rung_ref": "rung1:feb590cf2788978b383c11062ce67c1b",
    "track_ref": "track1:5be892494f3e6894a7de24a868084e0f",
    "milestone": "says at least two words together like more milk",
    "subdomain": "expressive_language",
    "families": ["expressive_vocabulary_growth", "two_word_phrases"],
}
ARTIFACT_DIGEST = \
    "609fff91ba98b9afbf09d6d1c64cee23c59650d0bd3e8e53841083b1ab95b437"
DOMAIN = "talking_and_communicating"
FLOOR = 18


def fail(message: str) -> None:
    print(f"PROOF FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def call(app, path, *, method="GET", bearer=None, body=b""):
    """A minimal WSGI client. Inlined because the image has no test helpers."""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "8080",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body),
        "CONTENT_LENGTH": str(len(body)),
    }
    if bearer:
        environ["HTTP_AUTHORIZATION"] = bearer
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = int(status.split()[0])
        captured["headers"] = headers

    chunks = app(environ, start_response)
    raw = b"".join(chunks)
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        parsed = raw.decode("utf-8", "replace")
    return captured.get("status"), parsed


def main() -> int:
    emulator = (os.environ.get("FIRESTORE_EMULATOR_HOST") or "").strip()
    in_memory = os.environ.get("PROOF_IN_MEMORY") == "1"
    if not emulator and not in_memory:
        fail("neither FIRESTORE_EMULATOR_HOST nor PROOF_IN_MEMORY is set; "
             "refusing to guess a persistence backend")
    backend = "in-memory" if in_memory else f"firestore-emulator {emulator}"

    # -- 1. the image's own composition ---------------------------------
    from pilot_runtime.composition import build_runtime
    from pilot_runtime.integration.static_rung_source import (
        StaticRungTableSource,
    )

    env = {
        "PILOT_ENVIRONMENT": "dev",
        "PILOT_DEV_AUTH_ENABLED": "true",
        "PILOT_GCP_PROJECT_ID": "pilot-container-proof",
        "PILOT_ALLOWED_ORIGINS": "http://localhost:3000",
    }
    if emulator and not in_memory:
        env["FIRESTORE_EMULATOR_HOST"] = emulator
    runtime = build_runtime(env, in_memory=in_memory)

    if runtime.rung_source is None:
        fail("the built image composed rung_source = None")
    if type(runtime.rung_source) is not StaticRungTableSource:
        fail(f"rung_source is {type(runtime.rung_source).__name__}, "
             "not StaticRungTableSource")
    if runtime.rung_source.artifact_digest != ARTIFACT_DIGEST:
        fail(f"artifact digest {runtime.rung_source.artifact_digest} "
             f"!= committed {ARTIFACT_DIGEST}")
    if runtime.application._rung_source is not runtime.rung_source:
        fail("the WSGI application does not hold the composed rung source")

    # -- 2. the clinical lookup, from the artifact in the image ----------
    target = runtime.rung_source.next_rung_target(DOMAIN, FLOOR)
    if target is None:
        fail("the image's rung source returned no target for floor 18")
    rung = runtime.rung_source.rung_for_target(target)
    lookup = {
        "target_months": rung.source_rung_months,
        "rung_ref": rung.rung_ref,
        "track_ref": rung.track_ref,
        "milestone": rung.milestone_text,
        "subdomain": rung.subdomain,
        "families": list(rung.activity_family_refs),
    }
    if lookup != EXPECTED:
        fail(f"lookup mismatch\n  got      {lookup}\n  expected {EXPECTED}")
    if not rung.is_activity_mappable:
        fail("the resolved rung is not activity-mappable")

    # -- 3. seed a fictional world --------------------------------------
    from pilot_backend.auth.verifiers import VerifiedToken
    from pilot_backend.domain.connections import (
        CaregiverChildConnection,
        ProviderChildConnection,
    )
    from pilot_backend.domain.entities import (
        Caregiver,
        Child,
        Practice,
        Provider,
        utc_now,
    )
    from pilot_backend.domain.enums import (
        CaregiverRelationship,
        ProviderDiscipline,
    )
    from pilot_backend.domain.managing_clinician import (
        ManagingClinicianAssignment,
    )
    from pilot_backend.domain.parent_baseline_projection import (
        ParentBaselineProjection,
    )
    from pilot_backend.domain.roles import ActorRole
    from pilot_backend.domain.source_link import SourceSystem, SourceSystemLink

    repos = runtime.repos
    now = utc_now()
    # Unique per run so repeated hosted runs against one emulator cannot
    # collide on a deterministic id and report a false duplicate.
    nonce = uuid.uuid4().hex
    child_id = f"chld_{nonce}"
    session_id = f"sess-container-proof-{nonce}"

    repos.children.create(Child(child_id=child_id, created_at=now,
                                updated_at=now))
    caregiver = Caregiver.create("Fictional Caregiver",
                                 auth_subject=f"cg-{nonce}", now=now)
    repos.caregivers.create(caregiver)
    repos.caregiver_child.connect(CaregiverChildConnection.create(
        caregiver.caregiver_id, child_id, CaregiverRelationship.PARENT,
        actor_id=caregiver.caregiver_id, now=now))

    practice = Practice.create("Fictional Practice", now=now)
    repos.practices.create(practice)
    provider = Provider.create(practice.practice_id, ProviderDiscipline.SLP,
                               "Hannah", auth_subject=f"pr-{nonce}", now=now)
    repos.providers.create(provider)
    connection = ProviderChildConnection.create(
        provider.provider_id, child_id, practice.practice_id,
        actor_id=caregiver.caregiver_id, now=now).activate(now=now)
    repos.provider_child.connect(connection)
    repos.managing_clinicians.create(ManagingClinicianAssignment.create(
        child_id, provider.provider_id, practice.practice_id,
        provider_connection_id=connection.connection_id,
        actor_id=caregiver.caregiver_id, now=now))

    repos.source_links.create(SourceSystemLink.create(
        child_id, SourceSystem.PARENT, session_id, actor_id="proof",
        actor_role=ActorRole.CAREGIVER.value, now=now))
    projection = ParentBaselineProjection.build(
        child_id=child_id, source_session_id=session_id,
        source_record_digest=("%064x" % int(nonce, 16))[:64],
        projection={"domain": DOMAIN, "area_id": "talking",
                    "entry_choice_id": "many_single_words",
                    "routing_anchor_months": FLOOR,
                    "not_demonstrated_months": 24,
                    "status": "BOUNDED",
                    "baseline_version": "parent-2.4-functional-baseline-v1"},
        now=now)
    repos.parent_baseline_projections.create(projection)

    # One fictional token on the image's OWN verifier.
    token = f"token-proof-{nonce}"
    runtime.verifier.add(token, VerifiedToken(
        subject=f"pr-{nonce}", email="hannah@fictional.invalid"))

    # -- 4. the route, twice --------------------------------------------
    path = f"/pilot/children/{child_id}/goal-suggestions/generate"
    first_status, first = call(runtime.application, path, method="POST",
                               bearer=f"Bearer {token}")
    if first_status != 200:
        fail(f"first POST returned {first_status}: {first}")
    if first.get("created") is not True:
        fail(f"first POST created={first.get('created')}, expected True")
    if first.get("target_rung_months") != 24:
        fail(f"first POST target months {first.get('target_rung_months')}")
    if first.get("target_rung_ref") != EXPECTED["rung_ref"]:
        fail(f"first POST rung ref {first.get('target_rung_ref')}")
    if len(first.get("suggestion_ids") or []) != 1:
        fail(f"first POST produced {first.get('suggestion_ids')}")

    second_status, second = call(runtime.application, path, method="POST",
                                 bearer=f"Bearer {token}")
    if second_status != 200:
        fail(f"second POST returned {second_status}: {second}")
    if second.get("created") is not False:
        fail(f"second POST created={second.get('created')}, expected False")
    if second.get("suggestion_ids") != first.get("suggestion_ids"):
        fail("the replay resolved to different suggestions")

    # -- 5. the records, counted for THIS child -------------------------
    suggestions = repos.goal_suggestions.list_for_child(child_id)
    anchors = [a for a in (repos.suggestion_anchors.find(s.suggestion_id)
                           for s in suggestions) if a is not None]
    # The claim is looked up by its DETERMINISTIC id, recomputed here from the
    # clinical inputs. That is a stronger check than listing the collection:
    # it proves the id the route committed is the one the key derivation
    # predicts, so the requester cannot have entered into it.
    from pilot_backend.domain.suggestion_generation import (
        generation_claim_id,
        generation_key,
    )

    claim_id = generation_claim_id(generation_key(
        projection_id=first["projection_id"],
        domain_key=DOMAIN,
        target_rung_ref=EXPECTED["rung_ref"],
        taxonomy_version=rung.taxonomy_version,
        gold_standard_version=rung.baseline_version))
    claims = repos.suggestion_generation_claims.find(claim_id)
    goals = repos.clinical_goals.list_for_child(child_id)

    counts = {
        "suggestions": len(suggestions),
        "anchors": len(anchors),
        "claims": 1 if claims is not None else 0,
        "clinical_goals": len(goals),
    }
    if counts != {"suggestions": 1, "anchors": 1, "claims": 1,
                  "clinical_goals": 0}:
        fail(f"record counts {counts}")

    anchor = anchors[0]
    if anchor.rung.rung_ref != EXPECTED["rung_ref"]:
        fail(f"anchor rung_ref {anchor.rung.rung_ref}")
    if anchor.rung.track_ref != EXPECTED["track_ref"]:
        fail(f"anchor track_ref {anchor.rung.track_ref}")

    # -- 6. the lineage correction, asserted in the container -----------
    suggestion = suggestions[0]
    if suggestion.evidence.prior_month_summary_id is not None:
        fail("prior_month_summary_id is populated on a FIRST baseline; it "
             "would falsely add prior-cycle continuity to the evidence score")
    if suggestion.suggestion_id not in tuple(claims.suggestion_ids):
        fail("the generation claim does not name the suggestion it produced")
    if claims.projection_id != projection.projection_id:
        fail("the generation claim does not name the projection")

    print(json.dumps({
        "backend": backend,
        "composition": {
            "rung_source": type(runtime.rung_source).__name__,
            "is_none": False,
            "artifact_digest": runtime.rung_source.artifact_digest,
        },
        "lookup": lookup,
        "mappable": True,
        "http": {
            "first": {"status": first_status, "created": True,
                      "target_rung_months": first["target_rung_months"]},
            "second": {"status": second_status, "created": False},
        },
        "records": counts,
        "lineage": {
            "prior_month_summary_id": None,
            "claim_projection_id": claims.projection_id,
            "claim_suggestion_ids": list(claims.suggestion_ids),
            "anchor_rung_ref": anchor.rung.rung_ref,
        },
    }, indent=2, sort_keys=True))
    print("CONTAINER PROOF PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
