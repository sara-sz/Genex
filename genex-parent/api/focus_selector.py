"""
api/focus_selector.py — Beta 2.2 primary-focus selection (API layer only).

Keeps genex_core FROZEN. Maps a parent's concern text to the 4 developmental focus
areas (== the 4 existing domain keys) using API-layer keyword patterns, then picks
the single PRIMARY focus using a confirmed clinical priority order.

Focus key == domain key (1:1):
  language_and_communication → "Speech & Communication"
  movement_and_physical      → "Fine & Gross Motor & Daily Skills"
  cognitive                  → "Learning, Attention & Thinking"
  social_and_emotional       → "Social, Emotional & Behavior"

Pure module: no genex_core imports, no I/O. Seizures / medical red flags carry NO
focus keyword here, so they never become a developmental focus (they remain safety
context via genex_core.build_safety_profile, untouched).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# Parent-facing labels.
FOCUS_LABELS: Dict[str, str] = {
    "language_and_communication": "Speech & Communication",
    "movement_and_physical": "Fine & Gross Motor & Daily Skills",
    "cognitive": "Learning, Attention & Thinking",
    "social_and_emotional": "Social, Emotional & Behavior",
}

# Presentation order for all_focus_areas (parent-facing).
FOCUS_ORDER: List[str] = [
    "language_and_communication",
    "movement_and_physical",
    "cognitive",
    "social_and_emotional",
]

# Confirmed primary-focus priority for multiple detected concerns:
#   1) Speech  2) Learning/Attention  3) Motor/Daily  4) Social/Emotional
FOCUS_PRIORITY: List[str] = [
    "language_and_communication",
    "cognitive",
    "movement_and_physical",
    "social_and_emotional",
]

# API-layer keyword patterns per focus area. Covers common parent phrasings,
# singular/plural, and natural wording. Matched case-insensitively on
# "{diagnosis} | {concern}".
FOCUS_KEYWORDS: Dict[str, List[str]] = {
    "language_and_communication": [
        r"speech delay", r"speech regression", r"speech", r"not talking", r"\btalk",
        r"language delay", r"\blanguage\b", r"communicat", r"\bwords?\b", r"verbal",
        r"non[\s-]?verbal", r"babbl", r"echolalia", r"articulat", r"stutter",
        r"vocab", r"naming", r"expressive", r"receptive",
    ],
    "movement_and_physical": [
        r"not walking", r"\bwalk", r"unstead", r"not running", r"\brun(ning|s)?\b",
        r"gross motor", r"fine motor", r"\bmotor\b", r"coordination", r"\bbalance\b",
        r"clumsy", r"crawl", r"\bjump", r"\bhop", r"daily skills", r"self[\s-]?help",
        r"self[\s-]?care", r"\bdress", r"feeding", r"\bphysical\b", r"\bpt\b",
        r"\bot\b", r"grasp", r"low muscle tone", r"hypotonia", r"\bstairs\b",
    ],
    "cognitive": [
        r"learning difficult\w*", r"learning delay", r"\blearning\b", r"lack of attention",
        r"attention problem", r"\battention\b", r"\bfocus\b", r"concentrat", r"hyperactiv",
        r"\badhd\b", r"distract", r"\bmemory\b", r"problem solving", r"cognit", r"thinking",
        r"\bletters?\b", r"\bnumbers?\b", r"colou?rs?", r"count(ing|s)?",
        r"follow(s|ing)?\s+(simple\s+)?directions", r"concepts?",
    ],
    "social_and_emotional": [
        r"socially afraid", r"socially anxious", r"social anxiety",
        r"afraid (of|around)\s*(other\s*)?(child|kid|people)", r"shy with", r"\bshy\b",
        r"\bsocial\b", r"\bpeer", r"friends?", r"tantrum", r"meltdown", r"emotional",
        r"regulat", r"behavio", r"aggress", r"anxious", r"anxiety", r"withdrawn",
        r"eye contact", r"play(s|ing)?\s+(with|alongside)", r"sharing", r"turn[\s-]?taking",
    ],
}


def detect_focus_areas(diagnosis: str, concern: str) -> List[str]:
    """Return the focus keys whose keywords appear in the concern/diagnosis text,
    in FOCUS_ORDER (presentation order)."""
    text = f"{diagnosis or ''} | {concern or ''}".lower()
    return [
        key for key in FOCUS_ORDER
        if any(re.search(pat, text) for pat in FOCUS_KEYWORDS[key])
    ]


def primary_from_detected(detected: List[str]) -> str:
    """Pick the primary focus from detected areas using the priority order.
    Returns "" if nothing was detected (caller decides the fallback)."""
    for key in FOCUS_PRIORITY:
        if key in detected:
            return key
    return ""


def build_focus_block(primary_key: str, detected: List[str]) -> Dict[str, Any]:
    """Build the Beta 2.2 focus metadata from a chosen primary + detected areas.

    - recommended_focus_area_keys: detected areas (excluding primary), priority order.
    - remaining_focus_areas: all areas except primary; recommended ones first (priority
      order), then the rest (presentation order). EVERY remaining area is listed
      (available), each flagged `recommended`.
    """
    detected_set = set(detected)
    recommended = [k for k in FOCUS_PRIORITY if k in detected_set and k != primary_key]
    non_detected = [k for k in FOCUS_ORDER if k != primary_key and k not in detected_set]
    remaining_order = recommended + non_detected

    return {
        "primary_focus_key": primary_key,
        "primary_focus_label": FOCUS_LABELS.get(primary_key, primary_key),
        "all_focus_areas": [{"key": k, "label": FOCUS_LABELS[k]} for k in FOCUS_ORDER],
        "recommended_focus_area_keys": recommended,
        "added_focus_areas": [],
        "remaining_focus_areas": [
            {"key": k, "label": FOCUS_LABELS[k], "recommended": k in recommended}
            for k in remaining_order
        ],
    }


def earliest_focus_in_text(text: str) -> str:
    """Return the focus whose keyword appears EARLIEST in `text`, or "" if none.

    This implements the product rule: the primary focus is the FIRST addressable
    developmental concern the parent writes. If two focus areas match at the same
    earliest position (a true tie / ambiguous order), the fixed FOCUS_PRIORITY
    order breaks it. Medical/safety-only terms (e.g. seizures) carry no focus
    keyword, so they are naturally skipped here.
    """
    t = (text or "").lower()
    positions: Dict[str, int] = {}
    for key in FOCUS_ORDER:
        best = None
        for pat in FOCUS_KEYWORDS[key]:
            m = re.search(pat, t)
            if m is not None:
                best = m.start() if best is None else min(best, m.start())
        if best is not None:
            positions[key] = best
    if not positions:
        return ""
    min_pos = min(positions.values())
    tied = [k for k in FOCUS_ORDER if positions.get(k) == min_pos]
    if len(tied) == 1:
        return tied[0]
    return next(k for k in FOCUS_PRIORITY if k in tied)  # tie-break by priority


def select_focus(diagnosis: str, concern: str) -> Tuple[str, List[str]]:
    """Return (primary_key, detected_keys).

    Primary = the EARLIEST addressable developmental focus mentioned in the parent's
    CONCERN text. Fixed FOCUS_PRIORITY is used ONLY as a fallback — when there is a
    positional tie, or the concern text names no focus (then detection across
    diagnosis+concern is used with priority order). primary_key is "" only when
    nothing is detected anywhere — the caller falls back to the genex_core pick.
    """
    detected = detect_focus_areas(diagnosis, concern)
    primary = earliest_focus_in_text(concern)
    if not primary:
        # No focus named in the concern text → fall back to detection (incl.
        # diagnosis) ordered by the fixed priority.
        primary = primary_from_detected(detected)
    return primary, detected
