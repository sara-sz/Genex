"""pilot_backend/integration/gold_standard_source.py — the canonical-rung port.

0.5E-A created `CanonicalRung` and made allocation refuse any clinical goal
without a valid anchor. It did NOT create a way to obtain a rung: outside
tests, nothing in production ever constructed one, so no real suggestion could
be anchored. This port is that missing seam, declared the same way
`ParentSessionSource` is — a Protocol here, a live adapter in
`pilot_runtime/integration/`.

## Why a port and not a table

The Parent Gold Standard workbook and the activity-family taxonomy ARE the
source of truth for which rungs exist and which activity families may serve
them. `pilot_backend` cannot read either: they live in the `genex-parent`
namespace, which is not importable from the repository root where the pilot
suite runs, and reaching across would couple the pilot's release to Parent's.

The obvious alternative was to restate the SLP rungs here as data and pin them
with a test. That was rejected: a second handwritten copy of clinical content
is exactly the mirror this architecture exists to avoid, and a mirror pinned by
a literal-versus-literal test only diverges more quietly, not less. So the
rungs stay in one place and cross the boundary as explicit, validated input.

## What an adapter is allowed to do, and what it may never do

An adapter TRANSLATES. It reads the workbook and the taxonomy and assembles a
`CanonicalRung` out of what it finds there. It may not invent a milestone, pick
a different rung than the one asked for, or supply an activity family the
taxonomy does not define.

That last point is the reason `rung_for_target` RAISES rather than returning a
partial rung. A workbook rung can name an activity family this taxonomy has no
entry for — before Scenario C that was true of 24 of the 30 family values the
Talking & Communicating rungs reference. An `ActivityFamilyBinding` cannot even
be constructed for such a family, because it requires the family's allowed
domains and there are none to read. The adapter therefore has exactly two
honest options: refuse, or silently drop the family and hand back a rung whose
bindings no longer match the workbook. Dropping would make
`is_activity_mappable` describe a rung nobody authored — mappable precisely
BECAUSE the inconvenient family vanished. So it refuses.

## Read-only, structurally

Neither method writes. There is no `save`, `register`, `upsert` or `delete`
here, so an adapter satisfying this protocol cannot modify the workbook or the
taxonomy even by mistake: the capability is absent from the contract rather
than merely unused. The same argument `ParentSessionSource` makes about Parent
session blobs.

## Errors carry no content

Both errors declare `PHI_SAFE_MESSAGE`. A lookup failure would otherwise be
the natural place to quote the milestone text and the domain back to the
caller, and milestone text is clinical content. The caller already knows what
it asked for; the error says only that the answer is no.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Tuple, runtime_checkable

from ..domain.canonical_rung import CanonicalRung


class GoldStandardSourceError(Exception):
    """Base for every refusal from a canonical-rung source."""

    PHI_SAFE_MESSAGE = True


class RungNotFoundError(GoldStandardSourceError):
    """No rung in the Gold Standard matches the requested target.

    Raised rather than returning None because a caller asking for a specific
    (domain, months, milestone) triple is asserting that it exists. A missing
    rung means the caller's idea of the workbook and the workbook itself have
    diverged, which is a defect, not an expected empty result.
    """


class RungNotMappableError(GoldStandardSourceError):
    """The rung exists, but at least one activity family does not resolve.

    Distinct from `RungNotFoundError` on purpose: "this target is not in the
    Gold Standard" and "this target is real but its activity families are not
    reconciled yet" need different responses. The first is a bug in the
    caller; the second is known, expected content work, and the correct
    handling is to leave the domain unanchored so a clinician reviews it.
    """


class RungTrackUndeclaredError(GoldStandardSourceError):
    """The rung exists and resolves, but no functional-baseline track claims it.

    `track_ref` is computed from a track's DECLARED subdomains and families —
    `BaselineArea.track_subdomains` and `EntryChoice.track_families`. Only two
    of the six Talking & Communicating subdomains appear in a declared track
    (`expressive_language` and `early_vocalization_and_babbling`), because the
    functional baseline asks about those and routes on them.

    For a rung outside any declared track there is no track to hash. The
    adapter could fall back to the rung's own subdomain and produce a
    plausible-looking `track_ref`, and that is precisely why this error
    exists: such an identifier would be a structure the Parent brain never
    declared, minted by the pilot and then frozen into an immutable anchor.
    Refusing keeps track identity something Parent owns.
    """


@dataclass(frozen=True)
class RungTarget:
    """The identity of one Gold Standard rung, and nothing else.

    Exactly the three fields `compute_rung_ref` hashes. A target carries no
    child id, no age, no diagnosis and no free text, so a lookup cannot become
    a channel for anything about a particular child.
    """

    domain_key: str
    source_rung_months: int
    milestone_text: str

    def __post_init__(self) -> None:
        if not (self.domain_key or "").strip():
            raise GoldStandardSourceError("a rung target requires a domain")
        if not isinstance(self.source_rung_months, int) or \
                isinstance(self.source_rung_months, bool):
            raise GoldStandardSourceError(
                "a rung target requires integer months")
        if self.source_rung_months <= 0:
            raise GoldStandardSourceError(
                "a rung target requires positive months")
        if not (self.milestone_text or "").strip():
            raise GoldStandardSourceError(
                "a rung target requires a milestone text")


@runtime_checkable
class GoldStandardRungSource(Protocol):
    """Obtain canonical rungs from the real Gold Standard. No write exists."""

    def rung_for_target(self, target: RungTarget) -> CanonicalRung:
        """The canonical rung for exactly this target.

        Raises `RungNotFoundError` when the Gold Standard has no such rung,
        `RungNotMappableError` when it has one whose activity families do not
        all resolve in the taxonomy, and `RungTrackUndeclaredError` when no
        functional-baseline track declares the rung's subdomain.

        It never returns the nearest rung, a rung at an adjacent month, or a
        rung with a reduced family set. An implementation that substituted
        would be writing provenance for a target the clinician never chose.
        """
        ...

    def mappable_rungs_for_domain(self, domain_key: str
                                  ) -> Tuple[CanonicalRung, ...]:
        """Every rung in this domain that resolves completely, months-ordered.

        Deliberately excludes rungs that would raise `RungNotMappableError`:
        this is the set a planner may actually choose from, so returning an
        unmappable rung here would push the refusal to a later and less
        informative point.
        """
        ...


class InMemoryGoldStandardRungSource:
    """Explicit rungs for `pilot_backend` tests and fictional runs.

    Not a stand-in for a missing adapter — the live one reads the real
    workbook from `pilot_runtime/integration/`. This exists so the
    `pilot_backend` suite stays dependency-pure (no pandas, no workbook, no
    `genex-parent` on the path), the same relationship
    `InMemoryParentSessionSource` has to the GCS adapter.

    `unmappable` holds targets the fixture should refuse as not-reconciled, so
    a test can exercise the fail-closed path without needing a rung whose
    families genuinely do not resolve.
    """

    def __init__(self, rungs: Tuple[CanonicalRung, ...] = (),
                 unmappable: Tuple[RungTarget, ...] = ()) -> None:
        self._rungs = tuple(rungs)
        self._unmappable = tuple(unmappable)

    @staticmethod
    def _key(domain_key: str, months: int, milestone: str) -> Tuple[str, int, str]:
        return ((domain_key or "").strip(), months, " ".join(
            (milestone or "").split()).casefold())

    def rung_for_target(self, target: RungTarget) -> CanonicalRung:
        wanted = self._key(target.domain_key, target.source_rung_months,
                           target.milestone_text)
        for blocked in self._unmappable:
            if self._key(blocked.domain_key, blocked.source_rung_months,
                         blocked.milestone_text) == wanted:
                raise RungNotMappableError(
                    "the rung's activity families are not reconciled")
        for rung in self._rungs:
            if self._key(rung.domain_key, rung.source_rung_months,
                         rung.milestone_text) == wanted:
                return rung
        raise RungNotFoundError("no such canonical rung")

    def mappable_rungs_for_domain(self, domain_key: str
                                  ) -> Tuple[CanonicalRung, ...]:
        wanted = (domain_key or "").strip()
        return tuple(sorted(
            (r for r in self._rungs
             if r.domain_key == wanted and r.is_activity_mappable),
            key=lambda r: (r.source_rung_months, r.rung_ref)))


def try_rung_for_target(source: GoldStandardRungSource, target: RungTarget
                        ) -> Optional[CanonicalRung]:
    """The rung, or None when it is absent or not reconciled.

    A convenience for the generation path, which must proceed over the other
    domains when one cannot be anchored. It swallows only the two modelled
    refusals — any other exception is a real fault and keeps propagating, so
    this cannot quietly turn a broken workbook read into "no rung today".

    Returning None here is what leaves a suggestion UNANCHORED, which is the
    fail-closed outcome: an unanchored goal is refused by `allocate_goal`, so
    the gap surfaces to a clinician instead of being filled by a guess.
    """
    try:
        return source.rung_for_target(target)
    except (RungNotFoundError, RungNotMappableError,
            RungTrackUndeclaredError):
        return None
