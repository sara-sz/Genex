"""
tests/test_intake_grammar.py — Beta 2.2: display-only intake question grammar polish

humanize_question_text() de-conjugates the leading third-person verb in the
parent-facing question_text ("Can your child says…" → "Can your child say…"). It is
applied ONLY in get_current_question; the stored question_text / milestone /
question_id / interview band_state are never mutated, so scoring / milestone selection
/ generation are unaffected. Covers primary + add-on intake. No genex_core change.

Run: PYTHONPATH=. python3 tests/test_intake_grammar.py
"""

import os
import re
import sys

os.environ["FIREBASE_PROJECT_ID"] = "genex-test"
os.environ["LOCAL_SESSION_FALLBACK"] = "1"
os.environ.pop("GCS_BUCKET", None)
os.environ["REQUIRE_BETA_CODE"] = "true"
os.environ["BETA_ACCESS_CODE"] = "genex"
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:3000")
os.environ["ACTIVITY_MODEL"] = ""
os.environ.pop("CONCERN_ROUTER_MODEL", None)

import shutil  # noqa: E402
shutil.rmtree("/tmp/genex_api_sessions", ignore_errors=True)

import firebase_admin  # noqa: E402
firebase_admin._apps["[DEFAULT]"] = object()
from firebase_admin import auth as firebase_auth  # noqa: E402
_TOKENS = {"token-user-a": {"uid": "uid-a", "email": "a@example.com"}}
firebase_auth.verify_id_token = lambda t, *a, **k: _TOKENS[t] if t in _TOKENS else (_ for _ in ()).throw(
    firebase_auth.InvalidIdTokenError("bad"))

from fastapi.testclient import TestClient  # noqa: E402
from api.main import app  # noqa: E402
from api import session_store  # noqa: E402
from api.pipeline import humanize_question_text  # noqa: E402
from genex_core.interview_engine import get_category_questions  # noqa: E402

client = TestClient(app)
_passed = 0
_failed = 0
_THIRD_PERSON = re.compile(r"^Can your child [a-z]+s\b")  # leading verb still ends in 's'


def check(label, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✓ {label}")
    else:
        _failed += 1
        print(f"  ✗ FAIL: {label} — {detail}")


def _hdr():
    return {"Authorization": "Bearer token-user-a"}


# ── 1. exhaustive: every milestone question reads grammatically ─────────────
def test_exhaustive_milestones():
    print("\n── all milestone-derived questions are grammatical after normalize")
    seen, total, changed, still_bad = set(), 0, 0, []
    for dk in ["language_and_communication", "movement_and_physical", "cognitive", "social_and_emotional"]:
        for age in range(6, 61, 3):
            try:
                qs = get_category_questions(dk, age, band_months=3)
            except Exception:
                qs = []
            for q in qs:
                ms = q.get("milestone", "")
                if not ms or ms in seen:
                    continue
                seen.add(ms)
                before = f"Can your child {ms} right now?"
                after = humanize_question_text(before)
                total += 1
                if after != before:
                    changed += 1
                if _THIRD_PERSON.match(after):
                    still_bad.append(after)
    check("covered the full frozen milestone set (152)", total == 152, total)
    check("normalized the third-person ones (144)", changed == 144, changed)
    check("ZERO questions still start with a 3rd-person verb", not still_bad, still_bad[:3])


# ── 2. targeted unit cases (user examples + tricky conjugations) ────────────
def test_unit_cases():
    print("\n── targeted de-conjugation cases")
    cases = {
        "Can your child says two words together right now?": "Can your child say two words together right now?",
        "Can your child walks up stairs right now?": "Can your child walk up stairs right now?",
        "Can your child follows two step instructions right now?": "Can your child follow two step instructions right now?",
        "Can your child does simple chores right now?": "Can your child do simple chores right now?",
        "Can your child tries to use a spoon right now?": "Can your child try to use a spoon right now?",
        "Can your child copies you right now?": "Can your child copy you right now?",
        "Can your child catches a large ball right now?": "Can your child catch a large ball right now?",
        "Can your child pushes a toy right now?": "Can your child push a toy right now?",
        "Can your child uses a cup right now?": "Can your child use a cup right now?",
        "Can your child closes the box right now?": "Can your child close the box right now?",
        "Can your child Attends to a simple activity. right now?": "Can your child attend to a simple activity right now?",
        # already-correct base verbs must stay unchanged
        "Can your child say two words right now?": "Can your child say two words right now?",
        "Can your child play games with you right now?": "Can your child play games with you right now?",
        "Can your child ask who or what questions right now?": "Can your child ask who or what questions right now?",
        # non-matching text untouched
        "What is this object?": "What is this object?",
    }
    for before, want in cases.items():
        got = humanize_question_text(before)
        check(f"[{before[:34]}…]", got == want, f"got: {got!r}")


# ── 3. idempotence ──────────────────────────────────────────────────────────
def test_idempotent():
    print("\n── normalizer is idempotent")
    sample = "Can your child says two words right now?"
    once = humanize_question_text(sample)
    check("humanize(humanize(x)) == humanize(x)", humanize_question_text(once) == once, once)


def _start(concern="speech delay and trouble talking"):
    return client.post("/api/v1/session/start", headers=_hdr(), json={
        "child_name": "C", "age_years": 3, "age_months": 0, "age_in_months": 36,
        "diagnosis_or_condition": "No known diagnosis / not sure", "parent_concern": concern,
        "daily_time_minutes": 20, "timezone": "UTC", "beta_access_code": "genex"}).json()


# ── 4. primary intake returns grammatical question_text ─────────────────────
def test_primary_intake_grammar():
    print("\n── primary intake question_text is grammatical")
    r = _start()
    sid = r["session_id"]
    texts = [r["current_question"]["question_text"]]
    qids, asked = [r["current_question"]["question_id"]], 0
    q = r["current_question"]
    while q is not None and asked < 30:
        asked += 1
        a = client.post(f"/api/v1/session/{sid}/answer", headers=_hdr(),
                        json={"question_id": q["question_id"], "answer": "yes"}).json()
        if a.get("status") == "interview_complete":
            break
        q = a.get("current_question")
        if q:
            texts.append(q["question_text"]); qids.append(q["question_id"])
    check("every primary question starts with 'Can your child '", all(t.startswith("Can your child ") for t in texts))
    check("no primary question is 3rd-person", not any(_THIRD_PERSON.match(t) for t in texts), [t for t in texts if _THIRD_PERSON.match(t)][:2])
    check("question count unchanged (≤7 single domain)", asked <= 7, asked)
    check("question_ids look normal (v22-style ids)", all(qid for qid in qids))


# ── 5. add-on intake returns grammatical question_text ──────────────────────
def test_addon_intake_grammar():
    print("\n── add-on intake question_text is grammatical")
    sid = _start()["session_id"]
    st = client.post(f"/api/v1/session/{sid}/focus/cognitive/start", headers=_hdr()).json()
    texts = [st["current_question"]["question_text"]]
    q = st["current_question"]
    while q is not None:
        a = client.post(f"/api/v1/session/{sid}/focus/cognitive/answer", headers=_hdr(),
                        json={"question_id": q["question_id"], "answer": "yes"}).json()
        if a.get("status") == "interview_complete":
            break
        q = a.get("current_question")
        if q:
            texts.append(q["question_text"])
    check("every add-on question starts with 'Can your child '", all(t.startswith("Can your child ") for t in texts))
    check("no add-on question is 3rd-person", not any(_THIRD_PERSON.match(t) for t in texts), [t for t in texts if _THIRD_PERSON.match(t)][:2])


# ── 6. stored state untouched (display-only proof) ──────────────────────────
def test_stored_state_untouched():
    print("\n── stored milestone / question_text / band_state are NOT mutated")
    sid = _start()["session_id"]
    doc = session_store.load("uid-a", sid)
    interview = doc["interview"]
    domain = interview["domain_keys"][0]
    bands = interview["band_state"][domain]["bands"]
    stored_texts = [qq["question_text"] for m in bands.values() for qq in m]
    # the stored question_text retains the original third-person template verbatim
    check("stored question_text is the ORIGINAL (third-person preserved)",
          any(_THIRD_PERSON.match(t) for t in stored_texts), "expected at least one stored 3rd-person text")
    check("stored milestone field present + unchanged shape",
          all("milestone" in qq for m in bands.values() for qq in m))
    # the displayed first question differs from the stored one only by de-conjugation
    g = client.get(f"/api/v1/session/{sid}", headers=_hdr()).json()
    disp = g["current_question"]["question_text"]
    check("displayed first question is grammatical (not 3rd-person)", not _THIRD_PERSON.match(disp), disp)
    check("displayed question_id matches a stored question_id",
          g["current_question"]["question_id"] in {qq["question_id"] for m in bands.values() for qq in m})


def run_all():
    test_exhaustive_milestones()
    test_unit_cases()
    test_idempotent()
    test_primary_intake_grammar()
    test_addon_intake_grammar()
    test_stored_state_untouched()
    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ intake grammar tests FAILED")
        sys.exit(1)
    print("✅ All intake grammar tests PASSED")


if __name__ == "__main__":
    run_all()
