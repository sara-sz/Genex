"""
genex_core/safety.py
--------------------
Safety / practical constraint profile builder and activity safety pass.
Extracted from genex_interview_activity_v11.ipynb — logic unchanged.
"""

import re
from typing import Any, Dict, List

from genex_core.config import SAFETY_KEYWORD_MAP, SAFETY_CONSTRAINT_TEMPLATES


# ---------------------------------------------------------------------------
# Safe movement replacement cards  (seizure / unstable-walk profiles)
# ---------------------------------------------------------------------------
# Used when jump/hop/climb activities are hard-blocked.  Cycling through the
# pool ensures no duplicate titles even when multiple activities are replaced.

_SAFE_MOVEMENT_CARDS: List[Dict[str, str]] = [
    {
        "title": "Supported Squat-and-Reach Game",
        "instructions": (
            "With your child standing near a wall or holding your hands for support, "
            "place a favourite toy on the floor in front of them. "
            "Help them bend their knees slowly to reach down and pick it up, then stand back up. "
            "Do 4–5 slow squat-and-reach turns. Keep both feet flat on the floor throughout. "
            "Use stable support and close adult supervision throughout."
        ),
        "materials": "wall or caregiver hand support, 5 small favourite toys, a basket",
        "avoid": (
            "Avoid any jumping, hopping, or movement without full caregiver support. "
            "Keep both feet flat and stable at all times."
        ),
        "success_criteria": "Your child bends and reaches to pick up one toy with support.",
        "make_easier": "Support under both arms and guide through a very gentle knee bend together.",
        "make_harder": "Let your child reach slightly farther forward while holding just one hand.",
        "group_play_line": "With another child, one drops the toy and the other picks it up.",
    },
    {
        "title": "Sticker Tap While Seated",
        "instructions": (
            "Sit your child on a stable chair or cushion on the floor. "
            "Place 4–5 round stickers on a low surface in front of them at arm's reach. "
            "Say 'tap the sticker!' and model pressing one sticker firmly. "
            "Let your child tap each sticker in turn. Celebrate each tap. "
            "Keep everything within safe reaching distance — no standing needed. "
            "Use stable support and close adult supervision throughout."
        ),
        "materials": "stable chair or cushion, 4–5 round stickers on a low table or tray",
        "avoid": (
            "Avoid asking your child to stand, balance on one leg, or reach far sideways. "
            "This is a fully seated activity."
        ),
        "success_criteria": "Your child taps at least 2 stickers while seated.",
        "make_easier": "Hold the sticker sheet yourself and let your child reach to press each dot.",
        "make_harder": "Spread stickers across a slightly wider area to encourage gentle trunk rotation.",
        "group_play_line": "Two children sit side by side and each tap their own row of stickers.",
    },
    {
        "title": "Slow Stand-and-Sit Practice",
        "instructions": (
            "Place a low sturdy chair or the couch in front of your child. "
            "Hold their hands or forearms for support. "
            "Say 'sit down slowly… now stand up slowly' and move together at a gentle pace. "
            "Count 'one, two, three' on the way up and on the way down. "
            "Do 4–5 slow sit-stand turns. Rest between each one. "
            "Use stable support and close adult supervision throughout."
        ),
        "materials": "low sturdy chair or couch edge, caregiver hand support",
        "avoid": (
            "Avoid rushing, unsupported standing, or asking your child to push off without full support. "
            "No jumping or hopping."
        ),
        "success_criteria": "Your child completes one slow stand-up or sit-down movement with support.",
        "make_easier": "Only practise the sit-down direction and hold both hands throughout.",
        "make_harder": "Count to 3 before offering the next hand — let your child begin the movement alone.",
        "group_play_line": "An older sibling can model the slow stand-sit while you support your child.",
    },
    {
        "title": "Seated Animal Arms Game",
        "instructions": (
            "Sit together on the floor or on low chairs facing each other. "
            "Say an animal name and show what its arms do: "
            "'elephant — swing your arms like a trunk!' "
            "'bird — flap slowly!' 'bear — big hug arms!' "
            "Stay seated throughout. Do 4–5 animals, celebrating each attempt. "
            "Use stable support and close adult supervision throughout."
        ),
        "materials": "no materials needed",
        "avoid": (
            "Avoid standing up, jumping, or any fast movements. "
            "Keep this a calm, fully seated game throughout."
        ),
        "success_criteria": "Your child copies at least one arm movement while staying seated.",
        "make_easier": "Hold your child's hands and gently move them through the motion together.",
        "make_harder": "Do the motion without naming the animal and let your child guess which one.",
        "group_play_line": "With another child, one calls the animal and both copy the arms together.",
    },
    {
        "title": "Supported Step-and-Stop Game",
        "instructions": (
            "Hold your child's hand firmly on flat, clear floor. "
            "Take one step forward together, then stop and stand still for 3 seconds. "
            "Say 'step… and stop!' with each move. "
            "Take 4–5 single steps with pauses — no rushing or running. "
            "Use stable support and close adult supervision throughout."
        ),
        "materials": "clear flat floor space, caregiver hand support",
        "avoid": (
            "Avoid multiple quick steps in a row, turning fast, stepping on uneven surfaces, "
            "or letting go of the caregiver's hand at any point. No jumping or hopping."
        ),
        "success_criteria": "Your child takes at least one controlled step and pauses with support.",
        "make_easier": "Start from sitting — just practise standing up and stopping before adding a step.",
        "make_harder": "Add a fun stop signal: say 'freeze!' and see if your child pauses on the word.",
        "group_play_line": "An older sibling holds the other hand to give extra balance support.",
    },
]


def build_safety_profile(child: Dict[str, Any]) -> Dict[str, Any]:
    """Build a broad safety/practical constraint profile from diagnosis + concern text."""
    diagnosis = str(child.get("diagnosis", "") or "")
    concern = str(child.get("concern", "") or "")
    combined_text = f"{diagnosis} | {concern}".lower()

    risk_scores = {k: 0.0 for k in SAFETY_KEYWORD_MAP.keys()}
    matched_patterns = {k: [] for k in SAFETY_KEYWORD_MAP.keys()}

    for risk_name, patterns in SAFETY_KEYWORD_MAP.items():
        matches = []
        for pat in patterns:
            if re.search(pat, combined_text):
                matches.append(pat)
        if matches:
            risk_scores[risk_name] = min(1.0, 0.35 * len(matches))
            matched_patterns[risk_name] = matches

    top_risks = [
        {"risk": k, "weight": round(v, 2)}
        for k, v in sorted(risk_scores.items(), key=lambda kv: kv[1], reverse=True)
        if v > 0
    ][:6]

    constraints = [
        SAFETY_CONSTRAINT_TEMPLATES[risk_name]
        for risk_name, score in sorted(risk_scores.items(), key=lambda kv: kv[1], reverse=True)
        if score >= 0.35 and risk_name in SAFETY_CONSTRAINT_TEMPLATES
    ]

    hard_avoid = []
    preferred_adaptations = []

    # Seizure / Dravet / medical-monitoring profiles also require the same
    # fall/jump hard-blocks — sudden loss of awareness during jumping or
    # unsupported balancing is a serious injury risk.
    high_fall_or_mobility = (
        risk_scores.get("falls_balance_gait", 0.0) >= 0.35
        or risk_scores.get("mobility_equipment_support", 0.0) >= 0.35
        or risk_scores.get("seizure_or_medical_monitoring", 0.0) >= 0.35
    )
    if high_fall_or_mobility:
        hard_avoid.extend([
            "jumping from heights or unsupported jumping drills",
            "hopping, trampoline-style activities, or any unsupported airborne movement",
            "playground climbing or high unstable surfaces",
            "unsupported balance challenges on unstable surfaces",
            "racing, obstacle courses, or fast-paced movement sequences",
        ])
        preferred_adaptations.extend([
            "prefer ground-level or fully seated versions of movement activities",
            "use hand support, wall support, or equipment support at all times",
            "favour supported reaching, slow weight shifting, and step-and-stop practice",
        ])

    if risk_scores.get("postural_low_tone_fatigue", 0.0) >= 0.35:
        hard_avoid.extend([
            "long sustained postures without rest",
            "high-endurance tasks that assume good postural endurance",
        ])
        preferred_adaptations.extend([
            "short bouts with rest breaks",
            "supported positioning and lower-endurance versions",
        ])

    if risk_scores.get("feeding_or_oral_motor", 0.0) >= 0.35:
        hard_avoid.extend([
            "unsafe oral-motor suggestions",
            "choking-risk foods or unsupervised feeding tasks",
        ])
        preferred_adaptations.extend([
            "keep feeding upright and closely supervised",
            "use safer textures and stop if coughing, gagging, or distress occurs",
        ])

    if risk_scores.get("regulation_frustration", 0.0) >= 0.35:
        hard_avoid.extend([
            "long multi-step activities without breaks",
            "activities that depend on long waiting or repeated failure without support",
        ])
        preferred_adaptations.extend([
            "keep tasks short, predictable, and easy to start",
            "offer choices and stop before escalation",
        ])

    if risk_scores.get("sensory_sensitivity", 0.0) >= 0.35:
        preferred_adaptations.extend([
            "use lower-noise and lower-clutter materials when possible",
            "introduce textures and sounds gradually",
        ])

    def _dedupe(items):
        seen = set()
        out = []
        for x in items:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    constraints = _dedupe(constraints)
    hard_avoid = _dedupe(hard_avoid)
    preferred_adaptations = _dedupe(preferred_adaptations)

    if not constraints:
        constraints = [
            "No major safety constraints were inferred from the diagnosis/concern text. "
            "Keep activities parent-supervised and home-safe."
        ]

    return {
        "combined_text": combined_text,
        "risk_scores": risk_scores,
        "matched_patterns": matched_patterns,
        "top_risks": top_risks,
        "constraints": constraints,
        "hard_avoid": hard_avoid,
        "preferred_adaptations": preferred_adaptations,
    }


def ensure_safety_profile(state: Dict[str, Any]) -> Dict[str, Any]:
    if not state.get("safety_profile"):
        state["safety_profile"] = build_safety_profile(state["child"])
    return state["safety_profile"]


def format_safety_constraints_for_prompt(profile: Dict[str, Any]) -> str:
    lines = []
    constraints = profile.get("constraints", [])
    hard_avoid = profile.get("hard_avoid", [])
    adaptations = profile.get("preferred_adaptations", [])

    if constraints:
        lines.append("Planning constraints:")
        lines.extend([f"- {c}" for c in constraints])
    if hard_avoid:
        lines.append("Hard avoidances:")
        lines.extend([f"- {c}" for c in hard_avoid])
    if adaptations:
        lines.append("Preferred adaptations:")
        lines.extend([f"- {c}" for c in adaptations])

    if not lines:
        lines = ["- No special safety constraints flagged. Keep activities parent-supervised and home-safe."]

    return "\n".join(lines)


CONTEXT_DEPENDENT_BONUS_PATTERN = re.compile(
    r"\b(playdate|park meetup|playground peer|group social|group activity|peer practice|community class|outing|meetup)\b",
    flags=re.IGNORECASE,
)


def is_context_dependent_bonus_activity(activity: Dict[str, Any]) -> bool:
    text = " ".join([
        str(activity.get("title", "")),
        str(activity.get("instructions", "")),
        str(activity.get("materials", "")),
        str(activity.get("extended_reason", "")),
    ])
    return bool(CONTEXT_DEPENDENT_BONUS_PATTERN.search(text))


def apply_safety_constraints_to_activities(
    state: Dict[str, Any],
    category_key: str,
    activities: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Deterministically adapt or sanitize activities based on the broad safety profile."""
    profile = ensure_safety_profile(state)
    risk_scores = profile.get("risk_scores", {})

    # Seizure / Dravet / medical-monitoring also triggers the movement hard-block —
    # sudden loss of awareness during jumping or unsupported balance is a fall risk.
    high_fall_or_mobility = (
        risk_scores.get("falls_balance_gait", 0.0) >= 0.35
        or risk_scores.get("mobility_equipment_support", 0.0) >= 0.35
        or risk_scores.get("seizure_or_medical_monitoring", 0.0) >= 0.35
    )
    low_tone_or_fatigue = risk_scores.get("postural_low_tone_fatigue", 0.0) >= 0.35
    feeding_risk = risk_scores.get("feeding_or_oral_motor", 0.0) >= 0.35
    regulation_risk = risk_scores.get("regulation_frustration", 0.0) >= 0.35
    sensory_risk = risk_scores.get("sensory_sensitivity", 0.0) >= 0.35

    safe_activities = []
    # Counter for cycling through safe replacement cards so each replaced
    # activity gets a distinct title — avoids duplicate titles on the schedule.
    _replace_counter = 0

    for activity in activities:
        a = dict(activity)
        title = str(a.get("title", ""))
        instructions = str(a.get("instructions", ""))
        materials = str(a.get("materials", "common household items"))
        duration_min = int(a.get("duration_min", 5))
        lower_text = f"{title} {instructions}".lower()

        if high_fall_or_mobility and category_key == "movement_and_physical" and re.search(
            r'\b(jump|jumping|trampoline|hop|hopping|frog|climb|climbing|playground|race|racing|stomp|stomping)\b'
            r'|squat\s+and\s+reach',
            lower_text,
        ):
            card = _SAFE_MOVEMENT_CARDS[_replace_counter % len(_SAFE_MOVEMENT_CARDS)]
            _replace_counter += 1
            a["title"] = card["title"]
            a["instructions"] = card["instructions"]
            a["materials"] = card["materials"]
            a["avoid"] = card["avoid"]
            a["success"] = card.get("success_criteria", a.get("success", ""))
            a["make_easier"] = card.get("make_easier", a.get("make_easier", ""))
            a["make_harder"] = card.get("make_harder", a.get("make_harder", ""))
            a["group_play"] = card.get("group_play_line", a.get("group_play", ""))
            a["duration_min"] = min(duration_min, 7)
            if a.get("_debug"):
                a["_debug"]["safety_replaced"] = True
                a["_debug"]["safety_replace_reason"] = "jump_hop_blocked_high_fall_or_seizure_risk"
            title = a["title"]
            instructions = a["instructions"]
            duration_min = a["duration_min"]
            lower_text = f"{title} {instructions}".lower()

        if high_fall_or_mobility and category_key == "movement_and_physical":
            support_note = " Use stable support and close adult supervision throughout."
            if ("close adult support" not in instructions.lower()
                    and "stable support" not in instructions.lower()
                    and "supervision throughout" not in instructions.lower()):
                a["instructions"] = instructions.rstrip() + support_note
                instructions = a["instructions"]

        if low_tone_or_fatigue:
            a["duration_min"] = min(int(a.get("duration_min", duration_min)), 7)
            if "rest break" not in instructions.lower():
                a["instructions"] = instructions.rstrip() + " Keep the activity in short bouts and pause for rest if the child tires."
                instructions = a["instructions"]

        if regulation_risk:
            a["duration_min"] = min(int(a.get("duration_min", duration_min)), 5)
            if "predictable" not in instructions.lower():
                a["instructions"] = instructions.rstrip() + " Keep it short and predictable, offer a simple choice, and stop before frustration escalates."
                instructions = a["instructions"]

        if sensory_risk and "lower-noise" not in instructions.lower():
            a["instructions"] = instructions.rstrip() + " Use lower-noise, lower-clutter materials when possible."
            instructions = a["instructions"]

        if feeding_risk and re.search(r'\b(food|snack|chew|mouth|eat|drink|feeding)\b', lower_text):
            if "upright" not in instructions.lower():
                a["instructions"] = instructions.rstrip() + (
                    " Keep the child upright and closely supervised. "
                    "Avoid choking-risk foods and stop if coughing, gagging, or distress occurs."
                )
                instructions = a["instructions"]

        if is_context_dependent_bonus_activity(a):
            a["is_extended_activity"] = True
            normalized_duration = int(a.get("duration_min", duration_min))
            if normalized_duration < 30:
                normalized_duration = 30
            if normalized_duration > 45:
                normalized_duration = 45
            a["duration_min"] = normalized_duration
            if "optional weekly bonus" not in str(a.get("extended_reason", "")).lower():
                a["extended_reason"] = (
                    "Longer, context-dependent social/community activity best treated as "
                    "an optional weekly bonus rather than a short daily at-home task."
                )

        safe_activities.append(a)

    return safe_activities
