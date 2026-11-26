"""pilot_runtime/integration/developmental_ladder_generator.py — P0-B2a.

OFFLINE, build-time only. Generates `pilot_runtime/data/
developmental_ladder_talking_v1.json`: the five-node Pilot developmental
neighbourhood the founder approved (Option B), and nothing wider.

    lower endpoint -> Bridge 1 -> Bridge 2 -> Bridge 3 -> anchor endpoint

## SELECTED BY STRUCTURE, NEVER BY A LITERAL

This module names no milestone, no family and no rung (the CI no-mirror gate
forbids all three outside generated artifacts). Every node is found from the
frozen sources' STRUCTURE:

    anchor milestone  the ONE rung in the deployed rung table whose bound
                      families are exactly the reviewed bank's
                      `required_families` (the Pilot goal's target)
    anchor nodes      that milestone's workbook bridge rows, by
                      `bridge_step_number`; the final row is the endpoint when
                      its wording IS the milestone (else one is appended with
                      the CDC milestone text — the review-package rule)
    lower endpoint    the anchor's FIRST bridge names its predecessor with
                      `previous_bridge_step` + `previous_anchor_age`; it must
                      match EXACTLY ONE endpoint at that age (TEXT_LINKED).
                      Anything else fails the build rather than guessing.

Nothing above the anchor endpoint is emitted: the workbook branches there, and
the founder fixed the envelope at five nodes.

## A NODE'S POOL

A node's reviewed pool is the bank templates of the node's own workbook family
(resolved through the taxonomy aliases), and only when that family is BOTH
served by the reviewed bank AND bound by the anchor (the goal's approved
families — the P0-B1 invariant proof refuses anything else). Otherwise the pool
is EMPTY. Unreviewed candidates are never read here.

## WHICH TRANSITIONS ARE AUTOMATIC

Exactly the adjacent pairs whose two pools are both non-empty and DISJOINT —
i.e. moving would really change what the family practises, and only to
reviewed content. With today's sources that is one pair (Bridge 2 <-> Bridge
3). A later bridge-specific card mapping (a narrower `pool_template_ids` per
node) unlocks Bridge 1 <-> 2 or Bridge 3 <-> endpoint by regenerating this
artifact; no record schema changes.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unicodedata
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pilot_backend.domain.canonical_rung import (
    compute_rung_ref,
    normalize_milestone_text,
)
from pilot_backend.domain.developmental_ladder import (
    LADDER_ARTIFACT_SCHEMA,
    NODE_HASH_TAG,
    NODE_SCHEME,
    enabled_transitions,
    ladder_ref_for,
)

GENERATOR_VERSION = "pilot-developmental-ladder-generator-v1"
ARTIFACT_RELPATH = "pilot_runtime/data/developmental_ladder_talking_v1.json"
RUNG_TABLE_RELPATH = "pilot_runtime/data/rung_table_talking_v1.json"
BANK_RELPATH = "pilot_runtime/data/activity_bank_talking_v1.json"


class LadderBuildError(Exception):
    """The ladder could not be built unambiguously. Build-time only."""

    PHI_SAFE_MESSAGE = True


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def artifact_digest(body: Dict[str, Any]) -> str:
    without = {k: v for k, v in body.items() if k != "artifact_digest"}
    return hashlib.sha256(canonical_json(without).encode("utf-8")).hexdigest()


def render(body: Dict[str, Any]) -> str:
    return json.dumps(body, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # pandas NaN
        return True
    return not str(value).strip()


def _text(value: Any) -> Optional[str]:
    return None if _blank(value) else str(value).strip()


def _cmp(text: str) -> str:
    """Comparison key ONLY: letters and digits, folded."""
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.sub(r"[^0-9a-z]+", "", folded)


def node_ref(rung_ref: str, node_type: str, step: Optional[int],
             wording: str) -> str:
    """Identical to the P0-B2 review package's `node1:` derivation."""
    payload = "\x1f".join((NODE_HASH_TAG, rung_ref, node_type,
                           "endpoint" if step is None else str(step),
                           normalize_milestone_text(wording)))
    return NODE_SCHEME + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _import_parent(root: Path):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from genex_core import milestones  # noqa: WPS433
    from genex_core import table_loader  # noqa: WPS433
    from parent_taxonomy import activity_families  # noqa: WPS433
    return milestones, table_loader, activity_families


def _milestone_nodes(rows: Sequence[Dict[str, Any]], domain: str,
                     months: int, milestone: str, families) -> List[Dict]:
    """The workbook rows of ONE milestone as nodes, endpoint last."""
    rung_ref = compute_rung_ref(domain, months, milestone)
    rs = sorted(rows, key=lambda r: int(r["bridge_step_number"]))
    steps = [int(r["bridge_step_number"]) for r in rs]
    if steps != list(range(1, len(rs) + 1)):
        raise LadderBuildError("non-contiguous bridge steps")
    out: List[Dict[str, Any]] = []
    for i, r in enumerate(rs):
        wording = str(r["bridge_step"]).strip()
        is_endpoint = i == len(rs) - 1 and _cmp(wording) == _cmp(milestone)
        if not is_endpoint and _cmp(wording) == _cmp(milestone):
            raise LadderBuildError("a non-final bridge equals its milestone")
        raw = _text(r.get("activity_family")) or ""
        allowed = families.allowed_domains(raw) if raw else None
        canonical = (families.resolve_family_key(raw)
                     if allowed and domain in allowed else None)
        out.append({
            "node_type": "endpoint" if is_endpoint else "bridge",
            "step_number": int(r["bridge_step_number"]),
            "wording": wording,
            "rung_ref": rung_ref,
            "source_months": months,
            "workbook_family": raw or None,
            "canonical_family": canonical,
            "previous_bridge_step": _text(r.get("previous_bridge_step")),
            "previous_anchor_age": (None if _blank(r.get("previous_anchor_age"))
                                    else int(float(r["previous_anchor_age"]))),
            "endpoint_source": ("workbook_final_bridge_row" if is_endpoint
                                else None),
        })
    if out[-1]["node_type"] != "endpoint":
        out.append({
            "node_type": "endpoint", "step_number": None,
            "wording": milestone, "rung_ref": rung_ref,
            "source_months": months, "workbook_family": None,
            # An appended endpoint has no row and so inherits NO family.
            "canonical_family": None, "previous_bridge_step": None,
            "previous_anchor_age": None,
            "endpoint_source": "cdc_milestone_text_appended",
        })
    return out


def build_artifact(parent_root: Optional[Path] = None,
                   repo_root: Optional[Path] = None) -> Dict[str, Any]:
    repo = Path(repo_root) if repo_root is not None \
        else Path(__file__).resolve().parents[2]
    root = Path(parent_root) if parent_root is not None \
        else repo / "genex-parent"
    if not root.is_dir():
        raise LadderBuildError("the Parent package root is not present")
    milestones, table_loader, families = _import_parent(root)

    table = json.loads((repo / RUNG_TABLE_RELPATH).read_text(encoding="utf-8"))
    bank = json.loads((repo / BANK_RELPATH).read_text(encoding="utf-8"))
    domain = str(bank["domain_key"])
    required = sorted(bank["required_families"])

    # ---- the anchor: the rung binding exactly the bank's required families
    matches = [r for r in table["rungs"].values()
               if r.get("mappable") and sorted(
                   b["family_ref"] for b in r["family_bindings"]) == required]
    if len(matches) != 1:
        raise LadderBuildError("the anchor rung is not unique")
    anchor = matches[0]
    anchor_families = set(required)

    # ---- workbook rows, grouped by milestone ------------------------------
    frame = milestones.get_cdc_df()
    rows = [r for r in frame.to_dict(orient="records")
            if str(r.get("category_key") or "").strip() == domain]
    groups: Dict[Tuple[int, str], List[Dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((int(r["months"]), str(r["milestone"]).strip()),
                          []).append(r)
    by_rung = {compute_rung_ref(domain, m, ms): (m, ms) for (m, ms) in groups}
    if anchor["rung_ref"] not in by_rung:
        raise LadderBuildError("the anchor rung has no workbook rows")
    a_months, a_ms = by_rung[anchor["rung_ref"]]
    upper = _milestone_nodes(groups[(a_months, a_ms)], domain, a_months, a_ms,
                             families)

    # ---- the lower endpoint: TEXT_LINKED, exactly one ---------------------
    first = upper[0]
    age, text = first["previous_anchor_age"], first["previous_bridge_step"]
    if age is None or not text:
        raise LadderBuildError("the anchor's first bridge names no predecessor")
    candidates = []
    for (m, ms), rs in groups.items():
        if m != age:
            continue
        for n in _milestone_nodes(rs, domain, m, ms, families):
            if n["node_type"] == "endpoint" and _cmp(text) in (
                    _cmp(n["wording"]), _cmp(ms)):
                candidates.append(n)
    if len(candidates) != 1:
        raise LadderBuildError("the lower endpoint is not uniquely text-linked")
    chain = [candidates[0]] + upper

    # ---- pools: reviewed bank templates of the node's own family ----------
    served: Dict[str, List[str]] = {}
    for tid, t in bank["templates"].items():
        served.setdefault(t["activity_family_ref"], []).append(tid)
    nodes = []
    for index, n in enumerate(chain):
        fam = n["canonical_family"]
        pool_ok = bool(fam) and fam in served and fam in anchor_families
        nodes.append({
            "index": index,
            "node_ref": node_ref(n["rung_ref"], n["node_type"],
                                 None if n["node_type"] == "endpoint"
                                 else n["step_number"], n["wording"]),
            "node_type": n["node_type"],
            "step_number": n["step_number"],
            "wording": n["wording"],
            "rung_ref": n["rung_ref"],
            "is_anchor_milestone": n["rung_ref"] == anchor["rung_ref"],
            "workbook_family": n["workbook_family"],
            "pool_family_ref": fam if pool_ok else None,
            "pool_template_ids": sorted(served[fam]) if pool_ok else [],
            "endpoint_source": n["endpoint_source"],
        })
    links = [{"from": chain_a["node_ref"], "to": chain_b["node_ref"],
              "confidence": ("TEXT_LINKED" if i == 0 else "STRUCTURED")}
             for i, (chain_a, chain_b) in enumerate(zip(nodes, nodes[1:]))]
    questions = [n["node_ref"] for n in nodes
                 if n["is_anchor_milestone"] and n["node_type"] == "bridge"]
    if len(questions) != 3:
        raise LadderBuildError("the anchor milestone does not have 3 bridges")

    body: Dict[str, Any] = {
        "artifact_schema_version": LADDER_ARTIFACT_SCHEMA,
        "generator_version": GENERATOR_VERSION,
        "domain_key": domain,
        "anchor_rung_ref": anchor["rung_ref"],
        "anchor_family_refs": sorted(anchor_families),
        "nodes": nodes,
        "links": links,
        "baseline_question_node_refs": questions,
        "enabled_transitions": [
            {"from": a, "to": b} for a, b in enabled_transitions(
                [(n["node_ref"], tuple(n["pool_template_ids"]))
                 for n in nodes])],
        "provenance": {
            "bridge_workbook_relpath": str(Path(table_loader._get_path())
                                           .resolve().relative_to(root.resolve())),
            "bridge_workbook_sha256": _sha256_file(table_loader._get_path()),
            "rung_table_digest": table["artifact_digest"],
            "activity_bank_digest": bank["artifact_digest"],
            "unreviewed_candidates_read": False,
            "llm_used": False,
        },
    }
    body["ladder_ref"] = ladder_ref_for(
        [n["node_ref"] for n in nodes],
        [(t["from"], t["to"]) for t in body["enabled_transitions"]])
    body["artifact_digest"] = artifact_digest(body)
    return body


def write_artifact(destination: Path, parent_root: Optional[Path] = None
                   ) -> Dict[str, Any]:
    body = build_artifact(parent_root)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render(body), encoding="utf-8")
    return body


def main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover - CLI
    repo = Path(__file__).resolve().parents[2]
    body = write_artifact(repo / ARTIFACT_RELPATH)
    print(f"wrote {ARTIFACT_RELPATH}")
    print(f"  digest      {body['artifact_digest']}")
    print(f"  ladder_ref  {body['ladder_ref']}")
    print(f"  enabled     {len(body['enabled_transitions'])} pair(s)")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
