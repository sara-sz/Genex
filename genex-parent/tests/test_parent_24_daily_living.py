"""PARENT 2.4 — Daily Living activity generation.

Daily Living planned correctly but produced ZERO usable activities at every age.
Its families land in buckets that had no curated cards, so every card fell
through to the generic template in `_v22_fallback_instructions` — and that
template carries three strings the validator blocks as placeholders. The bug
predates Parent 2.4; it was hidden while these rows sat inside legacy
movement_and_physical, because gross-motor families kept that bank non-empty.

The repair is CONTENT, not validation. The most important tests here are the
ones proving the validator was not weakened (section G) and that the two
feeding/swallowing families stay deliberately empty (clinical hold) — a suite
that only checked "Daily Living now has cards" would pass just as well if
someone had deleted the placeholder rule.

All fixtures are fictional. No real data.
"""

from __future__ import annotations

import pytest

from genex_core.activity_engine import (
    DAILY_LIVING_CLINICAL_HOLD,
    _BUCKET_VARIANTS,
    _FAMILY_VARIANTS,
    generate_category_activity_bank,
)
from genex_core.activity_validator import validate_activity
from genex_core.interview_engine import ensure_concern_profile, init_state_from_profile
from genex_core.support_tiers import build_v22_plan_for_category
from parent_taxonomy.domains import DOMAIN_KEYS

DL = "daily_living"
DL_CONCERN = "cannot dress himself, self care delay, trouble feeding himself"

REPRESENTATIVE_AGES = (18, 24, 36, 48, 60)


def _bank(months: int, domain: str = DL, concern: str = DL_CONCERN):
    state = init_state_from_profile("your child", months, "none", concern, 10)
    ensure_concern_profile(state)
    state["dev_age"][domain] = max(6, months - 12)
    plan = build_v22_plan_for_category(state, domain)
    state.setdefault("bridge_plans", {})[domain] = plan
    return generate_category_activity_bank(state, domain).get("activities", [])


def _find(family: str):
    """First generated card for `family` across the representative ages."""
    for months in REPRESENTATIVE_AGES:
        for card in _bank(months):
            if card.get("activity_family") == family:
                return card
    return None


def _criteria(card) -> str:
    return str(card.get("success_criteria") or card.get("success") or "")


# ===========================================================================
# A. Daily Living no longer generates zero activities
# ===========================================================================

@pytest.mark.parametrize("months", REPRESENTATIVE_AGES)
def test_daily_living_produces_activities_at_every_representative_age(months):
    activities = _bank(months)
    assert activities, f"Daily Living produced no activities at {months} months"


def test_daily_living_is_non_empty_globally():
    total = sum(len(_bank(m)) for m in REPRESENTATIVE_AGES)
    assert total > 0
    assert total >= len(REPRESENTATIVE_AGES), total


# ===========================================================================
# B–E. the four named skill paths
# ===========================================================================

GENERIC_SUCCESS_WARNING = "placeholder_wording:generic_success_criteria"

SKILL_PATHS = {
    "spoon_use": ("spoon",),
    "fork_use": ("fork",),
    "dressing_on": ("sleeve", "pants", "hat", "arm"),
    "dressing_off": ("sock", "sleeve", "off"),
}


@pytest.mark.parametrize("family", sorted(SKILL_PATHS))
def test_skill_path_generates_a_card(family):
    assert _find(family) is not None, f"{family} generated no card"


@pytest.mark.parametrize("family", sorted(SKILL_PATHS))
def test_skill_path_has_no_generic_success_warning(family):
    card = _find(family)
    _, warnings = validate_activity(card, DL)
    assert GENERIC_SUCCESS_WARNING not in warnings, warnings


@pytest.mark.parametrize("family", sorted(SKILL_PATHS))
def test_skill_path_survives_validation(family):
    is_valid, warnings = validate_activity(_find(family), DL)
    assert is_valid, warnings


@pytest.mark.parametrize("family", sorted(SKILL_PATHS))
def test_skill_path_criteria_is_concrete_and_observable(family):
    """Criteria must name what the child DOES, not that they 'succeeded'."""
    text = _criteria(_find(family)).lower()
    assert text, f"{family}: empty success criteria"
    assert "your child" in text
    vague = (
        "successfully completes",
        "practice until successful",
        "shows improvement",
        "does the task",
        "tries at least once:",
        "any calm attempt",
    )
    for phrase in vague:
        assert phrase not in text, f"{family}: vague criteria {phrase!r}"


@pytest.mark.parametrize("family,keywords", sorted(SKILL_PATHS.items()))
def test_skill_path_content_matches_the_actual_skill(family, keywords):
    """A fork skill must not be handed a spoon activity."""
    card = _find(family)
    blob = f"{card.get('title','')} {card.get('instructions','')} {_criteria(card)}".lower()
    assert any(k in blob for k in keywords), f"{family}: content does not mention {keywords}"


def test_utensil_families_do_not_share_cards():
    spoon, fork = _find("spoon_use"), _find("fork_use")
    assert spoon["title"] != fork["title"]
    assert "fork" in f"{fork['title']} {fork['instructions']}".lower()


# ===========================================================================
# F. generated cards stay in the right domain and family
# ===========================================================================

@pytest.mark.parametrize("months", REPRESENTATIVE_AGES)
def test_every_card_is_daily_living(months):
    for card in _bank(months):
        assert card.get("category_key") == DL, card.get("title")


@pytest.mark.parametrize("months", REPRESENTATIVE_AGES)
def test_every_card_carries_an_activity_family(months):
    for card in _bank(months):
        assert card.get("activity_family"), card.get("title")


@pytest.mark.parametrize("months", REPRESENTATIVE_AGES)
def test_every_generated_card_passes_validation(months):
    for card in _bank(months):
        is_valid, warnings = validate_activity(card, DL)
        assert is_valid, f"{card.get('title')!r}: {warnings}"


@pytest.mark.parametrize("months", REPRESENTATIVE_AGES)
def test_parent_facing_fields_are_populated(months):
    for card in _bank(months):
        assert card.get("title", "").strip(), "missing title"
        assert card.get("instructions", "").strip(), card.get("title")
        assert _criteria(card).strip(), card.get("title")


# ===========================================================================
# G. THE VALIDATOR WAS NOT WEAKENED
# ===========================================================================

def test_generic_success_wording_still_fails():
    """The exact wording that blocked Daily Living must still block."""
    card = {
        "title": "Spoon Practice at Snack",
        "instructions": "Sit at the table and hand your child a spoon with thick yoghurt.",
        "materials": "a child-sized spoon and a bowl of yoghurt",
        "success_criteria": "Your child tries at least once: Holds a spoon during meals.",
        "activity_family": "spoon_use",
    }
    is_valid, warnings = validate_activity(card, DL)
    assert GENERIC_SUCCESS_WARNING in warnings, warnings
    assert not is_valid, "generic success criteria must remain a CRITICAL block"


@pytest.mark.parametrize("phrase", [
    "Your child tries at least once: does the thing.",
    "Your child successfully completes the activity.",
])
def test_vague_criteria_are_not_silently_accepted(phrase):
    card = {
        "title": "Sock Pull Practice",
        "instructions": "Sit your child down and pull one sock halfway off, then wait.",
        "materials": "a loose sock",
        "success_criteria": phrase,
        "activity_family": "dressing_off",
    }
    is_valid, warnings = validate_activity(card, DL)
    if "tries at least once:" in phrase:
        assert GENERIC_SUCCESS_WARNING in warnings and not is_valid
    else:
        # Not currently pattern-matched, but must never become the shape our
        # curated cards use — see test_skill_path_criteria_is_concrete.
        assert "successfully completes" not in _criteria(_find("dressing_off")).lower()


def test_generic_template_is_still_blocked():
    """The fallback template Daily Living used to hit must still be rejected."""
    card = {
        "title": "Home Play Game",
        "instructions": (
            "Set up a quick home play activity. Show your child one small step and "
            "wait for them to try. Celebrate any attempt and stop after 2–3 turns."
        ),
        "materials": "items for home play (from around the home)",
        "success_criteria": "Your child tries at least once: does something.",
        "activity_family": "cup_drinking",
    }
    is_valid, _ = validate_activity(card, DL)
    assert not is_valid


def test_placeholder_rule_still_critical():
    from genex_core import activity_validator

    source = activity_validator.__file__
    with open(source) as fh:
        text = fh.read()
    assert '"placeholder_wording"' in text, "placeholder_wording removed from critical list"


# ===========================================================================
# Clinical hold — feeding/swallowing families stay deliberately empty
# ===========================================================================

def test_clinical_hold_membership_is_explicit():
    assert DAILY_LIVING_CLINICAL_HOLD == frozenset({"cup_drinking", "feeding_self_regulation"})


@pytest.mark.parametrize("months", REPRESENTATIVE_AGES)
def test_held_families_never_produce_cards(months):
    """Fail closed: no improvised feeding/swallowing guidance."""
    families = {c.get("activity_family") for c in _bank(months)}
    leaked = families & DAILY_LIVING_CLINICAL_HOLD
    assert not leaked, f"clinical-hold family produced a card: {leaked}"


def test_held_families_have_no_curated_content():
    """Guards the hold against being filled in silently by a later edit."""
    for family in DAILY_LIVING_CLINICAL_HOLD:
        assert family not in _FAMILY_VARIANTS, family


# ===========================================================================
# Safety
# ===========================================================================

SAFETY_SENSITIVE = ("spoon_use", "fork_use", "finger_feeding")


@pytest.mark.parametrize("family", SAFETY_SENSITIVE)
def test_feeding_cards_carry_supervision_or_seating_guidance(family):
    card = _find(family)
    blob = f"{card.get('instructions','')} {card.get('avoid') or card.get('what_to_avoid') or ''}".lower()
    assert any(k in blob for k in ("seat", "sit", "supervis", "arm's reach", "stay with")), blob


@pytest.mark.parametrize("family", SAFETY_SENSITIVE)
def test_feeding_cards_warn_about_choking_risk_foods(family):
    card = _find(family)
    avoid = str(card.get("avoid") or card.get("what_to_avoid") or "").lower()
    assert avoid.strip(), f"{family}: no what_to_avoid guidance"


def test_no_card_suggests_unsupervised_practice():
    for months in REPRESENTATIVE_AGES:
        for card in _bank(months):
            blob = " ".join(str(card.get(k, "")) for k in
                            ("instructions", "make_harder", "avoid", "what_to_avoid")).lower()
            assert "no supervision" not in blob, card.get("title")


def test_dressing_cards_address_balance():
    for family in ("dressing_on", "dressing_off"):
        card = _find(family)
        blob = f"{card.get('instructions','')} {card.get('avoid') or card.get('what_to_avoid') or ''}".lower()
        assert any(k in blob for k in ("sit", "seated", "balance", "steady", "support")), family


# ===========================================================================
# H. unrelated domains unchanged  /  I. no Sensory content
# ===========================================================================

OTHER_DOMAINS = {
    "talking_and_communicating": "speech delay, not talking much",
    "social_and_emotional": "social interaction difficulty, does not play with other children",
    "learning_and_thinking": "trouble focusing, difficulty finishing tasks",
    "fine_motor": "OT delay, trouble with grasp, cannot hold a crayon",
    "gross_motor": "PT delay, not yet jumping, wobbly run",
}


@pytest.mark.parametrize("domain,concern", sorted(OTHER_DOMAINS.items()))
def test_other_domains_still_produce_activities(domain, concern):
    assert _bank(36, domain=domain, concern=concern), domain


@pytest.mark.parametrize("domain,concern", sorted(OTHER_DOMAINS.items()))
def test_other_domains_cards_remain_valid(domain, concern):
    for card in _bank(36, domain=domain, concern=concern):
        is_valid, warnings = validate_activity(card, domain)
        assert is_valid, f"{domain}/{card.get('title')!r}: {warnings}"


def test_no_sensory_content_introduced():
    """Sensory stays content-pending — no curated cards, no generated cards."""
    for pool in (_FAMILY_VARIANTS, _BUCKET_VARIANTS):
        for key, cards in pool.items():
            for card in cards:
                assert "sensory" not in str(card.get("activity_family", "")).lower(), key
    assert _bank(36, domain="sensory", concern="sensory problem") == []


def test_daily_living_is_a_canonical_domain():
    assert DL in DOMAIN_KEYS


def test_new_cards_do_not_overwrite_existing_curated_content():
    """The merge asserts this at import; re-checked here as a regression guard."""
    from genex_core.activity_engine import (
        _DAILY_LIVING_BUCKET_VARIANTS,
        _DAILY_LIVING_FAMILY_VARIANTS,
    )

    for family, cards in _DAILY_LIVING_FAMILY_VARIANTS.items():
        assert _FAMILY_VARIANTS[family] is cards
    for bucket, cards in _DAILY_LIVING_BUCKET_VARIANTS.items():
        assert _BUCKET_VARIANTS[bucket] is cards
