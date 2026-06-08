"""
genex_core/concern_router_llm.py
---------------------------------
LLM fallback for ambiguous parent concern classification.

Called by the concern routing pipeline ONLY when deterministic keyword routing
produces low confidence (routing_confidence < LOW_CONFIDENCE_THRESHOLD).

Rules:
- LLM classifies concern text into 1–2 allowed Genex domains ONLY.
- LLM does NOT diagnose, score development, or assign developmental age.
- Child first name is NEVER sent to the LLM.
- Input: age in months, diagnosis (if any), concern text.
- Output: constrained JSON with selected_domains, confidence, reason,
          matched_phrases, safety_flags.
- If LLM confidence is still low after classification:
    - logs a warning and returns the best guess with low_confidence=True
    - caller can choose to ask one clarifying question OR proceed with
      parent_concern_support_no_clear_gap (post-milestone, not here)
- Uses CONCERN_ROUTER_MODEL env var. If not set, returns None and
  logs a warning — deterministic routing result is used as-is.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from genex_core.config import CONCERN_ROUTER_MODEL, DOMAIN_CONFIG

logger = logging.getLogger(__name__)

# Threshold below which we call the LLM
LOW_CONFIDENCE_THRESHOLD = 0.35

# Threshold below which the LLM result is marked low_confidence
LLM_LOW_CONFIDENCE_THRESHOLD = 0.50

ALLOWED_DOMAIN_DISPLAYS = [cfg["display"] for cfg in DOMAIN_CONFIG.values()]
DISPLAY_TO_KEY = {cfg["display"]: key for key, cfg in DOMAIN_CONFIG.items()}

_SYSTEM_PROMPT = """\
You are a developmental domain classifier for Genex, a child developmental support app.

Your ONLY job is to classify a parent's concern about their child into 1 or 2 of these developmental domains:
- Movement / Physical
- Social / Emotional
- Language / Communication
- Cognitive / Adaptive

Rules you must follow:
1. Do NOT diagnose the child.
2. Do NOT estimate developmental age.
3. Do NOT score development.
4. Only classify the concern text into existing domains.
5. If the concern is truly ambiguous, pick the single closest domain and set confidence below 0.60.
6. Return ONLY valid JSON. No explanation, no extra text.
7. The child's first name will NOT be provided. Use "the child" if you need to refer to them.

Output format (JSON only):
{
  "selected_domains": ["Language / Communication"],
  "confidence": 0.85,
  "reason": "Parent describes limited speech output and gestures.",
  "matched_phrases": ["limited speech", "uses gestures"],
  "safety_flags": []
}

Allowed domain values: """ + ", ".join(f'"{d}"' for d in ALLOWED_DOMAIN_DISPLAYS)


def _build_user_message(
    age_months: int,
    diagnosis: str,
    concern: str,
) -> str:
    parts = [f"Child age: {age_months} months"]
    if diagnosis and diagnosis.strip() and diagnosis.lower() not in {"none", "no diagnosis", "n/a", ""}:
        parts.append(f"Diagnosis/condition: {diagnosis.strip()}")
    parts.append(f"Parent concern: {concern.strip()}")
    return "\n".join(parts)


def _parse_llm_output(raw: str) -> Optional[Dict[str, Any]]:
    """Extract and validate the JSON object from LLM output."""
    raw = raw.strip()
    # Strip markdown fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Try to find a JSON object
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group())
        except json.JSONDecodeError:
            return None

    # Validate structure
    if not isinstance(data.get("selected_domains"), list):
        return None
    if not isinstance(data.get("confidence"), (int, float)):
        return None

    # Validate domain names
    valid_domains = []
    for d in data["selected_domains"]:
        if d in DISPLAY_TO_KEY:
            valid_domains.append(d)
    if not valid_domains:
        return None

    data["selected_domains"] = valid_domains[:2]  # max 2
    data["confidence"] = float(data["confidence"])
    data["reason"] = str(data.get("reason", ""))
    data["matched_phrases"] = list(data.get("matched_phrases", []))
    data["safety_flags"] = list(data.get("safety_flags", []))
    return data


def _call_openai(prompt_user: str, model: str) -> Optional[str]:
    try:
        from openai import OpenAI  # lazy import
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            logger.warning("[concern_router_llm] OPENAI_API_KEY not set.")
            return None
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt_user},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
            max_tokens=300,
        )
        return response.choices[0].message.content
    except Exception as exc:
        logger.warning("[concern_router_llm] OpenAI call failed: %s", exc)
        return None


def route_concern_with_llm(
    age_months: int,
    diagnosis: str,
    concern: str,
) -> Optional[Dict[str, Any]]:
    """Call the LLM concern router.

    Returns a dict with selected_domain_keys, confidence, reason,
    matched_phrases, safety_flags, low_confidence.
    Returns None if no model is configured or the call fails.

    Privacy: child first name is NOT included in the prompt.
    """
    if not CONCERN_ROUTER_MODEL:
        logger.warning(
            "[concern_router_llm] CONCERN_ROUTER_MODEL env var not set. "
            "Using deterministic routing result as-is."
        )
        return None

    user_msg = _build_user_message(age_months, diagnosis, concern)
    raw = _call_openai(user_msg, CONCERN_ROUTER_MODEL)
    if raw is None:
        return None

    parsed = _parse_llm_output(raw)
    if parsed is None:
        logger.warning("[concern_router_llm] Could not parse LLM output: %s", raw[:200])
        return None

    # Map display names to category keys
    selected_keys = [DISPLAY_TO_KEY[d] for d in parsed["selected_domains"]]
    low_confidence = parsed["confidence"] < LLM_LOW_CONFIDENCE_THRESHOLD

    if low_confidence:
        logger.info(
            "[concern_router_llm] Low-confidence result (%.2f) for concern: %s",
            parsed["confidence"],
            concern[:80],
        )

    return {
        "selected_domain_keys": selected_keys,
        "selected_domain_displays": parsed["selected_domains"],
        "confidence": parsed["confidence"],
        "reason": parsed["reason"],
        "matched_phrases": parsed["matched_phrases"],
        "safety_flags": parsed["safety_flags"],
        "low_confidence": low_confidence,
        "source": "llm",
    }


def augment_concern_profile(
    concern_profile: Dict[str, Any],
    age_months: int,
    diagnosis: str,
    concern: str,
) -> Dict[str, Any]:
    """Augment the deterministic concern profile with LLM result if needed.

    Call this after concern_router() when routing_confidence is low.
    Merges LLM domain signals into the existing profile.
    Returns the (possibly augmented) profile.
    """
    routing_confidence = float(concern_profile.get("routing_confidence", 1.0))
    if routing_confidence >= LOW_CONFIDENCE_THRESHOLD:
        return concern_profile  # deterministic routing is sufficient

    llm_result = route_concern_with_llm(age_months, diagnosis, concern)
    if llm_result is None:
        # No model available — keep deterministic result
        concern_profile["llm_augmented"] = False
        return concern_profile

    # Boost domain weights for LLM-selected domains
    domain_weights = dict(concern_profile.get("domain_weights", {}))
    for key in llm_result["selected_domain_keys"]:
        # LLM result raises the weight to at least 0.50 for selected domains
        domain_weights[key] = max(domain_weights.get(key, 0.0), 0.50)

    concern_profile = dict(concern_profile)
    concern_profile["domain_weights"] = domain_weights
    concern_profile["llm_augmented"] = True
    concern_profile["llm_result"] = llm_result
    concern_profile["routing_confidence"] = max(
        routing_confidence, llm_result["confidence"]
    )

    # If LLM is still low confidence, flag it for the caller
    concern_profile["needs_clarification"] = llm_result.get("low_confidence", False)

    return concern_profile
