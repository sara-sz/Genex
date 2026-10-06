"""0.5F-B — the SLP rung population counts, pinned by PREDICATE.

Three different "mappable" counts over the same 40 Talking & Communicating
rungs have been reported during this work, and the confusion was never
arithmetic. It was that the predicate went unnamed:

    PREDICATE 1  "every activity family resolves in the taxonomy AND permits
                  this domain"                               40 = 27 + 13
    PREDICATE 2  "the frozen 0.5E-B adapter yields a usable CanonicalRung"
                                                             40 = 17 + 23
    POPULATION   "the rung is on the DECLARED Parent SLP track"
                                                             21 = 17 + 4

All three are true at once. Predicate 2 is stricter than predicate 1 because
`RungTrackUndeclaredError` refuses an OFF-TRACK rung even when its families
resolve perfectly — so the 23 refusals are 13 family failures plus 10
track-undeclared ones.

The record is corrected here: an earlier report called "17 + 23 = 40" a
denominator mix. It is not. It is a correct statement of predicate 2 over all
40 rungs. What was wrong was reporting a count without saying which predicate
produced it, which let it read as the declared-track figure (21 = 17 + 4).

Every number below is COMPUTED from the frozen workbook and taxonomy, never
restated, so none of them can drift from the artifacts they describe.
"""

from __future__ import annotations

import pytest

from pilot_backend.domain.canonical_rung import normalize_milestone_text
from pilot_runtime.integration.parent_gold_standard_source import (
    ParentGoldStandardSource,
)

DOMAIN = "talking_and_communicating"

#: The DECLARED Parent SLP functional-baseline track, from
#: `BaselineArea.track_subdomains`. Only these two of the six Talking
#: subdomains are asked about and routed on by the functional baseline.
DECLARED_TRACK_SUBDOMAINS = frozenset({
    "expressive_language", "early_vocalization_and_babbling"})

ALL_TALKING_RUNGS = 40
FAMILY_RESOLVABLE = 27
FAMILY_UNRESOLVABLE = 13
ADAPTER_USABLE = 17
ADAPTER_REFUSED = 23
REFUSED_FOR_FAMILIES = 13
REFUSED_FOR_UNDECLARED_TRACK = 10
DECLARED_TRACK_RUNGS = 21
DECLARED_TRACK_MAPPABLE = 17
DECLARED_TRACK_UNRESOLVED = 4
OFF_TRACK_RUNGS = 19


@pytest.fixture(scope="module")
def world():
    source = ParentGoldStandardSource()
    source._load()
    milestones, activity_families, _baseline = source._import_parent()

    rungs = {}
    for record in milestones.get_cdc_df().to_dict(orient="records"):
        domain = str(record.get("category_key", "") or "").strip()
        milestone = str(record.get("milestone", "") or "").strip()
        if domain != DOMAIN or not milestone:
            continue
        try:
            months = int(record.get("months"))
        except (TypeError, ValueError):
            continue
        key = (months, normalize_milestone_text(milestone))
        entry = rungs.setdefault(key, {"subdomains": set(), "families": set()})
        subdomain = str(record.get("subdomain", "") or "").strip()
        if subdomain:
            entry["subdomains"].add(subdomain)
        family = str(record.get("activity_family", "") or "").strip()
        if family:
            entry["families"].add(family)

    def family_permits(family: str) -> bool:
        allowed = activity_families.allowed_domains(family)
        return allowed is not None and DOMAIN in set(allowed)

    resolvable = {k for k, e in rungs.items()
                  if e["families"] and all(family_permits(f)
                                           for f in e["families"])}
    on_track = {k for k, e in rungs.items()
                if e["subdomains"] & DECLARED_TRACK_SUBDOMAINS}
    return {"source": source, "rungs": rungs, "resolvable": resolvable,
            "on_track": on_track}


def test_the_whole_talking_population_is_forty(world):
    assert len(world["rungs"]) == ALL_TALKING_RUNGS


def test_predicate_1_family_resolvability_is_twenty_seven_of_forty(world):
    """"Every activity family resolves AND permits this domain."

    This is the count the frozen Parent alias work reports
    (`test_parent_24_activity_families.py`: "7 -> 27 of 40 rungs mappable").
    It says nothing about whether a track claims the rung.
    """
    resolvable = len(world["resolvable"])
    assert resolvable == FAMILY_RESOLVABLE
    assert len(world["rungs"]) - resolvable == FAMILY_UNRESOLVABLE


def test_predicate_2_adapter_usability_is_seventeen_of_forty(world):
    """"The frozen 0.5E-B adapter yields a usable CanonicalRung."

    Stricter than predicate 1, and this is where "17 + 23 = 40" comes from. It
    is a CORRECT statement — of this predicate. The earlier report that called
    it a denominator mix was wrong; the real defect was quoting a count without
    naming its predicate.
    """
    source = world["source"]
    usable = len([r for r in source._rungs.values() if r.domain_key == DOMAIN])
    refused = len([k for k in source._refusals if k[0] == DOMAIN])
    assert usable == ADAPTER_USABLE
    assert refused == ADAPTER_REFUSED
    assert usable + refused == ALL_TALKING_RUNGS


def test_the_twenty_three_refusals_split_by_reason(world):
    """13 family failures + 10 track-undeclared. Pinned so one refusal quietly
    becoming the other reads as a change, not as unchanged coverage."""
    from collections import Counter

    counts = Counter(
        type(refusal).__name__
        for key, refusal in world["source"]._refusals.items()
        if key[0] == DOMAIN)
    assert counts["RungNotMappableError"] == REFUSED_FOR_FAMILIES
    assert counts["RungTrackUndeclaredError"] == REFUSED_FOR_UNDECLARED_TRACK
    assert sum(counts.values()) == ADAPTER_REFUSED


def test_the_declared_slp_track_is_twenty_one_rungs(world):
    """21 = 17 mappable + 4 intentionally unresolved. The pilot's population."""
    on_track = world["on_track"]
    mappable = on_track & world["resolvable"]
    assert len(on_track) == DECLARED_TRACK_RUNGS
    assert len(mappable) == DECLARED_TRACK_MAPPABLE
    assert len(on_track - world["resolvable"]) == DECLARED_TRACK_UNRESOLVED


def test_off_track_talking_rungs_are_nineteen(world):
    assert len(world["rungs"]) - len(world["on_track"]) == OFF_TRACK_RUNGS
    assert DECLARED_TRACK_RUNGS + OFF_TRACK_RUNGS == ALL_TALKING_RUNGS


def test_adapter_usable_and_declared_track_mappable_are_the_same_set(world):
    """Why BOTH predicates report 17 — and it is structural, not coincidence.

    An off-track rung can never be adapter-usable, because
    `RungTrackUndeclaredError` refuses it regardless of its families. So
    adapter-usable is a SUBSET of the declared track, and the two counts
    coincide exactly when every on-track family-resolvable rung also assembles.

    Pinned as set equality rather than count equality: two different sets of
    size 17 would otherwise pass.
    """
    usable = {(r.source_rung_months, normalize_milestone_text(r.milestone_text))
              for r in world["source"]._rungs.values()
              if r.domain_key == DOMAIN}
    assert usable == (world["on_track"] & world["resolvable"])


def test_the_generated_artifact_reports_the_declared_track_numbers():
    """The shipped artifact must carry the TRACK population, not either
    whole-domain count — it is the pilot's planning population."""
    import json

    from pilot_runtime.integration.static_rung_source import (
        DEFAULT_ARTIFACT_PATH,
    )

    artifact = json.loads(DEFAULT_ARTIFACT_PATH.read_text(encoding="utf-8"))
    assert artifact["population"] == {
        "declared_track_rungs": DECLARED_TRACK_RUNGS,
        "declared_track_mappable": DECLARED_TRACK_MAPPABLE,
        "declared_track_unresolved": DECLARED_TRACK_UNRESOLVED,
    }
