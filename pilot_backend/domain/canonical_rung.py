"""0.5E-A — the canonical rung: a provenance-backed developmental target.

A `CanonicalRung` names ONE rung of the Parent Brain's developmental ladder.
It is the only thing in this package that can make a clinical goal eligible to
drive weekly activity generation.

## This module computes an identifier. It does not establish canonicity.

`rung_ref` is a hash over three supplied fields. Recomputing it proves that
the fields and the id AGREE — nothing more. It does **not** prove the rung
exists in the Gold Standard, because `pilot_backend` cannot read the Gold
Standard and must not try: the workbook, the ladder and the activity taxonomy
all live in the frozen `genex-parent` package, and `goal_vocabulary` already
documents why reaching across is refused.

Canonicity is therefore a TRUST BOUNDARY, not a calculation. It is established
by the rule enforced in `goals/service.py`: an anchor may only be minted from
canonical provenance that the suggestion-generation boundary already persisted.
A rung may never arrive from a browser request. The hash is an integrity check
on top of that rule, and is worthless without it.

## Why these three fields and no others

    domain_key          one of the seven canonical domains, already mirrored
    source_rung_months  the canonical month rung, a clean int in the workbook
    milestone_text      the canonical skill text

Measured against the frozen workbook: 369 rows collapse to 163 distinct
milestones, because a ROW is a bridge step and a MILESTONE is the rung. The
Parent brain's own `_rows_for_domain` dedupes on `(months, milestone)`, so
this is the granularity the ladder already uses — keying on rows would mint up
to five ids for one rung.

Excluded deliberately:

    subdomain           functionally determined (0 conflicts across all 163),
                        so it adds no uniqueness and would break the id if a
                        subdomain were ever relabelled
    activity_family     NOT functionally determined — two canonical rungs map
                        to two families each, which is why families are a
                        multi-value binding rather than part of identity
    bridge_step*        the wrong granularity, per the 369 -> 163 collapse
    parent_explanation  display content
    category,
    legacy_category_key display and legacy

## Stability

Stable across FORMATTING: Unicode form, typographic punctuation, whitespace
and case are normalized away before hashing. NOT stable across semantic
REWORDING — a reworded milestone is a new rung identity, which is the approved
and honest reading, since the text is the only semantic identity available.
`taxonomy_version` travels with every anchor so historical provenance stays
legible after the workbook moves on.

Order independence is structural: nothing here reads a row index, a sort
position or a sequence number.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Mapping, Sequence, Tuple

from .goal_vocabulary import require_canonical_domain

#: Prefix on every rung identifier. A future scheme change gets a new prefix
#: rather than silently producing different digests under the same name.
RUNG_SCHEME = "rung1:"

#: Mixed into the digest so a hash computed for some other purpose over the
#: same three fields cannot be mistaken for a rung id.
RUNG_HASH_TAG = "genex-rung-v1"

#: Prefix on a track identifier. Same reasoning as `RUNG_SCHEME`.
TRACK_SCHEME = "track1:"
TRACK_HASH_TAG = "genex-track-v1"

#: ASCII unit separator. Cannot occur in any canonical field, which makes the
#: joined string injective — without it, ("a", "b|c") and ("a|b", "c") would
#: hash identically.
_UNIT_SEP = "\x1f"

#: Typographic characters mapped to their ASCII equivalents before hashing.
#:
#: This is what buys stability against display formatting. The frozen workbook
#: currently contains exactly ONE non-ASCII character — an en dash in "2-3
#: minutes" — but a later copy-edit replacing a hyphen with an en dash, or
#: straight quotes with curly ones, must not change a rung's identity.
_TYPOGRAPHIC: Mapping[str, str] = {
    "‐": "-", "‑": "-", "‒": "-",  # hyphen variants
    "–": "-", "—": "-", "―": "-",  # en/em/horizontal dash
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "…": "...",
    " ": " ", " ": " ", " ": " ", " ": " ",
}


class RungError(ValueError):
    """A rung could not be formed from the supplied fields.

    PHI-safe: names the field at fault and never a child. Milestone text is
    canonical content, not child-specific, so quoting a domain or a month is
    safe; the text itself is never interpolated into a message.
    """

    PHI_SAFE_MESSAGE = True


def normalize_milestone_text(text: str) -> str:
    """Canonical form of a milestone string, for hashing only.

    Never stored in place of the original and never shown to anyone: the
    anchor keeps the real `milestone_text` for display and audit.

    Order is load-bearing. NFC first, so composed and decomposed accents agree
    before anything else looks at the characters. Typographic folding next,
    while the string is still in a known form. Whitespace last, because the
    typographic table maps several space-like characters to ASCII space and
    those must then collapse with their neighbours.
    """
    if not isinstance(text, str):
        raise RungError("milestone text must be a string")
    normalized = unicodedata.normalize("NFC", text)
    normalized = "".join(_TYPOGRAPHIC.get(ch, ch) for ch in normalized)
    normalized = " ".join(normalized.split())
    normalized = normalized.casefold()
    if not normalized:
        raise RungError("milestone text must not be blank")
    return normalized


def compute_rung_ref(domain_key: str, source_rung_months: int,
                     milestone_text: str) -> str:
    """The deterministic identifier for one canonical rung."""
    domain = require_canonical_domain(domain_key)
    if isinstance(source_rung_months, bool) or not isinstance(
            source_rung_months, int):
        raise RungError("source_rung_months must be an integer")
    if source_rung_months < 0:
        raise RungError("source_rung_months must not be negative")
    payload = _UNIT_SEP.join((
        RUNG_HASH_TAG, domain, str(source_rung_months),
        normalize_milestone_text(milestone_text),
    ))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{RUNG_SCHEME}{digest}"


def compute_track_ref(domain_key: str, track_subdomains: Sequence[str],
                      track_families: Sequence[str]) -> str:
    """The deterministic identifier for a declared skill track.

    Derived from the components the Parent brain already declares —
    `BaselineArea.track_subdomains` and `EntryChoice.track_families` — rather
    than from a free-form label, because a track is only meaningful as the set
    of rungs it admits.

    Daily Living is the reason `track_families` participates. Its declared
    subdomain still spans independent routines (self-feeding versus
    dressing/fastening), so two Daily Living tracks can share a subdomain and
    differ only by family. Hashing the subdomains alone would collapse them
    and reintroduce exactly the cross-routine bracketing the Parent brain
    removed.

    Both component lists are deduplicated and sorted before hashing, so the
    identifier cannot depend on the order a caller happened to supply.
    """
    domain = require_canonical_domain(domain_key)
    subdomains = _clean_tuple(track_subdomains, "track_subdomains")
    families = _clean_tuple(track_families, "track_families")
    if not subdomains:
        raise RungError("a track requires at least one subdomain")
    payload = _UNIT_SEP.join((
        TRACK_HASH_TAG, domain,
        ",".join(subdomains), ",".join(families),
    ))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{TRACK_SCHEME}{digest}"


def _clean_tuple(values: Sequence[str], label: str) -> Tuple[str, ...]:
    """Deduplicated, sorted, blank-free. Order of input is irrelevant."""
    if isinstance(values, str) or not isinstance(values, (tuple, list)):
        raise RungError(f"{label} must be a sequence of strings")
    cleaned = set()
    for value in values:
        if not isinstance(value, str):
            raise RungError(f"{label} must contain only strings")
        stripped = value.strip()
        if stripped:
            cleaned.add(stripped)
    return tuple(sorted(cleaned))


@dataclass(frozen=True)
class ActivityFamilyBinding:
    """One activity family this rung may be served by, and where it is valid.

    `allowed_domains` is the family's canonical domain set from the Parent
    activity taxonomy — `primary_domain` plus `secondary_domains`. It travels
    with the binding because `pilot_backend` cannot read the taxonomy: without
    it, "is every family allowed for this domain?" would be unanswerable here
    and the mappability rule would be a claim rather than a check.
    """

    family_ref: str
    allowed_domains: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.family_ref, str) or not self.family_ref.strip():
            raise RungError("an activity family binding requires a family ref")
        object.__setattr__(self, "family_ref", self.family_ref.strip())
        object.__setattr__(self, "allowed_domains",
                           _clean_tuple(self.allowed_domains,
                                        "allowed_domains"))
        if not self.allowed_domains:
            raise RungError(
                "an activity family binding requires its allowed domains")
        for domain in self.allowed_domains:
            require_canonical_domain(domain)

    def permits(self, domain_key: str) -> bool:
        return domain_key in self.allowed_domains


@dataclass(frozen=True)
class CanonicalRung:
    """An immutable, provenance-backed developmental target.

    Every field is canonical content supplied by the generation boundary. The
    two derived identifiers are recomputed in `__post_init__` and a mismatch
    is refused, so a rung cannot be constructed whose id disagrees with its
    own fields.

    `is_activity_mappable` is a PROPERTY, never a stored field. That is
    deliberate: a boolean written beside the anchor could drift away from the
    state it claims to summarise, and nothing would notice. Derived from
    validated state, it cannot.
    """

    domain_key: str
    source_rung_months: int
    milestone_text: str
    subdomain: str
    family_bindings: Tuple[ActivityFamilyBinding, ...]
    track_subdomains: Tuple[str, ...]
    track_families: Tuple[str, ...]
    rung_ref: str
    track_ref: str
    taxonomy_version: str
    baseline_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain_key",
                           require_canonical_domain(self.domain_key))
        if isinstance(self.source_rung_months, bool) or not isinstance(
                self.source_rung_months, int):
            raise RungError("source_rung_months must be an integer")
        if not isinstance(self.milestone_text, str) or not self.milestone_text.strip():
            raise RungError("a rung requires milestone text")
        object.__setattr__(self, "milestone_text", self.milestone_text.strip())
        if not isinstance(self.subdomain, str) or not self.subdomain.strip():
            raise RungError("a rung requires a subdomain")
        object.__setattr__(self, "subdomain", self.subdomain.strip())
        for label in ("taxonomy_version", "baseline_version"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value.strip():
                raise RungError(f"a rung requires {label}")
            object.__setattr__(self, label, value.strip())

        # Families: deduplicated by ref and ordered by ref. There is no
        # canonical family ordering in the Parent taxonomy, so sorted order is
        # the only ordering that cannot depend on workbook row order.
        if not isinstance(self.family_bindings, (tuple, list)):
            raise RungError("family_bindings must be a sequence")
        by_ref = {}
        for binding in self.family_bindings:
            if not isinstance(binding, ActivityFamilyBinding):
                raise RungError(
                    "family_bindings must contain ActivityFamilyBinding values")
            existing = by_ref.get(binding.family_ref)
            if existing is not None and existing != binding:
                # Same family, two different allowed-domain sets. Refused
                # rather than merged: merging would invent a permission.
                raise RungError(
                    "family_bindings disagree about one family's allowed domains")
            by_ref[binding.family_ref] = binding
        object.__setattr__(self, "family_bindings",
                           tuple(by_ref[ref] for ref in sorted(by_ref)))

        object.__setattr__(self, "track_subdomains",
                           _clean_tuple(self.track_subdomains,
                                        "track_subdomains"))
        object.__setattr__(self, "track_families",
                           _clean_tuple(self.track_families,
                                        "track_families"))

        # The integrity check. Proves the fields and the ids agree; it does
        # NOT prove the rung is in the Gold Standard — see the module
        # docstring. The trust boundary is in goals/service.py.
        expected_rung = compute_rung_ref(self.domain_key,
                                         self.source_rung_months,
                                         self.milestone_text)
        if self.rung_ref != expected_rung:
            raise RungError("rung_ref does not match the supplied rung fields")
        expected_track = compute_track_ref(self.domain_key,
                                           self.track_subdomains,
                                           self.track_families)
        if self.track_ref != expected_track:
            raise RungError("track_ref does not match the supplied track fields")

    @property
    def activity_family_refs(self) -> Tuple[str, ...]:
        """Every valid family for this rung, deduplicated and ordered.

        No primary family is designated. 0.5E-A deliberately refuses to
        choose: two canonical rungs legitimately map to two families each, and
        the weekly planner must make that choice explicitly with both options
        in front of it.
        """
        return tuple(binding.family_ref for binding in self.family_bindings)

    @property
    def is_activity_mappable(self) -> bool:
        """Whether this rung may drive weekly activity generation.

        True only when at least one family exists AND every family is valid
        for this rung's canonical domain. A single family that does not permit
        the domain makes the whole rung unmappable rather than being dropped —
        dropping it would silently narrow a mapping nobody reviewed.
        """
        if not self.family_bindings:
            return False
        return all(binding.permits(self.domain_key)
                   for binding in self.family_bindings)

    @staticmethod
    def build(*, domain_key: str, source_rung_months: int,
              milestone_text: str, subdomain: str,
              family_bindings: Sequence[ActivityFamilyBinding],
              track_subdomains: Sequence[str],
              track_families: Sequence[str] = (),
              taxonomy_version: str, baseline_version: str) -> "CanonicalRung":
        """Construct a rung, computing both identifiers from the fields.

        The only constructor callers should use. Passing the ids in by hand is
        possible but pointless — `__post_init__` recomputes and compares them.
        """
        return CanonicalRung(
            domain_key=domain_key,
            source_rung_months=source_rung_months,
            milestone_text=milestone_text,
            subdomain=subdomain,
            family_bindings=tuple(family_bindings),
            track_subdomains=tuple(track_subdomains),
            track_families=tuple(track_families),
            rung_ref=compute_rung_ref(domain_key, source_rung_months,
                                      milestone_text),
            track_ref=compute_track_ref(domain_key, track_subdomains,
                                        track_families),
            taxonomy_version=taxonomy_version,
            baseline_version=baseline_version,
        )
