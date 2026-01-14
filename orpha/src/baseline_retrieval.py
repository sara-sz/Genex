# src/baseline_retrieval.py

from __future__ import annotations
import argparse
import json
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
from scipy.sparse import load_npz, issparse

DP = Path("data_proc")

def _flatten(x) -> np.ndarray:
    """Return a 1D numpy array regardless of dense/sparse/matrix types."""
    if issparse(x):
        return x.A.ravel()          # sparse -> ndarray
    arr = np.asarray(x)             # dense -> ndarray
    return arr.ravel()

def _load_mappings(p: Path):
    m = json.loads(p.read_text(encoding="utf-8"))
    # expected keys set by make_matrices.py
    need = {"row_to_condition_id","col_to_feature_id",
            "condition_id_to_row","feature_id_to_col"}
    missing = need - set(m)
    if missing:
        raise ValueError(f"mappings.json missing keys: {sorted(missing)}")
    # turn lists into friendly dicts
    row_to_cond = m["row_to_condition_id"]
    col_to_feat = m["col_to_feature_id"]
    cond_to_row = m["condition_id_to_row"]
    feat_to_col = m["feature_id_to_col"]
    return row_to_cond, col_to_feat, cond_to_row, feat_to_col

def _pick_feature_indices(hpo_list, feat_to_col: dict[str,int]) -> tuple[list[int], list[str], list[str]]:
    seen = set()
    cols, ok, bad = [], [], []
    for h in hpo_list:
        h = h.strip().upper()
        if not h:
            continue
        if h in seen:
            continue
        seen.add(h)
        j = feat_to_col.get(h)
        if j is None:
            bad.append(h)
        else:
            ok.append(h); cols.append(j)
    return cols, ok, bad

def run_query(hpo_terms: list[str], topk: int, use_ic: bool, save_csv: bool):
    # Load artifacts
    X = load_npz(DP / "X_hpo_csr.npz")                  # (n_cond, n_feat) CSR
    row_to_cond, col_to_feat, cond_to_row, feat_to_col = _load_mappings(DP / "mappings.json")
    feat = pd.read_parquet(DP / "feature.parquet")      # has columns: feature_id, label, ic
    cond = pd.read_parquet(DP / "condition.parquet")    # has columns: condition_id, name, ...

    # choose feature columns
    jlist, ok, bad = _pick_feature_indices(hpo_terms, feat_to_col)
    if bad:
        print(f"[warn] {len(bad)} HPO codes not found and were ignored: {', '.join(bad)}")
    if not jlist:
        raise SystemExit("No valid HPO codes given. Nothing to score.")

    # weights: IC or ones
    if use_ic:
        ic_map = dict(zip(feat["feature_id"].astype(str), feat["ic"].fillna(0.0)))
        w_vals = [float(ic_map.get(col_to_feat[j], 0.0)) for j in jlist]
    else:
        w_vals = [1.0 for _ in jlist]

    w = np.asarray(w_vals, dtype=float)           # (m,)
    sub = X[:, jlist]                             # (n_cond, m) sparse

    # score = sub @ w (sum of selected cols, weighted)
    scores = _flatten(sub @ w)                    # robust flatten

    # rank
    idx = np.argsort(-scores)[:topk]
    top_rows = []
    for r in idx:
        cid = row_to_cond[r]
        name = cond.loc[cond["condition_id"] == cid, "name"]
        name = name.iloc[0] if len(name) else ""
        top_rows.append((cid, name, float(scores[r])))

    df = pd.DataFrame(top_rows, columns=["condition_id","name","score"])

    # pretty print
    print("\n=== Baseline retrieval ===")
    print("HPO used :", ", ".join(ok))
    print("Weights  :", ", ".join(f"{v:.2f}" for v in w))
    print("\nTop results:")
    with pd.option_context("display.max_colwidth", 90):
        print(df.to_string(index=False))

    # optional CSV
    if save_csv:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = DP / f"retrieval_{ts}.csv"
        df.to_csv(out, index=False)
        print(f"\nSaved: {out}")

    return df

def main():
    ap = argparse.ArgumentParser(description="Simple HPO → condition retrieval (cosine-ish sum of weighted columns).")
    ap.add_argument("--hpo", nargs="+", required=True, help="HPO terms (e.g., HP:0001250 HP:0004322)")
    ap.add_argument("--topk", type=int, default=10, help="Top results to show")
    ap.add_argument("--use-ic", action="store_true", help="Weight columns by IC from feature.parquet")
    ap.add_argument("--save-csv", action="store_true", help="Save results to data_proc/")
    args = ap.parse_args()

    print(f"HPO set: {', '.join(args.hpo)}")
    run_query(args.hpo, topk=args.topk, use_ic=args.use_ic, save_csv=args.save_csv)

if __name__ == "__main__":
    main()


# Quick ways to improve:

# Cosine/Jaccard normalization: divide by condition length or query length.
# Ancestor expansion: add all ancestor HPOs for each query HPO (with smaller weights).
# Bayesian baseline: treat each HPO as evidence with P(hpo|condition) and multiply/posterior.
# Learning-to-rank: collect labeled cases, train a small model on these features.
