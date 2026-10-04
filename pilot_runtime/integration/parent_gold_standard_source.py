"""pilot_runtime/integration/parent_gold_standard_source.py — the live bridge.

Implements `pilot_backend.integration.gold_standard_source.GoldStandardRungSource`
over the REAL Parent milestone workbook and the REAL activity-family taxonomy.
A translation layer and nothing more: Parent is not modified, not written to,
and nothing is restated here.

## WHICH workbook, precisely

Measured, not assumed. The generation path opens exactly two files under
`genex-parent/`:

    data/parent_2_4/activity_family_taxonomy_v1.xlsx        the taxonomy
    data/parent_2_4/cdc_milestones_parent_2_4_candidate.xlsx the rungs

The second is the Parent 2.4 workbook the Brain itself reads through
`genex_core.table_loader`, NOT the immutable upstream snapshot
`data/cdc_milestones_with_bridges_family_cleaned_final_app_ready.xlsx`
(sha256 c2b6735d...). That distinction matters and is deliberate: this adapter
must agree with what the Parent brain actually routes on today, so it reads the
same file the brain reads. The c2b6735d snapshot remains the frozen provenance
input and is untouched by the pilot.

Both files are byte-pinned in CI, the taxonomy because it decides which
activity families exist and the candidate workbook because it decides which
rungs exist — and a `rung_ref` is frozen into an immutable anchor the moment a
clinician approves a goal. If the rung set moved underneath an already-minted
anchor, that anchor would reference a milestone no longer in the workbook, and
nothing in the pilot could detect it after the fact.

The same shape as `parent_gcs_source.py`, which bridges Parent session
identity. That one translates a GCS blob; this one translates two workbooks.

## Why this file is in `pilot_runtime` and not `pilot_backend`

`pilot_backend` is dependency-pure by gate: AST tests walk its modules and
assert no cloud SDK, no auth provider, no model client and no database driver
is imported anywhere in it. It also cannot see `genex-parent`, which is a
sibling top-level namespace rather than a package of the pilot.

`pilot_runtime` is the composition layer that is ALLOWED to depend on both
sides — it already imports `firebase_admin` and `google.cloud.firestore` for
exactly this reason. So the dependency on pandas and on the Parent modules
lands here, where a reviewer expects adapters to be, and `pilot_backend`
continues to receive explicit validated input.

## Where this runs: the GENERATION path, never the serving path

The served pilot API image copies `pilot_backend/` and `pilot_runtime/` and
nothing else, and its requirements file carries three pins. This adapter needs
the Parent modules, the two workbooks, pandas and openpyxl — none of which
belong in a narrow PHI-reviewed web image.

It does not have to be there. `GoalService.generate_suggestions` has no HTTP
route: the suggestions route is GET-only, and generation is an offline job.
So this module is imported by the generation job image
(`pilot_runtime/deploy/Dockerfile.generation`) and by CI, and never by
`pilot_runtime/server.py`. A test asserts the served entrypoint's import graph
does not reach it.

## Which Parent modules, and the openai question

Four modules are read:

    parent_taxonomy.activity_families   families + the alias sheet
    genex_core.milestones               the Gold Standard rungs
    genex_core.table_loader             (transitively, the workbook reader)
    genex_core.functional_baseline      the DECLARED tracks

`functional_baseline` imports `genex_core.activity_engine`, which contains the
Parent activity writer's OpenAI client. That import is LAZY — it lives inside
`_get_openai_client()`, behind a `try/except ImportError`, and behind two
environment variables. So importing `functional_baseline` does not import
`openai`, and the generation image deliberately does not install the package:
with no client library and no API key the lazy path cannot activate. A test
pins that this adapter's transitive import graph names no model client.

## It reads; it cannot write

Both public methods are reads. There is no method here that opens either
workbook for writing, and a test asserts no method of this class names a
mutating operation — the same structural argument `parent_gcs_source` makes.

## Nothing is inferred

The adapter answers only about the exact rung asked for. It never substitutes a
nearby month, never guesses a family, and refuses — rather than dropping the
offending family — when a workbook family does not resolve in the taxonomy.
Milestone text is matched on the SAME normalisation
`compute_rung_ref` hashes, so "found" here and "same identity" there cannot
disagree.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pilot_backend.domain.canonical_rung import (
    ActivityFamilyBinding,
    CanonicalRung,
    normalize_milestone_text,
)
from pilot_backend.integration.gold_standard_source import (
    GoldStandardSourceError,
    RungNotFoundError,
    RungNotMappableError,
    RungTarget,
    RungTrackUndeclaredError,
)

#: The baseline contract version this adapter stamps onto every rung it builds.
#: Travels with the anchor so a goal created today stays legible after the
#: functional baseline moves on. Pinned by the cross-system CI gate against the
#: declared tracks it describes.
BASELINE_VERSION = "parent-2.4-functional-baseline-v1"

#: Relative path from the repository root to the Parent package root. The
#: directory name contains a hyphen, so it can never be an importable package
#: name — `genex-parent` has to go on `sys.path` for `parent_taxonomy` and
#: `genex_core` to resolve. That is why this adapter takes an explicit root
#: rather than relying on an ambient import working.
PARENT_PACKAGE_DIRNAME = "genex-parent"


def default_parent_root() -> Path:
    """The `genex-parent` directory beside this repository's pilot packages.

    Resolved from this file rather than from the process working directory, so
    a job started from `/` finds the same workbook a test started from the
    repository root does.
    """
    return Path(__file__).resolve().parents[2] / PARENT_PACKAGE_DIRNAME


class ParentGoldStandardSource:
    """The real Gold Standard, adapted to the pilot's canonical-rung port."""

    def __init__(self, parent_root: Optional[Path] = None) -> None:
        root = Path(parent_root) if parent_root is not None \
            else default_parent_root()
        if not root.is_dir():
            # Deliberately does not quote the path: a deployment path is not
            # clinical content, but this error is raised in a job whose logs
            # are read by operators and the pilot's logging guard rejects
            # anything resembling a filesystem dump.
            raise GoldStandardSourceError(
                "the Parent package root is not present")
        self._root = root
        self._loaded = False
        #: normalized (domain, months, milestone) -> the assembled rung
        self._rungs: Dict[Tuple[str, int, str], CanonicalRung] = {}
        #: normalized target -> why it could not be assembled
        self._refusals: Dict[Tuple[str, int, str], GoldStandardSourceError] = {}

    # -- loading ----------------------------------------------------------

    def _import_parent(self):
        """Put `genex-parent` on the path and import the four modules.

        Inserted at position 0 only if absent, and never removed: removing it
        would break the already-imported modules' own lazy intra-package
        imports (`functional_baseline` imports `genex_core.milestones` inside a
        function).
        """
        root = str(self._root)
        if root not in sys.path:
            sys.path.insert(0, root)
        from genex_core import functional_baseline  # noqa: WPS433
        from genex_core import milestones  # noqa: WPS433
        from parent_taxonomy import activity_families  # noqa: WPS433
        return milestones, activity_families, functional_baseline

    @staticmethod
    def _declared_tracks(functional_baseline) -> Dict[str, Tuple[
            Tuple[str, ...], Tuple[str, ...]]]:
        """subdomain -> (that area's track subdomains, its track families).

        Built from `BaselineArea.track_subdomains` and the union of its
        choices' `track_families`, which is how `compute_track_ref` is defined.
        A subdomain absent from this mapping has no declared track, and the
        adapter refuses rather than inventing one.

        The union is taken across choices because a track's family set is a
        property of the TRACK, while a choice names the subset relevant at one
        entry level. Talking & Communicating declares no families at all — only
        Daily Living does — so for SLP this is empty and `track_ref` depends on
        the subdomains alone.
        """
        tracks: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {}
        for area in functional_baseline.AREAS:
            subdomains = tuple(area.track_subdomains)
            families: List[str] = []
            for choice in getattr(area, "choices", ()):
                families.extend(choice.track_families)
            entry = (subdomains, tuple(sorted(set(families))))
            for subdomain in subdomains:
                tracks[subdomain] = entry
        return tracks

    def _load(self) -> None:
        if self._loaded:
            return
        milestones, activity_families, functional_baseline = \
            self._import_parent()

        taxonomy_version = activity_families.ACTIVITY_TAXONOMY_VERSION
        tracks = self._declared_tracks(functional_baseline)
        frame = milestones.get_cdc_df()

        # A workbook ROW is a bridge step; a MILESTONE is the rung. 369 rows
        # collapse to 163 rungs, so rows are folded by identity first and the
        # rung assembled once per milestone. Keying on rows would mint up to
        # five rungs for one milestone.
        folded: Dict[Tuple[str, int, str], Dict[str, object]] = {}
        for record in frame.to_dict(orient="records"):
            domain = str(record.get("category_key", "") or "").strip()
            milestone = str(record.get("milestone", "") or "").strip()
            subdomain = str(record.get("subdomain", "") or "").strip()
            if not domain or not milestone:
                continue
            try:
                months = int(record.get("months"))
            except (TypeError, ValueError):
                continue
            key = (domain, months, normalize_milestone_text(milestone))
            entry = folded.setdefault(key, {
                "domain": domain, "months": months, "milestone": milestone,
                "subdomains": set(), "families": set(),
            })
            if subdomain:
                entry["subdomains"].add(subdomain)
            family = str(record.get("activity_family", "") or "").strip()
            if family:
                entry["families"].add(family)

        for key, entry in folded.items():
            try:
                self._rungs[key] = self._assemble(
                    entry, tracks, activity_families, taxonomy_version)
            except GoldStandardSourceError as refusal:
                self._refusals[key] = refusal
        self._loaded = True

    def _assemble(self, entry, tracks, activity_families,
                  taxonomy_version: str) -> CanonicalRung:
        """One rung, or a refusal. Never a partial rung."""
        domain = entry["domain"]
        subdomains = sorted(entry["subdomains"])
        if not subdomains:
            raise RungTrackUndeclaredError("the rung declares no subdomain")

        # The workbook's subdomain is functionally determined per milestone
        # (measured: zero conflicts across all 163 rungs). A rung that somehow
        # carried two would make `subdomain` a choice, so it is refused rather
        # than resolved by picking the first.
        if len(subdomains) > 1:
            raise RungNotMappableError(
                "the rung names more than one subdomain")
        subdomain = subdomains[0]

        families = sorted(entry["families"])
        if not families:
            raise RungNotMappableError("the rung names no activity family")

        bindings: List[ActivityFamilyBinding] = []
        for family in families:
            # Resolves THROUGH the alias sheet — this is where Scenario C's 13
            # approved aliases take effect. An unknown family has no allowed
            # domains to read, so no binding can be built for it.
            allowed = activity_families.allowed_domains(family)
            if allowed is None:
                raise RungNotMappableError(
                    "an activity family does not resolve in the taxonomy")
            canonical_family = activity_families.resolve_family_key(family)
            bindings.append(ActivityFamilyBinding(
                family_ref=canonical_family,
                allowed_domains=tuple(sorted(allowed)),
            ))

        # Two workbook values can alias onto ONE family (four gesture values
        # do). After resolution those bindings are identical, so collapse them
        # — `CanonicalRung` would otherwise see a duplicate family_ref.
        deduped = {b.family_ref: b for b in bindings}
        if len(deduped) < len(bindings):
            bindings = [deduped[k] for k in sorted(deduped)]

        if subdomain not in tracks:
            raise RungTrackUndeclaredError(
                "no functional-baseline track declares this subdomain")
        track_subdomains, track_families = tracks[subdomain]

        rung = CanonicalRung.build(
            domain_key=domain,
            source_rung_months=entry["months"],
            milestone_text=entry["milestone"],
            subdomain=subdomain,
            family_bindings=bindings,
            track_subdomains=track_subdomains,
            track_families=track_families,
            taxonomy_version=taxonomy_version,
            baseline_version=BASELINE_VERSION,
        )
        # The adapter's contract is that a rung it RETURNS is usable. A rung
        # whose families all resolved but which still is not mappable means
        # some family does not permit this domain, and allowing it through
        # would move the refusal to allocation, long after the information
        # needed to explain it is gone.
        if not rung.is_activity_mappable:
            raise RungNotMappableError(
                "an activity family does not permit this domain")
        return rung

    # -- the port ---------------------------------------------------------

    def rung_for_target(self, target: RungTarget) -> CanonicalRung:
        self._load()
        key = (target.domain_key.strip(), target.source_rung_months,
               normalize_milestone_text(target.milestone_text))
        found = self._rungs.get(key)
        if found is not None:
            return found
        refusal = self._refusals.get(key)
        if refusal is not None:
            raise refusal
        raise RungNotFoundError("no such canonical rung")

    def mappable_rungs_for_domain(self, domain_key: str
                                  ) -> Tuple[CanonicalRung, ...]:
        self._load()
        wanted = (domain_key or "").strip()
        return tuple(sorted(
            (r for r in self._rungs.values() if r.domain_key == wanted),
            key=lambda r: (r.source_rung_months, r.rung_ref)))

    # -- introspection, for the cross-system gate -------------------------

    def unmappable_targets_for_domain(self, domain_key: str
                                      ) -> Tuple[Tuple[int, str, str], ...]:
        """(months, rung_ref, refusal class name) for every refused rung.

        Exists so the cross-system CI gate can assert WHICH rungs are still
        unreconciled and WHY, instead of only counting the ones that work. A
        coverage number that rose because one refusal silently became a
        different refusal would otherwise read as progress.

        Identified by `rung_ref` rather than by milestone text: the ref is
        deterministic and carries no clinical content, so this is safe to
        print in a CI log. Normalisation is idempotent, so hashing the already
        normalised key yields the same ref the rung itself would have.
        """
        self._load()
        from pilot_backend.domain.canonical_rung import compute_rung_ref

        out = []
        for (domain, months, normalized), refusal in self._refusals.items():
            if domain != (domain_key or "").strip():
                continue
            out.append((months, compute_rung_ref(domain, months, normalized),
                        type(refusal).__name__))
        return tuple(sorted(out))
