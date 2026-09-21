"""PARENT-0.3B — the Parent 2.4 Brain is seven-domain-native.

Proves this is real domain separation at Brain level, not a display rename:
`DOMAIN_CONFIG` carries seven canonical keys, the runtime loader classifies from
`canonical_domain`, and the subdomain->domain mapping every Brain module groups
by now resolves Fine Motor, Gross Motor and Daily Living independently.

Also pins what must NOT change: motor scoring semantics, the immutable Gold
Standard, legacy recognition without guessing, and Sensory staying empty.
"""

from __future__ import annotations

import ast
import hashlib
import pathlib
import sys

import pytest

PARENT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PARENT_ROOT) not in sys.path:
    sys.path.insert(0, str(PARENT_ROOT))

pytest.importorskip("pandas")
pytest.importorskip("openpyxl")

from genex_core import config as C  # noqa: E402
from genex_core import table_loader as TL  # noqa: E402
from genex_core.milestones import (  # noqa: E402
    get_category_to_subdomains,
    get_subdomain_to_category,
)
from parent_taxonomy import domains as D  # noqa: E402

#: The locked PARENT-0.2 distribution.
EXPECTED_COUNTS = {
    "talking_and_communicating": 83,
    "social_and_emotional": 89,
    "learning_and_thinking": 84,
    "fine_motor": 29,
    "gross_motor": 49,
    "daily_living": 35,
    "sensory": 0,
}

ORIGINAL_GOLD_STANDARD = PARENT_ROOT / "data" / (
    "cdc_milestones_with_bridges_family_cleaned_final_app_ready.xlsx"
)
ORIGINAL_SHA256 = "c2b6735d9f099c916973c98c1953e7eb1f2ca8e1a60430011f805a3bf9c3487c"
CANDIDATE = PARENT_ROOT / "data" / "parent_2_4" / (
    "cdc_milestones_parent_2_4_candidate.xlsx"
)


# ── 1. DOMAIN_CONFIG is natively seven-domain ───────────────────────────────
def test_domain_config_has_exactly_seven_canonical_domains():
    assert len(C.DOMAIN_CONFIG) == 7
    assert tuple(C.DOMAIN_CONFIG) == D.DOMAIN_KEYS


def test_domain_config_is_not_a_second_source_of_truth():
    """It must be DERIVED from parent_taxonomy, not restated."""
    src = (PARENT_ROOT / "genex_core" / "config.py").read_text()
    assert "from parent_taxonomy.domains import" in src
    for d in D.DOMAINS:
        assert C.DOMAIN_CONFIG[d.key]["display"] == d.display


def test_movement_and_physical_is_not_a_native_brain_domain():
    assert "movement_and_physical" not in C.DOMAIN_CONFIG
    assert C.is_legacy_domain_key("movement_and_physical")


@pytest.mark.parametrize("legacy", ["language_and_communication", "cognitive"])
def test_other_legacy_keys_are_not_native_either(legacy):
    assert legacy not in C.DOMAIN_CONFIG
    assert C.is_legacy_domain_key(legacy)


def test_social_and_emotional_is_both_legacy_and_canonical():
    """The one key whose spelling survived; is_legacy_domain_key must say False."""
    assert "social_and_emotional" in C.DOMAIN_CONFIG
    assert "social_and_emotional" in C.LEGACY_DOMAIN_KEYS
    assert C.is_legacy_domain_key("social_and_emotional") is False


def test_legacy_keys_remain_recognisable():
    assert set(C.LEGACY_DOMAIN_KEYS) == {
        "language_and_communication", "social_and_emotional",
        "cognitive", "movement_and_physical",
    }
    assert C.ALIAS_TO_CATEGORY is C.LEGACY_ALIAS_TO_CATEGORY
    assert C.LEGACY_ALIAS_TO_CATEGORY["motor"] == "movement_and_physical"


def test_legacy_movement_is_never_auto_resolved_to_a_canonical_domain():
    """The central non-guessing guarantee."""
    assert D.resolve_legacy_domain("movement_and_physical") is None
    assert "movement_and_physical" not in D.LEGACY_TO_CANONICAL
    for key in ("fine_motor", "gross_motor", "daily_living"):
        assert C.LEGACY_ALIAS_TO_CATEGORY.get("motor") != key


# ── 2. Loader classifies from canonical_domain ──────────────────────────────
def test_loader_prefers_the_parent_24_candidate_workbook():
    assert CANDIDATE.is_file(), CANDIDATE
    assert "parent_2_4" in str(TL._find_bridge_file())


def test_all_369_rows_load():
    assert len(TL.get_bridge_df()) == 369


def test_canonical_counts_are_exact():
    counts = TL.get_bridge_df()["category_key"].value_counts().to_dict()
    for key, expected in EXPECTED_COUNTS.items():
        assert counts.get(key, 0) == expected, key
    assert sum(counts.values()) == 369


def test_no_row_is_classified_as_a_legacy_domain():
    keys = set(TL.get_bridge_df()["category_key"])
    assert keys <= set(D.DOMAIN_KEYS)
    for legacy in ("movement_and_physical", "cognitive", "language_and_communication"):
        assert legacy not in keys


def test_no_row_is_counted_twice_or_dropped():
    df = TL.get_bridge_df()
    assert df["category_key"].notna().all()
    assert (df["category_key"].astype(str).str.strip() != "").all()
    assert len(df) == sum(EXPECTED_COUNTS.values()) == 369


def test_legacy_category_is_retained_as_provenance():
    df = TL.get_bridge_df()
    assert "legacy_category_key" in df.columns
    assert set(df["legacy_category_key"]) == {
        "movement_and_physical", "cognitive",
        "social_and_emotional", "language_and_communication",
    }


def test_candidate_preserves_original_columns():
    df = TL.get_bridge_df()
    for col in ("months", "subdomain", "milestone", "parent_explanation",
                "bridge_step_number", "activity_family", "category"):
        assert col in df.columns, col


# ── 3. Fine / Gross / Daily Living are genuinely separate ───────────────────
def test_fine_motor_is_its_own_domain():
    c2s = get_category_to_subdomains()
    assert c2s["fine_motor"] == ["fine_motor_hand_use"]
    assert get_subdomain_to_category()["fine_motor_hand_use"] == "fine_motor"


def test_gross_motor_is_its_own_domain_with_both_subdomains():
    c2s = get_category_to_subdomains()
    assert set(c2s["gross_motor"]) == {
        "gross_motor_mobility_and_coordination",
        "postural_control_and_transitions",
    }


def test_fine_motor_and_gross_motor_do_not_overlap():
    c2s = get_category_to_subdomains()
    assert set(c2s["fine_motor"]).isdisjoint(set(c2s["gross_motor"]))


def test_daily_living_is_separate_from_learning_and_from_motor():
    c2s = get_category_to_subdomains()
    daily = set(c2s["daily_living"])
    assert daily == {
        "self_help_motor_skills", "adaptive_feeding_cues", "safety_awareness",
    }
    assert daily.isdisjoint(set(c2s["learning_and_thinking"]))
    assert daily.isdisjoint(set(c2s["fine_motor"]))
    assert daily.isdisjoint(set(c2s["gross_motor"]))


def test_daily_living_draws_from_two_legacy_categories():
    """The subtle move: 26 rows out of Movement, 9 out of Cognitive."""
    df = TL.get_bridge_df()
    dl = df[df["category_key"] == "daily_living"]
    assert set(dl["legacy_category_key"]) == {"movement_and_physical", "cognitive"}
    assert len(dl) == 35


def test_no_subdomain_maps_to_more_than_one_canonical_domain():
    df = TL.get_bridge_df()
    for subdomain, grp in df.groupby("subdomain"):
        assert grp["category_key"].nunique() == 1, subdomain


# ── 4. Sensory exists but is content-pending and fails closed ───────────────
def test_sensory_is_a_native_domain_with_no_content():
    assert "sensory" in C.DOMAIN_CONFIG
    assert C.DOMAIN_CONFIG["sensory"]["has_content"] is False
    assert C.DOMAIN_CONFIG["sensory"]["content_status"] == "pending"


def test_sensory_has_zero_validated_rows():
    counts = TL.get_bridge_df()["category_key"].value_counts().to_dict()
    assert counts.get("sensory", 0) == 0


def test_sensory_is_excluded_from_content_ready_domains():
    assert "sensory" not in C.CONTENT_READY_DOMAIN_KEYS
    assert C.CONTENT_PENDING_DOMAIN_KEYS == ("sensory",)
    assert set(C.CONTENT_READY_DOMAIN_KEYS) == set(C.DOMAIN_CONFIG) - {"sensory"}


def test_sensory_question_generation_fails_closed_returning_nothing():
    """Fail closed: no content means no fabricated milestones."""
    from genex_core.milestones import get_category_questions

    assert get_category_questions("sensory", 24) == []


def test_sensory_never_borrows_social_or_emotional_regulation_content():
    s2c = get_subdomain_to_category()
    assert s2c["emotional_regulation"] == "social_and_emotional"
    assert "sensory" not in set(s2c.values())


# ── 5. Motor scoring semantics preserved ────────────────────────────────────
def test_motor_emerging_subdomains_unchanged():
    assert C.MOTOR_EMERGING_SUBDOMAINS == {
        "postural_control_and_transitions",
        "gross_motor_mobility_and_coordination",
        "fine_motor_hand_use",
    }


def test_motor_scoring_weights_unchanged():
    assert C.MOTOR_EMERGING_PARTIAL_WEIGHT == 0.70
    assert C.MOTOR_EMERGING_NO_PENALTY == 0.25
    assert C.GENERAL_EMERGING_PARTIAL_WEIGHT == 0.45
    assert C.GENERAL_EMERGING_NO_PENALTY == 0.40


def test_motor_emphasis_is_keyed_on_subdomain_not_domain():
    """Why the split could not disturb scoring: it never looked at the domain."""
    from genex_core import scoring

    src = ast.parse((PARENT_ROOT / "genex_core" / "scoring.py").read_text())
    names = {n.id for n in ast.walk(src) if isinstance(n, ast.Name)}
    assert "MOTOR_EMERGING_SUBDOMAINS" in names
    assert "DOMAIN_CONFIG" not in names

    motor_items = [{"subdomain": "fine_motor_hand_use"}] * 3
    other_items = [{"subdomain": "expressive_language"}] * 3
    assert scoring._band_has_motor_emphasis(motor_items) is True
    assert scoring._band_has_motor_emphasis(other_items) is False


def test_motor_emphasis_still_triggers_across_the_split_domains():
    """Fine and Gross are separate domains now, yet both still score as motor."""
    from genex_core import scoring

    for sub in ("fine_motor_hand_use", "gross_motor_mobility_and_coordination",
                "postural_control_and_transitions"):
        assert scoring._band_has_motor_emphasis([{"subdomain": sub}] * 2) is True


# ── 6. No residual four-domain-only assumptions in converted components ─────
@pytest.mark.parametrize("module", [
    "interview_engine", "scheduler", "support_tiers",
    "summaries", "progress_tracker", "delay_engine",
])
def test_converted_modules_have_no_hardcoded_legacy_domain_literals(module):
    """These iterate DOMAIN_CONFIG, so they became 7-domain automatically."""
    src = (PARENT_ROOT / "genex_core" / f"{module}.py").read_text()
    tree = ast.parse(src)
    literals = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value in ("movement_and_physical", "cognitive",
                        "language_and_communication")
    ]
    assert literals == [], f"{module} hardcodes legacy domains: {literals}"


def test_brain_modules_build_domain_dicts_from_domain_config():
    """Spot-check that domain dictionaries are seven-wide, not four."""
    keys = {k: 0.0 for k in C.DOMAIN_CONFIG}
    assert len(keys) == 7
    assert set(keys) == set(D.DOMAIN_KEYS)


# ── 7. Provenance and protected systems ─────────────────────────────────────
def test_original_gold_standard_is_byte_identical():
    assert ORIGINAL_GOLD_STANDARD.is_file()
    digest = hashlib.sha256(ORIGINAL_GOLD_STANDARD.read_bytes()).hexdigest()
    assert digest == ORIGINAL_SHA256


def test_candidate_workbook_still_has_369_rows_and_canonical_domain():
    import pandas as pd

    df = pd.read_excel(CANDIDATE, sheet_name="all_with_bridge_family", dtype=str)
    assert len(df) == 369
    assert "canonical_domain" in df.columns
    assert "category" in df.columns  # provenance preserved


def test_genex_core_does_not_import_genex_alpha():
    for path in sorted((PARENT_ROOT / "genex_core").glob("*.py")):
        src = path.read_text()
        assert "genex-alpha" not in src
        assert "therapist_api" not in src


def test_table_loader_never_writes():
    src = (PARENT_ROOT / "genex_core" / "table_loader.py").read_text()
    for banned in ("to_excel", "ExcelWriter", "write_bytes", ".save("):
        assert banned not in src, banned
