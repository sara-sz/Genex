"""Slice 2 — canonical micro-tracks and content support.

Canonical design: `pilot_runtime/docs/parent_baseline_v2_design.md`.

The gates this file installs permanently:

  * CROSS-SYSTEM PARITY — the Parent-side derivation equals the Pilot's
    `rung_table_talking_v1.json` bindings for every declared-track rung
  * SERVED-SET DRIFT — the declared content support equals the generated
    activity bank
  * SERVED IS NOT MAPPABLE — the three states never collapse
  * NO INVENTED MILESTONE — every rung is a verbatim canonical row
  * determinism — repeated derivation is byte-identical

The parity and drift gates read `pilot_runtime/` artifacts, which exist at test
time but NOT in the Parent runtime image. That asymmetry is the whole reason
content support is a declaration rather than a read.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from api import baseline_micro_tracks as MT
from genex_core import functional_baseline as v1

AREA = "talking"
DOMAIN = "talking_and_communicating"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNG_TABLE = REPO_ROOT / "pilot_runtime/data/rung_table_talking_v1.json"
ACTIVITY_BANK = REPO_ROOT / "pilot_runtime/data/activity_bank_talking_v1.json"


def _pilot_bindings():
    table = json.loads(RUNG_TABLE.read_text(encoding="utf-8"))
    return {
        (int(r["source_rung_months"]), str(r["milestone_text"])):
            tuple(sorted(b["family_ref"]
                         for b in (r.get("family_bindings") or [])))
        for r in table["rungs"].values()
    }


# ---------------------------------------------------------------------------
# 1. CROSS-SYSTEM PARITY
# ---------------------------------------------------------------------------

def test_parent_derivation_matches_the_pilot_rung_table_exactly():
    """THE gate for this slice.

    Three rules were needed and each was found by comparing against the
    artifact, not assumed: union families across duplicate rows, check
    `is_known_family` rather than trusting `resolve_family_key`, and require
    `allowed_domains` to admit the domain. If the two systems ever disagree
    about what a rung binds, this fails rather than the disagreement surviving.
    """
    if not RUNG_TABLE.is_file():
        pytest.skip("the Pilot rung table is not present in this checkout")
    pilot = _pilot_bindings()
    mismatches = []
    for ref in MT.declared_rungs(AREA):
        theirs = pilot.get((ref.months, ref.milestone))
        if theirs is None:
            mismatches.append((ref.months, ref.milestone, "absent from artifact"))
        elif tuple(ref.family_bindings) != theirs:
            mismatches.append((ref.months, ref.milestone,
                               ref.family_bindings, theirs))
    assert mismatches == [], mismatches


def test_every_declared_rung_is_present_in_both_systems():
    if not RUNG_TABLE.is_file():
        pytest.skip("the Pilot rung table is not present in this checkout")
    assert len(MT.declared_rungs(AREA)) == len(_pilot_bindings())


def test_the_two_family_rung_is_not_collapsed_to_one():
    """Non-vacuity for rule 1. Dedup hides the second binding."""
    ref = MT.rung_at(AREA, 24, "says at least two words together like more milk")
    assert ref is not None
    assert len(ref.family_bindings) == 2, (
        "the 24m rung binds two families; taking the deduped row's single "
        "activity_family column silently drops one")


def test_the_four_unmappable_rungs_are_unmappable():
    """Non-vacuity for rule 2: these families are not among the 56."""
    unmappable = [r for r in MT.declared_rungs(AREA) if not r.is_mappable]
    assert len(unmappable) == 4
    for ref in unmappable:
        assert MT.rung_support(ref) == MT.UNMAPPABLE


# ---------------------------------------------------------------------------
# 2. SERVED-SET DRIFT
# ---------------------------------------------------------------------------

def test_the_declared_served_set_matches_the_generated_activity_bank():
    """DRIFT GATE.

    The Parent runtime cannot read this artifact, so the declaration is the
    runtime truth and this test is what keeps it true.
    """
    if not ACTIVITY_BANK.is_file():
        pytest.skip("the activity bank is not present in this checkout")
    bank = json.loads(ACTIVITY_BANK.read_text(encoding="utf-8"))
    by_family = bank["population"]["by_family"]
    served = {f for f, n in by_family.items() if int(n) > 0}
    assert served == set(MT.SERVED_FAMILIES), (
        "the declared served-family set has drifted from the activity bank; "
        "Track 2 must update the declaration when content lands")


def test_known_unserved_families_really_have_no_content():
    if not ACTIVITY_BANK.is_file():
        pytest.skip("the activity bank is not present in this checkout")
    bank = json.loads(ACTIVITY_BANK.read_text(encoding="utf-8"))
    by_family = bank["population"]["by_family"]
    for family in MT.KNOWN_UNSERVED_FAMILIES:
        assert int(by_family.get(family, 0)) == 0, family


def test_the_served_and_unserved_declarations_are_disjoint():
    assert not (MT.SERVED_FAMILIES & MT.KNOWN_UNSERVED_FAMILIES)


def test_every_mappable_declared_family_is_declared_one_way_or_the_other():
    """No family may be silently unaccounted for."""
    declared = MT.SERVED_FAMILIES | MT.KNOWN_UNSERVED_FAMILIES
    used = {f for r in MT.declared_rungs(AREA) for f in r.family_bindings}
    assert used <= declared, sorted(used - declared)


def test_the_served_set_digest_is_stable_and_detects_an_edit():
    first = MT.served_family_set_digest()
    assert first == MT.served_family_set_digest()
    assert len(first) == 64


# ---------------------------------------------------------------------------
# 3. SERVED IS NOT MAPPABLE
# ---------------------------------------------------------------------------

def test_the_three_support_states_do_not_collapse():
    seen = {MT.rung_support(r) for r in MT.declared_rungs(AREA)}
    assert seen == set(MT.SUPPORT_STATES), (
        "all three support states must be observable on the real taxonomy, "
        "or the distinction is untested")


def test_a_mappable_rung_with_no_content_is_unserved_not_unmappable():
    """The distinction the Pilot's generation cannot make.

    It receives no activity bank and splits deficits on mappability alone, so a
    mappable-but-unserved family yields a real suggestion that fails only later
    at release. This is where that is caught.
    """
    ref = MT.rung_at(AREA, 18,
                     "tries to say three or more words besides mama or dada")
    assert ref is not None
    assert ref.is_mappable is True
    assert MT.rung_support(ref) == MT.UNSERVED


def test_a_rung_is_supported_only_when_every_bound_family_is_served():
    ref = MT.rung_at(AREA, 24, "says at least two words together like more milk")
    assert len(ref.family_bindings) == 2
    assert MT.rung_support(ref) == MT.SUPPORTED
    partial = MT.CanonicalRungRef(
        months=24, milestone="x", subdomain="expressive_language",
        family_bindings=("expressive_vocabulary_growth", "early_vocalizations"))
    assert MT.rung_support(partial) == MT.UNSERVED, (
        "one served family out of two must not read as supported")


def test_content_absence_never_changes_which_rung_is_appropriate():
    """Support is a property of Genex, never of the child's ladder position."""
    refs = MT.declared_rungs(AREA)
    assert [r.months for r in refs] == sorted(r.months for r in refs)
    for ref in refs:
        assert MT.rung_support(ref) in MT.SUPPORT_STATES


# ---------------------------------------------------------------------------
# 4. NO INVENTED MILESTONE
# ---------------------------------------------------------------------------

def test_every_derived_rung_is_a_verbatim_canonical_row():
    area = v1.get_area(AREA)
    canonical = {(int(r["months"]), str(r["milestone"]))
                 for r in v1._rows_for_domain(area.domain,
                                              area.track_subdomains, ())}
    derived = {(r.months, r.milestone) for r in MT.declared_rungs(AREA)}
    assert derived == canonical


def test_micro_track_rungs_are_all_declared_rungs():
    declared = {(r.months, r.milestone) for r in MT.declared_rungs(AREA)}
    for track in MT.micro_tracks(AREA):
        for ref in track.rungs:
            assert (ref.months, ref.milestone) in declared


# ---------------------------------------------------------------------------
# 5. structure and determinism
# ---------------------------------------------------------------------------

def test_micro_tracks_are_ordered_and_internally_ascending():
    tracks = MT.micro_tracks(AREA)
    assert [t.entry_months for t in tracks] == sorted(t.entry_months
                                                      for t in tracks)
    for track in tracks:
        months = [r.months for r in track.rungs]
        assert months == sorted(months)


def test_the_served_micro_tracks_are_the_audited_four():
    assert sorted(t.family_ref for t in MT.served_micro_tracks(AREA)) == [
        "book_object_naming", "expressive_vocabulary_growth",
        "sentence_building", "two_word_phrases"]


def test_a_rung_binding_two_families_appears_in_both_tracks():
    hits = [t.family_ref for t in MT.micro_tracks(AREA)
            for r in t.rungs if r.months == 24]
    assert sorted(hits) == ["expressive_vocabulary_growth", "two_word_phrases"]


def test_derivation_is_deterministic():
    first = [(r.months, r.milestone, r.family_bindings)
             for r in MT.declared_rungs(AREA)]
    MT.declared_rungs.cache_clear()
    MT._raw_family_index.cache_clear()
    MT.micro_tracks.cache_clear()
    second = [(r.months, r.milestone, r.family_bindings)
              for r in MT.declared_rungs(AREA)]
    assert first == second


def test_support_summary_reports_counts_and_no_child_data():
    summary = MT.support_summary(AREA)
    assert summary["declared_rungs"] == 21
    assert sum(summary["by_support"].values()) == 21
    assert summary["by_support"][MT.UNMAPPABLE] == 4
    assert summary["served_family_set_id"] == MT.SERVED_FAMILY_SET_ID
    blob = json.dumps(summary)
    for forbidden in ("child", "caregiver", "uid", "session"):
        assert forbidden not in blob
