# src/build_tables.py
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DR, DP = ROOT / "data_raw", ROOT / "data_proc"
HPO_DIR = DR / "hpo"
DP.mkdir(parents=True, exist_ok=True)

HP_PAT = re.compile(r"^HP:\d+$")
ORPHA_PAT = re.compile(r"^ORPHA:\d+$")
RATIO_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")
PCT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s*$")

FREQ_MAP = {
    "HP:0040286": 1.0,   # Obligate
    "HP:0040280": 0.90,  # Very frequent
    "HP:0040281": 0.60,  # Frequent
    "HP:0040282": 0.17,  # Occasional
    "HP:0040283": 0.03,  # Rare
    "HP:0040284": 0.005, # Very rare
    "HP:0040285": 0.0,   # Excluded
}

def freq_to_weight(x: Optional[str]) -> float:
    if x is None or pd.isna(x):
        return 0.5
    s = str(x).strip()
    if s in FREQ_MAP:
        return float(FREQ_MAP[s])
    m = RATIO_RE.match(s)
    if m:
        num, den = float(m.group(1)), float(m.group(2))
        return max(0.0, min(1.0, num / den)) if den > 0 else 0.5
    m = PCT_RE.match(s)
    if m:
        return max(0.0, min(1.0, float(m.group(1)) / 100.0))
    return 0.5

def _read_hpoa(path: Path) -> pd.DataFrame:
    rows = []
    header = None
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if header is None and ("hpo_id" in parts or "HPO_ID" in parts or "HPO-ID" in parts):
                header = parts
                continue
            if header is None:
                # no header line: keep positional columns
                rows.append(parts)
            else:
                rec = {header[i]: (parts[i] if i < len(parts) else "") for i in range(len(header))}
                rows.append(rec)
    if not rows:
        raise RuntimeError(f"HPOA appears empty: {path}")
    if header is None:
        # fabricate names col0..coln for fallback
        maxlen = max(len(r) for r in rows)
        header = [f"col{i}" for i in range(maxlen)]
        rows = [{header[i]: (r[i] if i < len(r) else "") for i in range(maxlen)} for r in rows]
    return pd.DataFrame(rows)

def _pick_hpo_col(df: pd.DataFrame) -> str:
    best_col, best_frac = None, -1.0
    for c in df.columns:
        s = df[c].astype(str)
        frac = s.str.match(HP_PAT).mean()
        if frac > best_frac:
            best_frac, best_col = frac, c
    if best_col is None or best_frac < 0.05:
        # try known names
        for c in ["hpo_id", "HPO_ID", "HPO-ID"]:
            if c in df.columns:
                return c
        raise RuntimeError("Could not detect HPO ID column (HP:####).")
    return best_col

def _pick_orpha_id_col(df: pd.DataFrame) -> Optional[str]:
    best_col, best_frac = None, -1.0
    for c in df.columns:
        frac = df[c].astype(str).str.match(ORPHA_PAT).mean()
        if frac > best_frac:
            best_frac, best_col = frac, c
    return best_col if best_col and best_frac >= 0.02 else None

def _pick_db_and_numeric_cols(df: pd.DataFrame) -> Tuple[Optional[str], Optional[str]]:
    # column with many 'ORPHA' tokens
    db_best, db_frac = None, -1.0
    for c in df.columns:
        frac = df[c].astype(str).str.upper().eq("ORPHA").mean()
        if frac > db_frac:
            db_frac, db_best = frac, c
    # numeric id column
    id_best, id_frac = None, -1.0
    for c in df.columns:
        frac = df[c].astype(str).str.fullmatch(r"\d+").mean()
        if frac > id_frac:
            id_frac, id_best = frac, c
    if db_best and db_frac >= 0.02 and id_best and id_frac >= 0.02:
        return db_best, id_best
    return None, None

def _pick_name_col(df: pd.DataFrame) -> Optional[str]:
    # prefer disease_name-ish, else any '*name*'
    for c in df.columns:
        lc = c.lower()
        if "disease" in lc and "name" in lc:
            return c
    for c in df.columns:
        if "name" in c.lower():
            return c
    return None

def load_hpo_labels(hp_json: Path) -> Dict[str, str]:
    with hp_json.open("r", encoding="utf-8") as f:
        data = json.load(f)
    labels = {}
    for g in (data.get("graphs") or []):
        for n in (g.get("nodes") or []):
            nid = n.get("id")
            if isinstance(nid, str) and nid.startswith("HP:"):
                lbl = n.get("lbl") or n.get("label")
                if isinstance(lbl, str) and lbl.strip():
                    labels[nid] = lbl.strip()
    return labels

def parse_hpoa(path: Path) -> pd.DataFrame:
    raw = _read_hpoa(path)

    # detect columns
    hpo_col = _pick_hpo_col(raw)
    orpha_id_col = _pick_orpha_id_col(raw)

    if orpha_id_col is None:
        db_col, num_col = _pick_db_and_numeric_cols(raw)
        if not db_col or not num_col:
            # ultimate fallback: row-wise scan for ORPHA and digits
            cond_id = []
            for _, r in raw.iterrows():
                vals = [str(v) for v in r.values]
                # ORPHA:#### anywhere?
                found = next((v for v in vals if ORPHA_PAT.match(v or "")), None)
                if found:
                    cond_id.append(found)
                    continue
                # ORPHA + number pair
                if any(v.upper() == "ORPHA" for v in vals):
                    num = next((v for v in vals if v and v.isdigit()), "")
                    cond_id.append(f"ORPHA:{num}" if num else None)
                else:
                    cond_id.append(None)
            raw["condition_id"] = cond_id
        else:
            cond_id = np.where(
                raw[db_col].astype(str).str.upper().eq("ORPHA"),
                "ORPHA:" + raw[num_col].astype(str).str.extract(r"(\d+)")[0].fillna(""),
                None,
            )
            raw["condition_id"] = cond_id
    else:
        raw["condition_id"] = raw[orpha_id_col].astype(str)

    raw = raw[raw["condition_id"].notna() & raw["condition_id"].astype(str).str.startswith("ORPHA:")]

    if raw.empty:
        raise RuntimeError("Could not find any ORPHA rows in phenotype.hpoa after auto-detection.")

    name_col = _pick_name_col(raw)
    freq_col = "frequency" if "frequency" in raw.columns else None
    qual_col = "qualifier" if "qualifier" in raw.columns else None

    out = pd.DataFrame({
        "condition_id": raw["condition_id"].astype(str).str.strip(),
        "name": (raw[name_col].astype(str).str.strip() if name_col else ""),
        "hpo_id": raw[hpo_col].astype(str).str.strip(),
        "frequency": (raw[freq_col] if freq_col else ""),
        "qualifier": (raw[qual_col] if qual_col else "")
    })

    # drop negated annotations
    out = out[out["qualifier"].astype(str).str.upper().ne("NOT")]

    out["weight"] = out["frequency"].map(freq_to_weight).astype(float)
    return out.reset_index(drop=True)

def compute_ic(df_hpoa: pd.DataFrame) -> pd.DataFrame:
    N = df_hpoa["condition_id"].nunique()
    counts = (
        df_hpoa[["condition_id", "hpo_id"]]
        .drop_duplicates()
        .groupby("hpo_id")["condition_id"]
        .nunique()
        .rename("df")
        .reset_index()
    )
    counts["ic"] = (-np.log((counts["df"] + 1.0) / (N + 1.0))).astype(float)
    return counts[["hpo_id", "ic"]]

def build_tables() -> None:
    hp_json = HPO_DIR / "hp.json"
    hpoa = HPO_DIR / "phenotype.hpoa"
    if not hp_json.exists():
        raise FileNotFoundError(f"Missing {hp_json}")
    if not hpoa.exists():
        raise FileNotFoundError(f"Missing {hpoa}")

    print("Parsing phenotype.hpoa ...")
    df_hpoa = parse_hpoa(hpoa)

    print("Loading HPO labels from hp.json ...")
    labels = load_hpo_labels(hp_json)

    # condition.parquet
    df_condition = (
        df_hpoa[["condition_id", "name"]]
        .drop_duplicates()
        .sort_values("condition_id")
        .reset_index(drop=True)
    )
    # placeholders for future Orphadata join
    for col in ("category", "inheritance", "prevalence_band"):
        df_condition[col] = None

    # feature.parquet (label fix + IC)
    df_ic = compute_ic(df_hpoa)
    df_feature = (
        df_hpoa[["hpo_id"]]
        .drop_duplicates()
        .rename(columns={"hpo_id": "feature_id"})
    )
    df_feature["label"] = df_feature["feature_id"].map(labels).fillna(df_feature["feature_id"])
    df_feature = df_feature.merge(
        df_ic.rename(columns={"hpo_id": "feature_id"}), on="feature_id", how="left"
    )
    df_feature["ic"] = df_feature["ic"].fillna(0.0).astype(float)

    # condition_feature.parquet
    df_cf = df_hpoa.rename(columns={"hpo_id": "feature_id"})[
        ["condition_id", "feature_id", "weight"]
    ].dropna()

    DP.mkdir(exist_ok=True, parents=True)
    df_condition.to_parquet(DP / "condition.parquet", index=False)
    df_feature.to_parquet(DP / "feature.parquet", index=False)
    df_cf.to_parquet(DP / "condition_feature.parquet", index=False)

    print("Wrote:",
          DP / "condition.parquet",
          DP / "feature.parquet",
          DP / "condition_feature.parquet")

def main():
    build_tables()

if __name__ == "__main__":
    main()
