# src/inspect_nulls.py
from __future__ import annotations
from pathlib import Path
import pandas as pd
from src.utils import safe_read_parquet, missing_label_count, with_fallback_labels


ROOT = Path(__file__).resolve().parents[1]
DP = ROOT / "data_proc"

COND_F = DP / "condition.parquet"
FEAT_F = DP / "feature.parquet"
CF_F   = DP / "condition_feature.parquet"

OUT_DIR = DP / "diagnostics"

def _null_summary(df: pd.DataFrame, name: str) -> pd.DataFrame:
    rows = []
    for c in df.columns:
        n_null = int(df[c].isna().sum())
        pct = (n_null / len(df)) if len(df) else 0.0
        rows.append({
            "table":  name,
            "column": c,
            "dtype":  str(df[c].dtype),
            "n_null": n_null,
            "pct_null": pct,
            "n_unique": int(df[c].nunique(dropna=True)),
        })
    out = pd.DataFrame(rows).sort_values(["table", "column"])
    print(out.to_string(index=False))
    return out

def main():
    assert COND_F.exists() and FEAT_F.exists() and CF_F.exists(), (
        "Missing one of the parquet files in data_proc/. "
        "Run: python -m src.build_tables && python -m src.make_matrices"
    )

    cond = pd.read_parquet(COND_F)
    cf   = pd.read_parquet(CF_F)

    feat = pd.read_parquet(FEAT_F)
    feat = with_fallback_labels(feat)
    n_missing = missing_label_count(feat)
    print(f"\nMissing/empty feature labels: {n_missing}")

    print("\n=== NULLS SUMMARY (HPO-only) ===")
    ns_cond = _null_summary(cond, "condition")
    ns_feat = _null_summary(feat, "feature")
    ns_cf   = _null_summary(cf,   "condition_feature")

    # ID hygiene (no vendor enforcement)
    print("\n[check] Feature ID prefixes (should mostly be 'HP'):")
    print(
        feat["feature_id"].astype(str).str.split(":").str[0]
        .value_counts().head(10).to_string()
    )

    print("\n[check] Condition ID prefixes:")
    print(
        cond["condition_id"].astype(str).str.split(":").str[0]
        .value_counts().head(10).to_string()
    )

    # Missing/empty feature labels
    n_empty = int(
        ((feat["label"].isna()) |
         (feat["label"].astype(str).str.strip().str.len() == 0)).sum()
    )
    print(f"\nMissing/empty feature labels: {n_empty}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ns_cond.to_csv(OUT_DIR / "nulls_condition.csv", index=False)
    ns_feat.to_csv(OUT_DIR / "nulls_feature.csv", index=False)
    ns_cf.to_csv(OUT_DIR / "nulls_condition_feature.csv", index=False)
    print(f"\nSaved CSV reports to {OUT_DIR}")

    feat = pd.read_parquet("data_proc/feature.parquet")
    print("Missing/empty feature labels:", missing_label_count(feat))
    feat = with_fallback_labels(feat)  # if you want to preview with fallback text

if __name__ == "__main__":
    main()
