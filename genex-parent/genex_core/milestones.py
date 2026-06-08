"""
genex_core/milestones.py
------------------------
Public interface for milestone data access — V22 version.

Delegates all data loading to table_loader.py (which reads the V22 Excel).
The public API is unchanged so existing callers (interview_engine, scoring,
activity_engine) continue to work without modification.

Public API (preserved from pre-V22):
    get_cdc_df()                  → pd.DataFrame
    get_category_questions()      → List[Dict]
    get_cdc_ages()                → List[int]
    get_subdomain_to_category()   → Dict[str, str]
    get_category_to_subdomains()  → Dict[str, List[str]]
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from genex_core.table_loader import get_bridge_df, get_bridge_step1_df


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_cdc_df() -> pd.DataFrame:
    """Return the full bridge milestone DataFrame (V22 table, all bridge steps).

    Columns: months, category, subdomain, milestone, parent_explanation,
             bridge_step_number, bridge_step, activity_family,
             previous_bridge_step, previous_anchor_age, category_key
    """
    return get_bridge_df()


def get_category_questions(
    category_key: str,
    age_months: int,
    band_months: int = 6,
    include_adjacent: bool = True,
) -> List[Dict[str, Any]]:
    """Return milestone questions for a category near the given age.

    Returns bridge_step_number=1 rows only (the target milestone rows used
    during the parent interview).  Each dict includes the fields the interview
    engine expects: months, subdomain, milestone, parent_explanation.

    Parameters
    ----------
    category_key     : e.g. "language_and_communication"
    age_months       : estimated developmental age (or chronological)
    band_months      : half-width of the age window to search (default 6)
    include_adjacent : if True, also include adjacent band on misses
    """
    df = get_bridge_step1_df()

    if "category_key" not in df.columns:
        return []

    cat_df = df[df["category_key"] == category_key].copy()
    if cat_df.empty:
        return []

    lo = max(2, age_months - band_months)
    hi = age_months + band_months

    window = cat_df[(cat_df["months"] >= lo) & (cat_df["months"] <= hi)]

    if window.empty and include_adjacent:
        # Widen to ±2 bands
        lo2 = max(2, age_months - band_months * 2)
        hi2 = age_months + band_months * 2
        window = cat_df[(cat_df["months"] >= lo2) & (cat_df["months"] <= hi2)]

    if window.empty:
        # Fall back to closest age band available
        cat_df["_dist"] = (cat_df["months"] - age_months).abs()
        closest_age = cat_df.loc[cat_df["_dist"].idxmin(), "months"]
        window = cat_df[cat_df["months"] == closest_age]

    questions: List[Dict[str, Any]] = []
    seen_milestones: set = set()
    for _, row in window.sort_values("months").iterrows():
        milestone = str(row.get("milestone", "") or "").strip()
        if not milestone or milestone in seen_milestones:
            continue
        seen_milestones.add(milestone)
        questions.append({
            "months": int(row["months"]) if pd.notna(row["months"]) else age_months,
            "subdomain": str(row.get("subdomain", "") or "").strip(),
            "milestone": milestone,
            "parent_explanation": str(row.get("parent_explanation", "") or "").strip(),
            "activity_family": str(row.get("activity_family", "") or "").strip(),
            "bridge_step": str(row.get("bridge_step", "") or "").strip(),
        })
    return questions


def get_cdc_ages(category_key: Optional[str] = None) -> List[int]:
    """Return sorted list of distinct milestone age bands for a category (or all)."""
    df = get_bridge_step1_df()
    if category_key and "category_key" in df.columns:
        df = df[df["category_key"] == category_key]
    ages = sorted(df["months"].dropna().astype(int).unique().tolist())
    return ages


def get_subdomain_to_category() -> Dict[str, str]:
    """Return {subdomain: category_key} mapping derived from the V22 table."""
    df = get_bridge_df()
    if "category_key" not in df.columns or "subdomain" not in df.columns:
        return {}
    result: Dict[str, str] = {}
    for subdomain, grp in df.groupby("subdomain"):
        keys = [k for k in grp["category_key"].dropna().astype(str).unique() if k]
        if keys:
            result[str(subdomain)] = keys[0]
    return result


def get_category_to_subdomains() -> Dict[str, List[str]]:
    """Return {category_key: [subdomain, ...]} mapping from the V22 table."""
    df = get_bridge_df()
    if "category_key" not in df.columns or "subdomain" not in df.columns:
        return {}
    result: Dict[str, List[str]] = {}
    for category_key, grp in df.groupby("category_key"):
        subs = sorted(grp["subdomain"].dropna().astype(str).unique().tolist())
        result[str(category_key)] = subs
    return result
