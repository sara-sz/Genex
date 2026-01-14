from pathlib import Path
import json
import numpy as np
import pandas as pd
from scipy.sparse import coo_matrix, save_npz

DP = Path("data_proc")

def main():
    DP.mkdir(parents=True, exist_ok=True)

    # Load parquet tables (created by build_tables.py)
    cond = pd.read_parquet(DP / "condition.parquet", columns=["condition_id", "name"]).drop_duplicates("condition_id")
    feat = pd.read_parquet(DP / "feature.parquet",   columns=["feature_id", "label"]).drop_duplicates("feature_id")

    # For condition_feature, read all columns (names can vary: 'frequency_weight' vs 'weight')
    cf = pd.read_parquet(DP / "condition_feature.parquet")

    # Normalize expected columns
    for col in ("condition_id", "feature_id"):
        if col not in cf.columns:
            raise KeyError(f"Missing required column '{col}' in condition_feature.parquet. Found: {list(cf.columns)}")

    # Pick a value column robustly
    value_col = next((c for c in ["frequency_weight", "weight", "val", "value"] if c in cf.columns), None)
    if value_col is None:
        # If nothing exists, treat as unweighted edges
        cf["__val__"] = 1.0
        value_col = "__val__"

    # Standardize types for deterministic joins/JSON
    cond["condition_id"] = cond["condition_id"].astype(str)
    feat["feature_id"]   = feat["feature_id"].astype(str)
    cf["condition_id"]   = cf["condition_id"].astype(str)
    cf["feature_id"]     = cf["feature_id"].astype(str)

    # Deterministic ordering of rows/cols
    cond = cond.sort_values("condition_id").reset_index(drop=True)
    feat = feat.sort_values("feature_id").reset_index(drop=True)

    # Build id <-> index maps
    row_to_condition_id = cond["condition_id"].tolist()
    col_to_feature_id   = feat["feature_id"].tolist()
    condition_id_to_row = {cid: i for i, cid in enumerate(row_to_condition_id)}
    feature_id_to_col   = {fid: j for j, fid in enumerate(col_to_feature_id)}

    # Map pairs to matrix coords and clean the value column
    cf = (
        cf.assign(
            row = cf["condition_id"].map(condition_id_to_row),
            col = cf["feature_id"].map(feature_id_to_col),
            val = pd.to_numeric(cf[value_col], errors="coerce").fillna(0.5).clip(0, 1)
        )
        .dropna(subset=["row", "col"])
    )

    rows = cf["row"].astype(int).to_numpy()
    cols = cf["col"].astype(int).to_numpy()
    vals = cf["val"].astype(float).to_numpy()

    X = coo_matrix((vals, (rows, cols)), shape=(len(row_to_condition_id), len(col_to_feature_id))).tocsr()

    # Save matrix
    save_npz(DP / "X_hpo_csr.npz", X)

    # Save mappings with BOTH naming styles (aliases)
    mappings = {
        # Primary names
        "row_to_condition_id": row_to_condition_id,
        "col_to_feature_id":   col_to_feature_id,
        "condition_id_to_row": condition_id_to_row,
        "feature_id_to_col":   feature_id_to_col,
        # Aliases some scripts expect
        "idx_to_condition_id": row_to_condition_id,
        "condition_id_to_idx": condition_id_to_row,
        "idx_to_feature_id":   col_to_feature_id,
        "feature_id_to_idx":   feature_id_to_col,
        # Meta
        "meta": {"n_rows": int(X.shape[0]), "n_cols": int(X.shape[1]), "nnz": int(X.nnz)}
    }
    (DP / "mappings.json").write_text(json.dumps(mappings, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Saved {DP/'X_hpo_csr.npz'} and {DP/'mappings.json'}")
    print(f"Matrix shape={X.shape}, nnz={X.nnz}")

if __name__ == "__main__":
    main()
