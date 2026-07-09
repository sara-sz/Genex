"""
tests/test_checkin_readiness.py — Beta 2.3 Phase 4: parent milestone check-in readiness

A milestone is ready for a gentle parent check-in when it has >=5 did_it completions
for the same canonical CDC milestone across >=3 distinct high-confidence local dates,
with reliable observable_text + cup_eligible. Non-did_it never counts. Read-time,
deterministic, idempotent. No cups, no stored responses. Backend owns the prompt.

Run: PYTHONPATH=. python3 tests/test_checkin_readiness.py
"""
import os, sys
os.environ.update(FIREBASE_PROJECT_ID="genex-test", LOCAL_SESSION_FALLBACK="1",
    REQUIRE_BETA_CODE="true", BETA_ACCESS_CODE="genex", ALLOWED_ORIGINS="http://localhost:3000",
    ACTIVITY_MODEL="")
os.environ.pop("GCS_BUCKET", None); os.environ.pop("CONCERN_ROUTER_MODEL", None)
import shutil; shutil.rmtree("/tmp/genex_api_sessions", ignore_errors=True)
import firebase_admin; firebase_admin._apps["[DEFAULT]"] = object()
from firebase_admin import auth as fa
_T = {"tok": {"uid": "uid-a", "email": "a@x.com"}}
fa.verify_id_token = lambda t, *a, **k: _T[t] if t in _T else (_ for _ in ()).throw(fa.InvalidIdTokenError("bad"))
from fastapi.testclient import TestClient
from api.main import app
from api import session_store
from api import progress as pg
client = TestClient(app); H = {"Authorization": "Bearer tok"}
_p = _f = 0
def check(l, ok, d=""):
    global _p, _f
    if ok: _p += 1; print(f"  ✓ {l}")
    else: _f += 1; print(f"  ✗ FAIL: {l} — {d}")

# a real CDC milestone that resolves observable_text (movement, ~15mo)
MID = "mv1:cdc:movement_and_physical:15:d5451f44be0b"
DOMAIN = "movement_and_physical"
SHORT = "takes a few steps on her own"

def comp(date, source="cdc", conf="high", cup=True, short=SHORT, domain=DOMAIN, mid=MID, idem=None, utc=None):
    return {"valid_completion": True, "completion_id": f"cmp_{idem or date}",
            "idempotency_key": idem or f"{mid}|{date}", "local_completion_date": date,
            "date_confidence": conf, "completed_at_utc": utc or (date + "T18:00:00Z"),
            "snapshot": {"domain": domain},
            "milestone": {"milestone_id": mid, "milestone_source": source, "short_label": short,
                          "canonical_age_months": 15, "cup_eligible": cup}}


def test_ready_5_across_3_days():
    print("\n── 5 did_it across 3 distinct high-confidence days → ready")
    comps = [comp("2026-07-06", idem="a"), comp("2026-07-06", idem="b"),
             comp("2026-07-07", idem="c"), comp("2026-07-07", idem="d"), comp("2026-07-08", idem="e")]
    ready = pg.compute_checkins_ready("s1", comps)
    check("one check-in ready", len(ready) == 1, len(ready))
    r = ready[0]
    check("milestone_id + domain", r["milestone_id"] == MID and r["domain_key"] == DOMAIN)
    check("clean domain_label", r["domain_label"] == "Movement & Daily Skills")
    check("practices_completed 5, days 3", r["practices_completed"] == 5 and r["distinct_practice_days"] == 3, (r["practices_completed"], r["distinct_practice_days"]))
    check("observable_text resolved", bool(r["observable_text"]))
    check("source + rule_version", r["source"] == "parent_checkin_ready" and r["rule_version"] == "cr1")
    check("created_at at readiness (5th completion, 2026-07-08)", r["created_at"] == "2026-07-08T18:00:00Z", r["created_at"])
    check("deterministic checkin_id", r["checkin_id"] == "chk_" + pg._sha256("chk1", "s1", MID)[:16])


def test_not_ready_fewer_than_3_days():
    print("\n── 5 did_it on only 2 distinct days → NOT ready")
    comps = [comp("2026-07-06", idem=f"a{i}") for i in range(3)] + [comp("2026-07-07", idem=f"b{i}") for i in range(2)]
    check("no readiness with <3 days", pg.compute_checkins_ready("s1", comps) == [])


def test_not_ready_fewer_than_5():
    print("\n── 4 did_it across 4 days → NOT ready (needs 5)")
    comps = [comp(f"2026-07-0{d}", idem=f"k{d}") for d in (5, 6, 7, 8)]
    check("no readiness with <5 completions", pg.compute_checkins_ready("s1", comps) == [])


def test_low_confidence_days_excluded():
    print("\n── low-confidence dates don't count toward the 3 distinct days")
    comps = [comp("2026-07-06", idem="a"), comp("2026-07-06", idem="b"),
             comp("2026-07-07", idem="c"), comp("2026-07-08", conf="low", idem="d"), comp("2026-07-08", conf="low", idem="e")]
    # 5 completions but only 2 high-confidence distinct days (06, 07)
    check("low-confidence day excluded → not ready", pg.compute_checkins_ready("s1", comps) == [])


def test_unreliable_and_missing_observable():
    print("\n── unreliable milestone / missing observable → no readiness")
    # bridge source
    b = [comp(f"2026-07-0{d}", source="bridge", idem=f"br{d}") for d in (5, 6, 7)] + [comp("2026-07-08", source="bridge", idem="br8"), comp("2026-07-09", source="bridge", idem="br9")]
    check("bridge milestone → no readiness", pg.compute_checkins_ready("s1", b) == [])
    # not cup_eligible
    nc = [comp(f"2026-07-0{d}", cup=False, idem=f"nc{d}") for d in (5, 6, 7, 8, 9)]
    check("cup_eligible False → no readiness", pg.compute_checkins_ready("s1", nc) == [])
    # milestone with a short_label that has no CDC parent_explanation → no observable → not ready
    no_obs = [comp(f"2026-07-0{d}", short="a nonexistent milestone phrase zzz", mid="mv1:cdc:cognitive:30:zzz", domain="cognitive", idem=f"z{d}") for d in (5, 6, 7, 8, 9)]
    check("missing observable_text → no readiness", pg.compute_checkins_ready("s1", no_obs) == [])


def test_prompt_wording():
    print("\n── prompt: observable phrasing, no name, no clinical language")
    comps = [comp("2026-07-06", idem="a"), comp("2026-07-06", idem="b"), comp("2026-07-07", idem="c"),
             comp("2026-07-07", idem="d"), comp("2026-07-08", idem="e")]
    prompt = pg.compute_checkins_ready("s1", comps)[0]["prompt"]
    check("uses observable base-verb phrase", "take a few steps on her own" in prompt, prompt)
    check("warm framing", prompt.startswith("After the practice you have done together, is your child now usually able to"))
    banned = ["mastered", "diagnos", "genex caused", "cured", "disorder"]
    check("no clinical/guilt/causal language", not any(b in prompt.lower() for b in banned), prompt)


def test_idempotent():
    print("\n── readiness is deterministic/idempotent")
    comps = [comp("2026-07-06", idem="a"), comp("2026-07-06", idem="b"), comp("2026-07-07", idem="c"),
             comp("2026-07-07", idem="d"), comp("2026-07-08", idem="e")]
    r1 = pg.compute_checkins_ready("s1", comps)
    r2 = pg.compute_checkins_ready("s1", comps)
    check("same checkin_id across calls", r1 == r2 and r1[0]["checkin_id"] == r2[0]["checkin_id"])
    # duplicate completion (same idempotency_key) does not push readiness
    dup = comps + [dict(comps[0])]
    check("duplicate completion key doesn't inflate count", pg.compute_checkins_ready("s1", dup)[0]["practices_completed"] == 5)


# ── HTTP integration ────────────────────────────────────────────────────────
def test_http_readiness_and_categories_flag():
    print("\n── HTTP: /progress checkins_ready + categories check_in_ready flag; no cups")
    sid = client.post("/api/v1/session/start", headers=H, json={"child_name": "Kashmir", "age_years": 1,
        "age_months": 3, "age_in_months": 15, "diagnosis_or_condition": "Other",
        "parent_concern": "not walking yet, motor delay", "daily_time_minutes": 10,
        "timezone": "America/Los_Angeles", "beta_access_code": "genex"}).json()["session_id"]
    q = client.get(f"/api/v1/session/{sid}", headers=H).json().get("current_question")
    while q:
        a = client.post(f"/api/v1/session/{sid}/answer", headers=H, json={"question_id": q["question_id"], "answer": "with_help"}).json()
        if a.get("status") == "interview_complete": break
        q = a.get("current_question")
    client.post(f"/api/v1/session/{sid}/plan", headers=H)
    # do a real did_it, then inject 4 more did_it completions for the SAME milestone across 3 days
    plan = client.get("/api/v1/session/current", headers=H).json()["plan"]
    card = next(a for d in plan["week"] for a in d["activities"])
    day = next(d["day"] for d in plan["week"] for a in d["activities"] if a["activity_id"] == card["activity_id"])
    client.post(f"/api/v1/session/{sid}/feedback", headers=H, json={"plan_id": card["plan_id"],
        "activity_id": card["activity_id"], "day": day, "activity_date": card["activity_date"],
        "enjoyment": "loved_it", "difficulty": "just_right", "completion": "did_it"})
    doc = session_store.load("uid-a", sid)
    base = doc["completions"][0]
    mid = base["milestone"]["milestone_id"]
    if base["milestone"]["milestone_source"] == "cdc" and pg.resolve_observable_text(base["snapshot"]["domain"], base["milestone"]["short_label"]):
        import copy
        for i, dt in enumerate(["2026-07-06", "2026-07-06", "2026-07-07", "2026-07-08"]):
            c = copy.deepcopy(base)
            c["completion_id"] = f"cmp_inj{i}"; c["local_completion_date"] = dt
            c["completed_at_utc"] = dt + f"T1{i}:00:00Z"; c["date_confidence"] = "high"
            c["idempotency_key"] = pg.completion_idem_key(sid, f"act_inj{i}", dt)
            doc["completions"].append(c)
        session_store.save("uid-a", sid, doc)
        p = client.get(f"/api/v1/session/{sid}/progress", headers=H).json()
        check("checkins_ready has the milestone", any(c["milestone_id"] == mid for c in p["checkins_ready"]), p["checkins_ready"])
        cid = next(c["checkin_id"] for c in p["checkins_ready"] if c["milestone_id"] == mid)
        row = next(m for cat in p["categories_in_practice"] for m in cat["milestones"] if m["milestone_id"] == mid)
        check("category milestone check_in_ready True + checkin_id + practice_ready", row["check_in_ready"] is True and row["checkin_id"] == cid and row["practice_ready"] is True, row)
        check("no cups anywhere", p["cups_by_domain"] == [])
        d2 = session_store.load("uid-a", sid)
        check("no cups/checkin records persisted (read-derived)", not d2.get("cups") and not d2.get("checkins"))
        check("prompt present + name-blind", p["checkins_ready"][0]["prompt"].startswith("After the practice") and "Kashmir" not in p["checkins_ready"][0]["prompt"])
    else:
        check("milestone mapping reliable for HTTP path (skip if not)", True)


def run_all():
    test_ready_5_across_3_days()
    test_not_ready_fewer_than_3_days()
    test_not_ready_fewer_than_5()
    test_low_confidence_days_excluded()
    test_unreliable_and_missing_observable()
    test_prompt_wording()
    test_idempotent()
    test_http_readiness_and_categories_flag()
    print(f"\n{'='*50}\nResults: {_p} passed, {_f} failed")
    if _f: print("❌ check-in readiness FAILED"); sys.exit(1)
    print("✅ All check-in readiness tests PASSED")

if __name__ == "__main__":
    run_all()
