"""P0-B2a — the accelerated multi-week rehearsal, on a REAL emulator.

The permanent form of the fictional rehearsal the founder accepted as the
authoritative proof of P0-B2a's multi-week behaviour (a real-calendar staging
run would take weeks and must not get a clock override). Week 1 -> Week 2 ->
Week 3 in one test, every product step an HTTP request.

What is real
    * `build_runtime` — the production composition root, with the production
      ladder, reviewed activity bank and rung table artifacts;
    * every product step is an HTTP request to the composed WSGI app;
    * the Parent -> Pilot observation body comes from the Parent's own
      `api.progress.pilot_observation_payload`;
    * the Firestore emulator.

What is doubled or seeded (stated, not hidden)
    * TIME. Every pilot module's `datetime` name is swapped (monkeypatch, so it
      is undone after the test) for a subclass whose `now()` reads a pinned
      America/Los_Angeles clock. No runtime code, route or setting changes;
    * `firebase_admin.auth.verify_id_token`, exactly as `test_e2e_pre_phi`;
    * onboarding (caregiver/provider/child/accepted connection, managing
      clinician, Parent source link, A2 v2 projection) is SEEDED — the
      out-of-band setup, proven elsewhere (0.5B / 0.5F-A2 / 0.5F-A3);
    * RTM episode/period opening and the evidence summary go through
      RTMService: there is no HTTP route for them.

Worlds: M (main: Bridge 2 -> 3 -> 2, successor), U (Not sure; wasnt_ready),
X (out of order; conflicting), R2 (Bridge 1; unsupported up), S (one signal;
no move), R4 (Bridge 2 too_hard; unsupported down), N (off-ladder control).
Fictional data only.
"""

from __future__ import annotations

import datetime as _dt
import importlib
import io
import json
import pathlib
import pkgutil
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import pytest

TZ = "America/Los_Angeles"
LA = ZoneInfo(TZ)
UTC = _dt.timezone.utc
MONTH = "2026-12"
CAP = 3
WRITERS = 8
DOMAIN = "talking_and_communicating"
ANCHOR = "rung1:feb590cf2788978b383c11062ce67c1b"
LADDER_REF = "ldr1:b55ffcff182180a486a6a93aeff03691"
BOOK_30 = "rung1:431020f1dccb8b2adbcad72db2d51160"
BASELINE_V2 = "parent-2.4-functional-baseline-v2"
RUN = secrets.token_hex(3)
EVIDENCE = {"run": RUN, "steps": []}

# ---------------------------------------------------------------------------
# the pinned clock
# ---------------------------------------------------------------------------
REAL = _dt.datetime


class _Clock:
    value = REAL(2026, 12, 2, 16, 0, tzinfo=UTC)


CLOCK = _Clock()


def at(day: str, hhmm: str) -> None:
    CLOCK.value = REAL.fromisoformat(f"{day}T{hhmm}:00").replace(
        tzinfo=LA).astimezone(UTC)


class _Meta(type):
    def __instancecheck__(cls, obj):
        return isinstance(obj, REAL)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, REAL)


class FrozenDatetime(REAL, metaclass=_Meta):
    @classmethod
    def now(cls, tz=None):
        v = CLOCK.value
        return v.astimezone(tz) if tz is not None else v.astimezone(
            UTC).replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return CLOCK.value.astimezone(UTC).replace(tzinfo=None)


def install_clock(monkeypatch):
    import pilot_backend
    import pilot_runtime
    failed = []
    for pkg in (pilot_backend, pilot_runtime):
        for info in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
            if ".tests" in info.name or ".deploy" in info.name:
                continue
            try:
                importlib.import_module(info.name)
            except Exception as exc:          # recorded, reported
                failed.append((info.name, type(exc).__name__))
    patched = []
    for name, mod in list(sys.modules.items()):
        if not name.startswith(("pilot_backend", "pilot_runtime")) or \
                ".tests" in name:
            continue
        for attr, val in list(vars(mod).items()):
            if val is REAL:
                monkeypatch.setattr(mod, attr, FrozenDatetime)
                patched.append(f"{name}.{attr}")
    return patched, failed


def clock_leaks():
    return [f"{n}.{a}" for n, m in list(sys.modules.items())
            if n.startswith(("pilot_backend", "pilot_runtime"))
            and ".tests" not in n
            for a, v in list(vars(m).items()) if v is REAL]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Http:
    def __init__(self, app):
        self.app = app
        self.log = []

    def __call__(self, method, path, token=None, body=None, query=""):
        raw = b"" if body is None else json.dumps(body).encode()
        environ = {"REQUEST_METHOD": method, "PATH_INFO": path,
                   "QUERY_STRING": query, "SERVER_NAME": "testserver",
                   "SERVER_PORT": "80", "SERVER_PROTOCOL": "HTTP/1.1",
                   "wsgi.input": io.BytesIO(raw), "wsgi.url_scheme": "http",
                   "CONTENT_LENGTH": str(len(raw))}
        if token:
            environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        got = {}

        def start_response(status, headers):
            got["status"] = int(status.split()[0])

        out = json.loads(b"".join(self.app(environ, start_response)))
        self.log.append((method, path, got["status"]))
        assert got["status"] != 500, (method, path, out)   # stop condition
        return got["status"], out


# ---------------------------------------------------------------------------
# worlds
# ---------------------------------------------------------------------------

class World:
    pass


def make_world(rt, fb, name, *, on_ladder=True):
    from pilot_backend.domain.managing_clinician import (
        ManagingClinicianAssignment)
    from pilot_backend.domain.parent_baseline_projection_v2 import (
        ParentBaselineProjectionV2, ProjectedBandTotal, ProjectedSkillEvidence)
    from pilot_backend.domain.roles import ActorRole
    from pilot_backend.domain.source_link import SourceSystem, SourceSystemLink
    from pilot_backend.fixtures.secure_topology import build_secure_topology

    repos = rt.repos
    now = CLOCK.value
    topo = build_secure_topology(repos, now=now,
                                 subject_suffix=f"-reh-{RUN}-{name}")
    w = World()
    w.name, w.topo = name, topo
    w.child = topo.child_alpha.child_id
    session = f"sess-reh-{RUN}-{name}"
    repos.managing_clinicians.create(ManagingClinicianAssignment.create(
        w.child, topo.provider_alpha.provider_id, topo.practice.practice_id,
        provider_connection_id=topo.link_alpha_provider.connection_id,
        actor_id=topo.caregiver_alpha.caregiver_id, now=now))
    repos.source_links.create(SourceSystemLink.create(
        w.child, SourceSystem.PARENT, session, actor_id="fixture",
        actor_role=ActorRole.CAREGIVER.value, now=now))
    roster30 = list(rt.rung_source.declared_band_roster(DOMAIN, 30))
    assert len(roster30) == 4 and BOOK_30 in roster30
    if on_ladder:      # the P0-B1 positive-path shape -> the 24m anchor
        ev = [ProjectedSkillEvidence(rung_ref=ANCHOR, months=24,
                                     state="emerging")]
        ev += [ProjectedSkillEvidence(rung_ref=r, months=30,
                                      state="demonstrated") for r in roster30]
    else:              # off-ladder control: 24m mastered, 30m book naming ND
        ev = [ProjectedSkillEvidence(rung_ref=ANCHOR, months=24,
                                     state="demonstrated")]
        ev += [ProjectedSkillEvidence(
            rung_ref=r, months=30,
            state="not_demonstrated" if r == BOOK_30 else "demonstrated")
            for r in roster30]
    repos.parent_baseline_projections_v2.create(ParentBaselineProjectionV2.build(
        child_id=w.child, source_session_id=session,
        source_record_digest="d" * 64,
        summary={"domain": DOMAIN, "area_id": "talking",
                 "entry_choice_id": "two_three_words",
                 "routing_anchor_months": 24, "not_demonstrated_months": 30,
                 "status": "BOUNDED", "baseline_version": BASELINE_V2},
        skill_evidence=tuple(ev),
        band_totals=(ProjectedBandTotal(months=24, total_skills=1),
                     ProjectedBandTotal(months=30, total_skills=4)),
        now=now))
    w.pr = fb.issue(f"tok-{RUN}-{name}-pr", topo.provider_alpha.auth_subject)
    w.pr2 = fb.issue(f"tok-{RUN}-{name}-pr2", topo.provider_beta.auth_subject)
    w.cg = fb.issue(f"tok-{RUN}-{name}-cg", topo.caregiver_alpha.auth_subject)
    w.cg2 = fb.issue(f"tok-{RUN}-{name}-cg2", topo.caregiver_beta.auth_subject)
    w.fb_seq = 0
    w.weeks = {}
    return w


def rows(rt, collection, **match):
    return [d for _i, d in rt.repos.store.list_all(collection)
            if all(d.get(k) == v for k, v in match.items())]


def step(label, **facts):
    EVIDENCE["steps"].append({"step": label, **facts})
    print(f"  ✓ {label}  " + "  ".join(f"{k}={v}" for k, v in facts.items()))


# --- caregiver privacy ------------------------------------------------------
INTERNAL = ("node1:", "ldr1:", "rung1:", "step_", "atpl", "sha256",
            "requires_clinician_review", "transition_not_enabled",
            "outside_envelope", "conflicting_signals", "evidence_already_used",
            "repeated_too_easy", "repeated_too_hard", "movement", "placement",
            "moved", "bridge 1", "bridge 2", "bridge 3", "clinician_review",
            "review_items", "proposed", "envelope", "months", "-month",
            "expressive_vocabulary_growth", "two_word_phrases")
CAREGIVER_BLOBS = []


def caregiver_clean(label, body):
    blob = json.dumps(body).lower()
    hits = [b for b in INTERNAL if b in blob]
    CAREGIVER_BLOBS.append(label)
    assert not hits, (label, hits)


# --- product calls ----------------------------------------------------------

def generate_and_approve_target(http, rt, w):
    s, b = http("POST", f"/pilot/children/{w.child}/goal-suggestions/generate-v2",
                w.pr)
    assert s == 200 and b["outcome"] == "suggestions_generated", b
    w.suggestion = b["suggestion_ids"][0]
    anchor = rt.repos.suggestion_anchors.find(w.suggestion)
    w.target = anchor.rung.rung_ref
    return w.target


def approve(http, w):
    s, b = http("POST", f"/pilot/children/{w.child}/goals", w.pr,
                {"edit_type": "accepted_verbatim", "suggestion_id": w.suggestion})
    assert s == 200, b
    w.goal = b["goal"]["goal_id"]


def bridge_get(http, w, token=None):
    return http("GET", f"/pilot/children/{w.child}/bridge-baseline",
                token or w.cg)


def bridge_post(http, w, answers, token=None):
    return http("POST", f"/pilot/children/{w.child}/bridge-baseline",
                token or w.cg, {"answers": list(answers)})


def plan_and_allocate(http, w):
    s, b = http("POST", f"/pilot/children/{w.child}/monthly-plan", w.pr,
                {"cycle_month": MONTH, "timezone_of_record": TZ})
    assert s == 200 and b["plan"]["state"] == "draft", b
    w.plan = b["plan"]["focus_plan_id"]
    s, b = http("POST", f"/pilot/monthly-plans/{w.plan}/allocations", w.pr,
                {"goal_kind": "clinical", "goal_id": w.goal, "priority_rank": 1})
    assert s == 200, b


def start_view(http, w, token=None):
    s, b = http("GET", f"/pilot/children/{w.child}/developmental-start",
                token or w.pr)
    return s, (b.get("developmental_start") if s == 200 else b)


def place(http, w, step_key, token=None):
    return http("POST", f"/pilot/monthly-plans/{w.plan}/developmental-placement",
                token or w.pr, {"step_key": step_key})


def activate(http, w):
    s, b = http("POST", f"/pilot/monthly-plans/{w.plan}/activate", w.pr)
    assert s == 200 and b["plan"]["state"] == "active", b


def release(http, w, token=None):
    return http("POST", f"/pilot/children/{w.child}/weekly-cycles/current/release",
                token or w.pr, {"family_declared_capacity": CAP})


def this_week(http, w, label):
    s, b = http("GET", f"/pilot/children/{w.child}/this-week", w.cg)
    assert s == 200 and b["released"] is True, b
    caregiver_clean(f"{w.name} this-week {label}", b)
    return b["week"]


def activities(week_view):
    return [(d["local_date"], a) for d in week_view["days"]
            for a in d["activities"]]


def observe(http, w, cycle_id, activity_ref, day, *, completion="did_it",
            difficulty="just_right", supersedes=None):
    from api import progress as parent_progress   # the Parent's own mapping
    w.fb_seq += 1
    fid = f"fb_reh_{RUN}_{w.name}_{w.fb_seq}"
    feedback = {"feedback_id": fid, "pilot_cycle_id": cycle_id,
                "pilot_activity_ref": activity_ref, "activity_date": day,
                "local_submission_date": day, "completion": completion,
                "difficulty": difficulty, "enjoyment": "it_was_okay"}
    if supersedes:
        feedback["supersedes_feedback_id"] = supersedes
    payload = parent_progress.pilot_observation_payload(feedback)
    assert payload["cycle_id"] == cycle_id
    s, b = http("POST", f"/pilot/cycles/{cycle_id}/observations", w.cg,
                payload["body"])
    assert s == 200, b
    blob = json.dumps(b).lower()
    assert not [t for t in ("node1:", "ldr1:", "rung1:", "step_", "movement",
                            "placement", "review") if t in blob], b
    return fid, b["observation"]["event_id"]


def snapshot_of(rt, cycle_id):
    (snap,) = rows(rt, "pilot_weekly_plan_snapshots", cycle_id=cycle_id)
    return snap


def templates(rt, cycle_id):
    doc = json.loads(snapshot_of(rt, cycle_id)["resolved_plan_document"])
    return {a["activity_template_id"] for a in doc["activities"]}


def pool(rt, step_key):
    return set(rt.developmental_ladder.by_step_key(step_key).pool_template_ids)


def movement_for(rt, w, cycle_id):
    found = rows(rt, "pilot_developmental_movements", child_id=w.child,
                 to_cycle_id=cycle_id)
    assert len(found) == 1, found
    return found[0]


def placement_events(rt, w):
    return [e for e in rows(rt, "pilot_audit_events", child_id=w.child)
            if e.get("action") == "developmental_placement_confirmed"]


def concurrently(fn, n=WRITERS):
    barrier = threading.Barrier(n)
    out, errors = [], []

    def run(_i):
        barrier.wait(timeout=30)
        try:
            return fn()
        except AssertionError:
            raise
        except Exception as exc:
            errors.append(type(exc).__name__)
            return None

    with ThreadPoolExecutor(max_workers=n) as pool_:
        out = list(pool_.map(run, range(n)))
    return out, errors


# ---------------------------------------------------------------------------
# the rehearsal
# ---------------------------------------------------------------------------

@pytest.fixture()
def parent_on_path(monkeypatch):
    """The Parent package is a sibling namespace; fail (never skip) if absent."""
    root = pathlib.Path(__file__).resolve().parents[3] / "genex-parent"
    assert (root / "api" / "progress.py").is_file(), root
    monkeypatch.syspath_prepend(str(root))


def test_p0b2a_accelerated_rehearsal(firestore_client, parent_on_path,
                                     monkeypatch):
    from pilot_runtime.auth.firebase_decoder import FirebaseTokenDecoder
    from pilot_runtime.composition import build_runtime

    from .test_e2e_pre_phi import ScriptedFirebaseAuth

    fb = ScriptedFirebaseAuth()
    logs = []
    rt = build_runtime(
        {"PILOT_ENVIRONMENT": "dev",
         "PILOT_ALLOWED_ORIGINS": "https://genex-kiddo-compass.lovable.app"},
        process_env={}, firestore_client=firestore_client,
        decoder=FirebaseTokenDecoder(app=object(), verify_module=fb),
        log_sink=logs)
    patched, import_failures = install_clock(monkeypatch)
    assert len(patched) >= 40, patched
    assert rt.developmental_ladder.ladder_ref == LADDER_REF
    step("composition: production ladder + bank + rung table",
         ladder=LADDER_REF, clock_patched_modules=len(patched),
         import_failures=import_failures)
    http = Http(rt.application)

    # ======================= Wed 2026-12-02 — onboarding ====================
    at("2026-12-02", "08:00")
    M = make_world(rt, fb, "M")
    U = make_world(rt, fb, "U")          # Not sure -> later R1 (wasnt_ready)
    X = make_world(rt, fb, "X")          # out of order -> later R3 (conflict)
    R2 = make_world(rt, fb, "R2")        # B1 placement -> unsupported move
    S = make_world(rt, fb, "S")          # ONE too_easy -> no move
    R4 = make_world(rt, fb, "R4")        # B2 too_hard x2 -> unsupported down
    N = make_world(rt, fb, "N", on_ladder=False)
    worlds = (M, U, X, R2, S, R4)

    for w in (*worlds, N):
        # Before generation: no target -> the questions are not asked.
        s, b = bridge_get(http, w)
        assert (s, b.get("baseline")) == (200, {"applicable": False}), b
        target = generate_and_approve_target(http, rt, w)
        assert target == (ANCHOR if w is not N else BOOK_30), (w.name, target)
    step("F-B v2 targets: 4 ladder worlds -> 24m anchor; N -> 30m book naming",
         anchor=ANCHOR)

    # ---- A. Parent baseline ------------------------------------------------
    for w in worlds:
        s, b = bridge_get(http, w)
        bl = b["baseline"]
        assert s == 200 and bl["applicable"] is True and not bl["submitted"]
        assert bl["prompt"] == "Right now, does this describe your child?"
        assert [o["label"] for o in bl["options"]] == [
            "Yes", "Sometimes", "Not yet", "Not sure"]
        assert len(bl["questions"]) == 3
        caregiver_clean(f"{w.name} bridge GET", b)
    s, b = bridge_get(http, N)
    assert b["baseline"] == {"applicable": False}
    s, _b = bridge_post(http, N, ["yes", "yes", "yes"])
    assert s == 403
    assert not rows(rt, "pilot_bridge_baseline_answers", child_id=N.child)
    step("N off-ladder control: applicable=false, POST 403, nothing written")

    # The provider can neither read nor answer the family's questions.
    assert bridge_post(http, M, ["yes", "yes", "yes"], token=M.pr)[0] == 403
    assert bridge_get(http, M, token=M.cg2)[0] == 403

    answers = {"M": ["yes", "sometimes", None],        # A -> Bridge 2
               "U": ["not_sure", None, None],          # B -> no proposal
               "X": ["not_yet", "yes", None],          # C -> out of order
               "R2": ["yes", "not_yet", None],
               "S": ["yes", "sometimes", None],
               "R4": ["yes", "sometimes", None]}
    for w in worlds:
        at("2026-12-02", "08:10")
        s, b = bridge_post(http, w, answers[w.name])
        assert s == 200 and set(b) == {"recorded", "request_id"}, b
        caregiver_clean(f"{w.name} bridge POST", b)
        stored = rows(rt, "pilot_bridge_baseline_answers", child_id=w.child)
        assert len(stored) == 1
    step("A1-A3 baseline answers recorded; responses = {recorded, request_id}")

    for w in worlds:
        approve(http, w)
    # After approval the TARGET set is the active goal; still applicable.
    assert bridge_get(http, M)[1]["baseline"]["submitted"] is True

    # ---- B. Therapist plan setup ------------------------------------------
    at("2026-12-02", "08:30")
    expected = {"M": ("step_3", "consistent"), "U": (None, None),
                "X": (None, "needs_clinician_confirmation")}
    for w in worlds:
        plan_and_allocate(http, w)
        s, v = start_view(http, w)
        assert s == 200, v
        w.view0 = v
        blob = json.dumps(v)
        assert not [t for t in ("node1:", "ldr1:", "rung1:", "atpl", "sha256")
                    if t in blob], w.name
        assert [x["selectable"] for x in v["steps"]] == [
            False, True, True, True, True]
        assert v["automatic_moves"] == [{"between": ["step_3", "step_4"]}]
        assert v["proposal_basis"] == "baseline"
        assert start_view(http, w, token=w.cg)[0] == 403
        assert start_view(http, w, token=w.pr2)[0] == 403
    assert M.view0["proposed_step_key"] == "step_3"
    assert M.view0["baseline"]["status"] == "consistent"
    assert U.view0["proposed_step_key"] is None
    assert X.view0["proposed_step_key"] is None
    assert X.view0["baseline"]["status"] == "needs_clinician_confirmation"
    step("B1-B3 developmental-start", M=("step_3", "consistent"),
         U=(None, U.view0["baseline"]["status"]),
         X=(None, "needs_clinician_confirmation"),
         R2=(R2.view0["proposed_step_key"], R2.view0["baseline"]["status"]))

    # Activation before placement is the clinician's choice (P0-B1 path); the
    # proof places every ladder world. Refusals first:
    assert place(http, M, "step_1")[0] == 403           # no reviewed pool
    assert place(http, M, "step_3", token=M.cg)[0] == 403
    assert place(http, M, "step_3", token=M.pr2)[0] == 403

    # M: 8 concurrent identical placements through the route.
    results, errors = concurrently(lambda: place(http, M, "step_3"))
    oks = [b for s, b in results if s == 200]
    assert oks and all(b["placement"]["starting_step_key"] == "step_3"
                       for b in oks)
    assert place(http, M, "step_3")[0] == 200                     # replay
    assert place(http, M, "step_4")[0] == 403                     # different
    plc = rows(rt, "pilot_developmental_placements", focus_plan_id=M.plan)
    ev = placement_events(rt, M)
    assert len(plc) == 1 and len(ev) == 1, (plc, ev)
    md = ev[0]["metadata"]
    assert md["placement_source"] == "confirmed_baseline_proposal"
    assert md["starting_node_index"] == "2" and md["ladder_ref"] == LADDER_REF
    assert md["proposal_basis"] == "baseline"
    assert md["starting_node_ref"].startswith("node1:")
    assert not [k for k in md if "answer" in k or "wording" in k or "age" in k]
    step("B4-B5 M placement: 8-way concurrent + replay + conflicting",
         http_200=len(oks), refused=sorted({s for s, _ in results if s != 200}),
         placements=1, audit_events=1, source=md["placement_source"],
         index=md["starting_node_index"])

    for w, k in ((U, "step_3"), (X, "step_3"), (R2, "step_2"), (S, "step_3"),
                 (R4, "step_3")):
        s, b = place(http, w, k)
        assert s == 200, b
        w.placement_source = b["placement"]["source"]
        assert len(placement_events(rt, w)) == 1
    assert U.placement_source == X.placement_source == "clinician_choice"
    assert S.placement_source == R4.placement_source == \
        "confirmed_baseline_proposal"
    step("U/X placed step_3 and R2 placed step_2 by the clinician",
         U=U.placement_source, X=X.placement_source, R2=R2.placement_source)

    for w in worlds:
        activate(http, w)
    assert place(http, M, "step_3")[0] in (200, 403)
    assert len(placement_events(rt, M)) == 1, "no event after activation"

    # ---- B6. Week 1 --------------------------------------------------------
    at("2026-12-02", "09:00")
    assert release(http, M, token=M.cg)[0] == 403
    for w in worlds:
        s, b = release(http, w)
        assert s == 200 and b["created"] is True, b
        w.weeks[1] = b["cycle_id"]
        wk = this_week(http, w, "W1")
        assert wk["week"]["starts_on"] == "2026-12-02"
        assert wk["week"]["ends_on"] == "2026-12-06"
        w.w1_view = wk
        w.w1_snapshot = snapshot_of(rt, w.weeks[1])
    assert templates(rt, M.weeks[1]) <= pool(rt, "step_3")
    assert templates(rt, R2.weeks[1]) <= pool(rt, "step_2")
    assert not (pool(rt, "step_3") & pool(rt, "step_4"))
    step("B6 Week 1 released Dec 2-6 (LA)", M_templates=len(
        templates(rt, M.weeks[1])), M_pool="step_3 (vocabulary, 12)")

    # RTM monitoring start for M (service: no HTTP route exists).
    from pilot_backend.domain.goals import GoalKind, GoalRef
    from pilot_backend.rtm.service import RTMService
    from pilot_backend.auth.interface import VerifiedToken
    from pilot_backend.auth.resolver import resolve_principal
    prov = resolve_principal(VerifiedToken(
        subject=M.topo.provider_alpha.auth_subject), rt.repos)
    rtm = RTMService(repos=rt.repos, recorder=rt.recorder)
    episode = rtm.open_episode(prov, M.child, [GoalRef(GoalKind.CLINICAL, M.goal)])
    period = rtm.open_period(prov, episode.episode_id, M.plan, cycle_month=MONTH)

    # ---- C. too_easy x2 (Week 1 -> Week 2) --------------------------------
    m_acts = activities(M.w1_view)
    first_day = [a for d, a in m_acts if d == "2026-12-02"]
    title = sorted(a["title"] for a in first_day)[0]
    ref_w1 = next(a["activity_ref"] for _d, a in m_acts if a["title"] == title)
    at("2026-12-02", "18:00")
    fid_a, ev_a = observe(http, M, M.weeks[1], ref_w1, "2026-12-02",
                          difficulty="just_right")
    at("2026-12-02", "18:05")
    _fid, ev_a2 = observe(http, M, M.weeks[1], ref_w1, "2026-12-02",
                          difficulty="too_easy", supersedes=fid_a)  # revision
    at("2026-12-03", "18:00")
    _fid, ev_b = observe(http, M, M.weeks[1], ref_w1, "2026-12-03",
                         difficulty="too_easy")
    step("C1 M too_easy on Dec 2 (a revision superseding just_right) + Dec 3",
         activity=title, evidence=[ev_a2, ev_b], superseded=ev_a)

    # R1 (U): wasnt_ready x2. R3 (X): too_easy x2 on a, too_hard x2 on b.
    # R2: too_easy x2 at B1.
    def two(w, ref, **kw):
        out = []
        for day in ("2026-12-02", "2026-12-03"):
            at(day, "19:00")
            out.append(observe(http, w, w.weeks[1], ref, day, **kw)[1])
        return out

    u_refs = sorted({a["activity_ref"] for _d, a in activities(U.w1_view)})
    x_titles = {}
    for _d, a in activities(X.w1_view):
        x_titles.setdefault(a["title"], a["activity_ref"])
    x_a, x_b = [x_titles[t] for t in sorted(x_titles)[:2]]
    r2_ref = sorted({a["activity_ref"] for _d, a in activities(R2.w1_view)})[0]
    U.ev = two(U, u_refs[0], completion="wasnt_ready_yet")
    X.ev = two(X, x_a, difficulty="too_easy") + two(X, x_b, difficulty="too_hard")
    R2.ev = two(R2, r2_ref, difficulty="too_easy")
    R4.ev = two(R4, sorted({a["activity_ref"] for _d, a in
                            activities(R4.w1_view)})[0], difficulty="too_hard")
    at("2026-12-02", "19:30")
    S.ev = [observe(http, S, S.weeks[1], sorted(
        {a["activity_ref"] for _d, a in activities(S.w1_view)})[0],
        "2026-12-02", difficulty="too_easy")[1]]

    w1_before = {w.name: dict(snapshot_of(rt, w.weeks[1])) for w in worlds}

    # ---- C2. Mon 2026-12-07: Week 2, M via 8 concurrent HTTP releases -----
    at("2026-12-07", "09:00")
    results, errors = concurrently(lambda: release(http, M))
    ok = [b for s, b in results if s == 200]
    assert ok, (results, errors)
    w2_statuses = sorted(s for s, _b in results)
    assert len({b["cycle_id"] for b in ok}) == 1
    final = release(http, M)
    assert final[0] == 200 and final[1]["created"] is False      # C4 replay
    M.weeks[2] = final[1]["cycle_id"]
    m2 = movement_for(rt, M, M.weeks[2])
    assert m2["outcome"] == "moved" and m2["direction"] == "up"
    assert (m2["node_before_index"], m2["node_after_index"]) == (2, 3)
    assert sorted(m2["evidence_event_ids"]) == sorted([ev_a2, ev_b])
    assert ev_a not in m2["evidence_event_ids"], "superseded row never evidence"
    dec = rows(rt, "pilot_weekly_adaptation_decisions", to_cycle_id=M.weeks[2])
    rec = rows(rt, "pilot_adaptation_records", to_cycle_id=M.weeks[2])
    assert len(dec) == 1, dec
    assert len(rows(rt, "pilot_developmental_movements", child_id=M.child)) == 1
    assert templates(rt, M.weeks[2]) and templates(rt, M.weeks[2]) <= pool(
        rt, "step_4")
    M.w2_view = this_week(http, M, "W2")
    step("C2/C4 Week 2: B2 -> B3 exactly once (8-way + replay)",
         statuses=w2_statuses, errors=sorted(set(errors)),
         created=[b["created"] for b in ok].count(True),
         movement=m2["movement_id"], evidence=len(m2["evidence_event_ids"]),
         decisions=len(dec), adaptation_records=len(rec),
         w2_templates=sorted(templates(rt, M.weeks[2])), pool="step_4 (two-word, 6)")

    # ---- E. clinician-review cases (released Dec 7) -----------------------
    review = {}
    for w, reason, n_ev, direction, stay_pool in (
            (U, "wasnt_ready", 2, "", "step_3"),
            (R2, "transition_not_enabled", 2, "up", "step_2"),
            (X, "conflicting_signals", 4, "", "step_3"),
            (R4, "transition_not_enabled", 2, "down", "step_3")):
        s, b = release(http, w)
        assert s == 200 and b["created"] is True, b
        w.weeks[2] = b["cycle_id"]
        m = movement_for(rt, w, w.weeks[2])
        assert m["outcome"] == "requires_clinician_review", m
        assert m["reason"] == reason, m
        assert m["node_before_index"] == m["node_after_index"], "no move"
        assert (m["direction"] or "") == direction, m
        assert sorted(m["evidence_event_ids"]) == sorted(w.ev), m
        assert len(rows(rt, "pilot_weekly_adaptation_decisions",
                        to_cycle_id=w.weeks[2])) == 1
        assert templates(rt, w.weeks[2]) <= pool(rt, stay_pool)
        this_week(http, w, "W2")
        review[w.name] = m
    assert len(rows(rt, "pilot_developmental_movements", child_id=X.child)
               [0]["trigger_activity_refs"]) == 2
    # S: one too_easy is not a trigger -> a movement row, NOT a move.
    s, b = release(http, S)
    assert s == 200 and b["created"] is True, b
    S.weeks[2] = b["cycle_id"]
    ms = movement_for(rt, S, S.weeks[2])
    assert ms["outcome"] != "moved" and ms["outcome"] != \
        "requires_clinician_review", ms
    assert ms["node_before_index"] == ms["node_after_index"] == 2
    assert templates(rt, S.weeks[2]) <= pool(rt, "step_3")
    this_week(http, S, "W2")
    step("S single too_easy: no trigger, stays at B2", outcome=ms["outcome"],
         reason=ms["reason"])
    step("E1-E3 review outcomes, no move",
         R1_U=review["U"]["reason"], R2=review["R2"]["reason"] + "/up",
         R3_X=review["X"]["reason"] + f"/{len(review['X']['evidence_event_ids'])}ev",
         R4=review["R4"]["reason"] + "/down")

    # ---- F1. provider sees structured review items -------------------------
    for w in (U, R2, X, R4):
        s, v = start_view(http, w)
        assert s == 200
        (p,) = [p for p in v["placements"] if p["focus_plan_id"] == w.plan]
        (item,) = p["review_items"]
        m = review[w.name]
        assert set(item) == {"movement_id", "cycle_id", "current_step_key",
                             "reason", "attempted_direction",
                             "evidence_event_ids"}, sorted(item)
        assert item["movement_id"] == m["movement_id"]
        # child = the route's child; plan = the enclosing placement; both are
        # re-checked against the stored movement row.
        assert m["child_id"] == w.child and m["focus_plan_id"] == w.plan
        assert item["cycle_id"] == w.weeks[2]
        assert item["current_step_key"] == p["current_step_key"]
        assert item["current_step_key"] == ("step_2" if w is R2 else "step_3")
        wording = {x["step_key"]: x["wording"] for x in v["steps"]}
        assert wording[item["current_step_key"]]       # wording via steps[]
        assert item["reason"] == m["reason"]
        assert (item["attempted_direction"] or "") == (m["direction"] or "")
        assert sorted(item["evidence_event_ids"]) == sorted(m["evidence_event_ids"])
        assert p["notes"], "plain-language note shown"
        w.review_item = item
    s, v = start_view(http, M)
    (pm,) = [p for p in v["placements"] if p["focus_plan_id"] == M.plan]
    assert pm["review_items"] == [] and pm["current_step_key"] == "step_4"
    step("F1 provider review_items match the movement rows (R1-R4); M has none",
         fields=sorted(U.review_item))

    # ---- C3. Week 1 snapshots unchanged ------------------------------------
    for w in worlds:
        assert dict(snapshot_of(rt, w.weeks[1])) == w1_before[w.name], w.name
    w2_before = dict(snapshot_of(rt, M.weeks[2]))

    # ---- D. too_hard x2 (Week 2 -> Week 3) --------------------------------
    m2_acts = activities(M.w2_view)
    t2 = sorted({a["title"] for d, a in m2_acts if d == "2026-12-08"})[0]
    ref_w2 = next(a["activity_ref"] for _d, a in m2_acts if a["title"] == t2)
    at("2026-12-08", "18:00")
    _f, ev_c = observe(http, M, M.weeks[2], ref_w2, "2026-12-08",
                       difficulty="too_hard")
    at("2026-12-09", "18:00")
    _f, ev_d = observe(http, M, M.weeks[2], ref_w2, "2026-12-09",
                       difficulty="too_hard")
    at("2026-12-14", "09:00")
    results, errors = concurrently(lambda: release(http, M))
    assert any(s == 200 for s, _ in results), (results, errors)
    s, b = release(http, M)
    assert s == 200 and b["created"] is False
    M.weeks[3] = b["cycle_id"]
    m3 = movement_for(rt, M, M.weeks[3])
    assert m3["outcome"] == "moved" and m3["direction"] == "down"
    assert (m3["node_before_index"], m3["node_after_index"]) == (3, 2)
    assert m3["node_before_ref"] == m2["node_after_ref"]
    assert sorted(m3["evidence_event_ids"]) == sorted([ev_c, ev_d])
    assert not set(m3["evidence_event_ids"]) & set(m2["evidence_event_ids"])
    assert len(rows(rt, "pilot_developmental_movements", child_id=M.child)) == 2
    assert templates(rt, M.weeks[3]) <= pool(rt, "step_3")
    this_week(http, M, "W3")
    assert dict(snapshot_of(rt, M.weeks[1])) == w1_before["M"]
    assert dict(snapshot_of(rt, M.weeks[2])) == w2_before
    s, v = start_view(http, M)
    (pm,) = [p for p in v["placements"] if p["focus_plan_id"] == M.plan]
    assert pm["current_step_key"] == "step_3"
    step("D Week 3: B3 -> B2 exactly once, no skip; W1+W2 snapshots unchanged",
         movement=m3["movement_id"], movements_total=2,
         w3_templates=len(templates(rt, M.weeks[3])), pool="step_3")

    # ---- C5. RTM ----------------------------------------------------------
    summary = rtm.generate_evidence_summary(prov, period.period_id)
    # Independent count: released activity rows in the three FROZEN snapshots,
    # by their scheduled local date in the month.
    scheduled = sum(
        1 for k in (1, 2, 3)
        for a in json.loads(snapshot_of(rt, M.weeks[k])[
            "resolved_plan_document"])["activities"]
        if str(a["local_date"]).startswith(MONTH))
    assert summary.total_scheduled_opportunities == scheduled, (
        summary.total_scheduled_opportunities, scheduled)
    assert summary.total_distinct_observation_events == 4     # not 5
    assert summary.distinct_observed_local_dates == 4
    assert set(summary.cycle_refs) == {M.weeks[1], M.weeks[2], M.weeks[3]}
    assert summary.contributing_focus_plan_ids == (M.plan,)
    step("C5 RTM evidence", scheduled_opportunities=scheduled,
         observation_events=summary.total_distinct_observation_events,
         superseded_excluded=True, observed_dates=summary.distinct_observed_local_dates)

    # ---- successor plan (Jan 2027): a PROPOSAL only, never auto-carried ----
    at("2026-12-28", "09:00")
    dec_placement = rows(rt, "pilot_developmental_placements",
                         focus_plan_id=M.plan)[0]
    s, b = http("POST", f"/pilot/children/{M.child}/monthly-plan", M.pr,
                {"cycle_month": "2027-01", "timezone_of_record": TZ})
    assert s == 200 and b["plan"]["state"] == "draft", b
    jan = b["plan"]["focus_plan_id"]
    s, b = http("POST", f"/pilot/monthly-plans/{jan}/allocations", M.pr,
                {"goal_kind": "clinical", "goal_id": M.goal, "priority_rank": 1})
    assert s == 200, b
    assert not rows(rt, "pilot_developmental_placements", focus_plan_id=jan), \
        "nothing carried before the clinician confirms"
    s, v = start_view(http, M)
    assert v["proposal_basis"] == "previous_plan", v["proposal_basis"]
    assert v["proposed_step_key"] == "step_3"          # M's final node (B2)
    s, b = http("POST", f"/pilot/monthly-plans/{jan}/developmental-placement",
                M.pr, {"step_key": "step_3"})
    assert s == 200 and b["placement"]["source"] == \
        "confirmed_previous_plan_step", b
    jan_pl = rows(rt, "pilot_developmental_placements", focus_plan_id=jan)
    assert len(jan_pl) == 1 and jan_pl[0]["placement_id"] != \
        dec_placement["placement_id"]
    evs = placement_events(rt, M)
    assert len(evs) == 2, "one audit event per plan"
    jan_md = [e["metadata"] for e in evs if e["resource_id"] ==
              jan_pl[0]["placement_id"]][0]
    assert jan_md["proposal_basis"] == "previous_plan"
    assert jan_md["previous_placement_id"] == dec_placement["placement_id"]
    assert jan_md["placement_source"] == "confirmed_previous_plan_step"
    step("successor Jan draft: proposal=previous_plan step_3, confirmed by "
         "clinician; own placement + own audit event", audit_events_M=len(evs))

    # ---- F2/F3. privacy ---------------------------------------------------
    for w in worlds:
        for snap_cycle in w.weeks.values():
            blob = json.dumps(snapshot_of(rt, snap_cycle)).lower()
            assert not [t for t in ("node1:", "ldr1:", "step_", "placement",
                                    "movement") if t in blob], w.name
        s, b = http("GET", "/pilot/me/children", w.cg)
        assert s == 200 and b["child_ids"] == [w.child], b
        caregiver_clean(f"{w.name} me/children", b)
        caregiver_clean(f"{w.name} bridge GET after", bridge_get(http, w)[1])
    for m in (m2, m3, *review.values()):
        assert m["node_before_ref"].startswith("node1:")      # opaque, internal
    assert md["ladder_ref"] == LADDER_REF                     # audit provenance
    step("F2/F3 caregiver surfaces clean; opaque refs only in provider/audit rows",
         caregiver_responses_checked=len(CAREGIVER_BLOBS))

    leaks = clock_leaks()
    assert not leaks, leaks
    assert not [line for line in logs if "node1:" in line or "ldr1:" in line]
    statuses = {}
    for _m, _p, s in http.log:
        statuses[s] = statuses.get(s, 0) + 1
    step("HTTP totals", requests=len(http.log), by_status=statuses,
         status_500=statuses.get(500, 0))



def test_the_pinned_clock_is_undone_after_the_rehearsal():
    """The swap is monkeypatched, so later suites in this session see real time."""
    assert not [f"{n}.{a}" for n, m in list(sys.modules.items())
                if n.startswith(("pilot_backend", "pilot_runtime"))
                and ".tests" not in n
                for a, v in list(vars(m).items()) if v is FrozenDatetime]
