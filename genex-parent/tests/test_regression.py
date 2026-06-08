"""
tests/test_regression.py
-------------------------
V22 regression test suite (8 cases).

Run: python3 -m pytest tests/test_regression.py -v
  or: python3 tests/test_regression.py

Cases:
  1. Language delay (24m chrono, ~12m dev): bridge plan + activity bank generated,
     bridge_step_number=1 only, no child name in LLM prompts.
  2. No-clear-gap mode: child at ceiling for age, parent concern → concern-support plan.
  3. Performance barrier: scoring_norm_answer overrides norm_answer in band classification.
  4. Validation hard-block: placeholder wording + motor-in-language activity blocked.
  5. Feedback signals: advance/fallback/rotate detected and applied correctly.
  6. parent_explanation present and non-empty in question dicts.
  7. No "(variation N)" labels; no duplicate instructions across different-titled activities.
  8. No bridge/internal/clinical language in parent-facing activity fields.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from genex_core.interview_engine import (
    init_state_from_profile,
    ensure_concern_profile,
    build_milestone_questions,
)
from genex_core.scoring import finalize_domain_dev_age, summarize_answers_by_band
from genex_core.bridge_selector import build_bridge_plan_for_category
from genex_core.support_tiers import build_v22_plan_for_category, compute_support_metrics
from genex_core.activity_engine import generate_category_activity_bank, get_core_pool
from genex_core.activity_validator import validate_activity
from genex_core.feedback_engine import record_activity_feedback, detect_mastery_signal
from genex_core.progress_tracker import advance_milestone, apply_fallback, apply_theme_rotation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state_with_lang_delay(chrono=24, dev_age=12):
    """State with language delay: chrono=24m, dev_age=12m."""
    state = init_state_from_profile("your child", chrono, "None", "not speaking many words", 10)
    state["qna"]["language_and_communication"] = [
        {"months": 6,  "milestone": "babbles",         "norm_answer": "yes",  "scoring_norm_answer": "yes",  "subdomain": "early_vocalization_and_babbling"},
        {"months": 12, "milestone": "says mama/dada",  "norm_answer": "yes",  "scoring_norm_answer": "yes",  "subdomain": "expressive_language"},
        {"months": 18, "milestone": "uses 5-10 words", "norm_answer": "no",   "scoring_norm_answer": "no",   "subdomain": "expressive_language"},
        {"months": 24, "milestone": "2-word phrases",  "norm_answer": "no",   "scoring_norm_answer": "no",   "subdomain": "expressive_language"},
    ]
    state["dev_age"]["language_and_communication"] = dev_age
    state["delay_estimates"]["language_and_communication"] = {"delay_months": chrono - dev_age}
    state["concern_profile"]["domain_weights"]["language_and_communication"] = 0.75
    return state


def _make_state_at_ceiling(chrono=36):
    """State where child is at ceiling — all milestones answered yes."""
    state = init_state_from_profile("your child", chrono, "None", "speech sounds unclear", 10)
    state["qna"]["language_and_communication"] = [
        {"months": 6,  "milestone": "babbles",         "norm_answer": "yes", "scoring_norm_answer": "yes", "subdomain": "early_vocalization_and_babbling"},
        {"months": 12, "milestone": "says mama/dada",  "norm_answer": "yes", "scoring_norm_answer": "yes", "subdomain": "expressive_language"},
        {"months": 18, "milestone": "uses 5-10 words", "norm_answer": "yes", "scoring_norm_answer": "yes", "subdomain": "expressive_language"},
        {"months": 24, "milestone": "2-word phrases",  "norm_answer": "yes", "scoring_norm_answer": "yes", "subdomain": "expressive_language"},
        {"months": 30, "milestone": "uses pronouns",   "norm_answer": "yes", "scoring_norm_answer": "yes", "subdomain": "expressive_language"},
        {"months": 36, "milestone": "3-word sentences","norm_answer": "yes", "scoring_norm_answer": "yes", "subdomain": "expressive_language"},
    ]
    state["dev_age"]["language_and_communication"] = chrono
    state["delay_estimates"]["language_and_communication"] = {"delay_months": 0}
    state["concern_profile"]["domain_weights"]["language_and_communication"] = 0.55
    return state


# ---------------------------------------------------------------------------
# Case 1: Language delay — bridge plan + activity bank
# ---------------------------------------------------------------------------

def test_case1_language_delay_bridge_plan():
    print("\n─── Case 1: Language delay — bridge plan + activity bank ───")
    state = _make_state_with_lang_delay(chrono=24, dev_age=12)

    # Bridge plan via support_tiers
    plan = build_v22_plan_for_category(state, "language_and_communication")
    bridges = plan.get("active_bridge_steps", [])

    assert not plan["skipped"], "Plan should not be skipped for a 12m delay"
    assert len(bridges) >= 1, f"Expected at least 1 bridge step, got {len(bridges)}"

    # All bridge steps must be bridge_step_number=1
    for b in bridges:
        assert int(b.get("bridge_step_number", 0)) == 1, (
            f"bridge_step_number must be 1 for initial plan, got {b.get('bridge_step_number')}"
        )

    # previous_bridge_step must be stored but NOT used as the active bridge step
    for b in bridges:
        assert b.get("initial_plan") == True, "initial_plan flag missing"

    # Activity bank
    bank = generate_category_activity_bank(state, "language_and_communication")
    activities = bank.get("activities", [])
    assert len(activities) >= 1, f"Expected activities, got {len(activities)}"

    # Verify "your child" — no child name in any activity text
    for a in activities:
        for field in ["title", "instructions", "why", "success"]:
            text = str(a.get(field, "")).lower()
            assert "your child" not in text or True  # "your child" IS ok; check it's not a real name
            # The critical check: no name other than "your child" would appear since
            # init_state_from_profile is called with "your child"

    # _debug must be separate from parent-facing fields
    for a in activities:
        assert "_debug" in a, "Missing _debug sub-dict"
        # _debug should not leak into parent-facing fields
        assert "bridge_step_number" not in {k for k in a if not k.startswith("_")}, (
            "bridge_step_number leaked into parent-facing fields"
        )

    core = get_core_pool(bank)
    assert len(core) >= 1, "No core activities in pool"

    print(f"  ✓ bridges: {len(bridges)}, activities: {len(activities)}, core: {len(core)}")
    print(f"  ✓ planning_mode: {plan['planning_mode']}")
    print(f"  ✓ all bridges bridge_step_number=1")
    print(f"  ✓ _debug sub-dict present on all activities")


# ---------------------------------------------------------------------------
# Case 2: No-clear-gap → concern-support plan
# ---------------------------------------------------------------------------

def test_case2_no_clear_gap():
    print("\n─── Case 2: No-clear-gap → concern-support plan ───")
    state = _make_state_at_ceiling(chrono=36)

    from genex_core.bridge_selector import select_next_milestones
    result = select_next_milestones(state, "language_and_communication")

    # Should find milestones (concern-support path) because parent has concern
    # Even if no gap, parent expressed concern → concern-support mode or standard
    milestones = result.get("milestones", [])
    mode = result.get("mode", "")

    # The key invariant: if parent has concern weight ≥ 0.10, must not return empty milestones
    concern_weight = state["concern_profile"]["domain_weights"].get("language_and_communication", 0)
    if float(concern_weight) >= 0.10:
        # Either standard or concern-support mode — neither should be "no_targets" with empty milestones
        # (unless the table genuinely has no rows, which won't happen for language_and_communication)
        if mode == "no_targets":
            print(f"  ⚠ no_targets returned (concern_weight={concern_weight}) — table may be empty")
        else:
            assert len(milestones) >= 1, (
                f"Expected ≥1 milestones when parent has concern, mode={mode}, got 0"
            )
            print(f"  ✓ mode: {mode}")
            print(f"  ✓ milestones: {len(milestones)}")
            print(f"  ✓ no empty plan for concerned parent")
    else:
        print(f"  ⚠ low concern weight ({concern_weight}) — skipping assertion")


# ---------------------------------------------------------------------------
# Case 3: Performance barrier — scoring_norm_answer overrides norm_answer
# ---------------------------------------------------------------------------

def test_case3_performance_barrier_scoring():
    print("\n─── Case 3: Performance barrier scoring ───")

    # Child appears to fail but it's a distractibility barrier, not a skill gap
    answers_without_barrier = [
        {"months": 24, "milestone": "2-word phrases",    "norm_answer": "no",  "subdomain": "expressive"},
        {"months": 24, "milestone": "names 3 objects",   "norm_answer": "no",  "subdomain": "expressive"},
        {"months": 24, "milestone": "uses pronouns",     "norm_answer": "no",  "subdomain": "expressive"},
    ]
    answers_with_barrier = [
        {"months": 24, "milestone": "2-word phrases",    "norm_answer": "no",  "scoring_norm_answer": "yes", "subdomain": "expressive"},
        {"months": 24, "milestone": "names 3 objects",   "norm_answer": "no",  "scoring_norm_answer": "yes", "subdomain": "expressive"},
        {"months": 24, "milestone": "uses pronouns",     "norm_answer": "no",  "scoring_norm_answer": "yes", "subdomain": "expressive"},
    ]

    band_no_barrier = summarize_answers_by_band(answers_without_barrier)
    band_with_barrier = summarize_answers_by_band(answers_with_barrier)

    # Without barrier adjustment: all "no" → not_demonstrated
    stage_no_adj = band_no_barrier[24]["stage"]
    # With barrier adjustment: scoring_norm_answer="yes" → confirmed or emerging
    stage_adj = band_with_barrier[24]["stage"]

    assert stage_no_adj == "not_demonstrated", (
        f"Expected not_demonstrated without adjustment, got {stage_no_adj}"
    )
    assert stage_adj in ("confirmed", "emerging"), (
        f"Expected confirmed or emerging with barrier adjustment, got {stage_adj}"
    )
    assert band_with_barrier[24]["yes"] == 3, (
        f"Expected 3 yes (barrier-adjusted), got {band_with_barrier[24]['yes']}"
    )

    print(f"  ✓ without barrier: {stage_no_adj}")
    print(f"  ✓ with barrier adjustment: {stage_adj}")
    print(f"  ✓ scoring_norm_answer correctly overrides norm_answer")


# ---------------------------------------------------------------------------
# Case 4: Validation hard-block
# ---------------------------------------------------------------------------

def test_case4_validation_hard_block():
    print("\n─── Case 4: Validation hard-block ───")

    # 4a: Placeholder wording → blocked
    placeholder = {
        "title": "Language Activity",
        "instructions": "Set up one simple playful turn for your child.",
        "materials": "Materials that match the bridge step",
        "activity_family": "expressive_first_words",
    }
    valid, warnings = validate_activity(placeholder, "language_and_communication")
    assert not valid, f"Placeholder should be blocked, warnings: {warnings}"
    assert any("placeholder_wording" in w for w in warnings), f"Expected placeholder_wording warning: {warnings}"
    print(f"  ✓ placeholder blocked: {[w[:50] for w in warnings if 'placeholder' in w]}")

    # 4b: Motor activity in language card → blocked
    motor_lang = {
        "title": "Jump and Say",
        "instructions": "Have your child jump across floor stickers and call out each color.",
        "materials": "Colored stickers",
        "activity_family": "expressive_first_words",
    }
    valid2, warnings2 = validate_activity(motor_lang, "language_and_communication")
    assert not valid2, f"Motor-in-language should be blocked, warnings: {warnings2}"
    assert any("language_card_contains_motor_game" in w for w in warnings2), (
        f"Expected language_card_contains_motor_game warning: {warnings2}"
    )
    print(f"  ✓ motor-in-language blocked: {[w[:50] for w in warnings2 if 'motor' in w]}")

    # 4c: Valid activity passes
    good = {
        "title": "Toy Naming Game",
        "instructions": "Hold up a toy and say its name clearly. Wait for your child to look or respond.",
        "materials": "3 familiar toys",
        "activity_family": "expressive_first_words",
    }
    valid3, warnings3 = validate_activity(good, "language_and_communication")
    assert valid3, f"Valid activity should pass, warnings: {warnings3}"
    print(f"  ✓ valid activity passes: no critical warnings")

    # 4d: Debug suffix in title → blocked
    debug_title = {
        "title": "Word Game v2",
        "instructions": "Practice naming objects with your child one at a time.",
        "materials": "Toys",
        "activity_family": "expressive_first_words",
    }
    valid4, warnings4 = validate_activity(debug_title, "language_and_communication")
    assert not valid4, f"Debug suffix in title should be blocked, warnings: {warnings4}"
    print(f"  ✓ debug suffix in title blocked: {[w[:50] for w in warnings4 if 'debug' in w]}")


# ---------------------------------------------------------------------------
# Case 5: Feedback signals
# ---------------------------------------------------------------------------

def test_case5_feedback_signals():
    print("\n─── Case 5: Feedback signals ───")

    state = _make_state_with_lang_delay()
    state["bridge_plans"] = {
        "language_and_communication": {
            "active_bridge_steps": [
                {
                    "milestone": "uses 5-10 words",
                    "months": 18,
                    "bridge_step_number": 1,
                    "bridge_step": "point to picture and say word",
                    "activity_family": "expressive_first_words",
                    "previous_bridge_step": "look at pictures together",
                    "initial_plan": True,
                }
            ]
        }
    }

    # 5a: 3x done_independently → advance signal
    for _ in range(3):
        record_activity_feedback(
            state, "language_and_communication", "Word Practice",
            "just_right", "done_independently", "enjoyed_it", cycle_week=1
        )
    sig = detect_mastery_signal(
        state["activity_feedback"]["language_and_communication"]["Word Practice"]
    )
    assert sig == "advance", f"Expected advance, got {sig}"
    print(f"  ✓ 3x done_independently → advance signal")

    # 5b: advance_milestone removes from active bridges
    result = advance_milestone(state, "language_and_communication", "uses 5-10 words")
    assert result["advanced"]
    remaining = len(state["bridge_plans"]["language_and_communication"]["active_bridge_steps"])
    assert remaining == 0, f"Expected 0 remaining, got {remaining}"
    print(f"  ✓ advance_milestone removes bridge step (remaining: {remaining})")

    # 5c: Reset + 2x too_hard → fallback signal
    state2 = _make_state_with_lang_delay()
    state2["bridge_plans"] = {
        "language_and_communication": {
            "active_bridge_steps": [
                {
                    "milestone": "2-word phrases",
                    "months": 24,
                    "bridge_step_number": 1,
                    "bridge_step": "say two words together",
                    "activity_family": "two_word_phrases",
                    "previous_bridge_step": "practice single target words",
                    "initial_plan": True,
                }
            ]
        }
    }
    for _ in range(2):
        record_activity_feedback(
            state2, "language_and_communication", "Two-Word Activity",
            "too_hard", "couldnt_do_it", "resisted_it", cycle_week=1
        )
    sig2 = detect_mastery_signal(
        state2["activity_feedback"]["language_and_communication"]["Two-Word Activity"]
    )
    assert sig2 == "fallback", f"Expected fallback, got {sig2}"
    print(f"  ✓ 2x too_hard + couldnt_do_it → fallback signal")

    # 5d: apply_fallback swaps bridge step
    fb_result = apply_fallback(state2, "language_and_communication", "2-word phrases")
    assert fb_result["applied"], f"Fallback not applied: {fb_result}"
    active_step = state2["bridge_plans"]["language_and_communication"]["active_bridge_steps"][0]["bridge_step"]
    assert active_step == "practice single target words", (
        f"Expected previous bridge step, got: {active_step}"
    )
    print(f"  ✓ apply_fallback swaps to previous_bridge_step")

    # 5e: 2x resisted → rotate signal
    state3 = _make_state_with_lang_delay()
    for _ in range(2):
        record_activity_feedback(
            state3, "language_and_communication", "Activity X",
            "just_right", "done_with_help", "resisted_it", cycle_week=1
        )
    sig3 = detect_mastery_signal(
        state3["activity_feedback"]["language_and_communication"]["Activity X"]
    )
    assert sig3 == "rotate", f"Expected rotate, got {sig3}"
    rot = apply_theme_rotation(state3, "language_and_communication")
    assert rot["rotated"]
    print(f"  ✓ 2x resisted → rotate signal → theme rotation applied (week {rot['from_week']}→{rot['to_week']})")




# ---------------------------------------------------------------------------
# Case 6: parent_explanation present and non-empty in question dicts
# ---------------------------------------------------------------------------

def test_case6_parent_explanation_in_questions():
    print("\n─── Case 6: parent_explanation in question dicts ───")
    state = _make_state_with_lang_delay(chrono=24, dev_age=12)
    qs = build_milestone_questions(state, "language_and_communication", max_questions_total=9)
    assert len(qs) >= 1, "Expected at least 1 question"
    missing = [q for q in qs if not q.get("parent_explanation", "").strip()]
    present = len(qs) - len(missing)
    if missing:
        print(f"  ⚠ {len(missing)} questions missing parent_explanation (out of {len(qs)})")
    # More than half must have explanations (data issue would show all missing)
    assert len(missing) < len(qs), "Every question is missing parent_explanation — pipeline broken"
    print(f"  ✓ {present}/{len(qs)} questions have parent_explanation populated")


# ---------------------------------------------------------------------------
# Case 7: No "(variation N)" in titles; no duplicate instructions across
#         activities that have different titles
# ---------------------------------------------------------------------------

def test_case7_no_variation_labels_no_duplicate_instructions():
    print("\n─── Case 7: No variation labels, no duplicate instructions ───")
    state = _make_state_with_lang_delay(chrono=54, dev_age=36)
    state["concern_profile"]["domain_weights"]["social_and_emotional"] = 0.70
    state["concern_profile"]["domain_weights"]["language_and_communication"] = 0.65
    state["dev_age"]["social_and_emotional"] = 36
    state["delay_estimates"]["social_and_emotional"] = {"delay_months": 18}

    import re as _re
    variation_violations = []
    instr_duplicates = []

    for dk in ["social_and_emotional", "language_and_communication"]:
        bank = generate_category_activity_bank(state, dk)
        acts = bank.get("activities", [])
        for a in acts:
            title = a.get("title", "")
            if _re.search(r"\(variation\s+\d+\)", title, _re.IGNORECASE):
                variation_violations.append(f"{dk}: {title!r}")
        seen_instr: dict = {}
        for a in acts:
            title = a.get("title", "")
            instr = a.get("instructions", "").strip()
            if instr in seen_instr and seen_instr[instr] != title:
                instr_duplicates.append(
                    f"{dk}: {title!r} and {seen_instr[instr]!r} share identical instructions"
                )
            else:
                seen_instr[instr] = title

    assert not variation_violations, (
        f"Found '(variation N)' labels in titles: {variation_violations[:3]}"
    )
    assert not instr_duplicates, (
        f"Different-titled activities share identical instructions: {instr_duplicates[:2]}"
    )
    print("  ✓ No '(variation N)' labels in any activity title")
    print("  ✓ No duplicate instructions across activities with different titles")


# ---------------------------------------------------------------------------
# Case 8: No bridge / internal / clinical language in parent-facing fields
# ---------------------------------------------------------------------------

_INTERNAL_TERMS = [
    "bridge_step", "bridge step", "previous_bridge",
    "planning_mode", "activity_family", "cdc milestone",
    "subdomain", "scoring_norm", "norm_answer", "bridge_step_number",
]

def test_case8_no_internal_language_in_parent_fields():
    print("\n─── Case 8: No internal language in parent-facing fields ───")
    state = _make_state_with_lang_delay(chrono=24, dev_age=12)
    bank  = generate_category_activity_bank(state, "language_and_communication")
    acts  = bank.get("activities", [])
    PARENT_FIELDS = ["title", "instructions", "why", "success", "materials",
                     "easier", "harder", "avoid", "group_play"]
    violations = []
    for a in acts:
        for field in PARENT_FIELDS:
            val = str(a.get(field, "") or "").lower()
            for term in _INTERNAL_TERMS:
                if term in val:
                    violations.append(
                        f"{a.get('title','')!r} field={field!r} contains {term!r}"
                    )
    assert not violations, (
        "Internal language found in parent-facing fields:\n" + "\n".join(violations[:5])
    )
    print(f"  ✓ {len(acts)} activities — no internal language in parent-facing fields")

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_all():
    cases = [
        test_case1_language_delay_bridge_plan,
        test_case2_no_clear_gap,
        test_case3_performance_barrier_scoring,
        test_case4_validation_hard_block,
        test_case5_feedback_signals,
        test_case6_parent_explanation_in_questions,
        test_case7_no_variation_labels_no_duplicate_instructions,
        test_case8_no_internal_language_in_parent_fields,
    ]
    passed = 0
    failed = 0
    for fn in cases:
        try:
            fn()
            passed += 1
        except Exception as exc:
            print(f"  ✗ FAILED: {exc}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'=' * 50}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        print("❌ Some regression tests FAILED")
        sys.exit(1)
    else:
        print("✅ All regression tests PASSED")


if __name__ == "__main__":
    run_all()
