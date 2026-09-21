"""
genex_core/table_loader.py
--------------------------
Single source of truth for loading the V22 bridge milestone table.

Loads `cdc_milestones_with_bridges_family_cleaned_final_app_ready.xlsx`:
  - sheet `all_with_bridge_family`  → bridge_df (369 rows, 10 columns)
  - sheet `activity_family_legend`  → activity_family_legend dict

All other genex_core modules import from here. Nobody reads the Excel directly.

Columns in all_with_bridge_family:
  months, category, subdomain, milestone, parent_explanation,
  bridge_step_number, bridge_step, activity_family,
  previous_bridge_step, previous_anchor_age
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

# ---------------------------------------------------------------------------
# Filename — only the final app-ready file is accepted as primary source.
# ---------------------------------------------------------------------------
_BRIDGE_FILENAME = "cdc_milestones_with_bridges_family_cleaned_final_app_ready.xlsx"
_BRIDGE_SHEET = "all_with_bridge_family"
_LEGEND_SHEET = "activity_family_legend"

# ---------------------------------------------------------------------------
# Parent 2.4 candidate workbook (Phase 0.3B) — the Brain's classification source
#
# The Parent 2.4 Brain loads the CANDIDATE workbook: the original 369-row table
# plus one added `canonical_domain` column. The original file stays archival and
# immutable (its 159->369 bridge builder is unrecoverable), and the candidate is
# purely additive — every original column is preserved and `category`/`subdomain`
# remain available as provenance.
#
# `category_key` is READ from `canonical_domain` rather than re-derived, so the
# runtime cannot drift from the migration that produced and verified it. If the
# candidate is absent the loader falls back to the original workbook and derives
# legacy four-domain keys, which the Parent 2.4 tests detect rather than accept
# silently.
# ---------------------------------------------------------------------------
_PARENT24_SUBDIR = "parent_2_4"
_PARENT24_FILENAME = "cdc_milestones_parent_2_4_candidate.xlsx"
_CANONICAL_DOMAIN_COLUMN = "canonical_domain"

# LEGACY category-display mapping. Retained ONLY for the fallback path and for
# reading the historical `category` provenance column; it produces pre-2.4
# four-domain keys and is never the Parent 2.4 classification source.
_CATEGORY_DISPLAY_TO_KEY: Dict[str, str] = {
    "cognitive": "cognitive",
    "cognitive / adaptive": "cognitive",
    "cognitive/adaptive": "cognitive",
    "adaptive": "cognitive",
    "movement": "movement_and_physical",
    "movement / physical": "movement_and_physical",
    "movement/physical": "movement_and_physical",
    "physical": "movement_and_physical",
    "motor": "movement_and_physical",
    "language": "language_and_communication",
    "language / communication": "language_and_communication",
    "language/communication": "language_and_communication",
    "communication": "language_and_communication",
    "speech": "language_and_communication",
    "social": "social_and_emotional",
    "social / emotional": "social_and_emotional",
    "social/emotional": "social_and_emotional",
    "emotional": "social_and_emotional",
}


def _find_bridge_file() -> Path:
    """Locate the milestone workbook, preferring the Parent 2.4 candidate.

    The candidate carries `canonical_domain` and is what makes the Brain
    seven-domain-native. The original app-ready workbook remains a valid
    fallback so the loader still works in an export that lacks the candidate —
    but it yields legacy four-domain keys, which the Parent 2.4 test suite
    detects rather than silently accepting.
    """
    this_dir = Path(__file__).parent          # genex_core/
    app_dir = this_dir.parent                  # genex-parent/
    candidates = [
        # Parent 2.4 candidate first.
        app_dir / "data" / _PARENT24_SUBDIR / _PARENT24_FILENAME,
        this_dir / "data" / _PARENT24_SUBDIR / _PARENT24_FILENAME,
        Path.cwd() / "data" / _PARENT24_SUBDIR / _PARENT24_FILENAME,
        # Archival original (legacy classification).
        app_dir / "data" / _BRIDGE_FILENAME,
        this_dir / "data" / _BRIDGE_FILENAME,
        Path.cwd() / "data" / _BRIDGE_FILENAME,
        Path.cwd() / _BRIDGE_FILENAME,
    ]
    for path in candidates:
        if path.exists():
            return path.resolve()
    raise FileNotFoundError(
        f"Could not find {_PARENT24_FILENAME} or {_BRIDGE_FILENAME}. "
        f"Expected at {app_dir / 'data' / _PARENT24_SUBDIR / _PARENT24_FILENAME}"
    )


def _norm_category(raw: Any) -> str:
    """Normalise a category display string to a category_key."""
    s = re.sub(r"\s+", " ", str(raw or "").strip().lower())
    return _CATEGORY_DISPLAY_TO_KEY.get(s, s.replace(" ", "_").replace("/", "_"))


def _load_bridge_df_raw(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=_BRIDGE_SHEET, dtype=str)
    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    # Normalise numeric columns
    for col in ("months", "bridge_step_number", "previous_anchor_age"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Parent 2.4 classification.
    #
    # `category_key` is the key every downstream Brain module groups by, so this
    # single assignment is what makes milestone selection, focus ranking,
    # scheduling and progress seven-domain-native. Read from `canonical_domain`
    # when present; never re-derive it, so runtime cannot drift from the
    # verified migration.
    if _CANONICAL_DOMAIN_COLUMN in df.columns:
        df["category_key"] = df[_CANONICAL_DOMAIN_COLUMN].astype(str).str.strip()
        # The historical four-domain value stays available as provenance.
        if "category" in df.columns:
            df["legacy_category_key"] = df["category"].apply(_norm_category)
    elif "category" in df.columns:
        # Fallback: archival workbook only. Yields LEGACY four-domain keys.
        df["category_key"] = df["category"].apply(_norm_category)
        df["legacy_category_key"] = df["category_key"]

    # Strip all string columns
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].str.strip()

    return df


def _load_legend_raw(path: Path) -> Dict[str, str]:
    """Load activity_family_legend into {activity_family: description} dict."""
    try:
        df = pd.read_excel(path, sheet_name=_LEGEND_SHEET, dtype=str, header=None)
        # Row 0 is the header row ("activity_family", "description / allowed...")
        # Data starts at row 1
        result: Dict[str, str] = {}
        for _, row in df.iloc[1:].iterrows():
            fam = str(row.iloc[0] or "").strip()
            desc = str(row.iloc[1] if len(row) > 1 else "").strip()
            if fam and fam.lower() not in ("activity_family", "nan", ""):
                result[fam] = desc
        return result
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _cached_load(path_str: str):
    path = Path(path_str)
    bridge_df = _load_bridge_df_raw(path)
    legend = _load_legend_raw(path)
    return bridge_df, legend


def _get_path() -> str:
    return str(_find_bridge_file())


def get_bridge_df() -> pd.DataFrame:
    """Return the full all_with_bridge_family DataFrame (cached)."""
    bridge_df, _ = _cached_load(_get_path())
    return bridge_df


def get_activity_family_legend() -> Dict[str, str]:
    """Return {activity_family: description} dict from the legend sheet (cached)."""
    _, legend = _cached_load(_get_path())
    return legend


def get_bridge_step1_df() -> pd.DataFrame:
    """Return only bridge_step_number == 1 rows (the initial planning rows)."""
    df = get_bridge_df()
    return df[df["bridge_step_number"] == 1].copy()


def get_families_for_category(category_key: str) -> list:
    """Return distinct activity_family values for a category_key."""
    df = get_bridge_df()
    if "category_key" not in df.columns:
        return []
    rows = df[df["category_key"] == category_key]
    return sorted(rows["activity_family"].dropna().unique().tolist())


def get_family_description(activity_family: str) -> str:
    """Return legend description for an activity_family, or empty string."""
    legend = get_activity_family_legend()
    return legend.get(activity_family, "")


def reload_cache() -> None:
    """Force reload on next access (useful for tests)."""
    _cached_load.cache_clear()
