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
    # 3 did_it (one with metadata_found+title=complete, one partial, one bad-timestamp=unavailable)
    # + 1 not_yet (ignored). No pre-existing completions/index (pre-Phase-1 shape).
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
    check("did_it examined == 3", stats["feedback_examined"] == 3, stats)
    check("complete == 1", stats["complete"] == 1, stats)
    check("partial == 1", stats["partial"] == 1, stats)
    check("unavailable == 1 (bad timestamp)", stats["unavailable"] == 1, stats)
    check("stars_proposed == 2 (unavailable excluded)", stats["stars_proposed"] == 2, stats)
    check("changed False in dry-run", changed is False)
    check("doc UNCHANGED by dry-run", doc == before)


def test_apply_creates_completions_and_stars():
    print("\n── apply: creates completions + migration stars")
    doc = _legacy_doc()
    stats, changed = backfill_doc(doc, dry_run=False)
    check("changed True", changed is True)
    check("2 completions created", len(doc["completions"]) == 2, len(doc.get("completions", [])))
    check("all backfilled+migration provenance", all(c.get("backfilled") for c in doc["completions"]))
    ev = [e for e in doc["events"] if e["type"] == "star_awarded"]
    check("2 migration star events", len(ev) == 2 and all(e["actor_type"] == "migration" and e["provenance"] == "migrated" for e in ev), ev)
    check("completion_index has 2", len(doc["completion_index"]) == 2)
    # all-time stars via progress
    prog = pg.build_progress_response(session_id="s1", timezone_str="America/Los_Angeles",
                                      completions=doc["completions"])
    check("all-time stars == 2", prog["stars"]["all_time"] == 2, prog["stars"])
    # data completeness distinguished
    comp = {c["activity_instance_id"]: c["data_completeness"] for c in doc["completions"]}
    check("a1 complete, a2 partial", comp.get("a1") == "complete" and comp.get("a2") == "partial", comp)


def test_idempotent_rerun():
    print("\n── idempotent: second apply skips all")
    doc = _legacy_doc()
    backfill_doc(doc, dry_run=False)
    snapshot = copy.deepcopy(doc)
    stats2, changed2 = backfill_doc(doc, dry_run=False)
    check("second run skips 2 existing", stats2["skipped_existing"] == 2, stats2)
    check("second run stars_proposed 0", stats2["stars_proposed"] == 0, stats2)
    check("second run changed False", changed2 is False)
    check("doc unchanged by second run", doc["completions"] == snapshot["completions"] and doc["events"] == snapshot["events"])


def test_low_confidence_excluded_from_circles():
    print("\n── missing/invalid tz → low confidence, excluded from week circles")
    doc = _legacy_doc(tz="Not/AZone")     # invalid → default_utc, low confidence
    backfill_doc(doc, dry_run=False)
    check("completions marked low confidence", all(c["date_confidence"] == "low" for c in doc["completions"]))
    check("still count toward all-time stars",
          pg.build_progress_response(session_id="s1", timezone_str="Not/AZone", completions=doc["completions"])["stars"]["all_time"] == 2)
    # week circles must NOT show low-confidence dates
    from datetime import datetime, timezone
    week = pg.compute_week(doc["completions"], "2026-07-08")
    check("no low-confidence date shown as practiced",
          all(d["status"] == "no_practice" for d in week), [d["status"] for d in week])


def run_all():
    test_dry_run_counts_no_mutation()
    test_apply_creates_completions_and_stars()
    test_idempotent_rerun()
    test_low_confidence_excluded_from_circles()
    print(f"\n{'='*50}\nResults: {_p} passed, {_f} failed")
    if _f: print("❌ backfill FAILED"); sys.exit(1)
    print("✅ All backfill tests PASSED")

if __name__ == "__main__":
    run_all()
