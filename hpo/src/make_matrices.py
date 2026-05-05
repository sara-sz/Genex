"""
make_matrices.py  (HPO-only, robust)

Description:
    Construct a sparse condition × feature matrix from preprocessed HPO tables.
    Reads normalized parquet files produced by `build_tables.py`, builds index
    mappings for conditions and features, and generates a CSR (Compressed Sparse
    Row) matrix for downstream ML tasks.

Inputs (data_proc/):
  - condition.parquet               [condition_id, name]
  - feature.parquet                 [feature_id, label, ic]
  - condition_feature.parquet       [condition_id, feature_id, weight]

Outputs (data_proc/):
  - X_hpo_csr.npz                   (scipy.sparse CSR matrix; shape = n_conditions × n_features)
  - mappings.json                   (JSON with idx_to_cond, idx_to_feat, meta stats)

Run:
  python -m src.make_matrices

Notes:
  - Ensures backward compatibility if the weight column is missing or renamed.
  - Drops edges with unmapped condition/feature IDs.
  - Duplicate edges are automatically summed during CSR conversion.

Author: Sara Soltanizadeh
Created: 2025-09-09
"""

from __future__ import annotations
from pathlib import Path
import json
import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix, save_npz

# Define project root and processed-data directory.
ROOT = Path(__file__).resolve().parents[1]
DP   = ROOT / "data_proc"

# Input parquet tables (built previously by build_tables.py).
COND_F = DP / "condition.parquet"
FEAT_F = DP / "feature.parquet"
CF_F   = DP / "condition_feature.parquet"

# Output files: sparse matrix (npz) and mappings (json).
X_OUT  = DP / "X_hpo_csr.npz"
MAP_F  = DP / "mappings.json"

# Main pipeline: load parquet inputs, build mappings, construct sparse matrix, save outputs.
def main():
    # Ensure prerequisites exist before running.
    assert COND_F.exists(), f"Missing {COND_F}. Run build_tables first."
    assert FEAT_F.exists(), f"Missing {FEAT_F}. Run build_tables first."
    assert CF_F.exists(),   f"Missing {CF_F}. Run build_tables first."

    # Load normalized tables.
    cond = pd.read_parquet(COND_F)
    feat = pd.read_parquet(FEAT_F)
    cf   = pd.read_parquet(CF_F)

    # Ensure a weight column exists (fallback if missing/renamed).
    if "weight" not in cf.columns:
        # Backward compatibility: rename or create weight
        alt = [c for c in cf.columns if "weight" in c]
        if alt:
            cf = cf.rename(columns={alt[0]: "weight"})
        else:
            cf["weight"] = 1.0

    # Build unique vocabularies for conditions and features.
    idx_to_cond = sorted(pd.Series(cond["condition_id"].astype(str)).unique().tolist())
    idx_to_feat = sorted(pd.Series(feat["feature_id"].astype(str)).unique().tolist())
    n_rows, n_cols = len(idx_to_cond), len(idx_to_feat)

    # Create mappings: ID → row/col index.
    cond_to_idx = {cid: i for i, cid in enumerate(idx_to_cond)}
    feat_to_idx = {fid: j for j, fid in enumerate(idx_to_feat)}

    # Map condition_feature edges into row/col index arrays.
    rows = cf["condition_id"].astype(str).map(cond_to_idx)
    cols = cf["feature_id"].astype(str).map(feat_to_idx)
    w    = cf["weight"].astype(float)

    # Drop invalid edges with missing mappings.
    mask = rows.notna() & cols.notna()
    dropped = int((~mask).sum())
    if dropped:
        print(f"Warning: dropped {dropped} edges with unknown ids")

    rows = rows[mask].astype(np.int64).values
    cols = cols[mask].astype(np.int64).values
    w    = w[mask].values

    # Build sparse COO matrix and convert to CSR (duplicates summed automatically).
    X = coo_matrix((w, (rows, cols)), shape=(n_rows, n_cols)).tocsr()

    # Save sparse matrix to disk.
    save_npz(X_OUT, X)

    # Save index mappings + metadata for reproducibility.
    meta = {"n_rows": n_rows, "n_cols": n_cols, "nnz": int(X.nnz)}
    with MAP_F.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "idx_to_cond": idx_to_cond,
                "idx_to_feat": idx_to_feat,
                "meta": meta,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    # Print summary of outputs for quick verification.
    print(f"Saved {X_OUT} and {MAP_F}")
    print(f"Matrix shape=({n_rows:,}, {n_cols:,}), nnz={X.nnz:,}")

# Run the pipeline if called as a script.
if __name__ == "__main__":
    main()
