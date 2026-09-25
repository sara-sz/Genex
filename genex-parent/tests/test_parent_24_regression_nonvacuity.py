"""PARENT-0.3B — non-vacuity guard for the migrated regression suite.

`tests/test_regression.py` was written against the legacy four-domain
vocabulary. Under the seven-domain Brain a legacy domain key yields **zero**
questions and **zero** activities, so tests that fed one and then iterated over
the result asserted nothing at all and still reported success. Ten of the
nineteen "passing" tests were green for that reason.

Migrating the vocabulary fixed it, but a green suite is not by itself proof that
the fix held — a future edit could reintroduce a key that produces an empty
collection and the suite would go quiet again rather than red.

This module is that proof, kept separate so it cannot be satisfied by accident:

  * every canonical domain the regression suite drives must yield a non-empty
    question set and a non-empty activity bank;
  * every legacy key must yield nothing, which is what made the vacuity
    possible in the first place;
  * the regression suite must not reintroduce legacy domain keys.

All fixtures are fictional. No real data.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from genex_core.activity_engine import generate_category_activity_bank
from genex_core.interview_engine import (
    build_domain_questions,
    ensure_concern_profile,
    init_state_from_profile,
)
from genex_core.support_tiers import build_v22_plan_for_category
from parent_taxonomy.domains import CONTENT_READY_KEYS

REGRESSION_SUITE = pathlib.Path(__file__).resolve().parent / "test_regression.py"

LEGACY_KEYS = ("language_and_communication", "cognitive", "movement_and_physical")

# Profiles mirroring those the regression suite drives.
PROFILES = {
    "speech": ("none", "speech delay, not talking much", 36),
    "adhd": ("ADHD", "hyperactivity, trouble focusing, difficulty finishing tasks", 60),
    "dravet": ("Dravet syndrome", "seizures, falls often, unsteady walking, low muscle tone", 40),
    "down_syndrome": ("Down syndrome", "low muscle tone, unsteady walking, falls often", 24),
}


def _state(profile: str):
    diagnosis, concern, months = PROFILES[profile]
    state = init_state_from_profile("your child", months, diagnosis, concern, 10)
    ensure_concern_profile(state)
    return state


def _bank(state, category_key):
    plan = build_v22_plan_for_category(state, category_key)
    state.setdefault("bridge_plans", {})[category_key] = plan
    return generate_category_activity_bank(state, category_key).get("activities", [])


# ---------------------------------------------------------------------------
# canonical domains must produce real content
# ---------------------------------------------------------------------------

# A concern that actually targets each domain, so an empty result means a real
# gap rather than simply "this child has no delay there".
DOMAIN_CONCERN = {
    "talking_and_communicating": "speech delay, not talking much",
    "social_and_emotional": "social interaction difficulty, does not play with other children",
    "learning_and_thinking": "trouble focusing, difficulty finishing tasks",
    "fine_motor": "OT delay, trouble with grasp, cannot hold a crayon",
    "gross_motor": "PT delay, not yet jumping, wobbly run",
    "daily_living": "cannot dress himself, self care delay, trouble feeding himself",
}

# RESOLVED. Daily Living used to plan correctly but yield zero activities: its
# families fell through to the generic fallback template, whose wording trips
# the validator's generic_success_criteria rule, so every card was blocked. The
# bug predated Parent 2.4 — verified at tag parent-2.4-0.3a-hosted-ci, where
# dressing_off and spoon_use were already 100% blocked inside the legacy
# movement bank, masked by gross-motor families keeping that bank non-empty.
#
# Fixed by the Daily Living repair with CURATED CONTENT, not by relaxing the
# validator — see tests/test_parent_24_daily_living.py, which pins both that the
# skills now generate real cards and that the generic wording still fails.
#
# The set is deliberately kept (empty) rather than deleted: it is the mechanism
# that forced this entry to be revisited instead of quietly staying broken, and
# it is where a future content gap should be recorded.
KNOWN_EMPTY_ACTIVITY_DOMAINS: set = set()


def _concern_state(domain: str):
    state = init_state_from_profile("your child", 36, "none", DOMAIN_CONCERN[domain], 10)
    ensure_concern_profile(state)
    state["dev_age"][domain] = 24
    return state


@pytest.mark.parametrize("domain", sorted(CONTENT_READY_KEYS))
def test_canonical_domain_yields_questions(domain):
    questions = build_domain_questions(_state("speech"), domain, max_questions_total=20)
    assert questions, f"{domain} produced no questions — assertions over it would be vacuous"


@pytest.mark.parametrize(
    "domain", sorted(set(CONTENT_READY_KEYS) - KNOWN_EMPTY_ACTIVITY_DOMAINS)
)
def test_canonical_domain_yields_activities(domain):
    assert _bank(_concern_state(domain), domain), f"{domain} produced an empty activity bank"


@pytest.mark.parametrize("domain", sorted(KNOWN_EMPTY_ACTIVITY_DOMAINS))
def test_known_content_gap_still_reaches_the_planner(domain):
    """Daily Living must still PLAN, even though no card survives validation.

    Pinning both halves keeps the gap honest: if activities ever start flowing,
    this test fails and the debt entry gets removed deliberately.
    """
    state = _concern_state(domain)
    plan = build_v22_plan_for_category(state, domain)
    assert plan.get("active_bridge_steps"), f"{domain}: planner produced no bridge steps"
    state.setdefault("bridge_plans", {})[domain] = plan
    assert generate_category_activity_bank(state, domain).get("activities", []) == [], (
        f"{domain} now yields activities — remove it from KNOWN_EMPTY_ACTIVITY_DOMAINS"
    )


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_every_regression_profile_yields_activities(profile):
    """Each fictional profile the regression suite drives must produce cards."""
    state = _state(profile)
    from genex_core.interview_engine import choose_focus_domains

    focus = choose_focus_domains(state)
    assert focus, f"{profile}: no focus domains"
    for domain in focus:
        assert _bank(_state(profile), domain), f"{profile}/{domain}: empty bank"


def test_dravet_gross_motor_bank_is_non_empty():
    """The exact path that made test_case18 a false green."""
    assert _bank(_state("dravet"), "gross_motor")


def test_down_syndrome_gross_motor_bank_is_non_empty():
    assert _bank(_state("down_syndrome"), "gross_motor")


# ---------------------------------------------------------------------------
# the legacy keys are what made vacuity possible — prove they still yield nothing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("legacy", LEGACY_KEYS)
def test_legacy_key_yields_nothing(legacy):
    """Documents the trap. A legacy key is silently empty, never an error.

    This is precisely why a vocabulary slip cannot be caught by the suite going
    red — it goes QUIET instead. Hence the guards above.
    """
    state = _state("speech")
    assert build_domain_questions(state, legacy, max_questions_total=20) == []
    assert _bank(_state("speech"), legacy) == []


# ---------------------------------------------------------------------------
# the regression suite must not drift back to legacy vocabulary
# ---------------------------------------------------------------------------

def test_regression_suite_has_no_legacy_domain_string_literals():
    """Structural: quoted legacy keys only, so prose/comments do not false-positive."""
    source = REGRESSION_SUITE.read_text()
    offenders = []
    for legacy in LEGACY_KEYS:
        for quote in ('"', "'"):
            if f"{quote}{legacy}{quote}" in source:
                offenders.append(f"{quote}{legacy}{quote}")
    assert offenders == [], offenders


def test_regression_suite_still_exercises_safety_assertions():
    """Guard against the safety checks being deleted rather than migrated."""
    source = REGRESSION_SUITE.read_text()
    for marker in ("_RISKY_EXT", "unsafe movement", "_RISKY_MOVEMENT_PATTERNS"):
        assert marker in source, f"safety machinery {marker!r} missing from the suite"


def test_regression_suite_has_no_skips_or_xfails():
    """The suite must not have been made green by skipping."""
    source = REGRESSION_SUITE.read_text()
    assert not re.search(r"@pytest\.mark\.(skip|xfail)", source)
    assert "pytest.skip(" not in source
