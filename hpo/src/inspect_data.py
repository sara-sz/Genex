"""
Inspect the HPO datasets we built.
- Prints table shapes & quick samples
- Optionally shows matrix/X metadata if present

Usage:
  python -m src.inspect_data [--head 20] [--save-previews] [--hpo HP:0001250 HP:0004322 ...]

Notes:
- Looks only in data_proc/ for:
    condition.parquet, feature.parquet, condition_feature.parquet
- If X_hpo_csr.npz + mappings.json exist, prints their shapes.

Author: Sara soltanizadeh
Created: 2025-09-10
"""

from __future__ import annotations
from pathlib import Path
import argparse, json
import pandas as pd

# Define project root and processed-data directory.
ROOT = Path(__file__).resolve().parents[1]
DP   = ROOT / "data_proc"

# Expected input/output files from previous pipeline steps.
COND_F = DP / "condition.parquet"
FEAT_F = DP / "feature.parquet"
CF_F   = DP / "condition_feature.parquet"
X_F    = DP / "X_hpo_csr.npz"
MAP_F  = DP / "mappings.json"

# Safe parquet reader: warn + return None if file missing.
def _safe_read(p: Path):
    if not p.exists():
        print(f"[warn] missing: {p}")
        return None
    return pd.read_parquet(p)

# Main CLI entrypoint: show table info, samples, matrix meta, and optional previews.
def main():
    # Parse command-line arguments (head rows, save previews, filter HPO ids).
    ap = argparse.ArgumentParser()
    ap.add_argument("--head", type=int, default=20)
    ap.add_argument("--save-previews", action="store_true")
    ap.add_argument("--hpo", nargs="*", default=None,
                    help="Optional list of HPO ids to peek at in condition_feature.")
    args = ap.parse_args()

    # Try loading condition/feature/condition_feature tables.
    cond = _safe_read(COND_F)
    feat = _safe_read(FEAT_F)
    cf   = _safe_read(CF_F)

    # Print shape + uniqueness stats for loaded tables.
    print("\n=== TABLE SHAPES ===")
    if cond is not None:
        print(f"condition.parquet : {cond.shape}  (unique condition_id : {cond['condition_id'].nunique()})")
    if feat is not None:
        print(f"feature.parquet   : {feat.shape}  (unique feature_id   : {feat['feature_id'].nunique()})")
    if cf is not None:
        print(f"condition_feature : {cf.shape}  (unique pairs: {cf[['condition_id','feature_id']].drop_duplicates().shape[0]})")

    # Print head() samples of each table for inspection.
    n = args.head
    if cond is not None:
        print("\n=== SAMPLE CONDITIONS ===")
        print(cond.head(n))
    if feat is not None:
        print("\n=== SAMPLE FEATURES ===")
        print(feat.head(n))
    if cf is not None:
        print("\n=== SAMPLE PAIRS ===")
        show_cols = [c for c in ("condition_id","feature_id","weight") if c in cf.columns]
        print(cf[show_cols].head(n))

    # If matrix metadata exists, display it.
    if MAP_F.exists():
        meta = json.loads(MAP_F.read_text(encoding="utf-8"))
        print("\n=== MATRIX META (from mappings.json) ===")
        print(meta)
    else:
        print("\n(no mappings.json found — skip matrix meta)")

    # Optionally filter condition_feature by specific HPO ids and preview.
    # Peek at selected HPO terms in pairs
    if args.hpo and cf is not None:
        want = set(args.hpo)
        sub = cf[cf["feature_id"].isin(want)].copy()
        print(f"\n=== Pairs matching given HPO ids ({len(want)}) ===")
        show_cols = [c for c in ("condition_id","feature_id","weight") if c in sub.columns]
        print(sub[show_cols].head(max(5, n)))

    # Optionally save CSV previews for quick inspection in data_proc/.
    if args.save_previews:
        if cond is not None: cond.head(n).to_csv(DP/"preview_condition.csv", index=False)
        if feat is not None: feat.head(n).to_csv(DP/"preview_feature.csv", index=False)
        if cf   is not None: cf.head(n).to_csv(DP/"preview_condition_feature.csv", index=False)
        print("\nSaved CSV previews in data_proc/")

# Run as a script via `python file.py`.
if __name__ == "__main__":
    main()
