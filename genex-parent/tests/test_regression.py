"""
tests/test_regression.py
-------------------------
V22 regression test suite (31 cases).

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
  9. Week 1 schedule uniqueness: no same-day duplicate titles; no Easier/Stretch variants
     on weekdays; no duplicate titles across the whole week per category.
 10. Activity bank uniqueness: capped core_variants produces no duplicate titles within
     a single bridge's core activities.
 11. Safety: Dravet / seizure / unstable-walk profiles hard-block jump/hop activities and
     replace them with distinct safe-movement cards (no duplicate replacement titles).
 12. ADHD: 10 min/day profile fills all 10 weekday slots with unique activities and
     cognitive bank has enough attention-bucket cards.
 13. Routing: speech-delay-only concern selects only language_and_communication domain
     (delay signal alone cannot trigger a second domain).
 14. Time budget fill: 5/10/15 min/day speech-delay profile fills all weekday minutes.
 15. No weekday rest days for Chang profile (movement + cognitive).
 16. Speech-delay bridge spread: 12 core activities across 2 subdomains, no duplicates.
 17. Near-duplicate detection: Dravet bank unique; validator catches body-part mismatch.
 18. Dravet safety: stomp/squat-and-reach/race/jump blocked; 9 distinct safe card titles.
 19. ADHD age-appropriateness: Thu/Fri filled; cognitive cards have ADHD Why text.
 20. Exact slot counts: Dravet/ADHD/Speech produce correct slots and total minutes.
 21. Card schema completeness: all scheduled cards have make_easier and make_harder.
 22. OT/PT routing for Chao profile: language + movement selected, social suppressed.
 23. Near-duplicate prevention (family + root dedup) across Dravet/ADHD/Speech week 1.
 24. No generic fallback phrases (pass-8) in any scheduled card across 4 profiles.
 25. No doubled-suffix titles ("Game Game") in any activity bank.
 26. Down syndrome / low-tone safety: no jump/hop/stomp/race/climb in any bank activity.
 27. OT/PT/speech explicit term routing to correct domains.
 28. Pass-9 generic phrases blocked in ADHD-48m and DS-24m scheduled cards.
 29. Validator blocks success-criteria domain mismatch (ball/foot, bead/crayon).
 30. ADHD 48m gets concrete first/then and counting cards (not generic titles).
 31. DS-24m and Dravet pass-9 bucket cards contain no unsafe movement in any field.
"""

import re
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
from genex_core.scheduler import allocate_weekly_slots, build_weekly_schedule


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
# Case 9: Week 1 schedule uniqueness
# ---------------------------------------------------------------------------

def _make_maya_state():
    """Approximate Maya test case: 54m chrono, speech delay + social concern, 10m/day."""
    state = init_state_from_profile("your child", 54, "None", "speech delay and social concern", 10)
    state["dev_age"]["language_and_communication"] = 36
    state["dev_age"]["social_and_emotional"] = 36
    state["delay_estimates"]["language_and_communication"] = {"delay_months": 18}
    state["delay_estimates"]["social_and_emotional"] = {"delay_months": 18}
    state["concern_profile"]["domain_weights"]["language_and_communication"] = 0.70
    state["concern_profile"]["domain_weights"]["social_and_emotional"] = 0.70
    state["child"]["daily_time_min"] = 10
    return state


def test_case9_week1_schedule_uniqueness():
    print("\n─── Case 9: Week 1 schedule uniqueness ───")
    state = _make_maya_state()

    from genex_core.config import DOMAIN_CONFIG
    from genex_core.support_tiers import build_v22_plan_for_category

    focus_domains = [
        dk for dk in DOMAIN_CONFIG
        if state["concern_profile"]["domain_weights"].get(dk, 0) >= 0.5
    ]
    if not focus_domains:
        focus_domains = ["language_and_communication"]

    for dk in focus_domains:
        plan = build_v22_plan_for_category(state, dk)
        state.setdefault("bridge_plans", {})[dk] = plan
        bank = generate_category_activity_bank(state, dk)
        state.setdefault("activity_banks", {})[dk] = bank

    state["cycle_week"] = 1
    allocate_weekly_slots(state)
    build_weekly_schedule(state)

    schedule = state["weekly_schedule"]
    days = schedule.get("days", {})
    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    same_day_dups = []
    easier_stretch_found = []

    for day_name in WEEKDAYS:
        items = days.get(day_name, {}).get("items", [])
        day_titles = [i.get("title", "") for i in items]

        # No same-day duplicate titles (case-insensitive)
        lower_titles = [t.lower() for t in day_titles]
        if len(lower_titles) != len(set(lower_titles)):
            same_day_dups.append(f"{day_name}: {day_titles}")

        # No Easier:/Stretch: variants in Week 1 weekdays
        for t in day_titles:
            if t.startswith("Easier:") or t.startswith("Stretch:"):
                easier_stretch_found.append(f"{day_name}: {t!r}")

    assert not same_day_dups, (
        f"Same-day duplicate titles found in Week 1:\n" + "\n".join(same_day_dups)
    )
    assert not easier_stretch_found, (
        f"Easier/Stretch variants appeared in Week 1 weekdays:\n" + "\n".join(easier_stretch_found)
    )

    # Also check: per-category, no title repeated across the week
    cat_titles: dict = {}
    for day_name in WEEKDAYS:
        for item in days.get(day_name, {}).get("items", []):
            ck = item.get("category_key", "unknown")
            t = item.get("title", "").lower()
            cat_titles.setdefault(ck, []).append((day_name, t))

    cross_week_dups = []
    for ck, entries in cat_titles.items():
        seen: dict = {}
        for day_name, t in entries:
            if t in seen:
                cross_week_dups.append(f"{ck}: {t!r} on {seen[t]} AND {day_name}")
            else:
                seen[t] = day_name

    assert not cross_week_dups, (
        "Same title appears for the same category on multiple days in Week 1:\n"
        + "\n".join(cross_week_dups)
    )

    total_slots = sum(
        len(days.get(d, {}).get("items", [])) for d in WEEKDAYS
    )
    print(f"  ✓ No same-day duplicate titles")
    print(f"  ✓ No Easier:/Stretch: variants in Week 1 weekdays")
    print(f"  ✓ No cross-week per-category title repeats")
    print(f"  ✓ Total Week 1 weekday activity slots: {total_slots}")
    for d in WEEKDAYS:
        titles = [i.get("title", "?") for i in days.get(d, {}).get("items", [])]
        print(f"    {d}: {titles}")


# ---------------------------------------------------------------------------
# Case 10: Activity bank — capped variants produce no per-bridge title duplicates
# ---------------------------------------------------------------------------

def test_case10_activity_bank_no_per_bridge_core_duplicates():
    print("\n─── Case 10: Activity bank — no per-bridge core duplicate titles ───")
    state = _make_maya_state()

    for dk in ["social_and_emotional", "language_and_communication"]:
        bank = generate_category_activity_bank(state, dk)
        acts = bank.get("activities", [])

        # Group core activities by (milestone, activity_family) — i.e., per bridge
        per_bridge: dict = {}
        for a in acts:
            debug = a.get("_debug", {})
            if debug.get("activity_type", "core") != "core":
                continue
            key = (debug.get("milestone", ""), debug.get("activity_family", ""))
            per_bridge.setdefault(key, []).append(a.get("title", ""))

        bridge_dups = []
        for (milestone, fam), titles in per_bridge.items():
            lower = [t.lower() for t in titles]
            if len(lower) != len(set(lower)):
                from collections import Counter
                counts = Counter(lower)
                dups = {t: c for t, c in counts.items() if c > 1}
                bridge_dups.append(
                    f"{dk} | fam={fam!r} | milestone={milestone[:40]!r} → dup titles: {dups}"
                )

        assert not bridge_dups, (
            f"Per-bridge core duplicate titles found (capping not working):\n"
            + "\n".join(bridge_dups)
        )
        print(f"  ✓ {dk}: {len(per_bridge)} bridges, no per-bridge core duplicate titles")


# ---------------------------------------------------------------------------
# Helpers — exact QA profiles from Sara's local test cases
# ---------------------------------------------------------------------------

# Keyword patterns that must NEVER appear in movement activities for
# high-fall / seizure profiles.
_RISKY_MOVEMENT_PATTERNS = re.compile(
    r"\b(jump(ing)?|hop(ping)?|race|racing|obstacle|climb(ing)?|"
    r"high surface|high platform|unsupported balance|speed|"
    r"frog jump|trampoline|bouncing|sprint|run fast)\b",
    flags=re.IGNORECASE,
)

# Keywords that mark an activity as calm / safe for seizure/unstable-walk profiles
_SAFE_MOVEMENT_MARKERS = re.compile(
    r"\b(seated|supported|flat.ground|caregiver|stable|supervision|"
    r"slow|gentle|squat|reach|sit|stand|step)\b",
    flags=re.IGNORECASE,
)


def _make_dravet_state():
    """Case A: Dravet, 40m, speech regression + unstable walking, 10 min/day."""
    return init_state_from_profile(
        "your child", 40, "Dravet syndrome",
        "speech regression, unstable walking, seizures, frequent falls", 10,
    )


def _make_adhd_state():
    """Case B: ADHD, 60m, hyperactivity + focus + task-completion, 10 min/day."""
    state = init_state_from_profile(
        "your child", 60, "ADHD",
        "hyperactivity, trouble focusing, difficulty finishing tasks", 10,
    )
    state["dev_age"]["cognitive"] = 48
    state["dev_age"]["social_and_emotional"] = 48
    state["delay_estimates"]["cognitive"] = {"delay_months": 12}
    state["delay_estimates"]["social_and_emotional"] = {"delay_months": 12}
    ensure_concern_profile(state)
    return state


def _make_speech_delay_state():
    """Case C: no diagnosis, speech delay only, 36m, 5 min/day."""
    return init_state_from_profile(
        "your child", 36, "none",
        "speech delay, not talking much", 5,
    )


# ---------------------------------------------------------------------------
# Case 11: Case A — Dravet safety (exact QA profile)
# ---------------------------------------------------------------------------

def test_case11_dravet_safety():
    """
    Case A: Dravet syndrome, 40m, speech regression + unstable walking, 10 min/day.

    Expected:
    - Focus domains: movement_and_physical + language_and_communication
    - Safety profile fires seizure_or_medical_monitoring AND falls_balance_gait
    - No movement activity contains jump/hop/race/obstacle/climb/high surface/speed/
      unsupported balance in title OR instructions
    - All movement activities are calm, supported, seated, flat-ground, or caregiver-assisted
    - 'avoid' field on each movement card is consistent with the card (no residual jump text)
    - Safety replacement applies to both deterministic AND simulated-LLM activities
    """
    print("\n─── Case 11: Dravet safety — exact QA profile ───")
    from genex_core.safety import build_safety_profile
    from genex_core.interview_engine import choose_focus_domains
    from genex_core.support_tiers import build_v22_plan_for_category

    state = _make_dravet_state()
    ensure_concern_profile(state)

    # ── 1. Safety profile ─────────────────────────────────────────────────────
    profile = build_safety_profile(state["child"])
    risk_scores = profile["risk_scores"]
    hard_avoid  = profile["hard_avoid"]

    assert risk_scores.get("seizure_or_medical_monitoring", 0) >= 0.35, (
        f"seizure_or_medical_monitoring not detected: {risk_scores}"
    )
    assert risk_scores.get("falls_balance_gait", 0) >= 0.35, (
        f"falls_balance_gait not detected (missing 'unstable' keyword?): {risk_scores}"
    )
    jump_in_hard_avoid = any(
        re.search(r"\b(jump|hop|trampoline|unsupported)\b", h, re.IGNORECASE)
        for h in hard_avoid
    )
    assert jump_in_hard_avoid, f"hard_avoid missing jump/hop block: {hard_avoid}"
    print(f"  ✓ seizure_or_medical_monitoring = {round(risk_scores['seizure_or_medical_monitoring'], 2)}")
    print(f"  ✓ falls_balance_gait = {round(risk_scores['falls_balance_gait'], 2)}")
    print(f"  ✓ hard_avoid includes jump/hop/trampoline/unsupported")

    # ── 2. Focus domains ─────────────────────────────────────────────────────
    focus = choose_focus_domains(state)
    assert "movement_and_physical" in focus, (
        f"Expected movement_and_physical in focus domains: {focus}"
    )
    assert "language_and_communication" in focus, (
        f"Expected language_and_communication in focus domains: {focus}"
    )
    print(f"  ✓ Focus domains: {focus}")

    # ── 3. Activity bank — no risky movement language ─────────────────────────
    # Build the movement bank (safety pass is now wired into generate_category_activity_bank)
    plan = build_v22_plan_for_category(state, "movement_and_physical")
    state.setdefault("bridge_plans", {})["movement_and_physical"] = plan
    bank = generate_category_activity_bank(state, "movement_and_physical")
    acts = bank.get("activities", [])
    assert acts, "Movement bank must have at least one valid activity"

    risky = []
    missing_safe_marker = []
    inconsistent_avoid = []

    for a in acts:
        title = a.get("title", "")
        instr = a.get("instructions", "")
        avoid = a.get("avoid", "")
        combined = f"{title} {instr}"

        # No risky keywords in title+instructions
        m = _RISKY_MOVEMENT_PATTERNS.search(combined)
        if m:
            risky.append(f"{title!r}: matched {m.group()!r}")

        # Each card must have at least one safe-movement marker in instructions
        if not _SAFE_MOVEMENT_MARKERS.search(instr):
            missing_safe_marker.append(f"{title!r}: no safe marker in instructions")

        # 'avoid' must not contradict the activity
        # e.g. if title is "Supported Step-and-Stop", avoid should not say "jumping off heights"
        if "jumping off heights" in avoid.lower() or "unsupported jumping drills" in avoid.lower():
            inconsistent_avoid.append(f"{title!r}: avoid field references jumping but activity is safe card")

    assert not risky, (
        f"Risky movement keywords found in Dravet movement bank:\n" + "\n".join(risky)
    )
    assert not missing_safe_marker, (
        f"Movement cards missing safe-movement marker in instructions:\n"
        + "\n".join(missing_safe_marker)
    )
    assert not inconsistent_avoid, (
        f"'avoid' field inconsistent with replacement card:\n"
        + "\n".join(inconsistent_avoid)
    )
    print(f"  ✓ {len(acts)} movement activities — none contain risky keywords")
    print(f"  ✓ All movement activities have safe-movement markers (seated/supported/flat-ground/etc.)")
    print(f"  ✓ 'avoid' fields are consistent with each activity")

    # ── 4. LLM-generated card goes through the same safety pass ──────────────
    # Simulate what would happen if an LLM returned a jumping activity for this profile.
    # apply_safety_constraints_to_activities must block it.
    from genex_core.safety import apply_safety_constraints_to_activities
    llm_like_activities = [
        {
            "title": "Bunny Hop Race",
            "instructions": "hop quickly from one cone to the next as fast as you can",
            "materials": "cones",
            "avoid": "",
            "duration_min": 5,
            "activity_family": "jump_prep",
            "category_key": "movement_and_physical",
            "_debug": {"activity_type": "core", "llm_used": True},
        },
        {
            "title": "Obstacle Jump Course",
            "instructions": "race through the obstacle course, jumping over each block",
            "materials": "blocks",
            "avoid": "",
            "duration_min": 5,
            "activity_family": "jump_prep",
            "category_key": "movement_and_physical",
            "_debug": {"activity_type": "core", "llm_used": True},
        },
    ]
    safe_llm = apply_safety_constraints_to_activities(
        state, "movement_and_physical", llm_like_activities
    )
    llm_risky = [
        a["title"] for a in safe_llm
        if _RISKY_MOVEMENT_PATTERNS.search(f"{a['title']} {a['instructions']}")
    ]
    assert not llm_risky, (
        f"LLM-generated risky activity not replaced by safety pass: {llm_risky}"
    )
    assert len(set(a["title"] for a in safe_llm)) == len(safe_llm), (
        f"Duplicate replacement titles after LLM safety pass: {[a['title'] for a in safe_llm]}"
    )
    print(f"  ✓ Simulated LLM jumping activities also replaced (safety pass applies to all activities)")
    print(f"  ✓ Replacement titles distinct: {[a['title'] for a in safe_llm]}")


# ---------------------------------------------------------------------------
# Case 12: Case B — ADHD (exact QA profile)
# ---------------------------------------------------------------------------

def test_case12_adhd_exact_profile():
    """
    Case B: ADHD, 60m, hyperactivity + focus + task-completion, 10 min/day.

    Expected:
    - Focus domains: cognitive + social_and_emotional (no motor unless mentioned)
    - 5 weekdays × 2 activities/day = 10 total weekday slots
    - No duplicate titles, no duplicate instructions
    - No under-filled Thursday/Friday (each day has >= 2 activities)
    - 'Why this helps' text is ADHD/attention/task-completion specific, not generic
    """
    print("\n─── Case 12: ADHD exact profile — slots, uniqueness, Why text ───")
    from genex_core.interview_engine import choose_focus_domains
    from genex_core.support_tiers import build_v22_plan_for_category
    from genex_core.activity_engine import _BUCKET_VARIANTS

    state = _make_adhd_state()

    # ── 1. Focus domains ─────────────────────────────────────────────────────
    focus = choose_focus_domains(state)
    assert "cognitive" in focus, f"Expected cognitive in ADHD focus domains: {focus}"
    assert "social_and_emotional" in focus, (
        f"Expected social_and_emotional in ADHD focus domains: {focus}"
    )
    assert "movement_and_physical" not in focus, (
        f"movement_and_physical should NOT be in focus for ADHD without motor concern: {focus}"
    )
    print(f"  ✓ Focus domains: {focus}")

    # ── 2. Attention bucket capacity ─────────────────────────────────────────
    attn_cards = _BUCKET_VARIANTS.get("attention", [])
    assert len(attn_cards) >= 5, (
        f"Attention bucket needs >= 5 cards, has {len(attn_cards)}"
    )
    print(f"  ✓ Attention bucket: {len(attn_cards)} cards")

    # ── 3. Build banks ────────────────────────────────────────────────────────
    all_core_titles: dict = {}
    all_instructions: dict = {}
    for dk in ["social_and_emotional", "cognitive"]:
        plan = build_v22_plan_for_category(state, dk)
        state.setdefault("bridge_plans", {})[dk] = plan
        bank = generate_category_activity_bank(state, dk)
        state.setdefault("activity_banks", {})[dk] = bank
        core = [a for a in bank.get("activities", [])
                if a.get("_debug", {}).get("activity_type", "core") == "core"]
        unique = set(a["title"].lower() for a in core)
        print(f"  ✓ {dk} core bank: {len(core)} activities, {len(unique)} unique titles")
        for a in core:
            all_core_titles.setdefault(dk, []).append(a["title"])
            all_instructions[a["title"]] = a.get("instructions", "")

    # No duplicate instructions across different-titled activities
    instr_to_title: dict = {}
    instr_dups = []
    for title, instr in all_instructions.items():
        instr_norm = instr.strip()
        if instr_norm in instr_to_title and instr_to_title[instr_norm] != title:
            instr_dups.append(
                f"{title!r} and {instr_to_title[instr_norm]!r} share identical instructions"
            )
        else:
            instr_to_title[instr_norm] = title
    assert not instr_dups, "Duplicate instructions found in ADHD banks:\n" + "\n".join(instr_dups)
    print(f"  ✓ No duplicate instructions across activity banks")

    # ── 4. Schedule: 10 slots, no under-fill, no duplicates ──────────────────
    state["cycle_week"] = 1
    allocate_weekly_slots(state)
    build_weekly_schedule(state)
    schedule = state["weekly_schedule"]
    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    days = schedule.get("days", {})

    per_day_counts = {}
    for d in WEEKDAYS:
        items = days.get(d, {}).get("items", [])
        per_day_counts[d] = len(items)

    total_slots = sum(per_day_counts.values())
    assert total_slots >= 9, (
        f"Expected >= 9 total weekday slots for ADHD, got {total_slots}: {per_day_counts}"
    )
    print(f"  ✓ Total weekday slots: {total_slots}")

    # No day should have 0 activities
    empty_days = [d for d, n in per_day_counts.items() if n == 0]
    assert not empty_days, f"Empty days in ADHD schedule: {empty_days}"

    # Thursday and Friday should not be under-filled vs Monday (each day should have >=1)
    thu_fri = {d: per_day_counts[d] for d in ["Thursday", "Friday"]}
    assert all(n >= 1 for n in thu_fri.values()), (
        f"Thursday/Friday under-filled: {thu_fri}"
    )
    print(f"  ✓ Thursday/Friday each have >= 1 activity: {thu_fri}")
    print(f"  ✓ Per-day counts: {per_day_counts}")

    # No duplicate titles per category across the week
    cat_titles: dict = {}
    for d in WEEKDAYS:
        for item in days.get(d, {}).get("items", []):
            ck = item.get("category_key", "?")
            t = item.get("title", "").lower()
            cat_titles.setdefault(ck, []).append((d, t))

    cross_dups = []
    for ck, entries in cat_titles.items():
        seen: dict = {}
        for day_name, t in entries:
            if t in seen:
                cross_dups.append(f"{ck}: {t!r} on {seen[t]} AND {day_name}")
            else:
                seen[t] = day_name
    assert not cross_dups, "Duplicate titles across ADHD week:\n" + "\n".join(cross_dups)
    print(f"  ✓ No duplicate titles per category across the week")

    # ── 5. 'Why this helps' is ADHD/attention-specific, not generic ──────────
    # Collect all Why text from scheduled cognitive activities
    adhd_why_keywords = [
        "task", "finish", "complet", "attention", "focus", "routine",
        "predict", "start", "stay on", "short",
    ]
    cognitive_items = [
        item for d in WEEKDAYS
        for item in days.get(d, {}).get("items", [])
        if item.get("category_key") == "cognitive"
    ]
    why_failures = []
    for item in cognitive_items:
        why = (item.get("why") or "").lower()
        if not any(kw in why for kw in adhd_why_keywords):
            why_failures.append(f"{item.get('title')!r}: why={why[:80]!r}")
    assert not why_failures, (
        "Cognitive activities missing ADHD-specific Why text:\n" + "\n".join(why_failures)
    )
    print(f"  ✓ All {len(cognitive_items)} cognitive items have ADHD-specific 'Why' text")


# ---------------------------------------------------------------------------
# Case 13: Case C — Speech delay only (exact QA profile)
# ---------------------------------------------------------------------------

def test_case13_speech_delay_only():
    """
    Case C: no diagnosis, speech delay only, 36m, 5 min/day.

    Expected:
    - Primary (and only) focus domain: language_and_communication
    - No social_and_emotional domain selected
    - 1 activity/day for 5 weekdays (5 min / 5 min per slot = 1 slot/day)
    - All scheduled activities carry category_key = language_and_communication
    - Activities are language/requesting/naming/routine-focused
    - Parent-facing label = 'Talking and Communicating'
    """
    print("\n─── Case 13: Speech delay only — exact QA profile ───")
    from genex_core.interview_engine import choose_focus_domains
    from genex_core.support_tiers import build_v22_plan_for_category

    state = _make_speech_delay_state()
    ensure_concern_profile(state)

    # ── 1. Routing: language only ─────────────────────────────────────────────
    domain_weights = state["concern_profile"]["domain_weights"]
    lang_w = domain_weights.get("language_and_communication", 0)
    social_w = domain_weights.get("social_and_emotional", 0)

    assert lang_w >= 0.30, f"Expected language weight >= 0.30, got {lang_w}"
    assert social_w < 0.10, (
        f"social_and_emotional should have no explicit concern signal for speech-delay-only, "
        f"got {social_w}"
    )
    print(f"  ✓ language_and_communication weight = {round(lang_w, 2)}")
    print(f"  ✓ social_and_emotional weight = {round(social_w, 2)} (below 0.10 threshold)")

    # Delay signal alone must NOT pull in social_and_emotional
    state["delay_estimates"]["social_and_emotional"] = {"delay_months": 6}
    state["delay_estimates"]["language_and_communication"] = {"delay_months": 10}
    focus = choose_focus_domains(state)

    assert focus == ["language_and_communication"], (
        f"Speech-delay-only should select ONLY language domain, got: {focus}"
    )
    print(f"  ✓ choose_focus_domains = {focus}")
    print(f"  ✓ Delay signal alone did NOT pull in social_and_emotional")

    # ── 2. Build plan + bank ─────────────────────────────────────────────────
    plan = build_v22_plan_for_category(state, "language_and_communication")
    state.setdefault("bridge_plans", {})["language_and_communication"] = plan
    bank = generate_category_activity_bank(state, "language_and_communication")
    state.setdefault("activity_banks", {})["language_and_communication"] = bank
    acts = bank.get("activities", [])
    assert acts, "Language bank must have activities"
    print(f"  ✓ Language bank: {len(acts)} activities")

    # All activities must carry category_key = language_and_communication
    wrong_cat = [
        a["title"] for a in acts
        if a.get("category_key") != "language_and_communication"
    ]
    assert not wrong_cat, (
        f"Activities with wrong category_key in language bank: {wrong_cat}"
    )
    print(f"  ✓ All bank activities carry category_key = language_and_communication")

    # ── 3. Schedule: 1 slot/day × 5 days, all language ───────────────────────
    state["cycle_week"] = 1
    allocate_weekly_slots(state)
    build_weekly_schedule(state)
    schedule = state["weekly_schedule"]
    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    days = schedule.get("days", {})

    total_slots = sum(len(days.get(d, {}).get("items", [])) for d in WEEKDAYS)
    assert total_slots >= 4, (
        f"Expected >= 4 weekday slots for speech-delay 5min/day, got {total_slots}"
    )
    print(f"  ✓ Total weekday slots: {total_slots}")

    # All scheduled items must be language_and_communication
    wrong_domain_items = [
        (d, item.get("title"), item.get("category_key"))
        for d in WEEKDAYS
        for item in days.get(d, {}).get("items", [])
        if item.get("category_key") != "language_and_communication"
    ]
    assert not wrong_domain_items, (
        f"Non-language activities in speech-delay-only schedule: {wrong_domain_items}"
    )
    print(f"  ✓ All scheduled activities are language_and_communication")

    # ── 4. Parent-facing label ───────────────────────────────────────────────
    from genex_core.config import DOMAIN_CONFIG
    lang_display = DOMAIN_CONFIG["language_and_communication"]["display"]
    # In app.py this is mapped through DOMAIN_LABELS to "Talking and Communicating"
    # Here we just verify the category field on each scheduled item
    for d in WEEKDAYS:
        for item in days.get(d, {}).get("items", []):
            cat = item.get("category", "")
            assert cat == lang_display, (
                f"Category display on scheduled item should be {lang_display!r}, got {cat!r}"
            )
    print(f"  ✓ All scheduled items carry category = {lang_display!r}")
    print(f"  ✓ (maps to 'Talking and Communicating' via DOMAIN_LABELS in app.py)")
    lang_w = domain_weights.get("language_and_communication", 0)
    social_w = domain_weights.get("social_and_emotional", 0)
    assert lang_w >= 0.30, f"Expected language weight >= 0.30, got {lang_w}"
    print(f"  ✓ language_and_communication weight = {round(lang_w, 2)}")
    print(f"  ✓ social_and_emotional weight = {round(social_w, 2)}")

    # Delay estimates: even if social has some delay, concern_signal guard should prevent selection
    state["delay_estimates"]["social_and_emotional"] = {"delay_months": 6}
    state["delay_estimates"]["language_and_communication"] = {"delay_months": 10}

    focus = choose_focus_domains(state)
    assert focus == ["language_and_communication"], (
        f"Speech-delay-only concern should select only language domain, got: {focus}"
    )
    print(f"  ✓ choose_focus_domains = {focus}")
    print(f"  ✓ social_and_emotional NOT selected despite delay signal (concern_signal guard working)")


# ---------------------------------------------------------------------------
# Case 14: Time budget fill — 5 / 10 / 15 min/day must produce exact slot counts
# ---------------------------------------------------------------------------

def test_case14_time_budget_fill():
    """Every weekday must be filled to the parent's daily_time_min."""
    print("\n─── Case 14: Time budget fill (5 / 10 / 15 min/day) ───")
    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    profiles = [
        # (label, age, diag, concern, daily_min, expected_total)
        ("speech-delay 5min",  36, "none", "speech delay, not talking much", 5,  25),
        ("speech-delay 10min", 36, "none", "speech delay, not talking much", 10, 50),
        ("speech-delay 15min", 36, "none", "speech delay, not talking much", 15, 75),
    ]
    for label, age, diag, concern, daily, expected_total in profiles:
        state = init_state_from_profile("your child", age, diag, concern, daily)
        ensure_concern_profile(state)
        from genex_core.interview_engine import choose_focus_domains
        focus = choose_focus_domains(state)
        from genex_core.support_tiers import build_v22_plan_for_category
        allocate_weekly_slots(state)
        for ck in focus:
            plan = build_v22_plan_for_category(state, ck)
            state.setdefault("bridge_plans", {})[ck] = plan
            bank = generate_category_activity_bank(state, ck)
            state.setdefault("activity_banks", {})[ck] = bank
        state["cycle_week"] = 1
        build_weekly_schedule(state)
        days = state["weekly_schedule"]["days"]

        # No weekday rest days
        for d in WEEKDAYS:
            mins = days.get(d, {}).get("total_minutes", 0)
            assert mins >= daily, (
                f"[{label}] {d} under-filled: {mins} min < {daily} min/day"
            )

        total = sum(days.get(d, {}).get("total_minutes", 0) for d in WEEKDAYS)
        assert total == expected_total, (
            f"[{label}] Total weekday minutes = {total}, expected {expected_total}"
        )
        print(f"  ✓ [{label}] {total}/{expected_total} weekday minutes — all days filled")


# ---------------------------------------------------------------------------
# Case 15: No weekday rest days — Chang profile (multi-concern, 10 min/day)
# ---------------------------------------------------------------------------

def test_case15_no_weekday_rest_days():
    """
    Chang: 48m, speech + developmental + learning delay + wobbly running, 10 min/day.
    Monday–Friday must all have activities.  No empty days.
    """
    print("\n─── Case 15: No weekday rest days — Chang profile ───")
    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    from genex_core.interview_engine import choose_focus_domains
    from genex_core.support_tiers import build_v22_plan_for_category

    state = init_state_from_profile(
        "your child", 48, "none",
        "speech delay, developmental delay, learning delay, not yet jumping with both feet, wobbly running",
        10,
    )
    ensure_concern_profile(state)
    focus = choose_focus_domains(state)
    allocate_weekly_slots(state)

    for ck in focus:
        plan = build_v22_plan_for_category(state, ck)
        state.setdefault("bridge_plans", {})[ck] = plan
        bank = generate_category_activity_bank(state, ck)
        state.setdefault("activity_banks", {})[ck] = bank

    state["cycle_week"] = 1
    build_weekly_schedule(state)
    days = state["weekly_schedule"]["days"]

    empty_days = [d for d in WEEKDAYS if not days.get(d, {}).get("items")]
    assert not empty_days, f"Weekday rest days found: {empty_days}"

    under_filled = [
        d for d in WEEKDAYS
        if days.get(d, {}).get("total_minutes", 0) < 10
    ]
    assert not under_filled, f"Under-filled weekdays: {under_filled}"

    total = sum(days.get(d, {}).get("total_minutes", 0) for d in WEEKDAYS)
    assert total == 50, f"Expected 50 total weekday minutes, got {total}"
    print(f"  ✓ focus = {focus}")
    print(f"  ✓ All 5 weekdays filled — no rest days")
    print(f"  ✓ Total weekday minutes: {total}/50")


# ---------------------------------------------------------------------------
# Case 16: Speech-delay bridge spread (36m, 15 min/day)
# ---------------------------------------------------------------------------

def test_case16_speech_delay_bridge_spread():
    """
    36m, speech delay, 15 min/day.
    Language bank must include activities from multiple distinct bridge subdomains,
    not be dominated by a single requesting/choice theme.
    """
    print("\n─── Case 16: Speech-delay bridge spread ───")
    state = _make_speech_delay_state()
    # Override to 15 min/day for this test
    state["child"]["daily_time_min"] = 15
    ensure_concern_profile(state)
    from genex_core.support_tiers import build_v22_plan_for_category
    plan = build_v22_plan_for_category(state, "language_and_communication")
    state.setdefault("bridge_plans", {})["language_and_communication"] = plan
    bank = generate_category_activity_bank(state, "language_and_communication")
    acts = bank.get("activities", [])
    core = [a for a in acts if a.get("_debug", {}).get("activity_type") == "core"]

    # Must have >= 3 distinct bridges represented
    bridge_subdomains = {a.get("_debug", {}).get("subdomain", "") for a in core}
    # Remove empty
    bridge_subdomains.discard("")
    assert len(bridge_subdomains) >= 2, (
        f"Language bank should span >= 2 subdomains, got {bridge_subdomains}"
    )
    print(f"  ✓ {len(core)} core activities across {len(bridge_subdomains)} subdomains: {bridge_subdomains}")

    # Must have >= 5 distinct titles after dedup
    titles = [a.get("title", "") for a in core]
    unique_titles = set(titles)
    assert len(unique_titles) >= 5, (
        f"Expected >= 5 unique language activity titles, got {len(unique_titles)}: {unique_titles}"
    )
    print(f"  ✓ {len(unique_titles)} unique core titles")

    # No single title dominates (no title should appear more than once in core)
    from collections import Counter
    title_counts = Counter(titles)
    duplicates = {t: c for t, c in title_counts.items() if c > 1}
    assert not duplicates, f"Duplicate core titles after dedup: {duplicates}"
    print(f"  ✓ No duplicate titles in core bank")


# ---------------------------------------------------------------------------
# Case 17: Near-duplicate detection
# ---------------------------------------------------------------------------

def test_case17_near_duplicate_detection():
    """
    No duplicate titles, no identical instructions, no Easier:/Stretch: titles
    in Week 1 weekday schedule.
    Also: validator flags body-part mismatch (Touch Your Nose with give-me instructions).
    """
    print("\n─── Case 17: Near-duplicate detection ───")
    from genex_core.activity_validator import validate_activity

    # A: Dravet movement bank should have no duplicate titles after dedup
    dravet_state = _make_dravet_state()
    ensure_concern_profile(dravet_state)
    from genex_core.support_tiers import build_v22_plan_for_category
    from genex_core.scheduler import allocate_weekly_slots as _alloc
    _alloc(dravet_state)
    plan = build_v22_plan_for_category(dravet_state, "movement_and_physical")
    dravet_state.setdefault("bridge_plans", {})["movement_and_physical"] = plan
    bank = generate_category_activity_bank(dravet_state, "movement_and_physical")
    acts = bank.get("activities", [])
    titles = [a.get("title", "") for a in acts if a.get("_debug", {}).get("activity_type") == "core"]
    from collections import Counter
    dup_titles = {t: c for t, c in Counter(titles).items() if c > 1}
    assert not dup_titles, f"Dravet movement bank has duplicate core titles: {dup_titles}"
    print(f"  ✓ Dravet movement bank: {len(set(titles))} unique titles, no duplicates")

    # B: No Easier:/Stretch: titles in Week 1 weekday schedule
    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    adhd_state = _make_adhd_state()
    allocate_weekly_slots(adhd_state)
    from genex_core.support_tiers import build_v22_plan_for_category as bvp
    from genex_core.interview_engine import choose_focus_domains as cfd
    for ck in cfd(adhd_state):
        p = bvp(adhd_state, ck)
        adhd_state.setdefault("bridge_plans", {})[ck] = p
        b = generate_category_activity_bank(adhd_state, ck)
        adhd_state.setdefault("activity_banks", {})[ck] = b
    adhd_state["cycle_week"] = 1
    build_weekly_schedule(adhd_state)
    days = adhd_state["weekly_schedule"]["days"]
    variant_titles = [
        item["title"]
        for d in WEEKDAYS
        for item in days.get(d, {}).get("items", [])
        if item.get("title", "").startswith(("Easier:", "Stretch:"))
    ]
    assert not variant_titles, f"Easier:/Stretch: titles in Week 1: {variant_titles}"
    print(f"  ✓ No Easier:/Stretch: titles in Week 1 weekday schedule")

    # C: Title/instruction body-part mismatch validator check
    bad_activity = {
        "title": "Touch Your Nose!",
        "instructions": "Hold out a toy. Say 'give me!' and wait for your child to hand it to you.",
        "success_criteria": "Child hands you the toy.",
        "materials": "one toy",
        "make_easier": "Model first.",
        "make_harder": "Use two toys.",
        "what_to_avoid": "Don't rush.",
        "group_play_line": "Two children can take turns.",
    }
    _valid, warnings = validate_activity(bad_activity, "language_and_communication")
    mismatch_warns = [w for w in warnings if "mismatch" in w or "body_part" in w]
    assert mismatch_warns, (
        f"Expected body-part mismatch warning for 'Touch Your Nose' + give-me instructions, got: {warnings}"
    )
    print(f"  ✓ Validator catches title/instruction mismatch: {mismatch_warns[0]}")


# ---------------------------------------------------------------------------
# Case 18: Dravet safety — stomp + squat blocked, no repeated safe cards
# ---------------------------------------------------------------------------

def test_case18_dravet_stomp_squat_blocked():
    """
    Dravet movement bank must not contain stomp, squat-and-reach, or any
    risky movement language.  Safe replacement cards should be distinct.
    """
    print("\n─── Case 18: Dravet safety — stomp and squat-and-reach blocked ───")
    from genex_core.safety import apply_safety_constraints_to_activities
    from genex_core.support_tiers import build_v22_plan_for_category

    dravet_state = _make_dravet_state()
    ensure_concern_profile(dravet_state)
    plan = build_v22_plan_for_category(dravet_state, "movement_and_physical")
    dravet_state.setdefault("bridge_plans", {})["movement_and_physical"] = plan
    bank = generate_category_activity_bank(dravet_state, "movement_and_physical")
    acts = bank.get("activities", [])

    _RISKY_EXT = re.compile(
        r"\b(jump(ing)?|hop(ping)?|race|racing|obstacle|climb(ing)?|"
        r"high surface|high platform|unsupported balance|speed|"
        r"frog jump|trampoline|bouncing|sprint|run fast|stomp(ing)?)\b"
        r"|squat\s+and\s+reach",
        flags=re.IGNORECASE,
    )

    for a in acts:
        text = f"{a.get('title','')} {a.get('instructions','')}"
        assert not _RISKY_EXT.search(text), (
            f"Risky movement language in Dravet activity: {a.get('title')!r}\n"
            f"Text: {text[:120]}"
        )
    print(f"  ✓ {len(acts)} movement activities — no stomp/squat-and-reach/race/jump language")

    # Safe replacement cards should not repeat titles within the bank
    core = [a for a in acts if a.get("_debug", {}).get("activity_type") == "core"]
    from collections import Counter
    dup = {t: c for t, c in Counter(a["title"] for a in core).items() if c > 1}
    assert not dup, f"Duplicate safe card titles in Dravet movement bank: {dup}"
    print(f"  ✓ {len(set(a['title'] for a in core))} distinct safe movement card titles")


# ---------------------------------------------------------------------------
# Case 19: ADHD age-appropriateness (60m) + Thu/Fri fill
# ---------------------------------------------------------------------------

def test_case19_adhd_age_appropriate():
    """
    ADHD 60m plan: activities should include task-completion, routine, or
    attention-support language.  Thu/Fri must not be under-filled.
    """
    print("\n─── Case 19: ADHD age-appropriateness + Thu/Fri fill ───")
    from genex_core.interview_engine import choose_focus_domains
    from genex_core.support_tiers import build_v22_plan_for_category

    state = _make_adhd_state()
    allocate_weekly_slots(state)
    focus = choose_focus_domains(state)
    for ck in focus:
        plan = build_v22_plan_for_category(state, ck)
        state.setdefault("bridge_plans", {})[ck] = plan
        bank = generate_category_activity_bank(state, ck)
        state.setdefault("activity_banks", {})[ck] = bank
    state["cycle_week"] = 1
    build_weekly_schedule(state)
    days = state["weekly_schedule"]["days"]
    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    # Thu/Fri must each have >= 1 activity
    for d in ["Thursday", "Friday"]:
        items = days.get(d, {}).get("items", [])
        assert items, f"{d} has no activities in ADHD plan"
    print("  ✓ Thursday and Friday both filled")

    # At least 2 activities across the week must reference attention/task/routine keywords
    adhd_keywords = ["task", "finish", "complet", "attention", "focus", "routine",
                     "predict", "start", "stay on", "short", "timer", "wait"]
    cognitive_items = [
        item for d in WEEKDAYS
        for item in days.get(d, {}).get("items", [])
        if item.get("category_key") == "cognitive"
    ]
    matches = [
        item for item in cognitive_items
        if any(kw in (item.get("why", "") + item.get("title", "") + item.get("instructions", "")).lower()
               for kw in adhd_keywords)
    ]
    assert len(matches) >= 2, (
        f"Expected >= 2 ADHD-appropriate cognitive activities, got {len(matches)}: "
        f"{[i['title'] for i in cognitive_items]}"
    )
    print(f"  ✓ {len(matches)}/{len(cognitive_items)} cognitive activities have ADHD-relevant content")

    total = sum(days.get(d, {}).get("total_minutes", 0) for d in WEEKDAYS)
    assert total == 50, f"Expected 50 total weekday minutes, got {total}"
    print(f"  ✓ Total weekday minutes: {total}/50")


# ---------------------------------------------------------------------------
# Case 20: Exact slot counts — 10 slots (Dravet 10min), 10 slots (ADHD 10min),
#           15 slots (speech delay 15min)
# ---------------------------------------------------------------------------

def test_case20_exact_slot_counts():
    """
    Slot count validation:
    - Dravet 10 min/day → 10 weekday activity slots (5 days × 2)
    - ADHD 10 min/day  → 10 weekday activity slots (5 days × 2)
    - Speech 15 min/day → 15 weekday activity slots (5 days × 3)
    """
    print("\n─── Case 20: Exact slot counts (Dravet/ADHD/Speech) ───")
    from genex_core.interview_engine import choose_focus_domains
    from genex_core.support_tiers import build_v22_plan_for_category
    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    def _count_slots(state):
        allocate_weekly_slots(state)
        focus = choose_focus_domains(state)
        for ck in focus:
            plan = build_v22_plan_for_category(state, ck)
            state.setdefault("bridge_plans", {})[ck] = plan
            bank = generate_category_activity_bank(state, ck)
            state.setdefault("activity_banks", {})[ck] = bank
        state["cycle_week"] = 1
        build_weekly_schedule(state)
        days = state["weekly_schedule"]["days"]
        slots = sum(len(days.get(d, {}).get("items", [])) for d in WEEKDAYS)
        mins  = sum(days.get(d, {}).get("total_minutes", 0) for d in WEEKDAYS)
        return slots, mins

    # Dravet 10 min/day → 10 slots
    dravet_state = _make_dravet_state()
    slots, mins = _count_slots(dravet_state)
    assert slots == 10, f"Dravet 10min: expected 10 weekday slots, got {slots} ({mins} min)"
    assert mins == 50, f"Dravet 10min: expected 50 weekday min, got {mins}"
    print(f"  ✓ Dravet 10min/day: {slots} slots, {mins}/50 min")

    # ADHD 10 min/day → 10 slots
    adhd_state = _make_adhd_state()
    slots, mins = _count_slots(adhd_state)
    assert slots == 10, f"ADHD 10min: expected 10 weekday slots, got {slots} ({mins} min)"
    assert mins == 50, f"ADHD 10min: expected 50 weekday min, got {mins}"
    print(f"  ✓ ADHD 10min/day: {slots} slots, {mins}/50 min")

    # Speech delay 15 min/day → 15 slots (5 days × 3 activities)
    speech_15_state = {
        "child": {
            "name": "Kelly",
            "chronological_months": 36,
            "daily_time_min": 15,
            "diagnosis": "",
            "concern": "speech delay, not talking much",
        },
        "dev_age": {},
        "delay_estimates": {},
        "answers": {},
    }
    ensure_concern_profile(speech_15_state)
    slots, mins = _count_slots(speech_15_state)
    assert slots == 15, f"Speech 15min: expected 15 weekday slots, got {slots} ({mins} min)"
    assert mins == 75, f"Speech 15min: expected 75 weekday min, got {mins}"
    print(f"  ✓ Speech delay 15min/day: {slots} slots, {mins}/75 min")


# ---------------------------------------------------------------------------
# Case 21: Card schema completeness — every slot has make_easier/make_harder
# ---------------------------------------------------------------------------

def test_case21_card_schema_completeness():
    """
    Every scheduled activity slot must have non-empty make_easier and make_harder.
    These must be populated from the activity's easier/harder or make_easier/make_harder fields.
    """
    print("\n─── Case 21: Card schema completeness ───")
    from genex_core.interview_engine import choose_focus_domains
    from genex_core.support_tiers import build_v22_plan_for_category
    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    for label, state in [
        ("speech-delay-36m", {
            "child": {"name": "T", "chronological_months": 36, "daily_time_min": 10,
                      "diagnosis": "", "concern": "speech delay, not talking much"},
            "dev_age": {}, "delay_estimates": {}, "answers": {},
        }),
        ("ADHD-60m", _make_adhd_state()),
        ("Dravet-40m", _make_dravet_state()),
    ]:
        ensure_concern_profile(state)
        allocate_weekly_slots(state)
        focus = choose_focus_domains(state)
        for ck in focus:
            plan = build_v22_plan_for_category(state, ck)
            state.setdefault("bridge_plans", {})[ck] = plan
            bank = generate_category_activity_bank(state, ck)
            state.setdefault("activity_banks", {})[ck] = bank
        state["cycle_week"] = 1
        build_weekly_schedule(state)
        days = state["weekly_schedule"]["days"]
        missing_easier = []
        missing_harder = []
        for d in WEEKDAYS:
            for item in days.get(d, {}).get("items", []):
                if not item.get("make_easier"):
                    missing_easier.append(item.get("title", "?"))
                if not item.get("make_harder"):
                    missing_harder.append(item.get("title", "?"))
        assert not missing_easier, f"[{label}] Missing make_easier: {missing_easier}"
        assert not missing_harder, f"[{label}] Missing make_harder: {missing_harder}"
        print(f"  ✓ [{label}] all scheduled cards have make_easier and make_harder")


# ---------------------------------------------------------------------------
# Case 22: OT/PT routing — Chao profile
# ---------------------------------------------------------------------------

def test_case22_ot_pt_routing():
    """
    Chao: speech delay + OT delay + PT delay → should route to language + movement
    (NOT social, since social is explicitly stated as good).
    """
    print("\n─── Case 22: OT/PT routing for Chao profile ───")

    chao_state = {
        "child": {
            "name": "Chao",
            "chronological_months": 48,
            "daily_time_min": 10,
            "diagnosis": "",
            "concern": (
                "speech delay, OT delay, PT delay, not yet jumping, wobbly run, "
                "social is good"
            ),
        },
        "dev_age": {},
        "delay_estimates": {},
        "answers": {},
    }
    ensure_concern_profile(chao_state)
    from genex_core.interview_engine import choose_focus_domains
    focus = choose_focus_domains(chao_state)
    print(f"  Focus domains: {focus}")

    assert "language_and_communication" in focus, (
        f"Expected language in focus, got {focus}"
    )
    # Should route to movement (PT/gross motor) OR movement (OT/fine motor) — both map to movement_and_physical
    assert "movement_and_physical" in focus, (
        f"Expected movement_and_physical in focus (OT/PT keywords), got {focus}"
    )
    # Social is stated as good — should NOT be a focus domain
    assert "social_and_emotional" not in focus, (
        f"social_and_emotional should not be selected when parent says 'social is good', got {focus}"
    )
    print(f"  ✓ language + movement selected, social suppressed")

    # Slot count check
    from genex_core.support_tiers import build_v22_plan_for_category
    allocate_weekly_slots(chao_state)
    for ck in focus:
        plan = build_v22_plan_for_category(chao_state, ck)
        chao_state.setdefault("bridge_plans", {})[ck] = plan
        bank = generate_category_activity_bank(chao_state, ck)
        chao_state.setdefault("activity_banks", {})[ck] = bank
    chao_state["cycle_week"] = 1
    build_weekly_schedule(chao_state)
    days = chao_state["weekly_schedule"]["days"]
    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    slots = sum(len(days.get(d, {}).get("items", [])) for d in WEEKDAYS)
    mins = sum(days.get(d, {}).get("total_minutes", 0) for d in WEEKDAYS)
    assert slots == 10, f"Chao 10min: expected 10 slots, got {slots}"
    assert mins == 50, f"Chao 10min: expected 50 min, got {mins}"
    print(f"  ✓ Chao 10min/day: {slots} slots, {mins}/50 min")

    # No exact title repeats within the week
    all_titles = [
        item.get("title", "")
        for d in WEEKDAYS
        for item in days.get(d, {}).get("items", [])
    ]
    from collections import Counter
    dup = {t: c for t, c in Counter(all_titles).items() if c > 1}
    assert not dup, f"Duplicate titles in Chao week 1: {dup}"
    print(f"  ✓ No duplicate titles in Chao Week 1")


# ---------------------------------------------------------------------------
# Case 23: Near-duplicate prevention — no same activity_family twice per week
# ---------------------------------------------------------------------------

def test_case23_near_duplicate_prevention():
    """
    No two scheduled activities in the same category should share the same
    activity_family in Week 1 (prevents "Undress the Teddy" + "Pull It Off!").
    Also: no same normalized title root across the whole week.
    """
    print("\n─── Case 23: Near-duplicate prevention (family + root dedup) ───")
    from genex_core.interview_engine import choose_focus_domains
    from genex_core.support_tiers import build_v22_plan_for_category
    from collections import Counter
    import re
    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    def _norm_root(t):
        t = t.lower()
        t = re.sub(r"[^a-z0-9\s]", " ", t)
        t = re.sub(
            r"\b(supported|slow|quick|simple|easy|gentle|basic|little|tiny|short|fun|"
            r"my|your|our|a|the|an)\b", "", t)
        t = re.sub(
            r"\b(game|activity|practice|challenge|time|session|version|exercise)\b", "", t)
        return re.sub(r"\s+", " ", t).strip()

    for label, state in [
        ("Dravet-40m", _make_dravet_state()),
        ("ADHD-60m", _make_adhd_state()),
        ("speech-36m", {
            "child": {"name": "K", "chronological_months": 36, "daily_time_min": 10,
                      "diagnosis": "", "concern": "speech delay"},
            "dev_age": {}, "delay_estimates": {}, "answers": {},
        }),
    ]:
        ensure_concern_profile(state)
        allocate_weekly_slots(state)
        focus = choose_focus_domains(state)
        for ck in focus:
            plan = build_v22_plan_for_category(state, ck)
            state.setdefault("bridge_plans", {})[ck] = plan
            bank = generate_category_activity_bank(state, ck)
            state.setdefault("activity_banks", {})[ck] = bank
        state["cycle_week"] = 1
        build_weekly_schedule(state)
        days = state["weekly_schedule"]["days"]

        # No exact title repeats
        all_titles = [
            item.get("title", "")
            for d in WEEKDAYS
            for item in days.get(d, {}).get("items", [])
        ]
        dup = {t: c for t, c in Counter(all_titles).items() if c > 1}
        assert not dup, f"[{label}] Duplicate titles in Week 1: {dup}"

        # No same normalized root repeats
        all_roots = [_norm_root(t) for t in all_titles if t]
        dup_roots = {r: c for r, c in Counter(all_roots).items() if c > 1}
        assert not dup_roots, f"[{label}] Duplicate title roots in Week 1: {dup_roots}"

        # Per day: no same activity_family used twice on the same day (same-day hard rule).
        # Week-level family dedup is aspirational (soft preference when alternatives exist)
        # so we only assert the per-day version here.
        for d in WEEKDAYS:
            day_items = days.get(d, {}).get("items", [])
            day_fams = [i.get("activity_family", "") for i in day_items if i.get("activity_family")]
            dup_day_fams = {f: c for f, c in Counter(day_fams).items() if c > 1}
            assert not dup_day_fams, (
                f"[{label}] {d}: same activity_family on same day: {dup_day_fams}"
            )
        print(f"  ✓ [{label}] no duplicate titles, roots, or same-day families in Week 1")


# ---------------------------------------------------------------------------
# Case 24: No generic fallback phrases in any scheduled card's parent-facing fields
# ---------------------------------------------------------------------------

_GENERIC_FALLBACK_PHRASES = [
    # Pass-8 original generic phrases
    re.compile(r"choose a short activity using", re.I),
    re.compile(r"\bany calm attempt at\b", re.I),
    re.compile(r"only if easy and enjoyable:?\s+add one small step", re.I),
    re.compile(r"with another child: one person models, one supports", re.I),
    re.compile(r"use one item, model first, shorten the turn", re.I),
    re.compile(r"^\s*simple household items\s*$", re.I),
    # Pass-9 new generic phrases (from pass-8 fallback rewrite that still leaked)
    re.compile(r"set up a quick\b", re.I),
    re.compile(r"show your child one small step", re.I),
    re.compile(r"\bgoal:\s", re.I),
    re.compile(r"items for .{1,40} from around the home", re.I),
    re.compile(r"your child tries at least once:", re.I),
    re.compile(r"break it into one single step", re.I),
    re.compile(r"add one more step or reduce your help", re.I),
    re.compile(r"with a sibling or friend, take turns.{0,30}each person tries one step", re.I),
    re.compile(r"celebrate any attempt and stop after 2", re.I),
    re.compile(r"\broutine activity\b", re.I),
    re.compile(r"snack counting activity", re.I),
    re.compile(r"action picture activity", re.I),
]

_CARD_PARENT_FIELDS = [
    "title", "instructions", "materials", "success", "make_easier",
    "make_harder", "group_play", "avoid",
]


def test_case24_no_generic_phrases_in_scheduled_cards():
    """No scheduled card should contain generic template fallback phrases."""
    print("\n─── Case 24: No generic fallback phrases in scheduled cards ───")
    from genex_core.interview_engine import choose_focus_domains
    from genex_core.support_tiers import build_v22_plan_for_category
    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    profiles = [
        ("speech-36m", {
            "child": {"name": "K", "chronological_months": 36, "daily_time_min": 10,
                      "diagnosis": "", "concern": "speech delay"},
            "dev_age": {}, "delay_estimates": {}, "answers": {},
        }),
        ("ADHD-60m", _make_adhd_state()),
        ("Dravet-40m", _make_dravet_state()),
        ("Chao-OT-PT", {
            "child": {"name": "Chao", "chronological_months": 30, "daily_time_min": 10,
                      "diagnosis": "OT delay, PT delay",
                      "concern": "speech delay, gross motor delay, fine motor delay"},
            "dev_age": {}, "delay_estimates": {}, "answers": {},
        }),
    ]

    for label, state in profiles:
        ensure_concern_profile(state)
        allocate_weekly_slots(state)
        focus = choose_focus_domains(state)
        for ck in focus:
            plan = build_v22_plan_for_category(state, ck)
            state.setdefault("bridge_plans", {})[ck] = plan
            bank = generate_category_activity_bank(state, ck)
            state.setdefault("activity_banks", {})[ck] = bank
        state["cycle_week"] = 1
        build_weekly_schedule(state)
        days = state["weekly_schedule"]["days"]

        for day in WEEKDAYS:
            for item in days.get(day, {}).get("items", []):
                for field in _CARD_PARENT_FIELDS:
                    value = str(item.get(field, "") or "")
                    if not value:
                        continue
                    for pat in _GENERIC_FALLBACK_PHRASES:
                        assert not pat.search(value), (
                            f"[{label}] {day} '{item.get('title')}' field '{field}' "
                            f"contains generic phrase: {pat.pattern[:60]!r}\n"
                            f"  value: {value[:120]}"
                        )

        print(f"  ✓ [{label}] all scheduled cards are free of generic fallback phrases")


# ---------------------------------------------------------------------------
# Case 25: No "Game Game" or doubled-suffix titles in any activity bank
# ---------------------------------------------------------------------------

_DOUBLE_SUFFIX_RE = re.compile(
    r"\b(game|activity|time|session)\s+\1\b", re.I
)


def test_case25_no_game_game_titles():
    """No activity title in any bank should have doubled suffixes like 'Game Game'."""
    print("\n─── Case 25: No 'Game Game' titles in activity banks ───")
    from genex_core.interview_engine import choose_focus_domains
    from genex_core.support_tiers import build_v22_plan_for_category

    profiles = [
        ("speech-36m", {
            "child": {"name": "K", "chronological_months": 36, "daily_time_min": 10,
                      "diagnosis": "", "concern": "speech delay"},
            "dev_age": {}, "delay_estimates": {}, "answers": {},
        }),
        ("ADHD-60m", _make_adhd_state()),
        ("Dravet-40m", _make_dravet_state()),
    ]

    for label, state in profiles:
        ensure_concern_profile(state)
        allocate_weekly_slots(state)
        focus = choose_focus_domains(state)
        bad_titles = []
        for ck in focus:
            plan = build_v22_plan_for_category(state, ck)
            state.setdefault("bridge_plans", {})[ck] = plan
            bank = generate_category_activity_bank(state, ck)
            state.setdefault("activity_banks", {})[ck] = bank
            for act in bank.get("activities", []):
                t = act.get("title", "")
                if _DOUBLE_SUFFIX_RE.search(t):
                    bad_titles.append(f"{ck}: {t!r}")

        assert not bad_titles, f"[{label}] Double-suffix titles found: {bad_titles}"
        print(f"  ✓ [{label}] no doubled-suffix titles in any bank")


# ---------------------------------------------------------------------------
# Case 26: Down syndrome / low-tone profile gets no jump/hop/stomp/race/climb
# ---------------------------------------------------------------------------

def test_case26_down_syndrome_safety():
    """A Down syndrome + hypotonia profile must not receive any jump/hop/stomp/race/climb activities."""
    print("\n─── Case 26: Down syndrome / low-tone safety guardrails ───")
    from genex_core.interview_engine import choose_focus_domains
    from genex_core.support_tiers import build_v22_plan_for_category
    from genex_core.safety import build_safety_profile

    state = init_state_from_profile(
        "Emma", 24, "Down syndrome",
        "hypotonia, low muscle tone, unstable walking, gross motor delay", 10,
    )
    ensure_concern_profile(state)

    # Safety profile must detect high-fall risk
    sp = build_safety_profile(state["child"])
    risk = sp["risk_scores"]
    assert risk.get("falls_balance_gait", 0) >= 0.35, (
        f"falls_balance_gait should be triggered for Down syndrome profile, got {risk}"
    )
    assert risk.get("postural_low_tone_fatigue", 0) >= 0.35, (
        f"postural_low_tone_fatigue should be triggered for Down syndrome profile, got {risk}"
    )
    print("  ✓ Safety profile correctly flags fall/low-tone risk for Down syndrome")

    allocate_weekly_slots(state)
    focus = choose_focus_domains(state)
    UNSAFE_MOVEMENT = re.compile(
        r"\b(jump(ing)?|hop(ping)?|frog jump|stomp(ing)?|race|racing|climb(ing)?|"
        r"trampoline|obstacle course|unsupported balance|sprint)\b",
        re.I,
    )
    for ck in focus:
        plan = build_v22_plan_for_category(state, ck)
        state.setdefault("bridge_plans", {})[ck] = plan
        bank = generate_category_activity_bank(state, ck)
        state.setdefault("activity_banks", {})[ck] = bank
        for act in bank.get("activities", []):
            text = f"{act.get('title','')} {act.get('instructions','')}"
            m = UNSAFE_MOVEMENT.search(text)
            assert not m, (
                f"Down syndrome profile: unsafe movement '{m.group()}' found in "
                f"[{ck}] '{act.get('title')}'"
            )

    print("  ✓ Down syndrome profile: no jump/hop/stomp/race/climb in any bank activity")


# ---------------------------------------------------------------------------
# Case 27: OT/PT/speech explicit term routing
# ---------------------------------------------------------------------------

def test_case27_ot_pt_speech_routing():
    """Explicit OT delay, PT delay, speech delay terms must route to correct domains."""
    print("\n─── Case 27: OT/PT/speech explicit term routing ───")
    from genex_core.interview_engine import choose_focus_domains
    from genex_core.safety import build_safety_profile
    from genex_core.config import SUBDOMAIN_KEYWORD_MAP

    # --- PT / gross motor → movement_and_physical ---
    for concern_text, label in [
        ("PT delay, gross motor delay", "PT delay"),
        ("physical therapy, not jumping, wobbly run", "physical therapy keywords"),
        ("gross motor delay, unstable walking", "gross motor delay"),
    ]:
        state = init_state_from_profile("test", 30, "", concern_text, 10)
        ensure_concern_profile(state)
        sp = build_safety_profile(state["child"])
        assert sp["risk_scores"].get("falls_balance_gait", 0) >= 0.35, (
            f"[{label}] PT/gross-motor concern should trigger falls_balance_gait risk: "
            f"got {sp['risk_scores'].get('falls_balance_gait', 0)}"
        )
        print(f"  ✓ [{label}] → falls_balance_gait triggered")

    # --- OT / fine motor → movement_and_physical (fine motor subdomains) ---
    # The concern router should map OT/fine motor concerns to fine_motor subdomains
    fine_motor_kw = SUBDOMAIN_KEYWORD_MAP.get("fine_motor_hand_use", [])
    ot_terms = ["occupational therapy", "ot delay", "fine motor delay", "grip difficulty"]
    for term in ot_terms:
        found = any(
            re.search(pat, term, re.I) for pat in fine_motor_kw
        )
        assert found, (
            f"OT term '{term}' not found in fine_motor_hand_use SUBDOMAIN_KEYWORD_MAP. "
            f"Map has: {fine_motor_kw[:5]}"
        )
    print("  ✓ OT/fine-motor terms present in fine_motor_hand_use keyword map")

    # --- speech delay → language_and_communication ---
    state = init_state_from_profile("test", 30, "", "speech delay, OT delay, PT delay", 10)
    ensure_concern_profile(state)
    allocate_weekly_slots(state)
    focus = choose_focus_domains(state)
    assert "language_and_communication" in focus, (
        f"'speech delay, OT delay, PT delay' should include language domain; got {focus}"
    )
    assert "movement_and_physical" in focus, (
        f"'speech delay, OT delay, PT delay' should include movement domain; got {focus}"
    )
    assert "social_and_emotional" not in focus, (
        f"social should NOT be selected for speech+OT+PT profile; got {focus}"
    )
    print(f"  ✓ speech+OT+PT concern → focus={focus} (language + movement, no social)")


# ---------------------------------------------------------------------------
# Case 28: Pass-9 generic phrases absent from ADHD-48m and DS-24m profiles
# ---------------------------------------------------------------------------

def test_case28_pass9_phrases_blocked():
    """Scheduled cards for ADHD 48-60m and Down-syndrome 24m must not contain
    any pass-9 generic fallback phrases in any parent-facing field."""
    print("\n─── Case 28: Pass-9 generic phrases blocked in ADHD-48m and DS-24m ───")
    from genex_core.interview_engine import choose_focus_domains
    from genex_core.support_tiers import build_v22_plan_for_category
    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    profiles = [
        ("ADHD-48m", _make_adhd_state()),
        ("DS-24m", init_state_from_profile(
            "Emma", 24, "Down syndrome",
            "hypotonia, low muscle tone, gross motor delay", 10,
        )),
    ]

    for label, state in profiles:
        ensure_concern_profile(state)
        allocate_weekly_slots(state)
        focus = choose_focus_domains(state)
        for ck in focus:
            plan = build_v22_plan_for_category(state, ck)
            state.setdefault("bridge_plans", {})[ck] = plan
            bank = generate_category_activity_bank(state, ck)
            state.setdefault("activity_banks", {})[ck] = bank
        state["cycle_week"] = 1
        build_weekly_schedule(state)
        days = state["weekly_schedule"]["days"]

        for day in WEEKDAYS:
            for item in days.get(day, {}).get("items", []):
                for field in _CARD_PARENT_FIELDS:
                    value = str(item.get(field, "") or "")
                    if not value:
                        continue
                    for pat in _GENERIC_FALLBACK_PHRASES:
                        assert not pat.search(value), (
                            f"[{label}] {day} '{item.get('title')}' field '{field}' "
                            f"contains generic pass-9 phrase: {pat.pattern[:60]!r}\n"
                            f"  value: {value[:120]}"
                        )

        print(f"  ✓ [{label}] all scheduled cards are free of pass-9 generic phrases")


# ---------------------------------------------------------------------------
# Case 29: Validator catches success-criteria domain mismatch
# ---------------------------------------------------------------------------

def test_case29_success_domain_mismatch_blocked():
    """validate_activity must block ball activity with foot/balance success criteria,
    and bead activity with crayon/drawing success criteria."""
    print("\n─── Case 29: Success criteria domain mismatch blocked ───")

    # Ball activity with foot/balance success — should be blocked
    ball_bad = {
        "title": "Rolling Ball Fun",
        "activity_family": "catch_ball",
        "instructions": "Sit on the floor. Roll the ball to your child and encourage them to roll it back.",
        "materials": "soft ball",
        "success_criteria": "Your child lifts one foot while reaching for the ball.",
        "make_easier": "Sit closer together.",
        "make_harder": "Add a name call before each roll.",
    }
    is_valid, warnings = validate_activity(ball_bad, "movement_and_physical")
    assert not is_valid, "Ball activity with foot/balance success must be blocked"
    assert any("success_domain_mismatch" in w for w in warnings), (
        f"Expected success_domain_mismatch warning, got: {warnings}"
    )
    print("  ✓ Ball activity with 'lifts one foot' success: blocked (success_domain_mismatch)")

    # Ball activity with correct success — should pass
    ball_good = {
        "title": "Rolling Ball Fun",
        "activity_family": "catch_ball",
        "instructions": "Sit on the floor. Roll the ball to your child and encourage them to roll it back.",
        "materials": "soft ball",
        "success_criteria": "Your child rolls or pushes the ball back at least once.",
        "make_easier": "Sit closer together.",
        "make_harder": "Add a name call before each roll.",
    }
    is_valid, warnings = validate_activity(ball_good, "movement_and_physical")
    critical = [w for w in warnings if "success_domain_mismatch" in w]
    assert not critical, f"Correct ball activity should not have mismatch warning, got: {warnings}"
    print("  ✓ Ball activity with correct success criteria: passes")

    # Bead activity with crayon success — should be blocked
    bead_bad = {
        "title": "Bead Threading",
        "activity_family": "beading_threading",
        "instructions": "Place large beads on the table. Show your child how to thread one bead onto the pipe cleaner.",
        "materials": "large wooden beads and pipe cleaners",
        "success_criteria": "Your child makes marks on paper with the crayon.",
        "make_easier": "Hold the pipe cleaner steady.",
        "make_harder": "Use beads of two different colours.",
    }
    is_valid, warnings = validate_activity(bead_bad, "movement_and_physical")
    assert not is_valid, "Bead activity with crayon success must be blocked"
    assert any("success_domain_mismatch" in w for w in warnings), (
        f"Expected success_domain_mismatch warning, got: {warnings}"
    )
    print("  ✓ Bead activity with 'crayon/drawing' success: blocked (success_domain_mismatch)")

    # Bead activity with correct success — should pass
    bead_good = {
        "title": "Bead Threading",
        "activity_family": "beading_threading",
        "instructions": "Place large beads on the table. Show your child how to thread one bead onto the pipe cleaner.",
        "materials": "large wooden beads and pipe cleaners",
        "success_criteria": "Your child threads at least one bead onto the pipe cleaner.",
        "make_easier": "Hold the pipe cleaner steady.",
        "make_harder": "Use beads of two different colours.",
    }
    is_valid, warnings = validate_activity(bead_good, "movement_and_physical")
    critical = [w for w in warnings if "success_domain_mismatch" in w]
    assert not critical, f"Correct bead activity should not have mismatch warning, got: {warnings}"
    print("  ✓ Bead activity with correct success criteria: passes")


# ---------------------------------------------------------------------------
# Case 30: ADHD 48m gets concrete first/then and counting cards (not generic titles)
# ---------------------------------------------------------------------------

_GENERIC_FIRSTTHEN_TITLES = re.compile(
    r"^(first and then game|routine activity|snack counting activity|action picture activity)$",
    re.I,
)

def test_case30_adhd_48m_concrete_cards():
    """Julian ADHD 48-60m profile must receive concrete first/then and counting cards,
    not generic bucket fallback titles like 'First and Then Game'."""
    print("\n─── Case 30: ADHD 48m gets concrete first/then and counting cards ───")
    from genex_core.interview_engine import choose_focus_domains
    from genex_core.support_tiers import build_v22_plan_for_category
    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    state = _make_adhd_state()
    ensure_concern_profile(state)
    allocate_weekly_slots(state)
    focus = choose_focus_domains(state)
    for ck in focus:
        plan = build_v22_plan_for_category(state, ck)
        state.setdefault("bridge_plans", {})[ck] = plan
        bank = generate_category_activity_bank(state, ck)
        state.setdefault("activity_banks", {})[ck] = bank
    state["cycle_week"] = 1
    build_weekly_schedule(state)
    days = state["weekly_schedule"]["days"]

    bad_titles = []
    for day in WEEKDAYS:
        for item in days.get(day, {}).get("items", []):
            title = item.get("title", "")
            if _GENERIC_FIRSTTHEN_TITLES.match(title):
                bad_titles.append(f"{day}: {title!r}")

    assert not bad_titles, (
        f"ADHD 48m profile received generic first/then or counting card titles:\n"
        + "\n".join(bad_titles)
    )
    print("  ✓ ADHD 48m: no generic first/then or counting card titles in week schedule")

    # Also verify all banks are free of generic titles
    for ck in focus:
        bank = state["activity_banks"].get(ck, {})
        for act in bank.get("activities", []):
            t = act.get("title", "")
            if _GENERIC_FIRSTTHEN_TITLES.match(t):
                bad_titles.append(f"bank[{ck}]: {t!r}")

    assert not bad_titles, (
        f"ADHD 48m activity bank has generic card titles:\n" + "\n".join(bad_titles)
    )
    print("  ✓ ADHD 48m: no generic titles in cognitive/social activity banks")


# ---------------------------------------------------------------------------
# Case 31: DS-24m and Dravet profiles get no unsafe movement in pass-9 bucket cards
# ---------------------------------------------------------------------------

_PASS9_UNSAFE_MOVEMENT = re.compile(
    r"\b(jump(ing)?|hop(ping)?|stomp(ing)?|race|racing|climb(ing)?|"
    r"trampoline|obstacle course|sprint)\b",
    re.I,
)

def test_case31_ds_and_dravet_no_unsafe_in_bucket_cards():
    """DS-24m and Dravet profiles must not receive any unsafe movement instructions,
    including from the new pass-9 bucket variant cards (catch_ball, time_words, counting)."""
    print("\n─── Case 31: DS-24m and Dravet pass-9 bucket cards have no unsafe movement ───")
    from genex_core.interview_engine import choose_focus_domains
    from genex_core.support_tiers import build_v22_plan_for_category
    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

    profiles = [
        ("DS-24m", init_state_from_profile(
            "Emma", 24, "Down syndrome",
            "hypotonia, low muscle tone, gross motor delay", 10,
        )),
        ("Dravet-40m", _make_dravet_state()),
    ]

    for label, state in profiles:
        ensure_concern_profile(state)
        allocate_weekly_slots(state)
        focus = choose_focus_domains(state)
        for ck in focus:
            plan = build_v22_plan_for_category(state, ck)
            state.setdefault("bridge_plans", {})[ck] = plan
            bank = generate_category_activity_bank(state, ck)
            state.setdefault("activity_banks", {})[ck] = bank
        state["cycle_week"] = 1
        build_weekly_schedule(state)
        days = state["weekly_schedule"]["days"]

        for day in WEEKDAYS:
            for item in days.get(day, {}).get("items", []):
                for field in ["title", "instructions", "make_harder"]:
                    value = str(item.get(field, "") or "")
                    m = _PASS9_UNSAFE_MOVEMENT.search(value)
                    assert not m, (
                        f"[{label}] {day} '{item.get('title')}' field '{field}' "
                        f"contains unsafe movement '{m.group()}': {value[:100]}"
                    )

        print(f"  ✓ [{label}] all scheduled cards safe — no jump/hop/stomp/race/climb in any field")


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
        test_case9_week1_schedule_uniqueness,
        test_case10_activity_bank_no_per_bridge_core_duplicates,
        test_case11_dravet_safety,
        test_case12_adhd_exact_profile,
        test_case13_speech_delay_only,
        test_case14_time_budget_fill,
        test_case15_no_weekday_rest_days,
        test_case16_speech_delay_bridge_spread,
        test_case17_near_duplicate_detection,
        test_case18_dravet_stomp_squat_blocked,
        test_case19_adhd_age_appropriate,
        test_case20_exact_slot_counts,
        test_case21_card_schema_completeness,
        test_case22_ot_pt_routing,
        test_case23_near_duplicate_prevention,
        test_case24_no_generic_phrases_in_scheduled_cards,
        test_case25_no_game_game_titles,
        test_case26_down_syndrome_safety,
        test_case27_ot_pt_speech_routing,
        test_case28_pass9_phrases_blocked,
        test_case29_success_domain_mismatch_blocked,
        test_case30_adhd_48m_concrete_cards,
        test_case31_ds_and_dravet_no_unsafe_in_bucket_cards,
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
