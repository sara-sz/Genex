# src/inspect_nulls.py
import argparse, json, re
from pathlib import Path
import pandas as pd

DP = Path("data_proc")
RAW = Path("data_raw")

HP_ID_RE = re.compile(r"^HP:\d+$")
ORPHA_ID_RE = re.compile(r"^ORPHA:\d+$")

def _summary(df: pd.DataFrame, name: str) -> pd.DataFrame:
    out = pd.DataFrame({
        "column": df.columns,
        "dtype": [str(df[c].dtype) for c in df.columns],
        "n_null": [int(df[c].isna().sum()) for c in df.columns],
        "pct_null": [float(df[c].isna().mean()) for c in df.columns],
        "n_unique": [int(df[c].nunique(dropna=True)) for c in df.columns],
    })
    out.insert(0, "table", name)
    return out

def _load_hp_labels_from_json(hp_json: Path) -> dict[str, str]:
    """Return {HP:nnnnnnn -> label} from hp.json."""
    g = json.loads(hp_json.read_text(encoding="utf-8"))
    nodes = g.get("graphs", [])[0].get("nodes", [])
    labels = {}
    for n in nodes:
        curie = n.get("id")
        if curie and curie.startswith("HP:"):
            labels[curie] = n.get("lbl")
    return labels

def _check_ids(df_feat: pd.DataFrame, df_cond: pd.DataFrame) -> pd.DataFrame:
    problems = []
    bad_feat = df_feat.loc[~df_feat["feature_id"].astype(str).str.match(HP_ID_RE)]
    for _, r in bad_feat.iterrows():
        problems.append({"table":"feature", "row_id": r.get("feature_id"),
                         "issue":"bad_feature_id_format"})
    bad_cond = df_cond.loc[~df_cond["condition_id"].astype(str).str.match(ORPHA_ID_RE)]
    for _, r in bad_cond.iterrows():
        problems.append({"table":"condition", "row_id": r.get("condition_id"),
                         "issue":"bad_condition_id_format"})
    return pd.DataFrame(problems)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save-csv", action="store_true", help="Write CSV summaries in data_proc/diagnostics/")
    ap.add_argument("--backfill-labels", action="store_true",
                    help="If any feature.label is missing, try to backfill from data_raw/hpo/hp.json and overwrite feature.parquet.")
    args = ap.parse_args()

    dp = DP; outdir = dp / "diagnostics"; outdir.mkdir(parents=True, exist_ok=True)

    cond = pd.read_parquet(dp / "condition.parquet")
    feat = pd.read_parquet(dp / "feature.parquet")
    cf   = pd.read_parquet(dp / "condition_feature.parquet")

    print("\n=== NULLS SUMMARY ===")
    s1 = _summary(cond, "condition")
    s2 = _summary(feat, "feature")
    s3 = _summary(cf,   "condition_feature")
    summary = pd.concat([s1, s2, s3], ignore_index=True)
    print(summary.to_string(index=False))

    # Check ID formats
    id_issues = _check_ids(feat, cond)
    if not id_issues.empty:
        print("\n=== ID FORMAT ISSUES ===")
        print(id_issues.to_string(index=False))
    else:
        print("\nNo ID format issues found.")

    # Show any feature labels that are null/empty
    missing_label = feat["label"].isna() | (feat["label"].astype(str).str.strip().eq("") | feat["label"].astype(str).eq("None"))
    n_missing = int(missing_label.sum())
    print(f"\nMissing/empty feature labels: {n_missing}")
    if n_missing:
        print(feat.loc[missing_label, ["feature_id","label"]].head(20).to_string(index=False))

    # Optional: backfill labels from hp.json (if any remain missing)
    if args.backfill_labels and n_missing:
        hp_json = RAW / "hpo" / "hp.json"
        if not hp_json.exists():
            print(f"\nCannot backfill: {hp_json} not found.")
        else:
            hp_labels = _load_hp_labels_from_json(hp_json)
            patched = 0
            idxs = feat.index[missing_label].tolist()
            for i in idxs:
                fid = str(feat.at[i, "feature_id"])
                lab = hp_labels.get(fid)
                if lab and str(lab).strip():
                    feat.at[i, "label"] = lab
                    patched += 1
            feat.to_parquet(dp / "feature.parquet", index=False)
            print(f"\nBackfilled {patched} labels from hp.json and overwrote feature.parquet.")
            # Recompute and show final count
            n_missing2 = int((feat["label"].isna() | feat["label"].astype(str).str.strip().eq("") | feat["label"].astype(str).eq("None")).sum())
            print(f"Remaining missing labels after backfill: {n_missing2}")

    if args.save_csv:
        summary.to_csv(outdir / "nulls_summary.csv", index=False)
        if not id_issues.empty:
            id_issues.to_csv(outdir / "id_issues.csv", index=False)
        # also dump small heads to help quick inspection
        cond.head(50).to_csv(outdir / "condition_head.csv", index=False)
        feat.head(50).to_csv(outdir / "feature_head.csv", index=False)
        cf.head(50).to_csv(outdir / "condition_feature_head.csv", index=False)
        print(f"\nSaved CSV reports to {outdir}/")

if __name__ == "__main__":
    main()
