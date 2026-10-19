"""api/baseline_micro_tracks.py — canonical micro-tracks and content support.

Slice 2 of the founder-approved 6/9 Baseline v2 redesign. See the canonical
design note: `pilot_runtime/docs/parent_baseline_v2_design.md`.

## WHAT A MICRO-TRACK IS, AND WHY IT IS NOT A MONTH BAND

A micro-track is one canonical ACTIVITY FAMILY and the declared-track rungs
that bind it, in month order. That grouping is already in the taxonomy — a
family spanning several months IS an actionable progression — and it is the
axis the month-band ladder does not walk. The audited consequence of walking
bands instead: a parent must answer a rung that can never be a target (the 30m
rung whose family is outside the taxonomy) in order to reach the three
actionable rungs beside it.

Nothing here is invented. Every rung is a verbatim canonical row and every
family ref comes from `parent_taxonomy`.

## THE THREE RESOLUTION RULES, DERIVED FROM THE PILOT ARTIFACT

Reproducing the Pilot's own rung-table bindings took three rules, each of
which was found by comparing against the artifact rather than assumed:

  1. UNION ACROSS DUPLICATE ROWS. `_rows_for_domain` dedups on
     `(months, milestone)`, and the workbook carries several rows per milestone
     whose `activity_family` values DIFFER. The 24-month rung binds two
     families, and taking the deduped row's single column silently drops one.
  2. `is_known_family` DECIDES MAPPABILITY. `resolve_family_key` returns its
     input unchanged for an unknown family, so resolving is not checking. Four
     declared-track rungs name families that are not among the taxonomy's 56 —
     those rungs are UNMAPPABLE and can never be a target.
  3. `allowed_domains` MUST ADMIT THE DOMAIN. A family resolvable in general
     is not thereby usable here.

A test asserts this derivation equals the Pilot's `rung_table_talking_v1.json`
bindings for all 21 declared-track rungs. If the two ever diverge, CI fails
rather than the two systems quietly disagreeing about what a rung means.

## WHY CONTENT SUPPORT IS DECLARED HERE RATHER THAN READ

The Parent image contains `genex-parent/` only — `pilot_runtime/` is not in it,
so the activity-bank artifact cannot be read at runtime. Content support is
therefore a VERSIONED, DIGESTED DECLARATION in this module, and a CI drift gate
compares it against `pilot_runtime/data/activity_bank_talking_v1.json`, which
is present at test time. Divergence is a build failure, never a silent runtime
disagreement.

Track 1 (this workstream) reads content status only. Creating or curating cards
belongs entirely to Track 2.

## SERVED IS NOT MAPPABLE

Three distinct states, and collapsing any two of them is a clinical error:

    SUPPORTED    mappable, and EVERY bound family has reviewed content
    UNSERVED     mappable and clinically appropriate, but content is missing
    UNMAPPABLE   no canonical family at all; never a target

`UNSERVED` exists because content availability must never decide which skill is
clinically appropriate. A missing card set is a Genex gap, not a reason to aim
at a different child's target — so a target in this state yields
`TARGET_UNSUPPORTED_BY_CONTENT` and never a substitution.

The Pilot's generation cannot make this distinction: it receives no activity
bank and splits deficits on mappability alone, so a mappable-but-unserved
family produces a real suggestion that fails only later, at release. That is
why the distinction has to exist on this side.
"""

from __future__ import annotations

import functools
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple

from genex_core import functional_baseline as v1
from parent_taxonomy import activity_families as AF

#: Content support states. See the module docstring on why three.
SUPPORTED = "supported"
UNSERVED = "unserved"
UNMAPPABLE = "unmappable"

SUPPORT_STATES: Tuple[str, ...] = (SUPPORTED, UNSERVED, UNMAPPABLE)


class MicroTrackError(Exception):
    """A micro-track could not be derived. PHI-safe: names no child."""

    PHI_SAFE_MESSAGE = True


# ---------------------------------------------------------------------------
# the versioned content-support declaration
# ---------------------------------------------------------------------------

#: Activity families with REVIEWED content, as audited against the generated
#: activity bank. Versioned and digested because the Parent runtime cannot read
#: the Pilot artifact; the drift gate in
#: `tests/test_parent_24_baseline_micro_tracks.py` holds them equal.
#:
#: Track 2 adds to this ONLY after a clinical reviewer approves the cards. A
#: family appearing here without approved content would make an unservable goal
#: look releasable, which is the one direction that must never be guessed.
SERVED_FAMILY_SET_ID = "talking-served-families-v1"

SERVED_FAMILIES: FrozenSet[str] = frozenset({
    "book_object_naming",
    "expressive_vocabulary_growth",
    "sentence_building",
    "two_word_phrases",
})

#: Audited as present-but-EMPTY. Listed explicitly rather than inferred from
#: absence so a reviewer can see that the gap is known, and so a family going
#: missing from the taxonomy is distinguishable from one awaiting content.
KNOWN_UNSERVED_FAMILIES: FrozenSet[str] = frozenset({
    "action_picture_labeling",
    "early_vocalizations",
    "expressive_first_words",
    "function_question_answering",
    "narration_storytelling",
})


def served_family_set_digest() -> str:
    """Digest of the declaration, so drift is detectable rather than arguable."""
    payload = {
        "served_family_set_id": SERVED_FAMILY_SET_ID,
        "served": sorted(SERVED_FAMILIES),
        "known_unserved": sorted(KNOWN_UNSERVED_FAMILIES),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# canonical derivation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CanonicalRungRef:
    """One declared-track rung, by its canonical identity. Never invented."""

    months: int
    milestone: str
    subdomain: str
    family_bindings: Tuple[str, ...]

    @property
    def is_mappable(self) -> bool:
        return bool(self.family_bindings)


@dataclass(frozen=True)
class MicroTrack:
    """One activity family and its rungs, ascending. Derived, never stored."""

    family_ref: str
    rungs: Tuple[CanonicalRungRef, ...]

    @property
    def is_served(self) -> bool:
        return self.family_ref in SERVED_FAMILIES

    @property
    def entry_months(self) -> int:
        return self.rungs[0].months


@functools.lru_cache(maxsize=8)
def _raw_family_index(domain: str) -> Mapping[Tuple[int, str], FrozenSet[str]]:
    """`(months, milestone) -> every raw workbook family`, UNION not first.

    Rule 1 from the module docstring. Cached because the workbook read is
    expensive and the result is a pure function of frozen data.
    """
    from genex_core.milestones import get_cdc_df

    index: Dict[Tuple[int, str], set] = {}
    for record in get_cdc_df().to_dict(orient="records"):
        if str(record.get("category_key") or "") != domain:
            continue
        months = record.get("months")
        milestone = str(record.get("milestone", "") or "").strip()
        if months is None or not milestone:
            continue
        family = str(record.get("activity_family", "") or "").strip()
        if not family:
            continue
        index.setdefault((int(months), milestone), set()).add(family)
    return {key: frozenset(value) for key, value in index.items()}


def resolve_family_bindings(domain: str, months: int,
                            milestone: str) -> Tuple[str, ...]:
    """Canonical family refs for one rung, or `()` when UNMAPPABLE.

    Applies all three rules. `()` is a finding — "this rung can never be a
    target" — and is deliberately not an exception: an unmappable rung must be
    reportable beside its mappable siblings rather than aborting them.
    """
    raw = _raw_family_index(domain).get((int(months), str(milestone).strip()),
                                       frozenset())
    resolved = set()
    for family in raw:
        key = AF.resolve_family_key(family)
        # Rule 2: resolving is not checking. An unknown family resolves to
        # itself, so membership has to be asked separately.
        if not AF.is_known_family(key):
            continue
        # Rule 3: usable in general is not usable in this domain.
        allowed = AF.allowed_domains(key)
        if allowed and domain not in allowed:
            continue
        resolved.add(key)
    return tuple(sorted(resolved))


@functools.lru_cache(maxsize=8)
def declared_rungs(area_id: str) -> Tuple[CanonicalRungRef, ...]:
    """Every declared-track rung for an area, in canonical order.

    Ordered by `(months, milestone)` — the same total order the engine uses, so
    a question sequence derived here and one derived there cannot disagree.
    """
    area = v1.get_area(area_id)
    rows = v1._rows_for_domain(area.domain, area.track_subdomains, ())
    refs = [
        CanonicalRungRef(
            months=int(row["months"]),
            milestone=str(row["milestone"]),
            subdomain=str(row.get("subdomain") or ""),
            family_bindings=resolve_family_bindings(
                area.domain, int(row["months"]), str(row["milestone"])),
        )
        for row in rows
    ]
    refs.sort(key=lambda r: (r.months, r.milestone))
    return tuple(refs)


@functools.lru_cache(maxsize=8)
def micro_tracks(area_id: str) -> Tuple[MicroTrack, ...]:
    """Micro-tracks for an area, ordered by entry month then family ref.

    A rung binding two families belongs to BOTH tracks — it is genuinely the
    entry point of two progressions, which is why the 24-month rung's goal
    binds two activity families.
    """
    by_family: Dict[str, list] = {}
    for ref in declared_rungs(area_id):
        for family in ref.family_bindings:
            by_family.setdefault(family, []).append(ref)
    tracks = [
        MicroTrack(family_ref=family,
                   rungs=tuple(sorted(refs, key=lambda r: (r.months,
                                                           r.milestone))))
        for family, refs in by_family.items()
    ]
    tracks.sort(key=lambda t: (t.entry_months, t.family_ref))
    return tuple(tracks)


def served_micro_tracks(area_id: str) -> Tuple[MicroTrack, ...]:
    return tuple(t for t in micro_tracks(area_id) if t.is_served)


# ---------------------------------------------------------------------------
# content support for a rung
# ---------------------------------------------------------------------------

def rung_support(ref: CanonicalRungRef) -> str:
    """`SUPPORTED` / `UNSERVED` / `UNMAPPABLE` for one rung.

    EVERY bound family must be served, not merely one. The 24-month rung binds
    two families and a week built from only the served half would practise one
    clinical target while reporting both as covered — the exact substitution
    `require_all_families_served` refuses on the Pilot side, applied here early
    enough to avoid proposing the goal at all.
    """
    if not ref.family_bindings:
        return UNMAPPABLE
    if all(family in SERVED_FAMILIES for family in ref.family_bindings):
        return SUPPORTED
    return UNSERVED


def rung_at(area_id: str, months: int,
            milestone: str) -> Optional[CanonicalRungRef]:
    for ref in declared_rungs(area_id):
        if ref.months == int(months) and ref.milestone == milestone:
            return ref
    return None


def support_summary(area_id: str) -> Dict[str, Any]:
    """Counts for diagnostics and the content-gap report. Never child data."""
    refs = declared_rungs(area_id)
    counts = {state: 0 for state in SUPPORT_STATES}
    for ref in refs:
        counts[rung_support(ref)] += 1
    return {
        "area_id": area_id,
        "declared_rungs": len(refs),
        "by_support": counts,
        "served_family_set_id": SERVED_FAMILY_SET_ID,
        "served_family_set_digest": served_family_set_digest(),
    }


__all__ = [
    "KNOWN_UNSERVED_FAMILIES",
    "SERVED_FAMILIES",
    "SERVED_FAMILY_SET_ID",
    "SUPPORTED",
    "SUPPORT_STATES",
    "UNMAPPABLE",
    "UNSERVED",
    "CanonicalRungRef",
    "MicroTrack",
    "MicroTrackError",
    "declared_rungs",
    "micro_tracks",
    "resolve_family_bindings",
    "rung_at",
    "rung_support",
    "served_family_set_digest",
    "served_micro_tracks",
    "support_summary",
]
