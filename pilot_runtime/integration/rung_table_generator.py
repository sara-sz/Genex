"""pilot_runtime/integration/rung_table_generator.py — 0.5F-B, Option C.

Emits the static clinical lookup artifact the BROWSER Pilot API uses to answer
`GoldStandardRungSource` without the Parent package, the workbooks, pandas or
openpyxl.

## WHY THIS EXISTS

0.5F-B's provider-triggered generation route needs a canonical rung. The only
implementation of that port — `ParentGoldStandardSource` — needs four things the
browser serving image does not and must not have: `genex_core`,
`parent_taxonomy`, two `.xlsx` workbooks, and pandas/openpyxl to read them.
Proven in the inspection: in the real staged serving context
`ParentGoldStandardSource()` raises at construction, so the route failed closed
with 403 for three independent reasons.

Three fixes were considered. Shipping the Parent slice into the browser image
was rejected (a spreadsheet parser and the clinical workbook in the
PHI-serving, browser-facing container). A second runtime generation service was
rejected (a new service for one lookup). This is the third: precompute the
lookups OFFLINE, from the frozen machinery, and ship a small JSON table.

## THERE IS STILL EXACTLY ONE TRAVERSAL

This generator does not know the developmental order of anything. Every
clinical fact in the artifact is READ OUT of frozen code:

    the ladder order        `functional_baseline._step(domain, m, +1, track)`
    the milestone at a rung `functional_baseline.question_at(...)`
    the declared track      `BaselineArea.track_subdomains` + choices'
                            `track_families`
    rung identity + families `ParentGoldStandardSource.rung_for_target`, which
                            is the frozen 0.5E-B adapter over the real workbook
                            and the real taxonomy

So the ladder is traversed by the Parent engine at BUILD time and by nobody at
runtime. The runtime adapter does dict lookups. `_step` has one
implementation, in Parent, as before.

## THE STEP TABLE IS ENUMERATED, NOT SUMMARISED

`_step(from_months)` is recorded for EVERY integer input in
`[0, MAX_PROBED_FROM_MONTHS]`, one entry each. The obvious smaller encoding —
store the ordered track months and have the runtime pick "the lowest one above
`from_months`" — was rejected: that comparison IS the ladder traversal, just
reimplemented in the browser image under a different name. An explicit
input -> output table cannot drift into an algorithm.

The tail is probed well past the top of the ladder and the generator ASSERTS
the tail is genuinely empty, so "no entry" means "the Parent engine returned
None here", never "the generator stopped early".

## ALL 21 DECLARED-TRACK OUTCOMES, INCLUDING THE FOUR FAILURES

The artifact represents every rung on the declared track, not only the usable
ones. The four intentionally unresolved rungs are recorded with
`mappable: false` and the refusal class the frozen adapter raised for them.

That is load-bearing rather than tidy. The step table's `30 -> 36` entry targets
one of those four, so a real runtime request from a floor of 30 months MUST fail
closed. Omitting the failures would have made that target simply absent, which
the runtime would have read as "the top of the track" — a silent wrong answer in
place of a refusal. Representing them makes the refusal explicit and testable.

## WHAT IS DELIBERATELY NOT IN THE ARTIFACT

No child's ceiling. `target <= not_demonstrated_months` stays a RUNTIME check
against the projection, because the ceiling is a property of one child's
baseline and this table is the same for every child. Encoding a ceiling here
would bake one child's limits into shared clinical data.

No chronological age, no diagnosis, no entry-choice mapping. The runtime maps
its own observed floor; this table answers only "what is one rung up from
`n` months".

## PROVENANCE NAMES BOTH WORKBOOKS, AND WHY

    rung_workbook   data/parent_2_4/cdc_milestones_parent_2_4_candidate.xlsx
                    a16ce10f... — the file the Parent brain reads and therefore
                    the file every rung here actually came from
    gold_standard_snapshot
                    data/cdc_milestones_with_bridges_family_cleaned_final_app_ready.xlsx
                    c2b6735d... — the frozen upstream provenance input

Recording only the second would be false provenance: a change to the candidate
workbook would move every rung in this artifact while the recorded SHA stayed
put. Both are already byte-pinned in CI, so both are reproducible inputs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pilot_backend.domain.canonical_rung import normalize_milestone_text
from pilot_backend.integration.gold_standard_source import (
    GoldStandardSourceError,
    RungTarget,
)

from .parent_gold_standard_source import (
    BASELINE_VERSION,
    ParentGoldStandardSource,
    default_parent_root,
)

#: The artifact's own schema. A change in SHAPE gets a new version here, which
#: changes the digest and so cannot pass the drift gate unnoticed.
ARTIFACT_SCHEMA_VERSION = "pilot-rung-table-v1"

#: The generator's version — the procedure below, not the data it reads. Bumped
#: when HOW the table is derived changes, even if the numbers come out the same.
GENERATOR_VERSION = "pilot-rung-table-generator-v1"

#: The pilot's only generatable domain, matching
#: `baseline_suggestion_generation.SUPPORTED_DOMAIN`. Widening this is a
#: clinical decision with its own track and coverage review.
DOMAIN_KEY = "talking_and_communicating"

#: How far past the top of the ladder `_step` is probed. The declared SLP track
#: tops out at 60 months, so this is double the needed range: enough that the
#: emptiness of the tail is a measured fact rather than an assumption.
MAX_PROBED_FROM_MONTHS = 120

#: Workbook paths, relative to the Parent package root. Recorded in the
#: artifact so a reader can verify the SHAs without reading this file.
RUNG_WORKBOOK_RELPATH = "data/parent_2_4/cdc_milestones_parent_2_4_candidate.xlsx"
TAXONOMY_RELPATH = "data/parent_2_4/activity_family_taxonomy_v1.xlsx"
GOLD_STANDARD_SNAPSHOT_RELPATH = (
    "data/cdc_milestones_with_bridges_family_cleaned_final_app_ready.xlsx")

#: Where the committed artifact lives. Under `pilot_runtime/` so the browser
#: image's `gcloudignore` (which admits `pilot_backend/**` + `pilot_runtime/**`
#: and nothing else) carries it without any deploy-config change.
ARTIFACT_RELPATH = "pilot_runtime/data/rung_table_talking_v1.json"


class RungTableError(Exception):
    """The artifact could not be generated. Build-time only, never served."""

    PHI_SAFE_MESSAGE = True


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(payload: Any) -> str:
    """The one serialisation the digest and the drift gate both use.

    Sorted keys and no insignificant whitespace, so two structurally equal
    artifacts have one byte representation. The committed FILE is written
    pretty-printed for reviewable diffs; the digest is taken over this form, so
    reformatting the file cannot change its identity and cannot be used to
    smuggle a content change past the gate either.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def artifact_digest(body: Dict[str, Any]) -> str:
    """sha256 over the canonical body, with any existing digest excluded.

    Excluding the field is what makes the digest verifiable: a reader
    recomputes it from the same input the generator had, rather than having to
    trust the value beside it.
    """
    without = {k: v for k, v in body.items() if k != "artifact_digest"}
    return hashlib.sha256(canonical_json(without).encode("utf-8")).hexdigest()


def _declared_track(functional_baseline) -> Tuple[Tuple[str, ...],
                                                  Tuple[str, ...]]:
    """The declared track for the pilot domain: (subdomains, families).

    Exactly the definition `ParentGoldStandardSource._declared_tracks` and
    `next_rung_target` already use — `BaselineArea.track_subdomains` plus the
    union of its choices' `track_families` — so the artifact's `track_ref`
    cannot disagree with the one the live adapter computes.
    """
    area = functional_baseline.area_for_domain(DOMAIN_KEY)
    if area is None:
        raise RungTableError("the pilot domain declares no baseline area")
    subdomains = tuple(area.track_subdomains)
    families: List[str] = []
    for choice in getattr(area, "choices", ()):
        families.extend(choice.track_families)
    return subdomains, tuple(sorted(set(families)))


def _rung_entry(rung) -> Dict[str, Any]:
    """One usable rung, flattened to exactly what `CanonicalRung.build` needs.

    `rung_ref` and `track_ref` are written out even though the runtime
    recomputes them: `CanonicalRung.__post_init__` compares the recomputed
    value against the supplied one, so storing them turns every runtime lookup
    into an integrity check on the artifact for free.
    """
    return {
        "rung_ref": rung.rung_ref,
        "source_rung_months": rung.source_rung_months,
        "milestone_text": rung.milestone_text,
        "subdomain": rung.subdomain,
        "mappable": True,
        "unresolved_reason": None,
        "family_bindings": [
            {"family_ref": b.family_ref,
             "allowed_domains": list(b.allowed_domains)}
            for b in rung.family_bindings
        ],
        "track_ref": rung.track_ref,
    }


def _unresolved_entry(months: int, milestone: str, subdomain: str,
                      refusal: str, rung_ref: str) -> Dict[str, Any]:
    """One declared-track rung the frozen adapter refused.

    `family_bindings` is EMPTY rather than partial, and that is the honest
    encoding: an `ActivityFamilyBinding` cannot be constructed for a family the
    taxonomy does not define, because its allowed domains are what makes it a
    binding and there are none to read. A partial list would describe a rung
    nobody authored.
    """
    return {
        "rung_ref": rung_ref,
        "source_rung_months": months,
        "milestone_text": milestone,
        "subdomain": subdomain,
        "mappable": False,
        "unresolved_reason": refusal,
        "family_bindings": [],
        "track_ref": None,
    }


def build_artifact(parent_root: Optional[Path] = None) -> Dict[str, Any]:
    """Generate the artifact body. Reads only; writes nothing."""
    root = Path(parent_root) if parent_root is not None else default_parent_root()
    if not root.is_dir():
        raise RungTableError("the Parent package root is not present")

    source = ParentGoldStandardSource(root)
    # Deliberately uses the adapter's own loader rather than reading the
    # workbook again here: a second reader is a second interpretation.
    source._load()
    _milestones, activity_families, functional_baseline = \
        source._import_parent()

    track_subdomains, track_families = _declared_track(functional_baseline)
    on_track = set(track_subdomains)

    # -- the 21 declared-track rungs ---------------------------------------
    rungs: Dict[str, Dict[str, Any]] = {}
    for rung in source._rungs.values():
        if rung.domain_key != DOMAIN_KEY or rung.subdomain not in on_track:
            continue
        rungs[rung.rung_ref] = _rung_entry(rung)
    mappable_count = len(rungs)

    # The refused rungs. `_refusals` is keyed by normalized text and does not
    # keep the subdomain, so the workbook frame is re-read for those two fields
    # ONLY — never for a family, a month or an ordering.
    frame = _milestones.get_cdc_df()
    detail: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
    for record in frame.to_dict(orient="records"):
        domain = str(record.get("category_key", "") or "").strip()
        milestone = str(record.get("milestone", "") or "").strip()
        subdomain = str(record.get("subdomain", "") or "").strip()
        if domain != DOMAIN_KEY or not milestone:
            continue
        try:
            months = int(record.get("months"))
        except (TypeError, ValueError):
            continue
        key = (domain, months, normalize_milestone_text(milestone))
        found = detail.setdefault(key, {"milestone": milestone,
                                        "subdomains": set()})
        if subdomain:
            found["subdomains"].add(subdomain)

    from pilot_backend.domain.canonical_rung import compute_rung_ref

    unresolved_count = 0
    for key, refusal in source._refusals.items():
        if key[0] != DOMAIN_KEY:
            continue
        info = detail.get(key)
        if info is None or not (info["subdomains"] & on_track):
            # Off the declared track. Excluded by design: the pilot plans on
            # the declared SLP track only, and admitting an off-track rung
            # would mint a target the Parent brain never declared.
            continue
        subdomains = sorted(info["subdomains"] & on_track)
        if len(subdomains) != 1:
            raise RungTableError(
                "a declared-track rung names more than one track subdomain")
        ref = compute_rung_ref(DOMAIN_KEY, key[1], key[2])
        if ref in rungs:
            raise RungTableError("a rung is both usable and refused")
        rungs[ref] = _unresolved_entry(key[1], info["milestone"],
                                       subdomains[0],
                                       type(refusal).__name__, ref)
        unresolved_count += 1

    # -- the step table, one entry per integer input ------------------------
    steps: Dict[str, Dict[str, Any]] = {}
    highest_with_target = -1
    for from_months in range(0, MAX_PROBED_FROM_MONTHS + 1):
        target = source.next_rung_target(DOMAIN_KEY, from_months)
        if target is None:
            continue
        ref = compute_rung_ref(DOMAIN_KEY, target.source_rung_months,
                              target.milestone_text)
        if ref not in rungs:
            # The frozen engine stepped to a rung the declared-track
            # enumeration above did not contain. That would mean the two
            # frozen definitions of "the track" disagree, so the artifact is
            # refused rather than quietly extended.
            raise RungTableError(
                "the frozen step reached a rung outside the declared track")
        steps[str(from_months)] = {
            "target_months": target.source_rung_months,
            "target_rung_ref": ref,
        }
        highest_with_target = from_months

    if highest_with_target < 0:
        raise RungTableError("the declared track produced no step at all")
    # Proves the tail is empty because `_step` said so, not because probing
    # stopped. Without this, a truncated range would look like "top of track".
    for from_months in range(highest_with_target + 1,
                             MAX_PROBED_FROM_MONTHS + 1):
        if str(from_months) in steps:  # pragma: no cover - loop invariant
            raise RungTableError("the step table tail is not contiguous")

    body: Dict[str, Any] = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "provenance": {
            "baseline_version": BASELINE_VERSION,
            "taxonomy_version": activity_families.ACTIVITY_TAXONOMY_VERSION,
            "rung_workbook_relpath": RUNG_WORKBOOK_RELPATH,
            "rung_workbook_sha256": _sha256_file(root / RUNG_WORKBOOK_RELPATH),
            "taxonomy_relpath": TAXONOMY_RELPATH,
            "taxonomy_sha256": _sha256_file(root / TAXONOMY_RELPATH),
            "gold_standard_snapshot_relpath": GOLD_STANDARD_SNAPSHOT_RELPATH,
            "gold_standard_snapshot_sha256": _sha256_file(
                root / GOLD_STANDARD_SNAPSHOT_RELPATH),
        },
        "declared_track": {
            "domain_key": DOMAIN_KEY,
            "track_subdomains": sorted(track_subdomains),
            "track_families": sorted(track_families),
        },
        "population": {
            "declared_track_rungs": mappable_count + unresolved_count,
            "declared_track_mappable": mappable_count,
            "declared_track_unresolved": unresolved_count,
        },
        "step_domain": {
            "min_from_months": 0,
            "max_probed_from_months": MAX_PROBED_FROM_MONTHS,
            "highest_from_months_with_target": highest_with_target,
        },
        "steps": steps,
        "rungs": rungs,
    }
    body["artifact_digest"] = artifact_digest(body)
    return body


def render(body: Dict[str, Any]) -> str:
    """The committed file's bytes: pretty, sorted, newline-terminated."""
    return json.dumps(body, sort_keys=True, indent=2,
                      ensure_ascii=False) + "\n"


def write_artifact(destination: Path,
                   parent_root: Optional[Path] = None) -> Dict[str, Any]:
    body = build_artifact(parent_root)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render(body), encoding="utf-8")
    return body


def main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover - CLI
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=None,
                        help="destination path for the artifact")
    parser.add_argument("--parent-root", default=None,
                        help="the genex-parent directory")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[2]
    out = Path(args.out) if args.out else repo_root / ARTIFACT_RELPATH
    body = write_artifact(
        out, Path(args.parent_root) if args.parent_root else None)
    print(f"wrote {out}")
    print(f"  digest {body['artifact_digest']}")
    print(f"  rungs  {body['population']}")
    print(f"  steps  {len(body['steps'])}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
