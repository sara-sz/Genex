# -*- coding: utf-8 -*-
"""
HPO-only EDA

Loads data_proc/{condition.parquet, feature.parquet, condition_feature.parquet}
and produces:
  - Nulls/uniques summary
  - Top HPO feature frequency plot (label fallback if missing)
  - IC histogram (if column exists)
  - Per-condition feature-count histogram (+ top conditions table)
  - Optional sparse matrix density if X_hpo_csr.npz & mappings.json exist

Run:
  python -m src.eda_hpo
"""
from __future__ import annotations

from pathlib import Path
import json
import sys
from typing import Optional

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# ---------------------------------------------------------------------
# Paths
ROOT = Path(__file__).resolve().parents[1]
DP   = ROOT / "data_proc"

COND_F = DP / "condition.parquet"
FEAT_F = DP / "feature.parquet"
CF_F   = DP / "condition_feature.parquet"
X_F    = DP / "X_hpo_csr.npz"
MAP_F  = DP / "mappings.json"

# ---------------------------------------------------------------------
# Label fallback helper
try:
    # If you added the helper to src/utils.py, use it
    from src.utils import with_fallback_labels  # type: ignore
except Exception:
    # Local fallback: fill missing/blank labels with feature_id
    def with_fallback_labels(feat: pd.DataFrame) -> pd.DataFrame:
        """Ensure 'label' exists and has no blanks by backfilling with feature_id."""
        f = feat.copy()
        if "label" not in f.columns:
            f["label"] = f["feature_id"].astype(str)
            return f
        lab = f["label"].astype(str)
        bad = lab.isna() | (lab.str.len() == 0) | lab.str.match(r"^\s*$")
        f.loc[bad, "label"] = f.loc[bad, "feature_id"].astype(str)
        return f

# ---------------------------------------------------------------------
def _safe_read_parquet(p: Path, name: str) -> Optional[pd.DataFrame]:
    if not p.exists():
        print(f"[skip] {name} missing -> {p}")
        return None
    try:
        return pd.read_parquet(p)
    except Exception as e:
        print(f"[warn] failed to load {name} from {p}: {e}")
        return None

def _nulls_summary(df: Optional[pd.DataFrame], name: str) -> Optional[pd.DataFrame]:
    if df is None:
        print(f"[{name}] missing")
        return None
    s = []
    for c in df.columns:
        n_null = int(df[c].isna().sum())
        s.append({
            "table": name,
            "column": c,
            "dtype": str(df[c].dtype),
            "n_null": n_null,
            "pct_null": (n_null / len(df)) if len(df) else 0.0,
            "n_unique": int(df[c].nunique(dropna=True)),
        })
    out = pd.DataFrame(s).sort_values(["table", "column"])
    print(out.to_string(index=False))
    return out

def main() -> None:
    # ----------------- Load -----------------
    cond = _safe_read_parquet(COND_F, "condition")
    feat = _safe_read_parquet(FEAT_F, "feature")
    cf   = _safe_read_parquet(CF_F,   "condition_feature")

    print("\n=== Shapes ===")
    for nm, df in [("condition", cond), ("feature", feat), ("condition_feature", cf)]:
        if df is not None:
            print(f"{nm:17s}: {tuple(df.shape)}")
        else:
            print(f"{nm:17s}: MISSING")

    # ----------------- Label fallback -----------------
    if feat is not None:
        feat = with_fallback_labels(feat)

    # ----------------- Nulls / uniques summary -----------------
    print("\n=== NULLS / UNIQUES SUMMARY ===")
    _ = _nulls_summary(cond, "condition")
    _ = _nulls_summary(feat, "feature")
    _ = _nulls_summary(cf,   "condition_feature")

    # ----------------- Feature frequency (top terms) -----------------
    if cf is not None and feat is not None:
        print("\nTop HPO features by number of linked conditions:")
        freq = (
            cf.groupby("feature_id")
              .size()
              .rename("n_conditions")
              .reset_index()
              .sort_values("n_conditions", ascending=False)
        )
        feat_lbl = feat[["feature_id", "label"]].drop_duplicates()
        top = freq.head(30).merge(feat_lbl, on="feature_id", how="left")
        # Final safety fallback
        top["label"] = top["label"].fillna(top["feature_id"].astype(str))

        print(top[["feature_id", "label", "n_conditions"]].to_string(index=False))

        plt.figure()
        plt.bar(top["label"].astype(str), top["n_conditions"].astype(int))
        plt.xticks(rotation=90)
        plt.title("Top HPO features by number of linked conditions")
        plt.tight_layout()
        plt.show()
    else:
        print("\n[skip] Feature frequency plot (need condition_feature & feature)")

    # ----------------- IC histogram -----------------
    if feat is not None and "ic" in feat.columns:
        vals = feat["ic"].dropna().values
        plt.figure()
        plt.hist(vals, bins=40)
        plt.xlabel("IC")
        plt.ylabel("Count")
        plt.title("HPO Information Content (IC) distribution")
        plt.tight_layout()
        plt.show()
    else:
        print("\n[skip] IC histogram (feature.ic not present)")

    # ----------------- Per-condition feature counts -----------------
    if cf is not None and cond is not None:
        per = (
            cf.groupby("condition_id")
              .size()
              .rename("n_features")
              .reset_index()
        )
        plt.figure()
        plt.hist(per["n_features"].values, bins=40)
        plt.xlabel("# HPO terms")
        plt.ylabel("Count of conditions")
        plt.title("Number of HPO features per condition")
        plt.tight_layout()
        plt.show()

        # Show top-k conditions by feature count
        topk = per.sort_values("n_features", ascending=False).head(10)
        show = topk.merge(
            cond[["condition_id", "name"]] if "name" in (cond.columns if cond is not None else []) else cond,
            on="condition_id",
            how="left",
        )
        print("\nTop conditions by number of HPO features:")
        cols = ["condition_id", "name", "n_features"] if "name" in show.columns else ["condition_id", "n_features"]
        print(show[cols].to_string(index=False))
    else:
        print("\n[skip] Per-condition feature count plots (need condition_feature & condition)")

    # ----------------- Matrix density (optional) -----------------
    try:
        from scipy.sparse import load_npz
        have_sparse = X_F.exists() and MAP_F.exists()
        if have_sparse:
            X = load_npz(X_F)
            meta = json.loads(MAP_F.read_text(encoding="utf-8"))
            density = float(X.nnz) / float(X.shape[0] * X.shape[1]) if X.shape[0] and X.shape[1] else 0.0
            print(f"\nMatrix: shape={X.shape}, nnz={X.nnz}, density={density:.6f}")
            print("Meta keys:", list(meta.keys()))

            plt.figure()
            plt.bar(["density"], [density])
            plt.title("Matrix density (nnz / total cells)")
            plt.tight_layout()
            plt.show()
        else:
            print("\n[skip] Matrix density (X_hpo_csr.npz or mappings.json missing)")
    except Exception as e:
        print(f"\n[warn] Could not inspect sparse matrix: {e}")

if __name__ == "__main__":
    main()
