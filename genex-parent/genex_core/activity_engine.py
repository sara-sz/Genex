"""
genex_core/activity_engine.py
------------------------------
V22 activity bank generation.

Rules:
- activity_family is a hard guardrail — LLM cannot switch domains.
- LLM is used only for parent-friendly wording (title, theme, instructions).
  All scoring, routing, and bridge selection are deterministic.
- Child first name is NEVER sent to the LLM ("your child" always).
- Uses ACTIVITY_MODEL env var; falls back to deterministic text if not set.
- initial plans: bridge_step_number = 1 only (enforced by bridge_selector).
- previous_bridge_step stored in activity debug fields but NOT used.
- Validators run before any activity is returned.
- Parent-facing card schema:
    title, duration_minutes, why, instructions, success,
    easier, harder, group_play, avoid, materials, feedback_options
- Debug-only fields (in _debug sub-dict):
    subdomain, milestone, bridge_step_1, activity_family,
    planning_mode, source_table_row, validation_warnings
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from genex_core.activity_validator import filter_valid_activities
from genex_core.bridge_selector import build_bridge_plan_for_category, select_next_milestones
from genex_core.config import (
    ACTIVITY_FEEDBACK_OPTIONS,
    ACTIVITY_MODEL,
    DOMAIN_CONFIG,
    ENGINE_VERSION,
    V22_MAX_MILESTONES_PER_DOMAIN,
    V22_MIN_MILESTONES_PER_DOMAIN,
    V22_PER_ACTIVITY_MIN,
    V22_WEEK1_DAYS,
    V22_MAX_DAILY_ACTIVITIES,
)
from genex_core.interview_engine import ensure_concern_profile
from genex_core.safety import ensure_safety_profile, format_safety_constraints_for_prompt
from genex_core.table_loader import get_family_description

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenAI client (lazy)
# ---------------------------------------------------------------------------

_openai_client = None
_openai_initialized = False


def _get_openai_client():
    global _openai_client, _openai_initialized
    if _openai_initialized:
        return _openai_client
    _openai_initialized = True
    if not ACTIVITY_MODEL:
        logger.warning(
            "[activity_engine] ACTIVITY_MODEL env var not set. "
            "Using deterministic fallback activity wording."
        )
        return None
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.warning("[activity_engine] OPENAI_API_KEY not set.")
        return None
    try:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=api_key)
    except ImportError:
        logger.warning("[activity_engine] openai package not installed.")
        _openai_client = None
    return _openai_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _clean(value: Any) -> str:
    s = str(value or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _family_bucket(fam: str, category_key: str = "") -> str:
    fam = _norm(fam)
    patterns = [
        ("book_page", r"book_page|page_turn"),
        ("fork_spoon", r"fork|spoon|utensil|feeding"),
        ("dressing_on", r"dressing_on|clothes_on"),
        ("dressing_off", r"dressing_off|clothes_off"),
        ("buttoning", r"button|fastener|zipper"),
        ("beading", r"bead|thread|peg|pincer|grasp|prewriting|scribble|crayon|draw|stack|block|fine_motor"),
        ("catch_ball", r"catch_ball|ball"),
        ("jump_prep", r"jump|hop|squat|balance|walk|stair|gross_motor|safe_"),
        ("expressive_word", r"expressive|vocabulary|first_word|single_word|three_words|book_object_naming|object_naming"),
        ("sound", r"sound|vocal|babbl|raspberr|squeal|coo|early_vocal"),
        ("gesture", r"gesture|request|attention_getting|arms_up|social_caregiver"),
        ("receptive_direction", r"receptive|direction|body_part|book_picture"),
        ("action_label", r"action_picture|action_label|action_words"),
        ("function_question", r"function_question"),
        ("sentence", r"sentence|phrase|two_word"),
        ("conversation", r"conversation"),
        ("time_words", r"time_words"),
        ("counting", r"count|number"),
        ("letters", r"letter"),
        ("attention", r"attention"),
        ("matching", r"match|sort|color|shape"),
        ("routine", r"routine|cleanup"),
        ("social_turn", r"peer|turn_taking|sharing|imitation|peekaboo|laughter|social|referencing|emotion|pretend|helper|affection|face"),
    ]
    for bucket, pat in patterns:
        if re.search(pat, fam):
            return bucket
    if category_key == "language_and_communication":
        return "expressive_word"
    if category_key == "social_and_emotional":
        return "social_turn"
    if category_key == "movement_and_physical":
        return "beading"
    if category_key == "cognitive":
        return "attention"
    return "general"


# ---------------------------------------------------------------------------
# Theme rotation for weeks 3-4
# ---------------------------------------------------------------------------

_FAMILY_THEMES: Dict[str, List[str]] = {
    "book_page": ["board book", "picture book", "peek-a-boo book", "interactive book"],
    "fork_spoon": ["snack time", "mealtime", "pretend restaurant", "teddy feeding"],
    "dressing_on": ["morning routine", "dress-up game", "laundry helper", "mirror routine"],
    "dressing_off": ["bath routine", "bedtime routine", "teddy dressing", "laundry pull"],
    "buttoning": ["button board", "dress-up fasteners", "button treasure"],
    "beading": ["bead game", "peg stacker", "art time", "block build"],
    "catch_ball": ["soft ball game", "basket target", "rolling game"],
    "jump_prep": ["frog game", "floor sticker", "squat toy game"],
    "expressive_word": ["toy choice", "snack choice", "family photo names", "book naming"],
    "sound": ["sound mirror", "animal sounds", "song pause", "silly sounds"],
    "gesture": ["choice request", "help request", "routine pause", "pointing game"],
    "receptive_direction": ["give-me game", "cleanup direction", "body part game"],
    "action_label": ["action picture", "family action", "puppet action"],
    "function_question": ["object function", "function basket", "pretend shopping"],
    "sentence": ["photo sentence", "toy scene talk", "phrase expansion"],
    "conversation": ["short chat", "puppet chat", "photo conversation"],
    "time_words": ["routine sort", "now/later choice", "first/then routine"],
    "counting": ["counting blocks", "snack counting", "toy lineup"],
    "letters": ["letter hunt", "book letter search", "letter basket"],
    "attention": ["two-minute finish", "sticker card", "block build finish"],
    "matching": ["same/different match", "color sort", "sock match"],
    "routine": ["cleanup routine", "helper job", "two-step routine"],
    "social_turn": ["my-turn-your-turn", "peekaboo", "copy-me game", "social referencing"],
}


def _variant_theme(fam: str, variant: int, week: int = 1) -> str:
    bucket = _family_bucket(fam)
    themes = _FAMILY_THEMES.get(bucket, ["home play", "daily routine", "family activity"])
    # Week 3+ rotates to later theme slots for novelty (same bridge, different context)
    offset = 2 if week >= 3 else 0
    idx = ((variant - 1) + offset) % max(1, len(themes))
    return themes[idx]


def _get_allowed_themes(fam: str) -> List[str]:
    bucket = _family_bucket(fam)
    return _FAMILY_THEMES.get(bucket, ["home play", "daily routine"])


# ---------------------------------------------------------------------------
# LLM prompt  (V22 — privacy: "your child" always)
# ---------------------------------------------------------------------------

def _v22_activity_prompt(
    state: Dict[str, Any],
    category_key: str,
    bridge: Dict[str, Any],
    variant: int,
    week: int = 1,
) -> str:
    child = state.get("child", {})
    fam = bridge.get("activity_family", "")
    desc = get_family_description(fam) or ""
    safety = format_safety_constraints_for_prompt(ensure_safety_profile(state))
    focus = _clean(bridge.get("bridge_step", "") or bridge.get("activity_focus_step", ""))
    theme = _variant_theme(fam, variant, week)
    allowed_themes = ", ".join(_get_allowed_themes(fam))

    return (
        "You are writing one parent-facing Genex home activity card.\n\n"
        "Hard rules:\n"
        "- Write ONLY for bridge_step_1 and the specified activity_family.\n"
        "- Do NOT use previous_bridge_step. It is hidden future troubleshooting metadata.\n"
        "- Do NOT regress to earlier prerequisites.\n"
        "- Do NOT create a motor game unless the domain is Movement / Physical.\n"
        "- Do NOT create a generic placeholder activity.\n"
        "- Instructions must say exactly what the parent does, what the child does, "
        "what counts as success, and when to stop.\n"
        "- Keep it playful, low-pressure, and doable in 5 minutes.\n"
        "- Use safe household materials only.\n"
        "- Privacy: refer to the child as 'your child' — never use a name.\n"
        "- Return JSON only with exactly these keys: title, theme, instructions, "
        "success_criteria, make_easier, make_harder, group_play_line, what_to_avoid, materials.\n\n"
        f"Child profile (anonymised):\n"
        f"- age: {child.get('chronological_months', '')} months\n"
        f"- diagnosis/condition: {child.get('diagnosis', '') or 'none'}\n"
        f"- parent concern: {child.get('concern', '') or 'none'}\n\n"
        f"Planning inputs:\n"
        f"- domain: {DOMAIN_CONFIG.get(category_key, {}).get('display', category_key)}\n"
        f"- subdomain: {bridge.get('subdomain', '')}\n"
        f"- CDC milestone: {bridge.get('milestone', '')}\n"
        f"- bridge_step_1: {focus}\n"
        f"- activity_family: {fam}\n"
        f"- activity_family_description: {desc}\n"
        f"- suggested theme for this variant: {theme}\n"
        f"- allowed themes: {allowed_themes}\n"
        f"- variant number: {variant}\n"
        f"- safety notes: {safety}"
    )


# ---------------------------------------------------------------------------
# LLM writer
# ---------------------------------------------------------------------------

def _v22_call_llm_activity_writer(
    state: Dict[str, Any],
    category_key: str,
    bridge: Dict[str, Any],
    variant: int,
    week: int = 1,
) -> Optional[Dict[str, Any]]:
    client = _get_openai_client()
    if not client or not ACTIVITY_MODEL:
        return None
    prompt = _v22_activity_prompt(state, category_key, bridge, variant, week)
    try:
        response = client.chat.completions.create(
            model=ACTIVITY_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            response_format={"type": "json_object"},
            max_tokens=600,
        )
        raw = (response.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("title") and data.get("instructions"):
            return data
    except Exception as exc:
        logger.warning("[activity_engine] LLM call failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Deterministic fallback text  (V22)
# ---------------------------------------------------------------------------

def _v22_fallback_instructions(
    bucket: str,
    focus: str,
    fam: str,
    variant: int,
    week: int = 1,
) -> Dict[str, str]:
    theme = _variant_theme(fam, variant, week)
    base = {
        "title": f"{theme.title()} Practice",
        "theme": theme,
        "materials": "simple household items",
        "instructions": (
            f"Choose a short activity using {theme}. "
            f"Model the small step once: {focus}. "
            f"Invite your child to try one turn with as much help as needed. "
            f"Celebrate any attempt and stop after 2-3 turns."
        ),
        "success_criteria": f"Any calm attempt at: {focus}.",
        "make_easier": "Use one item, model first, shorten the turn, or accept a smaller response.",
        "make_harder": "Only if easy and enjoyable: add one small step or reduce support slightly.",
        "group_play_line": (
            "With another child: one person models, one supports, your child takes one short turn."
        ),
        "what_to_avoid": (
            "Avoid pressure, repeated correction, or continuing after frustration or fatigue."
        ),
    }
    overrides = {
        "book_page": {
            "title": f"Book Page Turn — {theme.title()}",
            "materials": "board book or thick-page book",
            "instructions": (
                "Sit together with a thick-page book. Hold it steady, lift one page edge, "
                "and invite your child to push or pull one page over. "
                "One assisted page turn counts as success."
            ),
            "what_to_avoid": "Avoid thin paper pages, rushing, or turning pages for your child.",
        },
        "fork_spoon": {
            "title": f"Fork/Spoon Practice — {theme.title()}",
            "materials": "child fork or spoon, soft safe food pieces or pretend food, plate",
            "instructions": (
                "Sit at a table with soft food or pretend food. "
                "Model one slow fork or spoon movement, then help your child stab or scoop one piece. "
                "Keep it supervised and brief."
            ),
            "what_to_avoid": "Avoid choking-risk foods, pressure to eat, or rushing.",
        },
        "dressing_on": {
            "title": f"Clothes On — {theme.title()}",
            "materials": "loose jacket, shirt, or pants",
            "instructions": (
                "Hold one loose sleeve or waistband open. Say a simple cue like 'arm in' "
                "and help your child push or pull one small clothing step. "
                "Any partial movement counts."
            ),
            "what_to_avoid": "Avoid tight clothing, multiple steps at once, or rushing.",
        },
        "dressing_off": {
            "title": f"Clothes Off — {theme.title()}",
            "materials": "loose elastic-waist pants or jacket",
            "instructions": (
                "Start the removal movement for your child, then invite one small pull, "
                "push, or arm-out movement. Sitting is fine if balance is difficult."
            ),
            "what_to_avoid": "Avoid tight clothing or multiple items at once.",
        },
        "buttoning": {
            "title": f"Button Practice — {theme.title()}",
            "materials": "large button board or shirt with big buttons",
            "instructions": (
                "Show one large button or closure. Help your child pull apart, push through, "
                "or line up one closure step. One partial movement counts."
            ),
            "what_to_avoid": "Avoid small buttons, frustration, or requiring full completion.",
        },
        "catch_ball": {
            "title": f"Soft Ball Game — {theme.title()}",
            "materials": "soft ball or rolled socks",
            "instructions": (
                "Sit close. Roll or gently pass a soft ball toward your child. "
                "Encourage looking at it and bringing hands toward it. Catching is not required."
            ),
            "what_to_avoid": "Avoid hard balls, long distances, or pressure to catch.",
        },
        "jump_prep": {
            "title": f"Safe Movement Practice — {theme.title()}",
            "materials": "clear flat floor, caregiver hand support",
            "instructions": (
                "On a clear flat floor, hold your child's hands or stay close. "
                "Model bending knees and standing tall. "
                "A knee bend, weight shift, or stand-up counts — do not require a jump."
            ),
            "what_to_avoid": "Avoid high surfaces, unsupported jumping, or speed.",
        },
        "expressive_word": {
            "title": f"Word Practice — {theme.title()}",
            "materials": "two favorite objects or pictures",
            "instructions": (
                f"Hold up two items related to {theme}. Pause and wait for your child "
                "to look, reach, point, or make a sound. Name it once and give it right away."
            ),
            "what_to_avoid": "Avoid asking 'say the word' repeatedly or withholding the item.",
        },
        "gesture": {
            "title": f"Gesture and Request — {theme.title()}",
            "materials": "two favorite objects or a clear container",
            "instructions": (
                "Put a favorite item in reach or a clear container. "
                "Pause and wait for your child to look, reach, point, gesture, or vocalize. "
                "Give the item right away and name it once."
            ),
            "what_to_avoid": "Avoid requiring a verbal word; accept any communication.",
        },
        "receptive_direction": {
            "title": f"One-Step Direction — {theme.title()}",
            "materials": "one familiar toy and a basket or simple target",
            "instructions": (
                "Give one clear familiar direction like 'give me the [item]' or 'put it in.' "
                "Add a gesture only if needed. Celebrate any attempt."
            ),
            "what_to_avoid": "Avoid two-step directions or repeating the direction more than once.",
        },
        "social_turn": {
            "title": f"Turn-Taking — {theme.title()}",
            "materials": "one favorite toy or no materials needed",
            "instructions": (
                "Use one toy or peekaboo. Say 'my turn,' take a brief turn, then 'your turn.' "
                "Keep turns very short and predictable. 2-3 back-and-forth exchanges."
            ),
            "what_to_avoid": "Avoid long turns, competition, or keeping score.",
        },
        "attention": {
            "title": f"Focus Practice — {theme.title()}",
            "materials": "3-5 simple pieces or a sticker card",
            "instructions": (
                "Choose a tiny task with a clear finish: 3 blocks or 3 stickers. "
                "Help your child stay with it until done, then stop."
            ),
            "what_to_avoid": "Avoid open-ended tasks or continuing past the clear finish.",
        },
    }
    if bucket in overrides:
        base.update(overrides[bucket])
    return base


# ---------------------------------------------------------------------------
# Make one activity  (V22)
# ---------------------------------------------------------------------------

def _v22_make_activity(
    category_key: str,
    bridge: Dict[str, Any],
    activity_type: str,
    variant: int,
    state: Dict[str, Any],
    week: int = 1,
) -> Dict[str, Any]:
    fam = str(bridge.get("activity_family", "") or "")
    bucket = _family_bucket(fam, category_key)
    focus = _clean(bridge.get("bridge_step", "") or bridge.get("activity_focus_step", ""))
    cdc_goal = _clean(bridge.get("milestone", "") or bridge.get("cdc_milestone", ""))
    category_display = DOMAIN_CONFIG.get(category_key, {}).get("display", category_key)

    fallback = _v22_fallback_instructions(bucket, focus, fam, variant, week)
    llm_data = _v22_call_llm_activity_writer(state, category_key, bridge, variant, week)

    data = dict(fallback)
    if llm_data:
        for k, v in llm_data.items():
            if isinstance(v, str) and v.strip():
                data[k] = _clean(v)

    title = _clean(data.get("title", fallback["title"]))
    theme = _clean(data.get("theme", fallback["theme"]))
    instructions = _clean(data.get("instructions", fallback["instructions"]))
    success = _clean(data.get("success_criteria", fallback["success_criteria"]))
    easier = _clean(data.get("make_easier", fallback["make_easier"]))
    harder = _clean(data.get("make_harder", fallback["make_harder"]))
    group_play = _clean(data.get("group_play_line", fallback["group_play_line"]))
    avoid = _clean(data.get("what_to_avoid", fallback["what_to_avoid"]))
    materials = _clean(data.get("materials", fallback["materials"]))

    if activity_type == "easier_backup":
        title = f"Easier: {title}" if not title.startswith("Easier") else title
        instructions += " Simplify: use one item, model first, or accept the smallest response."
    elif activity_type == "harder_stretch":
        title = f"Stretch: {title}" if not title.startswith("Stretch") else title
        instructions += " Only try this if the main version is easy and enjoyable."

    why = (
        f"This activity works on bridge step 1 — {focus.lower()} — "
        f'as a small step toward "{cdc_goal}".'
        if cdc_goal else
        f"This activity practices {focus.lower()} through playful repetition."
    )

    return {
        # Parent-facing
        "title": title,
        "theme": theme,
        "domain": category_display,
        "duration_minutes": V22_PER_ACTIVITY_MIN,
        "why": why,
        "instructions": instructions,
        "success": success,
        "easier": easier,
        "harder": harder,
        "group_play": group_play,
        "avoid": avoid,
        "materials": materials,
        "feedback_options": ACTIVITY_FEEDBACK_OPTIONS,
        # Debug fields (hidden from parents by default)
        "_debug": {
            "activity_id": f"v22_{category_key}_{_norm(cdc_goal)[:24]}_b1_{activity_type}_{variant}",
            "subdomain": bridge.get("subdomain", "unspecified"),
            "milestone": cdc_goal,
            "bridge_step_1": focus,
            "activity_family": fam,
            "activity_type": activity_type,
            "planning_mode": bridge.get("planning_mode", "standard"),
            "bridge_step_number": bridge.get("bridge_step_number", 1),
            "previous_bridge_step": bridge.get("previous_bridge_step", ""),
            "previous_bridge_status": "not_used_initial_plan__feedback_fallback_only",
            "engine_version": ENGINE_VERSION,
            "llm_used": bool(llm_data),
            "variant": variant,
            "week": week,
        },
        # For validator
        "activity_family": fam,
        "category_key": category_key,
        "validation_warnings": [],
    }


# ---------------------------------------------------------------------------
# Uniquify titles
# ---------------------------------------------------------------------------

def _v22_title_key(title: str) -> str:
    return re.sub(r"\W+", "_", title.lower()).strip("_")


def _v22_uniquify_titles(activities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Dict[str, int] = {}
    result = []
    for act in activities:
        key = _v22_title_key(act.get("title", ""))
        if key in seen:
            seen[key] += 1
            act = dict(act)
            act["title"] = f"{act['title']} (variation {seen[key]})"
        else:
            seen[key] = 1
        result.append(act)
    return result


# ---------------------------------------------------------------------------
# generate_category_activity_bank  (V22)
# ---------------------------------------------------------------------------

def generate_category_activity_bank(
    state: Dict[str, Any],
    category_key: str,
) -> Dict[str, Any]:
    """Generate the full activity bank for one category."""
    category_display = DOMAIN_CONFIG.get(category_key, {}).get(
        "display", category_key.replace("_", " ").title()
    )
    next_steps = select_next_milestones(
        state, category_key,
        max_milestones=V22_MAX_MILESTONES_PER_DOMAIN,
        min_milestones=V22_MIN_MILESTONES_PER_DOMAIN,
    )
    targets = next_steps.get("milestones", [])

    if not targets:
        bank = _empty_bank(category_key, category_display, next_steps)
        state.setdefault("activity_banks", {})[category_key] = bank
        return bank

    bridge_plan = build_bridge_plan_for_category(state, category_key, targets)
    active_bridges = bridge_plan.get("active_bridge_steps", [])
    planning_mode = bridge_plan.get("planning_mode", "standard")

    daily_time = int(state.get("child", {}).get("daily_time_min", 10) or 10)
    daily_slots = min(max(1, daily_time // V22_PER_ACTIVITY_MIN), V22_MAX_DAILY_ACTIVITIES)
    desired_week1_slots = V22_WEEK1_DAYS * daily_slots
    core_variants = max(2, math.ceil(desired_week1_slots / max(1, len(active_bridges))))
    core_variants = min(max(core_variants, 2), 7)

    raw_activities: List[Dict[str, Any]] = []
    for bridge in active_bridges:
        for variant in range(1, core_variants + 1):
            raw_activities.append(
                _v22_make_activity(category_key, bridge, "core", variant, state, week=1)
            )
        raw_activities.append(
            _v22_make_activity(category_key, bridge, "easier_backup", 1, state, week=1)
        )
        raw_activities.append(
            _v22_make_activity(category_key, bridge, "harder_stretch", 2, state, week=1)
        )

    raw_activities = _v22_uniquify_titles(raw_activities)
    valid_activities, blocked_activities = filter_valid_activities(raw_activities, category_key)

    warnings = list({w for a in raw_activities for w in a.get("validation_warnings", [])})

    bank = {
        "status": "ok" if valid_activities else "no_valid_activities",
        "version": ENGINE_VERSION,
        "category_key": category_key,
        "category": category_display,
        "planning_mode": planning_mode,
        "summary": next_steps.get("message", ""),
        "target_milestones": targets,
        "active_bridges": len(active_bridges),
        "activities": valid_activities,
        "blocked_activities": blocked_activities,
        "validation_warnings": warnings,
        "daily_slots": daily_slots,
        "core_variants_per_bridge": core_variants,
    }
    state.setdefault("activity_banks", {})[category_key] = bank
    return bank


def _empty_bank(
    category_key: str,
    category_display: str,
    next_steps: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "status": "no_targets",
        "version": ENGINE_VERSION,
        "category_key": category_key,
        "category": category_display,
        "planning_mode": next_steps.get("mode", "no_targets"),
        "summary": next_steps.get("message", "No target milestones found."),
        "target_milestones": [],
        "active_bridges": 0,
        "activities": [],
        "blocked_activities": [],
        "validation_warnings": [next_steps.get("message", "no_targets")],
        "daily_slots": 1,
        "core_variants_per_bridge": 0,
    }


def get_core_pool(bank: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return only 'core' type activities from a bank (used by scheduler)."""
    return [
        a for a in bank.get("activities", [])
        if a.get("_debug", {}).get("activity_type", "core") == "core"
    ]
