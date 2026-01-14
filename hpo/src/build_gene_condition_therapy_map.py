"""
GeneX: Build unified Gene–Condition–Phenotype (HPO) → Therapy dataset
=====================================================================
This script joins three layers to produce a therapy-aware knowledge table:

1) HPO therapy-relevant terms with tags
   - data_proc/dev_therapy_terms.csv
     columns: feature_id (HPO ID), label, definition, keyword
   - data_proc/therapy_relevant_terms_tagged.csv
     columns: label, label_clean, therapy_category  (and optionally feature_id)

2) Gene → Phenotype (+ condition) relationships (from HPO)
   - data_raw/hpo/genes_to_phenotype.txt (tab-separated, standard HPO export)
     expected columns after parsing:
       gene_symbol, gene_id, hpo_id, hpo_label, condition_name, condition_id

Output 1 (long-form, row-per-phenotype):
data_proc/gene_condition_therapy_map.csv
columns:
  gene_symbol, gene_id,
  condition_name, condition_id,
  hpo_id, hpo_label, hpo_definition, keyword,
  therapy_category

Output 2 (compact condition summary):
data_proc/condition_to_therapies.csv
columns:
  condition_id, condition_name, therapy_category, n_phenotypes, example_hpo_labels

Notes
-----
- Multiple rows per condition are expected (one per therapy-relevant phenotype).
- If 'therapy_relevant_terms_tagged.csv' lacks feature_id, we re-attach it by
  merging on exact 'label' from dev_therapy_terms.csv.
- All joins are INNER on HPO ID, so only therapy-relevant phenotypes are retained.
"""

import os
import pandas as pd

# ---------- Paths ----------
PROC_DIR = "data_proc"
RAW_DIR  = "data_raw/hpo"

DEV_TERMS_PATH = os.path.join(PROC_DIR, "dev_therapy_terms.csv")
TAGS_PATH      = os.path.join(PROC_DIR, "therapy_relevant_terms_tagged.csv")
GENE_PHENO_PATH= os.path.join(RAW_DIR, "genes_to_phenotype.txt")

OUT_LONG   = os.path.join(PROC_DIR, "gene_condition_therapy_map.csv")
OUT_SUMMARY= os.path.join(PROC_DIR, "condition_to_therapies.csv")

print("🔹 Loading inputs...")
dev_terms = pd.read_csv(DEV_TERMS_PATH)  # feature_id, label, definition, keyword
tags      = pd.read_csv(TAGS_PATH)       # label, label_clean, therapy_category (maybe feature_id)
print(f"  - dev_therapy_terms.csv: {len(dev_terms)} rows")
print(f"  - therapy_relevant_terms_tagged.csv: {len(tags)} rows")

# ---------- Ensure we have HPO IDs with the tags ----------
# We want: tags_with_ids: (hpo_id, hpo_label, hpo_definition, keyword, therapy_category)
# If tags already contains 'feature_id' (same as hpo_id), use it.
if "feature_id" in tags.columns:
    tags_with_ids = tags.merge(
        dev_terms[["feature_id", "label", "definition", "keyword"]],
        on=["feature_id", "label"],
        how="left"
    )
else:
    # Fall back: attach HPO IDs by joining on label (exact)
    tags_with_ids = tags.merge(
        dev_terms[["feature_id", "label", "definition", "keyword"]],
        on="label",
        how="left"
    )

# Rename to normalized column names
tags_with_ids = tags_with_ids.rename(
    columns={
        "feature_id": "hpo_id",
        "label": "hpo_label",
        "definition": "hpo_definition"
    }
)

# Basic sanity
missing_ids = tags_with_ids["hpo_id"].isna().sum()
if missing_ids > 0:
    print(f"Warning: {missing_ids} tagged rows lack HPO IDs after merge. They will be dropped.")
tags_with_ids = tags_with_ids.dropna(subset=["hpo_id"]).drop_duplicates(subset=["hpo_id", "therapy_category", "hpo_label"])

print(f"  - tags_with_ids: {len(tags_with_ids)} rows (therapy-tagged HPO terms with IDs)")

# ---------- Load gene → phenotype (+ condition) map ----------
# Standard HPO 'genes_to_phenotype.txt' has a header row or comments; we handle both cases.
# Typical column order: gene_symbol  gene_id  hpo_id  hpo_label  disease_name  disease_id
genes_pheno = pd.read_csv(
    GENE_PHENO_PATH,
    sep="\t",
    comment="#",
    header=None,
    names=["gene_symbol", "gene_id", "hpo_id", "hpo_label_from_hpo", "condition_name", "condition_id"],
    dtype=str
)

# Keep only needed columns and de-duplicate
genes_pheno = genes_pheno[["gene_symbol", "gene_id", "hpo_id", "hpo_label_from_hpo", "condition_name", "condition_id"]].drop_duplicates()
print(f"  - genes_to_phenotype.txt: {len(genes_pheno)} rows (raw)")

# ---------- Join only therapy-relevant phenotypes ----------
merged = genes_pheno.merge(
    tags_with_ids[["hpo_id", "hpo_label", "hpo_definition", "keyword", "therapy_category"]],
    on="hpo_id",
    how="inner"
)

# Prefer the curated hpo_label if present; fall back to HPO’s label otherwise.
merged["hpo_label_final"] = merged["hpo_label"].fillna(merged["hpo_label_from_hpo"])
merged = merged.drop(columns=["hpo_label_from_hpo", "hpo_label"]).rename(columns={"hpo_label_final": "hpo_label"})

# Final tidy & sort
final_df = (
    merged[[
        "gene_symbol", "gene_id",
        "condition_name", "condition_id",
        "hpo_id", "hpo_label", "hpo_definition", "keyword",
        "therapy_category"
    ]]
    .drop_duplicates()
    .sort_values(["condition_name", "gene_symbol", "hpo_id", "therapy_category"], na_position="last")
)

print(f"Built long-form table with {len(final_df)} rows")

# ---------- Save long form ----------
final_df.to_csv(OUT_LONG, index=False)
print(f"Saved long-form master → {OUT_LONG}")

# ---------- Build compact per-condition summary ----------
def agg_examples(series, k=3):
    # show up to k unique examples for readability
    return "; ".join(series.dropna().unique()[:k])

summary = (
    final_df
    .groupby(["condition_id", "condition_name", "therapy_category"], as_index=False)
    .agg(n_phenotypes=("hpo_id", "nunique"),
         example_hpo_labels=("hpo_label", agg_examples))
    .sort_values(["condition_name", "therapy_category"])
)

summary.to_csv(OUT_SUMMARY, index=False)
print(f"Saved condition → therapies summary → {OUT_SUMMARY}")

# ---------- Quick printouts ----------
print("\n=== Sample (long-form) ===")
print(final_df.head(12).to_string(index=False))

print("\n=== Sample (per-condition summary) ===")
print(summary.head(12).to_string(index=False))

print("\nReady: search by gene_symbol or condition_name to see therapy-tagged phenotypes.")
