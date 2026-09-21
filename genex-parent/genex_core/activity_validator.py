"""
genex_core/activity_validator.py
---------------------------------
V22 hard-block activity validators.

Runs before any activity card is shown to a parent.
Returns (is_valid: bool, warnings: list[str]).

Hard-block rules (from V22 spec):
  - title missing
  - instructions missing
  - materials malformed
  - placeholder/generic wording
  - debug suffixes in titles
  - activity_family mismatch (wrong domain)
  - unsafe harder version
  - language goal using gross motor activity
  - time_words family missing time/routine language
  - book_page_turning missing book/page mechanics
  - fork_spoon_use missing utensils/feeding
  - dressing_on/off missing clothing
"""

from __future__ import annotations

import re
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from parent_taxonomy.domains import LEGACY_AMBIGUOUS, resolve_legacy_domain


# ---------------------------------------------------------------------------
# Family → allowed domains (hard guardrail)
# ---------------------------------------------------------------------------
#
# Parent 2.4: the hard-coded `FAMILY_TO_CATEGORY` dict is RETIRED. It pinned each
# activity family to exactly ONE legacy domain, which was wrong twice over:
#
#   1. It could not express families that legitimately serve more than one
#      domain — `buttoning_fasteners` is Daily Living *and* Fine Motor;
#      `conversation_turn_taking` is Talking & Communicating *and* Social &
#      Emotional. 27 of 56 families carry a secondary domain.
#   2. Its values were legacy spellings. Once the Brain became seven-domain
#      native, 45 of 56 families raised a CRITICAL
#      `activity_family_category_mismatch` and blocked every card using them.
#
# The founder-reviewed workbook is now the single source of truth and the
# allowed set is DERIVED there ({primary} ∪ secondaries) — see
# parent_taxonomy/activity_families.py. No replacement dict lives here; a second
# copy is exactly what drifted.
#
# The check below is therefore a MEMBERSHIP test, not an equality test.


def _allowed_domains_for_family(fam: str) -> Optional[FrozenSet[str]]:
    """Canonical domains `fam` may serve, or None when the family is unknown.

    Imported lazily: the workbook read is deferred until a card is actually
    validated, so importing this module stays cheap.
    """
    from parent_taxonomy.activity_families import allowed_domains

    return allowed_domains(fam)


# Derived from the legacy vocabulary rather than restated, so these rules cannot
# drift from parent_taxonomy.
#
# Rules 5 and 6 below were still keyed on legacy spellings after PARENT-0.3B.
# Left that way they were silent-failure bugs: rule 5 (a CRITICAL check) would
# never fire again, and rule 6 would fire on every card including genuine motor
# ones, since no category_key equals "movement_and_physical" any more.
_TALKING_DOMAIN = resolve_legacy_domain("language_and_communication")

#: Domains for which motor content in the instructions is expected, not a smell.
#: Legacy `movement_and_physical` split into exactly these three.
_MOTOR_DOMAINS: FrozenSet[str] = frozenset(LEGACY_AMBIGUOUS["movement_and_physical"])

# Gross motor patterns that should NOT appear in non-movement activity instructions
_MOTOR_GAME_PATTERN = re.compile(
    r"\b(floor sticker|jump|hop|toss|throw|catch|race|obstacle course|climb|balance beam|trampoline"
    r"|laundry basket toss|basket ball|basketball)\b",
    re.I,
)

# Placeholder/generic wording patterns — any match in ANY parent-facing field blocks the card
_PLACEHOLDER_PATTERNS = [
    re.compile(r"set up one simple playful turn", re.I),
    re.compile(r"materials that match the bridge step", re.I),
    re.compile(r"\[insert\b", re.I),
    re.compile(r"\bTODO\b"),
    re.compile(r"<activity_family>", re.I),
    re.compile(r"\bplaceholder\b", re.I),
    # Generic fallback template phrases — never show to parents
    re.compile(r"choose a short activity using", re.I),
    re.compile(r"\bany calm attempt at\b", re.I),
    re.compile(r"only if easy and enjoyable:?\s+add one small step", re.I),
    re.compile(r"with another child: one person models, one supports", re.I),
    re.compile(r"use one item, model first, shorten the turn", re.I),
    # Pass-8 rewrite phrases that are still too generic
    re.compile(r"set up a quick\b", re.I),
    re.compile(r"show your child one small step", re.I),
    re.compile(r"\bgoal:\s", re.I),
    re.compile(r"items for .{1,40} from around the home", re.I),
    re.compile(r"your child tries at least once:", re.I),
    re.compile(r"break it into one single step", re.I),
    re.compile(r"add one more step or reduce your help", re.I),
    re.compile(r"with a sibling or friend, take turns.{0,30}each person tries one step", re.I),
    re.compile(r"celebrate any attempt and stop after 2", re.I),
    # Bucket-theme titles that leaked into instructions/success
    re.compile(r"\broutine activity\b", re.I),
    re.compile(r"snack counting activity", re.I),
    re.compile(r"action picture activity", re.I),
]

# Generic materials field — exact phrase signals template fallback
_GENERIC_MATERIALS_PHRASE = re.compile(r"^\s*simple household items\s*$", re.I)

# Title double-suffix bug — "Game Game", "Activity Activity", etc.
_TITLE_DOUBLE_SUFFIX_PATTERN = re.compile(
    r"\b(game|activity|time|session)\s+\1\b", re.I
)

# Completely generic title patterns
_GENERIC_TITLE_PATTERN = re.compile(
    r"^home play game$", re.I
)

# Debug suffix patterns in titles
_DEBUG_SUFFIX_PATTERN = re.compile(
    r"(v\d+|debug|_b1|_core|_easier_backup|_harder_stretch|bridge_step)\b", re.I
)

# Safety: "harder" version hard-avoids
_UNSAFE_HARDER_PATTERNS = [
    re.compile(r"\bjump from (height|table|chair|bed|furniture)\b", re.I),
    re.compile(r"\brun (fast|race|sprint)\b", re.I),
    re.compile(r"\bclimb (high|ladder|tree)\b", re.I),
    re.compile(r"\bno supervision\b", re.I),
]


def _full_text(activity: Dict[str, Any]) -> str:
    """Concatenate all text fields for pattern matching."""
    fields = ["title", "theme", "instructions", "materials", "cdc_goal",
              "bridge_step", "what_to_avoid", "make_harder", "make_easier",
              "success_criteria", "success", "group_play_line", "group_play"]
    return " ".join(str(activity.get(f, "") or "") for f in fields).lower()


def validate_activity(
    activity: Dict[str, Any],
    category_key: str,
) -> Tuple[bool, List[str]]:
    """Run all V22 validators.  Returns (is_valid, warnings).

    is_valid=False means the activity must be blocked or regenerated.
    """
    warnings: List[str] = []
    text = _full_text(activity)

    # 1. Required fields
    title = str(activity.get("title", "") or "").strip()
    instructions = str(activity.get("instructions", "") or "").strip()
    materials = str(activity.get("materials", "") or "").strip()

    if not title:
        warnings.append("missing_title")
    if not instructions:
        warnings.append("missing_instructions")
    if not materials:
        warnings.append("missing_materials")

    if not title or not instructions:
        return False, warnings

    # 2. Placeholder/generic wording (checked across ALL parent-facing fields)
    for pat in _PLACEHOLDER_PATTERNS:
        if pat.search(text):
            warnings.append(f"placeholder_wording:{pat.pattern[:40]}")

    # 2b. Generic materials field
    if _GENERIC_MATERIALS_PHRASE.match(materials):
        warnings.append("placeholder_wording:generic_materials_simple_household_items")

    # 3. Debug suffixes in title
    if _DEBUG_SUFFIX_PATTERN.search(title):
        warnings.append(f"debug_suffix_in_title:{title}")

    # 3b. Title double-suffix bug (e.g. "Bead Game Game", "Activity Activity")
    if _TITLE_DOUBLE_SUFFIX_PATTERN.search(title):
        warnings.append(f"title_double_suffix:{title}")

    # 3c. Completely generic titles
    if _GENERIC_TITLE_PATTERN.match(title):
        warnings.append(f"title_generic:{title}")

    # 4. activity_family / domain mismatch
    #
    # Membership, not equality: a family may legitimately serve several domains.
    # An UNKNOWN family yields None and is PERMISSIVE — the same behaviour as the
    # retired dict's .get() returning None. This taxonomy covers the 56 families
    # the old dict knew, not every family the Gold Standard or activity_engine
    # can emit, so blocking unknowns would be a new and much wider rule.
    fam = str(activity.get("activity_family", "") or "").strip().lower()
    allowed = _allowed_domains_for_family(fam)
    if allowed is not None and category_key not in allowed:
        warnings.append(
            f"activity_family_category_mismatch:{fam}->"
            f"{'|'.join(sorted(allowed))} not {category_key}"
        )

    # 5. Language goal using gross motor activity
    if category_key == _TALKING_DOMAIN:
        motor_match = _MOTOR_GAME_PATTERN.search(
            str(activity.get("instructions", "") or "")
        )
        if motor_match:
            warnings.append(f"language_card_contains_motor_game:{motor_match.group()}")

    # 6. Non-movement card contains motor activity in main instructions
    if category_key not in _MOTOR_DOMAINS:
        motor_match = _MOTOR_GAME_PATTERN.search(
            str(activity.get("instructions", "") or "")
        )
        if motor_match:
            warnings.append(f"non_movement_card_contains_motor:{motor_match.group()}")

    # 7. Family-specific hard guardrails
    bucket = _family_bucket(fam, category_key)

    if bucket == "book_page":
        if not re.search(r"\b(book|page|turn|lift|separate)\b", text):
            warnings.append("book_page_family_missing_book_page_mechanics")

    if bucket == "fork_spoon":
        if not re.search(r"\b(fork|spoon|utensil|bite|scoop|stab|food|snack|plate|pretend food)\b", text):
            warnings.append("fork_spoon_family_missing_utensil_mechanics")

    if bucket in ("dressing_on", "dressing_off"):
        if not re.search(r"\b(cloth|dress|shirt|pants|jacket|coat|sock|sleeve|button|zip)\b", text):
            warnings.append("dressing_family_missing_clothing")

    if bucket == "time_words":
        if not re.search(r"\b(morning|night|now|later|first|then|today|tomorrow|routine|bedtime)\b", text):
            warnings.append("time_words_family_missing_time_routine_language")

    if bucket == "receptive_direction":
        if not re.search(r"\b(direction|give|put|show|point|find|cleanup|clean.up|body part|touch)\b", text):
            warnings.append("receptive_direction_family_missing_direction_action")

    if bucket == "action_label":
        if not re.search(r"\b(action|picture|doing|running|eating|playing|point|show|act)\b", text):
            warnings.append("action_label_family_missing_action_picture")

    # 8. Unsafe harder version
    harder = str(activity.get("make_harder", "") or "")
    for pat in _UNSAFE_HARDER_PATTERNS:
        if pat.search(harder):
            warnings.append(f"unsafe_harder_version:{pat.pattern[:40]}")

    # 9. Title/instruction body-part mismatch
    _BODY_PARTS = ["nose", "ear", "head", "belly", "toe", "knee", "elbow", "hand", "arm"]
    title_lower = title.lower()
    instructions_lower = instructions.lower()
    for part in _BODY_PARTS:
        if re.search(r'\b' + part + r'\b', title_lower):
            if not re.search(r'\b' + part + r'\b', instructions_lower):
                warnings.append(f"title_body_part_mismatch:{part}")

    # 10. Title says "touch" but instructions talk about giving/putting
    if re.search(r'\btouch\b', title_lower):
        if re.search(r'\b(give me|put it in)\b', instructions_lower):
            warnings.append("title_instruction_action_mismatch")

    # 11. Success criteria domain mismatch
    success_text = str(activity.get("success_criteria", "") or activity.get("success", "") or "").lower()
    if success_text:
        # Ball activity should not have foot/balance success criteria
        if re.search(r'\b(ball|rolling|toss|throw|catch)\b', text):
            if re.search(r'\b(lifts? one foot|stand on one|balances? on)\b', success_text):
                warnings.append("success_domain_mismatch:ball_activity_has_foot_balance_success")
        # Bead/threading activity should not have crayon/drawing success criteria
        if re.search(r'\b(bead|thread|peg|lace)\b', text):
            if re.search(r'\b(crayon|drawing|marks? on paper)\b', success_text):
                warnings.append("success_domain_mismatch:bead_activity_has_drawing_success")
        # Generic "tries at least once" or "any calm attempt" are always blocked
        if re.search(r"your child tries at least once:", success_text):
            warnings.append("placeholder_wording:generic_success_criteria")

    # Determine is_valid: block on critical warnings
    critical = {w for w in warnings if any(
        w.startswith(prefix) for prefix in [
            "missing_title", "missing_instructions",
            "placeholder_wording", "debug_suffix_in_title",
            "title_double_suffix", "title_generic",
            "activity_family_category_mismatch",
            "language_card_contains_motor_game",
            "book_page_family_missing_book_page_mechanics",
            "fork_spoon_family_missing_utensil_mechanics",
            "dressing_family_missing_clothing",
            "unsafe_harder_version",
            "success_domain_mismatch",
        ]
    )}

    return len(critical) == 0, warnings


def _family_bucket(fam: str, category_key: str = "") -> str:
    """Map activity_family string to a broad bucket for guardrail checks."""
    fam = fam.lower()
    patterns = [
        ("book_page", r"book_page|page_turn"),
        ("fork_spoon", r"fork|spoon|utensil|feeding"),
        ("dressing_on", r"dressing_on|clothes_on"),
        ("dressing_off", r"dressing_off|clothes_off"),
        ("buttoning", r"button|fastener|zipper"),
        ("beading", r"bead|thread|peg|pincer|grasp|prewriting|scribble"),
        ("catch_ball", r"catch_ball|ball"),
        ("jump_prep", r"jump|hop|squat|balance|safe_"),
        ("time_words", r"time_words|routine"),
        ("receptive_direction", r"receptive|direction|body_part|book_picture_receptive"),
        ("action_label", r"action_picture|action_label|action_words"),
    ]
    for bucket, pat in patterns:
        if re.search(pat, fam):
            return bucket
    return "general"


def filter_valid_activities(
    activities: List[Dict[str, Any]],
    category_key: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split activities into (valid, blocked).

    Blocked activities have validation_warnings populated and should not be
    shown to parents.  In admin/debug mode they can be inspected.
    """
    valid = []
    blocked = []
    for act in activities:
        is_valid, warnings = validate_activity(act, category_key)
        act = dict(act)
        act["validation_warnings"] = warnings
        if is_valid:
            valid.append(act)
        else:
            blocked.append(act)
    return valid, blocked
