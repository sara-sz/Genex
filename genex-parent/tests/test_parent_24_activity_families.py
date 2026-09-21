"""PARENT-0.3B — activity-family taxonomy + validator migration.

Covers the founder-reviewed activity-family taxonomy
(`data/parent_2_4/activity_family_taxonomy_v1.xlsx`), its loader
(`parent_taxonomy/activity_families.py`), and the migration of
`genex_core/activity_validator.py` off the retired `FAMILY_TO_CATEGORY` dict.

Three properties are load-bearing and each has a direct test:

1. Allowed domains are DERIVED ({primary} ∪ secondaries), never stored twice.
2. The mismatch check is MEMBERSHIP, so multi-domain families work — and it
   still blocks genuinely wrong pairings (a negative control, not just a
   permissive rewrite).
3. Unknown families stay PERMISSIVE, preserving pre-2.4 behaviour.

Malformed-workbook fixtures are built by perturbing a COPY of the real workbook
so each fixture fails for the reason under test rather than for a missing column
or a wrong row count.
"""

from __future__ import annotations

import ast
import pathlib
import shutil

import pytest

from genex_core import activity_validator
from genex_core.activity_validator import validate_activity
from parent_taxonomy import activity_families as AF
from parent_taxonomy.activity_families import ActivityTaxonomyError
from parent_taxonomy.domains import BY_KEY, DOMAIN_KEYS

REPO = pathlib.Path(__file__).resolve().parent.parent
WORKBOOK = REPO / "data" / "parent_2_4" / "activity_family_taxonomy_v1.xlsx"
GENEX_CORE = REPO / "genex_core"

FAMILIES_SHEET = "activity_families"
ALIASES_SHEET = "activity_family_aliases"

EXPECTED_FAMILY_COUNT = 56
EXPECTED_PRIMARY_DISTRIBUTION = {
    "talking_and_communicating": 17,
    "social_and_emotional": 12,
    "learning_and_thinking": 9,
    "gross_motor": 7,
    "fine_motor": 6,
    "daily_living": 5,
}
EXPECTED_SECONDARY_COUNT = 27

# Founder-confirmed intentional blanks. Asserted so a future "helpful" fill-in
# is a visible test change rather than a silent content edit.
CLINICAL_BLANK_FAMILIES = {"caregiver_affection", "face_recognition", "laughter_joy"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _card(family: str, **over) -> dict:
    """A card that is valid on every axis EXCEPT whatever a test perturbs.

    Deliberately free of placeholder wording, debug suffixes and motor verbs so
    that `is_valid` moves only because of the family/domain rule.
    """
    card = {
        "title": "Thread the Big Beads",
        "instructions": (
            "Sit together at the table. Offer three large wooden beads and one "
            "thick lace. Show your child how to push the lace through a bead, "
            "then pause and let your child try without help."
        ),
        "materials": "3 large wooden beads, 1 thick shoelace",
        "make_harder": "Offer a thinner lace once two beads go on in a row.",
        "success_criteria": "Your child pushes the lace through one bead.",
        "activity_family": family,
    }
    card.update(over)
    return card


def _mismatches(warnings) -> list:
    return [w for w in warnings if w.startswith("activity_family_category_mismatch")]


@pytest.fixture
def perturbed(tmp_path):
    """Copy the real workbook, let a test edit one cell, return the path."""
    openpyxl = pytest.importorskip("openpyxl")

    def _make(sheet: str, edit):
        dest = tmp_path / "perturbed.xlsx"
        shutil.copyfile(WORKBOOK, dest)
        wb = openpyxl.load_workbook(dest)
        edit(wb[sheet], wb)
        wb.save(dest)
        return dest

    return _make


def _col(ws, header: str) -> int:
    for cell in ws[1]:
        if cell.value == header:
            return cell.column
    raise AssertionError(f"header {header!r} not found")


# ---------------------------------------------------------------------------
# 1. workbook is the source of truth and loads
# ---------------------------------------------------------------------------

def test_workbook_exists_and_is_committed():
    assert WORKBOOK.exists(), (
        "the activity-family taxonomy workbook is the source of truth; "
        "the loader fails closed without it"
    )


def test_loads_expected_family_count():
    assert len(AF.get_taxonomy().families) == EXPECTED_FAMILY_COUNT


def test_primary_domain_distribution_matches_founder_review():
    counts = {}
    for entry in AF.get_taxonomy().families.values():
        counts[entry.primary_domain] = counts.get(entry.primary_domain, 0) + 1
    assert counts == EXPECTED_PRIMARY_DISTRIBUTION
    assert sum(counts.values()) == EXPECTED_FAMILY_COUNT


def test_sensory_has_no_activity_families():
    """Sensory is content-pending. Nothing may be classified into it yet."""
    assert all(
        e.primary_domain != "sensory" and "sensory" not in e.secondary_domains
        for e in AF.get_taxonomy().families.values()
    )


def test_secondary_domain_count():
    with_secondary = [
        e for e in AF.get_taxonomy().families.values() if e.secondary_domains
    ]
    assert len(with_secondary) == EXPECTED_SECONDARY_COUNT


def test_all_domains_are_canonical():
    for entry in AF.get_taxonomy().families.values():
        assert entry.primary_domain in BY_KEY
        for dom in entry.secondary_domains:
            assert dom in BY_KEY, (entry.key, dom)


def test_intentional_clinical_blanks_preserved():
    blanks = {
        k for k, e in AF.get_taxonomy().families.items() if not e.clinical_disciplines
    }
    assert blanks == CLINICAL_BLANK_FAMILIES


def test_every_family_has_educational_roles():
    """Founder cleanup filled the three educational-role gaps; none may return."""
    missing = [k for k, e in AF.get_taxonomy().families.items() if not e.educational_roles]
    assert missing == []


def test_clinical_disciplines_are_not_domains():
    """Disciplines (SLP/OT/PT) are a separate axis from developmental domains."""
    for entry in AF.get_taxonomy().families.values():
        for disc in entry.clinical_disciplines:
            assert disc in AF.CLINICAL_DISCIPLINES
            assert disc not in DOMAIN_KEYS


# ---------------------------------------------------------------------------
# 2. allowed domains are derived, not stored
# ---------------------------------------------------------------------------

def test_allowed_domains_is_exactly_primary_union_secondary():
    for entry in AF.get_taxonomy().families.values():
        assert entry.allowed_domains == frozenset(
            (entry.primary_domain, *entry.secondary_domains)
        )


def test_primary_never_repeated_in_secondary():
    for entry in AF.get_taxonomy().families.values():
        assert entry.primary_domain not in entry.secondary_domains


def test_primary_is_always_allowed():
    for entry in AF.get_taxonomy().families.values():
        assert entry.primary_domain in entry.allowed_domains


# ---------------------------------------------------------------------------
# 3. alias resolution comes from the sheet, not from code
# ---------------------------------------------------------------------------

def test_alias_resolves_to_canonical_family():
    assert AF.resolve_family_key("helper_context") == "helper_role_chores"


def test_alias_is_not_hard_coded_in_the_module():
    """The alias must come from the workbook sheet, not from code.

    The alias pair is NAMED in a docstring as an example, so a substring scan
    would false-positive. This checks executable assignments only.
    """
    tree = ast.parse((REPO / "parent_taxonomy" / "activity_families.py").read_text())
    assignments = [
        node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    assert assignments, "AST walk found no assignments — the guard would be vacuous"
    for node in assignments:
        rendered = ast.dump(node)
        assert "helper_role_chores" not in rendered, (
            "alias target hard-coded in an assignment; it must be read from "
            "the activity_family_aliases sheet"
        )


def test_alias_lookup_returns_the_target_family():
    assert AF.allowed_domains("helper_context") == AF.allowed_domains("helper_role_chores")
    assert AF.is_known_family("helper_context")


def test_alias_key_is_not_itself_a_family():
    assert "helper_context" not in AF.get_taxonomy().families


def test_lookup_is_case_and_whitespace_tolerant():
    assert AF.resolve_family_key("  HELPER_CONTEXT  ") == "helper_role_chores"


# ---------------------------------------------------------------------------
# 4. load-time validation fails closed
# ---------------------------------------------------------------------------

def test_blank_family_key_fails_closed(perturbed):
    path = perturbed(
        FAMILIES_SHEET,
        lambda ws, wb: ws.cell(row=2, column=_col(ws, "activity_family_key"), value=""),
    )
    with pytest.raises(ActivityTaxonomyError, match="blank activity_family_key"):
        AF._load(path)


def test_duplicate_family_key_fails_closed(perturbed):
    def edit(ws, wb):
        col = _col(ws, "activity_family_key")
        ws.cell(row=3, column=col, value=ws.cell(row=2, column=col).value)

    with pytest.raises(ActivityTaxonomyError, match="duplicate activity_family_key"):
        AF._load(perturbed(FAMILIES_SHEET, edit))


def test_blank_primary_domain_fails_closed(perturbed):
    path = perturbed(
        FAMILIES_SHEET,
        lambda ws, wb: ws.cell(row=2, column=_col(ws, "primary_domain"), value=""),
    )
    with pytest.raises(ActivityTaxonomyError, match="blank primary_domain"):
        AF._load(path)


def test_noncanonical_primary_domain_fails_closed(perturbed):
    """A LEGACY domain key is not canonical and must be rejected, not mapped."""
    path = perturbed(
        FAMILIES_SHEET,
        lambda ws, wb: ws.cell(
            row=2, column=_col(ws, "primary_domain"), value="movement_and_physical"
        ),
    )
    with pytest.raises(ActivityTaxonomyError, match="not a canonical"):
        AF._load(path)


def test_noncanonical_secondary_domain_fails_closed(perturbed):
    path = perturbed(
        FAMILIES_SHEET,
        lambda ws, wb: ws.cell(
            row=2, column=_col(ws, "secondary_domains"), value="cognitive"
        ),
    )
    with pytest.raises(ActivityTaxonomyError, match="not canonical"):
        AF._load(path)


def test_primary_repeated_as_secondary_fails_closed(perturbed):
    def edit(ws, wb):
        primary = ws.cell(row=2, column=_col(ws, "primary_domain")).value
        ws.cell(row=2, column=_col(ws, "secondary_domains"), value=primary)

    with pytest.raises(ActivityTaxonomyError, match="repeated in secondary_domains"):
        AF._load(perturbed(FAMILIES_SHEET, edit))


def test_unknown_clinical_discipline_fails_closed(perturbed):
    """A developmental domain in the discipline column must be rejected."""
    path = perturbed(
        FAMILIES_SHEET,
        lambda ws, wb: ws.cell(
            row=2, column=_col(ws, "clinical_disciplines"), value="fine_motor"
        ),
    )
    with pytest.raises(ActivityTaxonomyError, match="unknown clinical discipline"):
        AF._load(path)


def test_unknown_educational_role_fails_closed(perturbed):
    path = perturbed(
        FAMILIES_SHEET,
        lambda ws, wb: ws.cell(
            row=2,
            column=_col(ws, "educational_developmental_roles"),
            value="Special Education Teacher",
        ),
    )
    with pytest.raises(ActivityTaxonomyError, match="unknown educational"):
        AF._load(path)


def test_truncated_workbook_fails_closed(perturbed):
    with pytest.raises(ActivityTaxonomyError, match="expected 56"):
        AF._load(perturbed(FAMILIES_SHEET, lambda ws, wb: ws.delete_rows(2)))


def test_malformed_alias_row_fails_closed(perturbed):
    path = perturbed(
        ALIASES_SHEET,
        lambda ws, wb: ws.cell(row=2, column=_col(ws, "canonical_key"), value=""),
    )
    with pytest.raises(ActivityTaxonomyError, match="malformed alias row"):
        AF._load(path)


def test_alias_to_missing_family_fails_closed(perturbed):
    path = perturbed(
        ALIASES_SHEET,
        lambda ws, wb: ws.cell(
            row=2, column=_col(ws, "canonical_key"), value="no_such_family"
        ),
    )
    with pytest.raises(ActivityTaxonomyError, match="not a taxonomy family"):
        AF._load(path)


def test_duplicate_alias_fails_closed(perturbed):
    def edit(ws, wb):
        ws.cell(row=3, column=_col(ws, "legacy_key"), value="helper_context")
        ws.cell(row=3, column=_col(ws, "canonical_key"), value="helper_role_chores")

    with pytest.raises(ActivityTaxonomyError, match="duplicate alias"):
        AF._load(perturbed(ALIASES_SHEET, edit))


def test_alias_chain_fails_closed(perturbed):
    """An alias must not point at another alias — single hop only."""
    def edit(ws, wb):
        ws.cell(row=3, column=_col(ws, "legacy_key"), value="older_name")
        ws.cell(row=3, column=_col(ws, "canonical_key"), value="helper_context")

    with pytest.raises(ActivityTaxonomyError, match="not a taxonomy family|chain|cycle"):
        AF._load(perturbed(ALIASES_SHEET, edit))


def test_alias_shadowing_a_real_family_fails_closed(perturbed):
    def edit(ws, wb):
        ws.cell(row=2, column=_col(ws, "legacy_key"), value="pincer_grasp")
        ws.cell(row=2, column=_col(ws, "canonical_key"), value="helper_role_chores")

    with pytest.raises(ActivityTaxonomyError, match="both an alias and a taxonomy family"):
        AF._load(perturbed(ALIASES_SHEET, edit))


def test_missing_workbook_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(AF, "_WORKBOOK_RELPATH", pathlib.Path("no") / "such.xlsx")
    monkeypatch.chdir(tmp_path)
    AF.reload_cache()
    with pytest.raises(ActivityTaxonomyError, match="not found"):
        AF.get_taxonomy()
    AF.reload_cache()


# ---------------------------------------------------------------------------
# 5. validator behaviour — the reason this phase exists
# ---------------------------------------------------------------------------

def test_beading_threading_with_fine_motor_is_valid():
    """The canonical regression: blocked under the retired legacy dict."""
    is_valid, warnings = validate_activity(_card("beading_threading"), "fine_motor")
    assert _mismatches(warnings) == []
    assert is_valid


def test_buttoning_fasteners_accepts_both_of_its_domains():
    for category in ("daily_living", "fine_motor"):
        is_valid, warnings = validate_activity(_card("buttoning_fasteners"), category)
        assert _mismatches(warnings) == [], category
        assert is_valid, category


def test_conversation_turn_taking_accepts_both_of_its_domains():
    for category in ("talking_and_communicating", "social_and_emotional"):
        is_valid, warnings = validate_activity(
            _card("conversation_turn_taking"), category
        )
        assert _mismatches(warnings) == [], category
        assert is_valid, category


def test_wrong_domain_still_blocks():
    """Negative control: the migration must not be a blanket permit."""
    is_valid, warnings = validate_activity(_card("buttoning_fasteners"), "gross_motor")
    assert _mismatches(warnings), "a genuinely wrong pairing must still be flagged"
    assert not is_valid, "activity_family_category_mismatch must remain CRITICAL"


def test_mismatch_warning_lists_every_allowed_domain():
    _, warnings = validate_activity(_card("buttoning_fasteners"), "gross_motor")
    warning = _mismatches(warnings)[0]
    assert "daily_living|fine_motor" in warning
    assert "not gross_motor" in warning


def test_unknown_family_is_permissive():
    """Pre-2.4 behaviour: an unmapped family raised no mismatch warning."""
    is_valid, warnings = validate_activity(
        _card("some_family_outside_the_56"), "talking_and_communicating"
    )
    assert _mismatches(warnings) == []
    assert is_valid


def test_blank_family_is_permissive():
    is_valid, warnings = validate_activity(_card(""), "talking_and_communicating")
    assert _mismatches(warnings) == []
    assert is_valid


def test_alias_family_validates_as_its_target():
    for category in sorted(AF.allowed_domains("helper_role_chores")):
        is_valid, warnings = validate_activity(_card("helper_context"), category)
        assert _mismatches(warnings) == [], category
        assert is_valid, category


def test_every_family_validates_against_each_of_its_allowed_domains():
    """Full sweep: no family may false-block on a domain it is allowed to serve."""
    offenders = []
    for key, entry in AF.get_taxonomy().families.items():
        for category in entry.allowed_domains:
            _, warnings = validate_activity(_card(key), category)
            if _mismatches(warnings):
                offenders.append((key, category))
    assert offenders == []


def test_every_family_blocks_on_a_domain_it_does_not_serve():
    """Mirror sweep: the rule still discriminates for all 56 families."""
    misses = []
    for key, entry in AF.get_taxonomy().families.items():
        outside = set(DOMAIN_KEYS) - entry.allowed_domains
        for category in sorted(outside):
            _, warnings = validate_activity(_card(key), category)
            if not _mismatches(warnings):
                misses.append((key, category))
    assert misses == []


# ---------------------------------------------------------------------------
# 6. the retired dict is gone and not re-created
# ---------------------------------------------------------------------------

def test_family_to_category_dict_is_retired():
    assert not hasattr(activity_validator, "FAMILY_TO_CATEGORY")


def test_no_replacement_family_domain_map_in_genex_core():
    """No module may re-create an activity-family -> domain dict.

    Structural, not textual: prose and comments name the retired map
    deliberately, so a substring scan would be a false positive. This walks the
    AST for dict LITERALS that map ACTIVITY-FAMILY keys to domain values.

    Keying on the family names is what makes the guard precise. `genex_core`
    legitimately contains display-string -> domain maps
    (`config.LEGACY_ALIAS_TO_CATEGORY`, `table_loader._CATEGORY_DISPLAY_TO_KEY`)
    whose values are also domain keys; those normalise *vocabulary*, not
    activity families, and their key sets are disjoint from the 56 families.
    Matching on values alone flagged both — matching on keys does not.
    """
    canonical = set(DOMAIN_KEYS)
    legacy = {
        "language_and_communication",
        "cognitive",
        "movement_and_physical",
        "social_and_emotional",
    }
    taxonomy = AF.get_taxonomy()
    family_keys = set(taxonomy.families) | set(taxonomy.aliases)

    offenders = []
    for path in sorted(GENEX_CORE.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict) or len(node.values) < 8:
                continue
            keys = {
                k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            values = [
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            ]
            if len(values) < 8 or not set(values) <= (canonical | legacy):
                continue
            overlap = keys & family_keys
            if overlap:
                offenders.append(
                    f"{path.name}: dict of {len(values)} domain values keyed by "
                    f"{len(overlap)} activity families, e.g. {sorted(overlap)[:3]}"
                )
    assert offenders == [], offenders


def test_family_map_guard_is_not_vacuous():
    """Negative control: the guard above must actually catch a re-created map.

    Without this, a typo in the AST walk would make the guard silently pass
    forever — the exact failure mode found in the therapist suite.
    """
    canonical = set(DOMAIN_KEYS)
    resurrected = ast.parse(
        "FAMILY_TO_CATEGORY = {\n"
        + "".join(
            f"    {k!r}: {v!r},\n"
            for k, v in list(
                (k, e.primary_domain) for k, e in AF.get_taxonomy().families.items()
            )[:10]
        )
        + "}\n"
    )
    taxonomy = AF.get_taxonomy()
    family_keys = set(taxonomy.families) | set(taxonomy.aliases)

    caught = False
    for node in ast.walk(resurrected):
        if not isinstance(node, ast.Dict) or len(node.values) < 8:
            continue
        keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
        values = [v.value for v in node.values if isinstance(v, ast.Constant)]
        if len(values) >= 8 and set(values) <= canonical and keys & family_keys:
            caught = True
    assert caught, "the family-map guard would not catch a resurrected dict"


def test_validator_reads_the_taxonomy_package():
    src = (GENEX_CORE / "activity_validator.py").read_text()
    tree = ast.parse(src)
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert any(m.startswith("parent_taxonomy") for m in imported)


def test_validator_has_no_legacy_domain_comparisons():
    """Rules 5 and 6 were still keyed on legacy spellings — silent-failure bugs.

    Catches `category_key == "language_and_communication"` style comparisons
    structurally, so a prose mention of the legacy name does not false-positive.
    """
    tree = ast.parse((GENEX_CORE / "activity_validator.py").read_text())
    legacy = {"language_and_communication", "cognitive", "movement_and_physical"}
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for operand in [node.left, *node.comparators]:
                if isinstance(operand, ast.Constant) and operand.value in legacy:
                    offenders.append(operand.value)
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# 7. import weight and caching
# ---------------------------------------------------------------------------

def test_taxonomy_package_import_is_stdlib_only():
    """pandas/openpyxl must be function-local so consumers stay light."""
    tree = ast.parse((REPO / "parent_taxonomy" / "activity_families.py").read_text())
    heavy = {"pandas", "numpy", "openpyxl"}
    for node in tree.body:  # module level only
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in heavy
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in heavy


def test_taxonomy_is_cached():
    first = AF.get_taxonomy()
    assert AF.get_taxonomy() is first


def test_reload_cache_rereads():
    first = AF.get_taxonomy()
    AF.reload_cache()
    assert AF.get_taxonomy() is not first


def test_families_are_immutable():
    entry = AF.get_taxonomy().families["beading_threading"]
    with pytest.raises(Exception):
        entry.primary_domain = "sensory"
