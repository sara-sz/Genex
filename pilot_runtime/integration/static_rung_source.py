"""pilot_runtime/integration/static_rung_source.py — 0.5F-B, Option C.

The BROWSER Pilot API's implementation of
`pilot_backend.integration.gold_standard_source.GoldStandardRungSource`,
backed by one generated JSON artifact and nothing else.

## LOOKUP ONLY. THERE IS NO LADDER HERE.

Every method below is a dict lookup plus validation. In particular
`next_rung_target` reads `steps[str(from_months)]` — it does not compare months,
sort a ladder, or search for the nearest rung above. That is the whole point of
Option C: the ladder was traversed by the FROZEN Parent engine at build time
(see `rung_table_generator`), so `_step` keeps exactly one implementation and
the browser image keeps none.

The obvious smaller artifact would have stored the ordered track months and let
this module pick "the lowest above `from_months`". That comparison IS the
traversal. An explicit input -> output table cannot quietly become an algorithm;
a comparison can.

## WHAT THIS MODULE REFUSES TO IMPORT

No `genex_core`, no `parent_taxonomy`, no pandas, no openpyxl, no `.xlsx`, no
`openai`, no `anthropic`. Only the standard library and `pilot_backend`
domain types. A test walks this module's transitive import graph and asserts
exactly that, and the deployment-context test proves the serving image carries
none of those files.

## THE ARTIFACT IS VALIDATED, NOT TRUSTED

`CanonicalRung.build` recomputes `rung_ref` and `track_ref` from the fields and
`__post_init__` refuses a mismatch. So every lookup that returns a rung has
re-derived its identity from its own content — a hand-edited month or milestone
in the JSON raises here rather than serving a rung whose id describes a
different milestone. The artifact's self-digest is checked once at load for the
same reason, and the CI drift gate regenerates it from the frozen machinery so
a consistent-but-invented table cannot pass either.

## THE FOUR UNRESOLVED RUNGS ARE PRESENT, AND REFUSED

The artifact records all 21 declared-track rungs including the four whose
activity families the taxonomy does not reconcile. Asking for one of those
raises `RungNotMappableError` — the same refusal the live adapter raises, from
the same recorded reason.

This matters because it is reachable: the step table's 30 -> 36 entry targets
one of the four. Had the artifact omitted the failures, that target would simply
be missing and `next_rung_target` would have returned None, which the caller
reads as "the top of the track" — a wrong answer dressed as a normal one. The
refusal is the correct outcome and it is now explicit.

## NO CEILING LIVES HERE

`target <= not_demonstrated_months` is NOT checked in this module and must not
be. A ceiling is a property of one child's projected baseline; this artifact is
identical for every child. The runtime keeps that check in
`baseline_suggestion_generation.resolve_target`, against the projection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from pilot_backend.domain.canonical_rung import (
    ActivityFamilyBinding,
    CanonicalRung,
    RungError,
    normalize_milestone_text,
)
from pilot_backend.integration.gold_standard_source import (
    GoldStandardSourceError,
    RungNotFoundError,
    RungNotMappableError,
    RungTarget,
    RungTrackUndeclaredError,
)

#: Schema this adapter understands. An artifact declaring anything else is
#: refused at load rather than read optimistically: a shape change that this
#: code happened to tolerate would be the one case nobody notices.
SUPPORTED_ARTIFACT_SCHEMA = "pilot-rung-table-v1"

#: The committed artifact, resolved from THIS file rather than from the process
#: working directory — a container entrypoint starting from `/` must find the
#: same bytes a test started from the repository root does.
DEFAULT_ARTIFACT_PATH = Path(__file__).resolve().parents[1] / "data" / \
    "rung_table_talking_v1.json"

#: Refusal class name -> the exception to raise. Explicit rather than resolved
#: by `globals()[name]`: the artifact is data, and data must not be able to
#: name an arbitrary class for this module to instantiate.
_REFUSALS = {
    "RungNotMappableError": RungNotMappableError,
    "RungTrackUndeclaredError": RungTrackUndeclaredError,
    "RungNotFoundError": RungNotFoundError,
}


class StaticRungTableError(GoldStandardSourceError):
    """The artifact is absent, malformed, or fails its own integrity check.

    A subclass of `GoldStandardSourceError` so a composition that somehow
    shipped a broken table still fails CLOSED through the path the route
    already handles, rather than surfacing an unmodelled exception.
    """


def _canonical_json(payload: Any) -> str:
    """Must match `rung_table_generator.canonical_json` exactly."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


class StaticRungTableSource:
    """The generated rung table, adapted to the canonical-rung port."""

    def __init__(self, artifact_path: Optional[Path] = None) -> None:
        path = Path(artifact_path) if artifact_path is not None \
            else DEFAULT_ARTIFACT_PATH
        if not path.is_file():
            # Does not quote the path: the pilot's logging guard rejects
            # anything resembling a filesystem dump.
            raise StaticRungTableError("the rung table artifact is not present")
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StaticRungTableError(
                "the rung table artifact could not be read") from exc
        if not isinstance(body, dict):
            raise StaticRungTableError("the rung table artifact is malformed")

        if body.get("artifact_schema_version") != SUPPORTED_ARTIFACT_SCHEMA:
            raise StaticRungTableError(
                "the rung table artifact declares an unsupported schema")

        declared = body.get("artifact_digest")
        recomputed = self._digest(body)
        if not isinstance(declared, str) or declared != recomputed:
            # Catches accidental hand-editing. It is NOT a security control:
            # anyone able to edit the file can recompute the digest, which is
            # why the CI drift gate regenerates the table from the frozen
            # workbooks instead of merely verifying this value.
            raise StaticRungTableError(
                "the rung table artifact fails its own integrity digest")

        track = body.get("declared_track") or {}
        self._domain = str(track.get("domain_key") or "").strip()
        if not self._domain:
            raise StaticRungTableError(
                "the rung table artifact declares no domain")
        self._track_subdomains = tuple(track.get("track_subdomains") or ())
        self._track_families = tuple(track.get("track_families") or ())

        provenance = body.get("provenance") or {}
        self._taxonomy_version = str(
            provenance.get("taxonomy_version") or "").strip()
        self._baseline_version = str(
            provenance.get("baseline_version") or "").strip()
        if not self._taxonomy_version or not self._baseline_version:
            raise StaticRungTableError(
                "the rung table artifact declares no provenance versions")

        self._rungs: Dict[str, Dict[str, Any]] = dict(body.get("rungs") or {})
        self._steps: Dict[str, Dict[str, Any]] = dict(body.get("steps") or {})
        if not self._rungs or not self._steps:
            raise StaticRungTableError("the rung table artifact is empty")

        #: (domain, months, normalized milestone) -> rung_ref. Built once, with
        #: the SAME normalisation `compute_rung_ref` hashes, so "found here" and
        #: "same identity there" cannot disagree.
        self._by_target: Dict[Tuple[str, int, str], str] = {}
        for ref, entry in self._rungs.items():
            try:
                months = int(entry["source_rung_months"])
                key = (self._domain, months,
                       normalize_milestone_text(entry["milestone_text"]))
            except (KeyError, TypeError, ValueError, RungError) as exc:
                raise StaticRungTableError(
                    "the rung table artifact has a malformed rung") from exc
            if key in self._by_target:
                raise StaticRungTableError(
                    "the rung table artifact has duplicate rung identities")
            self._by_target[key] = ref

        self._digest_value = recomputed
        self._provenance = dict(provenance)

    # -- integrity ---------------------------------------------------------

    @staticmethod
    def _digest(body: Dict[str, Any]) -> str:
        import hashlib

        without = {k: v for k, v in body.items() if k != "artifact_digest"}
        return hashlib.sha256(
            _canonical_json(without).encode("utf-8")).hexdigest()

    @property
    def artifact_digest(self) -> str:
        """The verified digest, for the composition's startup log line."""
        return self._digest_value

    @property
    def provenance(self) -> Dict[str, Any]:
        """The recorded source SHAs and versions. Read-only copy."""
        return dict(self._provenance)

    # -- the port ----------------------------------------------------------

    def _build(self, ref: str, entry: Dict[str, Any]) -> CanonicalRung:
        """Rebuild a `CanonicalRung`, re-deriving both identifiers.

        `CanonicalRung.build` recomputes `rung_ref` and `track_ref`;
        `__post_init__` compares them against what it computed. Comparing the
        result against the artifact's own stored refs as well closes the
        remaining gap: build() alone would happily accept a hand-edited
        milestone by computing a NEW matching ref for it.
        """
        bindings = []
        for binding in entry.get("family_bindings") or ():
            try:
                bindings.append(ActivityFamilyBinding(
                    family_ref=binding["family_ref"],
                    allowed_domains=tuple(binding["allowed_domains"])))
            except (KeyError, TypeError, RungError) as exc:
                raise StaticRungTableError(
                    "the rung table artifact has a malformed family binding"
                ) from exc
        try:
            rung = CanonicalRung.build(
                domain_key=self._domain,
                source_rung_months=int(entry["source_rung_months"]),
                milestone_text=entry["milestone_text"],
                subdomain=entry["subdomain"],
                family_bindings=tuple(bindings),
                track_subdomains=self._track_subdomains,
                track_families=self._track_families,
                taxonomy_version=self._taxonomy_version,
                baseline_version=self._baseline_version)
        except (KeyError, TypeError, ValueError, RungError) as exc:
            raise StaticRungTableError(
                "the rung table artifact has an invalid rung") from exc
        if rung.rung_ref != ref or rung.track_ref != entry.get("track_ref"):
            raise StaticRungTableError(
                "a rung table entry disagrees with its own identifiers")
        if not rung.is_activity_mappable:
            # The artifact said mappable; the rebuilt rung says otherwise.
            # Refused rather than returned: the port's contract is that a rung
            # it RETURNS is usable.
            raise RungNotMappableError(
                "an activity family does not permit this domain")
        return rung

    def rung_for_target(self, target: RungTarget) -> CanonicalRung:
        """The canonical rung for exactly this target, or a refusal."""
        key = (target.domain_key.strip(), target.source_rung_months,
               normalize_milestone_text(target.milestone_text))
        ref = self._by_target.get(key)
        if ref is None:
            raise RungNotFoundError("no such canonical rung")
        entry = self._rungs[ref]
        if not entry.get("mappable"):
            # The recorded refusal from the frozen adapter, replayed. The
            # reason is read from the artifact rather than assumed, so one
            # refusal silently becoming a different one is visible in the
            # drift gate's diff.
            refusal = _REFUSALS.get(str(entry.get("unresolved_reason") or ""))
            if refusal is None:
                raise StaticRungTableError(
                    "the rung table artifact records an unknown refusal")
            raise refusal("the rung's activity families are not reconciled")
        return self._build(ref, entry)

    def next_rung_target(self, domain_key: str, from_months: int
                         ) -> Optional[RungTarget]:
        """The frozen `_step(+1)` result for this floor, READ FROM THE TABLE.

        No month comparison and no ordering. `None` means the Parent engine
        returned None for this input at build time — the top of the declared
        track — which the generator proved by probing well past the ladder's
        last rung and asserting the tail is empty.
        """
        if (domain_key or "").strip() != self._domain:
            # A domain this artifact does not cover. None, not a guess: the
            # caller's own domain gate refuses before this, and a fabricated
            # target would be worse than no target.
            return None
        if isinstance(from_months, bool) or not isinstance(from_months, int):
            raise GoldStandardSourceError("a floor must be an integer")
        if from_months < 0:
            raise GoldStandardSourceError("a floor must not be negative")
        step = self._steps.get(str(from_months))
        if step is None:
            return None
        ref = step.get("target_rung_ref")
        entry = self._rungs.get(ref)
        if entry is None:
            raise StaticRungTableError(
                "a rung table step names an absent rung")
        return RungTarget(domain_key=self._domain,
                          source_rung_months=int(entry["source_rung_months"]),
                          milestone_text=entry["milestone_text"])

    def mappable_rungs_for_domain(self, domain_key: str
                                  ) -> Tuple[CanonicalRung, ...]:
        """Every usable rung on the declared track, months-ordered."""
        if (domain_key or "").strip() != self._domain:
            return ()
        out = []
        for ref, entry in self._rungs.items():
            if entry.get("mappable"):
                out.append(self._build(ref, entry))
        return tuple(sorted(out, key=lambda r: (r.source_rung_months,
                                                r.rung_ref)))


def build_static_rung_source(artifact_path: Optional[Path] = None):
    """The composition seam. Raises rather than returning a degraded source.

    Deliberately NOT returning None on a missing artifact: `None` is the
    composition's "no Gold Standard configured" state, which makes the
    generation route fail closed with a 403 — the exact silent-but-safe outcome
    the inspection found and Option C exists to remove. A browser image built
    without its table is a build defect and should refuse to start.
    """
    return StaticRungTableSource(artifact_path)
