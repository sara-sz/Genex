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

from dataclasses import dataclass, field
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

#: The register of family identifiers the workbook REFERENCES but the taxonomy
#: deliberately does not define. Optional sheet: a workbook without it loads
#: with an empty register, so this read is additive.
_REGISTER_SHEET = "unresolved_family_register"

#: The two founder-approved resolution statuses.
#:
#: `deferred_post_pilot`  — a genuinely missing family. It needs a reviewed
#:                          definition, allowed domains, disciplines and
#:                          activity content before it exists.
#: `ambiguous_unresolved` — more than one existing family could claim it, so
#:                          aliasing one would be a guess recorded as
#:                          provenance. Left unresolved on purpose.
#:
#: BOTH mean "not mappable". The distinction is about WHY, and it exists so a
#: consumer can tell an intentional product deferral from a data defect.
#: Neither status makes a family resolvable — see `_load`, which refuses a
#: register entry that is also a family or an alias.
STATUS_DEFERRED_POST_PILOT = "deferred_post_pilot"
STATUS_AMBIGUOUS_UNRESOLVED = "ambiguous_unresolved"

RESOLUTION_STATUSES: FrozenSet[str] = frozenset({
    STATUS_DEFERRED_POST_PILOT, STATUS_AMBIGUOUS_UNRESOLVED,
})

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
class UnresolvedFamily:
    """One family identifier the taxonomy deliberately does not define.

    Carries WHY it is unresolved, so a consumer can distinguish an intentional
    product deferral from a taxonomy defect. It is NOT a family: it has no
    primary domain and no allowed domains, and nothing here makes it mappable.
    """

    key: str
    resolution_status: str
    review_note: str = ""


@dataclass(frozen=True)
class ActivityTaxonomy:
    families: Mapping[str, ActivityFamily]
    aliases: Mapping[str, str]
    #: Keyed by the RAW workbook identifier. Empty when the workbook carries no
    #: register sheet, which keeps this read additive.
    unresolved: Mapping[str, UnresolvedFamily] = field(default_factory=dict)

    def resolve_key(self, family: str) -> str:
        """Apply alias resolution. Unknown keys pass through unchanged."""
        key = (family or "").strip().lower()
        return self.aliases.get(key, key)

    def get(self, family: str) -> Optional[ActivityFamily]:
        """Return the family after alias resolution, or None when unknown."""
        return self.families.get(self.resolve_key(family))

    def unresolved_entry(self, family: str) -> Optional[UnresolvedFamily]:
        """The register entry for an identifier, or None if not registered.

        Deliberately does NOT alias-resolve: the register keys the identifier
        the workbook actually writes, and an alias-resolved key is by definition
        a real family. Looking one up here would always miss, and making it
        succeed would mean a registered key had become mappable.
        """
        return self.unresolved.get((family or "").strip().lower())


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

    unresolved = _load_register(path, families, aliases)
    return ActivityTaxonomy(families=families, aliases=aliases,
                            unresolved=unresolved)


def _load_register(path: Path, families: Mapping[str, ActivityFamily],
                   aliases: Mapping[str, str]
                   ) -> Dict[str, "UnresolvedFamily"]:
    """The register of deliberately-undefined family identifiers.

    ## THE SHEET IS OPTIONAL, THE VALIDATION IS NOT

    A workbook without the sheet yields an empty register, so this read is
    additive and an older workbook still loads. But every row that IS present is
    validated, because the whole point of the register is to be trustworthy
    enough for band selection to act on.

    ## A REGISTER ENTRY MUST NOT BE RESOLVABLE

    Refused when the identifier is already a family or already an alias. Both
    would be contradictions with real consequences: the register exists to say
    "this names nothing the taxonomy defines", and an entry that also resolved
    would let a caller read a resolution status for something that is in fact
    mappable — turning a deferral record into a second, disagreeing answer about
    whether content exists.
    """
    import pandas as pd  # local: same reason as `_load`

    try:
        reg_df = pd.read_excel(path, sheet_name=_REGISTER_SHEET,
                               dtype=str).fillna("")
    except ValueError:
        # No such sheet. An empty register is the honest representation.
        return {}

    register: Dict[str, UnresolvedFamily] = {}
    for row in reg_df.to_dict(orient="records"):
        key = str(row.get("family_key", "")).strip().lower()
        status = str(row.get("resolution_status", "")).strip()
        if not key and not status:
            continue
        if not key:
            raise ActivityTaxonomyError("blank family_key in the unresolved register")
        if key in register:
            raise ActivityTaxonomyError(
                f"duplicate unresolved-register entry for {key!r}")
        if status not in RESOLUTION_STATUSES:
            raise ActivityTaxonomyError(
                f"{key!r}: resolution_status {status!r} is not one of "
                f"{sorted(RESOLUTION_STATUSES)}")
        if key in families:
            raise ActivityTaxonomyError(
                f"{key!r} is registered as unresolved but IS a taxonomy family")
        if key in aliases:
            raise ActivityTaxonomyError(
                f"{key!r} is registered as unresolved but resolves through an "
                "alias")
        register[key] = UnresolvedFamily(
            key=key, resolution_status=status,
            review_note=str(row.get("review_note", "")).strip())
    return register


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


def resolution_status(family: str) -> Optional[str]:
    """Why this identifier is deliberately not a taxonomy family, or None.

    `None` means one of two genuinely different things, and the caller must not
    conflate them:

      * the identifier IS a known family — check `is_known_family` first; or
      * it is unknown AND unregistered, i.e. a taxonomy/data defect.

    This function never makes a family mappable and never widens
    `allowed_domains`: a registered identifier still has no allowed domains, so
    `allowed_domains()` keeps returning `None` for it and every existing
    mappability check behaves exactly as before.
    """
    entry = get_taxonomy().unresolved_entry(family)
    return None if entry is None else entry.resolution_status


def unresolved_register() -> Mapping[str, UnresolvedFamily]:
    """The whole register, keyed by raw workbook identifier. Read-only."""
    return get_taxonomy().unresolved
