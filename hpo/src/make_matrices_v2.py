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
  - X_hpo_weight_csr.npz            (weight matrix)
  - X_hpo_weight_norm_csr.npz       (row-normalized weight matrix)
  - X_hpo_weightic_csr.npz          (weight × IC matrix)
  - X_hpo_weightic_norm_csr.npz     (row-normalized weight × IC matrix)
  - mappings.json                   (shared row/col order + meta)

Run:
  python -m src.make_matrices

Notes:
  - Ensures backward compatibility if the weight column is missing or renamed.
  - Drops edges with unmapped condition/feature IDs.
  - Duplicate edges are automatically summed during CSR conversion.
  - If 'ic' is missing in feature.parquet, IC is computed as:
        IC(h) = -log( (df(h)+1) / (N_conditions+1) )
    where df(h) counts conditions with that feature (binary presence).

Author: Sara Soltanizadeh
Created: 2025-09-09
Last modified: 25-09-17
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
X_WEIGHT_OUT         = DP / "X_hpo_weight_csr.npz"
X_WEIGHT_NORM_OUT    = DP / "X_hpo_weight_norm_csr.npz"
X_WEIGHTIC_OUT       = DP / "X_hpo_weightic_csr.npz"
X_WEIGHTIC_NORM_OUT  = DP / "X_hpo_weightic_norm_csr.npz"
MAPPINGS_OUT         = DP / "mappings.json"

# Check that required input parquet files exist
def _ensure_inputs():
    assert COND_F.exists(), f"Missing {COND_F}. Run build_tables first."
    assert FEAT_F.exists(), f"Missing {FEAT_F}. Run build_tables first."
    assert CF_F.exists(),   f"Missing {CF_F}. Run build_tables first."


# Load parquet input tables and ensure a usable 'weight' column
def _load_tables():
    cond = pd.read_parquet(COND_F)
    feat = pd.read_parquet(FEAT_F)
    cf   = pd.read_parquet(CF_F)

    if "weight" not in cf.columns:
        alt = [c for c in cf.columns if "weight" in c.lower()]
        if alt:
            cf = cf.rename(columns={alt[0]: "weight"})
        else:
            cf["weight"] = 1.0

    return cond, feat, cf


# Build vocabularies and mapping dicts for condition_id and feature_id
def _build_vocab(cond: pd.DataFrame, feat: pd.DataFrame):
    idx_to_cond = sorted(cond["condition_id"].astype(str).unique().tolist())
    idx_to_feat = sorted(feat["feature_id"].astype(str).unique().tolist())
    cond_to_idx = {cid: i for i, cid in enumerate(idx_to_cond)}
    feat_to_idx = {fid: j for j, fid in enumerate(idx_to_feat)}
    return idx_to_cond, idx_to_feat, cond_to_idx, feat_to_idx


# Convert condition_feature edges into row/col arrays + weights
def _edges_to_arrays(cf: pd.DataFrame, cond_to_idx, feat_to_idx):
    rows = cf["condition_id"].astype(str).map(cond_to_idx)
    cols = cf["feature_id"].astype(str).map(feat_to_idx)
    w    = cf["weight"].astype(float)

    mask = rows.notna() & cols.notna()
    dropped = int((~mask).sum())
    if dropped:
        print(f"Warning: dropped {dropped} edges with unknown ids")

    rows = rows[mask].astype(np.int64).values
    cols = cols[mask].astype(np.int64).values
    w    = w[mask].values
    return rows, cols, w


# Helper to build a CSR sparse matrix from row/col/data arrays
def _coo_to_csr(rows: np.ndarray, cols: np.ndarray, data: np.ndarray, shape: tuple[int, int]) -> csr_matrix:
    X = coo_matrix((data, (rows, cols)), shape=shape, dtype=np.float32).tocsr()
    X.sum_duplicates()
    return X


# Row-normalize a CSR matrix with L2 norm (each condition vector length = 1)
def _row_normalize_l2(X: csr_matrix) -> csr_matrix:
    X = X.tocsr(copy=True)
    sq = X.multiply(X).sum(axis=1).A.ravel()
    inv = np.zeros_like(sq, dtype=np.float32)
    nz = sq > 0.0
    inv[nz] = (1.0 / np.sqrt(sq[nz])).astype(np.float32)
    X.data *= np.repeat(inv, np.diff(X.indptr))
    return X


# Construct the base weight matrix directly from condition_feature weights
def _build_weight_matrix(rows, cols, weights, shape) -> csr_matrix:
    return _coo_to_csr(rows, cols, weights.astype(np.float32), shape)


# Create an IC vector aligned to feature column order (or compute fallback)
def _align_ic_vector(feat: pd.DataFrame, idx_to_feat: list[str], n_cols: int, X_weight: csr_matrix) -> np.ndarray:
    ic_vec = None
    if "ic" in feat.columns:
        ic_series = (
            feat.assign(feature_id=feat["feature_id"].astype(str))
                .set_index("feature_id")["ic"]
        )
        ic_vec = np.array([float(ic_series.get(fid, np.nan)) for fid in idx_to_feat], dtype=np.float32)
        if np.isnan(ic_vec).any():
            med = np.nanmedian(ic_vec) if np.isfinite(np.nanmedian(ic_vec)) else 1.0
            ic_vec = np.nan_to_num(ic_vec, nan=med).astype(np.float32)
    else:
        Xbin = X_weight.copy().tocsr()
        if Xbin.nnz:
            Xbin.data[:] = 1.0
        n_rows = Xbin.shape[0]
        df = np.asarray(Xbin.sum(axis=0)).ravel().astype(np.float64)
        ic_vec = (-np.log((df + 1.0) / (n_rows + 1.0))).astype(np.float32)
        print("Info: 'ic' not in feature.parquet; computed IC from matrix presence.")

    ic_vec[~np.isfinite(ic_vec)] = np.nan
    if np.isnan(ic_vec).any():
        med = np.nanmedian(ic_vec) if np.isfinite(np.nanmedian(ic_vec)) else 1.0
        ic_vec = np.nan_to_num(ic_vec, nan=med).astype(np.float32)

    assert ic_vec.shape[0] == n_cols, "IC vector not aligned with matrix columns."
    return ic_vec


# Main pipeline: build weight/weight_norm/weight×IC/weight×IC_norm and save all outputs
def main():
    _ensure_inputs()
    cond, feat, cf = _load_tables()

    idx_to_cond, idx_to_feat, cond_to_idx, feat_to_idx = _build_vocab(cond, feat)
    n_rows, n_cols = len(idx_to_cond), len(idx_to_feat)

    rows, cols, w = _edges_to_arrays(cf, cond_to_idx, feat_to_idx)

    X_weight = _build_weight_matrix(rows, cols, w, (n_rows, n_cols))
    X_weight_norm = _row_normalize_l2(X_weight)

    ic_vec = _align_ic_vector(feat, idx_to_feat, n_cols, X_weight)
    X_weightic = X_weight.multiply(ic_vec)
    X_weightic_norm = _row_normalize_l2(X_weightic)

    save_npz(X_WEIGHT_OUT,        X_weight)
    save_npz(X_WEIGHT_NORM_OUT,   X_weight_norm)
    save_npz(X_WEIGHTIC_OUT,      X_weightic)
    save_npz(X_WEIGHTIC_NORM_OUT, X_weightic_norm)

    meta = {
        "weight":          {"n_rows": n_rows, "n_cols": n_cols, "nnz": int(X_weight.nnz)},
        "weight_norm":     {"n_rows": n_rows, "n_cols": n_cols, "nnz": int(X_weight_norm.nnz)},
        "weightic":        {"n_rows": n_rows, "n_cols": n_cols, "nnz": int(X_weightic.nnz)},
        "weightic_norm":   {"n_rows": n_rows, "n_cols": n_cols, "nnz": int(X_weightic_norm.nnz)},
    }
    with MAPPINGS_OUT.open("w", encoding="utf-8") as f:
        json.dump(
            {"cond_ids": idx_to_cond, "feat_ids": idx_to_feat, "meta": meta},
            f, ensure_ascii=False, indent=2,
        )

    print("Saved:")
    print(f"  {X_WEIGHT_OUT}")
    print(f"  {X_WEIGHT_NORM_OUT}")
    print(f"  {X_WEIGHTIC_OUT}")
    print(f"  {X_WEIGHTIC_NORM_OUT}")
    print(f"  {MAPPINGS_OUT}\n")

    print(f"weight:          shape=({n_rows}, {n_cols}), nnz={X_weight.nnz}")
    print(f"weight (norm):   shape=({n_rows}, {n_cols}), nnz={X_weight_norm.nnz}")
    print(f"weight×IC:       shape=({n_rows}, {n_cols}), nnz={X_weightic.nnz}")
    print(f"weight×IC (norm):shape=({n_rows}, {n_cols}), nnz={X_weightic_norm.nnz}")


# Run pipeline if called directly
if __name__ == "__main__":
    main()