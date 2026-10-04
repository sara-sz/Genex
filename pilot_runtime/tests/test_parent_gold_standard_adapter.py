"""The LIVE Parent -> Pilot canonical-rung bridge, against the real content.

CROSS-SYSTEM. This file reads the actual frozen Gold Standard milestone
workbook and the actual activity-family taxonomy, so it needs `genex-parent`
on the path and pandas installed. It runs in a dedicated CI step
(`working-directory: .`, `PYTHONPATH=genex-parent`) because neither existing
job can import both sides: the Parent steps run inside `genex-parent/`, and the
pilot steps run from the repository root.

That is the point. The alternative design — restate the 21 SLP rungs inside
`pilot_backend` and pin them with a test — was rejected, so there is no mirror
to compare against. The only thing that can prove the bridge is correct is
reading the real content, which is what happens here.

Dependency note: only pandas, openpyxl and pytest are required, which is
exactly `requirements-parent24-ci.txt` and exactly what the offline generation
image installs. Two tests below pin that: the adapter imports with the cloud
SDKs blocked, and its import graph names no model client.
"""

from __future__ import annotations

import ast
import builtins
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PARENT_ROOT = REPO_ROOT / "genex-parent"

pytestmark = pytest.mark.skipif(
    not (PARENT_ROOT / "parent_taxonomy" / "activity_families.py").is_file(),
    reason="genex-parent content is not present in this checkout",
)

if str(PARENT_ROOT) not in sys.path:  # pragma: no cover - import plumbing
    sys.path.insert(0, str(PARENT_ROOT))

from pilot_backend.integration.gold_standard_source import (  # noqa: E402
    GoldStandardRungSource,
    RungNotFoundError,
    RungNotMappableError,
    RungTarget,
    RungTrackUndeclaredError,
    try_rung_for_target,
)
from pilot_runtime.integration.parent_gold_standard_source import (  # noqa: E402
    BASELINE_VERSION,
    ParentGoldStandardSource,
)

SLP = "talking_and_communicating"

#: Measured against the real content after Scenario C's 13 aliases. 40 distinct
#: Talking & Communicating rungs split exactly three ways:
#:   17 build completely                     -> the declared-track coverage
#:   13 refused, an activity family unresolved
#:   10 refused, no functional-baseline track declares the subdomain
EXPECTED_BUILT = 17
EXPECTED_FAMILY_REFUSALS = 13
EXPECTED_TRACK_REFUSALS = 10
EXPECTED_TOTAL_SLP_RUNGS = 40

#: The four rungs on the DECLARED functional-baseline track that Scenario C
#: leaves unmappable, by deterministic ref. Three are blocked by genuinely
#: MISSING category-2 families (`sound_response_orientation`, `pronouns`,
#: `wh_question_asking`) and one by an AMBIGUOUS value
#: (`expressive_name_response`). All four are explicitly October post-pilot.
#:
#: Pinned so the deferral is visible: if a later change made one of these
#: mappable, that is new clinical content and must be reviewed, not absorbed
#: as a coverage improvement.
EXPECTED_UNMAPPABLE_ON_TRACK = {
    (4, "rung1:fa5c92858a2ae846de14314785f8aa93"),
    (30, "rung1:46378a17002642f50efa858d5f95fbcd"),
    (36, "rung1:b8bfbe44eeecf8752020b639b2af3fdd"),
    (36, "rung1:6a360697f70191b8f20080d011bbd277"),
}


@pytest.fixture(scope="module")
def source() -> ParentGoldStandardSource:
    return ParentGoldStandardSource()


@pytest.fixture(scope="module")
def declared_track():
    """The SLP track read LIVE from the functional baseline, not restated."""
    from genex_core import functional_baseline as fb

    for area in fb.AREAS:
        if "expressive_language" in area.track_subdomains:
            return tuple(area.track_subdomains)
    raise AssertionError("no functional-baseline area declares expressive_language")


# ---------------------------------------------------------------------------
# the bridge satisfies the port and builds from real content
# ---------------------------------------------------------------------------

def test_the_live_adapter_satisfies_the_port(source):
    assert isinstance(source, GoldStandardRungSource)


def test_the_adapter_builds_the_expected_slp_coverage(source):
    built = source.mappable_rungs_for_domain(SLP)
    assert len(built) == EXPECTED_BUILT, [r.rung_ref for r in built]
    assert all(r.is_activity_mappable for r in built)
    assert all(r.domain_key == SLP for r in built)


def test_every_slp_rung_is_accounted_for(source):
    """Built plus refused must equal the whole domain. A rung that simply
    vanished would show up as improved coverage."""
    built = source.mappable_rungs_for_domain(SLP)
    refused = source.unmappable_targets_for_domain(SLP)
    family = [r for r in refused if r[2] == "RungNotMappableError"]
    track = [r for r in refused if r[2] == "RungTrackUndeclaredError"]
    assert len(family) == EXPECTED_FAMILY_REFUSALS
    assert len(track) == EXPECTED_TRACK_REFUSALS
    assert len(built) + len(refused) == EXPECTED_TOTAL_SLP_RUNGS


def test_the_declared_track_is_read_from_parent_not_restated(
        source, declared_track):
    """`track_ref` must be derived from the track Parent DECLARES. Every rung
    the adapter builds carries that exact subdomain tuple."""
    assert set(declared_track) == {"expressive_language",
                                   "early_vocalization_and_babbling"}
    for rung in source.mappable_rungs_for_domain(SLP):
        assert set(rung.track_subdomains) == set(declared_track), rung.rung_ref
        # Talking & Communicating declares no track families; only Daily
        # Living does. So SLP track identity rests on the subdomains alone.
        assert rung.track_families == (), rung.rung_ref


def test_every_built_rung_sits_on_the_declared_track(source, declared_track):
    for rung in source.mappable_rungs_for_domain(SLP):
        assert rung.subdomain in declared_track, rung.rung_ref


def test_all_built_rungs_share_one_track_ref(source):
    """One declared track, therefore one track identity — the planner can use
    it to recognise two goals as being on the same ladder."""
    refs = {r.track_ref for r in source.mappable_rungs_for_domain(SLP)}
    assert len(refs) == 1, refs


def test_the_taxonomy_and_baseline_versions_travel_with_every_rung(source):
    from parent_taxonomy import activity_families as af

    for rung in source.mappable_rungs_for_domain(SLP):
        assert rung.taxonomy_version == af.ACTIVITY_TAXONOMY_VERSION
        assert rung.baseline_version == BASELINE_VERSION


# ---------------------------------------------------------------------------
# the aliases are what moved coverage, and they resolved to real families
# ---------------------------------------------------------------------------

def test_built_rungs_name_only_canonical_families_that_permit_the_domain(source):
    from parent_taxonomy import activity_families as af

    taxonomy = af.get_taxonomy()
    for rung in source.mappable_rungs_for_domain(SLP):
        for ref in rung.activity_family_refs:
            # Canonical, never an alias: the adapter resolves before binding.
            assert ref in taxonomy.families, ref
            assert ref not in taxonomy.aliases, ref
            assert SLP in af.allowed_domains(ref), ref


def test_scenario_c_aliases_actually_carry_rungs(source):
    """Coverage moved because aliases resolved, not because something was
    dropped. `early_vocalizations` is the proof: all six early-vocalisation
    rungs reach it through `early_vocalization_sound_play`, an identifier the
    taxonomy does not define."""
    from parent_taxonomy import activity_families as af

    assert "early_vocalization_sound_play" in af.get_taxonomy().aliases
    assert "early_vocalization_sound_play" not in af.get_taxonomy().families
    reached = [r for r in source.mappable_rungs_for_domain(SLP)
               if "early_vocalizations" in r.activity_family_refs]
    assert len(reached) == 6, [r.rung_ref for r in reached]
    assert all(r.subdomain == "early_vocalization_and_babbling"
               for r in reached)


def test_the_four_aliased_gesture_values_collapse_to_one_family():
    """Four workbook identifiers alias onto `gesture_communication`. After
    resolution they are the same binding, so a rung naming two of them must
    not end up with a duplicate family ref."""
    from parent_taxonomy import activity_families as af

    targets = {af.resolve_family_key(v) for v in (
        "gesture_request_pickup", "gesture_requesting",
        "gesture_variety", "gesture_waving")}
    assert targets == {"gesture_communication"}


def test_a_rung_with_two_distinct_families_keeps_both_sorted(source):
    """The 24-month two-word rung legitimately maps to two families and 0.5E-A
    designates no primary, so both must survive, deduped and sorted."""
    both = [r for r in source.mappable_rungs_for_domain(SLP)
            if len(r.activity_family_refs) > 1]
    assert both, "expected at least one multi-family rung"
    for rung in both:
        refs = list(rung.activity_family_refs)
        assert refs == sorted(set(refs)), refs


# ---------------------------------------------------------------------------
# determinism — these refs get frozen into immutable anchors
# ---------------------------------------------------------------------------

def test_refs_are_deterministic_across_independent_instances():
    first = ParentGoldStandardSource()
    second = ParentGoldStandardSource()
    a = {(r.source_rung_months, r.rung_ref, r.track_ref,
          tuple(r.activity_family_refs))
         for r in first.mappable_rungs_for_domain(SLP)}
    b = {(r.source_rung_months, r.rung_ref, r.track_ref,
          tuple(r.activity_family_refs))
         for r in second.mappable_rungs_for_domain(SLP)}
    assert a == b


def test_the_adapter_reproduces_the_anchor_already_in_fictional_staging(source):
    """The strongest available check on the bridge: a goal was approved
    through the Therapist UI against an anchor seeded before this adapter
    existed. Rebuilding that rung from the real workbook must yield the SAME
    identity, or the bridge and the record it will extend disagree.
    """
    rung = source.rung_for_target(
        RungTarget(SLP, 30, "says about 50 words"))
    assert rung.rung_ref == "rung1:9993f7881ea0041be4813976973f77e6"
    assert rung.track_ref == "track1:5be892494f3e6894a7de24a868084e0f"
    assert rung.activity_family_refs == ("expressive_vocabulary_growth",)
    assert rung.subdomain == "expressive_language"
    assert rung.is_activity_mappable


def test_every_built_rung_has_a_distinct_ref(source):
    refs = [r.rung_ref for r in source.mappable_rungs_for_domain(SLP)]
    assert len(refs) == len(set(refs))


# ---------------------------------------------------------------------------
# fail closed, with the deferral pinned
# ---------------------------------------------------------------------------

def test_the_four_deferred_track_rungs_are_refused(source):
    refused = source.unmappable_targets_for_domain(SLP)
    built_refs = {r.rung_ref for r in source.mappable_rungs_for_domain(SLP)}
    for months, ref in sorted(EXPECTED_UNMAPPABLE_ON_TRACK):
        assert (months, ref, "RungNotMappableError") in refused, (months, ref)
        assert ref not in built_refs


@pytest.mark.parametrize("months,milestone", [
    (30, "says words like I me or we"),                     # pronouns
    (36, "ask who or what or where or why questions like "
         "where is mommy or where is daddy"),               # wh_question_asking
    (36, "says first name when asked"),                     # ambiguous
])
def test_a_deferred_rung_raises_not_mappable(source, months, milestone):
    with pytest.raises(RungNotMappableError):
        source.rung_for_target(RungTarget(SLP, months, milestone))
    assert try_rung_for_target(source, RungTarget(SLP, months, milestone)) is None


def test_an_off_track_rung_raises_track_undeclared(source):
    """A rung whose families all resolve but whose subdomain no baseline area
    declares. The adapter will not mint a track identity Parent never
    declared, even though it easily could."""
    with pytest.raises(RungTrackUndeclaredError):
        source.rung_for_target(RungTarget(
            SLP, 24,
            "points to things in a book when you ask like where is the bear"))


def test_an_absent_target_is_not_found_and_no_neighbour_is_returned(source):
    with pytest.raises(RungNotFoundError):
        source.rung_for_target(RungTarget(SLP, 30, "says about 51 words"))
    with pytest.raises(RungNotFoundError):
        source.rung_for_target(RungTarget(SLP, 31, "says about 50 words"))


def test_no_refused_rung_is_silently_replaced_by_another(source):
    """The substitution the founder ruled out. For every refused target, the
    adapter returns nothing — not the nearest mappable rung."""
    built = {r.rung_ref for r in source.mappable_rungs_for_domain(SLP)}
    for _, ref, _ in source.unmappable_targets_for_domain(SLP):
        assert ref not in built


def test_a_missing_parent_root_fails_closed_without_quoting_a_path():
    from pilot_backend.integration.gold_standard_source import (
        GoldStandardSourceError)

    with pytest.raises(GoldStandardSourceError) as caught:
        ParentGoldStandardSource(parent_root=REPO_ROOT / "no-such-directory")
    assert "no-such-directory" not in str(caught.value)


# ---------------------------------------------------------------------------
# defensive guards the real content does not currently exercise
#
# Three mutation survivors drove this section. Each of the guards below is
# UNREACHABLE with today's workbook — no SLP rung names a family that denies
# the domain, and no rung names two subdomains — so deleting the guard broke
# nothing. That makes them exactly the guards most likely to be removed as
# dead code by someone who checked only that the suite still passed. They are
# tested directly against `_assemble`, which is a focused unit test of adapter
# behaviour rather than a second path into production.
# ---------------------------------------------------------------------------

def _entry(months=30, milestone="a fabricated milestone",
           subdomains=("expressive_language",), families=()):
    return {"domain": SLP, "months": months, "milestone": milestone,
            "subdomains": set(subdomains), "families": set(families)}


@pytest.fixture()
def assemble_args(source):
    """The three collaborators `_assemble` takes, from the real content."""
    from genex_core import functional_baseline as fb
    from parent_taxonomy import activity_families as af

    return (ParentGoldStandardSource._declared_tracks(fb), af,
            af.ACTIVITY_TAXONOMY_VERSION)


def test_a_family_that_resolves_but_denies_the_domain_is_refused(
        source, assemble_args):
    """`block_stacking` is a real taxonomy family, so it resolves and has
    allowed domains — but they are fine_motor only. A rung naming it must be
    refused rather than returned with `is_activity_mappable` False, because
    the adapter's contract is that what it returns is usable."""
    from parent_taxonomy import activity_families as af

    assert af.is_known_family("block_stacking")
    assert SLP not in af.allowed_domains("block_stacking")
    tracks, families, version = assemble_args
    with pytest.raises(RungNotMappableError):
        source._assemble(_entry(families=("block_stacking",)),
                         tracks, families, version)


def test_a_rung_naming_two_subdomains_is_refused_not_resolved_by_picking(
        source, assemble_args):
    """Subdomain is functionally determined in the real workbook — measured
    zero conflicts across all 163 rungs — so this cannot happen today. If it
    ever did, `subdomain` would silently become a choice between two, and the
    track lookup would follow whichever sorted first."""
    tracks, families, version = assemble_args
    with pytest.raises(RungNotMappableError):
        source._assemble(
            _entry(subdomains=("expressive_language",
                               "early_vocalization_and_babbling"),
                   families=("expressive_vocabulary_growth",)),
            tracks, families, version)


def test_a_rung_naming_no_activity_family_is_refused(source, assemble_args):
    tracks, families, version = assemble_args
    with pytest.raises(RungNotMappableError):
        source._assemble(_entry(families=()), tracks, families, version)


def test_a_rung_naming_no_subdomain_is_refused(source, assemble_args):
    tracks, families, version = assemble_args
    with pytest.raises(RungTrackUndeclaredError):
        source._assemble(_entry(subdomains=()), tracks, families, version)


def test_the_taxonomy_version_is_read_live_and_not_a_literal(monkeypatch):
    """The mirror this architecture rejects, in miniature. A hard-coded
    "activity_family_taxonomy_v1" passes every test today because that IS the
    current value — and would keep passing, silently stamping v1 onto anchors,
    after Parent moved to v2. Changing the source of truth must change the
    output."""
    from parent_taxonomy import activity_families as af

    monkeypatch.setattr(af, "ACTIVITY_TAXONOMY_VERSION",
                        "activity_family_taxonomy_vNEXT")
    rung = ParentGoldStandardSource().rung_for_target(
        RungTarget(SLP, 30, "says about 50 words"))
    assert rung.taxonomy_version == "activity_family_taxonomy_vNEXT"


def test_declared_track_families_are_the_union_across_a_whole_area():
    """Talking & Communicating declares no track families, so dropping the
    union over an area's choices is invisible from SLP alone. Daily Living is
    where it matters: its choices each name a subset, and `track_ref` is
    defined over the TRACK's families."""
    from genex_core import functional_baseline as fb

    tracks = ParentGoldStandardSource._declared_tracks(fb)
    assert tracks["expressive_language"][1] == ()
    subdomains, families = tracks["self_help_motor_skills"]
    assert subdomains == ("self_help_motor_skills",)
    # The union of every choice's families, deduplicated and sorted — not one
    # choice's subset, and not empty.
    assert families == ("buttoning_fasteners", "dressing_off", "dressing_on",
                        "finger_feeding", "fork_use", "serving_pouring_transfer",
                        "spoon_use")


# ---------------------------------------------------------------------------
# structural: no mirror, no write, no model client
# ---------------------------------------------------------------------------

ADAPTER = (REPO_ROOT / "pilot_runtime" / "integration"
           / "parent_gold_standard_source.py")


def test_the_adapter_restates_no_rung_or_family_content():
    """The anti-mirror guard. If the 21 SLP rungs were copied in here, this is
    where it would fail — the adapter must read content, never carry it."""
    tree = ast.parse(ADAPTER.read_text())
    assignments = [n for n in ast.walk(tree)
                   if isinstance(n, (ast.Assign, ast.AnnAssign))]
    assert assignments, "AST walk found no assignments — guard would be vacuous"
    rendered = "\n".join(ast.dump(n) for n in assignments)
    for content in ("expressive_vocabulary_growth", "early_vocalizations",
                    "gesture_communication", "two_word_phrases",
                    "says about 50 words", "expressive_language",
                    "early_vocalization_and_babbling", "rung1:", "track1:"):
        assert content not in rendered, (
            f"{content!r} is restated in the adapter; it must come from the "
            f"Parent workbook and taxonomy at runtime")


def test_the_adapter_declares_no_mutating_method():
    tree = ast.parse(ADAPTER.read_text())
    functions = [n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert functions, "AST walk found no functions — guard would be vacuous"
    forbidden = ("save", "write", "upload", "delete", "upsert", "patch",
                 "remove", "to_excel", "overwrite")
    for name in functions:
        assert not any(bad in name.lower() for bad in forbidden), name


def test_the_adapter_names_no_model_client_anywhere_in_its_graph():
    """`functional_baseline` reaches `activity_engine`, which holds Parent's
    OpenAI client. That import is lazy, behind a try/except and two env vars,
    so it never fires here — and the generation image does not install the
    package. This pins that the adapter itself names nothing of the sort."""
    text = ADAPTER.read_text()
    tree = ast.parse(text)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported, "AST walk found no imports — guard would be vacuous"
    for banned in ("openai", "anthropic", "requests", "httpx", "urllib"):
        assert banned not in imported, banned
    assert "openai" not in {n.id for n in ast.walk(tree)
                            if isinstance(n, ast.Name)}


def test_the_adapter_imports_with_the_cloud_sdks_absent():
    """Proves the generation image needs only pandas and openpyxl, not the
    serving image's firebase/firestore pins."""
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.split(".")[0] in ("firebase_admin", "openai"):
            raise ImportError(f"blocked for this test: {name}")
        return real_import(name, *args, **kwargs)

    for module in [m for m in list(sys.modules)
                   if m.startswith("pilot_runtime.integration"
                                   ".parent_gold_standard_source")]:
        del sys.modules[module]
    builtins.__import__ = blocked
    try:
        from pilot_runtime.integration import (
            parent_gold_standard_source as reimported)
        assert reimported.ParentGoldStandardSource(
        ).mappable_rungs_for_domain(SLP)
    finally:
        builtins.__import__ = real_import


def test_the_served_entrypoint_does_not_reach_this_adapter():
    """The serving image must stay narrow. `pilot_runtime/server.py` is what
    gunicorn loads; if its import graph reached this module, the served image
    would need pandas and the Parent workbook."""
    seen, queue = set(), ["pilot_runtime/server.py"]
    while queue:
        rel = queue.pop()
        if rel in seen:
            continue
        seen.add(rel)
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            modules = []
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and \
                    node.level == 0:
                modules = [node.module]
            for module in modules:
                if module.startswith(("pilot_runtime", "pilot_backend")):
                    queue.append(module.replace(".", "/") + ".py")
                    queue.append(module.replace(".", "/") + "/__init__.py")
    assert "pilot_runtime/server.py" in seen
    assert ("pilot_runtime/integration/parent_gold_standard_source.py"
            not in seen), "the served entrypoint reaches the workbook adapter"
