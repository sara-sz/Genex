# -*- coding: utf-8 -*-
"""
Enrich condition.parquet with Orphadata/ORDO attributes:
- category        : from Orphadata classification (product3*; tolerant fallback to product6)
- prevalence_band : from product4/product9_prev
- inheritance     : from ORDO (OWL restrictions with 'inherit' property)

Run:
  python -m src.enrich_conditions
"""
from __future__ import annotations
import re
import json
from pathlib import Path
from typing import Dict, Iterable, Tuple, Optional

import pandas as pd
import xml.etree.ElementTree as ET

# --- Optional rdflib (faster + simpler ORDO parsing). We fall back to pure-XML if missing.
try:
    from rdflib import Graph, RDFS, OWL  # type: ignore
    _HAVE_RDFLIB = True
except Exception:
    _HAVE_RDFLIB = False


ROOT = Path(__file__).resolve().parents[1]
DR   = ROOT / "data_raw" / "orpha"
DP   = ROOT / "data_proc"

COND_PQ = DP / "condition.parquet"
PREVIEW = DP / "condition_preview.csv"


def _local(tag: str) -> str:
    """Return XML localname without namespace."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _text(el: Optional[ET.Element]) -> Optional[str]:
    return None if el is None else (el.text or "").strip() or None


def _as_orpha(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    code = code.strip()
    if not code:
        return None
    if code.startswith("ORPHA:"):
        return code
    return f"ORPHA:{code}"


# ---------------------------------------------------------------------
# CATEGORY from Orphadata classification (product3*). Your environment may
# contain one or more versions: en_product3_150/156/181/189/202.xml.
# We scan *all present* and take the first non-empty group name we see.
# If product3 data is not useful, we try a tolerant read of product6.
# ---------------------------------------------------------------------
def _iter_present(paths: Iterable[Path]) -> Iterable[Path]:
    for p in paths:
        if p and p.exists():
            yield p


def parse_categories_orphadata() -> Dict[str, str]:
    """
    Try multiple product3 versions first; fallback to product6 if needed.
    The parser is tolerant: inside each <Disorder> it looks for:
      - <DisorderGroup>/<Name>, or any descendant with localname in {"Group","Classification"}
        and then a child/descendant <Name> or <Label>.
    """
    # common files seen in the wild
    p3_candidates = sorted(DR.glob("en_product3_*.xml"))
    cat: Dict[str, str] = {}

    def parse_one_product3(p: Path, out: Dict[str, str]) -> None:
        cur_orpha: Optional[str] = None
        cur_group: Optional[str] = None
        # stream parse
        it = ET.iterparse(str(p), events=("start", "end"))
        _, root = next(it)
        stack: list[ET.Element] = []
        for ev, el in it:
            if ev == "start":
                stack.append(el)
                continue

            ln = _local(el.tag)

            # capture OrphaCode in this disorder
            if ln == "OrphaCode":
                cur_orpha = _as_orpha(_text(el))

            # try to locate a group/classification name
            if ln in {"DisorderGroup", "Group", "Classification", "ClassificationNode"}:
                # look for a Name/Label child in this element
                nm = (next((c for c in el if _local(c.tag) in {"Name", "Label"}), None))
                cur_group = _text(nm) or _text(el) or cur_group

            # close of a Disorder: write + reset
            if ln == "Disorder":
                if cur_orpha and cur_group:
                    out.setdefault(cur_orpha, cur_group)
                cur_orpha, cur_group = None, None
                el.clear()
                root.clear()

            # pop stack
            if stack:
                stack.pop()

    # Pass 1: product3 files
    for p in _iter_present(p3_candidates):
        try:
            parse_one_product3(p, cat)
        except Exception:
            # tolerate schema quirks; keep going
            pass

    if cat:
        return cat

    # Fallback: try product6 in case your copy contains a grouping label
    p6 = DR / "en_product6.xml"
    if p6.exists():
        try:
            cur_orpha: Optional[str] = None
            cur_group: Optional[str] = None
            it = ET.iterparse(str(p6), events=("end",))
            for _ev, el in it:
                ln = _local(el.tag)
                if ln == "OrphaCode":
                    cur_orpha = _as_orpha(_text(el))
                elif ln in {"DisorderGroup", "Group"}:
                    nm = (next((c for c in el if _local(c.tag) in {"Name", "Label"}), None))
                    cur_group = _text(nm) or _text(el) or cur_group
                elif ln == "Disorder":
                    if cur_orpha and cur_group:
                        cat.setdefault(cur_orpha, cur_group)
                    cur_orpha, cur_group = None, None
                el.clear()
        except Exception:
            pass

    return cat


# ---------------------------------------------------------------------
# PREVALENCE: product4.xml (+ product9_prev.xml). We store the first
# non-empty band we encounter for each disorder for determinism.
# ---------------------------------------------------------------------
def parse_prevalence_band() -> Dict[str, str]:
    p4 = DR / "en_product4.xml"
    p9 = DR / "en_product9_prev.xml"
    bands: Dict[str, str] = {}

    def _scan(p: Path):
        cur_orpha: Optional[str] = None
        cur_band: Optional[str] = None
        it = ET.iterparse(str(p), events=("end",))
        for _ev, el in it:
            ln = _local(el.tag)
            if ln == "OrphaCode":
                cur_orpha = _as_orpha(_text(el))
            elif ln in {"PrevalenceClass", "Prevalence", "Label"}:
                nm = next((c for c in el if _local(c.tag) in {"Name", "Label"}), None)
                cur_band = (_text(nm) or _text(el) or cur_band)
                if cur_band:
                    cur_band = cur_band.strip()
            elif ln in {"PrevalenceList", "Disorder"}:
                if cur_orpha and cur_band:
                    bands.setdefault(cur_orpha, cur_band)
                cur_orpha, cur_band = None, None
            el.clear()

    for p in _iter_present([p4, p9]):
        try:
            _scan(p)
        except Exception:
            # ignore malformed or unexpected versions
            pass

    return bands


# ---------------------------------------------------------------------
# INHERITANCE from ORDO OWL/RDF
# Strategy:
#   A) If rdflib is installed, use it to find:
#       ?d rdfs:subClassOf [ owl:onProperty ?p ; owl:someValuesFrom ?inh ]
#      where localname(?p) contains 'inherit'.
#   B) Otherwise, pure-XML fallback:
#       Two passes:
#         1) Build {URI -> rdfs:label} map for classes
#         2) For each owl:Restriction with an owl:onProperty whose URI contains 'inherit',
#            record disorder (enclosing owl:Class/@rdf:about Orphanet_####) -> label(someValuesFrom).
# ---------------------------------------------------------------------
def parse_inheritance_from_ordo() -> Dict[str, str]:
    owl = DR / "ordo.owl"
    if not owl.exists():
        return {}

    if _HAVE_RDFLIB:
        try:
            g = Graph()
            g.parse(owl.as_posix())

            # identify properties that look like inheritance
            inherit_props = set()
            for (_s, p, _o) in g.triples((None, None, None)):
                p_str = str(p)
                ln = p_str.split("#")[-1] if "#" in p_str else p_str.rsplit("/", 1)[-1]
                if "inherit" in ln.lower():
                    inherit_props.add(p)

            inherit_map: Dict[str, str] = {}
            for d, _p, restr in g.triples((None, RDFS.subClassOf, None)):
                if not any(True for _ in g.triples((restr, OWL.onProperty, None))):
                    continue
                for _r, _onP, prop in g.triples((restr, OWL.onProperty, None)):
                    if prop not in inherit_props:
                        continue
                    for _r2, _svf, inh in g.triples((restr, OWL.someValuesFrom, None)):
                        # d is the disorder class URI (contains Orphanet_xxx)
                        m = re.search(r"Orphanet[_#](\d+)$", str(d))
                        if not m:
                            continue
                        curie = f"ORPHA:{m.group(1)}"
                        # get human label of inheritance class
                        label: Optional[str] = None
                        for _i, _lp, lab in g.triples((inh, RDFS.label, None)):
                            label = str(lab).strip()
                            break
                        label = label or str(inh).rsplit("/", 1)[-1]
                        inherit_map.setdefault(curie, label)
            return inherit_map
        except Exception:
            # fall through to XML fallback if rdflib chokes
            pass

    # --- XML fallback ---
    # Namespaces commonly used in ORDO
    NS = {
        "rdf":  "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "owl":  "http://www.w3.org/2002/07/owl#",
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    }

    # Pass 1: label map for classes
    labels: Dict[str, str] = {}

    for _ev, el in ET.iterparse(str(owl), events=("end",)):
        if _local(el.tag) == "Class":
            about = el.attrib.get(f"{{{NS['rdf']}}}about")
            if about:
                # rdfs:label child
                lab_el = next((c for c in el if _local(c.tag) == "label"), None)
                lab = _text(lab_el)
                if lab:
                    labels[about] = lab
        el.clear()

    # Pass 2: restrictions
    inh_map: Dict[str, str] = {}
    # We'll track the current enclosing class as we walk subtree to bind restrictions to it.
    current_class_about: Optional[str] = None
    stack: list[str] = []

    for ev, el in ET.iterparse(str(owl), events=("start", "end")):
        ln = _local(el.tag)

        if ev == "start":
            stack.append(ln)
            if ln == "Class":
                current_class_about = el.attrib.get(f"{{{NS['rdf']}}}about")
        else:
            # 'end'
            if ln == "Restriction":
                # Find onProperty and someValuesFrom under this Restriction
                onp = next((c for c in el if _local(c.tag) == "onProperty"), None)
                svf = next((c for c in el if _local(c.tag) == "someValuesFrom"), None)
                if onp is not None and svf is not None and current_class_about:
                    prop_uri = onp.attrib.get(f"{{{NS['rdf']}}}resource", "")
                    if "inherit" in prop_uri.lower():
                        inh_uri = svf.attrib.get(f"{{{NS['rdf']}}}resource", "")
                        # map enclosing disorder class -> inheritance label
                        m = re.search(r"Orphanet[_#](\d+)$", current_class_about or "")
                        if m:
                            curie = f"ORPHA:{m.group(1)}"
                            label = labels.get(inh_uri) or inh_uri.rsplit("/", 1)[-1]
                            inh_map.setdefault(curie, label)
            elif ln == "Class":
                current_class_about = None

            el.clear()
            if stack:
                stack.pop()

    return inh_map


# ---------------------------------------------------------------------
def main():
    assert COND_PQ.exists(), f"Missing {COND_PQ}. Run build_tables first."

    DP.mkdir(parents=True, exist_ok=True)

    cond = pd.read_parquet(COND_PQ)

    # Ensure columns exist (object dtype)
    for col in ["category", "prevalence_band", "inheritance"]:
        if col not in cond.columns:
            cond[col] = pd.Series([None] * len(cond), dtype="object")
        else:
            cond[col] = cond[col].astype("object")

    # --- CATEGORY ---
    print("Parsing classification (product3 / fallback product6) ...")
    cat_map = parse_categories_orphadata()
    print(f"  categories parsed: {len(cat_map):,}")

    # --- PREVALENCE ---
    print("Parsing prevalence (product4/product9) ...")
    prev_map = parse_prevalence_band()
    print(f"  prevalence classes parsed: {len(prev_map):,}")

    # --- INHERITANCE ---
    print("Parsing inheritance (ORDO RDF/XML) ...")
    if not _HAVE_RDFLIB:
        print("  rdflib not installed -> using XML fallback (works; slower).")
    inh_map = parse_inheritance_from_ordo()
    print(f"  inheritance edges parsed: {len(inh_map):,}")

    # --- Apply maps (don't overwrite existing non-null) ---
    cond["category"] = cond["category"].where(cond["category"].notna(), cond["condition_id"].map(cat_map))
    cond["prevalence_band"] = cond["prevalence_band"].where(cond["prevalence_band"].notna(),
                                                            cond["condition_id"].map(prev_map))
    cond["inheritance"] = cond["inheritance"].where(cond["inheritance"].notna(),
                                                    cond["condition_id"].map(inh_map))

    # Counts
    counts = {
        "category":        int(cond["category"].notna().sum()),
        "inheritance":     int(cond["inheritance"].notna().sum()),
        "prevalence_band": int(cond["prevalence_band"].notna().sum()),
    }
    print(
        f"Filled (non-null counts) -> category: {counts['category']:,}"
        f" | inheritance: {counts['inheritance']:,}"
        f" | prevalence_band: {counts['prevalence_band']:,}"
    )

    # Save
    cond.to_parquet(COND_PQ, index=False)
    print(f"Wrote enriched table: {COND_PQ}")

    # Preview
    prev = cond.sample(min(200, len(cond)), random_state=0).sort_values("condition_id")
    (DP / "condition_preview.csv").write_text(prev.to_csv(index=False), encoding="utf-8")
    print(f"Saved preview CSV (first 200 rows): {PREVIEW}")

    # Gentle guidance if we still have many nulls
    n_cat_missing = (cond["category"].isna().sum())
    if n_cat_missing == len(cond):
        print(
            "\nNote: No categories found. Your Orphadata product3 files may not include classification.\n"
            "Try downloading one of the classification dumps (e.g. en_product3_202.xml) and place it in data_raw/orpha/.\n"
        )

    if (cond["inheritance"].isna().sum() == len(cond)) and not (DR / "ordo.owl").exists():
        print(
            "\nNote: No inheritance mapped and 'ordo.owl' not found.\n"
            "Download ORDO (RDF/XML) and put it at data_raw/orpha/ordo.owl to enable inheritance parsing.\n"
        )


if __name__ == "__main__":
    main()
