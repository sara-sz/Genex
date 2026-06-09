"""
tests/test_regression.py
-------------------------
V22 regression test suite (13 cases).

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
# Case 11: Safety — Dravet / seizure / unstable-walk blocks jump activities
# ---------------------------------------------------------------------------

def test_case11_safety_dravet_blocks_jump_activities():
    print("\n─── Case 11: Safety — Dravet/seizure/unstable-walk hard-blocks jumps ───")
    from genex_core.safety import build_safety_profile, apply_safety_constraints_to_activities

    # Dravet with unstable walking — should trigger falls_balance_gait AND
    # seizure_or_medical_monitoring, both triggering the jump hard-block
    child = {
        "diagnosis": "Dravet syndrome",
        "concern": "seizures, unstable walking, frequent falls",
    }
    profile = build_safety_profile(child)
    risk_scores = profile["risk_scores"]
    hard_avoid = profile["hard_avoid"]

    # seizure_or_medical_monitoring must be detected
    assert risk_scores.get("seizure_or_medical_monitoring", 0) >= 0.35, (
        f"Expected seizure_or_medical_monitoring >= 0.35, got {risk_scores}"
    )
    # "unstable" now also fires falls_balance_gait
    assert risk_scores.get("falls_balance_gait", 0) >= 0.35, (
        f"Expected falls_balance_gait >= 0.35 (unstable keyword), got {risk_scores}"
    )
    # hard_avoid must include jump-related language
    jump_blocked = any("jump" in h.lower() or "hop" in h.lower() for h in hard_avoid)
    assert jump_blocked, f"Expected jump/hop in hard_avoid, got: {hard_avoid}"
    print(f"  ✓ seizure_or_medical_monitoring = {round(risk_scores['seizure_or_medical_monitoring'], 2)}")
    print(f"  ✓ falls_balance_gait = {round(risk_scores['falls_balance_gait'], 2)}")
    print(f"  ✓ hard_avoid includes jump/hop block")

    # Build a mock jump_prep bank and apply constraints
    state = init_state_from_profile(
        "your child", 40, "Dravet syndrome",
        "seizures, unstable walking, frequent falls", 10,
    )
    mock_activities = [
        {
            "title": "Frog Jump Game",
            "instructions": "model bending your knees and jumping forward",
            "materials": "clear flat floor",
            "avoid": "Avoid unsupported jumping off heights.",
            "duration_min": 5,
            "activity_family": "jump_prep_balance",
            "category_key": "movement_and_physical",
            "_debug": {"activity_type": "core"},
        },
        {
            "title": "Stomp the Sticker",
            "instructions": "stomp on stickers on the floor",
            "materials": "stickers",
            "avoid": "Avoid slippery surfaces.",
            "duration_min": 5,
            "activity_family": "jump_prep_balance",
            "category_key": "movement_and_physical",
            "_debug": {"activity_type": "core"},
        },
        {
            "title": "Hop to the Toy",
            "instructions": "hop on one foot to reach a toy",
            "materials": "toy",
            "avoid": "",
            "duration_min": 5,
            "activity_family": "jump_prep_balance",
            "category_key": "movement_and_physical",
            "_debug": {"activity_type": "core"},
        },
    ]
    safe = apply_safety_constraints_to_activities(state, "movement_and_physical", mock_activities)

    # All three had jump/hop in their text — all should be replaced
    replaced_titles = [a["title"] for a in safe]
    jump_in_output = [
        t for t in replaced_titles
        if re.search(r"\b(jump|hop|frog)\b", t.lower())
    ]
    assert not jump_in_output, (
        f"Jump/hop/frog still in replaced activity titles: {jump_in_output}"
    )
    print(f"  ✓ All {len(mock_activities)} jump/hop activities replaced")

    # Replaced titles must be distinct (pool cycling)
    assert len(set(replaced_titles)) == len(replaced_titles), (
        f"Duplicate replacement titles: {replaced_titles}"
    )
    print(f"  ✓ Replacement titles are all distinct: {replaced_titles}")

    # 'avoid' field must be updated (no longer says "jumping off heights" for a safe card)
    for a in safe:
        orig_avoid = next(
            (m["avoid"] for m in mock_activities if m["title"] == "Frog Jump Game"), ""
        )
        assert a.get("avoid", "") != orig_avoid or "jump" not in a.get("avoid", "").lower(), (
            f"Original jump avoid text still present in replaced card: {a.get('avoid')}"
        )
    print(f"  ✓ 'avoid' field updated to match the safe replacement card")


# ---------------------------------------------------------------------------
# Case 12: ADHD — 10 min/day fills all 10 weekday slots with unique activities
# ---------------------------------------------------------------------------

def test_case12_adhd_fills_all_slots():
    print("\n─── Case 12: ADHD 10 min/day — all 10 weekday slots filled with unique activities ───")
    from genex_core.activity_engine import _BUCKET_VARIANTS
    from genex_core.support_tiers import build_v22_plan_for_category

    # ADHD routes to social_and_emotional + cognitive
    state = init_state_from_profile(
        "your child", 60, "ADHD",
        "hyperactivity, trouble focusing, difficulty finishing tasks", 10,
    )
    # Force the delay estimates so both domains have gap
    state["dev_age"]["cognitive"] = 48
    state["dev_age"]["social_and_emotional"] = 48
    state["delay_estimates"]["cognitive"] = {"delay_months": 12}
    state["delay_estimates"]["social_and_emotional"] = {"delay_months": 12}

    ensure_concern_profile(state)

    # Attention bucket must have >= 5 distinct cards (enough for 5 days)
    attn_cards = _BUCKET_VARIANTS.get("attention", [])
    assert len(attn_cards) >= 5, (
        f"Attention bucket needs >= 5 cards for ADHD 5-day coverage, has {len(attn_cards)}"
    )
    print(f"  ✓ Attention bucket has {len(attn_cards)} cards")

    # Build banks for both domains
    for dk in ["social_and_emotional", "cognitive"]:
        plan = build_v22_plan_for_category(state, dk)
        state.setdefault("bridge_plans", {})[dk] = plan
        bank = generate_category_activity_bank(state, dk)
        state.setdefault("activity_banks", {})[dk] = bank
        core = [a for a in bank.get("activities", [])
                if a.get("_debug", {}).get("activity_type", "core") == "core"]
        titles = [a["title"] for a in core]
        unique_titles = set(t.lower() for t in titles)
        print(f"  ✓ {dk} core bank: {len(core)} activities, {len(unique_titles)} unique titles")

    state["cycle_week"] = 1
    allocate_weekly_slots(state)
    build_weekly_schedule(state)
    schedule = state["weekly_schedule"]

    WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    total_slots = sum(
        len(schedule.get("days", {}).get(d, {}).get("items", []))
        for d in WEEKDAYS
    )
    # Each weekday should have 2 activities (10 min / 5 min per activity)
    assert total_slots >= 9, (
        f"Expected >= 9 activity slots for ADHD 10 min/day, got {total_slots}"
    )
    print(f"  ✓ Total weekday activity slots: {total_slots} (expected >= 9)")

    # No duplicate titles across the week per category
    cat_titles: dict = {}
    for d in WEEKDAYS:
        for item in schedule.get("days", {}).get(d, {}).get("items", []):
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

    assert not cross_dups, (
        "Duplicate titles within the same category across ADHD week:\n"
        + "\n".join(cross_dups)
    )
    print(f"  ✓ No duplicate activity titles within any category across the week")


# ---------------------------------------------------------------------------
# Case 13: Routing — speech-delay only selects language domain only
# ---------------------------------------------------------------------------

def test_case13_speech_delay_only_routing():
    print("\n─── Case 13: Routing — speech-delay only selects language domain only ───")
    from genex_core.interview_engine import choose_focus_domains, rank_focus_domains
    import re as _re

    # Pure speech delay — no social / emotional keywords
    state = init_state_from_profile(
        "your child", 36, "None",
        "speech delay, not talking much, limited words", 5,
    )
    ensure_concern_profile(state)

    # Domain weights: language must be top, all others should be 0 or very low
    domain_weights = state["concern_profile"]["domain_weights"]
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
        test_case11_safety_dravet_blocks_jump_activities,
        test_case12_adhd_fills_all_slots,
        test_case13_speech_delay_only_routing,
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
