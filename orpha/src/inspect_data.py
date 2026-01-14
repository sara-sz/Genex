# src/inspect_data.py
from __future__ import annotations
import argparse, json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.sparse import load_npz


DP = Path("data_proc")


def _read_parquet(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Missing file: {path}")
    return pd.read_parquet(path)


def _load_tables():
    cond = _read_parquet(DP / "condition.parquet")
    feat = _read_parquet(DP / "feature.parquet")
    cf   = _read_parquet(DP / "condition_feature.parquet")
    return cond, feat, cf


def _load_matrix():
    p = DP / "X_hpo_csr.npz"
    if not p.exists():
        raise FileNotFoundError(f"Missing file: {p} (run: python -m src.make_matrices)")
    return load_npz(p)


def _load_mappings():
    p = DP / "mappings.json"
    if not p.exists():
        # allow running without matrix/mappings; just shapes
        return None
    m = json.loads(p.read_text(encoding="utf-8"))

    # Support old/new key sets
    if all(k in m for k in ["idx_to_cond", "idx_to_feat", "cond_to_idx", "feat_to_idx"]):
        return m

    expected_legacy = ["row_to_condition_id", "col_to_feature_id",
                       "condition_id_to_row", "feature_id_to_col"]
    if all(k in m for k in expected_legacy):
        return {
            "idx_to_cond": m["row_to_condition_id"],
            "idx_to_feat": m["col_to_feature_id"],
            "cond_to_idx": m["condition_id_to_row"],
            "feat_to_idx": m["feature_id_to_col"],
            "meta": m.get("meta", {})
        }

    raise ValueError(
        "mappings.json is missing expected keys. "
        "Regenerate it with: python -m src.make_matrices"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--hpo",
        nargs="*",
        default=None,
        help="HPO IDs, e.g. HP:0001250 HP:0004322. Optional: if omitted, only shapes are printed."
    )
    ap.add_argument("--head", type=int, default=20, help="How many preview rows to print (default 20).")
    ap.add_argument("--save-previews", action="store_true",
                    help="Also write small CSV previews into data_proc/_previews/")
    ap.add_argument("--use-ic", action="store_true",
                    help="(info only) flag that parallels baseline script; not used here.")
    args = ap.parse_args()

    cond, feat, cf = _load_tables()
    print("\n=== TABLE SHAPES ===")
    print(f"condition.parquet : {cond.shape}  (unique condition_id : {cond['condition_id'].nunique()})")
    print(f"feature.parquet   : {feat.shape}  (unique feature_id   : {feat['feature_id'].nunique()})")
    print(f"condition_feature : {cf.shape}  (unique pairs: {cf[['condition_id','feature_id']].drop_duplicates().shape[0]})")

    # Matrix + meta are optional to load (only if they exist)
    try:
        X = _load_matrix()
        print(f"X_hpo_csr         : shape={X.shape}, nnz={X.nnz}")
    except FileNotFoundError:
        X = None
        print("X_hpo_csr         : (not found — generate with `python -m src.make_matrices`)")

    m = _load_mappings()
    if m:
        meta = m.get("meta", {})
        print(f"meta              : {meta}")
    else:
        print("meta              : (no mappings.json)")

    if not args.hpo:
        print("\nTip: pass --hpo HP:... to preview matching rows and optionally save CSVs.")
        return

    # Filter feature + pairs for selected HPOs
    hpo_set = set(args.hpo)
    missing = [h for h in hpo_set if h not in set(feat["feature_id"])]
    if missing:
        print(f"\nWarning: {len(missing)} HPO IDs not found: {', '.join(missing[:10])}"
              + (" ..." if len(missing) > 10 else ""))

    feat_sel = feat[feat["feature_id"].isin(hpo_set)].copy()
    cf_sel = cf[cf["feature_id"].isin(hpo_set)].merge(
        cond[["condition_id", "name"]], on="condition_id", how="left"
    ).merge(
        feat_sel[["feature_id", "label"]], on="feature_id", how="left"
    )

    print("\n=== SAMPLE ROWS ===")
    print("Conditions:\n", cond[["condition_id", "name"]].head(args.head), "\n")
    print("Features  :\n", feat[["feature_id", "label"]].head(args.head), "\n")
    print("Pairs     :\n", cf_sel[["condition_id", "feature_id"]].head(args.head), "\n")

    if args.save_previews:
        outdir = DP / "_previews"
        outdir.mkdir(parents=True, exist_ok=True)
        cond.head(args.head).to_csv(outdir / "condition_head.csv", index=False)
        feat.head(args.head).to_csv(outdir / "feature_head.csv", index=False)
        cf_sel.head(args.head).to_csv(outdir / "pairs_filtered_head.csv", index=False)
        print(f"Saved CSV previews in {outdir}/")


if __name__ == "__main__":
    main()
