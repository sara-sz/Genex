"""PARENT-0.3B REPAIR PASS 2 — founder decisions.

Two founder decisions land here:

1. `_DOMAIN_WHY` copy for Fine Motor / Gross Motor / Daily Living. The exact
   approved strings are pinned below, so any future edit is a deliberate,
   visible test change.

2. Focus-slot diversity. Splitting legacy Movement into Fine + Gross created a
   crowd-out the four-domain Brain could not produce: two motor siblings could
   consume both focus slots and silently drop an explicit speech concern. The
   rule gives slot 2 to the highest-ranked explicit concern OUTSIDE the
   fine/gross pair when — and only when — both slots would otherwise go to that
   pair.

   It is selection diversity ONLY. It does not merge Fine and Gross, does not
   alter the seven-domain taxonomy, does not touch any score, and never selects
   a third domain. Daily Living is NOT a member of the sibling pair.

All fixtures are fictional. No real data.
"""

from __future__ import annotations

import pytest

from genex_core.activity_engine import _DOMAIN_WHY, _SUBDOMAIN_WHY
from genex_core.interview_engine import (
    MOTOR_SIBLING_DOMAINS,
    choose_focus_domains,
    ensure_concern_profile,
    init_state_from_profile,
    rank_focus_domains,
)
from parent_taxonomy.domains import BY_KEY

# ---------------------------------------------------------------------------
# Founder-approved parent-facing copy — EXACT strings
# ---------------------------------------------------------------------------

FOUNDER_WHY_COPY = {
    "fine_motor": (
        "Small hand and finger movements build coordination and control for everyday "
        "skills like picking up small objects, drawing and pre-writing, using utensils, "
        "and fastening clothes."
    ),
    "gross_motor": (
        "Big-body movement builds strength, balance, and coordination for everyday "
        "movement and play, like running, climbing stairs, jumping, and moving safely "
        "through the environment."
    ),
    "daily_living": (
        "Everyday routines build independence and confidence with skills like dressing, "
        "eating, cleaning up, and helping with simple tasks at home."
    ),
}


@pytest.mark.parametrize("domain,copy", sorted(FOUNDER_WHY_COPY.items()))
def test_founder_why_copy_is_exact(domain, copy):
    assert _DOMAIN_WHY[domain] == copy


def test_why_copy_covers_six_of_seven_domains():
    assert set(_DOMAIN_WHY) == set(BY_KEY) - {"sensory"}


def test_sensory_copy_not_authored():
    """Sensory stays content-pending. No parent-facing copy may be invented."""
    assert "sensory" not in _DOMAIN_WHY


def test_subdomain_copy_still_outranks_domain_copy():
    """Precedence order must be unchanged: subdomain first, then domain."""
    import inspect

    from genex_core import activity_engine

    source = inspect.getsource(activity_engine)
    subdomain_at = source.index("_SUBDOMAIN_WHY.get(subdomain)")
    domain_at = source.index("_DOMAIN_WHY.get(", subdomain_at)
    assert subdomain_at < domain_at, "domain copy must not precede subdomain copy"


def test_existing_subdomain_copy_untouched():
    assert "gross_motor_mobility_and_coordination" in _SUBDOMAIN_WHY
    assert "emotional_regulation" in _SUBDOMAIN_WHY


# ---------------------------------------------------------------------------
# Focus-slot diversity
# ---------------------------------------------------------------------------

def _state(concern: str, months: int = 48, diagnosis: str = ""):
    state = init_state_from_profile("your child", months, diagnosis, concern, 10)
    ensure_concern_profile(state)
    return state


def _explicit(state):
    """The existing deterministic explicit-concern signal: concern_signal >= 0.10."""
    return [
        (r["category_key"], r["concern_signal"])
        for r in rank_focus_domains(state)
        if r["concern_signal"] >= 0.10
    ]


CHAO = "speech delay, OT delay, PT delay, not yet jumping, wobbly run, social is good"
FINE_GROSS_ONLY = "OT delay, PT delay, not yet jumping, wobbly run, trouble with grasp"
GROSS_DAILY = "PT delay, not yet jumping, wobbly run, cannot dress himself, trouble with self care"
FINE_DAILY_SPEECH = (
    "OT delay, trouble with grasp, cannot dress himself, self care delay, speech delay"
)


def test_sibling_group_is_fine_and_gross_only():
    assert MOTOR_SIBLING_DOMAINS == frozenset({"fine_motor", "gross_motor"})
    assert "daily_living" not in MOTOR_SIBLING_DOMAINS
    assert MOTOR_SIBLING_DOMAINS <= set(BY_KEY)


# --- Case A: speech + Fine Motor + Gross Motor -----------------------------

def test_case_a_explicit_speech_is_not_crowded_out():
    state = _state(CHAO)
    selected = choose_focus_domains(state)
    assert "talking_and_communicating" in selected, selected
    assert len(selected) == 2


def test_case_a_primary_is_still_top_ranked():
    state = _state(CHAO)
    ranked = rank_focus_domains(state)
    assert choose_focus_domains(state)[0] == ranked[0]["category_key"] == "gross_motor"


def test_case_a_displaced_sibling_is_recorded_not_lost():
    state = _state(CHAO)
    choose_focus_domains(state)
    noted = [n["domain"] for n in state.get("noted_concerns", [])]
    assert "fine_motor" in noted, noted


def test_case_a_fine_and_gross_do_not_take_both_slots():
    selected = choose_focus_domains(_state(CHAO))
    assert not set(selected) == MOTOR_SIBLING_DOMAINS


# --- Case B: Fine Motor + Gross Motor only ---------------------------------

def test_case_b_fine_and_gross_may_take_both_slots():
    """Rule 4: with no explicit outside concern, the pair is legitimate."""
    state = _state(FINE_GROSS_ONLY)
    explicit = dict(_explicit(state))
    assert not (set(explicit) - MOTOR_SIBLING_DOMAINS), explicit
    assert set(choose_focus_domains(state)) == MOTOR_SIBLING_DOMAINS


# --- Case C: Gross Motor + Daily Living ------------------------------------

def test_case_c_daily_living_may_take_second_slot():
    state = _state(GROSS_DAILY)
    selected = choose_focus_domains(state)
    assert selected[0] == "gross_motor"
    assert "daily_living" in selected, selected


# --- Case D: Fine Motor + Daily Living + speech ----------------------------

def test_case_d_no_artificial_grouping_affects_daily_living():
    """Daily Living is not a sibling, so the rule must not displace it."""
    state = _state(FINE_DAILY_SPEECH)
    selected = choose_focus_domains(state)
    assert selected[0] == "fine_motor"
    assert selected[1] == "daily_living", selected


# --- Case E: primary ordering unchanged ------------------------------------

@pytest.mark.parametrize("concern", [CHAO, FINE_GROSS_ONLY, GROSS_DAILY, FINE_DAILY_SPEECH])
def test_case_e_primary_always_equals_top_ranked_domain(concern):
    state = _state(concern)
    assert choose_focus_domains(state)[0] == rank_focus_domains(state)[0]["category_key"]


# --- Invariants the rule must not break ------------------------------------

@pytest.mark.parametrize("concern", [CHAO, FINE_GROSS_ONLY, GROSS_DAILY, FINE_DAILY_SPEECH])
def test_never_selects_a_third_domain(concern):
    assert len(choose_focus_domains(_state(concern))) <= 2


@pytest.mark.parametrize("concern", [CHAO, FINE_GROSS_ONLY, GROSS_DAILY, FINE_DAILY_SPEECH])
def test_selected_domains_are_canonical_and_distinct(concern):
    selected = choose_focus_domains(_state(concern))
    assert len(set(selected)) == len(selected)
    assert all(d in BY_KEY for d in selected)


@pytest.mark.parametrize("concern", [CHAO, FINE_GROSS_ONLY, GROSS_DAILY, FINE_DAILY_SPEECH])
def test_score_arithmetic_is_untouched(concern):
    """The rule reorders SELECTION only — it must not perturb any score.

    Ranking is recomputed from the same state after selection; every
    triage_score, concern_signal and delay_signal must be identical.
    """
    state = _state(concern)
    before = {
        r["category_key"]: (r["triage_score"], r["concern_signal"], r["delay_signal"])
        for r in rank_focus_domains(state)
    }
    choose_focus_domains(state)
    after = {
        r["category_key"]: (r["triage_score"], r["concern_signal"], r["delay_signal"])
        for r in rank_focus_domains(state)
    }
    assert before == after


def test_selection_only_ever_draws_from_explicit_concerns():
    """The rescued slot-2 domain must itself be an explicit concern."""
    state = _state(CHAO)
    explicit = {d for d, _ in _explicit(state)}
    assert set(choose_focus_domains(state)) <= explicit


def test_rule_is_inert_without_two_explicit_concerns():
    """A single-concern profile must be unaffected by the diversity rule."""
    state = _state("speech delay, not talking much", months=36)
    selected = choose_focus_domains(state)
    assert selected[0] == rank_focus_domains(state)[0]["category_key"]


def test_fine_and_gross_remain_separate_domains():
    """Diversity grouping must never collapse the taxonomy."""
    from genex_core.config import DOMAIN_CONFIG

    assert "fine_motor" in DOMAIN_CONFIG
    assert "gross_motor" in DOMAIN_CONFIG
    assert DOMAIN_CONFIG["fine_motor"] is not DOMAIN_CONFIG["gross_motor"]
    assert len(DOMAIN_CONFIG) == 7
