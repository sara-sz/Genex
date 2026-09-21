"""PARENT-0.3B REPAIR PASS 1 — canonical runtime safety + legacy-key cleanup.

Covers the mechanically resolvable stale four-domain runtime logic that the
0.3B regression audit found. Each fix is a vocabulary or domain-scope repair;
none changes scoring mathematics, thresholds, weights, or clinical semantics.

Deliberately NOT covered here (founder review pending):
  * choose_focus_domains / max_domains focus-budget policy
  * _DOMAIN_WHY copy for Fine Motor / Gross Motor / Daily Living
  * the broad legacy vocabulary still in tests/test_regression.py

All fixtures are fictional. No real data.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from genex_core import safety as safety_mod
from genex_core.activity_engine import _DOMAIN_WHY, _SUBDOMAIN_WHY, _family_bucket
from genex_core.bridge_selector import _has_regression_concern
from genex_core.config import LANGUAGE_SCORING_TRACKS
from genex_core.final_plan_gate import SAFE_FILLER_CARDS
from genex_core.interview_engine import ensure_concern_profile, init_state_from_profile
from genex_core.safety import _FALL_RISK_DOMAINS, apply_safety_constraints_to_activities
from genex_core.scoring import compute_language_scoring_profile, get_effective_dev_age
from parent_taxonomy.activity_families import get_taxonomy
from parent_taxonomy.domains import BY_KEY, DOMAIN_KEYS

REPO = pathlib.Path(__file__).resolve().parent.parent
GENEX_CORE = REPO / "genex_core"

LEGACY_KEYS = {"language_and_communication", "cognitive", "movement_and_physical"}

MOTOR_DESCENDED = ("fine_motor", "gross_motor", "daily_living")


# ---------------------------------------------------------------------------
# fixtures — fictional profiles only
# ---------------------------------------------------------------------------

def _profile(diagnosis: str, concern: str, months: int = 40):
    state = init_state_from_profile("your child", months, diagnosis, concern, 10)
    ensure_concern_profile(state)
    return state


@pytest.fixture
def dravet():
    return _profile(
        "Dravet syndrome",
        "seizures, falls often, unsteady walking, low muscle tone",
    )


@pytest.fixture
def down_syndrome():
    return _profile(
        "Down syndrome",
        "low muscle tone, unsteady walking, falls often",
        months=24,
    )


@pytest.fixture
def no_fall_risk():
    """Fictional profile with NO fall / mobility / seizure risk."""
    return _profile("none", "not talking much, small vocabulary", months=36)


def _card(**over):
    card = {
        "title": "Frog Jump Game",
        "instructions": (
            "Have your child jump like a frog across the room, stomp on the "
            "stickers, then climb onto the low step."
        ),
        "materials": "floor stickers, a low step",
        "duration_min": 6,
    }
    card.update(over)
    return card


def _apply(state, category_key, card):
    return apply_safety_constraints_to_activities(state, category_key, [dict(card)])[0]


# ===========================================================================
# 1. safety.py — canonical motor safety
# ===========================================================================

def test_fall_risk_domains_are_canonical():
    assert _FALL_RISK_DOMAINS <= set(BY_KEY)
    assert not (_FALL_RISK_DOMAINS & LEGACY_KEYS)


def test_fall_risk_is_gross_motor_only():
    """Locomotor risk is Gross Motor.

    Fine Motor (table-top hand use) and Daily Living (self-help routines) also
    descend from the legacy movement bucket, but neither carries jump / hop /
    climb risk, so the rule must not widen to them.
    """
    assert _FALL_RISK_DOMAINS == frozenset({"gross_motor"})


@pytest.mark.parametrize("verb", ["jump", "stomp", "climb", "hop", "trampoline", "race"])
def test_locomotor_verbs_blocked_for_gross_motor_dravet(dravet, verb):
    card = _card(
        title=f"{verb.title()} Game",
        instructions=f"Encourage your child to {verb} across the room and back.",
    )
    out = _apply(dravet, "gross_motor", card)
    assert out["title"] != card["title"], f"{verb!r} not replaced for Dravet gross_motor"


@pytest.mark.parametrize("verb", ["jump", "stomp", "climb"])
def test_locomotor_verbs_blocked_for_gross_motor_down_syndrome(down_syndrome, verb):
    card = _card(
        title=f"{verb.title()} the Sticker",
        instructions=f"Ask your child to {verb} onto each sticker on the floor.",
    )
    out = _apply(down_syndrome, "gross_motor", card)
    assert out["title"] != card["title"], f"{verb!r} not replaced for DS gross_motor"


def test_replacement_card_is_actually_safe(dravet):
    """The substituted card must not itself contain locomotor language."""
    import re

    out = _apply(dravet, "gross_motor", _card())
    text = f"{out['title']} {out['instructions']}".lower()
    assert not re.search(r"\b(jump|hop|frog|climb|trampoline|race|stomp)\b", text), text


@pytest.mark.parametrize("category", ["fine_motor", "daily_living", "talking_and_communicating"])
def test_locomotor_rule_does_not_leak_to_other_domains(dravet, category):
    """Must not fire merely because a domain descended from legacy movement."""
    out = _apply(dravet, category, _card())
    assert out["title"] == "Frog Jump Game"


def test_safe_support_marker_applied_to_gross_motor(dravet):
    out = _apply(dravet, "gross_motor", _card(title="Ball Roll", instructions="Roll a ball."))
    assert "stable support" in out["instructions"].lower()
    assert "supervision throughout" in out["instructions"].lower()


@pytest.mark.parametrize("category", ["fine_motor", "daily_living"])
def test_safe_support_marker_not_applied_to_non_locomotor(dravet, category):
    out = _apply(dravet, category, _card(title="Ball Roll", instructions="Roll a ball."))
    assert "stable support" not in out["instructions"].lower()


def test_safety_rule_still_gated_on_risk(no_fall_risk):
    """A child with no fall/mobility/seizure risk must be unaffected."""
    out = _apply(no_fall_risk, "gross_motor", _card())
    assert out["title"] == "Frog Jump Game"
    assert "stable support" not in out["instructions"].lower()


def test_replacements_cycle_to_distinct_titles(dravet):
    """The cycling that prevents duplicate titles on the schedule must work."""
    cards = [_card() for _ in range(4)]
    out = apply_safety_constraints_to_activities(dravet, "gross_motor", cards)
    titles = [o["title"] for o in out]
    assert len(set(titles)) == len(titles), titles


def test_safety_module_has_no_legacy_domain_comparison():
    tree = ast.parse((GENEX_CORE / "safety.py").read_text())
    offenders = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and n.value in LEGACY_KEYS
    ]
    assert offenders == [], offenders


# ===========================================================================
# 2. scoring.py — canonical Talking & Communicating
# ===========================================================================

def _language_state(key: str):
    from genex_core.interview_engine import build_domain_questions

    state = _profile("none", "speech delay, not talking much", months=36)
    questions = build_domain_questions(state, "talking_and_communicating", max_questions_total=20)
    pattern = ["yes", "sometimes", "no"]
    state.setdefault("qna", {})[key] = [
        {**q, "norm_answer": pattern[i % 3], "scoring_norm_answer": pattern[i % 3]}
        for i, q in enumerate(questions)
    ]
    state.setdefault("dev_age", {})[key] = 24
    return state


def test_canonical_talking_receives_split_scoring():
    profile = compute_language_scoring_profile(_language_state("talking_and_communicating"))
    assert profile["raw_dev_age_months"] == 24
    assert profile["track_counts"], "split scoring did not run for the canonical key"
    assert set(profile["track_weights"]) == set(LANGUAGE_SCORING_TRACKS)


def test_legacy_key_no_longer_feeds_split_scoring():
    """The legacy spelling is not a second supported input."""
    profile = compute_language_scoring_profile(_language_state("language_and_communication"))
    assert profile["raw_dev_age_months"] is None


def test_effective_dev_age_uses_split_scoring_for_talking():
    state = _language_state("talking_and_communicating")
    assert get_effective_dev_age(state, "talking_and_communicating") is not None


def test_scoring_mathematics_unchanged():
    """Weights/tracks are config constants — the repair moved a KEY, not math.

    The numeric proof is the byte-identical profile: the same answers stored
    under the legacy key at HEAD and under the canonical key now produce the
    same track_counts, track_dev_ages and track_weights.
    """
    profile = compute_language_scoring_profile(_language_state("talking_and_communicating"))
    assert profile["track_weights"] == {
        "expressive_speech": 0.70,
        "receptive": 0.15,
        "gesture": 0.05,
    }
    assert profile["track_counts"] == {"expressive_speech": 10, "receptive": 2, "gesture": 1}
    assert profile["track_dev_ages"] == {"expressive_speech": 30, "receptive": 24, "gesture": 24}


def test_scoring_module_has_no_legacy_domain_literal():
    tree = ast.parse((GENEX_CORE / "scoring.py").read_text())
    offenders = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and n.value in LEGACY_KEYS
        and not isinstance(getattr(n, "parent", None), ast.Expr)
    ]
    # The only permitted occurrence is the resolve_legacy_domain() argument.
    assert offenders.count("language_and_communication") <= 1, offenders
    assert "cognitive" not in offenders
    assert "movement_and_physical" not in offenders


# ===========================================================================
# 3. bridge_selector.py — canonical regression detection
# ===========================================================================

@pytest.mark.parametrize("text", ["stopped talking", "lost words", "language loss", "speech loss"])
def test_language_specific_regression_detected_for_talking(text):
    state = _profile("none", f"my child {text} recently")
    assert _has_regression_concern(state, "talking_and_communicating") is True


def test_language_specific_phrase_not_matched_by_generic_branch():
    """Proves the language branch is actually taken, not the generic fallback.

    'lost words' matches ONLY the language pattern; the generic pattern needs
    'regress' / 'lost skill' / 'lost ability'. If the language branch were dead
    this would be False for talking and False elsewhere.
    """
    state = _profile("none", "my child lost words recently")
    assert _has_regression_concern(state, "talking_and_communicating") is True
    assert _has_regression_concern(state, "gross_motor") is False


def test_generic_regression_semantics_preserved():
    state = _profile("none", "my child lost ability to walk steadily")
    assert _has_regression_concern(state, "gross_motor") is True


def test_no_regression_concern_when_absent():
    state = _profile("none", "not talking much, small vocabulary")
    assert _has_regression_concern(state, "talking_and_communicating") is False


# ===========================================================================
# 4. activity_engine — seven-domain bucket fallback
# ===========================================================================

INERT_FAMILY = "zzz"   # matches none of the family patterns

EXPECTED_FALLBACK = {
    "talking_and_communicating": "expressive_word",   # legacy-preserved (1:1 rename)
    "social_and_emotional": "social_turn",            # legacy-preserved (key unchanged)
    "learning_and_thinking": "attention",             # legacy-preserved (1:1 rename)
    "fine_motor": "beading",                          # taxonomy majority 4/6
    "gross_motor": "jump_prep",                       # taxonomy majority 5/7
    "daily_living": "general",                        # 5-way tie -> not derivable
    "sensory": "general",                             # content-pending
}


def test_inert_probe_family_matches_no_pattern():
    assert _family_bucket(INERT_FAMILY, "") == "general", "probe is not inert"


@pytest.mark.parametrize("domain,expected", sorted(EXPECTED_FALLBACK.items()))
def test_domain_fallback_bucket(domain, expected):
    assert _family_bucket(INERT_FAMILY, domain) == expected


def test_every_canonical_domain_is_covered():
    assert set(EXPECTED_FALLBACK) == set(DOMAIN_KEYS)


def test_sensory_fallback_invents_nothing():
    assert _family_bucket(INERT_FAMILY, "sensory") == "general"


@pytest.mark.parametrize("domain", ["fine_motor", "gross_motor"])
def test_motor_fallback_matches_taxonomy_majority(domain):
    """The two derived entries must stay pinned to the taxonomy that justified them."""
    import collections

    taxonomy = get_taxonomy()
    families = [k for k, e in taxonomy.families.items() if e.primary_domain == domain]
    buckets = collections.Counter(
        b for b in (_family_bucket(f) for f in families) if b != "general"
    )
    winner, count = buckets.most_common(1)[0]
    assert count * 2 > len(families), f"{domain}: no strict majority ({buckets})"
    assert EXPECTED_FALLBACK[domain] == winner


def test_daily_living_has_no_majority_bucket():
    """Documents WHY daily_living falls through to 'general' rather than a guess."""
    import collections

    taxonomy = get_taxonomy()
    families = [k for k, e in taxonomy.families.items() if e.primary_domain == "daily_living"]
    buckets = collections.Counter(_family_bucket(f) for f in families)
    assert max(buckets.values()) == 1, f"a majority now exists: {buckets}"


# ===========================================================================
# 5. final_plan_gate — SAFE_FILLER_CARDS canonical domains
# ===========================================================================

def test_filler_card_count_unchanged():
    assert len(SAFE_FILLER_CARDS) == 11


def test_all_filler_cards_use_canonical_domains():
    for card in SAFE_FILLER_CARDS:
        assert card["category_key"] in BY_KEY, card["title"]
        assert card["category_key"] not in LEGACY_KEYS, card["title"]


def test_filler_card_domain_matches_taxonomy_primary():
    """Each card's domain is DERIVED from its activity_family, not from its title."""
    taxonomy = get_taxonomy()
    for card in SAFE_FILLER_CARDS:
        entry = taxonomy.get(card["activity_family"])
        assert entry is not None, f"{card['title']}: family not in taxonomy"
        assert card["category_key"] == entry.primary_domain, card["title"]


def test_filler_cards_retain_required_content():
    for card in SAFE_FILLER_CARDS:
        for field in ("title", "activity_family", "instructions", "materials", "duration_min"):
            assert card.get(field), f"{card.get('title')}: missing {field}"


def test_filler_cards_cover_the_domains_they_did_before():
    domains = {c["category_key"] for c in SAFE_FILLER_CARDS}
    assert "talking_and_communicating" in domains
    assert domains & set(MOTOR_DESCENDED), "no motor-descended filler remains"


# ===========================================================================
# 6. _DOMAIN_WHY — audited, partially migrated, rest pending founder copy
# ===========================================================================

LEGACY_WHY_VERBATIM = {
    "talking_and_communicating": (
        "Practising communication in small, playful moments builds the connection between "
        "hearing, understanding, and expressing — the foundation of language."
    ),
    "social_and_emotional": (
        "Small social moments teach your child how to connect, trust, and feel safe — "
        "building emotional skills one shared turn at a time."
    ),
    "learning_and_thinking": (
        "Play that involves thinking and exploring helps your child build attention, "
        "curiosity, and the ability to learn new things."
    ),
}


def test_domain_why_keys_are_canonical():
    assert set(_DOMAIN_WHY) <= set(BY_KEY)
    assert not (set(_DOMAIN_WHY) & LEGACY_KEYS)


def test_migrated_why_copy_is_verbatim_unchanged():
    """The three 1:1 renames reuse existing approved copy with no edits."""
    for domain, copy in LEGACY_WHY_VERBATIM.items():
        assert _DOMAIN_WHY[domain] == copy, domain


def test_motor_domains_now_have_founder_copy():
    """Repair Pass 2: the founder supplied copy for all three motor domains.

    Pass 1 asserted these were absent, deliberately, so that filling them
    required an explicit test change rather than passing silently. The exact
    strings are pinned in test_parent_24_repair_pass2.py.
    """
    for domain in MOTOR_DESCENDED:
        assert domain in _DOMAIN_WHY, domain
        assert _DOMAIN_WHY[domain].strip(), domain


def test_sensory_has_no_parent_copy():
    assert "sensory" not in _DOMAIN_WHY


def test_gross_motor_partially_covered_by_subdomain_copy():
    """Mitigation worth recording: subdomain copy outranks domain copy."""
    assert "gross_motor_mobility_and_coordination" in _SUBDOMAIN_WHY
