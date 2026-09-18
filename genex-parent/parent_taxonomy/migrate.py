"""Parent 2.4 Gold Standard taxonomy migration — deterministic and additive.

Fictional/dev work. No Firestore, no Firebase, no RTM, no real data.

## Input is immutable

The authoritative runtime workbook is the INPUT SNAPSHOT and is never written:

    genex-parent/data/cdc_milestones_with_bridges_family_cleaned_final_app_ready.xlsx
    sha256 c2b6735d9f099c916973c98c1953e7eb1f2ca8e1a60430011f805a3bf9c3487c
    369 rows x 10 columns, sheet `all_with_bridge_family`

The 159 -> 369 bridge builder could not be recovered from the repository or its
history (see the phase doc), so the workbook cannot be regenerated from its
ancestor. That makes preserving it exactly non-negotiable: it is the only copy
of work that cannot currently be rebuilt.

## Additive, not destructive

The migration **adds** a `canonical_domain` column and **keeps** the historical
`category` column untouched. Two reasons:

1. **Provenance.** `category` is the only in-file record of the pre-2.4
   classification. Overwriting it would destroy the lineage evidence that makes
   the re-parenting auditable.
2. **Reversibility.** With both columns present, the migration is verifiable
   row-by-row and trivially reversible; the candidate workbook is a superset of
   the source, so nothing is lost.

Every other column is copied verbatim and verified cell-by-cell.

## Protected columns

    months, category, subdomain, milestone, parent_explanation,
    bridge_step_number, bridge_step, activity_family,
    previous_bridge_step, previous_anchor_age

`verify_candidate` proves all ten are identical between source and candidate,
compared as raw strings so no numeric re-typing can hide a change.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .domains import BY_KEY, DOMAIN_KEYS
from .subdomain_map import SubdomainMappingError, resolve, validate_map

MIGRATION_VERSION = "parent-2.4-migration-v1"

SOURCE_RELPATH = (
    "genex-parent/data/"
    "cdc_milestones_with_bridges_family_cleaned_final_app_ready.xlsx"
)
SOURCE_SHA256 = "c2b6735d9f099c916973c98c1953e7eb1f2ca8e1a60430011f805a3bf9c3487c"
MAIN_SHEET = "all_with_bridge_family"
EXPECTED_ROWS = 369

#: Columns the migration must never alter.
PROTECTED_COLUMNS: Tuple[str, ...] = (
    "months",
    "category",
    "subdomain",
    "milestone",
    "parent_explanation",
    "bridge_step_number",
    "bridge_step",
    "activity_family",
    "previous_bridge_step",
    "previous_anchor_age",
)

#: The single column this migration introduces.
CANONICAL_COLUMN = "canonical_domain"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return h.hexdigest()


@dataclass(frozen=True)
class MappingRow:
    """One row of the subdomain audit table."""

    subdomain: str
    old_category: str
    canonical_domain: str
    row_count: int


@dataclass(frozen=True)
class MigrationReport:
    """Everything needed to verify the migration without re-reading the files."""

    source_sha256: str
    source_rows: int
    candidate_rows: int
    mapping_rows: Tuple[MappingRow, ...]
    domain_counts: Dict[str, int]
    protected_columns_identical: bool

    @property
    def total_mapped(self) -> int:
        return sum(m.row_count for m in self.mapping_rows)


def _read_main(path: Path):
    import pandas as pd  # local import: pandas is not needed to import this module

    return pd.read_excel(path, sheet_name=MAIN_SHEET, dtype=str)


def build_mapping_table(source: Path) -> Tuple[MappingRow, ...]:
    """Derive the subdomain audit table FROM THE DATA. Counts are never hardcoded."""
    validate_map()
    df = _read_main(source)
    rows: List[MappingRow] = []
    grouped = df.groupby(["subdomain", "category"], dropna=False).size()
    for (subdomain, category), count in grouped.items():
        rows.append(
            MappingRow(
                subdomain=str(subdomain),
                old_category=str(category),
                canonical_domain=resolve(str(subdomain)),
                row_count=int(count),
            )
        )
    rows.sort(key=lambda r: (DOMAIN_KEYS.index(r.canonical_domain), r.subdomain))
    return tuple(rows)


def domain_counts(source: Path) -> Dict[str, int]:
    """Row count per canonical domain, derived from the data. Zero-filled."""
    df = _read_main(source)
    counts = Counter(resolve(str(s)) for s in df["subdomain"])
    return {k: int(counts.get(k, 0)) for k in DOMAIN_KEYS}


def migrate(source: Path, candidate: Path) -> MigrationReport:
    """Write the Parent 2.4 candidate workbook. The source is never modified.

    Adds `canonical_domain`; copies every other sheet and column verbatim.
    """
    import pandas as pd

    source, candidate = Path(source), Path(candidate)
    if candidate.resolve() == source.resolve():
        raise ValueError("refusing to overwrite the authoritative source workbook")

    validate_map()
    src_sha = sha256_of(source)

    xl = pd.ExcelFile(source)
    main = pd.read_excel(source, sheet_name=MAIN_SHEET, dtype=str)
    if len(main) != EXPECTED_ROWS:
        raise SubdomainMappingError(
            f"source has {len(main)} rows, expected {EXPECTED_ROWS}"
        )

    out = main.copy()
    # Fail-closed per row: resolve() raises on unknown/ambiguous subdomains.
    out[CANONICAL_COLUMN] = [resolve(str(s)) for s in out["subdomain"]]

    candidate.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(candidate, engine="openpyxl") as writer:
        out.to_excel(writer, sheet_name=MAIN_SHEET, index=False)
        for sheet in xl.sheet_names:
            if sheet == MAIN_SHEET:
                continue
            pd.read_excel(source, sheet_name=sheet, dtype=str, header=None).to_excel(
                writer, sheet_name=sheet, index=False, header=False
            )

    report = MigrationReport(
        source_sha256=src_sha,
        source_rows=len(main),
        candidate_rows=len(out),
        mapping_rows=build_mapping_table(source),
        domain_counts=domain_counts(source),
        protected_columns_identical=verify_candidate(source, candidate),
    )
    if sha256_of(source) != src_sha:
        raise RuntimeError("source workbook changed during migration")
    return report


def verify_candidate(source: Path, candidate: Path) -> bool:
    """Prove every protected column is identical between source and candidate.

    Compared as raw strings after a NaN->"" normalisation, so a numeric column
    silently re-typed on the round-trip would still be caught.
    """
    import pandas as pd

    a = pd.read_excel(source, sheet_name=MAIN_SHEET, dtype=str)
    b = pd.read_excel(candidate, sheet_name=MAIN_SHEET, dtype=str)

    if len(a) != len(b):
        return False
    for col in PROTECTED_COLUMNS:
        if col not in a.columns or col not in b.columns:
            return False
        if list(a[col].fillna("")) != list(b[col].fillna("")):
            return False
    # The candidate must be a strict superset: originals + exactly one new column.
    if set(b.columns) != set(a.columns) | {CANONICAL_COLUMN}:
        return False
    return True
