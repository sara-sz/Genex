"""
tests/test_timezone_week.py — Beta 2.3 Phase 1: timezone-safe local dates + week model.

Pure unit tests over api.progress (no HTTP). Covers UTC↔local + Monday/Sunday
boundaries, DST transitions, non-UTC timezones, invalid/missing tz → low confidence,
7-entry week, and star bucketing.

Run: PYTHONPATH=. python3 tests/test_timezone_week.py
"""
import os, sys
os.environ.setdefault("FIREBASE_PROJECT_ID", "genex-test")
os.environ.setdefault("LOCAL_SESSION_FALLBACK", "1")
from datetime import datetime, timezone
from api import progress as pg

_p = _f = 0
def check(l, ok, d=""):
    global _p, _f
    if ok: _p += 1; print(f"  ✓ {l}")
    else: _f += 1; print(f"  ✗ FAIL: {l} — {d}")
def U(s): return datetime.fromisoformat(s.replace("Z", "+00:00"))


def test_validate_tz():
    print("\n── timezone validation")
    check("valid IANA → session", pg.validate_timezone("America/Los_Angeles") == ("America/Los_Angeles", "session"))
    check("UTC → session", pg.validate_timezone("UTC") == ("UTC", "session"))
    check("invalid → default_utc", pg.validate_timezone("Not/AZone") == ("UTC", "default_utc"))
    check("empty → default_utc", pg.validate_timezone("") == ("UTC", "default_utc"))
    check("None → default_utc", pg.validate_timezone(None) == ("UTC", "default_utc"))


def test_local_date_boundary():
    print("\n── UTC↔local date boundary")
    # 2026-07-08 03:00 UTC = 2026-07-07 20:00 PDT → previous local day
    check("LA rolls back across midnight", pg.local_date_for(U("2026-07-08T03:00:00Z"), "America/Los_Angeles") == "2026-07-07")
    # 2026-07-08 09:00 UTC = 2026-07-08 18:00 JST → same/next
    check("Tokyo ahead", pg.local_date_for(U("2026-07-08T09:00:00Z"), "Asia/Tokyo") == "2026-07-08")
    check("Tokyo next-day at 16:00Z", pg.local_date_for(U("2026-07-08T16:00:00Z"), "Asia/Tokyo") == "2026-07-09")
    check("UTC identity", pg.local_date_for(U("2026-07-08T12:00:00Z"), "UTC") == "2026-07-08")


def test_week_bounds_monday_sunday():
    print("\n── Monday–Sunday week bounds")
    check("Wed → Mon..Sun", pg.week_bounds("2026-07-08") == ("2026-07-06", "2026-07-12"))
    check("Monday itself", pg.week_bounds("2026-07-06") == ("2026-07-06", "2026-07-12"))
    check("Sunday itself", pg.week_bounds("2026-07-12") == ("2026-07-06", "2026-07-12"))
    check("Sunday→next Monday boundary", pg.week_bounds("2026-07-13")[0] == "2026-07-13")


def test_dst_transition():
    print("\n── daylight-saving transitions (US spring-forward / fall-back)")
    # US DST 2026: spring forward Mar 8, fall back Nov 1.
    check("day before spring-forward", pg.local_date_for(U("2026-03-08T06:00:00Z"), "America/Los_Angeles") == "2026-03-07")
    check("after spring-forward (PDT)", pg.local_date_for(U("2026-03-08T19:00:00Z"), "America/Los_Angeles") == "2026-03-08")
    check("around fall-back", pg.local_date_for(U("2026-11-01T07:30:00Z"), "America/Los_Angeles") == "2026-11-01")
    # a UTC instant near midnight local across DST still yields a correct single local date
    check("fall-back local date stable", pg.local_date_for(U("2026-11-01T08:30:00Z"), "America/Los_Angeles") == "2026-11-01")


def test_week_array_and_status():
    print("\n── /progress week: 7 entries, no daily_plan_completed in Phase 1")
    # attempts (effort) drive circles + stars
    atts = [
        {"local_date": "2026-07-06", "idempotency_key": "a"},
        {"local_date": "2026-07-08", "idempotency_key": "b"},
        {"local_date": "2026-07-08", "idempotency_key": "c"},
        {"local_date": "2026-07-01", "idempotency_key": "d"},  # prior week
        {"local_date": "2026-07-09", "date_confidence": "low", "idempotency_key": "e"},  # excluded from circles
    ]
    resp = pg.build_progress_response(session_id="s", timezone_str="America/Los_Angeles",
                                      attempts=atts, now_utc=U("2026-07-08T19:00:00Z"))
    week = resp["week"]
    check("exactly 7 days", len(week) == 7, len(week))
    check("ordered Monday→Sunday", [d["day"] for d in week] ==
          ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"])
    by = {d["date"]: d for d in week}
    check("Mon practiced", by["2026-07-06"]["status"] == "practiced")
    check("Wed (today) practiced + is_today", by["2026-07-08"]["status"] == "practiced" and by["2026-07-08"]["is_today"])
    check("Tue no_practice", by["2026-07-07"]["status"] == "no_practice")
    check("low-confidence Thu excluded from circles", by["2026-07-09"]["status"] == "no_practice")
    statuses = {d["status"] for d in week}
    check("never emits daily_plan_completed", "daily_plan_completed" not in statuses, statuses)
    check("statuses ⊆ {no_practice, practiced}", statuses <= {"no_practice", "practiced"}, statuses)


def test_stars_weekly_and_alltime():
    print("\n── stars (effort attempts): weekly bucket + all-time + dedupe")
    atts = [
        {"local_date": "2026-07-06", "idempotency_key": "k1"},
        {"local_date": "2026-07-08", "idempotency_key": "k2"},
        {"local_date": "2026-07-08", "idempotency_key": "k2"},  # dup key → not double counted
        {"local_date": "2026-06-30", "idempotency_key": "k3"},  # prior week
    ]
    resp = pg.build_progress_response(session_id="s", timezone_str="America/Los_Angeles",
                                      attempts=atts, now_utc=U("2026-07-08T19:00:00Z"))
    check("all_time dedup (3 unique)", resp["stars"]["all_time"] == 3, resp["stars"])
    check("this_week only current Mon–Sun (2)", resp["stars"]["this_week"] == 2, resp["stars"])
    check("future arrays present + empty",
          resp["latest_wins"] == [] and resp["badges"] == [] and resp["milestones_in_practice"] == []
          and resp["checkins_ready"] == [] and resp["cups_by_domain"] == [])
    check("schema version + timezone echoed", resp["progress_schema_version"] == 1 and resp["timezone"] == "America/Los_Angeles")


def run_all():
    test_validate_tz(); test_local_date_boundary(); test_week_bounds_monday_sunday()
    test_dst_transition(); test_week_array_and_status(); test_stars_weekly_and_alltime()
    print(f"\n{'='*50}\nResults: {_p} passed, {_f} failed")
    if _f: print("❌ timezone/week FAILED"); sys.exit(1)
    print("✅ All timezone/week tests PASSED")

if __name__ == "__main__":
    run_all()
