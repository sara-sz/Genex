"""Parent 2.4 activity-family taxonomy — allowed developmental domains per family.

The founder-reviewed workbook is the SINGLE SOURCE OF TRUTH:

    genex-parent/data/parent_2_4/activity_family_taxonomy_v1.xlsx

## Why this module exists

The pre-2.4 validator carried a hard-coded `FAMILY_TO_CATEGORY` dict mapping each
activity family to exactly ONE legacy domain. That model cannot express reality:
`buttoning_fasteners` is Daily Living *and* Fine Motor; `conversation_turn_taking`
is Talking & Communicating *and* Social & Emotional. 27 of the 56 families carry
a secondary domain.

Worse, once the Brain became seven-domain-native (PARENT-0.3B) the legacy values
stopped matching canonical `category_key`s, so **45 of 56 families produced a
critical `activity_family_category_mismatch` and blocked every activity** that
used them.

## Allowed domains are DERIVED, never stored twice

    allowed_domains(family) == {primary_domain} | set(secondary_domains)

There is deliberately no second allowed-domain map to drift out of sync with the
workbook. The workbook holds primary + secondary; this module computes the union.

## Scope — read this before assuming coverage

This taxonomy covers the **56 families that the retired `FAMILY_TO_CATEGORY`
dictionary already knew**. It is NOT a complete catalogue of every activity
family: the Gold Standard alone contains 128 distinct `activity_family` values,
and `activity_engine` can emit families outside both sets.

A family absent from this taxonomy is **unknown**, and unknown families stay
PERMISSIVE — exactly as they were before, when `FAMILY_TO_CATEGORY.get(fam)`
returned `None` and no warning was raised. Widening the validator to block
unknown families would be a new behaviour and is explicitly out of scope here.

## Fail closed at load, permissive at lookup

Load-time validation raises on structurally invalid data (unknown domain,
duplicate key, dangling alias, ...). Lookup-time behaviour for a *legitimately
unknown* family is permissive. Those are different concerns and are handled
differently on purpose.

## Import weight

pandas/openpyxl are imported INSIDE the loader function so that importing
`parent_taxonomy` remains stdlib-only — a property the Parent 2.4 CI asserts by
blocking those modules and importing anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, FrozenSet, Mapping, Optional, Tuple

from .domains import BY_KEY

#: Distinct from `domains.TAXONOMY_VERSION` on purpose — the domain vocabulary
#: and the activity-family classification version independently, and a shared
#: name would silently shadow one of them at package level.
ACTIVITY_TAXONOMY_VERSION = "activity_family_taxonomy_v1"

_WORKBOOK_RELPATH = Path("data") / "parent_2_4" / "activity_family_taxonomy_v1.xlsx"
_FAMILIES_SHEET = "activity_families"
_ALIASES_SHEET = "activity_family_aliases"

#: Vocabularies the workbook may use. Sourced from its own `professional_roles`
#: sheet; restated here only so load-time validation can fail closed without a
#: circular read.
CLINICAL_DISCIPLINES: FrozenSet[str] = frozenset({"SLP", "OT", "PT"})
EDUCATIONAL_ROLES: FrozenSet[str] = frozenset({
    "Early Intervention Specialist",
    "ECSE / Special Education Teacher",
})

#: Expected family count. A guard against a silently truncated workbook, not a
#: cap — update deliberately if the founder adds families.
EXPECTED_FAMILY_COUNT = 56


class ActivityTaxonomyError(ValueError):
    """The activity-family taxonomy source is structurally invalid."""


@dataclass(frozen=True)
class ActivityFamily:
    """One founder-reviewed activity family."""

    key: str
    primary_domain: str
    secondary_domains: Tuple[str, ...]
    clinical_disciplines: Tuple[str, ...]
    educational_roles: Tuple[str, ...]
    taxonomy_note: str = ""

    @property
    def allowed_domains(self) -> FrozenSet[str]:
        """Derived: primary plus any secondaries. Never stored separately."""
        return frozenset((self.primary_domain, *self.secondary_domains))


@dataclass(frozen=True)
class ActivityTaxonomy:
    families: Mapping[str, ActivityFamily]
    aliases: Mapping[str, str]

    def resolve_key(self, family: str) -> str:
        """Apply alias resolution. Unknown keys pass through unchanged."""
        key = (family or "").strip().lower()
        return self.aliases.get(key, key)

    def get(self, family: str) -> Optional[ActivityFamily]:
        """Return the family after alias resolution, or None when unknown."""
        return self.families.get(self.resolve_key(family))


def _split(cell: object) -> Tuple[str, ...]:
    """Split a semicolon list, stripping delimiter whitespace only.

    Whitespace around the delimiter is formatting; the tokens themselves are
    never rewritten, so a semantic variant surfaces as a validation error rather
    than being silently normalised into something valid.
    """
    raw = "" if cell is None else str(cell)
    if raw.strip().lower() in ("", "nan"):
        return ()
    return tuple(t.strip() for t in raw.split(";") if t.strip())


def _find_workbook() -> Path:
    here = Path(__file__).resolve().parent          # parent_taxonomy/
    app_dir = here.parent                            # genex-parent/
    for candidate in (app_dir / _WORKBOOK_RELPATH, Path.cwd() / _WORKBOOK_RELPATH):
        if candidate.exists():
            return candidate.resolve()
    raise ActivityTaxonomyError(
        f"activity-family taxonomy workbook not found; expected at "
        f"{app_dir / _WORKBOOK_RELPATH}"
    )


def _load(path: Path) -> ActivityTaxonomy:
    import pandas as pd  # local: keeps `import parent_taxonomy` stdlib-only

    fam_df = pd.read_excel(path, sheet_name=_FAMILIES_SHEET, dtype=str).fillna("")
    alias_df = pd.read_excel(path, sheet_name=_ALIASES_SHEET, dtype=str).fillna("")

    families: Dict[str, ActivityFamily] = {}
    for row in fam_df.to_dict(orient="records"):
        key = str(row.get("activity_family_key", "")).strip().lower()
        if not key:
            raise ActivityTaxonomyError("blank activity_family_key")
        if key in families:
            raise ActivityTaxonomyError(f"duplicate activity_family_key: {key!r}")

        primary = str(row.get("primary_domain", "")).strip()
        if not primary:
            raise ActivityTaxonomyError(f"{key!r}: blank primary_domain")
        if primary not in BY_KEY:
            raise ActivityTaxonomyError(
                f"{key!r}: primary_domain {primary!r} is not a canonical Parent 2.4 domain"
            )

        secondary = _split(row.get("secondary_domains"))
        for dom in secondary:
            if dom not in BY_KEY:
                raise ActivityTaxonomyError(
                    f"{key!r}: secondary domain {dom!r} is not canonical"
                )
            if dom == primary:
                raise ActivityTaxonomyError(
                    f"{key!r}: primary_domain {primary!r} repeated in secondary_domains"
                )
        if len(set(secondary)) != len(secondary):
            raise ActivityTaxonomyError(f"{key!r}: duplicate secondary domain")

        clinical = _split(row.get("clinical_disciplines"))
        for disc in clinical:
            if disc not in CLINICAL_DISCIPLINES:
                raise ActivityTaxonomyError(
                    f"{key!r}: unknown clinical discipline {disc!r}"
                )

        educational = _split(row.get("educational_developmental_roles"))
        for role in educational:
            if role not in EDUCATIONAL_ROLES:
                raise ActivityTaxonomyError(
                    f"{key!r}: unknown educational/developmental role {role!r}"
                )

        families[key] = ActivityFamily(
            key=key,
            primary_domain=primary,
            secondary_domains=secondary,
            clinical_disciplines=clinical,
            educational_roles=educational,
            taxonomy_note=str(row.get("taxonomy_note", "")).strip(),
        )

    if len(families) != EXPECTED_FAMILY_COUNT:
        raise ActivityTaxonomyError(
            f"expected {EXPECTED_FAMILY_COUNT} activity families, found {len(families)}"
        )

    aliases: Dict[str, str] = {}
    for row in alias_df.to_dict(orient="records"):
        legacy = str(row.get("legacy_key", "")).strip().lower()
        canonical = str(row.get("canonical_key", "")).strip().lower()
        if not legacy and not canonical:
            continue
        if not legacy or not canonical:
            raise ActivityTaxonomyError(f"malformed alias row: {legacy!r} -> {canonical!r}")
        if legacy in aliases:
            raise ActivityTaxonomyError(f"duplicate alias for {legacy!r}")
        if canonical not in families:
            raise ActivityTaxonomyError(
                f"alias {legacy!r} points at {canonical!r}, which is not a taxonomy family"
            )
        if legacy in families:
            raise ActivityTaxonomyError(
                f"{legacy!r} is both an alias and a taxonomy family"
            )
        if canonical in aliases:
            # Single-hop only: an alias target must not itself be an alias.
            raise ActivityTaxonomyError(f"alias chain/cycle detected at {canonical!r}")
        aliases[legacy] = canonical

    # Second pass: catch a cycle introduced by row ordering.
    for legacy, canonical in aliases.items():
        if canonical in aliases:
            raise ActivityTaxonomyError(f"alias chain/cycle detected: {legacy!r} -> {canonical!r}")

    return ActivityTaxonomy(families=families, aliases=aliases)


@lru_cache(maxsize=1)
def _cached(path_str: str) -> ActivityTaxonomy:
    return _load(Path(path_str))


def get_taxonomy() -> ActivityTaxonomy:
    """Load (and cache) the founder-reviewed activity-family taxonomy."""
    return _cached(str(_find_workbook()))


def reload_cache() -> None:
    """Drop the cached taxonomy. Tests only."""
    _cached.cache_clear()


def allowed_domains(family: str) -> Optional[FrozenSet[str]]:
    """Canonical domains this family may legitimately serve.

    Returns `None` for an UNKNOWN family — the caller must treat that as
    permissive, preserving the pre-2.4 behaviour where an unmapped family raised
    no warning. See the module docstring on scope.
    """
    entry = get_taxonomy().get(family)
    return None if entry is None else entry.allowed_domains


def is_known_family(family: str) -> bool:
    return get_taxonomy().get(family) is not None


def resolve_family_key(family: str) -> str:
    """Alias-resolved key, e.g. `helper_context` -> `helper_role_chores`."""
    return get_taxonomy().resolve_key(family)
