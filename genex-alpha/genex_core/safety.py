"""
genex_core/safety.py
--------------------
Safety / practical constraint profile builder and activity safety pass.
Extracted from genex_interview_activity_v11.ipynb — logic unchanged.
"""

import re
from typing import Any, Dict, List

from genex_core.config import SAFETY_KEYWORD_MAP, SAFETY_CONSTRAINT_TEMPLATES


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

    high_fall_or_mobility = (
        risk_scores.get("falls_balance_gait", 0.0) >= 0.35
        or risk_scores.get("mobility_equipment_support", 0.0) >= 0.35
    )
    if high_fall_or_mobility:
        hard_avoid.extend([
            "jumping from heights or unsupported jumping drills",
            "trampoline-style activities",
            "playground climbing or high unstable surfaces",
            "unsupported balance challenges on unstable surfaces",
        ])
        preferred_adaptations.extend([
            "prefer ground-level or seated versions when possible",
            "use hand support, wall support, or equipment support as needed",
            "favor supported reaching, weight shifting, step-up, and transfer practice over high-risk movement tasks",
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

    high_fall_or_mobility = (
        risk_scores.get("falls_balance_gait", 0.0) >= 0.35
        or risk_scores.get("mobility_equipment_support", 0.0) >= 0.35
    )
    low_tone_or_fatigue = risk_scores.get("postural_low_tone_fatigue", 0.0) >= 0.35
    feeding_risk = risk_scores.get("feeding_or_oral_motor", 0.0) >= 0.35
    regulation_risk = risk_scores.get("regulation_frustration", 0.0) >= 0.35
    sensory_risk = risk_scores.get("sensory_sensitivity", 0.0) >= 0.35

    safe_activities = []

    for activity in activities:
        a = dict(activity)
        title = str(a.get("title", ""))
        instructions = str(a.get("instructions", ""))
        materials = str(a.get("materials", "common household items"))
        duration_min = int(a.get("duration_min", 5))
        lower_text = f"{title} {instructions}".lower()

        if high_fall_or_mobility and re.search(
            r'\b(jump|jumping|trampoline|hop|hopping|climb|climbing|playground)\b', lower_text
        ):
            a["title"] = "Supported Balance and Reach Practice"
            a["instructions"] = (
                "Use a ground-level activity with close adult support. Practice supported standing, "
                "seated or supported reaching, and safe weight shifts while keeping both feet on a stable surface. "
                "Avoid jumping, climbing, or unstable surfaces."
            )
            a["materials"] = "stable chair or couch, wall or caregiver support, favorite toys"
            a["duration_min"] = min(duration_min, 7)
            title = a["title"]
            instructions = a["instructions"]
            duration_min = a["duration_min"]
            lower_text = f"{title} {instructions}".lower()

        if high_fall_or_mobility and category_key == "movement_and_physical":
            if "close adult support" not in instructions.lower() and "stable support" not in instructions.lower():
                a["instructions"] = instructions.rstrip() + " Use stable support and close adult supervision throughout."
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
