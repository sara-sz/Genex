"""0.6A-2 — the whole chain through the REAL composed routes, on the emulator.

Written because the `BodyError`/`TransportError` bug lived ONLY in the handler's
exception path: every service-level test passed while the route returned 500.
So this file calls nothing directly — every step is an HTTP request against the
composed WSGI application, with Firestore behind it.

    F-B v2 generation      POST /pilot/children/{id}/goal-suggestions/generate-v2
    Hannah's approval      POST /pilot/children/{id}/goals
    current week release   POST /pilot/children/{id}/weekly-cycles/current/release
    caregiver's week       GET  /pilot/children/{id}/this-week

The A2 v2 projection is written through its own private route in
`test_a2_v2_emulator.py`; here it is seeded directly so this file stays about the
PROVIDER and CAREGIVER surfaces. That is the one shortcut, and it is stated.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
DOMAIN = "talking_and_communicating"
CYCLE_MONTH = "2026-10"
CAPACITY = 3

AT_24 = "says at least two words together like more milk"
BOOK = "name things in a book when you point and ask what is this"
TWO_WORD = "say two or more words together with one action word"
VOCAB = "says about 50 words"
PRONOUNS = "says words like i me or we"
TAXONOMY_VERSION = "activity_family_taxonomy_v1"
GOLD_STANDARD_VERSION = "parent-2.4-functional-baseline-v1"
BASELINE_VERSION_V2 = "parent-2.4-functional-baseline-v2"
FAMILIES = {AT_24: "expressive_vocabulary_growth",
            BOOK: "book_object_naming",
            TWO_WORD: "two_word_phrases",
            VOCAB: "expressive_vocabulary_growth"}


def _call(app, path, *, method="GET", bearer=None, body=b""):
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
               "SERVER_NAME": "testserver", "SERVER_PORT": "80",
               "SERVER_PROTOCOL": "HTTP/1.1", "wsgi.input": io.BytesIO(body),
               "wsgi.url_scheme": "http", "CONTENT_LENGTH": str(len(body))}
    if bearer:
        environ["HTTP_AUTHORIZATION"] = bearer
    captured = {}

    def start_response(status, headers):
        captured["status"] = int(status.split()[0])

    chunks = app(environ, start_response)
    return captured["status"], json.loads(b"".join(chunks).decode("utf-8"))


@pytest.fixture()
def stack(repos, unique_suffix):
    """The composed WSGI app over the emulator, with a seeded v2 projection."""
    from pilot_backend.audit.recorder import AuditRecorder
    from pilot_backend.auth import DevAuthVerifier, VerifiedToken
    from pilot_backend.config import PilotSettings
    from pilot_backend.domain.canonical_rung import (
        ActivityFamilyBinding,
        CanonicalRung,
        compute_rung_ref,
    )
    from pilot_backend.domain.managing_clinician import (
        ManagingClinicianAssignment,
    )
    from pilot_backend.domain.parent_baseline_projection_v2 import (
        ParentBaselineProjectionV2,
        ProjectedBandTotal,
        ProjectedSkillEvidence,
    )
    from pilot_backend.domain.roles import ActorRole
    from pilot_backend.domain.source_link import SourceSystem, SourceSystemLink
    from pilot_backend.fixtures.secure_topology import build_secure_topology
    from pilot_backend.integration.gold_standard_source import (
        InMemoryGoldStandardRungSource,
        RungTarget,
    )
    from pilot_backend.transport.wsgi_app import build_application
    from pilot_runtime.integration.static_activity_bank import (
        StaticActivityBank,
    )

    def rung(milestone, months):
        return CanonicalRung.build(
            domain_key=DOMAIN, source_rung_months=months,
            milestone_text=milestone, subdomain="expressive_language",
            family_bindings=(ActivityFamilyBinding(
                family_ref=FAMILIES[milestone], allowed_domains=(DOMAIN,)),),
            track_subdomains=("early_vocalization_and_babbling",
                              "expressive_language"),
            track_families=(), taxonomy_version=TAXONOMY_VERSION,
            baseline_version=GOLD_STANDARD_VERSION)

    topo = build_secure_topology(repos, now=NOW, subject_suffix=unique_suffix)
    child_id = topo.child_alpha.child_id
    session_id = f"sess-chain{unique_suffix}"

    repos.managing_clinicians.create(ManagingClinicianAssignment.create(
        child_id, topo.provider_alpha.provider_id, topo.practice.practice_id,
        provider_connection_id=topo.link_alpha_provider.connection_id,
        actor_id=topo.caregiver_alpha.caregiver_id, now=NOW))
    repos.source_links.create(SourceSystemLink.create(
        child_id, SourceSystem.PARENT, session_id, actor_id="fixture",
        actor_role=ActorRole.CAREGIVER.value, now=NOW))

    states = {BOOK: "not_demonstrated", TWO_WORD: "demonstrated",
              VOCAB: "demonstrated", PRONOUNS: "unknown"}
    evidence = [ProjectedSkillEvidence(
        rung_ref=compute_rung_ref(DOMAIN, 24, AT_24), months=24,
        state="demonstrated")]
    evidence += [ProjectedSkillEvidence(
        rung_ref=compute_rung_ref(DOMAIN, 30, m), months=30, state=s)
        for m, s in states.items()]
    repos.parent_baseline_projections_v2.create(
        ParentBaselineProjectionV2.build(
            child_id=child_id, source_session_id=session_id,
            source_record_digest="c" * 64,
            summary={"domain": DOMAIN, "area_id": "talking",
                     "entry_choice_id": "two_three_words",
                     "routing_anchor_months": 24,
                     "not_demonstrated_months": 30, "status": "BOUNDED",
                     "baseline_version": BASELINE_VERSION_V2},
            skill_evidence=tuple(evidence),
            band_totals=(ProjectedBandTotal(months=24, total_skills=1),
                         ProjectedBandTotal(months=30, total_skills=4)),
            now=NOW))

    settings = PilotSettings.from_env({
        "PILOT_ENVIRONMENT": "dev",
        "PILOT_ALLOWED_ORIGINS": "http://localhost:3000",
        "PILOT_DEV_AUTH": "1"})
    verifier = DevAuthVerifier("dev")
    for token, subject in (
            ("token-provider", topo.provider_alpha.auth_subject),
            ("token-other-provider", topo.provider_beta.auth_subject),
            ("token-caregiver", topo.caregiver_alpha.auth_subject)):
        verifier.add(token, VerifiedToken(subject=subject,
                                          email="x@example.invalid"))
    app = build_application(
        settings=settings, repos=repos, verifier=verifier,
        recorder=AuditRecorder(repos.audit_events, environment="dev"),
        rung_source=InMemoryGoldStandardRungSource(
            rungs=(rung(AT_24, 24), rung(BOOK, 30), rung(TWO_WORD, 30),
                   rung(VOCAB, 30)),
            unmappable=(RungTarget(domain_key=DOMAIN, source_rung_months=30,
                                   milestone_text=PRONOUNS),)),
        activity_bank=StaticActivityBank(), log_sink=[])

    class Bundle:
        pass

    bundle = Bundle()
    bundle.app, bundle.repos, bundle.child_id = app, repos, child_id
    bundle.provider_subject = topo.provider_alpha.auth_subject
    bundle.generate = f"/pilot/children/{child_id}/goal-suggestions/generate-v2"
    bundle.goals_route = f"/pilot/children/{child_id}/goals"
    bundle.release = (f"/pilot/children/{child_id}"
                      "/weekly-cycles/current/release")
    bundle.this_week = f"/pilot/children/{child_id}/this-week"
    return bundle


def test_the_whole_chain_through_the_real_routes(stack):
    """One pass, every step an HTTP request, Firestore behind it."""
    trace = []

    # 1. F-B v2 generation, over the route.
    status, body = _call(stack.app, stack.generate, method="POST",
                         bearer="Bearer token-provider")
    assert status == 200, body
    assert body["outcome"] == "suggestions_generated"
    assert body["target_band_months"] == 30
    assert len(body["generated"]) == 1
    suggestion_id = body["suggestion_ids"][0]
    trace.append(f"POST generate-v2 -> 200  band=30m  "
                 f"suggestions={len(body['suggestion_ids'])}  "
                 f"unsupported={body['unsupported_target_refs']}  "
                 f"unknown={len(body['unknown_refs'])}")

    # 2. Hannah approves verbatim, over the route.
    status, body = _call(
        stack.app, stack.goals_route, method="POST",
        bearer="Bearer token-provider",
        # The route's allowlist is exactly edit_type/suggestion_id/text/reason
        # — `goal_kind` is not accepted, because the route IS the clinical one.
        body=json.dumps({"edit_type": "accepted_verbatim",
                         "suggestion_id": suggestion_id}).encode())
    assert status == 200, body
    goal_id = body["goal"]["goal_id"]
    goal_text = body["goal"]["text"]
    trace.append(f"POST goals -> 200  goal={goal_id[:18]}…")

    # 3. The caregiver cannot release.
    capacity_body = json.dumps(
        {"family_declared_capacity": CAPACITY}).encode()
    status, body = _call(stack.app, stack.release, method="POST",
                         bearer="Bearer token-caregiver", body=capacity_body)
    assert (status, body) == (403, {"error": "not permitted"})
    trace.append("POST release as CAREGIVER -> 403 not permitted")

    # 4. A connected but NON-MANAGING provider gets the same constant answer.
    status, body = _call(stack.app, stack.release, method="POST",
                         bearer="Bearer token-other-provider",
                         body=capacity_body)
    assert (status, body) == (403, {"error": "not permitted"})
    trace.append("POST release as NON-MANAGING provider -> 403 not permitted")

    # 5. Capacity omitted -> refused, same constant answer, nothing written.
    status, body = _call(stack.app, stack.release, method="POST",
                         bearer="Bearer token-provider", body=b"{}")
    assert (status, body) == (403, {"error": "not permitted"})
    trace.append("POST release with NO capacity -> 403 not permitted")

    # 6. Draft invisible: nothing released yet.
    status, body = _call(stack.app, stack.this_week,
                         bearer="Bearer token-caregiver")
    assert status == 200 and body["released"] is False and body["week"] is None
    trace.append("GET this-week before release -> 200 released=false week=null")

    # 7. The managing provider releases with capacity 3.
    status, body = _call(stack.app, stack.release, method="POST",
                         bearer="Bearer token-provider", body=capacity_body)
    assert status == 200, body
    assert body["created"] is True
    assert body["activity_count"] == CAPACITY
    assert body["goal_ids"] == [goal_id]
    week = body["week"]["week"]
    assert (week["starts_on"], week["ends_on"]) == ("2026-10-05",
                                                    "2026-10-11")
    trace.append(f"POST release capacity=3 -> 200 created=true  "
                 f"week={week['starts_on']}..{week['ends_on']}  "
                 f"activities={body['activity_count']}")

    # 8. The caregiver reads the released week.
    status, parent = _call(stack.app, stack.this_week,
                           bearer="Bearer token-caregiver")
    assert status == 200 and parent["released"] is True
    days = [d for d in parent["week"]["days"] if d["activities"]]
    titles = [(d["local_date"], a["title"])
              for d in days for a in d["activities"]]
    assert len(titles) == CAPACITY
    trace.append(f"GET this-week as CAREGIVER -> 200 released=true  "
                 f"activities={len(titles)}")

    # Exactly 3 reviewed activities, all from the ONE approved goal.
    goal_ids = {a["goal_id"] for d in days for a in d["activities"]}
    assert goal_ids == {goal_id}

    # Minimum necessary: the opaque handle, never the template digest.
    blob = json.dumps(parent)
    assert "atpl1:" not in blob
    assert "activity_template_id" not in blob
    assert "rung1:" not in blob and "track1:" not in blob
    for activity in (a for d in days for a in d["activities"]):
        assert activity["activity_ref"].startswith("pact1:")
        assert "activity_instance_ref" not in activity

    # 9. Replay is idempotent.
    status, again = _call(stack.app, stack.release, method="POST",
                          bearer="Bearer token-provider", body=capacity_body)
    assert status == 200 and again["created"] is False
    assert again["cycle_id"] == body["cycle_id"]
    assert again["week"] == body["week"]
    trace.append("POST release again -> 200 created=false (idempotent)")

    # 10. Immutable after a goal revision.
    from pilot_backend.domain.goals import GoalKind, GoalRef
    from pilot_backend.goals.service import GoalService
    from pilot_backend.auth.interface import VerifiedToken
    from pilot_backend.auth.resolver import resolve_principal

    goals = GoalService(repos=stack.repos, recorder=None, now=lambda: NOW)
    principal = resolve_principal(
        VerifiedToken(subject=stack.provider_subject), stack.repos)
    goals.revise_goal(principal, GoalRef(GoalKind.CLINICAL, goal_id),
                      text="A revised wording the released week must not adopt.",
                      reason="clinician refinement")

    status, after = _call(stack.app, stack.this_week,
                          bearer="Bearer token-caregiver")
    assert status == 200
    assert after["week"] == parent["week"], "the released week was rewritten"
    assert "revised wording" not in json.dumps(after)
    trace.append("GET this-week after goal revision -> unchanged (immutable)")

    print("\n=== FULL CHAIN, REAL ROUTES, EMULATOR ===")
    for line in trace:
        print(f"  {line}")
    print(f"\n  approved goal text : {goal_text!r}")
    print("  released activities:")
    for local_date, title in titles:
        print(f"    {local_date}  {title}")
