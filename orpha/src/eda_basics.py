# -*- coding: utf-8 -*-
"""
EDA for HPO–Orpha tables.

Usage (from repo root):
  .\.venv\Scripts\python -m src.eda_basics --save-previews

This saves small CSV previews and PNG charts into data_proc/_previews/.
It prints shapes, nulls, and matrix sparsity. It is safe to run even if
some optional columns (category, prevalence_band, inheritance) are missing.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# ------------------------------------------------------------------
# Locate project root (folder that contains data_proc/)
# ------------------------------------------------------------------
def find_root() -> Path:
    here = Path.cwd()
    if (here / "data_proc").exists():
        return here
    if (here.parent / "data_proc").exists():
        return here.parent
    try:
        f = Path(__file__).resolve()
        for p in [f.parent, f.parent.parent, f.parent.parent.parent]:
            if (p / "data_proc").exists():
                return p
    except NameError:
        pass
    raise FileNotFoundError("Could not locate project root (folder containing data_proc/)")

ROOT = find_root()
DP   = ROOT / "data_proc"
PREV = DP / "_previews"
PREV.mkdir(parents=True, exist_ok=True)

COND_F = DP / "condition.parquet"
FEAT_F = DP / "feature.parquet"
CF_F   = DP / "condition_feature.parquet"
X_F    = DP / "X_hpo_csr.npz"
MAP_F  = DP / "mappings.json"

print("ROOT:", ROOT)
print("Looking for:")
for p in [COND_F, FEAT_F, CF_F]:
    print(" ", p)

def _read(p: Path) -> Optional[pd.DataFrame]:
    try:
        return pd.read_parquet(p) if p.exists() else None
    except Exception as e:
        print(f"  WARN: failed to read {p.name}: {e}")
        return None

cond = _read(COND_F)
feat = _read(FEAT_F)
cf   = _read(CF_F)

print("\n=== SHAPES ===")
for name, df in (("condition",cond),("feature",feat),("condition_feature",cf)):
    print(f"{name:17s}: {'MISSING' if df is None else str(df.shape)}")

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def save_head(df: Optional[pd.DataFrame], name: str, n: int=20):
    if df is None: 
        return
    out = PREV / f"preview_{name}.csv"
    df.head(n).to_csv(out, index=False)

def null_summary(df: Optional[pd.DataFrame], name: str) -> Optional[pd.DataFrame]:
    if df is None:
        print(f"[{name}] missing")
        return None
    rows = []
    for c in df.columns:
        n_null = int(df[c].isna().sum())
        rows.append({
            "table": name,
            "column": c,
            "dtype":  str(df[c].dtype),
            "n_null": n_null,
            "pct_null": (n_null/len(df)) if len(df) else 0.0,
            "n_unique": int(df[c].nunique(dropna=True))
        })
    out = pd.DataFrame(rows).sort_values(["table","column"])
    out_path = PREV / f"nulls_{name}.csv"
    out.to_csv(out_path, index=False)
    return out

def barh(labels, values, title, fname):
    plt.figure()
    plt.barh(list(map(str, labels)), list(map(int, values)))
    plt.title(title)
    plt.tight_layout()
    plt.savefig(PREV / fname, dpi=150)
    plt.close()

def hist(vals, bins, title, xlabel, fname):
    plt.figure()
    plt.hist(vals, bins=bins)
    plt.title(title)
    plt.xlabel(xlabel); plt.ylabel("Count")
    plt.tight_layout()
    plt.savefig(PREV / fname, dpi=150)
    plt.close()

# ------------------------------------------------------------------
# Save previews + null summaries
# ------------------------------------------------------------------
save_head(cond, "condition")
save_head(feat, "feature")
save_head(cf,   "condition_feature")

ns_cond = null_summary(cond, "condition")
ns_feat = null_summary(feat, "feature")
ns_cf   = null_summary(cf,   "condition_feature")

if ns_cond is not None and ns_feat is not None and ns_cf is not None:
    all_nulls = pd.concat([ns_cond, ns_feat, ns_cf], ignore_index=True)
    all_nulls.to_csv(PREV / "nulls_all.csv", index=False)

# ------------------------------------------------------------------
# Feature frequency (top HPOs by # of conditions)
# ------------------------------------------------------------------
if cf is not None and feat is not None:
    freq = cf.groupby("feature_id").size().rename("n_conditions").reset_index()
    feat_lbl = feat[["feature_id","label"]].drop_duplicates()
    top = (freq.sort_values("n_conditions", ascending=False)
              .head(30).merge(feat_lbl, on="feature_id", how="left"))
    top["label_fallback"] = top["label"].fillna(top["feature_id"])
    top[["feature_id","label_fallback","n_conditions"]].to_csv(PREV/"top_hpo_by_conditions.csv", index=False)
    barh(top["label_fallback"].iloc[::-1], top["n_conditions"].iloc[::-1],
         "Top HPO features by number of linked conditions",
         "plot_top_hpo.png")
else:
    print("Skip: feature frequency (cf or feat missing)")

# ------------------------------------------------------------------
# IC distribution
# ------------------------------------------------------------------
if feat is not None and "ic" in feat.columns:
    vals = feat["ic"].dropna().values
    hist(vals, bins=40, title="HPO Information Content (IC)", xlabel="IC",
         fname="plot_ic_hist.png")
else:
    print("Skip: IC histogram (feature.ic missing)")

# ------------------------------------------------------------------
# Per-condition feature counts
# ------------------------------------------------------------------
if cf is not None and cond is not None:
    per = cf.groupby("condition_id").size().rename("n_features").reset_index()
    hist(per["n_features"].values, bins=40,
         title="# HPO features per condition", xlabel="# HPO terms",
         fname="plot_features_per_condition.png")
    topk = per.sort_values("n_features", ascending=False).head(10)
    show = topk.merge(cond[["condition_id","name"]], on="condition_id", how="left")
    show.to_csv(PREV / "top_conditions_by_feature_count.csv", index=False)
else:
    print("Skip: per-condition feature counts (cf or cond missing)")

# ------------------------------------------------------------------
# Category / Prevalence / Inheritance (optional)
# ------------------------------------------------------------------
if cond is not None and "category" in cond.columns:
    vc = cond["category"].dropna().astype(str).value_counts().head(20)
    vc.to_csv(PREV / "category_counts.csv", header=["n"])
    barh(vc.index[::-1], vc.values[::-1], "Top categories (Orphadata/ORDO-derived)",
         "plot_categories.png")
else:
    print("Skip: category (column not present)")

if cond is not None and "prevalence_band" in cond.columns:
    vc = cond["prevalence_band"].dropna().astype(str).value_counts()
    vc.to_csv(PREV / "prevalence_band_counts.csv", header=["n"])
    barh(vc.index[::-1], vc.values[::-1], "Prevalence bands",
         "plot_prevalence_bands.png")
else:
    print("Skip: prevalence_band (column not present)")

if cond is not None and "inheritance" in cond.columns:
    vc = cond["inheritance"].dropna().astype(str).value_counts().head(15)
    if len(vc) > 0:
        vc.to_csv(PREV / "inheritance_counts.csv", header=["n"])
        barh(vc.index[::-1], vc.values[::-1], "Inheritance modes (if parsed)",
             "plot_inheritance.png")
    else:
        print("Inheritance column present but empty — ok to ignore for now.")
else:
    print("Skip: inheritance (column not present)")

# ------------------------------------------------------------------
# Matrix sparsity (if artifacts exist)
# ------------------------------------------------------------------
def matrix_info():
    if not X_F.exists() or not MAP_F.exists():
        print("Skip: matrix (X_hpo_csr.npz or mappings.json missing)")
        return
    try:
        from scipy.sparse import load_npz
    except Exception:
        print("SciPy not installed -> skipping matrix section")
        return
    X = load_npz(X_F)
    meta = json.loads(MAP_F.read_text(encoding="utf-8"))
    density = X.nnz / (X.shape[0] * X.shape[1]) if X.shape[0] and X.shape[1] else 0.0
    info_txt = (
        f"Matrix shape: {X.shape}\n"
        f"Non-zeros (nnz): {X.nnz}\n"
        f"Density: {density:.6f}\n"
        f"Meta keys: {list(meta.keys())}\n"
    )
    (PREV / "matrix_info.txt").write_text(info_txt, encoding="utf-8")
    # tiny bar plot for density
    plt.figure()
    plt.bar(["density"], [density])
    plt.title("Matrix density (nnz / total cells)")
    plt.tight_layout()
    plt.savefig(PREV / "plot_matrix_density.png", dpi=150)
    plt.close()

matrix_info()

print(f"\nSaved previews and plots to: {PREV}")
