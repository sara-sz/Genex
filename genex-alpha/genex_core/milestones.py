"""
genex_core/milestones.py
------------------------
CDC milestone table loading, normalization, and question selection.
Extracted from genex_interview_activity_v11.ipynb — logic unchanged.
"""

from pathlib import Path
from typing import Optional
import pandas as pd

from genex_core.config import ALIAS_TO_CATEGORY, DOMAIN_CONFIG

# ------------------------------------------------------------------
# File discovery
# ------------------------------------------------------------------
PREFERRED_CDC_FILENAMES = [
    "cdc_milestones.xlsx",
    "milestone-cdc-table-improved-subdomains-advisor.xlsx",
    "milestone-cdc-table-improved-subdomains.xlsx",
    "milestone-cdc-table.xlsx",
]
ADVISOR_SUPPLEMENTAL_SHEET_NAME = "advisor_supplemental_review"

# Module-level cache so we only load once per session
_cdc_df: Optional[pd.DataFrame] = None
_cdc_path: Optional[Path] = None
_approved_supplemental_df: Optional[pd.DataFrame] = None
CDC_AGES: list = []


def find_cdc_file(path: Optional[str] = None) -> Path:
    """Find the CDC milestone table, preferring the improved subdomain-tagged version."""
    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Provided CDC file path does not exist: {p}")
        return p.resolve()

    # Look relative to this file's location first (data/ folder)
    this_dir = Path(__file__).parent.parent  # genex-alpha/
    data_dir = this_dir / "data"

    search_roots = [data_dir, this_dir, Path.cwd(), Path.cwd().parent]

    for name in PREFERRED_CDC_FILENAMES:
        for root in search_roots:
            candidate = root / name
            if candidate.exists():
                return candidate.resolve()

    # Fuzzy search
    candidate_paths = []
    for root in search_roots:
        if root.exists():
            candidate_paths.extend(root.rglob("*milestone*cdc*table*.xlsx"))
            candidate_paths.extend(root.rglob("*milestone*subdomain*.xlsx"))
            candidate_paths.extend(root.rglob("cdc_milestones*.xlsx"))

    candidate_paths = list({p.resolve() for p in candidate_paths if p.exists()})
    if candidate_paths:
        def rank_path(p: Path):
            name = p.name.lower()
            if "subdomain" in name or "improved" in name:
                return (0, len(name))
            return (1, len(name))
        candidate_paths = sorted(candidate_paths, key=rank_path)
        return candidate_paths[0]

    raise FileNotFoundError(
        "Could not find a CDC milestone spreadsheet. "
        "Expected at genex-alpha/data/cdc_milestones.xlsx"
    )


def _normalize_cdc_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.rename(columns={"category ": "category", "milestone ": "milestone"})

    if "subdomain" not in df.columns:
        df["subdomain"] = "unspecified"

    df["category"] = df["category"].astype(str).str.strip().str.lower()
    df["milestone"] = df["milestone"].astype(str).str.strip()
    df["subdomain"] = df["subdomain"].fillna("unspecified").astype(str).str.strip().str.lower()
    df["months"] = pd.to_numeric(df["months"], errors="coerce")

    df = df.dropna(subset=["months", "category", "milestone"]).copy()
    df["months"] = df["months"].astype(int)
    df["category_key"] = df["category"].map(
        lambda x: ALIAS_TO_CATEGORY.get(x, x.replace(" ", "_"))
    )
    df["question_id"] = [
        f"{row.category_key}_{row.months}_{i}"
        for i, row in enumerate(df.itertuples(), start=1)
    ]
    return df


def _normalize_supplemental_sheet(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]

    rename_map = {
        "main_category": "category",
        "target_subdomain": "subdomain",
        "proposed_supplemental_milestone_or_followup": "milestone",
        "include_in_model_after_review": "include_in_model",
    }
    df = df.rename(columns=rename_map)

    for col in ["category", "subdomain", "milestone", "include_in_model"]:
        if col not in df.columns:
            df[col] = None
    if "months" not in df.columns:
        df["months"] = None

    df["category"] = df["category"].astype(str).str.strip().str.lower()
    df["subdomain"] = df["subdomain"].astype(str).str.strip().str.lower()
    df["milestone"] = df["milestone"].astype(str).str.strip()
    df["include_in_model"] = df["include_in_model"].astype(str).str.strip().str.lower()
    df["months"] = pd.to_numeric(df["months"], errors="coerce")

    include_values = {"yes", "approved", "include", "1", "true", "y"}
    df = df[
        df["include_in_model"].isin(include_values)
        & df["months"].notna()
        & df["category"].astype(bool)
        & df["milestone"].astype(bool)
        & df["subdomain"].astype(bool)
    ].copy()

    if df.empty:
        return df

    df["months"] = df["months"].astype(int)
    df["category_key"] = df["category"].map(
        lambda x: ALIAS_TO_CATEGORY.get(x, x.replace(" ", "_"))
    )
    df["question_id"] = [
        f"{row.category_key}_{row.months}_supp_{i}"
        for i, row in enumerate(df.itertuples(), start=1)
    ]
    df["source"] = "advisor_supplemental"
    return df[["months", "category", "milestone", "subdomain", "category_key", "question_id", "source"]]


def load_cdc_table(path: Optional[str] = None):
    """Load the CDC backbone and, if present, append advisor-approved supplemental items."""
    path = find_cdc_file(path)
    xls = pd.ExcelFile(path)

    main_sheet = "all" if "all" in xls.sheet_names else xls.sheet_names[0]
    base_df = pd.read_excel(xls, sheet_name=main_sheet)
    base_df = _normalize_cdc_dataframe(base_df)
    base_df["source"] = "cdc_backbone"

    approved_supplemental_df = pd.DataFrame(columns=base_df.columns)

    if ADVISOR_SUPPLEMENTAL_SHEET_NAME in xls.sheet_names:
        supplemental_raw = pd.read_excel(xls, sheet_name=ADVISOR_SUPPLEMENTAL_SHEET_NAME)
        approved_supplemental_df = _normalize_supplemental_sheet(supplemental_raw)

        if not approved_supplemental_df.empty:
            missing_cols = [c for c in base_df.columns if c not in approved_supplemental_df.columns]
            for c in missing_cols:
                approved_supplemental_df[c] = None
            approved_supplemental_df = approved_supplemental_df[base_df.columns]

    combined_df = pd.concat([base_df, approved_supplemental_df], ignore_index=True, sort=False)
    combined_df = combined_df.sort_values(["months", "category", "milestone"]).reset_index(drop=True)

    return combined_df, path, approved_supplemental_df


def get_cdc_df() -> pd.DataFrame:
    """Return the cached CDC dataframe, loading it on first call."""
    global _cdc_df, _cdc_path, _approved_supplemental_df, CDC_AGES
    if _cdc_df is None:
        _cdc_df, _cdc_path, _approved_supplemental_df = load_cdc_table()
        CDC_AGES = sorted(_cdc_df["months"].dropna().unique().tolist())
    return _cdc_df


def get_cdc_ages() -> list:
    get_cdc_df()
    return CDC_AGES


def get_category_questions(category_key: str, min_months: int, max_months: int) -> pd.DataFrame:
    df = get_cdc_df()
    subset = df[
        (df["category_key"] == category_key)
        & (df["months"] >= min_months)
        & (df["months"] <= max_months)
    ].sort_values(["months", "milestone"])
    return subset.copy()


def get_subdomain_to_category() -> dict:
    """Build SUBDOMAIN_TO_CATEGORY mapping from the loaded CDC table."""
    df = get_cdc_df()
    mapping = {}
    for subdomain, group in df.groupby("subdomain"):
        cats = [c for c in group["category_key"].dropna().astype(str).unique().tolist() if c]
        if cats:
            mapping[subdomain] = cats[0]
    return mapping


def get_category_to_subdomains() -> dict:
    """Build CATEGORY_TO_SUBDOMAINS mapping from the loaded CDC table."""
    df = get_cdc_df()
    return {
        category_key: sorted(
            df.loc[df["category_key"] == category_key, "subdomain"]
            .dropna()
            .astype(str)
            .unique()
            .tolist()
        )
        for category_key in DOMAIN_CONFIG
    }
