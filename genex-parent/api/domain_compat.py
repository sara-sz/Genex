"""api/domain_compat.py — PARENT-0.3C legacy domain read compatibility.

Historical Beta 2.3 records store four-domain keys. The Parent 2.4 Brain is
seven-domain native and a legacy key yields **nothing** from it — no questions,
no activities, silently. This module is the one-directional read projection at
the boundary.

## One-directional, read-time only

    stored legacy value  ──project──▶  canonical domain (in memory)

Nothing here writes. Historical records are **never** rewritten to look
canonical: `project_domain` returns a result object and leaves its input
untouched. New Parent 2.4 writes are canonical from the start and never pass
through the legacy branches at all.

## The mapping, and the one case that has no mapping

    language_and_communication  ->  talking_and_communicating   (1:1)
    cognitive                   ->  learning_and_thinking       (1:1)
    social_and_emotional        ->  social_and_emotional        (unchanged)
    movement_and_physical       ->  AMBIGUOUS

`movement_and_physical` spanned Fine Motor, Gross Motor **and** Daily Living.
There is no correct single answer, so it is never mapped mechanically. It is
resolved only from deterministic evidence carried by the record itself:

  1. `subdomain`       — the Gold Standard subdomain, via `parent_taxonomy.subdomain_map`
  2. `activity_family` — via the founder-reviewed 56-family taxonomy

Evidence is required to land inside the three canonical successors of legacy
Movement. If a record's evidence points somewhere else, that is a contradiction
between the stored domain and the stored evidence — reported as CONTRADICTORY
rather than quietly preferring one of them.

With no usable evidence the result is **UNRESOLVED**. Callers must carry that
state rather than substituting a domain. Guessing here would silently
re-classify a child's history.

## Why a result object rather than a plain string

A plain string forces every caller to invent a sentinel for "cannot tell", and
the convenient sentinel is a real domain. `DomainProjection` makes the
unresolved case impossible to read as a successful projection by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Optional

from parent_taxonomy.domains import (
    BY_KEY as CANONICAL_DOMAINS,
    LEGACY_AMBIGUOUS,
    LEGACY_DOMAIN_KEYS,
    display_for,
    resolve_legacy_domain,
)
from parent_taxonomy.subdomain_map import SubdomainMappingError
from parent_taxonomy.subdomain_map import resolve as resolve_subdomain

#: The three canonical successors of legacy `movement_and_physical`.
#: Derived, not restated, so it cannot drift from the taxonomy.
MOTOR_SUCCESSORS: FrozenSet[str] = frozenset(LEGACY_AMBIGUOUS["movement_and_physical"])

#: How a projection was obtained. Part of the return value so callers and logs
#: can distinguish "already canonical" from "recovered from evidence".
SOURCE_CANONICAL = "canonical"          # already a Parent 2.4 domain
SOURCE_LEGACY_RENAME = "legacy_rename"  # deterministic 1:1 legacy mapping
SOURCE_SUBDOMAIN = "subdomain"          # ambiguous legacy + subdomain evidence
SOURCE_FAMILY = "activity_family"       # ambiguous legacy + family evidence
SOURCE_UNRESOLVED = "unresolved"        # ambiguous legacy, no usable evidence
SOURCE_CONTRADICTORY = "contradictory"  # evidence disagrees with stored domain


class UnknownStoredDomain(ValueError):
    """The stored value is neither canonical nor a recognised legacy key."""


@dataclass(frozen=True)
class DomainProjection:
    """Result of projecting one stored domain value.

    `domain` is None whenever the projection did not succeed. `resolved` is the
    single flag callers should branch on.
    """

    stored: str
    domain: Optional[str]
    source: str
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.domain is not None

    @property
    def is_unresolved_legacy(self) -> bool:
        """True when this is legacy Movement that could not be resolved."""
        return self.source in (SOURCE_UNRESOLVED, SOURCE_CONTRADICTORY)


def _family_domain(activity_family: str) -> Optional[str]:
    """Primary domain for an activity family, or None when unknown.

    Imported lazily: the taxonomy reads a workbook, and most projections
    (canonical values and 1:1 renames) never need it.
    """
    try:
        from parent_taxonomy.activity_families import get_taxonomy
    except Exception:  # pragma: no cover - taxonomy package always present
        return None
    entry = get_taxonomy().get(activity_family)
    return None if entry is None else entry.primary_domain


def project_domain(
    stored: Any,
    *,
    subdomain: str = "",
    activity_family: str = "",
) -> DomainProjection:
    """Project one stored domain value to a canonical Parent 2.4 domain.

    Pure and read-only — no argument is mutated. Raises `UnknownStoredDomain`
    for values that are neither canonical nor recognised legacy keys, so a typo
    fails loudly instead of becoming an unresolved record.
    """
    key = str(stored or "").strip()
    if not key:
        raise UnknownStoredDomain("stored domain must not be empty")

    # Already canonical — the normal Parent 2.4 path, including new writes.
    if key in CANONICAL_DOMAINS:
        return DomainProjection(key, key, SOURCE_CANONICAL)

    if key not in LEGACY_DOMAIN_KEYS:
        raise UnknownStoredDomain(
            f"{key!r} is neither a canonical Parent 2.4 domain nor a known "
            f"legacy domain key; refusing to guess"
        )

    # Deterministic 1:1 legacy renames. resolve_legacy_domain returns None only
    # for the genuinely ambiguous key, which falls through below.
    renamed = resolve_legacy_domain(key)
    if renamed is not None:
        return DomainProjection(key, renamed, SOURCE_LEGACY_RENAME)

    # --- legacy movement_and_physical: evidence required ---------------------
    # Subdomain is the stronger signal: it comes from the Gold Standard itself
    # and is what the 0.2 migration used to split these rows in the first place.
    sd = (subdomain or "").strip()
    if sd:
        try:
            resolved = resolve_subdomain(sd)
        except SubdomainMappingError:
            resolved = None
        if resolved is not None:
            if resolved in MOTOR_SUCCESSORS:
                return DomainProjection(key, resolved, SOURCE_SUBDOMAIN,
                                        f"subdomain={sd!r}")
            return DomainProjection(
                key, None, SOURCE_CONTRADICTORY,
                f"subdomain={sd!r} resolves to {resolved!r}, which is not a "
                f"successor of movement_and_physical",
            )

    fam = (activity_family or "").strip()
    if fam:
        fam_domain = _family_domain(fam)
        if fam_domain is not None:
            if fam_domain in MOTOR_SUCCESSORS:
                return DomainProjection(key, fam_domain, SOURCE_FAMILY,
                                        f"activity_family={fam!r}")
            return DomainProjection(
                key, None, SOURCE_CONTRADICTORY,
                f"activity_family={fam!r} maps to {fam_domain!r}, which is not "
                f"a successor of movement_and_physical",
            )

    return DomainProjection(
        key, None, SOURCE_UNRESOLVED,
        "legacy movement_and_physical with no subdomain or activity_family "
        "evidence; spans fine_motor, gross_motor and daily_living",
    )


def project_record(record: Dict[str, Any], *, field: str = "domain") -> DomainProjection:
    """Project a stored record's domain using evidence the record already carries.

    The record is NOT modified — this is a read projection. Callers that want a
    canonical view should build a copy from the returned projection.
    """
    return project_domain(
        record.get(field, ""),
        subdomain=str(record.get("subdomain", "") or ""),
        activity_family=str(record.get("activity_family", "") or ""),
    )


def canonical_view(record: Dict[str, Any], *, field: str = "domain") -> Dict[str, Any]:
    """Return a COPY of `record` with `field` projected to canonical.

    An unresolved legacy value is left exactly as stored and flagged, rather
    than replaced by a guess:

        {"domain": "movement_and_physical", "domain_unresolved_legacy": True}
    """
    projection = project_record(record, field=field)
    view = dict(record)
    if projection.resolved:
        view[field] = projection.domain
        view.pop("domain_unresolved_legacy", None)
    else:
        view["domain_unresolved_legacy"] = True
    return view


def canonical_display(domain: Any) -> str:
    """Parent-facing display label for a canonical domain.

    Sourced from `parent_taxonomy.domains`, the single display authority. This
    is deliberately NOT a second label map: the legacy four-area
    `focus_selector.FOCUS_LABELS` remains the public intake surface, while
    anything keyed by a canonical Brain domain resolves its label here.
    """
    key = str(domain or "").strip()
    if key not in CANONICAL_DOMAINS:
        return ""
    return display_for(key)


# ---------------------------------------------------------------------------
# Intake boundary: legacy four-area focus key -> canonical Brain domain
# ---------------------------------------------------------------------------
#
# PARENT-0.3C keeps the four-area focus surface for API compatibility, so the
# API-layer selector can still hand us `movement_and_physical`. That umbrella
# has no single canonical successor, and the Brain returns nothing for it.
#
# Rather than invent a keyword sub-mapping, this asks the ALREADY-APPROVED
# 0.3B ranking (`rank_focus_domains`, driven by the same concern_profile the
# Brain uses everywhere else) which motor successor the parent's own words
# actually point at. No new clinical judgement is introduced here.
#
#   "not walking yet, motor delay"           -> gross_motor
#   "trouble with grasp, cannot hold crayon" -> fine_motor
#   "cannot dress himself, self care delay"  -> daily_living
#
# TEMPORARY. The four-area umbrella is an intake/compatibility shape, not the
# Parent 2.4 taxonomy. See `focus_selector` and docs/PARENT_2_4_API_CONSUMERS.md.


class UnresolvableFocus(ValueError):
    """A legacy focus umbrella could not be resolved to a canonical domain."""


def resolve_focus_for_brain(focus_key: Any, brain_state: Dict[str, Any]) -> str:
    """Resolve an API-layer focus key to the canonical domain the Brain needs.

    Canonical keys and the 1:1 legacy renames resolve without consulting the
    state at all. Only the `movement_and_physical` umbrella needs ranking, and
    it is scored by the Brain's own approved machinery.

    Raises `UnresolvableFocus` rather than returning a plausible-looking domain
    when nothing can be determined — a wrong motor domain here would send a
    child down the wrong plan silently.
    """
    key = str(focus_key or "").strip()
    if not key:
        raise UnresolvableFocus("focus key must not be empty")

    if key in CANONICAL_DOMAINS:
        return key

    renamed = resolve_legacy_domain(key) if key in LEGACY_DOMAIN_KEYS else None
    if renamed is not None:
        return renamed

    if key not in LEGACY_AMBIGUOUS:
        raise UnresolvableFocus(
            f"{key!r} is neither canonical nor a known legacy focus key"
        )

    # The motor umbrella. Ask the approved 0.3B ranking which successor the
    # parent's concern text actually points at.
    from genex_core.interview_engine import rank_focus_domains

    successors = set(LEGACY_AMBIGUOUS[key])
    ranked = [r for r in rank_focus_domains(brain_state) if r["category_key"] in successors]
    if not ranked:
        raise UnresolvableFocus(f"no canonical successor available for {key!r}")

    best = max(ranked, key=lambda r: (r["concern_signal"], r["triage_score"]))
    if best["concern_signal"] <= 0.0 and best["triage_score"] <= 0.0:
        # The umbrella was selected by the API keyword layer but the Brain sees
        # no signal for any successor. Refuse rather than pick the first.
        raise UnresolvableFocus(
            f"{key!r} selected but no canonical successor carries any concern "
            f"or delay signal; refusing to guess between {sorted(successors)}"
        )
    return best["category_key"]


def is_canonical(value: Any) -> bool:
    return str(value or "").strip() in CANONICAL_DOMAINS


def assert_canonical_write(value: Any, *, context: str = "") -> str:
    """Guard for WRITE paths. New Parent 2.4 state must be canonical.

    Read compatibility is deliberately not available here: accepting a legacy
    key on a write would put four-domain vocabulary back into new records,
    which is exactly what the projection exists to avoid.
    """
    key = str(value or "").strip()
    if key not in CANONICAL_DOMAINS:
        where = f" ({context})" if context else ""
        raise UnknownStoredDomain(
            f"refusing to write non-canonical domain {key!r}{where}; "
            f"Parent 2.4 writes must use a canonical domain"
        )
    return key
