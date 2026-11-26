"""clinical_review/tools/build_p0b2_ladder_package.py — P0-B2 clinician-review package.

OFFLINE ONLY. Nothing in `pilot_backend` or the serving image imports this.
It turns the EXISTING Gold Standard CDC milestones + bridge steps and the
EXISTING 30 reviewed Pilot cards into the smallest package Hannah must review
before P0-B2 (one-node developmental recalibration) can be implemented.

## WHAT IT READS — ALL FROZEN, NOTHING NEW

    Gold Standard rows    genex_core.milestones.get_cdc_df()   (the loader the
                          Pilot's rung table and goal anchors already use)
    family aliases        parent_taxonomy.activity_families     (same resolution
                          as ParentGoldStandardSource)
    milestone identity    pilot_backend.domain.canonical_rung.compute_rung_ref
    reviewed cards        pilot_runtime/data/activity_bank_talking_v1.json
    unreviewed candidates clinical_review/candidates/*.json + review_ledger.json

## WHAT IT NEVER DOES

* invents a milestone, a bridge step, or any wording — every node's text is a
  workbook cell (a `bridge_step`, or the CDC `milestone` itself);
* infers an ordering, a successor or a card's node from prose — cross-milestone
  links are accepted only on an EXACT normalised text match to exactly one node
  at the stated `previous_anchor_age`; anything else is NEEDS_CLINICIAN_REVIEW;
* assigns a card to a node because its wording "sounds" easier/harder — the
  only evidence used is the workbook's structured per-step `activity_family`,
  and a card is proposed as UNIQUELY mapped only when that evidence names one
  node; otherwise every candidate is listed for Hannah;
* changes `rung1:` — it is recomputed and cited as provenance only.

## NODE IDENTITY (`node1:`)

    sha256("genex-ladder-node-v1" | rung_ref | node_type | step | wording)[:32]

`rung_ref` ties the node to its CDC milestone (and so to domain + month);
`node_type` is `bridge` or `endpoint`; `step` is the workbook
`bridge_step_number` (the literal "endpoint" for an endpoint); `wording` is the
cell text normalised exactly the way `rung1:` normalises milestone text
(Unicode, typography, whitespace, case). Stable across formatting; a reworded
cell is a new node, the same honest reading `rung1:` uses.

## ENDPOINT RULE (founder)

Every CDC milestone gets an explicit endpoint node. When the workbook's final
bridge row IS the milestone text, that row is the endpoint; otherwise the
endpoint is appended, worded with the existing CDC `milestone` cell.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
PARENT_ROOT = ROOT / "genex-parent"
OUT_DIR = ROOT / "clinical_review" / "p0b2"

NODE_SCHEME = "node1:"
NODE_HASH_TAG = "genex-ladder-node-v1"
PACKAGE_VERSION = "p0b2-ladder-review-package-v1"

#: The Pilot's local Talking neighbourhood around the 24m two-word target.
DOMAIN = "talking_and_communicating"
SUBDOMAIN = "expressive_language"
WINDOW_MONTHS = (12, 36)
#: Reference only — the positive child's current clinical anchor (never modified).
REFERENCE_ANCHOR_RUNG = "rung1:feb590cf2788978b383c11062ce67c1b"

STRUCTURED = "STRUCTURED"
TEXT_LINKED = "TEXT_LINKED"
REVIEW = "NEEDS_CLINICIAN_REVIEW"
CAPACITY = 3


def _imports():
    for p in (str(PARENT_ROOT), str(ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from genex_core import milestones  # noqa: WPS433
    from parent_taxonomy import activity_families  # noqa: WPS433
    from pilot_backend.domain.canonical_rung import (  # noqa: WPS433
        compute_rung_ref, normalize_milestone_text)
    return milestones, activity_families, compute_rung_ref, normalize_milestone_text


def _blank(value: Any) -> bool:
    """Empty workbook cell: None, NaN (pandas) or whitespace."""
    if value is None:
        return True
    if isinstance(value, float) and value != value:   # NaN
        return True
    return not str(value).strip()


def _text(value: Any) -> Optional[str]:
    return None if _blank(value) else str(value).strip()


def _cmp(text: str) -> str:
    """Comparison key ONLY (never identity): letters and digits, folded."""
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.sub(r"[^0-9a-z]+", "", folded)


def node_ref(norm, rung_ref: str, node_type: str, step: Optional[int], wording: str) -> str:
    payload = "\x1f".join((NODE_HASH_TAG, rung_ref, node_type,
                           "endpoint" if step is None else str(step), norm(wording)))
    return NODE_SCHEME + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def build_ladder() -> Dict[str, Any]:
    milestones, AF, compute_rung_ref, norm = _imports()
    df = milestones.get_cdc_df()
    rows = [r for r in df.to_dict(orient="records")
            if str(r.get("category_key") or "").strip() == DOMAIN]

    groups: Dict[Tuple[int, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[(int(r["months"]), str(r["milestone"]).strip())].append(r)

    nodes: Dict[str, Dict[str, Any]] = {}
    milestone_nodes: Dict[Tuple[int, str], List[str]] = {}
    inner_link_mismatches = []
    for (months, milestone), rs in sorted(groups.items(), key=lambda kv: (kv[0][0], _cmp(kv[0][1]))):
        rs = sorted(rs, key=lambda r: int(r["bridge_step_number"]))
        steps = [int(r["bridge_step_number"]) for r in rs]
        if steps != list(range(1, len(rs) + 1)):
            raise ValueError(f"non-contiguous bridge steps at {months}m: {steps}")
        subdomain = str(rs[0]["subdomain"]).strip()
        rung_ref = compute_rung_ref(DOMAIN, months, milestone)
        refs: List[str] = []
        for i, r in enumerate(rs):
            wording = str(r["bridge_step"]).strip()
            raw_family = _text(r.get("activity_family")) or ""
            is_endpoint = (i == len(rs) - 1) and _cmp(wording) == _cmp(milestone)
            if not is_endpoint and _cmp(wording) == _cmp(milestone):
                raise ValueError(f"a non-final bridge equals the milestone at {months}m")
            ntype = "endpoint" if is_endpoint else "bridge"
            step = None if is_endpoint else int(r["bridge_step_number"])
            ref = node_ref(norm, rung_ref, ntype, step, wording)
            allowed = AF.allowed_domains(raw_family) if raw_family else None
            nodes[ref] = {
                "node_ref": ref, "node_type": ntype, "domain": DOMAIN, "subdomain": subdomain,
                "cdc_month_anchor": months, "destination_milestone": milestone,
                "wording": wording, "bridge_step_number": int(r["bridge_step_number"]),
                "endpoint_source": "workbook_final_bridge_row" if is_endpoint else None,
                "rung_ref": rung_ref,
                "workbook_activity_family": raw_family or None,
                "canonical_activity_family": AF.resolve_family_key(raw_family) if allowed else None,
                "family_resolves_for_domain": bool(allowed and DOMAIN in allowed),
                "workbook_previous_bridge_step": _text(r.get("previous_bridge_step")),
                "workbook_previous_anchor_age": (None if _blank(r.get("previous_anchor_age"))
                                                 else int(float(r["previous_anchor_age"]))),
            }
            if i > 0:
                prev_word = nodes[refs[-1]]["wording"]
                pbs = nodes[ref]["workbook_previous_bridge_step"]
                if pbs is None or _cmp(pbs) != _cmp(prev_word):
                    inner_link_mismatches.append({"node_ref": ref, "workbook_previous_bridge_step": pbs,
                                                  "structured_previous_wording": prev_word})
            refs.append(ref)
        if nodes[refs[-1]]["node_type"] != "endpoint":
            ref = node_ref(norm, rung_ref, "endpoint", None, milestone)
            nodes[ref] = {
                "node_ref": ref, "node_type": "endpoint", "domain": DOMAIN, "subdomain": subdomain,
                "cdc_month_anchor": months, "destination_milestone": milestone, "wording": milestone,
                "bridge_step_number": None, "endpoint_source": "cdc_milestone_text_appended",
                "rung_ref": rung_ref,
                # An appended endpoint has no row of its own; it inherits NO family.
                "workbook_activity_family": None, "canonical_activity_family": None,
                "family_resolves_for_domain": False,
                "workbook_previous_bridge_step": None, "workbook_previous_anchor_age": None,
            }
            refs.append(ref)
        milestone_nodes[(months, milestone)] = refs

    # ---- edges -----------------------------------------------------------
    edges = []
    for (months, milestone), refs in milestone_nodes.items():
        for a, b in zip(refs, refs[1:]):
            edges.append({"from": a, "to": b, "confidence": STRUCTURED,
                          "basis": "same milestone, bridge_step_number order"
                                   + (" (explicit endpoint appended)"
                                      if nodes[b]["endpoint_source"] == "cdc_milestone_text_appended" else "")})

    def _candidates_at(age: Optional[int], text: Optional[str]) -> List[str]:
        if age is None or not text:
            return []
        return sorted(r for r, n in nodes.items() if n["cdc_month_anchor"] == age
                      and (_cmp(n["wording"]) == _cmp(text)
                           or (n["node_type"] == "endpoint" and _cmp(n["destination_milestone"]) == _cmp(text))))

    first_links = {}
    for (months, milestone), refs in milestone_nodes.items():
        first = nodes[refs[0]]
        text, age = first["workbook_previous_bridge_step"], first["workbook_previous_anchor_age"]
        cands = _candidates_at(age, text)
        if text is None:
            first_links[refs[0]] = {"status": REVIEW, "reason": "no previous link in the workbook (ladder start or gap)",
                                    "alternatives": []}
        elif len(cands) == 1 and nodes[cands[0]]["node_type"] == "endpoint":
            first_links[refs[0]] = {"status": TEXT_LINKED, "predecessor": cands[0],
                                    "basis": f"previous_bridge_step text matches exactly one endpoint at {age}m"}
        elif len(cands) == 1:
            first_links[refs[0]] = {"status": REVIEW, "reason": "previous link names a mid-milestone bridge, not an endpoint",
                                    "alternatives": cands}
        else:
            same_sub = sorted(r for r, n in nodes.items() if n["node_type"] == "endpoint"
                              and n["cdc_month_anchor"] == age and n["subdomain"] == first["subdomain"])
            first_links[refs[0]] = {"status": REVIEW,
                                    "reason": ("previous link text matches no node" if not cands
                                               else "previous link text matches several nodes"),
                                    "alternatives": cands or same_sub}
    succ: Dict[str, List[str]] = defaultdict(list)
    for first_ref, link in first_links.items():
        if link["status"] == TEXT_LINKED:
            edges.append({"from": link["predecessor"], "to": first_ref, "confidence": TEXT_LINKED,
                          "basis": link["basis"]})
            succ[link["predecessor"]].append(first_ref)

    for r in nodes:
        nodes[r]["predecessor_refs"] = sorted(e["from"] for e in edges if e["to"] == r)
        nodes[r]["successor_refs"] = sorted(e["to"] for e in edges if e["from"] == r)
        flags = []
        if len(nodes[r]["successor_refs"]) > 1:
            flags.append("BRANCH: more than one text-linked successor")
        if r in first_links and first_links[r]["status"] == REVIEW:
            flags.append(f"PREDECESSOR UNRESOLVED: {first_links[r]['reason']}")
        if nodes[r]["node_type"] == "endpoint" and not nodes[r]["successor_refs"]:
            flags.append("GAP: no text-linked successor")
        par = [k for k in milestone_nodes if k[0] == nodes[r]["cdc_month_anchor"]
               and nodes[milestone_nodes[k][0]]["subdomain"] == nodes[r]["subdomain"]]
        if len(par) > 1:
            flags.append(f"PARALLEL: {len(par)} milestones share {nodes[r]['cdc_month_anchor']}m in this subdomain")
        nodes[r]["flags"] = flags
        nodes[r]["predecessor_review"] = first_links.get(r)
    return {"nodes": nodes, "edges": edges, "milestones": milestone_nodes,
            "inner_link_mismatches": inner_link_mismatches}


def _bank():
    bank = json.loads((ROOT / "pilot_runtime" / "data" / "activity_bank_talking_v1.json").read_text())
    return bank


def _candidate_content(families: List[str]) -> Dict[str, Dict[str, Any]]:
    ledger = json.loads((ROOT / "clinical_review" / "review_ledger.json").read_text())
    statuses: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    entries = ledger["entries"]
    # The ledger keys entries BY template id (a sidecar keyed by content).
    for e in (entries.values() if isinstance(entries, dict) else entries):
        statuses[e["activity_family_ref"]][e["status"]] += 1
    out = {}
    for fam in families:
        path = ROOT / "clinical_review" / "candidates" / f"{fam}.json"
        if path.exists():
            out[fam] = {"candidate_file": str(path.relative_to(ROOT)),
                        "candidate_cards": len(json.loads(path.read_text())),
                        "ledger_statuses": dict(statuses.get(fam, {}))}
    return out


def _build_package_core() -> Dict[str, Any]:
    L = build_ladder()
    nodes = L["nodes"]
    lo, hi = WINDOW_MONTHS
    hood = sorted((n for n in nodes.values() if n["subdomain"] == SUBDOMAIN
                   and lo <= n["cdc_month_anchor"] <= hi),
                  key=lambda n: (n["cdc_month_anchor"], _cmp(n["destination_milestone"]),
                                 n["bridge_step_number"] or 99))
    hood_refs = {n["node_ref"] for n in hood}
    boundary = sorted({x for n in hood for x in n["predecessor_refs"] + n["successor_refs"]
                       + [a for a in ((n.get("predecessor_review") or {}).get("alternatives") or [])]}
                      - hood_refs)

    bank = _bank()
    prov = bank["provenance"]
    cards = []
    for tid, t in sorted(bank["templates"].items()):
        fam = t["activity_family_ref"]
        candidates = sorted(n["node_ref"] for n in hood if n["canonical_activity_family"] == fam)
        if t["source_tier"] == "family_curated":
            provenance = f"{t['source_pool_ref']} (sha256 pinned in bank: {prov['reviewed_card_files'].get(fam, '?')[:12]}…)"
        else:
            provenance = (f"{t['source_pool_ref']} — Parent frozen curated variant; bucket reuse approved "
                          f"for {fam}: {prov['bucket_reuse_approved'].get(fam)}")
        if not candidates:
            status, basis = "OUT_OF_NEIGHBOURHOOD", "no node in the window carries this card's family"
        elif len(candidates) == 1:
            status, basis = "STRUCTURED_CANDIDATE", "workbook per-step activity_family names exactly one node"
        else:
            status = REVIEW
            basis = (f"workbook per-step activity_family '{fam}' is shared by {len(candidates)} nodes; "
                     "no structured evidence separates them — Hannah chooses (multiple allowed)")
        cards.append({"activity_template_id": tid, "title": t["title"], "activity_family": fam,
                      "theme": t["theme"], "source_tier": t["source_tier"], "reviewed_provenance": provenance,
                      "candidate_node_refs": candidates, "mapping_status": status, "basis": basis})

    by_node: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for c in cards:
        for r in c["candidate_node_refs"]:
            by_node[r].append(c)
    zero_families = sorted({n["canonical_activity_family"] for n in hood
                            if n["canonical_activity_family"] and not by_node.get(n["node_ref"])})
    candidate_content = _candidate_content(zero_families)
    coverage = []
    for n in hood:
        eligible = by_node.get(n["node_ref"], [])
        themes = sorted({c["theme"] for c in eligible})
        count = len(eligible)
        coverage.append({
            "node_ref": n["node_ref"], "approved_mapped_cards": 0,
            "family_eligible_cards": count,
            "family_eligible_titles": sorted(c["title"] for c in eligible),
            "families": sorted({c["activity_family"] for c in eligible}),
            "theme_count": len(themes),
            "if_all_eligible_approved": ("capacity 3 supported" if count >= CAPACITY else
                                         "thinner week only" if count else "ZERO reviewed cards — no automatic move"),
            "same_node_theme_variation": len(themes) >= 2,
            "unreviewed_candidates_for_family": candidate_content.get(n["canonical_activity_family"] or "", None),
            "node_family_unresolved": n["workbook_activity_family"] and not n["family_resolves_for_domain"],
        })
    return {"package_version": PACKAGE_VERSION, "node_scheme": NODE_SCHEME,
            "scope": {"domain": DOMAIN, "subdomain": SUBDOMAIN, "months": list(WINDOW_MONTHS),
                      "reference_anchor_rung": REFERENCE_ANCHOR_RUNG},
            "bank_artifact_digest": bank["artifact_digest"],
            "neighbourhood_nodes": hood,
            "boundary_nodes": [nodes[r] for r in boundary],
            "edges": [e for e in L["edges"] if e["from"] in hood_refs or e["to"] in hood_refs],
            "inner_link_mismatches": [m for m in L["inner_link_mismatches"] if m["node_ref"] in hood_refs],
            "cards": cards, "coverage": coverage}


def build_package() -> Dict[str, Any]:
    pkg = _build_package_core()
    pkg["pilot"] = build_pilot_packet(pkg)
    return pkg


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _short(ref: Optional[str]) -> str:
    return ref[:17] + "…" if ref else "—"


def render_review_sheet(pkg: Dict[str, Any]) -> str:
    nodes = {n["node_ref"]: n for n in pkg["neighbourhood_nodes"] + pkg["boundary_nodes"]}
    cov = {c["node_ref"]: c for c in pkg["coverage"]}

    def label(ref):
        n = nodes.get(ref)
        if not n:
            return _short(ref)
        kind = "ENDPOINT" if n["node_type"] == "endpoint" else f"Bridge {n['bridge_step_number']}"
        off = "" if n["subdomain"] == SUBDOMAIN else f" ({n['subdomain']})"
        return f"[{n['cdc_month_anchor']}m{off} {kind}] {n['wording']}"

    out = io.StringIO()
    w = out.write
    w("# P0-B2 — Hannah review sheet (Talking · expressive language · 12–36m)\n\n")
    w(f"Generated by `clinical_review/tools/build_p0b2_ladder_package.py` ({PACKAGE_VERSION}). "
      "Every wording below is an existing Gold Standard cell; nothing is new clinical content.\n\n")
    w("Mark each row **YES / NO / EDIT**. `NEEDS_CLINICIAN_REVIEW` rows list the exact alternatives.\n\n")
    w("## A. Developmental nodes and ordering\n\n")
    w("| # | Node | Previous node | Next node | Ordering basis | Approve ordering? | Family-eligible cards (by structured family) | Approve mapping? | Notes |\n")
    w("|---|---|---|---|---|---|---|---|---|\n")
    for i, n in enumerate(pkg["neighbourhood_nodes"], 1):
        r = n["node_ref"]
        preds = n["predecessor_refs"]
        prev = "; ".join(label(p) for p in preds) if preds else "—"
        review = n.get("predecessor_review")
        if review and review["status"] == REVIEW:
            alts = "; ".join(label(a) for a in review.get("alternatives") or []) or "none found"
            prev = f"**{REVIEW}** — {review['reason']}. Alternatives: {alts}"
        nxt = "; ".join(label(s) for s in n["successor_refs"]) or "—"
        if len(n["successor_refs"]) > 1:
            nxt = f"**{REVIEW} (BRANCH)** — choose one or none: " + nxt
        elif n["node_type"] == "endpoint" and not n["successor_refs"]:
            nxt = f"**{REVIEW} (GAP)** — no successor in the workbook"
        basis = []
        if any(e["to"] == r and e["confidence"] == STRUCTURED for e in pkg["edges"]):
            basis.append(STRUCTURED)
        if any(e["to"] == r and e["confidence"] == TEXT_LINKED for e in pkg["edges"]):
            basis.append(TEXT_LINKED)
        c = cov[r]
        cards = ", ".join(c["family_eligible_titles"]) or "**none**"
        notes = "; ".join(n["flags"])
        for m in pkg["inner_link_mismatches"]:
            if m["node_ref"] == r:
                notes = (f"workbook previous-step cell says \"{m['workbook_previous_bridge_step']}\" "
                         f"(not the step above) — confirm order; " + notes).strip("; ")
        if n["endpoint_source"] == "cdc_milestone_text_appended":
            notes = ("explicit CDC endpoint (no workbook row); " + notes).strip("; ")
        if c["node_family_unresolved"]:
            notes = (f"workbook family '{n['workbook_activity_family']}' does not resolve in the taxonomy; " + notes).strip("; ")
        w(f"| {i} | {label(r)} | {prev} | {nxt} | {', '.join(basis) or REVIEW} | ☐ | {cards} | ☐ | {notes} |\n")
    w("\n## B. Card → node mapping (existing 30-card bank; only cards in this neighbourhood)\n\n")
    w("No card is placed by its wording. The only evidence is the workbook's per-step `activity_family`.\n\n")
    w("| Card | Family | Theme | Source | Candidate nodes | Status | Approve? |\n|---|---|---|---|---|---|---|\n")
    for c in pkg["cards"]:
        if c["mapping_status"] == "OUT_OF_NEIGHBOURHOOD":
            continue
        w(f"| {c['title']} | {c['activity_family']} | {c['theme']} | {c['source_tier']} | "
          f"{'<br>'.join(label(x) for x in c['candidate_node_refs'])} | {c['mapping_status']} | ☐ |\n")
    # ---- C. coverage -----------------------------------------------------
    w("\n## C. Coverage — developmental node → reviewed cards\n\n")
    w("Capacity 3 is a MAXIMUM. 3+ cards → normal week; 1–2 → thinner week; 0 → no automatic move "
      "(never borrow from another node). Counts assume Hannah approves every family-eligible card "
      "for the node; until she does, **0 cards are approved-mapped anywhere**.\n\n")
    w("| Node | Family-eligible cards | Themes | If approved | Unreviewed candidate content |\n|---|---|---|---|---|\n")
    for n in pkg["neighbourhood_nodes"]:
        c = cov[n["node_ref"]]
        cand = c["unreviewed_candidates_for_family"]
        cand_txt = (f"{cand['candidate_cards']} in `{cand['candidate_file']}` "
                    f"(ledger: {', '.join(f'{k}={v}' for k, v in sorted(cand['ledger_statuses'].items()))})"
                    if cand else "—")
        w(f"| {label(n['node_ref'])} | {c['family_eligible_cards']} | {c['theme_count']} | "
          f"{c['if_all_eligible_approved']} | {cand_txt} |\n")

    # ---- D. envelope -----------------------------------------------------
    by = {(n["cdc_month_anchor"], n["node_type"], n["bridge_step_number"], n["destination_milestone"]): n
          for n in pkg["neighbourhood_nodes"]}
    ref_nodes = [n for n in pkg["neighbourhood_nodes"] if n["rung_ref"] == REFERENCE_ANCHOR_RUNG]
    below = [n for n in pkg["neighbourhood_nodes"]
             if ref_nodes and n["node_ref"] in nodes[ref_nodes[0]["node_ref"]]["predecessor_refs"]]
    w("\n## D. Candidate 28-day clinical-plan envelope (reference: the 24m two-word milestone)\n\n")
    w("Genex may move ONE node at a time inside the envelope; a move that would leave it returns control to Hannah.\n\n")
    w("| Option | minimum_node_ref | starting_node_ref | maximum_node_ref | Content needed first |\n|---|---|---|---|---|\n")
    if ref_nodes:
        lo_n, hi_n = ref_nodes[0], ref_nodes[-1]
        w(f"| A (inside the current milestone) | {label(lo_n['node_ref'])} | **from baseline (section E)** | "
          f"{label(hi_n['node_ref'])} | none — only Hannah's card mapping |\n")
        if below:
            w(f"| B (one milestone below) | {label(below[0]['node_ref'])} | **from baseline (section E)** | "
              f"{label(hi_n['node_ref'])} | reviewed cards for `{below[0]['canonical_activity_family']}` "
              "(0 in the bank today) |\n")
    w("\nAbove the 24m endpoint the workbook BRANCHES (rows above), so no maximum beyond it is proposed "
      "until Hannah resolves that transition.\n")

    # ---- E. baseline → starting node -------------------------------------
    w("\n## E. Baseline → starting node\n\n")
    w("Today's baseline records each MILESTONE as demonstrated / emerging / not_demonstrated / unknown; "
      "it never asks about bridge steps, so it cannot choose a bridge. Smallest addition: ask the "
      "target milestone's EXISTING bridge steps, in order, as observable statements (no age labels):\n\n")
    w("> *Right now, does this describe your child?* — Yes / Sometimes / Not yet / Not sure\n\n")
    for n in ref_nodes:
        if n["node_type"] == "bridge":
            w(f"- \"{n['wording']}\"  ({_short(n['node_ref'])})\n")
    w("\nCandidate deterministic rule (for Hannah to approve): the starting node is the FIRST bridge, in "
      "step order, not answered *Yes*; if every bridge is *Yes*, the milestone endpoint. Any *Not sure* "
      "→ Hannah chooses the starting node.\n")
    return out.getvalue()


# ---------------------------------------------------------------------------
# Focused Pilot packet (founder decision: Option B, five nodes)
# ---------------------------------------------------------------------------

#: The 18m milestone whose ENDPOINT is the envelope's lower bound.
PILOT_LOWER_MILESTONE = (18, "tries to say three or more words besides mama or dada")
DECISION = "NEEDS CLINICIAN DECISION"
CANDIDATE_FAMILY_FOR_18M = "expressive_first_words"


def _candidate_cards() -> List[Dict[str, Any]]:
    """The existing UNREVIEWED candidates for the 18m endpoint's family."""
    sys.path.insert(0, str(ROOT))
    from pilot_backend.domain.activity_template import compute_template_id  # noqa: WPS433
    cards = json.loads((ROOT / "clinical_review" / "candidates"
                        / f"{CANDIDATE_FAMILY_FOR_18M}.json").read_text())
    entries = json.loads((ROOT / "clinical_review" / "review_ledger.json").read_text())["entries"]
    out = []
    for c in cards:
        tid = compute_template_id(CANDIDATE_FAMILY_FOR_18M, c)
        e = entries[tid]                       # every candidate has a ledger row
        out.append({"activity_template_id": tid, "card": c, "ledger_status": e["status"],
                    "reviewer": e["reviewer"], "ledger_note": e["note"],
                    "supersedes": e["supersedes"], "adapted_from": e["adapted_from"],
                    # Rungs the AUTHORING note itself names (read, not inferred).
                    "note_mentions_rungs": sorted({int(m) for m in re.findall(r"\b(\d+)m\b", e["note"])})})
    return out


def build_pilot_packet(pkg: Dict[str, Any]) -> Dict[str, Any]:
    nodes = {n["node_ref"]: n for n in pkg["neighbourhood_nodes"]}
    m_lo, t_lo = PILOT_LOWER_MILESTONE
    lower = [n for n in nodes.values() if n["cdc_month_anchor"] == m_lo and n["node_type"] == "endpoint"
             and _cmp(n["destination_milestone"]) == _cmp(t_lo)]
    ref24 = sorted((n for n in nodes.values() if n["rung_ref"] == REFERENCE_ANCHOR_RUNG),
                   key=lambda n: (n["node_type"] == "endpoint", n["bridge_step_number"] or 0))
    chain = lower + ref24
    if len(lower) != 1 or len(chain) != 5:
        raise ValueError("the five-node Pilot envelope is not what the founder approved")
    edges = {(e["from"], e["to"]): e for e in pkg["edges"]}
    links = []
    for a, b in zip(chain, chain[1:]):
        e = edges.get((a["node_ref"], b["node_ref"]))
        if e is None or e["confidence"] not in (STRUCTURED, TEXT_LINKED):
            raise ValueError("a Pilot envelope transition is not established by the workbook")
        links.append({"from": a["node_ref"], "to": b["node_ref"], "confidence": e["confidence"], "basis": e["basis"]})
    fam_nodes: Dict[str, List[str]] = defaultdict(list)
    for n in chain:
        if n["canonical_activity_family"]:
            fam_nodes[n["canonical_activity_family"]].append(n["node_ref"])

    def proposal(fam):
        allowed = fam_nodes.get(fam, [])
        if len(allowed) == 1:
            return allowed, allowed[0], "the only envelope node whose workbook bridge family matches"
        return allowed, DECISION, f"workbook family is shared by {len(allowed)} envelope nodes; no structured evidence separates them"

    bank_cards = []
    for c in pkg["cards"]:
        if c["activity_family"] in fam_nodes:
            allowed, proposed, basis = proposal(c["activity_family"])
            bank_cards.append({**{k: c[k] for k in ("activity_template_id", "title", "activity_family", "theme",
                                                    "source_tier", "reviewed_provenance")},
                               "allowed_nodes": allowed, "proposed_node": proposed, "basis": basis,
                               "review_status": "reviewed bank card — node assignment pending"})
    bank = _bank()["templates"]
    for c in bank_cards:
        t = bank[c["activity_template_id"]]
        c["instructions"], c["success_criteria"] = t["instructions"], t["success_criteria"]
    bank_cards.sort(key=lambda c: (c["activity_family"], _cmp(c["theme"]), _cmp(c["title"])))
    cands = []
    for c in _candidate_cards():
        allowed, proposed, basis = proposal(CANDIDATE_FAMILY_FOR_18M)
        cands.append({**c, "allowed_nodes": allowed, "proposed_node": proposed, "basis": basis})
    coverage = []
    for n in chain:
        r = n["node_ref"]
        reviewed = [c for c in bank_cards if r in c["allowed_nodes"]]
        unreviewed = [c for c in cands if r in c["allowed_nodes"]]
        coverage.append({"node_ref": r, "reviewed_cards_eligible": len(reviewed),
                         "unreviewed_candidates_eligible": len(unreviewed),
                         "themes_eligible": len({c["theme"] for c in reviewed} | {c["card"]["theme"] for c in unreviewed}),
                         "approved_now": 0})
    return {"envelope": {"minimum_node_ref": chain[0]["node_ref"], "maximum_node_ref": chain[-1]["node_ref"],
                         "starting_node_ref": "FROM BASELINE (section D) OR CLINICIAN"},
            "chain": chain, "links": links, "bank_cards": bank_cards, "candidates_18m": cands,
            "coverage": coverage}


_STEP_NAMES = ["Step 1", "Step 2", "Step 3", "Step 4", "Step 5"]


def _step_label(pilot, ref):
    i = [n["node_ref"] for n in pilot["chain"]].index(ref)
    n = pilot["chain"][i]
    where = ("earlier milestone (CDC 18 months)" if i == 0 else
             "the 24-month milestone itself" if n["node_type"] == "endpoint" else
             f"towards the 24-month milestone — bridge {n['bridge_step_number']}")
    return f"{_STEP_NAMES[i]} — {where}"


def render_hannah_packet(pkg: Dict[str, Any]) -> str:
    P = pkg["pilot"]
    out = io.StringIO(); w = out.write
    w("# Genex Pilot — developmental steps for review\n\n")
    w("Hannah, this is the small set of decisions Genex needs from you before it adapts activities "
      "between developmental steps. Everything here reuses the existing CDC milestones and Genex bridge "
      "milestones — nothing is a new milestone. Please mark each item **Approve / Edit / Reject** "
      "(or choose a step where asked).\n\n")
    w("How it works for a family: Genex picks activities for the child's current step. If an activity is "
      "rated *too hard* on two different practice days, the next week moves **one step down**; *too easy* "
      "on two different days moves **one step up**. A single rating never moves the child — the card's "
      "\"make it easier / make it harder\" tips cover the moment. Families never see step names.\n\n")
    w("## A. The five steps (skill: putting words together)\n\n")
    w("| Step | What the parent sees | Source | Order correct? |\n|---|---|---|---|\n")
    for i, n in enumerate(P["chain"]):
        src = ("CDC milestone" if n["node_type"] == "endpoint" else "Genex bridge milestone")
        w(f"| **{_step_label(P, n['node_ref'])}** | {n['wording']} | {src} | ☐ Approve ☐ Edit ☐ Reject |\n")
    w("\nThe step above Step 5 is deliberately not included yet: the existing milestones branch there "
      "(e.g. describing actions in pictures vs. longer phrases), so that choice stays with you.\n\n")

    w("## B. Which activities belong to which step\n\n")
    w("These are existing reviewed Genex activities. Genex can tell which *pair* of steps each one belongs "
      "to (from the milestone data), but **not which single step** — that is your call. Tick every step "
      "the activity fits; ticking two is allowed but please only when you mean it. Ideally each step ends "
      "up with **at least 3 activities** and more than one theme, so adjacent steps offer genuinely "
      "different practice.\n\n")
    for fam, steps in (("expressive_vocabulary_growth", "Step 2 or Step 3"), ("two_word_phrases", "Step 4 or Step 5")):
        group = [c for c in P["bank_cards"] if c["activity_family"] == fam]
        if not group:
            continue
        opts = [r for r in group[0]["allowed_nodes"]]
        heads = " | ".join(_step_label(P, r).split(" — ")[0] for r in opts)
        w(f"### Single words → early phrases ({steps}) — {len(group)} activities\n\n" if fam.startswith("expressive")
          else f"### Two-word phrases ({steps}) — {len(group)} activities\n\n")
        w(f"| Activity | Theme | What success looks like | {heads} | Notes |\n|---|---|---|" + "---|" * len(opts) + "---|\n")
        for c in group:
            w(f"| **{c['title']}** — {c['instructions']} | {c['theme']} | {c['success_criteria']} | "
              + " | ".join("☐" for _ in opts) + " |  |\n")
        w("\n")
    w("No existing activity belongs to **Step 1** — the seven new activities in section C are proposed for it.\n\n")

    w("## C. Seven new activities proposed for Step 1 (not yet clinically reviewed)\n\n")
    w("These were drafted for Genex's first-words activity family and revised by the founder before any "
      "clinical review. **None is approved.** They are proposed for Step 1 because it is the only step in "
      "this set that uses the first-words family. Some were originally written with an earlier first-words "
      "milestone in mind — noted per card.\n\n")
    for i, c in enumerate(P["candidates_18m"], 1):
        k = c["card"]
        w(f"### C{i}. {k['title']}  ☐ Approve ☐ Edit ☐ Reject — step: ☐ Step 1 ☐ other: ____\n\n")
        w(f"- **Theme:** {k['theme']}\n- **Materials:** {k['materials']}\n- **Instructions:** {k['instructions']}\n"
          f"- **Success looks like:** {k['success_criteria']}\n- **Make it easier:** {k['make_easier']}\n"
          f"- **Make it harder:** {k['make_harder']}\n- **What to avoid:** {k['what_to_avoid']}\n")
        mentions = [m for m in c["note_mentions_rungs"]]
        w(f"- **Origin:** founder-drafted, no clinician input yet."
          + (f" The drafting note refers to the {', '.join(str(m) + '-month' for m in mentions)} first-words milestone." if mentions else "")
          + "\n")
        if "supervision" in k["what_to_avoid"].lower() or "SUPERVISION" in c["ledger_note"]:
            w("- **Needs your explicit decision:** the bath-time supervision sentence in *What to avoid* "
              "was drafted outside clinical review — approve, replace, or supply standing wording.\n")
        w("\n")

    w("## D. Choosing a child's starting step\n\n")
    w("Today's onboarding tells us the child is working towards the 24-month milestone but not which "
      "bridge. We would add these three questions, in order, using the existing bridge wording:\n\n")
    w("> **Right now, does this describe your child?** — Yes / Sometimes / Not yet / Not sure\n\n")
    for q, n in enumerate([n for n in P["chain"] if n["node_type"] == "bridge"], 1):
        w(f"{q}. \"{n['wording']}\"  *(→ {_step_label(P, n['node_ref']).split(' — ')[0]})*\n")
    w("\n**Proposed rule:** start at the first of these that is not clearly happening yet; if all three "
      "are clearly happening, start at Step 5. Please choose how *Sometimes* should count:\n\n")
    w("| Answer | Option 1 (recommended) | Option 2 |\n|---|---|---|\n")
    w("| Yes | clearly happening → look at the next question | same |\n")
    w("| Sometimes | **emerging → start at this step** (practise what is emerging) | counts as happening → look at the next question |\n")
    w("| Not yet | start at this step | same |\n")
    w("| Not sure | you choose the starting step | same |\n\n")
    w("Option 1 matches how Genex's onboarding already treats *emerging* skills (as something to work on). "
      "Option 2 starts one step higher and risks the first week feeling too hard. Also: if answers are "
      "out of order (e.g. *Not yet* on question 1 but *Yes* on question 3), should Genex follow the rule "
      "or ask you to choose? ☐ Follow the rule ☐ Ask me\n\n")
    w("☐ Option 1  ☐ Option 2  ☐ Edit: ______\n\n")

    w("## E. Approval for automatic adjustment\n\n")
    w("Do you approve Genex moving the child **one step at a time**, automatically, only within "
      "**Step 1 ↔ Step 2 ↔ Step 3 ↔ Step 4 ↔ Step 5**, under these rules?\n\n")
    for rule in ("*Too hard* on two different practice days (same activity) → one step down",
                 "*Too easy* on two different practice days (same activity) → one step up",
                 "One rating only → no change of step (in-the-moment tips on the card)",
                 "Child repeatedly didn't want to try / didn't enjoy it → same step, different activity or theme",
                 "*Wasn't ready* → no automatic change; flagged for you",
                 "Signals pointing in opposite directions → no change; flagged for you",
                 "Never move outside Steps 1–5; a step with no approved activities is not entered — flagged for you"):
        w(f"- {rule}\n")
    w("\n☐ Approve  ☐ Approve with changes: ______  ☐ Reject\n")
    return out.getvalue()


def render_technical_appendix(pkg: Dict[str, Any]) -> str:
    P = pkg["pilot"]
    out = io.StringIO(); w = out.write
    w("# P0-B2 Pilot packet — technical appendix (engineering record)\n\n")
    w(f"Generator `clinical_review/tools/build_p0b2_ladder_package.py` ({PACKAGE_VERSION}); bank artifact "
      f"digest `{pkg['bank_artifact_digest']}`. Founder decision: Option B envelope.\n\n")
    w("## Envelope candidate\n\n")
    for k, v in P["envelope"].items():
        w(f"- `{k}`: `{v}`\n")
    w("\n## Chain (node identity and provenance)\n\n| Step | node_ref | type | step# | rung_ref | workbook family → canonical | endpoint source |\n|---|---|---|---|---|---|---|\n")
    for n in P["chain"]:
        w(f"| {_step_label(P, n['node_ref']).split(' — ')[0]} | `{n['node_ref']}` | {n['node_type']} | {n['bridge_step_number'] or '—'} | "
          f"`{n['rung_ref']}` | {n['workbook_activity_family']} → {n['canonical_activity_family']} | {n['endpoint_source'] or '—'} |\n")
    w("\n## Transitions\n\n")
    for l in P["links"]:
        w(f"- `{l['from'][:22]}…` → `{l['to'][:22]}…`: **{l['confidence']}** — {l['basis']}\n")
    w("\n## Card proposals\n\n| activity_template_id | title | family | tier | allowed nodes | proposed |\n|---|---|---|---|---|---|\n")
    for c in P["bank_cards"]:
        w(f"| `{c['activity_template_id']}` | {c['title']} | {c['activity_family']} | {c['source_tier']} | "
          f"{', '.join(_step_label(P, r).split(' — ')[0] for r in c['allowed_nodes'])} | {c['proposed_node'] if c['proposed_node'] == DECISION else _step_label(P, c['proposed_node']).split(' — ')[0]} |\n")
    for c in P["candidates_18m"]:
        w(f"| `{c['activity_template_id']}` | {c['card']['title']} (candidate) | {CANDIDATE_FAMILY_FOR_18M} | "
          f"ledger: {c['ledger_status']}, reviewer: {c['reviewer']} | "
          f"{', '.join(_step_label(P, r).split(' — ')[0] for r in c['allowed_nodes'])} | "
          f"{_step_label(P, c['proposed_node']).split(' — ')[0]} |\n")
    w("\n## Coverage (eligible, before Hannah's split; approved now = 0 everywhere)\n\n| Step | reviewed eligible | unreviewed candidates | themes |\n|---|---|---|---|\n")
    for c in P["coverage"]:
        w(f"| {_step_label(P, c['node_ref']).split(' — ')[0]} | {c['reviewed_cards_eligible']} | {c['unreviewed_candidates_eligible']} | {c['themes_eligible']} |\n")
    return out.getvalue()


def write_all(pkg: Dict[str, Any]) -> Dict[str, str]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = {
        "ladder_talking_candidate.json": json.dumps(
            {k: pkg[k] for k in ("package_version", "node_scheme", "scope", "neighbourhood_nodes",
                                 "boundary_nodes", "edges", "inner_link_mismatches")},
            indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        "activity_mapping_candidate.json": json.dumps(
            {"package_version": pkg["package_version"], "bank_artifact_digest": pkg["bank_artifact_digest"],
             "cards": pkg["cards"]}, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        "coverage_candidate.json": json.dumps(
            {"package_version": pkg["package_version"], "capacity_is_a_maximum": CAPACITY,
             "coverage": pkg["coverage"]}, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        "hannah_review_sheet.md": render_review_sheet(pkg),
        "pilot_review_packet_for_hannah.md": render_hannah_packet(pkg),
        "pilot_technical_appendix.md": render_technical_appendix(pkg),
        "pilot_envelope_candidate.json": json.dumps(
            {"package_version": pkg["package_version"], **pkg["pilot"]},
            indent=1, sort_keys=True, ensure_ascii=False, default=str) + "\n",
    }
    for name, text in files.items():
        (OUT_DIR / name).write_text(text, encoding="utf-8")
    return files


def main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover - CLI
    files = write_all(build_package())
    for name in sorted(files):
        print(f"wrote clinical_review/p0b2/{name}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
