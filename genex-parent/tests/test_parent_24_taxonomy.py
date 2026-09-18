"""Parent 2.4 taxonomy foundation + migration tests.

Proves the contract of the new canonical vocabulary, the fail-closed subdomain
map, and the additive Gold Standard migration.

Nothing here touches genex_core, the therapist suite, or any frozen tag. The
authoritative source workbook is read-only in every test and its SHA is asserted
unchanged.

Run:  python -m pytest genex-parent/tests/test_parent_24_taxonomy.py
"""

from __future__ import annotations

import pathlib
import sys

import pytest

# genex-parent/ is the package root for `parent_taxonomy`.
PARENT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PARENT_ROOT) not in sys.path:
    sys.path.insert(0, str(PARENT_ROOT))

from parent_taxonomy import domains as D  # noqa: E402
from parent_taxonomy import migrate as M  # noqa: E402
from parent_taxonomy import subdomain_map as S  # noqa: E402

SOURCE = PARENT_ROOT / "data" / (
    "cdc_milestones_with_bridges_family_cleaned_final_app_ready.xlsx"
)

pytest.importorskip("pandas", reason="pandas required for workbook migration tests")
pytest.importorskip("openpyxl", reason="openpyxl required for workbook migration tests")


# ── canonical taxonomy ──────────────────────────────────────────────────────
def test_exactly_seven_canonical_domains():
    assert len(D.DOMAINS) == 7
    assert len(D.DOMAIN_KEYS) == 7
    assert len(set(D.DOMAIN_KEYS)) == 7, "keys must be unique"


def test_canonical_keys_and_labels_are_exact():
    assert D.DOMAIN_KEYS == (
        "talking_and_communicating",
        "social_and_emotional",
        "learning_and_thinking",
        "fine_motor",
        "gross_motor",
        "daily_living",
        "sensory",
    )
    assert [d.display for d in D.DOMAINS] == [
        "Talking & Communicating",
        "Social & Emotional",
        "Learning & Thinking",
        "Fine Motor",
        "Gross Motor",
        "Daily Living",
        "Sensory",
    ]


def test_ordering_is_stable_and_contiguous():
    orders = [d.order for d in D.DOMAINS]
    assert orders == sorted(orders) == list(range(1, 8))
    assert D.ordered_domains() == D.DOMAINS


def test_movement_and_physical_is_not_a_canonical_domain():
    """The central 2.4 assertion: the collapsed bucket is gone."""
    assert "movement_and_physical" not in D.DOMAIN_KEYS
    assert not D.is_canonical("movement_and_physical")
    assert "Movement & Physical" not in [d.display for d in D.DOMAINS]
    with pytest.raises(D.UnknownDomain):
        D.get("movement_and_physical")


def test_legacy_keys_are_recognised_only_as_legacy():
    for k in D.LEGACY_DOMAIN_KEYS:
        assert D.is_legacy(k)
    assert not D.is_canonical("movement_and_physical")
    assert not D.is_canonical("cognitive")
    assert not D.is_canonical("language_and_communication")
    # social_and_emotional is the one key that is BOTH legacy and canonical.
    assert D.is_canonical("social_and_emotional") and D.is_legacy("social_and_emotional")


def test_unambiguous_legacy_keys_resolve():
    assert D.resolve_legacy_domain("language_and_communication") == "talking_and_communicating"
    assert D.resolve_legacy_domain("cognitive") == "learning_and_thinking"
    assert D.resolve_legacy_domain("social_and_emotional") == "social_and_emotional"


def test_movement_and_physical_resolves_to_ambiguous_not_a_guess():
    """It spanned three domains; collapsing it onto one would fabricate precision."""
    assert D.resolve_legacy_domain("movement_and_physical") is None
    assert D.LEGACY_AMBIGUOUS["movement_and_physical"] == (
        "fine_motor", "gross_motor", "daily_living",
    )


def test_unknown_domain_key_fails_closed():
    with pytest.raises(D.UnknownDomain):
        D.resolve_legacy_domain("not_a_domain")
    with pytest.raises(D.UnknownDomain):
        D.get("not_a_domain")


def test_canonical_keys_are_idempotent_through_legacy_resolution():
    for k in D.DOMAIN_KEYS:
        assert D.resolve_legacy_domain(k) == k


# ── sensory safety contract ─────────────────────────────────────────────────
def test_sensory_exists_but_has_no_content():
    sensory = D.get("sensory")
    assert sensory.content_status is D.ContentStatus.PENDING
    assert sensory.has_content is False
    assert "sensory" in D.CONTENT_PENDING_KEYS
    assert "sensory" not in D.CONTENT_READY_KEYS


def test_every_other_domain_has_content():
    assert set(D.CONTENT_READY_KEYS) == set(D.DOMAIN_KEYS) - {"sensory"}


def test_no_subdomain_maps_to_sensory():
    """Sensory must not borrow rows from another domain."""
    assert [s for s, d in S.SUBDOMAIN_TO_DOMAIN.items() if d == "sensory"] == []


def test_sensory_never_falls_back_to_emotional_regulation():
    """The pre-2.4 Brain routed sensory concerns into emotional_regulation."""
    assert S.SUBDOMAIN_TO_DOMAIN["emotional_regulation"] == "social_and_emotional"
    assert S.resolve("emotional_regulation") != "sensory"


# ── subdomain map ───────────────────────────────────────────────────────────
def test_map_internal_invariants_hold():
    S.validate_map()


def test_every_target_is_a_canonical_domain():
    for sub, dom in S.SUBDOMAIN_TO_DOMAIN.items():
        assert dom in D.BY_KEY, f"{sub} -> {dom}"


def test_unknown_subdomain_fails_closed():
    with pytest.raises(S.SubdomainMappingError):
        S.resolve("not_a_real_subdomain")


def test_empty_subdomain_fails_closed():
    for bad in ("", "   ", None):
        with pytest.raises(S.SubdomainMappingError):
            S.resolve(bad)  # type: ignore[arg-type]


def test_no_silent_fallback_to_the_old_buckets():
    """The pre-2.4 failure mode: unmapped values quietly becoming motor/cognitive."""
    with pytest.raises(S.SubdomainMappingError):
        S.resolve("some_new_motor_thing")
    with pytest.raises(S.SubdomainMappingError):
        S.resolve("some_new_adaptive_thing")


def test_map_has_no_duplicate_literal_keys():
    """A duplicated literal would be silently collapsed by the dict at parse time."""
    src = (PARENT_ROOT / "parent_taxonomy" / "subdomain_map.py").read_text()
    body = src.split("SUBDOMAIN_TO_DOMAIN: Dict[str, str] = {", 1)[1]
    body = body.split("\n}", 1)[0]
    keys = [
        line.split('"')[1]
        for line in body.splitlines()
        if line.strip().startswith('"') and '":' in line
    ]
    assert len(keys) == len(set(keys)), "duplicate literal subdomain key"
    assert len(keys) == len(S.SUBDOMAIN_TO_DOMAIN)


# ── migration against the real Gold Standard ────────────────────────────────
def test_source_workbook_matches_the_pinned_sha():
    assert SOURCE.is_file(), SOURCE
    assert M.sha256_of(SOURCE) == M.SOURCE_SHA256


def test_every_gold_standard_subdomain_maps_exactly_once():
    import pandas as pd

    df = pd.read_excel(SOURCE, sheet_name=M.MAIN_SHEET, dtype=str)
    present = set(df["subdomain"].dropna().astype(str))
    mapped = S.mapped_subdomains()
    assert present - mapped == set(), f"unmapped subdomains: {present - mapped}"
    assert mapped - present == set(), f"map has values absent from data: {mapped - present}"
    assert len(present) == 24


def test_row_counts_reconcile_to_369_derived_from_source():
    counts = M.domain_counts(SOURCE)
    assert sum(counts.values()) == 369
    assert set(counts) == set(D.DOMAIN_KEYS)


@pytest.mark.parametrize(
    "domain,expected",
    [
        ("talking_and_communicating", 83),
        ("social_and_emotional", 89),
        ("fine_motor", 29),
        ("gross_motor", 49),
        ("daily_living", 35),
        ("sensory", 0),
    ],
)
def test_expected_domain_counts(domain, expected):
    assert M.domain_counts(SOURCE)[domain] == expected


def test_learning_and_thinking_is_cognitive_minus_the_nine_moved_rows():
    """Derived, not hardcoded: old cognitive count minus the rows re-parented."""
    import pandas as pd

    df = pd.read_excel(SOURCE, sheet_name=M.MAIN_SHEET, dtype=str)
    old_cognitive = int((df["category"] == "cognitive").sum())
    moved = int(df["subdomain"].isin(["adaptive_feeding_cues", "safety_awareness"]).sum())
    assert moved == 9
    assert M.domain_counts(SOURCE)["learning_and_thinking"] == old_cognitive - moved


def test_gross_motor_is_the_sum_of_its_two_subdomains():
    import pandas as pd

    df = pd.read_excel(SOURCE, sheet_name=M.MAIN_SHEET, dtype=str)
    a = int((df["subdomain"] == "gross_motor_mobility_and_coordination").sum())
    b = int((df["subdomain"] == "postural_control_and_transitions").sum())
    assert (a, b) == (28, 21)
    assert M.domain_counts(SOURCE)["gross_motor"] == a + b


def test_daily_living_draws_from_two_historical_categories():
    """The cross-category move is the subtle part of the migration."""
    rows = M.build_mapping_table(SOURCE)
    dl = [r for r in rows if r.canonical_domain == "daily_living"]
    assert {r.old_category for r in dl} == {"movement and physical", "cognitive"}
    assert sum(r.row_count for r in dl) == 35


# ── migration output ────────────────────────────────────────────────────────
def test_migration_preserves_everything_protected(tmp_path):
    candidate = tmp_path / "candidate.xlsx"
    before = M.sha256_of(SOURCE)
    report = M.migrate(SOURCE, candidate)

    assert M.sha256_of(SOURCE) == before == M.SOURCE_SHA256, "source must not change"
    assert report.source_rows == report.candidate_rows == 369
    assert report.protected_columns_identical is True
    assert report.total_mapped == 369
    assert M.verify_candidate(SOURCE, candidate) is True


def test_candidate_adds_exactly_one_column(tmp_path):
    import pandas as pd

    candidate = tmp_path / "candidate.xlsx"
    M.migrate(SOURCE, candidate)
    a = pd.read_excel(SOURCE, sheet_name=M.MAIN_SHEET, dtype=str)
    b = pd.read_excel(candidate, sheet_name=M.MAIN_SHEET, dtype=str)
    assert set(b.columns) - set(a.columns) == {M.CANONICAL_COLUMN}
    assert set(a.columns) - set(b.columns) == set()


def test_candidate_keeps_historical_category_for_provenance(tmp_path):
    import pandas as pd

    candidate = tmp_path / "candidate.xlsx"
    M.migrate(SOURCE, candidate)
    b = pd.read_excel(candidate, sheet_name=M.MAIN_SHEET, dtype=str)
    assert "category" in b.columns
    assert set(b["category"]) == {
        "movement and physical", "cognitive",
        "social and emotional", "language and communication",
    }


def test_candidate_all_sheets_preserved(tmp_path):
    import pandas as pd

    candidate = tmp_path / "candidate.xlsx"
    M.migrate(SOURCE, candidate)
    assert pd.ExcelFile(candidate).sheet_names == pd.ExcelFile(SOURCE).sheet_names


def test_migration_is_deterministic_across_runs(tmp_path):
    """Semantic determinism: identical cell content on repeated runs."""
    import pandas as pd

    a, b = tmp_path / "a.xlsx", tmp_path / "b.xlsx"
    M.migrate(SOURCE, a)
    M.migrate(SOURCE, b)
    da = pd.read_excel(a, sheet_name=M.MAIN_SHEET, dtype=str).fillna("")
    db = pd.read_excel(b, sheet_name=M.MAIN_SHEET, dtype=str).fillna("")
    assert da.equals(db)


def test_migration_refuses_to_overwrite_the_source():
    with pytest.raises(ValueError):
        M.migrate(SOURCE, SOURCE)


def test_canonical_column_values_are_all_canonical(tmp_path):
    import pandas as pd

    candidate = tmp_path / "candidate.xlsx"
    M.migrate(SOURCE, candidate)
    b = pd.read_excel(candidate, sheet_name=M.MAIN_SHEET, dtype=str)
    assert set(b[M.CANONICAL_COLUMN]) <= set(D.DOMAIN_KEYS)
    assert "movement_and_physical" not in set(b[M.CANONICAL_COLUMN])
    assert "sensory" not in set(b[M.CANONICAL_COLUMN]), "no row may claim sensory"


# ── scope guards ────────────────────────────────────────────────────────────
def test_this_phase_defines_no_discipline_model():
    """SLP/OT/PT is a later phase — guard against silent scope creep."""
    src = (PARENT_ROOT / "parent_taxonomy" / "domains.py").read_text()
    tree_names = {d.key for d in D.DOMAINS}
    for banned in ("slp", "occupational_therapy", "physical_therapy", "discipline_map"):
        assert banned not in tree_names
    assert not hasattr(D, "DISCIPLINES")
    assert not hasattr(D, "DOMAIN_TO_DISCIPLINE")
    assert "SLP" in src, "the docstring should still record WHY disciplines are absent"


def test_parent_taxonomy_lives_outside_genex_core():
    """Keeping genex_core byte-identical is what preserves therapist CI."""
    assert (PARENT_ROOT / "parent_taxonomy").is_dir()
    assert not (PARENT_ROOT / "genex_core" / "parent_taxonomy").exists()


def test_module_imports_nothing_from_genex_core():
    for mod in ("domains.py", "subdomain_map.py", "migrate.py"):
        src = (PARENT_ROOT / "parent_taxonomy" / mod).read_text()
        assert "import genex_core" not in src
        assert "from genex_core" not in src
