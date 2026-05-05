# Phenotype → Condition Retrieval & Condition Clustering (HPO)

This project builds a **simple, reproducible pipeline** around the Human Phenotype Ontology (HPO) to:
- turn the official HPO annotations into clean tables,
- build a sparse matrix (conditions × HPO terms),
- sanity‑check the data (nulls, types, shapes),
- [to be continued with the rest of the tasks we perform like EDA, MLS, Evaluation etc.]

> Everything is derived from the **HPOA** file and HPO IDs.

---

## TL;DR — Quick Start

> In PowerShell on Windows (adjust `python` path if you use macOS/Linux).

```powershell
# 1) Create/activate venv and install deps (first time only)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip wheel
python -m pip install -r requirements.txt

# 2) Put data under data_raw/hpo/ (see “Get the data” below)
# you can also download the data directly from hpo website and put it under data_raw/hpo/
.\.venv\Scripts\python -m src.download_hpo_data

# 3) Build clean tables (HPO → parquet)
.\.venv\Scripts\python -m src.build_tables

# 4) Build the sparse matrix + id maps
.\.venv\Scripts\python -m src.make_matrices

# 5) Inspect + EDA (either)
.\.venv\Scripts\python -m src.inspect_nulls
.\.venv\Scripts\python -m src.inspect_data

```

Outputs land in **`data_proc/`**:
- `condition.parquet`, `feature.parquet`, `condition_feature.parquet`
- `X_hpo_csr.npz` (sparse matrix), `mappings.json` (row/col ID maps)
- `diagnostics/` (CSV snapshots)

---

## 1) What’s in this repo?

```
src/
  build_tables.py       # HPOA -> tidy parquet tables
  make_matrices.py      # tables -> sparse CSR matrix + mappings
  inspect_nulls.py      # nulls/types/ID-format checks
  inspect_data.py       # quick stats + samples
  utils.py              # small helpers (incl. label fallback)
data_raw/
  hpo/
    phenotype.hpoa     # REQUIRED (HPO annotations)
    hp.json            # OPTIONAL (labels; fallback is built‑in)
data_proc/
  (created by the scripts)
```

---

## 2) Get the data (HPO only)

Download the **HPOA annotation file** and place it here:

```
data_raw/hpo/phenotype.hpoa
```

Optional: if you have `hp.json` with HPO labels, place it here too:

```
data_raw/hpo/hp.json
```

> Don’t worry if `hp.json` is missing or has no labels — the scripts **fall back** to using the HPO ID (e.g., `HP:0001250`) as the label so nothing breaks.

---

## 3) Environment

```bash
# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# macOS/Linux
python3 -m venv .venv
source .venv/bin/activate

# Common
pip install -U pip wheel
pip install -r requirements.txt
```

---

## 4) Build the HPO tables

Parses `phenotype.hpoa` into three tidy parquet files:

- **condition.parquet**: one row per condition (`condition_id`, `name` if available)
- **feature.parquet**: one row per HPO term (`feature_id`, `label`, `ic` if available)
- **condition_feature.parquet**: links (`condition_id`, `feature_id`, `weight`)

Run:

```powershell
.\.venv\Scripts\python -m src.build_tables
```

You should see a summary like:

```
=== DONE (HPO-only) ===
conditions        : (12724, 2)
features          : (11420, 3)
condition_feature : (271305, 3)
```

> **Note on condition IDs:** they come directly from HPOA `disease_id`
> (strings like `OMIM:...`, `DECIPHER:...`, or other sources). Treat them as opaque IDs.
> Weight (from HPOA) = How often this symptom shows up in this specific disease.
> IC (Information Content) = How informative/rare this symptom is across all diseases.
---

## 5) Make the sparse matrix

This step converts `data_proc/condition_feature.parquet` (condition ↔ HPO with a numeric weight) plus `data_proc/feature.parquet` (HPO info, including ic) into four SciPy CSR matrices and a shared mapping file.

```powershell
.\.venv\Scripts\python -m src.make_matrices
```

Typical printout:

```
Saved:
  data_proc\X_hpo_weight_csr.npz
  data_proc\X_hpo_weight_norm_csr.npz
  data_proc\X_hpo_weightic_csr.npz
  data_proc\X_hpo_weightic_norm_csr.npz
  data_proc\mappings.json

weight:          shape=(4314, 8614), nnz=114841
weight (norm):   shape=(4314, 8614), nnz=114841
weight×IC:       shape=(4314, 8614), nnz=114841
weight×IC (norm):shape=(4314, 8614), nnz=114841

```

All files are written to data_proc/:

1. `X_hpo_weight_csr.npz`

- Shape: (n_conditions, n_features)
- Values: the frequency-derived weights from condition_feature.parquet (e.g., Rare=0.2, Occasional=0.5, Frequent=0.8, Typical/Very frequent=1.0, numeric % if provided).
- Use when you want to ignore information content.

2. `X_hpo_weight_norm_csr.npz`

- Same as #1, but row-normalized (each condition vector divided by its L2 norm).
- Handy for cosine-style similarity and clustering.

3. `X_hpo_weightic_csr.npz`

- Values: weight × IC(HPO), where
- IC(h) = -log( (df(h)+1) / (N_conditions+1) )
- rarer HPO terms get higher IC; ic is computed earlier in `build_tables.py` and stored in feature.parquet.
- Recommended default for retrieval (emphasizes distinctive phenotypes).

4. `X_hpo_weightic_norm_csr.npz`

- Same as #3, but row-normalized.
- Good starting point for both retrieval and clustering.

5. mappings.json (shared by all four matrices)
- cond_ids: list of condition_id in the row order of the matrices
- feat_ids: list of feature_id (HPO IDs) in the column order

- meta: { "n_rows": ..., "n_cols": ..., "nnz": ... } for quick sanity checks

> **note:** Without normalization, diseases with lots of phenotypes tend to get bigger raw dot products just because they have more non-zeros. Row-normalizing removes that bias.

---

## 6) Sanity checks

### Nulls & types

```powershell
.\.venv\Scripts\python -m src.inspect_nulls
```

This prints null counts per column, **checks prefixes of IDs** (feature IDs should start with `HP:`),
and reports **missing/empty feature labels**. Missing labels are fine — the code **falls back** to
the HPO ID string.

### Quick data peek

```powershell
.\.venv\Scripts\python -m src.inspect_data
```

Shows a few samples and basic stats; writes small CSVs to `data_proc/diagnostics/` for Excel/Sheets.

---

## 9) Reproduce end‑to‑end

1. **Setup & deps** (see §3).
2. **Download** `phenotype.hpoa` to `data_raw/hpo/` (§2).
3. **Build tables**: `python -m src.build_tables` (§4).
4. **Make matrix**: `python -m src.make_matrices` (§5).
5. **Inspect**: `python -m src.inspect_nulls` and `python -m src.inspect_data` (§6).

---

## 10) Troubleshooting

**Q: “File not found” errors**
Double‑check paths:
```
data_raw/hpo/phenotype.hpoa
data_proc/ (is created by the scripts)
```
Run from the **repo root** so relative paths resolve.

---

## 11) How the data is modeled (HPO‑only)

- **Condition** = a disease identifier string present in HPOA (e.g., `OMIM:...`, `DECIPHER:...`, etc.).
- **Feature** = an HPO term (`HP:...`).
- **Link** = condition ↔︎ HPO (optionally with a weight).

This keeps the graph simple and stable across HPO updates.

---

