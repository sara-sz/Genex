"""The canonical-rung PORT — contract, refusals and dependency purity.

Dependency-pure by design: nothing here imports pandas, the workbook, or any
`genex-parent` module. That is the property under test as much as the contract
is — if `pilot_backend` ever reached across to Parent to answer "which rungs
exist", this file is where it would start failing to import.

The LIVE adapter against the real workbook is tested separately, in
`pilot_runtime/tests/test_parent_gold_standard_adapter.py`, because that one
needs the real Parent content on the path.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from pilot_backend.domain.canonical_rung import (
    ActivityFamilyBinding,
    CanonicalRung,
)
from pilot_backend.integration.gold_standard_source import (
    GoldStandardRungSource,
    GoldStandardSourceError,
    InMemoryGoldStandardRungSource,
    RungNotFoundError,
    RungNotMappableError,
    RungTarget,
    RungTrackUndeclaredError,
    try_rung_for_target,
)

PILOT_ROOT = pathlib.Path(__file__).resolve().parents[1]

TAXONOMY_VERSION = "activity_family_taxonomy_v1"
BASELINE_VERSION = "parent-2.4-functional-baseline-v1"


def _rung(months: int = 30, milestone: str = "says about 50 words",
          families=(("expressive_vocabulary_growth",
                     ("talking_and_communicating",)),),
          domain: str = "talking_and_communicating",
          subdomain: str = "expressive_language") -> CanonicalRung:
    return CanonicalRung.build(
        domain_key=domain,
        source_rung_months=months,
        milestone_text=milestone,
        subdomain=subdomain,
        family_bindings=[ActivityFamilyBinding(family_ref=ref,
                                               allowed_domains=allowed)
                         for ref, allowed in families],
        track_subdomains=("early_vocalization_and_babbling",
                          "expressive_language"),
        track_families=(),
        taxonomy_version=TAXONOMY_VERSION,
        baseline_version=BASELINE_VERSION,
    )


# ---------------------------------------------------------------------------
# the target is identity only
# ---------------------------------------------------------------------------

def test_a_target_is_exactly_the_three_hashed_fields():
    target = RungTarget("talking_and_communicating", 30, "says about 50 words")
    assert target.domain_key == "talking_and_communicating"
    assert target.source_rung_months == 30
    # No child id, no age, no diagnosis, no free text can ride along.
    assert {f for f in vars(target)} == {
        "domain_key", "source_rung_months", "milestone_text"}


@pytest.mark.parametrize("domain,months,text", [
    ("", 30, "says about 50 words"),
    ("   ", 30, "says about 50 words"),
    ("talking_and_communicating", 30, ""),
    ("talking_and_communicating", 30, "   "),
    ("talking_and_communicating", 0, "says about 50 words"),
    ("talking_and_communicating", -6, "says about 50 words"),
])
def test_an_incomplete_target_is_refused(domain, months, text):
    with pytest.raises(GoldStandardSourceError):
        RungTarget(domain, months, text)


def test_months_must_be_an_int_and_a_bool_is_not_one():
    with pytest.raises(GoldStandardSourceError):
        RungTarget("talking_and_communicating", "30", "says about 50 words")
    # True == 1 would otherwise sail through as "1 month".
    with pytest.raises(GoldStandardSourceError):
        RungTarget("talking_and_communicating", True, "says about 50 words")


# ---------------------------------------------------------------------------
# the protocol and its refusals
# ---------------------------------------------------------------------------

def test_the_in_memory_source_satisfies_the_protocol():
    assert isinstance(InMemoryGoldStandardRungSource(), GoldStandardRungSource)


def test_a_known_target_returns_exactly_that_rung():
    rung = _rung()
    source = InMemoryGoldStandardRungSource(rungs=(rung,))
    got = source.rung_for_target(
        RungTarget("talking_and_communicating", 30, "says about 50 words"))
    assert got is rung
    assert got.rung_ref == rung.rung_ref


def test_an_unknown_target_raises_rather_than_returning_a_neighbour():
    """The refusal that keeps provenance honest: a source must never answer a
    target it was not asked for."""
    source = InMemoryGoldStandardRungSource(rungs=(_rung(months=30),))
    with pytest.raises(RungNotFoundError):
        source.rung_for_target(
            RungTarget("talking_and_communicating", 24, "says about 50 words"))
    with pytest.raises(RungNotFoundError):
        source.rung_for_target(
            RungTarget("talking_and_communicating", 30, "says about 20 words"))


def test_a_target_in_another_domain_is_not_found():
    source = InMemoryGoldStandardRungSource(rungs=(_rung(),))
    with pytest.raises(RungNotFoundError):
        source.rung_for_target(
            RungTarget("fine_motor", 30, "says about 50 words"))


def test_lookup_tolerates_formatting_but_not_rewording():
    """Matching uses the same normalisation `compute_rung_ref` hashes, so
    "found here" and "same identity there" cannot disagree."""
    source = InMemoryGoldStandardRungSource(rungs=(_rung(),))
    assert source.rung_for_target(RungTarget(
        "talking_and_communicating", 30, "  SAYS   about 50 words  "))
    with pytest.raises(RungNotFoundError):
        source.rung_for_target(RungTarget(
            "talking_and_communicating", 30, "says roughly 50 words"))


def test_an_unreconciled_target_raises_not_mappable_not_not_found():
    """Two different conditions with two different correct responses: a
    caller's target being absent is a defect, while its activity families
    being unreconciled is known, expected content work."""
    blocked = RungTarget("talking_and_communicating", 30,
                         "says words like I me or we")
    source = InMemoryGoldStandardRungSource(rungs=(_rung(),),
                                            unmappable=(blocked,))
    with pytest.raises(RungNotMappableError):
        source.rung_for_target(blocked)
    assert not isinstance(RungNotMappableError(), RungNotFoundError)


def test_a_refusal_takes_precedence_over_a_stored_rung():
    """If a source somehow holds both, the refusal must win. Returning the
    rung would hand back provenance a reviewer had flagged as unresolved."""
    rung = _rung()
    target = RungTarget("talking_and_communicating", 30, "says about 50 words")
    source = InMemoryGoldStandardRungSource(rungs=(rung,), unmappable=(target,))
    with pytest.raises(RungNotMappableError):
        source.rung_for_target(target)


# ---------------------------------------------------------------------------
# listing is the choosable set
# ---------------------------------------------------------------------------

def test_listing_returns_only_mappable_rungs_months_ordered():
    early = _rung(months=12, milestone="calls parents mama or dada")
    late = _rung(months=36, milestone="says what action is happening")
    source = InMemoryGoldStandardRungSource(rungs=(late, early))
    listed = source.mappable_rungs_for_domain("talking_and_communicating")
    assert [r.source_rung_months for r in listed] == [12, 36]
    assert all(r.is_activity_mappable for r in listed)


def test_listing_excludes_a_rung_whose_family_denies_the_domain():
    """`is_activity_mappable` is false when any family forbids the domain, and
    such a rung must not appear in the set a planner may choose from."""
    denied = _rung(families=(("block_stacking", ("fine_motor",)),))
    assert not denied.is_activity_mappable
    source = InMemoryGoldStandardRungSource(rungs=(denied, _rung()))
    listed = source.mappable_rungs_for_domain("talking_and_communicating")
    assert [r.rung_ref for r in listed] == [_rung().rung_ref]


def test_listing_an_unknown_domain_is_empty_not_an_error():
    source = InMemoryGoldStandardRungSource(rungs=(_rung(),))
    assert source.mappable_rungs_for_domain("gross_motor") == ()


def test_listing_is_stable_across_calls():
    """The order feeds a deterministic generation step, so it may not depend
    on dict or set iteration order."""
    rungs = tuple(_rung(months=30, milestone=f"milestone {i}")
                  for i in range(6))
    source = InMemoryGoldStandardRungSource(rungs=rungs)
    first = source.mappable_rungs_for_domain("talking_and_communicating")
    assert first == source.mappable_rungs_for_domain(
        "talking_and_communicating")
    assert [r.rung_ref for r in first] == sorted(r.rung_ref for r in first)


# ---------------------------------------------------------------------------
# try_rung_for_target: the generation path's fail-closed helper
# ---------------------------------------------------------------------------

def test_try_returns_none_for_each_modelled_refusal():
    source = InMemoryGoldStandardRungSource()
    assert try_rung_for_target(source, RungTarget(
        "talking_and_communicating", 30, "nothing here")) is None


def test_try_returns_none_for_a_track_undeclared_refusal():
    class Trackless:
        def rung_for_target(self, target):
            raise RungTrackUndeclaredError("no declared track")

        def mappable_rungs_for_domain(self, domain_key):
            return ()

    assert try_rung_for_target(Trackless(), RungTarget(
        "talking_and_communicating", 24, "points to things in a book")) is None


def test_try_does_not_swallow_an_unexpected_fault():
    """A broken workbook read must not be laundered into "no rung today" —
    that would turn an outage into silently unanchored goals."""
    class Broken:
        def rung_for_target(self, target):
            raise OSError("the workbook could not be read")

        def mappable_rungs_for_domain(self, domain_key):
            return ()

    with pytest.raises(OSError):
        try_rung_for_target(Broken(), RungTarget(
            "talking_and_communicating", 30, "says about 50 words"))


def test_try_passes_a_found_rung_through_unchanged():
    rung = _rung()
    source = InMemoryGoldStandardRungSource(rungs=(rung,))
    assert try_rung_for_target(source, RungTarget(
        "talking_and_communicating", 30, "says about 50 words")) is rung


# ---------------------------------------------------------------------------
# structural properties of the port itself
# ---------------------------------------------------------------------------

def test_every_error_in_the_port_is_phi_safe():
    for error in (GoldStandardSourceError, RungNotFoundError,
                  RungNotMappableError, RungTrackUndeclaredError):
        assert error.PHI_SAFE_MESSAGE is True, error.__name__


def test_the_two_content_refusals_are_distinguishable():
    """A caller must be able to tell "your target is wrong" from "our content
    is not reconciled yet", because only the second is expected."""
    assert not issubclass(RungNotMappableError, RungNotFoundError)
    assert not issubclass(RungNotFoundError, RungNotMappableError)
    assert not issubclass(RungTrackUndeclaredError, RungNotMappableError)
    for error in (RungNotFoundError, RungNotMappableError,
                  RungTrackUndeclaredError):
        assert issubclass(error, GoldStandardSourceError)


def test_the_port_declares_no_write_operation():
    """The capability is ABSENT from the contract, not merely unused — an
    adapter satisfying this protocol cannot modify Parent content by mistake.
    """
    tree = ast.parse(
        (PILOT_ROOT / "integration" / "gold_standard_source.py").read_text())
    functions = [n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert functions, "AST walk found no functions — the guard would be vacuous"
    forbidden = ("save", "write", "update", "delete", "upsert", "register",
                 "create", "put", "set_", "patch", "remove")
    for name in functions:
        assert not any(bad in name.lower() for bad in forbidden), name


def test_the_port_imports_no_parent_module_and_no_dataframe_library():
    """The whole reason the port exists. If `pilot_backend` could read the
    workbook itself there would be no boundary to keep honest."""
    tree = ast.parse(
        (PILOT_ROOT / "integration" / "gold_standard_source.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported, "AST walk found no imports — the guard would be vacuous"
    for banned in ("pandas", "numpy", "openpyxl", "genex_core",
                   "parent_taxonomy", "openai"):
        assert banned not in imported, banned


def test_no_rung_content_is_restated_in_the_port():
    """The mirror this architecture rejects. The port must carry no milestone
    text and no activity-family identifier — those live in the workbook and
    the taxonomy, and cross the boundary at runtime.
    """
    source = (PILOT_ROOT / "integration" / "gold_standard_source.py").read_text()
    tree = ast.parse(source)
    assignments = [n for n in ast.walk(tree)
                   if isinstance(n, (ast.Assign, ast.AnnAssign))]
    assert assignments, "AST walk found no assignments — guard would be vacuous"
    rendered = "\n".join(ast.dump(n) for n in assignments)
    for content in ("expressive_vocabulary_growth", "early_vocalizations",
                    "gesture_communication", "says about 50 words",
                    "expressive_language"):
        assert content not in rendered, (
            f"{content!r} is restated in the port; rung and family content "
            f"must come from the Parent workbook and taxonomy at runtime")
