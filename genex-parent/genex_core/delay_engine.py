"""
genex_core/delay_engine.py
--------------------------
Starting delay estimation per domain.
Uses OpenAI if available; falls back to deterministic heuristics.
This is ONLY a starting anchor for question selection — NOT a diagnosis or final dev age.
"""

import json
import os
from typing import Any, Dict, List, Optional

from genex_core.config import CONTENT_READY_DOMAIN_KEYS, DOMAIN_CONFIG

# Lazy client initialization
_openai_client = None
_openai_initialized = False


def _get_openai_client():
    global _openai_client, _openai_initialized
    if _openai_initialized:
        return _openai_client
    _openai_initialized = True
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if api_key:
        try:
            from openai import OpenAI
            _openai_client = OpenAI(api_key=api_key)
        except ImportError:
            _openai_client = None
    return _openai_client


# Domain keyword signals for heuristic fallback — PARENT 2.4 seven-domain.
#
# The legacy `movement_and_physical` list bundled three different concerns, so
# it is SPLIT here rather than renamed: hand/grasp/writing terms are Fine Motor,
# walking/balance/posture terms are Gross Motor, and the self-care terms that
# used to sit under `cognitive` ("toilet", "dressing", "self-care") move to
# Daily Living where they belong.
#
# Had these simply kept their legacy keys they would never match a Parent 2.4
# domain, and Fine Motor / Gross Motor / Daily Living would silently receive no
# keyword signal at all — a failure that looks like "no concern detected".
DOMAIN_KEYWORDS = {
    "fine_motor": [
        "fine motor", "grasp", "hand", "hands", "fingers", "pincer", "writing",
        "drawing", "scribble", "cutting", "buttons", "manipulation",
    ],
    "gross_motor": [
        "motor", "movement", "walk", "run", "jump", "balance", "coordination",
        "gross motor", "stairs", "falls", "hypotonia", "sitting", "rolling",
        "crawling", "posture", "standing", "clumsy",
    ],
    "daily_living": [
        "adaptive", "toilet", "toileting", "potty", "dressing", "self-care",
        "self care", "feeding", "mealtime", "independent", "routine",
        "safety", "washing",
    ],
    "talking_and_communicating": [
        "speech", "language", "talk", "communication", "words", "sentence",
        "understand", "expressive", "receptive", "verbal", "babbling", "no words",
    ],
    "social_and_emotional": [
        "social", "peer", "friend", "play", "emotion", "emotional",
        "behavior", "anger", "meltdown", "interaction", "turn taking",
        "regulation", "eye contact", "transitions",
    ],
    "learning_and_thinking": [
        "attention", "focus", "concentration", "school", "learning",
        "executive", "task", "directions", "problem solving", "memory",
    ],
    # Sensory deliberately has NO keyword list. The domain exists but has no
    # validated content, so producing a delay anchor for it would imply a plan
    # Genex cannot build. Fail closed instead — see estimate_all_delays.
}

FALLBACK_DELAY = {
    "fine_motor": 3,
    "gross_motor": 3,
    "daily_living": 6,
    "talking_and_communicating": 3,
    "social_and_emotional": 6,
    "learning_and_thinking": 6,
    # No entry for sensory — see above.
}


def estimate_delay_for_domain(
    diagnosis: str,
    concern: str,
    chronological_months: int,
    category_key: str,
    model: str = "gpt-4o-mini",
) -> Dict[str, Any]:
    """Estimate a rough starting delay in months for one domain.

    This is ONLY a starting anchor for question selection — not a final developmental age.
    AI is used here purely to improve the question window, not for scoring or diagnosis.
    Falls back to deterministic heuristics if OpenAI is unavailable.
    """
    client = _get_openai_client()
    category_display = DOMAIN_CONFIG[category_key]["display"]
    concern_l = (concern or "").lower()

    has_domain_signal = any(
        kw in concern_l for kw in DOMAIN_KEYWORDS.get(category_key, [])
    )

    fallback_delay = FALLBACK_DELAY.get(category_key, 6)

    if client is None:
        if not has_domain_signal:
            fallback_delay = min(fallback_delay, 6)
        return {
            "delay_months": fallback_delay,
            "reason": f"Deterministic fallback for {category_display} (no OpenAI key configured).",
            "source": "fallback",
        }

    prompt = f"""
You are a pediatric developmental delay estimator agent for children ages 0 to 5 years.

Your job is to estimate a SINGLE STARTING DELAY in months for one developmental domain only.

This is NOT a diagnosis.
This is NOT a final developmental age.
This is ONLY a rough starting anchor for question selection.

Definition:
delay_months = chronological age in months - estimated functional developmental age in this specific domain

Child information:
- Chronological age in months: {chronological_months}
- Diagnosis / condition: {diagnosis}
- Parent concern: {concern}
- Domain to estimate: {category_display}

Instructions:
1. Think only about THIS domain, not overall development.
2. If the diagnosis or concern does NOT meaningfully affect this domain, return 0 to 6 months.
3. If this domain IS affected, estimate the child's functional developmental level, then convert to delay_months.
4. Be conservative but realistic.
5. Never exceed the child's chronological age.
6. Return only one integer number of months.
7. Return strict JSON only.

Required JSON:
{{
  "delay_months": <integer>,
  "reason": "<one short sentence>"
}}
""".strip()

    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You return strict JSON only."},
                {"role": "user", "content": prompt},
            ],
        )
        parsed = json.loads(resp.choices[0].message.content)
        delay_months = int(parsed.get("delay_months", fallback_delay))
        delay_months = max(0, min(delay_months, chronological_months))

        if not has_domain_signal and delay_months > 6:
            delay_months = 6

        return {
            "delay_months": delay_months,
            "reason": parsed.get("reason", ""),
            "source": "openai",
        }

    except Exception as e:
        return {
            "delay_months": fallback_delay,
            "reason": f"Fallback used (OpenAI call failed): {e}",
            "source": "fallback",
        }


def estimate_all_delays(
    state: Dict[str, Any],
    categories: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Estimate starting delays for all (or specified) categories and store in state."""
    if not state.get("child"):
        raise ValueError("Child profile missing. Fill the profile form first.")

    # Parent 2.4: only content-ready domains get a delay anchor. Sensory is a
    # canonical domain with no validated rows, so estimating a delay for it
    # would promise a plan Genex cannot build. Fail closed by skipping it.
    categories = categories or list(CONTENT_READY_DOMAIN_KEYS)
    categories = [c for c in categories if c in CONTENT_READY_DOMAIN_KEYS]
    child = state["child"]

    for category_key in categories:
        est = estimate_delay_for_domain(
            diagnosis=child["diagnosis"],
            concern=child["concern"],
            chronological_months=child["chronological_months"],
            category_key=category_key,
        )
        state["delay_estimates"][category_key] = est

    return state["delay_estimates"]
