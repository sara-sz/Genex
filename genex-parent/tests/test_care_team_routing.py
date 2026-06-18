"""
tests/test_care_team_routing.py — Beta 2.0 Step 3B

Tests parent-note visibility filtering and the care-team routing constants in
api/report_generator.py. Pure report-layer logic — no auth, beta-code, plan/
activity generation, scheduler, or session-ownership behavior is exercised.

Mirrors the policy in docs/care_team_report_routing.md.

Run: PYTHONPATH=. python3 tests/test_care_team_routing.py
"""

import sys

from api.report_generator import (
    compute_note_visibility,
    note_visible_in_report,
    activity_relevance_providers,
    generate_report_body,
    REPORT_TYPE_TO_PROVIDER,
)

_passed = 0
_failed = 0


def check(label, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✓ {label}")
    else:
        _failed += 1
        print(f"  ✗ FAIL: {label} — {detail}")


def _note(text, *, tags=None, member=None):
    """Build a flagged feedback record carrying a unique note string."""
    rec = {
        "discuss_with_care_team": True,
        "note": text,
        "activity_date": "2026-06-18",
        "domain": "language_and_communication",
        "activity_family": "naming_routine",
    }
    if tags is not None:
        rec["care_team_tags"] = tags
    if member is not None:
        rec["care_team_member"] = member
    return rec


# Unique markers per case so we can assert presence/absence in the report body.
_A = _note("NOTE_ST_ONLY",    tags=["st"])
_B = _note("NOTE_OTPT_ONLY",  tags=["ot_pt"])
_C = _note("NOTE_DOC_ONLY",   tags=["doctor"])
_D = _note("NOTE_DOC_ST",     tags=["doctor", "st"])
_E = _note("NOTE_LEGACY_ST",  member="ST")
_F = _note("NOTE_LEGACY_OT",  member="OT")
_G = _note("NOTE_LEGACY_PT",  member="PT")
_H = _note("NOTE_LEGACY_DOC", member="Doctor")
_I = _note("NOTE_UNTAGGED")  # flagged, no tags, no member

_ALL_NOTES = [_A, _B, _C, _D, _E, _F, _G, _H, _I]


def _make_doc(feedback):
    return {
        "age_in_months": 36,
        "daily_time_minutes": 10,
        "diagnosis_or_condition": "",
        "current_plan_id": None,
        "plans": {},
        "feedback": feedback,
    }


# ── Unit: compute_note_visibility ────────────────────────────────────────────
def test_compute_note_visibility():
    print("\n── compute_note_visibility")
    check("explicit ['st']", compute_note_visibility(_A) == ["st"])
    check("explicit ['ot_pt']", compute_note_visibility(_B) == ["ot_pt"])
    check("explicit ['doctor','st'] preserved",
          compute_note_visibility(_D) == ["doctor", "st"], compute_note_visibility(_D))
    check("legacy ST → st", compute_note_visibility(_E) == ["st"])
    check("legacy OT → ot_pt", compute_note_visibility(_F) == ["ot_pt"])
    check("legacy PT → ot_pt", compute_note_visibility(_G) == ["ot_pt"])
    check("legacy Doctor → doctor", compute_note_visibility(_H) == ["doctor"])
    check("untagged flagged → doctor only", compute_note_visibility(_I) == ["doctor"])
    check("invalid tag ignored, falls back to doctor",
          compute_note_visibility({"care_team_tags": ["bogus"]}) == ["doctor"])
    check("tags win over legacy member",
          compute_note_visibility(_note("x", tags=["ot_pt"], member="ST")) == ["ot_pt"])


# ── Unit: note_visible_in_report ─────────────────────────────────────────────
def test_note_visible_in_report():
    print("\n── note_visible_in_report")
    check("doctor sees st-only note", note_visible_in_report(_A, "doctor"))
    check("doctor sees ot_pt-only note", note_visible_in_report(_B, "doctor"))
    check("doctor sees untagged note", note_visible_in_report(_I, "doctor"))
    check("speech_therapist sees st note", note_visible_in_report(_A, "speech_therapist"))
    check("speech_therapist hides ot_pt note", not note_visible_in_report(_B, "speech_therapist"))
    check("speech_therapist hides doctor-only note", not note_visible_in_report(_C, "speech_therapist"))
    check("ot_pt sees ot_pt note", note_visible_in_report(_B, "ot_pt"))
    check("ot_pt hides st note", not note_visible_in_report(_A, "ot_pt"))
    check("legacy occupational_therapist uses ot_pt filtering",
          note_visible_in_report(_B, "occupational_therapist") and
          not note_visible_in_report(_A, "occupational_therapist"))
    check("legacy physical_therapist uses ot_pt filtering",
          note_visible_in_report(_F, "physical_therapist") and
          not note_visible_in_report(_A, "physical_therapist"))
    check("report_type→provider map covers all Beta 2.0 + legacy types",
          set(REPORT_TYPE_TO_PROVIDER) == {
              "doctor", "speech_therapist", "occupational_therapist",
              "physical_therapist", "ot_pt"})


# ── End-to-end: generate_report_body filtering ───────────────────────────────
def _body(report_type):
    return generate_report_body(_make_doc(_ALL_NOTES), report_type)


def test_doctor_report_is_comprehensive():
    print("\n── doctor report shows ALL flagged notes")
    body = _body("doctor")
    markers = ["NOTE_ST_ONLY", "NOTE_OTPT_ONLY", "NOTE_DOC_ONLY", "NOTE_DOC_ST",
               "NOTE_LEGACY_ST", "NOTE_LEGACY_OT", "NOTE_LEGACY_PT",
               "NOTE_LEGACY_DOC", "NOTE_UNTAGGED"]
    for m in markers:
        check(f"doctor body contains {m}", m in body)


def test_speech_report_filtering():
    print("\n── speech_therapist report shows only st-visible notes")
    body = _body("speech_therapist")
    for m in ["NOTE_ST_ONLY", "NOTE_DOC_ST", "NOTE_LEGACY_ST"]:
        check(f"ST body contains {m}", m in body)
    for m in ["NOTE_OTPT_ONLY", "NOTE_DOC_ONLY", "NOTE_LEGACY_OT",
              "NOTE_LEGACY_PT", "NOTE_LEGACY_DOC", "NOTE_UNTAGGED"]:
        check(f"ST body EXCLUDES {m}", m not in body)


def test_ot_pt_report_filtering():
    print("\n── ot_pt report shows only ot_pt-visible notes")
    body = _body("ot_pt")
    for m in ["NOTE_OTPT_ONLY", "NOTE_LEGACY_OT", "NOTE_LEGACY_PT"]:
        check(f"OT/PT body contains {m}", m in body)
    for m in ["NOTE_ST_ONLY", "NOTE_DOC_ONLY", "NOTE_DOC_ST",
              "NOTE_LEGACY_ST", "NOTE_LEGACY_DOC", "NOTE_UNTAGGED"]:
        check(f"OT/PT body EXCLUDES {m}", m not in body)


def test_legacy_report_types_use_ot_pt():
    print("\n── legacy occupational_therapist / physical_therapist use ot_pt filtering")
    for rt in ("occupational_therapist", "physical_therapist"):
        body = _body(rt)
        check(f"{rt} contains ot_pt note", "NOTE_OTPT_ONLY" in body)
        check(f"{rt} excludes st-only note", "NOTE_ST_ONLY" not in body)


def test_report_shape_and_backward_compat():
    print("\n── report body is a non-empty string; old payloads (no tags) work")
    body = _body("speech_therapist")
    check("body is a non-empty string", isinstance(body, str) and len(body) > 0)
    # A doc whose feedback records have neither care_team_tags nor care_team_member
    # (oldest clients) must still generate without error and show those notes only
    # in the doctor report.
    old = _make_doc([_note("OLD_STYLE_NOTE")])
    doc_body = generate_report_body(old, "doctor")
    st_body = generate_report_body(old, "speech_therapist")
    check("old-style note in doctor report", "OLD_STYLE_NOTE" in doc_body)
    check("old-style note excluded from ST report", "OLD_STYLE_NOTE" not in st_body)


def test_no_flagged_section_when_empty_for_provider():
    print("\n── a provider with no visible notes omits the flagged section cleanly")
    # Only an st-only note exists; the ot_pt report should not render a flagged block.
    body = generate_report_body(_make_doc([_A]), "ot_pt")
    check("no 'Items Flagged' header when none visible",
          "Items Flagged for Care Team" not in body, body[-200:])
    # Doctor still sees it.
    check("doctor still shows it", "NOTE_ST_ONLY" in generate_report_body(_make_doc([_A]), "doctor"))


# ── Unit: activity relevance constants ───────────────────────────────────────
def test_activity_relevance_providers():
    print("\n── activity_relevance_providers (suggestion layer)")
    check("language domain → doctor+st",
          activity_relevance_providers("language_and_communication") == ["doctor", "st"])
    check("movement domain → doctor+ot_pt",
          activity_relevance_providers("movement_and_physical") == ["doctor", "ot_pt"])
    check("cognitive + language subdomain → doctor+st",
          activity_relevance_providers("cognitive", "receptive_language") == ["doctor", "st"])
    check("cognitive + visual-motor subdomain → doctor+ot_pt",
          activity_relevance_providers("cognitive", "visual_motor_attention") == ["doctor", "ot_pt"])
    check("cognitive unclear → doctor + both (domain default)",
          set(activity_relevance_providers("cognitive", "")) == {"doctor", "st", "ot_pt"})
    check("social communication subdomain → doctor+st",
          activity_relevance_providers("social_and_emotional", "social_communication") == ["doctor", "st"])
    check("social regulation/sensory subdomain → doctor+ot_pt",
          activity_relevance_providers("social_and_emotional", "sensory_regulation") == ["doctor", "ot_pt"])
    check("doctor always present", "doctor" in activity_relevance_providers("movement_and_physical"))


def run_all():
    test_compute_note_visibility()
    test_note_visible_in_report()
    test_doctor_report_is_comprehensive()
    test_speech_report_filtering()
    test_ot_pt_report_filtering()
    test_legacy_report_types_use_ot_pt()
    test_report_shape_and_backward_compat()
    test_no_flagged_section_when_empty_for_provider()
    test_activity_relevance_providers()

    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ care-team routing tests FAILED")
        sys.exit(1)
    print("✅ All care-team routing tests PASSED")


if __name__ == "__main__":
    run_all()
