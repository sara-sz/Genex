"""P0-B2 clinician-review package — guards on the OFFLINE generator.

The package is review material, not runtime. These tests pin what makes it
trustworthy for Hannah: deterministic, no invented wording, every CDC endpoint
explicit, `rung1:` untouched, ambiguity flagged rather than guessed, cards
placed only by structured family evidence, and nothing in the serving code
depends on it.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytest.importorskip("pandas")
pytest.importorskip("openpyxl")

from clinical_review.tools import build_p0b2_ladder_package as P  # noqa: E402


@pytest.fixture(scope="module")
def pkg():
    return P.build_package()


@pytest.fixture(scope="module")
def cells():
    milestones, _af, _rr, _norm = P._imports()
    df = milestones.get_cdc_df()
    rows = [r for r in df.to_dict(orient="records") if str(r.get("category_key")) == P.DOMAIN]
    return ({str(r["bridge_step"]).strip() for r in rows},
            {str(r["milestone"]).strip() for r in rows})


def test_the_package_is_deterministic(pkg):
    assert json.dumps(P.build_package(), sort_keys=True, default=str) == json.dumps(pkg, sort_keys=True, default=str)


def test_the_committed_outputs_match_a_fresh_build(pkg, tmp_path, monkeypatch):
    """Drift gate: the files Hannah reviews are exactly what the generator emits."""
    monkeypatch.setattr(P, "OUT_DIR", tmp_path)
    fresh = P.write_all(pkg)
    for name, text in fresh.items():
        committed = (ROOT / "clinical_review" / "p0b2" / name).read_text(encoding="utf-8")
        assert committed == text, f"{name} is stale — rerun the generator"


def test_no_wording_is_invented(pkg, cells):
    bridges, milestones = cells
    for n in pkg["neighbourhood_nodes"] + pkg["boundary_nodes"]:
        if n["node_type"] == "bridge":
            assert n["wording"] in bridges, n
        elif n["endpoint_source"] == "workbook_final_bridge_row":
            assert n["wording"] in bridges and P._cmp(n["wording"]) == P._cmp(n["destination_milestone"])
        else:
            assert n["endpoint_source"] == "cdc_milestone_text_appended"
            assert n["wording"] == n["destination_milestone"] and n["wording"] in milestones


def test_every_milestone_has_exactly_one_explicit_endpoint(pkg):
    by_ms = {}
    for n in pkg["neighbourhood_nodes"]:
        by_ms.setdefault(n["rung_ref"], []).append(n)
    assert by_ms
    for rung_ref, ns in by_ms.items():
        endpoints = [n for n in ns if n["node_type"] == "endpoint"]
        assert len(endpoints) == 1, rung_ref
        # The endpoint is the LAST node of its milestone.
        last = max(ns, key=lambda n: (n["node_type"] == "endpoint", n["bridge_step_number"] or 0))
        assert last["node_type"] == "endpoint"


def test_rung1_is_cited_not_changed(pkg):
    _m, _af, compute_rung_ref, _n = P._imports()
    table = json.loads((ROOT / "pilot_runtime/data/rung_table_talking_v1.json").read_text())["rungs"]
    table_refs = {r["rung_ref"] for r in table.values()}
    for n in pkg["neighbourhood_nodes"]:
        assert n["rung_ref"] == compute_rung_ref(P.DOMAIN, n["cdc_month_anchor"], n["destination_milestone"])
        assert n["rung_ref"].startswith("rung1:") and n["node_ref"].startswith("node1:")
        assert n["rung_ref"] in table_refs, "every neighbourhood milestone is a deployed rung"
    assert any(n["rung_ref"] == P.REFERENCE_ANCHOR_RUNG for n in pkg["neighbourhood_nodes"])


def test_node_refs_are_unique_and_formatting_stable(pkg):
    refs = [n["node_ref"] for n in pkg["neighbourhood_nodes"]]
    assert len(refs) == len(set(refs)) and all(re.fullmatch(r"node1:[0-9a-f]{32}", r) for r in refs)
    _m, _af, _rr, norm = P._imports()
    a = P.node_ref(norm, "rung1:x", "bridge", 2, "Imitates simple two-word phrases.")
    b = P.node_ref(norm, "rung1:x", "bridge", 2, "  imitates   SIMPLE two-word phrases. ")
    c = P.node_ref(norm, "rung1:x", "bridge", 2, "Imitates simple three-word phrases.")
    assert a == b != c


def test_cross_milestone_links_are_exact_or_flagged(pkg):
    nodes = {n["node_ref"]: n for n in pkg["neighbourhood_nodes"] + pkg["boundary_nodes"]}
    for e in pkg["edges"]:
        if e["confidence"] == P.TEXT_LINKED:
            src, dst = nodes[e["from"]], nodes[e["to"]]
            assert src["node_type"] == "endpoint"
            assert src["cdc_month_anchor"] == dst["workbook_previous_anchor_age"]
            assert P._cmp(dst["workbook_previous_bridge_step"]) in (P._cmp(src["wording"]),
                                                                  P._cmp(src["destination_milestone"]))
        else:
            assert e["confidence"] == P.STRUCTURED
            assert nodes[e["from"]]["rung_ref"] == nodes[e["to"]]["rung_ref"], "structured = same milestone"


def test_known_ambiguities_are_flagged_never_guessed(pkg):
    by_label = {(n["cdc_month_anchor"], n["node_type"], n["destination_milestone"][:30]): n
                for n in pkg["neighbourhood_nodes"]}
    e18 = by_label[(18, "endpoint", "tries to say three or more wor")]
    e24 = by_label[(24, "endpoint", "says at least two words togeth")]
    assert len(e18["successor_refs"]) > 1 and any("BRANCH" in f for f in e18["flags"])
    assert len(e24["successor_refs"]) > 1 and any("BRANCH" in f for f in e24["flags"])
    # A link naming a mid-milestone bridge is refused, not followed.
    b18 = by_label[(18, "bridge", "tries to say three or more wor")]
    assert b18["predecessor_review"]["status"] == P.REVIEW and not b18["predecessor_refs"]


def test_cards_are_placed_only_by_structured_family(pkg):
    nodes = {n["node_ref"]: n for n in pkg["neighbourhood_nodes"]}
    assert len(pkg["cards"]) == 30
    for c in pkg["cards"]:
        for r in c["candidate_node_refs"]:
            assert nodes[r]["canonical_activity_family"] == c["activity_family"]
        expected = ("OUT_OF_NEIGHBOURHOOD" if not c["candidate_node_refs"] else
                    "STRUCTURED_CANDIDATE" if len(c["candidate_node_refs"]) == 1 else P.REVIEW)
        assert c["mapping_status"] == expected
    # An appended endpoint has no workbook row, so it inherits no family and no card.
    for n in pkg["neighbourhood_nodes"]:
        if n["endpoint_source"] == "cdc_milestone_text_appended":
            assert n["canonical_activity_family"] is None
            assert not any(n["node_ref"] in c["candidate_node_refs"] for c in pkg["cards"])


def test_coverage_follows_the_capacity_rule(pkg):
    for c in pkg["coverage"]:
        assert c["approved_mapped_cards"] == 0, "nothing is approved until Hannah reviews"
        n = c["family_eligible_cards"]
        assert c["if_all_eligible_approved"] == ("capacity 3 supported" if n >= 3 else
                                                 "thinner week only" if n else
                                                 "ZERO reviewed cards — no automatic move")


def test_the_serving_code_never_imports_the_review_package():
    for base in ("pilot_backend", "pilot_runtime"):
        for path in (ROOT / base).rglob("*.py"):
            if "/tests/" in str(path):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                        else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                assert not any(m.startswith("clinical_review") for m in mods), path


# ---------------------------------------------------------------------------
# Focused Pilot packet (founder decision: Option B, five nodes)
# ---------------------------------------------------------------------------


def test_the_pilot_envelope_is_exactly_the_approved_five_nodes(pkg):
    P_ = pkg["pilot"]
    chain = P_["chain"]
    assert [(n["cdc_month_anchor"], n["node_type"], n["bridge_step_number"]) for n in chain] == [
        (18, "endpoint", 2), (24, "bridge", 1), (24, "bridge", 2), (24, "bridge", 3), (24, "endpoint", 4)]
    assert all(n["rung_ref"] == P.REFERENCE_ANCHOR_RUNG for n in chain[1:])
    assert [l["confidence"] for l in P_["links"]] == [P.TEXT_LINKED, P.STRUCTURED, P.STRUCTURED, P.STRUCTURED]
    assert P_["envelope"]["minimum_node_ref"] == chain[0]["node_ref"]
    assert P_["envelope"]["maximum_node_ref"] == chain[-1]["node_ref"]
    assert not P_["envelope"]["starting_node_ref"].startswith("node1:"), "the start is never pre-decided"


def test_no_existing_card_is_assigned_to_a_single_bridge_without_hannah(pkg):
    P_ = pkg["pilot"]
    chain = [n["node_ref"] for n in P_["chain"]]
    assert len(P_["bank_cards"]) == 18
    for c in P_["bank_cards"]:
        assert c["proposed_node"] == P.DECISION
        expect = chain[1:3] if c["activity_family"] == "expressive_vocabulary_growth" else chain[3:5]
        assert sorted(c["allowed_nodes"]) == sorted(expect), c["title"]


def test_the_seven_18m_candidates_are_carried_unapproved(pkg):
    cands = pkg["pilot"]["candidates_18m"]
    assert len(cands) == 7
    lower = pkg["pilot"]["chain"][0]["node_ref"]
    for c in cands:
        assert c["ledger_status"] == "generated_candidate" and c["reviewer"] is None
        assert c["proposed_node"] == lower and c["allowed_nodes"] == [lower]
        for f in ("title", "materials", "instructions", "success_criteria", "what_to_avoid",
                  "make_easier", "make_harder", "theme"):
            assert c["card"][f].strip()


def test_the_hannah_packet_is_clinical_not_technical(pkg):
    doc = P.render_hannah_packet(pkg)
    for banned in ("node1:", "rung1:", "atpl1:", "sha256", "codec", "hash", "NEEDS_CLINICIAN_REVIEW"):
        assert banned not in doc, banned
    for c in pkg["pilot"]["candidates_18m"]:
        assert c["card"]["title"] in doc and c["card"]["instructions"] in doc
    for n in pkg["pilot"]["chain"]:
        assert n["wording"] in doc
    bridges = [n["wording"] for n in pkg["pilot"]["chain"] if n["node_type"] == "bridge"]
    for i, q in enumerate(bridges, 1):
        assert f'{i}. "{q}"' in doc, "baseline questions use the existing bridge wording verbatim"
    assert "Right now, does this describe your child?" in doc
    assert "Yes / Sometimes / Not yet / Not sure" in doc
    assert not re.search(r"\b\d+\s*-?\s*month(s)?[- ]old\b|developmental age", doc, re.I)
