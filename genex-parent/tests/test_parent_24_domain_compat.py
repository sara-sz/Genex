"""PARENT-0.3C — legacy domain read-compatibility tests.

Covers `api/domain_compat.py`: the one-directional read projection from stored
Beta 2.3 four-domain keys to canonical Parent 2.4 domains.

The property that matters most is the one in section E: legacy
`movement_and_physical` without evidence must stay UNRESOLVED. It spanned Fine
Motor, Gross Motor and Daily Living, so any single answer would silently
re-classify a child's history. A test suite that only checked the happy
mappings would pass while the system guessed.

All fixtures are fictional. No real data, no PHI.
"""

from __future__ import annotations

import copy

import pytest

from api.domain_compat import (
    MOTOR_SUCCESSORS,
    SOURCE_CANONICAL,
    SOURCE_CONTRADICTORY,
    SOURCE_FAMILY,
    SOURCE_LEGACY_RENAME,
    SOURCE_SUBDOMAIN,
    SOURCE_UNRESOLVED,
    DomainProjection,
    UnknownStoredDomain,
    assert_canonical_write,
    canonical_view,
    is_canonical,
    project_domain,
    project_record,
)
from parent_taxonomy.domains import BY_KEY, DOMAIN_KEYS


# ===========================================================================
# A. legacy language read
# ===========================================================================

def test_legacy_language_projects_to_talking():
    p = project_domain("language_and_communication")
    assert p.domain == "talking_and_communicating"
    assert p.resolved and p.source == SOURCE_LEGACY_RENAME


# ===========================================================================
# B. legacy cognitive read
# ===========================================================================

def test_legacy_cognitive_projects_to_learning():
    p = project_domain("cognitive")
    assert p.domain == "learning_and_thinking"
    assert p.resolved and p.source == SOURCE_LEGACY_RENAME


# ===========================================================================
# C. unchanged social read
# ===========================================================================

def test_social_is_unchanged_and_reads_as_canonical():
    """Spelled identically in both vocabularies — must not be treated as legacy."""
    p = project_domain("social_and_emotional")
    assert p.domain == "social_and_emotional"
    assert p.source == SOURCE_CANONICAL, "social is canonical, not a legacy rename"


# ===========================================================================
# D. legacy movement WITH deterministic evidence
# ===========================================================================

@pytest.mark.parametrize("subdomain,expected", [
    ("fine_motor_hand_use", "fine_motor"),
    ("gross_motor_mobility_and_coordination", "gross_motor"),
    ("postural_control_and_transitions", "gross_motor"),
    ("self_help_motor_skills", "daily_living"),
    ("adaptive_feeding_cues", "daily_living"),
    ("safety_awareness", "daily_living"),
])
def test_legacy_movement_resolved_by_subdomain(subdomain, expected):
    p = project_domain("movement_and_physical", subdomain=subdomain)
    assert p.domain == expected
    assert p.source == SOURCE_SUBDOMAIN
    assert subdomain in p.detail


@pytest.mark.parametrize("family,expected", [
    ("beading_threading", "fine_motor"),
    ("pincer_grasp", "fine_motor"),
    ("hop_prep", "gross_motor"),
    ("fork_spoon_use", "daily_living"),
])
def test_legacy_movement_resolved_by_activity_family(family, expected):
    p = project_domain("movement_and_physical", activity_family=family)
    assert p.domain == expected
    assert p.source == SOURCE_FAMILY


def test_subdomain_evidence_outranks_family_evidence():
    """Subdomain comes from the Gold Standard and drove the 0.2 migration."""
    p = project_domain(
        "movement_and_physical",
        subdomain="fine_motor_hand_use",
        activity_family="fork_spoon_use",   # would say daily_living
    )
    assert p.domain == "fine_motor"
    assert p.source == SOURCE_SUBDOMAIN


def test_every_resolution_lands_in_a_motor_successor():
    for subdomain in (
        "fine_motor_hand_use",
        "gross_motor_mobility_and_coordination",
        "postural_control_and_transitions",
        "self_help_motor_skills",
        "adaptive_feeding_cues",
        "safety_awareness",
    ):
        assert project_domain("movement_and_physical", subdomain=subdomain).domain \
            in MOTOR_SUCCESSORS


def test_motor_successors_are_derived_not_restated():
    assert MOTOR_SUCCESSORS == frozenset({"fine_motor", "gross_motor", "daily_living"})
    assert MOTOR_SUCCESSORS <= set(BY_KEY)


# ===========================================================================
# E. legacy movement WITHOUT evidence — must NOT guess
# ===========================================================================

def test_legacy_movement_without_evidence_is_unresolved():
    p = project_domain("movement_and_physical")
    assert p.domain is None
    assert not p.resolved
    assert p.source == SOURCE_UNRESOLVED
    assert p.is_unresolved_legacy


@pytest.mark.parametrize("bad", ["", "   ", "not_a_subdomain"])
def test_unusable_subdomain_evidence_does_not_resolve(bad):
    p = project_domain("movement_and_physical", subdomain=bad)
    assert p.domain is None, f"guessed from unusable subdomain {bad!r}"


def test_unknown_activity_family_does_not_resolve():
    p = project_domain("movement_and_physical", activity_family="no_such_family")
    assert p.domain is None


def test_unresolved_never_returns_a_motor_domain():
    """The failure mode this whole module exists to prevent."""
    p = project_domain("movement_and_physical")
    assert p.domain not in MOTOR_SUCCESSORS
    assert p.domain not in set(DOMAIN_KEYS)


def test_contradictory_evidence_is_reported_not_silently_preferred():
    """Evidence pointing outside the motor successors is a data conflict."""
    p = project_domain("movement_and_physical", subdomain="expressive_language")
    assert p.domain is None
    assert p.source == SOURCE_CONTRADICTORY
    assert "not a successor" in p.detail


def test_unknown_stored_domain_fails_closed():
    for bad in ("banana", "Movement And Physical", "sensory_processing"):
        with pytest.raises(UnknownStoredDomain):
            project_domain(bad)


def test_empty_stored_domain_fails_closed():
    with pytest.raises(UnknownStoredDomain):
        project_domain("")


# ===========================================================================
# F. new canonical writes round-trip unchanged
# ===========================================================================

@pytest.mark.parametrize("domain", sorted(DOMAIN_KEYS))
def test_canonical_write_round_trips(domain):
    p = project_domain(domain)
    assert p.domain == domain
    assert p.source == SOURCE_CANONICAL
    # Round-trip: projecting a projection is a no-op.
    assert project_domain(p.domain).domain == domain


def test_sensory_round_trips_without_content_inference():
    """Sensory is canonical and content-pending; projection must not alter it."""
    p = project_domain("sensory")
    assert p.domain == "sensory" and p.source == SOURCE_CANONICAL


@pytest.mark.parametrize("legacy", [
    "language_and_communication", "cognitive", "movement_and_physical",
])
def test_write_guard_rejects_legacy_keys(legacy):
    with pytest.raises(UnknownStoredDomain):
        assert_canonical_write(legacy, context="test")


@pytest.mark.parametrize("domain", sorted(DOMAIN_KEYS))
def test_write_guard_accepts_every_canonical_domain(domain):
    assert assert_canonical_write(domain) == domain


def test_is_canonical_helper():
    assert is_canonical("gross_motor")
    assert not is_canonical("movement_and_physical")
    assert not is_canonical("")


# ===========================================================================
# G. the historical source record is never mutated
# ===========================================================================

def test_project_record_does_not_mutate_source():
    record = {
        "domain": "movement_and_physical",
        "subdomain": "fine_motor_hand_use",
        "title": "Thread the Beads",
    }
    before = copy.deepcopy(record)
    projection = project_record(record)
    assert projection.domain == "fine_motor"
    assert record == before, "read projection mutated the historical record"


def test_canonical_view_returns_a_copy():
    record = {"domain": "cognitive", "title": "Sorting Game"}
    before = copy.deepcopy(record)
    view = canonical_view(record)
    assert view["domain"] == "learning_and_thinking"
    assert record == before, "canonical_view mutated its input"
    assert view is not record


def test_canonical_view_flags_unresolved_instead_of_guessing():
    record = {"domain": "movement_and_physical", "title": "Old Card"}
    view = canonical_view(record)
    assert view["domain"] == "movement_and_physical", "stored value was overwritten"
    assert view["domain_unresolved_legacy"] is True


def test_canonical_view_clears_stale_unresolved_flag():
    record = {
        "domain": "movement_and_physical",
        "subdomain": "gross_motor_mobility_and_coordination",
        "domain_unresolved_legacy": True,
    }
    view = canonical_view(record)
    assert view["domain"] == "gross_motor"
    assert "domain_unresolved_legacy" not in view


def test_projection_is_frozen():
    p = project_domain("cognitive")
    assert isinstance(p, DomainProjection)
    with pytest.raises(Exception):
        p.domain = "sensory"


def test_projection_is_deterministic():
    record = {"domain": "movement_and_physical", "activity_family": "pincer_grasp"}
    results = {project_record(record).domain for _ in range(5)}
    assert results == {"fine_motor"}


# ===========================================================================
# H. intake boundary — the legacy four-area umbrella resolves canonically
# ===========================================================================

from api.domain_compat import (  # noqa: E402  (grouped with its own section)
    UnresolvableFocus,
    canonical_display,
    resolve_focus_for_brain,
)
from genex_core.interview_engine import (  # noqa: E402
    ensure_concern_profile,
    init_state_from_profile,
)


def _brain_state(concern: str, months: int = 24, diagnosis: str = "Other"):
    state = init_state_from_profile("your child", months, diagnosis, concern, 10)
    ensure_concern_profile(state)
    return state


@pytest.mark.parametrize("concern,expected", [
    ("not walking yet, motor delay", "gross_motor"),
    ("trouble with grasp, cannot hold crayon", "fine_motor"),
    ("cannot dress himself, self care delay", "daily_living"),
])
def test_movement_umbrella_resolves_via_approved_ranking(concern, expected):
    """Resolved by the already-approved 0.3B ranking — no new keyword taxonomy."""
    state = _brain_state(concern)
    assert resolve_focus_for_brain("movement_and_physical", state) == expected


def test_umbrella_resolution_never_returns_the_umbrella():
    for concern in ("not walking yet", "cannot hold a crayon", "cannot dress himself"):
        got = resolve_focus_for_brain("movement_and_physical", _brain_state(concern))
        assert got != "movement_and_physical"
        assert got in MOTOR_SUCCESSORS


@pytest.mark.parametrize("legacy,expected", [
    ("language_and_communication", "talking_and_communicating"),
    ("cognitive", "learning_and_thinking"),
    ("social_and_emotional", "social_and_emotional"),
])
def test_non_motor_focus_keys_resolve_without_consulting_state(legacy, expected):
    assert resolve_focus_for_brain(legacy, {}) == expected


@pytest.mark.parametrize("domain", sorted(DOMAIN_KEYS))
def test_canonical_focus_key_passes_through(domain):
    assert resolve_focus_for_brain(domain, {}) == domain


def test_umbrella_without_any_motor_signal_refuses_to_guess():
    """No successor carries signal → raise, rather than pick the first."""
    state = _brain_state("not talking much, very few words", months=36)
    with pytest.raises(UnresolvableFocus):
        resolve_focus_for_brain("movement_and_physical", state)


def test_unknown_focus_key_raises():
    with pytest.raises(UnresolvableFocus):
        resolve_focus_for_brain("not_a_focus", {})


# ===========================================================================
# I. new Parent 2.4 writes are canonical end-to-end
# ===========================================================================

@pytest.mark.parametrize("concern,expected", [
    ("speech delay", "talking_and_communicating"),
    ("not walking yet, motor delay", "gross_motor"),
    ("trouble with grasp, cannot hold crayon", "fine_motor"),
    ("cannot dress himself, self care delay", "daily_living"),
])
def test_pipeline_persists_canonical_selected_domain(concern, expected):
    """The umbrella must never be persisted; new state holds the resolved domain."""
    from api.pipeline import run_session_start

    brain_state, _interview = run_session_start(
        age_in_months=24, diagnosis_for_brain="Other",
        sanitized_concern=concern, daily_time_minutes=10,
    )
    selected = brain_state["selected_domain_keys"]
    assert selected == [expected], selected
    assert "movement_and_physical" not in selected
    assert all(is_canonical(d) for d in selected)


def test_pipeline_keeps_the_four_area_public_surface():
    """0.3C compatibility: the public focus surface is unchanged."""
    from api.pipeline import run_session_start

    brain_state, _ = run_session_start(
        age_in_months=24, diagnosis_for_brain="Other",
        sanitized_concern="not walking yet, motor delay", daily_time_minutes=10,
    )
    focus = brain_state["focus"]
    assert len(focus["all_focus_areas"]) == 4, "public focus surface changed shape"
    assert focus["primary_focus_key"] == "movement_and_physical"
    # ...while the canonical resolution is recorded additively for consumers.
    assert focus["primary_focus_domain"] == "gross_motor"


def test_earliest_mentioned_concern_behaviour_preserved():
    """The umbrella is CONVERTED, not re-chosen: speech first still wins."""
    from api.pipeline import run_session_start

    brain_state, _ = run_session_start(
        age_in_months=36, diagnosis_for_brain="Other",
        sanitized_concern="speech delay, and also not walking yet", daily_time_minutes=10,
    )
    assert brain_state["focus"]["primary_focus_key"] == "language_and_communication"
    assert brain_state["selected_domain_keys"] == ["talking_and_communicating"]


# ===========================================================================
# J. focus_origin / focus_label — the canonical-vs-legacy comparison bug
# ===========================================================================

def _plan(domain: str):
    return {"week": [{"day": "Monday", "activities": [
        {"domain": domain, "domain_label": "", "title": "Card"}
    ]}]}


def test_primary_card_is_labelled_primary_not_added():
    """Regression: canonical card domain vs legacy focus key always mismatched."""
    from api.adapters import apply_integrated_provenance

    out = apply_integrated_provenance(
        _plan("talking_and_communicating"), "language_and_communication"
    )
    card = out["week"][0]["activities"][0]
    assert card["focus_origin"] == "primary", card
    assert card["focus_label"] == "Talking & Communicating", card


def test_added_focus_still_labelled_added():
    """Negative control — the fix must not label everything primary."""
    from api.adapters import apply_integrated_provenance

    out = apply_integrated_provenance(
        _plan("gross_motor"), "language_and_communication"
    )
    assert out["week"][0]["activities"][0]["focus_origin"] == "added"


@pytest.mark.parametrize("domain", sorted(DOMAIN_KEYS))
def test_focus_label_populated_for_every_canonical_domain(domain):
    from api.adapters import apply_integrated_provenance

    out = apply_integrated_provenance(_plan(domain), domain)
    card = out["week"][0]["activities"][0]
    assert card["focus_label"], f"{domain} produced a blank focus_label"
    assert card["focus_label"] == canonical_display(domain)
    assert card["focus_origin"] == "primary"


def test_historical_legacy_card_still_matches_its_legacy_primary():
    """Read compatibility: an old card + old focus key must still pair up."""
    from api.adapters import apply_integrated_provenance

    out = apply_integrated_provenance(
        _plan("cognitive"), "cognitive"
    )
    card = out["week"][0]["activities"][0]
    assert card["focus_origin"] == "primary"
    assert card["focus_label"] == canonical_display("learning_and_thinking")


def test_unresolvable_legacy_card_is_not_called_primary():
    """Safe direction: only call a card primary when it provably is."""
    from api.adapters import apply_integrated_provenance

    out = apply_integrated_provenance(
        _plan("movement_and_physical"), "movement_and_physical"
    )
    assert out["week"][0]["activities"][0]["focus_origin"] == "added"


def test_domain_labels_cover_canonical_and_legacy():
    from api.adapters import DOMAIN_LABELS

    for domain in DOMAIN_KEYS:
        assert DOMAIN_LABELS.get(domain), f"no label for canonical {domain}"
    for legacy in ("language_and_communication", "cognitive", "movement_and_physical"):
        assert DOMAIN_LABELS.get(legacy), f"historical label lost for {legacy}"
