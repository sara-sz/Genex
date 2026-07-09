"""
tests/test_badges.py — Beta 2.3 Phase 3: consistency badges

Badges reward family rhythm/effort — a "practice day" is any local date with ≥1
eligible attempt (did_it OR wasnt_ready_yet OR didnt_want_to_try). Earned once,
permanent, from CONSECUTIVE local calendar practice days. No did_it required. No
guilt/missed-day state. latest_wins = recent badges only (no stars, no cups).

Unit tests over api.progress.compute_badges/compute_latest_wins + HTTP integration.
Run: PYTHONPATH=. python3 tests/test_badges.py
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


def att(date, status="did_it", conf="high", idem=None):
    return {"local_date": date, "status": status, "date_confidence": conf,
            "idempotency_key": idem or f"{date}|{status}"}
def ids(badges): return [b["badge_id"] for b in badges]


# ── unit tests ──────────────────────────────────────────────────────────────
def test_first_step():
    print("\n── First Step on the first practice day (any status)")
    check("first_step from one not_ready attempt", ids(pg.compute_badges([att("2026-07-06", "wasnt_ready_yet")])) == ["first_step"])
    check("no badges with no attempts", pg.compute_badges([]) == [])
    b = pg.compute_badges([att("2026-07-06", "didnt_want_to_try")])[0]
    check("earned_at + label + description", b["earned_at"] == "2026-07-06" and b["label"] == "First Step"
          and b["description"] == "For trying your first activity" and b["earned"] is True, b)


def test_consecutive_streaks():
    print("\n── consecutive-day streak thresholds")
    days3 = [att(f"2026-07-0{d}") for d in (6, 7, 8)]
    check("3 consecutive → First Step + 3-Day Rhythm", ids(pg.compute_badges(days3)) == ["first_step", "three_day_rhythm"])
    days7 = [att(f"2026-07-{d:02d}") for d in range(6, 13)]
    check("7 consecutive → up to 7-Day Streak", ids(pg.compute_badges(days7)) == ["first_step", "three_day_rhythm", "seven_day_streak"])
    days14 = [att(f"2026-07-{d:02d}") for d in range(6, 20)]
    check("14 consecutive → up to 14", "fourteen_day_streak" in ids(pg.compute_badges(days14)) and "thirty_day_streak" not in ids(pg.compute_badges(days14)))
    days30 = [att((__import__("datetime").date(2026, 7, 1) + __import__("datetime").timedelta(days=i)).isoformat()) for i in range(30)]
    check("30 consecutive → all five", ids(pg.compute_badges(days30)) == ["first_step", "three_day_rhythm", "seven_day_streak", "fourteen_day_streak", "thirty_day_streak"])
    check("30-day earned_at is the 30th day", pg.compute_badges(days30)[-1]["earned_at"] == "2026-07-30")


def test_non_consecutive_no_streak():
    print("\n── non-consecutive days do NOT earn a streak (but First Step does)")
    gappy = [att("2026-07-06"), att("2026-07-08"), att("2026-07-10")]   # 3 days, none consecutive
    check("First Step only", ids(pg.compute_badges(gappy)) == ["first_step"], ids(pg.compute_badges(gappy)))


def test_multiple_attempts_one_day():
    print("\n── multiple attempts same day = ONE practice day")
    same = [att("2026-07-06", "did_it", idem="a"), att("2026-07-06", "wasnt_ready_yet", idem="b"),
            att("2026-07-06", "didnt_want_to_try", idem="c")]
    check("only First Step (1 practice day, not 3)", ids(pg.compute_badges(same)) == ["first_step"])


def test_non_didit_counts():
    print("\n── badges do NOT require did_it — all eligible statuses count")
    days = [att("2026-07-06", "wasnt_ready_yet"), att("2026-07-07", "didnt_want_to_try"), att("2026-07-08", "wasnt_ready_yet")]
    check("3 consecutive non-did_it days → 3-Day Rhythm", "three_day_rhythm" in ids(pg.compute_badges(days)))


def test_gap_keeps_earned_badge():
    print("\n── a gap ends the current streak but never removes an earned badge")
    # 3 in a row (earn 3-Day Rhythm), then a gap, then isolated days
    days = [att("2026-07-06"), att("2026-07-07"), att("2026-07-08"), att("2026-07-20"), att("2026-07-25")]
    check("3-Day Rhythm still present after later gaps", "three_day_rhythm" in ids(pg.compute_badges(days)))
    check("earned_at frozen at first achievement", next(b for b in pg.compute_badges(days) if b["badge_id"] == "three_day_rhythm")["earned_at"] == "2026-07-08")


def test_low_confidence_excluded():
    print("\n── low-confidence dates excluded from streaks")
    days = [att("2026-07-06"), att("2026-07-07", conf="low"), att("2026-07-08")]  # middle day unreliable
    check("run broken by excluded low-confidence day → no 3-Day Rhythm", "three_day_rhythm" not in ids(pg.compute_badges(days)))


def test_latest_wins():
    print("\n── latest_wins = recent badges only (no stars, no cups)")
    days = [att(f"2026-07-0{d}") for d in (6, 7, 8)]
    badges = pg.compute_badges(days)
    wins = pg.compute_latest_wins(badges)
    check("all wins are badges", wins and all(w["type"] == "badge" for w in wins))
    check("most recent badge first", wins[0]["earned_at"] >= wins[-1]["earned_at"])
    check("no star/cup entries", all(w["type"] == "badge" for w in wins))


# ── HTTP integration ────────────────────────────────────────────────────────
def _bootstrap():
    sid = client.post("/api/v1/session/start", headers=H, json={"child_name": "R", "age_years": 4,
        "age_months": 0, "age_in_months": 48, "diagnosis_or_condition": "ADHD",
        "parent_concern": "ADHD, lack of attention", "daily_time_minutes": 10,
        "timezone": "America/Los_Angeles", "beta_access_code": "genex"}).json()["session_id"]
    q = client.get(f"/api/v1/session/{sid}", headers=H).json().get("current_question")
    while q:
        a = client.post(f"/api/v1/session/{sid}/answer", headers=H, json={"question_id": q["question_id"], "answer": "with_help"}).json()
        if a.get("status") == "interview_complete": break
        q = a.get("current_question")
    client.post(f"/api/v1/session/{sid}/plan", headers=H)
    plan = client.get("/api/v1/session/current", headers=H).json()["plan"]
    return sid, plan


def test_http_badges_from_attempts():
    print("\n── HTTP: badges + latest_wins appear from real attempts")
    sid, plan = _bootstrap()
    card = next(a for d in plan["week"] for a in d["activities"])
    day = next(d["day"] for d in plan["week"] for a in d["activities"] if a["activity_id"] == card["activity_id"])
    # one not_ready attempt (no did_it) → should still earn First Step (badges use attempts)
    client.post(f"/api/v1/session/{sid}/feedback", headers=H, json={"plan_id": card["plan_id"],
        "activity_id": card["activity_id"], "day": day, "activity_date": card["activity_date"],
        "enjoyment": "loved_it", "difficulty": "just_right", "completion": "wasnt_ready_yet"})
    p = client.get(f"/api/v1/session/{sid}/progress", headers=H).json()
    check("First Step earned from a non-did_it attempt", "first_step" in ids(p["badges"]), p["badges"])
    check("latest_wins has the badge (no stars)", p["latest_wins"] and p["latest_wins"][0]["type"] == "badge")
    check("stars still from attempts (1)", p["stars"]["all_time"] == 1)
    total_practice = sum(m["practices_completed"] for c in p["categories_in_practice"] for m in c["milestones"])
    check("milestone practice still 0 (no did_it)", total_practice == 0, total_practice)
    check("no cups / no check-ins", p["cups_by_domain"] == [] and p["checkins_ready"] == [])
    # duplicate attempt (same fingerprint) must not duplicate the badge
    client.post(f"/api/v1/session/{sid}/feedback", headers=H, json={"plan_id": card["plan_id"],
        "activity_id": card["activity_id"], "day": day, "activity_date": card["activity_date"],
        "enjoyment": "loved_it", "difficulty": "just_right", "completion": "wasnt_ready_yet"})
    p2 = client.get(f"/api/v1/session/{sid}/progress", headers=H).json()
    check("duplicate attempt does not duplicate badges", ids(p2["badges"]) == ids(p["badges"]) == ["first_step"])
    d = session_store.load("uid-a", sid)
    check("no badge/cup/checkin records persisted (read-derived)", not d.get("cups") and not d.get("checkins"))


def run_all():
    test_first_step()
    test_consecutive_streaks()
    test_non_consecutive_no_streak()
    test_multiple_attempts_one_day()
    test_non_didit_counts()
    test_gap_keeps_earned_badge()
    test_low_confidence_excluded()
    test_latest_wins()
    test_http_badges_from_attempts()
    print(f"\n{'='*50}\nResults: {_p} passed, {_f} failed")
    if _f: print("❌ badges FAILED"); sys.exit(1)
    print("✅ All badge tests PASSED")

if __name__ == "__main__":
    run_all()
