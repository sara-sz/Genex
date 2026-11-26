"""P0-B2a — the generated ladder artifact: drift gate, bank cross-check, and
the no-mirror discipline for the new modules. Needs pandas (taxonomy job); the
composition proof lives in `test_developmental_ladder_composition.py`."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from pilot_runtime.integration.static_activity_bank import StaticActivityBank
from pilot_runtime.integration.static_developmental_ladder import (
    DEFAULT_ARTIFACT_PATH,
    load_developmental_ladder,
)

ROOT = Path(__file__).resolve().parents[2]
NEW_MODULES = (
    "pilot_backend/domain/developmental_ladder.py",
    "pilot_backend/integration/developmental_recalibration.py",
    "pilot_runtime/integration/developmental_ladder_generator.py",
    "pilot_runtime/integration/static_developmental_ladder.py",
)


def test_the_committed_artifact_matches_a_fresh_build():
    """DRIFT GATE: regenerated from the frozen Gold Standard, rung table and
    reviewed bank, byte for byte."""
    pytest.importorskip("pandas")
    pytest.importorskip("openpyxl")
    from pilot_runtime.integration import developmental_ladder_generator as G

    fresh = G.build_artifact()
    committed = json.loads(DEFAULT_ARTIFACT_PATH.read_text(encoding="utf-8"))
    assert G.canonical_json(fresh) == G.canonical_json(committed)
    assert G.render(fresh) == DEFAULT_ARTIFACT_PATH.read_text(encoding="utf-8")
    assert G.artifact_digest(committed) == committed["artifact_digest"]
    assert committed["provenance"]["unreviewed_candidates_read"] is False
    assert committed["provenance"]["llm_used"] is False


def test_the_artifact_pools_are_exactly_the_reviewed_bank():
    ladder = load_developmental_ladder(activity_bank=StaticActivityBank())
    bank = StaticActivityBank()
    for node in ladder.nodes:
        if node.pool_family_ref:
            assert set(node.pool_template_ids) == {
                t.activity_template_id
                for t in bank.templates_for_families([node.pool_family_ref])}
        else:
            assert node.pool_template_ids == ()


def test_the_new_modules_restate_no_workbook_content():
    """Mirrors the CI no-mirror gate for the P0-B2a modules specifically."""
    import re
    banned = ("expressive_vocabulary_growth", "two_word_phrases",
              "expressive_first_words", "says at least two words",
              "imitates simple", "uses more single words", "mama")
    for rel in NEW_MODULES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not re.search(r"(?:rung1|track1|node1):[0-9a-f]{32}", text), rel
        for needle in banned:
            assert needle not in text.lower(), (rel, needle)


def test_the_serving_modules_never_import_the_generator_or_parent():
    for rel in NEW_MODULES:
        if rel.endswith("_generator.py"):
            continue
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                    else [node.module or ""] if isinstance(node, ast.ImportFrom)
                    else [])
            for m in mods:
                assert not m.startswith(("genex_core", "parent_taxonomy",
                                         "pandas", "clinical_review")), (rel, m)
                assert "generator" not in m, (rel, m)
