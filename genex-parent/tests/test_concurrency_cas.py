"""
tests/test_concurrency_cas.py — Beta 2.3 Phase 1

Proves the compare-and-swap loop (session_store.mutate_with_cas) makes two
simultaneous identical /feedback submissions create exactly one feedback record,
one completion, one activity_completed event, one star_awarded event — even when a
save loses the generation race (PreconditionFailedError → reload → replay).

Run: PYTHONPATH=. python3 tests/test_concurrency_cas.py
"""
import os, sys, copy
os.environ.setdefault("FIREBASE_PROJECT_ID", "genex-test")
os.environ.setdefault("LOCAL_SESSION_FALLBACK", "1")
from collections import Counter
from api import session_store as ss
from api import progress as pg

_p = _f = 0
def check(l, ok, d=""):
    global _p, _f
    if ok: _p += 1; print(f"  ✓ {l}")
    else: _f += 1; print(f"  ✗ FAIL: {l} — {d}")


def _make_mutator(session_id, activity_id, local_date):
    """A minimal feedback-style mutator using the same idempotency indexes as the real
    /feedback handler (fb_index + attempt_index + comp_index), producing 1 feedback +
    1 attempt + 1 completion + 3 events on first apply, replay thereafter."""
    fb_key = pg.feedback_req_key(session_id, activity_id, local_date, "did_it",
                                 "loved_it", "just_right", False, None, None, "")
    att_key = pg.attempt_idem_key(session_id, activity_id, local_date)
    comp_key = pg.completion_idem_key(session_id, activity_id, local_date)

    def mutator(d):
        feedback = d.setdefault("feedback", [])
        fb_index = d.setdefault("feedback_index", {})
        attempts = d.setdefault("attempts", [])
        attempt_index = d.setdefault("attempt_index", {})
        completions = d.setdefault("completions", [])
        comp_index = d.setdefault("completion_index", {})
        events = d.setdefault("events", [])
        if fb_key in fb_index:
            return False, {"feedback_id": fb_index[fb_key], "completion_id": comp_index.get(comp_key),
                           "idempotent_replay": True}
        fid = pg.new_id("fb")
        feedback.append({"feedback_id": fid, "activity_id": activity_id,
                         "local_submission_date": local_date, "completion": "did_it"})
        fb_index[fb_key] = fid
        events.append(pg.build_event(session_id=session_id, owner_uid="u", event_type="feedback_recorded",
                      source_record_id=fid, created_at_utc="t", local_event_date=local_date,
                      idempotency_key=fb_key, actor_type="parent", provenance="parent_reported"))
        star_id = pg.new_id("evt")
        att = pg.build_attempt_record(session_id=session_id, owner_uid="u", feedback_id=fid,
                    idempotency_key=att_key, status="did_it", completed_at_utc="t", completion_tz="UTC",
                    tz_source="session", local_date=local_date, date_confidence="high", plan_id="p",
                    scheduled_day="Monday", activity_instance_id=activity_id, source="primary",
                    module_id=None, provenance="original", snapshot={"title": "T"}, star_event_id=star_id)
        attempts.append(att); attempt_index[att_key] = att["attempt_id"]
        events.append(pg.build_event(event_id=star_id, session_id=session_id, owner_uid="u",
                      event_type="star_awarded", source_record_id=att["attempt_id"], created_at_utc="t",
                      local_event_date=local_date, idempotency_key=att_key, actor_type="system",
                      provenance="system_calculated", rule_version=pg.STAR_RULE_VERSION, metadata={"stars_delta": 1}))
        comp = pg.build_completion_record(session_id=session_id, owner_uid="u", feedback_id=fid,
                    idempotency_key=comp_key, completed_at_utc="t", completion_tz="UTC", tz_source="session",
                    local_completion_date=local_date, scheduled_date=local_date, date_confidence="high",
                    data_completeness="complete", plan_id="p", plan_period_id="p", cycle_week=1,
                    scheduled_day="Monday", activity_instance_id=activity_id, activity_template_id=None,
                    source="primary", module_id=None, provenance="original", source_activity_id=None,
                    generated_or_manual="generated", snapshot={"title": "T"}, milestone={"milestone_id": None},
                    attempt_id=att["attempt_id"])
        completions.append(comp); comp_index[comp_key] = comp["completion_id"]
        events.append(pg.build_event(session_id=session_id, owner_uid="u", event_type="activity_completed",
                      source_record_id=comp["completion_id"], created_at_utc="t", local_event_date=local_date,
                      idempotency_key=comp_key, actor_type="parent", provenance="app_recorded"))
        return True, {"feedback_id": fid, "completion_id": comp["completion_id"], "idempotent_replay": False}
    return mutator


class FakeGCS:
    """In-memory store with GCS-style generation semantics + a one-time lost race:
    the first save at generation 1 fails (PreconditionFailed) AFTER a concurrent
    identical writer has already applied its mutation to the stored doc."""
    def __init__(self, doc, racer_mutator):
        self.doc = doc; self.gen = 1; self.saved = 0
        self.racer_mutator = racer_mutator; self.race_pending = True
    def load(self):
        return copy.deepcopy(self.doc), self.gen
    def save(self, doc, generation):
        if self.race_pending and generation == 1:
            # a concurrent identical request wins first: apply its mutation, bump gen, reject us
            self.race_pending = False
            changed, _ = self.racer_mutator(self.doc)
            if changed:
                self.gen += 1
            raise ss.PreconditionFailedError("simulated lost race")
        if generation != self.gen:
            raise ss.PreconditionFailedError("stale generation")
        self.doc = doc; self.gen += 1; self.saved += 1


def test_cas_single_write_under_race():
    print("\n── two identical writers race → exactly one of each (CAS)")
    base = {"session_id": "s"}
    mut_racer = _make_mutator("s", "act-1", "2026-07-08")
    mut_ours = _make_mutator("s", "act-1", "2026-07-08")   # identical fingerprint
    store = FakeGCS(base, mut_racer)
    doc, changed, result = ss.mutate_with_cas(store.load, store.save, mut_ours)
    et = Counter(e["type"] for e in store.doc.get("events", []))
    check("our request became a replay (racer won)", result["idempotent_replay"] is True, result)
    check("exactly 1 feedback record", len(store.doc["feedback"]) == 1, len(store.doc["feedback"]))
    check("exactly 1 completion record", len(store.doc["completions"]) == 1, len(store.doc["completions"]))
    check("exactly 3 events total", sum(et.values()) == 3, dict(et))
    check("1 feedback_recorded + 1 activity_completed + 1 star_awarded",
          et["feedback_recorded"] == 1 and et["activity_completed"] == 1 and et["star_awarded"] == 1, dict(et))
    check("1 completion_index + 1 feedback_index", len(store.doc["completion_index"]) == 1 and len(store.doc["feedback_index"]) == 1)
    check("our save did not persist a duplicate", store.saved == 0, store.saved)


def test_cas_normal_write_no_conflict():
    print("\n── no conflict → single clean write")
    store = FakeGCS({"session_id": "s"}, _make_mutator("s", "act-9", "2026-07-08"))
    store.race_pending = False   # no race this time
    mut = _make_mutator("s", "act-2", "2026-07-08")
    _doc, changed, result = ss.mutate_with_cas(store.load, store.save, mut)
    et = Counter(e["type"] for e in store.doc.get("events", []))
    check("changed + not replay", changed is True and result["idempotent_replay"] is False)
    check("1 feedback / 1 completion / 3 events", len(store.doc["feedback"]) == 1
          and len(store.doc["completions"]) == 1 and sum(et.values()) == 3, dict(et))
    check("exactly one save", store.saved == 1, store.saved)


def test_sequential_duplicate_is_replay():
    print("\n── sequential duplicate on same store → replay, no new records")
    store = FakeGCS({"session_id": "s"}, _make_mutator("s", "act-x", "2026-07-08"))
    store.race_pending = False
    m1 = _make_mutator("s", "act-3", "2026-07-08")
    m2 = _make_mutator("s", "act-3", "2026-07-08")  # identical
    ss.mutate_with_cas(store.load, store.save, m1)
    _d, changed, result = ss.mutate_with_cas(store.load, store.save, m2)
    check("second is replay", result["idempotent_replay"] is True and changed is False)
    check("still 1 feedback / 1 completion", len(store.doc["feedback"]) == 1 and len(store.doc["completions"]) == 1)
    check("still exactly 1 save", store.saved == 1, store.saved)


def run_all():
    test_cas_single_write_under_race()
    test_cas_normal_write_no_conflict()
    test_sequential_duplicate_is_replay()
    print(f"\n{'='*50}\nResults: {_p} passed, {_f} failed")
    if _f: print("❌ concurrency CAS FAILED"); sys.exit(1)
    print("✅ All concurrency CAS tests PASSED")

if __name__ == "__main__":
    run_all()
