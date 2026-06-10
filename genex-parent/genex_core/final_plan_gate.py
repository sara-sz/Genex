"""
genex_core/final_plan_gate.py
------------------------------
End-of-pipeline deterministic validator + sanitizer.

Runs after:
  routing → questions → activity-bank generation → safety filtering →
  safety replacement → scheduling → deduping → field normalisation

Public API:
    repaired_plan, gate_report = validate_and_repair_final_plan(
        profile, selected_domains, question_domains,
        weekly_plan, candidate_bank, safety_profile=None,
    )

gate_report is for admin/debug only — never shown to parents.
"""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]

# ---------------------------------------------------------------------------
# Safe filler card pool
# Used when the scheduled bank is exhausted of safe/unique alternatives.
# Fields use scheduler-normalised names: success / avoid / group_play.
# All cards are safe for any safety profile (no movement, no jumping).
# ---------------------------------------------------------------------------

SAFE_FILLER_CARDS: List[Dict[str, Any]] = [
    {
        "title": "Pick Your Toy",
        "category_key": "language_and_communication",
        "activity_family": "expressive_first_words",
        "duration_min": 5,
        "materials": "2–3 favourite small toys",
        "instructions": (
            "Place 2–3 small toys in front of your child. "
            "Look at them and say 'which one do you want?' Wait 5 seconds. "
            "If they reach, point, or make any sound toward one, "
            "hand it over and name it: 'the ball!' Repeat with a fresh set."
        ),
        "success": "Your child reaches, points, looks, or vocalises toward a toy.",
        "make_easier": "Hold up just one toy and wait for any response before offering it.",
        "make_harder": "Ask 'do you want the [toy] or the [toy]?' and wait for a clear choice.",
        "avoid": "Avoid rushing — give a full 5 seconds before helping.",
        "group_play": "",
        "why": "Making choices with words or gestures builds expressive communication.",
    },
    {
        "title": "Help Me Open It",
        "category_key": "language_and_communication",
        "activity_family": "gesture_communication",
        "duration_min": 5,
        "materials": "a small jar, box, or bag that is slightly tight to open",
        "instructions": (
            "Put a favourite snack or toy inside a container. "
            "Hand it to your child and wait. "
            "If they try to open it, say 'open!' and help them. "
            "If they look at you, say 'you need help — say open.' "
            "Repeat with a different container."
        ),
        "success": "Your child requests help with a word, sound, gesture, or look.",
        "make_easier": "Only close the lid lightly so minimal effort is needed.",
        "make_harder": "Wait until your child uses a word or approximation before helping.",
        "avoid": "Avoid opening it immediately — the wait builds the request.",
        "group_play": "",
        "why": "Requesting help is an early communication skill that grows expressive language.",
    },
    {
        "title": "Show Me What You Want",
        "category_key": "language_and_communication",
        "activity_family": "gesture_communication",
        "duration_min": 5,
        "materials": "2 snack options or 2 small toys on a tray",
        "instructions": (
            "Put two items on a tray in front of your child. "
            "Say 'show me what you want.' "
            "Wait for a point, reach, or look. "
            "Name what they chose: 'cracker! you want the cracker.' "
            "Give it to them right away."
        ),
        "success": "Your child uses a gesture or word to indicate a choice.",
        "make_easier": "Offer one item at a time and wait for any response before giving it.",
        "make_harder": "Pause and ask 'what do you want?' before putting the options out.",
        "avoid": "Avoid guessing for your child — wait for them to signal first.",
        "group_play": "",
        "why": "Pointing and showing are foundational for early language development.",
    },
    {
        "title": "Family Photo Names",
        "category_key": "language_and_communication",
        "activity_family": "object_naming",
        "duration_min": 5,
        "materials": "3–4 printed or phone photos of familiar family members",
        "instructions": (
            "Sit together and look at 3–4 photos of familiar people. "
            "Point to each one and say their name: 'that's grandma!' "
            "Then point again and wait: 'who's that?' "
            "Celebrate any attempt — even a look at the right photo counts."
        ),
        "success": "Your child looks at, points to, or names at least one family member.",
        "make_easier": "Say the name first, then point again — no question yet.",
        "make_harder": "Mix in one unfamiliar photo and see if your child notices.",
        "avoid": "Avoid rapid-fire testing — one photo at a time, warm tone.",
        "group_play": "",
        "why": "Naming familiar people builds vocabulary and strengthens memory.",
    },
    {
        "title": "Snack Choice Words",
        "category_key": "language_and_communication",
        "activity_family": "expressive_vocabulary_growth",
        "duration_min": 5,
        "materials": "2 small snack options (e.g. crackers and raisins)",
        "instructions": (
            "Hold up two snacks, one in each hand. "
            "Say 'cracker or raisins?' and wait. "
            "Accept any word attempt, gesture, or point. "
            "Name what they chose as you give it: 'raisins!' "
            "Try 2–3 rounds."
        ),
        "success": "Your child communicates a choice using a word, sound, gesture, or look.",
        "make_easier": "Present only one snack and wait for any response before offering a piece.",
        "make_harder": "After handing it over, ask 'more cracker?' before the next piece.",
        "avoid": "Avoid giving the snack before your child responds — the wait is the opportunity.",
        "group_play": "",
        "why": "Snack-time choices create natural low-pressure language moments.",
    },
    {
        "title": "Routine Pause Point",
        "category_key": "language_and_communication",
        "activity_family": "receptive_directions_one_step",
        "duration_min": 5,
        "materials": "no materials — use any familiar daily routine",
        "instructions": (
            "During a routine your child knows well — washing hands, getting shoes, snack time — "
            "pause at a key step and wait. "
            "For example: pick up one shoe but hold it without putting it on. "
            "Say 'what comes next?' and wait 5 seconds. "
            "Give a hint if needed: 'on the foot?' Celebrate any response."
        ),
        "success": "Your child fills in the next step with a word, action, or gesture.",
        "make_easier": "Give a one-word hint and let your child complete the action.",
        "make_harder": "Pause earlier in the routine so more steps are left to fill in.",
        "avoid": "Avoid pausing at a step your child finds stressful.",
        "group_play": "",
        "why": "Routine pauses give your child a chance to predict what comes next.",
    },
    {
        "title": "Big Bead Threading",
        "category_key": "movement_and_physical",
        "activity_family": "beading_threading",
        "duration_min": 5,
        "materials": "5–6 large wooden beads and a pipe cleaner or thick shoelace",
        "instructions": (
            "Place 5–6 large beads on a tray. "
            "Show your child how to push one bead onto the pipe cleaner. "
            "Hand them one bead at a time and say 'push it on.' "
            "Keep turns short — 2–3 beads is a full round. "
            "Celebrate each attempt."
        ),
        "success": "Your child pushes at least one bead onto the pipe cleaner.",
        "make_easier": "Hold the pipe cleaner steady and guide their hand to start.",
        "make_harder": "Use a slightly thinner lace and beads with a smaller hole.",
        "avoid": "Avoid small beads — choking hazard and too hard to thread.",
        "group_play": "Two children take turns threading one bead each.",
        "why": "Threading builds the pincer grasp and hand-eye coordination used for drawing and dressing.",
    },
    {
        "title": "Sticker Peel and Place",
        "category_key": "movement_and_physical",
        "activity_family": "pincer_grasp",
        "duration_min": 5,
        "materials": "sheet of large round stickers, plain paper",
        "instructions": (
            "Peel back one corner of a sticker and hand it to your child. "
            "Say 'peel it off and put it here' pointing to a spot on the paper. "
            "Do 4–5 stickers. "
            "It is fine if they use two hands — the pinching motion is what matters."
        ),
        "success": "Your child peels at least one sticker and places it on the paper.",
        "make_easier": "Pre-peel the sticker and just ask your child to place it.",
        "make_harder": "Draw target shapes on the paper and ask them to place stickers on the shapes.",
        "avoid": "Avoid very small stickers — frustrating and hard to grip.",
        "group_play": "Two children each decorate their own paper side by side.",
        "why": "Peeling and placing builds the fine motor control needed for writing and self-care.",
    },
    {
        "title": "Spoon Scoop Practice",
        "category_key": "movement_and_physical",
        "activity_family": "fork_spoon_use",
        "duration_min": 5,
        "materials": "a large spoon, a small bowl, and soft food pieces (e.g. cereal, peas)",
        "instructions": (
            "Put a small amount of soft food in a bowl. "
            "Show your child how to scoop with the spoon and bring it to their mouth. "
            "Hand them the spoon and let them try. "
            "Guide their wrist gently if needed. "
            "3–4 scoops is a full turn."
        ),
        "success": "Your child scoops at least once and brings the spoon toward their mouth.",
        "make_easier": "Pre-load the spoon and just ask your child to bring it to their mouth.",
        "make_harder": "Use a slightly smaller spoon and smaller food pieces.",
        "avoid": "Avoid correcting grip mid-scoop — let them finish the motion first.",
        "group_play": "",
        "why": "Spoon use builds wrist rotation and hand coordination for self-feeding.",
    },
    {
        "title": "Paint Choice Words",
        "category_key": "language_and_communication",
        "activity_family": "expressive_vocabulary_growth",
        "duration_min": 5,
        "materials": "2 colours of washable paint or 2 crayons, plain paper",
        "instructions": (
            "Put out two colours. "
            "Say 'which colour do you want?' and wait. "
            "Accept any response — word, point, or reach. "
            "Name the colour as you hand it over: 'red! here is the red.' "
            "After each mark, ask 'more red or blue?' Keep it to 3–4 turns."
        ),
        "success": "Your child communicates a colour choice at least twice.",
        "make_easier": "Offer one colour at a time and celebrate any mark they make.",
        "make_harder": "Ask 'what colour is it?' before handing it over.",
        "avoid": "Avoid correcting marks — the choice and language are the goals.",
        "group_play": "Each child has their own paper and takes turns picking a colour to share.",
        "why": "Art choices build vocabulary, turn-taking, and expressive language.",
    },
    {
        "title": "Cleanup Request Game",
        "category_key": "language_and_communication",
        "activity_family": "receptive_directions_one_step",
        "duration_min": 5,
        "materials": "toys or objects on the floor, a bin or basket",
        "instructions": (
            "Scatter 4–5 small objects on the floor. "
            "Say 'can you put the [item] in the bin?' "
            "Wait for them to pick it up and drop it in. "
            "Try 3–4 different objects with a warm celebratory tone."
        ),
        "success": "Your child follows at least 2 one-step cleanup requests.",
        "make_easier": "Point to the object and the bin as you give the instruction.",
        "make_harder": "Give a two-step instruction: 'pick up the block AND the cup.'",
        "avoid": "Avoid making this feel like a chore — celebrate each drop-in warmly.",
        "group_play": "Two children take turns: one gives the instruction, one does the cleanup.",
        "why": "Following simple instructions builds receptive language and cooperation.",
    },
]

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_UNSAFE_RE = re.compile(
    r"\b(jump(ing)?|hop(ping)?|stomp(ing)?|race|racing|climb(ing)?|"
    r"trampoline|obstacle.course|balance.beam|one.leg.balance|"
    r"unsupported.balance|slippery.surface|hard.ball|fast.run(ning)?|sprint)\b",
    re.I,
)

_GENERIC_PATTERNS = [
    re.compile(r"set up a quick\b", re.I),
    re.compile(r"show your child one small step", re.I),
    re.compile(r"\bgoal:\s", re.I),
    re.compile(r"items for .{1,40} from around the home", re.I),
    re.compile(r"your child tries at least once:", re.I),
    re.compile(r"break it into one single step", re.I),
    re.compile(r"add one more step or reduce your help", re.I),
    re.compile(r"with a sibling or friend, take turns.{0,30}each person tries one step", re.I),
    re.compile(r"celebrate any attempt and stop after 2", re.I),
    re.compile(r"choose a short activity using", re.I),
    re.compile(r"\bany calm attempt at\b", re.I),
    re.compile(r"use one item, model first", re.I),
    re.compile(r"only if easy and enjoyable", re.I),
    re.compile(r"^\s*simple household items\s*$", re.I),
    re.compile(r"\broutine activity\b", re.I),
    re.compile(r"snack counting activity", re.I),
    re.compile(r"action picture activity", re.I),
]

_CARD_FIELDS = [
    "title", "instructions", "materials", "success",
    "make_easier", "make_harder", "avoid", "group_play", "why",
]

# ---------------------------------------------------------------------------
# Item-level checks
# ---------------------------------------------------------------------------

def _unsafe_phrase(item: Dict[str, Any]) -> Optional[str]:
    """Return the first unsafe movement phrase found, or None."""
    for field in ["title", "instructions", "make_harder"]:
        m = _UNSAFE_RE.search(str(item.get(field, "") or ""))
        if m:
            return m.group()
    return None


def _generic_hit(item: Dict[str, Any]) -> Optional[str]:
    """Return the first generic template phrase found across all parent-facing fields, or None."""
    combined = " ".join(str(item.get(f, "") or "") for f in _CARD_FIELDS)
    for pat in _GENERIC_PATTERNS:
        if pat.search(combined):
            return pat.pattern[:50]
    return None


def _success_mismatch(item: Dict[str, Any]) -> Optional[str]:
    """Return a description of success-criteria domain mismatch, or None."""
    text = " ".join(str(item.get(f, "") or "") for f in ["title", "instructions", "materials"])
    success = str(item.get("success", "") or "").lower()
    if re.search(r"\b(ball|rolling|toss|throw|catch)\b", text, re.I):
        if re.search(r"\b(lifts? one foot|stand on one|balances? on)\b", success):
            return "ball_activity_with_foot_balance_success"
    if re.search(r"\b(bead|thread|peg|lace)\b", text, re.I):
        if re.search(r"\b(crayon|drawing|marks? on paper)\b", success):
            return "bead_activity_with_crayon_success"
    return None


# ---------------------------------------------------------------------------
# Validation pass — returns list of violation dicts
# ---------------------------------------------------------------------------

def _validate(
    days: Dict[str, Any],
    selected_domains: List[str],
    high_fall: bool,
) -> List[Dict[str, Any]]:
    violations: List[Dict[str, Any]] = []

    # Rule 1: duplicate titles
    seen: Dict[str, str] = {}  # title_lower → first day
    for day in WEEKDAYS:
        for idx, item in enumerate(days.get(day, {}).get("items", [])):
            key = item.get("title", "").strip().lower()
            if not key:
                continue
            if key in seen:
                violations.append({
                    "rule": "duplicate_title",
                    "day": day, "idx": idx,
                    "title": item.get("title", ""),
                    "detail": f"also on {seen[key]}",
                })
            else:
                seen[key] = day

    # Rule 2: unsafe movement (only for high-fall profiles)
    if high_fall:
        for day in WEEKDAYS:
            for idx, item in enumerate(days.get(day, {}).get("items", [])):
                phrase = _unsafe_phrase(item)
                if phrase:
                    violations.append({
                        "rule": "unsafe_movement",
                        "day": day, "idx": idx,
                        "title": item.get("title", ""),
                        "detail": phrase,
                    })

    # Rule 3: generic template language
    for day in WEEKDAYS:
        for idx, item in enumerate(days.get(day, {}).get("items", [])):
            hit = _generic_hit(item)
            if hit:
                violations.append({
                    "rule": "generic_language",
                    "day": day, "idx": idx,
                    "title": item.get("title", ""),
                    "detail": hit,
                })

    # Rule 4: success criteria mismatch
    for day in WEEKDAYS:
        for idx, item in enumerate(days.get(day, {}).get("items", [])):
            mm = _success_mismatch(item)
            if mm:
                violations.append({
                    "rule": "success_mismatch",
                    "day": day, "idx": idx,
                    "title": item.get("title", ""),
                    "detail": mm,
                })

    # Rule 5: domain coverage (10+ slots → each selected domain ≥ 2)
    total_slots = sum(len(days.get(d, {}).get("items", [])) for d in WEEKDAYS)
    if total_slots >= 10 and len(selected_domains) >= 2:
        counts: Dict[str, int] = {}
        for day in WEEKDAYS:
            for item in days.get(day, {}).get("items", []):
                ck = item.get("category_key", "")
                counts[ck] = counts.get(ck, 0) + 1
        for ck in selected_domains:
            if counts.get(ck, 0) < 2:
                violations.append({
                    "rule": "domain_coverage",
                    "day": None, "idx": None,
                    "title": None,
                    "detail": f"{ck} has {counts.get(ck, 0)} slots (need >= 2)",
                    "domain": ck,
                })

    return violations


# ---------------------------------------------------------------------------
# Replacement helpers
# ---------------------------------------------------------------------------

def _current_week_titles(days: Dict[str, Any]) -> set:
    return {
        item.get("title", "").strip().lower()
        for day in WEEKDAYS
        for item in days.get(day, {}).get("items", [])
    }


def _apply_replacement(
    items: List[Dict[str, Any]],
    idx: int,
    card: Dict[str, Any],
    fallback_category_key: str,
    fallback_duration: int,
) -> None:
    """Overwrite items[idx] with card, keeping scheduling metadata."""
    old = items[idx]
    new = dict(old)
    new["title"]         = card.get("title", "")
    new["instructions"]  = card.get("instructions", "")
    new["materials"]     = card.get("materials", "")
    new["success"]       = card.get("success_criteria") or card.get("success", "")
    new["make_easier"]   = card.get("make_easier", "")
    new["make_harder"]   = card.get("make_harder", "")
    new["avoid"]         = card.get("what_to_avoid") or card.get("avoid", "")
    new["group_play"]    = card.get("group_play_line") or card.get("group_play", "")
    new["why"]           = card.get("why", old.get("why", ""))
    new["activity_family"] = card.get("activity_family", old.get("activity_family", ""))
    new["category_key"]  = card.get("category_key", fallback_category_key)
    new["duration_min"]  = card.get("duration_min", fallback_duration)
    new["_gate_repaired"] = True
    items[idx] = new


def _pick_replacement(
    used_titles: set,
    category_key: Optional[str],
    candidate_bank: Dict[str, Any],
    high_fall: bool,
) -> Optional[Dict[str, Any]]:
    """Find an unused, safe replacement card.

    Priority:
    1. Unused core card from candidate_bank[category_key]
    2. SAFE_FILLER_CARDS matching category_key
    3. Any SAFE_FILLER_CARD not yet in the week
    """
    # 1. Bank for same category
    if category_key:
        for act in candidate_bank.get(category_key, {}).get("activities", []):
            title_low = act.get("title", "").strip().lower()
            if title_low in used_titles:
                continue
            if high_fall and _unsafe_phrase(act):
                continue
            if _generic_hit(act):
                continue
            return act

    # 2. Filler cards — same category first
    for card in SAFE_FILLER_CARDS:
        if category_key and card["category_key"] != category_key:
            continue
        if card["title"].strip().lower() not in used_titles:
            return card

    # 3. Any filler card
    for card in SAFE_FILLER_CARDS:
        if card["title"].strip().lower() not in used_titles:
            return card

    return None


# ---------------------------------------------------------------------------
# Repair pass
# ---------------------------------------------------------------------------

_RULE_PRIORITY = {
    "duplicate_title": 0,
    "unsafe_movement": 1,
    "generic_language": 2,
    "success_mismatch": 3,
    "domain_coverage":  4,
}


def _repair(
    days: Dict[str, Any],
    violations: List[Dict[str, Any]],
    candidate_bank: Dict[str, Any],
    high_fall: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Repair violations in-place.  Returns (repairs_made, unrepaired)."""
    repairs:    List[Dict[str, Any]] = []
    unrepaired: List[Dict[str, Any]] = []

    sorted_viols = sorted(violations, key=lambda v: _RULE_PRIORITY.get(v["rule"], 99))

    for v in sorted_viols:
        rule = v["rule"]

        # --- Domain coverage: swap an over-represented slot with an under-rep card ---
        if rule == "domain_coverage":
            needed_ck = v.get("domain") or v["detail"].split(" ")[0]
            used = _current_week_titles(days)
            replacement = _pick_replacement(used, needed_ck, candidate_bank, high_fall)

            if replacement is None:
                unrepaired.append(v)
                continue

            # Find a slot from the most over-represented OTHER domain to swap
            domain_slots: Dict[str, List[Tuple[str, int]]] = {}
            for day in WEEKDAYS:
                for idx, item in enumerate(days.get(day, {}).get("items", [])):
                    ck = item.get("category_key", "")
                    if ck != needed_ck:
                        domain_slots.setdefault(ck, []).append((day, idx))

            if not domain_slots:
                unrepaired.append(v)
                continue

            over_ck = max(domain_slots, key=lambda k: len(domain_slots[k]))
            swap_day, swap_idx = domain_slots[over_ck][-1]
            old_title = days[swap_day]["items"][swap_idx].get("title", "")
            _apply_replacement(
                days[swap_day]["items"], swap_idx, replacement,
                needed_ck, days[swap_day]["items"][swap_idx].get("duration_min", 5),
            )
            repairs.append({
                "rule": rule, "day": swap_day,
                "old_title": old_title, "new_title": replacement.get("title"),
                "note": f"added {needed_ck} coverage",
            })
            continue

        # --- Slot-level repairs (duplicate, unsafe, generic, success_mismatch) ---
        day, idx = v.get("day"), v.get("idx")
        if day is None or idx is None:
            unrepaired.append(v)
            continue

        items = days.get(day, {}).get("items", [])
        if idx >= len(items):
            unrepaired.append(v)
            continue

        item = items[idx]
        # Guard: if this slot was already repaired to a different title, skip
        if item.get("title", "").strip().lower() != v["title"].strip().lower():
            continue

        # Do NOT subtract v["title"] — it may still exist on another day
        # (e.g. the original Monday copy of a duplicate), and we must not
        # accidentally "replace" Tuesday's copy with the same card.
        used = _current_week_titles(days)
        category_key = item.get("category_key")
        replacement = _pick_replacement(used, category_key, candidate_bank, high_fall)

        if replacement:
            old_title = item.get("title", "")
            _apply_replacement(items, idx, replacement, category_key, item.get("duration_min", 5))
            repairs.append({
                "rule": rule, "day": day,
                "old_title": old_title, "new_title": replacement.get("title"),
            })
        else:
            unrepaired.append(v)

    return repairs, unrepaired


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_and_repair_final_plan(
    profile: Dict[str, Any],
    selected_domains: List[str],
    question_domains: List[str],
    weekly_plan: Dict[str, Any],
    candidate_bank: Dict[str, Any],
    safety_profile: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Run the final plan gate.

    Returns (repaired_weekly_plan, gate_report).
    repaired_weekly_plan is safe to show to parents.
    gate_report is for admin/debug only.
    """
    from genex_core.safety import build_safety_profile as _build_sp

    if safety_profile is None:
        safety_profile = _build_sp(profile)

    risk = safety_profile.get("risk_scores", {})
    high_fall = (
        risk.get("falls_balance_gait", 0) >= 0.35
        or risk.get("mobility_equipment_support", 0) >= 0.35
        or risk.get("seizure_or_medical_monitoring", 0) >= 0.35
    )

    plan = deepcopy(weekly_plan)
    days = plan.get("days", {})

    gate_report: Dict[str, Any] = {
        "high_fall_profile": high_fall,
        "selected_domains": selected_domains,
        "question_domains": question_domains,
        "violations_pass1": [],
        "repairs": [],
        "unrepaired": [],
        "violations_pass2": [],
        "gate_passed": False,
    }

    # Pass 1: validate
    viols1 = _validate(days, selected_domains, high_fall)
    gate_report["violations_pass1"] = viols1

    if not viols1:
        gate_report["gate_passed"] = True
        return plan, gate_report

    # Repair
    repairs, unrepaired = _repair(days, viols1, candidate_bank, high_fall)
    gate_report["repairs"] = repairs
    gate_report["unrepaired"] = unrepaired

    # Pass 2: re-validate
    viols2 = _validate(days, selected_domains, high_fall)
    gate_report["violations_pass2"] = viols2
    gate_report["gate_passed"] = len(viols2) == 0

    return plan, gate_report
