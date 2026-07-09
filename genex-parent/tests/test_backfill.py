"""
tests/test_backfill.py — Beta 2.3 Phase 1: staging backfill of legacy feedback.

Verifies backfill_doc: dry-run counts without mutating; apply creates completions +
migration stars; idempotent re-run skips all; low-confidence dates excluded from
week circles; never fabricates dates for unparseable records.

Run: PYTHONPATH=. python3 tests/test_backfill.py
"""
import os, sys, copy
os.environ.setdefault("FIREBASE_PROJECT_ID", "genex-test")
os.environ.setdefault("LOCAL_SESSION_FALLBACK", "1")
from api.backfill import backfill_doc
from api import progress as pg

_p = _f = 0
def check(l, ok, d=""):
    global _p, _f
    if ok: _p += 1; print(f"  ✓ {l}")
    else: _f += 1; print(f"  ✗ FAIL: {l} — {d}")


def _legacy_doc(tz="America/Los_Angeles"):
    # eligible feedback: 2 did_it (complete, partial) + 1 did_it bad-timestamp (unavailable)
    # + 1 not_ready (now earns an EFFORT star, no completion). Pre-Phase-1 shape.
    return {
        "session_id": "s1", "owner_uid": "uid-a", "timezone": tz,
        "feedback": [
            {"feedback_id": "f1", "activity_id": "a1", "created_at": "2026-07-08T19:00:00Z",
             "activity_date": "2026-07-08", "completion": "did_it", "metadata_found": True,
             "title": "Book Naming Fun!", "domain": "language_and_communication",
             "milestone_text": "Says 50 words", "milestone_age_months": 30, "source": "primary"},
            {"feedback_id": "f2", "activity_id": "a2", "created_at": "2026-07-09T02:00:00Z",
             "activity_date": "2026-07-08", "completion": "did_it", "metadata_found": False,
             "domain": "cognitive", "source": "primary"},
            {"feedback_id": "f3", "activity_id": "a3", "created_at": "not-a-timestamp",
             "activity_date": "2026-07-08", "completion": "did_it", "source": "primary"},
            {"feedback_id": "f4", "activity_id": "a4", "created_at": "2026-07-08T20:00:00Z",
             "activity_date": "2026-07-08", "completion": "wasnt_ready_yet"},
        ],
    }


def test_dry_run_counts_no_mutation():
    print("\n── dry-run: counts, no mutation")
    doc = _legacy_doc()
    before = copy.deepcopy(doc)
    stats, changed = backfill_doc(doc, dry_run=True)
    check("eligible examined == 4 (3 did_it + 1 not_ready)", stats["feedback_examined"] == 4, stats)
    check("unavailable == 1 (bad timestamp)", stats["unavailable"] == 1, stats)
    check("stars_proposed == 3 (attempts; unavailable excluded)", stats["stars_proposed"] == 3, stats)
    check("completions_proposed == 2 (did_it only)", stats["completions_proposed"] == 2, stats)
    check("complete == 1, partial == 2", stats["complete"] == 1 and stats["partial"] == 2, stats)
    check("changed False in dry-run", changed is False)
    check("doc UNCHANGED by dry-run", doc == before)


def test_apply_creates_attempts_completions_stars():
    print("\n── apply: 3 effort stars (attempts) + 2 completions (did_it only)")
    doc = _legacy_doc()
    stats, changed = backfill_doc(doc, dry_run=False)
    check("changed True", changed is True)
    check("3 attempts (effort stars)", len(doc["attempts"]) == 3, len(doc.get("attempts", [])))
    check("2 completions (did_it only)", len(doc["completions"]) == 2, len(doc.get("completions", [])))
    check("not_ready attempt has NO completion (outcome not_ready)",
          any(a["outcome"] == "not_ready" and not a["is_completion"] for a in doc["attempts"]))
    ev = [e for e in doc["events"] if e["type"] == "star_awarded"]
    check("3 migration star events", len(ev) == 3 and all(e["actor_type"] == "migration" for e in ev), len(ev))
    check("all backfilled", all(a.get("backfilled") for a in doc["attempts"]) and all(c.get("backfilled") for c in doc["completions"]))
    prog = pg.build_progress_response(session_id="s1", timezone_str="America/Los_Angeles",
                                      attempts=doc["attempts"], completions=doc["completions"])
    check("all-time stars == 3 (effort)", prog["stars"]["all_time"] == 3, prog["stars"])
    comp = {c["activity_instance_id"]: c["data_completeness"] for c in doc["completions"]}
    check("a1 complete, a2 partial", comp.get("a1") == "complete" and comp.get("a2") == "partial", comp)


def test_idempotent_rerun():
    print("\n── idempotent: second apply skips all")
    doc = _legacy_doc()
    backfill_doc(doc, dry_run=False)
    snapshot = copy.deepcopy(doc)
    stats2, changed2 = backfill_doc(doc, dry_run=False)
    check("second run skips 3 existing attempts", stats2["skipped_existing"] == 3, stats2)
    check("second run stars_proposed 0 + completions_proposed 0", stats2["stars_proposed"] == 0 and stats2["completions_proposed"] == 0, stats2)
    check("second run changed False", changed2 is False)
    check("doc unchanged by second run",
          doc["attempts"] == snapshot["attempts"] and doc["completions"] == snapshot["completions"] and doc["events"] == snapshot["events"])


def test_low_confidence_excluded_from_circles():
    print("\n── missing/invalid tz → low confidence, excluded from week circles")
    doc = _legacy_doc(tz="Not/AZone")     # invalid → default_utc, low confidence
    backfill_doc(doc, dry_run=False)
    check("attempts marked low confidence", all(a["date_confidence"] == "low" for a in doc["attempts"]))
    check("still count toward all-time stars",
          pg.build_progress_response(session_id="s1", timezone_str="Not/AZone", attempts=doc["attempts"])["stars"]["all_time"] == 3)
    week = pg.compute_week(doc["attempts"], "2026-07-08")
    check("no low-confidence date shown as practiced",
          all(d["status"] == "no_practice" for d in week), [d["status"] for d in week])


def run_all():
    test_dry_run_counts_no_mutation()
    test_apply_creates_attempts_completions_stars()
    test_idempotent_rerun()
    test_low_confidence_excluded_from_circles()
    print(f"\n{'='*50}\nResults: {_p} passed, {_f} failed")
    if _f: print("❌ backfill FAILED"); sys.exit(1)
    print("✅ All backfill tests PASSED")

if __name__ == "__main__":
    run_all()
