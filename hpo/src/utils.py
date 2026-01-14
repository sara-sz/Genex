# -*- coding: utf-8 -*-
"""
Small helpers used across scripts (HPO-only).
"""

from __future__ import annotations
from pathlib import Path
import json
from typing import Dict, Any, Tuple
import pandas as pd

# ---------- Paths ----------
def find_root() -> Path:
    """
    Return project root (directory that contains data_proc/).
    Works whether called from src/ or repo root.
    """
    here = Path.cwd()
    for p in [here, here.parent, here.parent.parent]:
        if (p / "data_proc").exists():
            return p
    # Fallback: walk up from this file if available
    try:
        f = Path(__file__).resolve()
        for p in [f.parent, f.parent.parent, f.parent.parent.parent]:
            if (p / "data_proc").exists():
                return p
    except NameError:
        pass
    raise FileNotFoundError("Could not locate project root (folder containing data_proc/).")

# ---------- small io helpers ----------
def safe_read_parquet(path: Path | str) -> pd.DataFrame | None:
    """Read a parquet file if it exists; otherwise return None."""
    p = Path(path)
    if not p.exists():
        return None
    return pd.read_parquet(p)

# ---------- JSON ----------
def read_json(p: Path) -> Dict[str, Any]:
    return json.loads(Path(p).read_text(encoding="utf-8"))

def write_json(p: Path, obj: Dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")

# ---------- label helpers (HPO only) ----------
def missing_label_count(feat: pd.DataFrame) -> int:
    """
    Count how many HPO features have a missing/empty 'label' string.
    """
    if feat is None or len(feat) == 0:
        return 0
    if "label" not in feat.columns:
        # if there is no label column, treat all rows as missing
        return int(len(feat))

    s = feat["label"].astype("string")
    mask = s.isna() | (s.str.strip().str.len() == 0)
    return int(mask.sum())

# ---------- Label fallbacks ----------
def hpo_label_fallback(label: str | None, feature_id: str) -> str:
    """
    If the HPO label is missing/empty, return the feature_id (e.g., 'HP:0001250').
    """
    if label is None:
        return feature_id
    s = str(label).strip()
    return s if s else feature_id

def add_label_fallback_col(df_feat):
    """
    Adds a 'label_fallback' column to feature DataFrame:
    label_fallback = label if present else feature_id
    """
    if "feature_id" not in df_feat.columns:
        return df_feat
    if "label" not in df_feat.columns:
        df_feat["label"] = None
    df_feat["label_fallback"] = [
        hpo_label_fallback(lbl, fid) for lbl, fid in zip(df_feat["label"], df_feat["feature_id"])
    ]
    return df_feat

# ---------- Mapping helpers ----------
def make_id_index_maps(ids) -> Tuple[Dict[str, int], Dict[int, str]]:
    """
    Build lookup maps for a sequence of IDs.
    Returns (id->idx, idx->id).
    """
    id2idx = {i: n for n, i in enumerate(ids)}
    idx2id = {n: i for i, n in id2idx.items()}
    return id2idx, idx2id


def with_fallback_labels(feat: pd.DataFrame) -> pd.DataFrame:
    """
    Return a copy of `feat` with an added 'label_fallback' column:
    use 'label' when present, otherwise fall back to the 'feature_id'
    (e.g., 'HP:0004321').
    """
    if feat is None or len(feat) == 0:
        return feat

    out = feat.copy()
    if "label" not in out.columns:
        out["label"] = pd.Series([None] * len(out), dtype="object")

    base = out["label"].astype("string")
    mask = base.isna() | (base.str.strip().str.len() == 0)

    out["label_fallback"] = base.copy()
    out.loc[mask, "label_fallback"] = out.loc[mask, "feature_id"].astype(str)
    return out
