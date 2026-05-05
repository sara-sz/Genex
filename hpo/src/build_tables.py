"""
Build HPO-only tables.

Inputs (data_raw/hpo/):
  - phenotype.hpoa                  (HPOA associations; required)
  - genes_to_phenotype.txt          (optional; provides hpo_id → hpo_name)
  - phenotype_to_genes.txt          (optional; provides hpo_id → hpo_name)

Outputs (data_proc/):
  - condition.parquet               [condition_id, name]
  - feature.parquet                 [feature_id, label, ic]
  - condition_feature.parquet       [condition_id, feature_id, weight]
  - preview_condition.csv / preview_feature.csv / preview_condition_feature.csv

Run:
  python -m src.build_tables

Author: Sara soltanizadeh
Created: 2025-09-09
"""
from __future__ import annotations
import re
from pathlib import Path
from typing import Dict, Tuple
import numpy as np
import pandas as pd

# Project paths: repo root, raw HPO dir, and processed data dir (ensure exists).
ROOT = Path(__file__).resolve().parents[1]
DR_HPO = ROOT / "data_raw" / "hpo"
DP     = ROOT / "data_proc"
DP.mkdir(parents=True, exist_ok=True)

# Expected input files (primary + optional label helpers).
HPOA_F = DR_HPO / "phenotype.hpoa"
G2P_F  = DR_HPO / "genes_to_phenotype.txt"
P2G_F  = DR_HPO / "phenotype_to_genes.txt"

# --- Helpers ----------------------------------------------------------------

# Read a TSV with '#' comments ignored; fail fast if the file is missing.
def _read_tsv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Missing: {path}")
    # HPO/OMIM TSVs often start with comment lines '#'
    return pd.read_csv(path, sep="\t", comment="#", dtype=str, low_memory=False)

# Convert HPOA frequency (fraction or HP code) into a numeric weight [0..1].
def _freq_to_weight(freq: str | float | None) -> float:
    """Map HPOA frequency to numeric weight.
       - 'n/m' => n/m
       - 'HP:004028X' => mapped band
       - empty => 1.0
    """
    if freq is None or (isinstance(freq, float) and np.isnan(freq)):
        return 1.0
    s = str(freq).strip()
    # fraction 'n/m'
    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", s)
    if m:
        n, d = int(m.group(1)), int(m.group(2))
        return float(n) / d if d > 0 else 1.0
    # coded frequencies
    # (approximate midpoints; good enough for baselines/EDA)
    band_map = {
        "HP:0040280": 1.00,  # Obligate (100%)
        "HP:0040284": 0.90,  # Very frequent (80–99%)
        "HP:0040283": 0.60,  # Frequent (30–79%)
        "HP:0040282": 0.15,  # Occasional (5–29%)
        "HP:0040281": 0.02,  # Very rare (<5%)
        "HP:0040285": 0.00,  # Excluded
    }
    if s in band_map:
        return band_map[s]
    # If it looked like 'HP:00xxxxx' but not in map, treat as neutral
    # Default for unknown HP code: neutral weight.
    if s.startswith("HP:"):
        return 0.5
    # Default for anything else: treat as present.
    return 1.0

# Collect HPO term labels (hpo_id → hpo_name) from whichever optional TSV exists.
def _load_labels() -> Dict[str, str]:
    """Collect hpo_id -> hpo_name from whichever TSV exists."""
    labels: Dict[str, str] = {}

    # Merge labels from a DataFrame if it exposes hpo_id/hpo_name (case-insensitive).
    def add_from(df: pd.DataFrame, src: str):
        ok = {"hpo_id", "hpo_name"}.issubset(set(c.lower() for c in df.columns))
        if not ok:
            return
        # make case-insensitive robust
        cols = {c.lower(): c for c in df.columns}
        hid = cols["hpo_id"]
        hnm = cols["hpo_name"]
        for hp, name in zip(df[hid].astype(str), df[hnm].astype(str)):
            hp = hp.strip()
            if not hp.startswith("HP:"):
                continue
            if hp not in labels and name and name != "-":
                labels[hp] = name.strip()

    # Prefer genes_to_phenotype / phenotype_to_genes if present; otherwise return {}.
    if G2P_F.exists():
        add_from(_read_tsv(G2P_F), "genes_to_phenotype")
    if P2G_F.exists():
        add_from(_read_tsv(P2G_F), "phenotype_to_genes")

    return labels

# Estimate information content (IC) per feature: -log(freq(term)/N_conditions).
def _estimate_ic(cf: pd.DataFrame, n_conditions: int) -> pd.Series:
    """Simple IC estimate: -log( freq(term) / N ), freq = number of conditions that list the term."""
    term_counts = cf.groupby("feature_id").condition_id.nunique()
    # avoid log(0)
    p = (term_counts / max(n_conditions, 1)).clip(lower=1e-9)
    return -np.log(p)

# --- Readers ----------------------------------------------------------------

# Parse phenotype.hpoa into normalized condition, feature-id list, and condition_feature edges.
def _read_hpoa(path: Path) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Return (condition, feature_ids_only, condition_feature) from phenotype.hpoa
    """
    df = _read_tsv(path)

    # Build case-insensitive column map for robustness to header variants.
    # Normalize expected columns (robust to capitalization)
    cl = {c.lower(): c for c in df.columns}

    # Ensure the essential columns exist before proceeding.
    # Required logical fields
    need = ["database_id", "hpo_id"]
    missing = [n for n in need if n not in cl]
    if missing:
        raise ValueError(f"{path.name} missing columns: {missing}")

    # Rename expected fields to canonical names used downstream.
    df = df.rename(
        columns={
            cl["database_id"]: "condition_id",
            cl["hpo_id"]:      "feature_id",
            # keep disease_name if present
            **({cl["disease_name"]: "name"} if "disease_name" in cl else {}),
            **({cl["frequency"]: "frequency"} if "frequency" in cl else {}),
        }
    )

    # Build condition_feature table and derive weight from frequency when available.
    cf_cols = ["condition_id", "feature_id"]
    if "frequency" in df.columns:
        df["weight"] = df["frequency"].map(_freq_to_weight).astype(float)
        cf_cols.append("weight")
    else:
        df["weight"] = 1.0
        cf_cols.append("weight")

    # Keep valid pairs only and drop duplicates for a clean bipartite edge list.
    cf = (
        df[cf_cols]
        .dropna(subset=["condition_id", "feature_id"])
        .drop_duplicates()
    )

    # Build condition table (id + optional human-readable name).
    cond_cols = ["condition_id"]
    if "name" in df.columns:
        cond_cols.append("name")
    cond = (
        df[cond_cols]
        .drop_duplicates()
        .sort_values("condition_id")
    )
    if "name" not in cond.columns:
        cond["name"] = ""

    # Collect unique feature IDs; labels are attached later from optional TSVs.
    feat_ids = (
        df[["feature_id"]]
        .dropna()
        .drop_duplicates()
        .sort_values("feature_id")
    )

    return cond, feat_ids, cf

# --- Main -------------------------------------------------------------------

# Orchestrate loading, labeling, IC estimation, typing, writing outputs, and previews.
def main():
    print("Loading phenotype.hpoa ...")
    cond, feat_ids, cf = _read_hpoa(HPOA_F)

    print("Loading HPO labels from TSVs (if present) ...")
    lbl = _load_labels()
    n_lbl = len(lbl)

    # Create feature table; attach human-readable label where available.
    feature = feat_ids.copy()
    feature = feature.rename(columns={"feature_id": "feature_id"})
    feature["label"] = feature["feature_id"].map(lbl).astype("object")

    # Estimate IC for each feature from the condition-feature graph.
    print("Estimating IC from associations ...")
    feature["ic"] = _estimate_ic(cf, n_conditions=len(cond)).reindex(feature["feature_id"]).values

    # Normalize dtypes for consistent parquet schema.
    cond["condition_id"] = cond["condition_id"].astype("object")
    cond["name"]        = cond["name"].astype("object")
    feature["feature_id"] = feature["feature_id"].astype("object")
    feature["label"]      = feature["label"].astype("object")
    feature["ic"]         = feature["ic"].astype("float64")
    cf["condition_id"]    = cf["condition_id"].astype("object")
    cf["feature_id"]      = cf["feature_id"].astype("object")
    cf["weight"]          = cf["weight"].astype("float64")

    # File destinations for core parquet outputs.
    cond_path = DP / "condition.parquet"
    feat_path = DP / "feature.parquet"
    cf_path   = DP / "condition_feature.parquet"

    # Persist canonical tables for downstream use.
    cond.to_parquet(cond_path, index=False)
    feature.to_parquet(feat_path, index=False)
    cf.to_parquet(cf_path, index=False)

    # Write small CSV previews for quick inspection and sanity checks.
    cond.head(200).to_csv(DP / "preview_condition.csv", index=False)
    feature.head(200).to_csv(DP / "preview_feature.csv", index=False)
    cf.head(200).to_csv(DP / "preview_condition_feature.csv", index=False)

    # Console summary of what was produced.
    print("=== DONE (HPO-only) ===")
    print(f"conditions        : ({len(cond):,}, 2) (unique condition_id: {cond['condition_id'].nunique():,})")
    print(f"features          : ({len(feature):,}, 3) (unique feature_id : {feature['feature_id'].nunique():,})")
    print(f"condition_feature : ({len(cf):,}, 3) (unique pairs       : {cf[['condition_id','feature_id']].drop_duplicates().shape[0]:,})")
    print(f"Labels loaded     : {n_lbl:,} (from TSVs)")
    print(f"Wrote: {cond_path.name}, {feat_path.name}, {cf_path.name} (+ previews) in {DP}")

# Allow `python -m src.build_tables` / direct run to execute the pipeline.
if __name__ == "__main__":
    main()
