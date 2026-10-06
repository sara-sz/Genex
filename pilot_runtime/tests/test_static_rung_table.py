"""0.5F-B Option C — the generated rung table and the static browser adapter.

Five things are proved here, in order of how much they would cost to get wrong:

1. EQUIVALENCE. The static adapter and the live `ParentGoldStandardSource`
   agree on every step input and every rung. This is the test that makes the
   artifact trustworthy — not the digest, which only proves the file is
   internally consistent. A table that is self-consistent but says something
   the frozen engine never said would pass every other test in this file.
2. DRIFT. Regenerating from the pinned workbooks reproduces the committed
   artifact byte for byte. A developer editing `18 -> 30` by hand cannot get
   green.
3. POPULATION. All 21 declared-track outcomes are present, 17 mappable and 4
   unresolved, with no off-track rung admitted.
4. PURITY. The static adapter's transitive import graph contains no Parent
   module, no spreadsheet library and no model client.
5. FAIL-CLOSED. The reachable 30 -> 36 unresolved target refuses, the top of
   the track returns None, and a tampered artifact raises rather than serving.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

from pilot_backend.domain.canonical_rung import (
    compute_rung_ref,
    compute_track_ref,
    normalize_milestone_text,
)
from pilot_backend.integration.gold_standard_source import (
    GoldStandardSourceError,
    RungNotFoundError,
    RungNotMappableError,
    RungTarget,
)
from pilot_runtime.integration import rung_table_generator as GEN
from pilot_runtime.integration.parent_gold_standard_source import (
    ParentGoldStandardSource,
)
from pilot_runtime.integration.static_rung_source import (
    DEFAULT_ARTIFACT_PATH,
    StaticRungTableError,
    StaticRungTableSource,
    build_static_rung_source,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DOMAIN = "talking_and_communicating"

# The fictional fixture, pinned by the founder. Reproduced from the generated
# artifact, NOT from an injected test double.
FLOOR_MONTHS = 18
EXPECTED_TARGET_MONTHS = 24
EXPECTED_MILESTONE = "says at least two words together like more milk"
EXPECTED_RUNG_REF = "rung1:feb590cf2788978b383c11062ce67c1b"
EXPECTED_TRACK_REF = "track1:5be892494f3e6894a7de24a868084e0f"
EXPECTED_FAMILIES = ("expressive_vocabulary_growth", "two_word_phrases")
EXPECTED_SUBDOMAIN = "expressive_language"

# Frozen provenance. The candidate workbook is the file the Parent brain reads
# and therefore the file every rung actually came from; the snapshot is the
# frozen upstream provenance input. Both are byte-pinned in CI.
RUNG_WORKBOOK_SHA = \
    "a16ce10fe9c932bc7ca6e3275748541b3c8a1e15a0d8a2702b0560a56ddc5fd6"
TAXONOMY_SHA = \
    "e5835969ead556129530f01d4629c584b823729a1b4e4425e5e3d29ddba4d262"
GOLD_STANDARD_SNAPSHOT_SHA = \
    "c2b6735d9f099c916973c98c1953e7eb1f2ca8e1a60430011f805a3bf9c3487c"


@pytest.fixture(scope="module")
def artifact():
    return json.loads(DEFAULT_ARTIFACT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def static_source():
    return build_static_rung_source()


@pytest.fixture(scope="module")
def live_source():
    return ParentGoldStandardSource()


# ---------------------------------------------------------------------------
# 1. EQUIVALENCE — the static table says exactly what the frozen engine says
# ---------------------------------------------------------------------------


def test_every_step_input_agrees_with_the_frozen_parent_engine(
        static_source, live_source):
    """The whole probed input domain, not a sample.

    This is the test that earns the artifact its trust. The digest proves the
    file is internally consistent; only this proves it is TRUE. Run over every
    integer the generator probed, so a single wrong entry fails.
    """
    checked = 0
    for from_months in range(0, GEN.MAX_PROBED_FROM_MONTHS + 1):
        live = live_source.next_rung_target(DOMAIN, from_months)
        static = static_source.next_rung_target(DOMAIN, from_months)
        if live is None:
            assert static is None, from_months
            continue
        assert static is not None, from_months
        assert static.source_rung_months == live.source_rung_months, from_months
        assert normalize_milestone_text(static.milestone_text) == \
            normalize_milestone_text(live.milestone_text), from_months
        checked += 1
    # Non-vacuity: a bug that made every lookup return None would otherwise
    # satisfy the loop above.
    assert checked == 60


def test_every_declared_track_rung_agrees_with_the_live_adapter(
        static_source, artifact, live_source):
    """Mappable rungs resolve identically; unresolved ones refuse identically."""
    agreed_ok = agreed_refused = 0
    for ref, entry in artifact["rungs"].items():
        target = RungTarget(domain_key=DOMAIN,
                            source_rung_months=entry["source_rung_months"],
                            milestone_text=entry["milestone_text"])
        try:
            live = live_source.rung_for_target(target)
        except GoldStandardSourceError as live_refusal:
            with pytest.raises(type(live_refusal)):
                static_source.rung_for_target(target)
            agreed_refused += 1
            continue
        static = static_source.rung_for_target(target)
        assert static == live, ref
        agreed_ok += 1
    assert (agreed_ok, agreed_refused) == (17, 4)


def test_the_static_source_satisfies_the_same_port(static_source):
    from pilot_backend.integration.gold_standard_source import (
        GoldStandardRungSource,
    )

    assert isinstance(static_source, GoldStandardRungSource)


# ---------------------------------------------------------------------------
# 2. DRIFT — a hand edit cannot pass
# ---------------------------------------------------------------------------


def test_regenerating_reproduces_the_committed_artifact_exactly():
    """The local form of the hosted drift gate.

    Compared in CANONICAL form rather than raw bytes so reindenting the
    committed file is not a failure, while any content change is. The digest is
    compared separately, so a change that somehow survived canonicalisation
    still fails.
    """
    regenerated = GEN.build_artifact()
    committed = json.loads(DEFAULT_ARTIFACT_PATH.read_text(encoding="utf-8"))
    assert GEN.canonical_json(regenerated) == GEN.canonical_json(committed)
    assert regenerated["artifact_digest"] == committed["artifact_digest"]


def test_the_committed_file_is_byte_identical_to_its_renderer():
    """Catches a committed file that is content-correct but hand-reformatted."""
    regenerated = GEN.build_artifact()
    assert GEN.render(regenerated) == \
        DEFAULT_ARTIFACT_PATH.read_text(encoding="utf-8")


def test_the_digest_covers_the_whole_body(artifact):
    assert GEN.artifact_digest(artifact) == artifact["artifact_digest"]


def test_hand_editing_a_target_month_is_refused(tmp_path):
    """The `18 -> 30` scenario, exactly.

    Two layers catch it. The digest fails first; and if the editor recomputes
    the digest, the step then names a rung whose months disagree with the one
    it points at — so the lie has to be made consistent in two places and
    still cannot survive the drift gate, which regenerates from the workbooks.
    """
    body = json.loads(DEFAULT_ARTIFACT_PATH.read_text(encoding="utf-8"))
    body["steps"]["18"]["target_months"] = 30
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(StaticRungTableError):
        StaticRungTableSource(tampered)

    # Now recompute the digest, as a determined editor would.
    body["artifact_digest"] = GEN.artifact_digest(body)
    tampered.write_text(json.dumps(body), encoding="utf-8")
    source = StaticRungTableSource(tampered)
    # The step still resolves through the RUNG, whose own months are canonical,
    # so the edited `target_months` field is simply not believed.
    assert source.next_rung_target(DOMAIN, 18).source_rung_months == \
        EXPECTED_TARGET_MONTHS
    # And the drift gate still refuses it.
    assert GEN.canonical_json(GEN.build_artifact()) != \
        GEN.canonical_json(body)


def test_hand_editing_a_milestone_is_refused(tmp_path):
    """A reworded milestone changes the rung identity, so the ref stops matching."""
    body = json.loads(DEFAULT_ARTIFACT_PATH.read_text(encoding="utf-8"))
    body["rungs"][EXPECTED_RUNG_REF]["milestone_text"] = "says three words"
    body["artifact_digest"] = GEN.artifact_digest(body)
    tampered = tmp_path / "reworded.json"
    tampered.write_text(json.dumps(body), encoding="utf-8")
    source = StaticRungTableSource(tampered)
    with pytest.raises(StaticRungTableError):
        source.rung_for_target(RungTarget(
            domain_key=DOMAIN, source_rung_months=EXPECTED_TARGET_MONTHS,
            milestone_text="says three words"))


def test_an_unsupported_schema_is_refused(tmp_path):
    body = json.loads(DEFAULT_ARTIFACT_PATH.read_text(encoding="utf-8"))
    body["artifact_schema_version"] = "pilot-rung-table-v2"
    body["artifact_digest"] = GEN.artifact_digest(body)
    path = tmp_path / "v2.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(StaticRungTableError):
        StaticRungTableSource(path)


def test_a_missing_artifact_raises_rather_than_degrading(tmp_path):
    """Absent is a BUILD defect, not a safe configuration state.

    Returning None here would reinstate the exact silent 403 the 0.5F-B
    inspection found: a route that authorises correctly and then refuses for an
    unrelated-sounding reason.
    """
    with pytest.raises(StaticRungTableError):
        StaticRungTableSource(tmp_path / "absent.json")


# ---------------------------------------------------------------------------
# 3. POPULATION — 21 = 17 + 4, and nothing off-track
# ---------------------------------------------------------------------------


def test_the_declared_track_population_is_twenty_one(artifact):
    rungs = artifact["rungs"]
    mappable = [e for e in rungs.values() if e["mappable"]]
    unresolved = [e for e in rungs.values() if not e["mappable"]]
    assert len(rungs) == 21
    assert len(mappable) == 17
    assert len(unresolved) == 4
    assert artifact["population"] == {
        "declared_track_rungs": 21,
        "declared_track_mappable": 17,
        "declared_track_unresolved": 4,
    }


def test_the_four_unresolved_rungs_are_the_expected_ones(artifact):
    """Identified by months + refusal, with the milestone text pinned.

    Pinned explicitly so that an unresolved rung BECOMING mappable is a visible
    test failure requiring review, rather than a quiet coverage improvement.
    """
    unresolved = sorted(
        (e["source_rung_months"], e["unresolved_reason"], e["milestone_text"])
        for e in artifact["rungs"].values() if not e["mappable"])
    assert unresolved == [
        (4, "RungNotMappableError", "makes sounds back when you talk to him"),
        (30, "RungNotMappableError", "says words like I me or we"),
        (36, "RungNotMappableError",
         "ask who or what or where or why questions like where is mommy "
         "or where is daddy"),
        (36, "RungNotMappableError", "says first name when asked"),
    ]


def test_no_off_track_talking_rung_is_admitted(artifact):
    declared = set(artifact["declared_track"]["track_subdomains"])
    assert declared == {"expressive_language",
                        "early_vocalization_and_babbling"}
    for ref, entry in artifact["rungs"].items():
        assert entry["subdomain"] in declared, ref


def test_the_off_track_talking_rungs_are_excluded_and_counted(live_source):
    """19 Talking rungs are off the declared track and must not be in the table.

    Counted against the live adapter rather than restated, so the exclusion is
    measured from the same workbook the table was generated from.
    """
    live_source._load()
    declared = {"expressive_language", "early_vocalization_and_babbling"}
    on_track = [r for r in live_source._rungs.values()
                if r.domain_key == DOMAIN and r.subdomain in declared]
    total = len([r for r in live_source._rungs.values()
                 if r.domain_key == DOMAIN]) + \
        len([k for k in live_source._refusals if k[0] == DOMAIN])
    assert total == 40
    assert len(on_track) == 17
    # 40 total - 21 declared-track = 19 off-track.
    assert total - 21 == 19


def test_every_ref_in_the_artifact_recomputes(artifact):
    """Both identifiers, recomputed from the fields the artifact itself carries."""
    track_ref = compute_track_ref(
        DOMAIN,
        artifact["declared_track"]["track_subdomains"],
        artifact["declared_track"]["track_families"])
    assert track_ref == EXPECTED_TRACK_REF
    for ref, entry in artifact["rungs"].items():
        assert compute_rung_ref(DOMAIN, entry["source_rung_months"],
                                entry["milestone_text"]) == ref
        if entry["mappable"]:
            assert entry["track_ref"] == track_ref
        else:
            # An unresolved rung has no usable track binding to record.
            assert entry["track_ref"] is None


def test_every_step_target_exists_in_the_rung_table(artifact):
    for from_months, step in artifact["steps"].items():
        assert step["target_rung_ref"] in artifact["rungs"], from_months


def test_the_step_table_covers_a_contiguous_domain(artifact):
    """0..59 have targets and the tail is genuinely empty.

    The tail matters: "no entry" must mean "the Parent engine returned None",
    never "the generator stopped probing". The generator probes to 120.
    """
    present = {int(k) for k in artifact["steps"]}
    assert present == set(range(0, 60))
    assert artifact["step_domain"] == {
        "min_from_months": 0,
        "max_probed_from_months": GEN.MAX_PROBED_FROM_MONTHS,
        "highest_from_months_with_target": 59,
    }


def test_the_artifact_records_both_workbook_shas(artifact):
    """Provenance names the file the rungs CAME from, not only the snapshot.

    Recording only the frozen snapshot would be false provenance: a change to
    the candidate workbook would move every rung here while the recorded SHA
    stayed put.
    """
    p = artifact["provenance"]
    assert p["rung_workbook_sha256"] == RUNG_WORKBOOK_SHA
    assert p["taxonomy_sha256"] == TAXONOMY_SHA
    assert p["gold_standard_snapshot_sha256"] == GOLD_STANDARD_SNAPSHOT_SHA
    assert p["taxonomy_version"] == "activity_family_taxonomy_v1"
    assert p["baseline_version"] == "parent-2.4-functional-baseline-v1"


def test_the_recorded_shas_match_the_files_on_disk(artifact):
    """Non-vacuity for the test above: the constants are not just self-agreeing."""
    import hashlib

    root = REPO_ROOT / "genex-parent"
    for relpath_key, sha_key in (
            ("rung_workbook_relpath", "rung_workbook_sha256"),
            ("taxonomy_relpath", "taxonomy_sha256"),
            ("gold_standard_snapshot_relpath",
             "gold_standard_snapshot_sha256")):
        path = root / artifact["provenance"][relpath_key]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == artifact["provenance"][sha_key], relpath_key


def test_the_artifact_encodes_no_child_specific_ceiling(artifact):
    """A ceiling belongs to ONE child's baseline; this table is shared.

    Checked structurally over the whole document rather than per field, so a
    later addition cannot smuggle one in.
    """
    text = json.dumps(artifact)
    for forbidden in ("not_demonstrated", "ceiling", "child_id", "projection",
                      "dev_age", "chronological", "diagnosis", "entry_choice"):
        assert forbidden not in text, forbidden


# ---------------------------------------------------------------------------
# 4. PURITY — what the browser adapter may not import
# ---------------------------------------------------------------------------


FORBIDDEN_IMPORTS = (
    "pandas", "openpyxl", "xlrd", "genex_core", "parent_taxonomy",
    "openai", "anthropic",
)


def test_the_static_adapter_imports_nothing_forbidden():
    """A FRESH interpreter, so an import another test already did cannot hide one.

    Imports only the adapter and asserts the forbidden modules are absent from
    `sys.modules` afterwards — the transitive graph, not the import lines.
    """
    import subprocess

    code = (
        "import sys, json\n"
        "import pilot_runtime.integration.static_rung_source as M\n"
        "M.build_static_rung_source()\n"
        "print(json.dumps(sorted(\n"
        "    m for m in sys.modules\n"
        f"    if m.split('.')[0] in {list(FORBIDDEN_IMPORTS)!r})))\n"
    )
    result = subprocess.run([sys.executable, "-c", code],
                            capture_output=True, text=True, cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == []


def test_the_static_adapter_names_no_forbidden_module_in_its_source():
    """Belt and braces: a lazy import would not show up in the test above."""
    source = (Path(__file__).resolve().parents[1] / "integration" /
              "static_rung_source.py").read_text(encoding="utf-8")
    body = source.split('"""', 2)[2]
    for forbidden in FORBIDDEN_IMPORTS:
        assert f"import {forbidden}" not in body, forbidden
    assert ".xlsx" not in body


def test_the_static_adapter_has_no_write_operation():
    source = (Path(__file__).resolve().parents[1] / "integration" /
              "static_rung_source.py").read_text(encoding="utf-8")
    body = source.split('"""', 2)[2]
    for mutating in ("write_text", "write_bytes", "open(", "unlink", "mkdir"):
        assert mutating not in body, mutating


# ---------------------------------------------------------------------------
# 5. THE PINNED FIXTURE AND FAIL-CLOSED BEHAVIOUR
# ---------------------------------------------------------------------------


def test_the_pinned_fixture_resolves_through_the_static_table(static_source):
    """floor 18 -> frozen _step(+1) -> 24, from the artifact alone."""
    target = static_source.next_rung_target(DOMAIN, FLOOR_MONTHS)
    assert target is not None
    assert target.source_rung_months == EXPECTED_TARGET_MONTHS
    assert target.milestone_text == EXPECTED_MILESTONE

    rung = static_source.rung_for_target(target)
    assert rung.rung_ref == EXPECTED_RUNG_REF
    assert rung.track_ref == EXPECTED_TRACK_REF
    assert rung.subdomain == EXPECTED_SUBDOMAIN
    assert rung.activity_family_refs == EXPECTED_FAMILIES
    assert rung.is_activity_mappable is True
    assert rung.taxonomy_version == "activity_family_taxonomy_v1"
    assert rung.baseline_version == "parent-2.4-functional-baseline-v1"


def test_the_reachable_unresolved_target_fails_closed(static_source):
    """floor 30 steps to a 36m rung the taxonomy does not reconcile.

    Reachable, not hypothetical — which is why the artifact represents the four
    failures instead of omitting them. Omitted, this target would be absent and
    `next_rung_target` would return None, which the caller reads as "top of
    track": a wrong answer dressed as a normal one.
    """
    target = static_source.next_rung_target(DOMAIN, 30)
    assert target.source_rung_months == 36
    with pytest.raises(RungNotMappableError):
        static_source.rung_for_target(target)


def test_the_top_of_the_declared_track_returns_none(static_source):
    assert static_source.next_rung_target(DOMAIN, 60) is None
    assert static_source.next_rung_target(DOMAIN, 999) is None


def test_an_unknown_domain_yields_nothing(static_source):
    assert static_source.next_rung_target("fine_motor", 18) is None
    assert static_source.mappable_rungs_for_domain("fine_motor") == ()


def test_a_negative_or_non_integer_floor_is_refused(static_source):
    with pytest.raises(GoldStandardSourceError):
        static_source.next_rung_target(DOMAIN, -1)
    with pytest.raises(GoldStandardSourceError):
        static_source.next_rung_target(DOMAIN, True)


def test_an_absent_target_is_not_found(static_source):
    with pytest.raises(RungNotFoundError):
        static_source.rung_for_target(RungTarget(
            domain_key=DOMAIN, source_rung_months=24,
            milestone_text="a milestone the workbook does not contain"))


def test_mappable_rungs_are_months_ordered_and_exclude_the_unresolved(
        static_source):
    rungs = static_source.mappable_rungs_for_domain(DOMAIN)
    assert len(rungs) == 17
    assert all(r.is_activity_mappable for r in rungs)
    months = [r.source_rung_months for r in rungs]
    assert months == sorted(months)


def test_the_source_exposes_its_verified_digest(static_source, artifact):
    assert static_source.artifact_digest == artifact["artifact_digest"]
    assert static_source.provenance["taxonomy_sha256"] == TAXONOMY_SHA


# ---------------------------------------------------------------------------
# 6. COMPOSITION — the DEPLOYED browser app gets a live rung source
# ---------------------------------------------------------------------------


def test_the_built_runtime_has_a_non_none_rung_source():
    """The headline of Option C: `rung_source != None` in the real composition.

    Before this slice the deployed app passed nothing, so the generation route
    failed closed with 403 for a reason unrelated to the request.
    """
    from pilot_runtime.composition import build_runtime

    runtime = build_runtime({"PILOT_ENVIRONMENT": "dev",
                             "PILOT_DEV_AUTH_ENABLED": "true"},
                            in_memory=True)
    assert runtime.rung_source is not None
    assert isinstance(runtime.rung_source, StaticRungTableSource)
    # And the WSGI application holds the SAME object — not a second one, and
    # not None.
    assert runtime.application._rung_source is runtime.rung_source


def test_the_served_entrypoint_still_does_not_reach_the_live_adapter():
    """Composition must use the STATIC source, never the workbook adapter.

    Preserved from 0.5E-B. The live adapter needs pandas and the Parent
    package; a composition that imported it would reintroduce the dependency
    Option C exists to avoid, and would fail at runtime in the serving image.
    """
    import subprocess

    code = (
        "import sys, json\n"
        "import pilot_runtime.composition as C\n"
        "C.build_runtime({'PILOT_ENVIRONMENT': 'dev',\n"
        "                 'PILOT_DEV_AUTH_ENABLED': 'true'}, in_memory=True)\n"
        "print(json.dumps(\n"
        "    'pilot_runtime.integration.parent_gold_standard_source'\n"
        "    in sys.modules))\n"
    )
    result = subprocess.run([sys.executable, "-c", code],
                            capture_output=True, text=True, cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) is False


# ---------------------------------------------------------------------------
# 7. END TO END — the HTTP route, served by the REAL generated artifact
# ---------------------------------------------------------------------------


def test_the_generation_route_serves_eighteen_to_twenty_four_from_the_artifact():
    """A real POST, authorised as the managing clinician, no injected rung.

    Every other HTTP test for this route injects `InMemoryGoldStandardRungSource`
    with a hand-built rung. This one wires the REAL `StaticRungTableSource` over
    the committed artifact, so the suggestion's anchor is whatever the generated
    table actually says — the founder's "not an injected test fixture".
    """
    from pilot_backend.domain.parent_baseline_projection import (
        ParentBaselineProjection,
    )
    from pilot_backend.domain.source_link import SourceSystem, SourceSystemLink
    from pilot_backend.domain.entities import utc_now
    from pilot_backend.domain.roles import ActorRole
    from pilot_backend.domain.managing_clinician import (
        ManagingClinicianAssignment,
    )
    from pilot_backend.tests.test_integration_identity import build_http, call

    http = build_http(rung_source=build_static_rung_source())
    child_id = http.topo.child_alpha.child_id
    now = utc_now()
    # `build_secure_topology` connects provider_alpha but assigns no managing
    # clinician, and the route requires one — so the assignment is made here
    # from the topology's OWN connection rather than a second fabricated one.
    http.repos.managing_clinicians.create(ManagingClinicianAssignment.create(
        child_id, http.topo.provider_alpha.provider_id,
        http.topo.practice.practice_id,
        provider_connection_id=http.topo.link_alpha_provider.connection_id,
        actor_id=http.topo.caregiver_alpha.caregiver_id, now=now))
    session = "sess-fictional-option-c"
    http.repos.source_links.create(SourceSystemLink.create(
        child_id, SourceSystem.PARENT, session, actor_id="fixture",
        actor_role=ActorRole.CAREGIVER.value, now=now))
    http.repos.parent_baseline_projections.create(
        ParentBaselineProjection.build(
            child_id=child_id, source_session_id=session,
            source_record_digest="e" * 64,
            projection={"domain": DOMAIN, "area_id": "talking",
                        "entry_choice_id": "many_single_words",
                        "routing_anchor_months": FLOOR_MONTHS,
                        "not_demonstrated_months": EXPECTED_TARGET_MONTHS,
                        "status": "BOUNDED",
                        "baseline_version":
                            "parent-2.4-functional-baseline-v1"},
            now=now))

    status, body, _ = call(
        http.app, f"/pilot/children/{child_id}/goal-suggestions/generate",
        method="POST", bearer="Bearer token-provider-alpha")
    # 200, not 201: the frozen route returns HTTP_OK and reports whether it
    # created or resolved via the `created` flag.
    assert status == 200, body
    assert body["created"] is True

    # The ceiling check is the RUNTIME's, against this child's projection:
    # 24 <= 24, inclusive.
    assert body["target_rung_months"] == EXPECTED_TARGET_MONTHS
    assert body["target_rung_ref"] == EXPECTED_RUNG_REF

    # The anchor stores the whole canonical rung under `rung`, so the refs are
    # read from there rather than from a flattened column.
    anchors = http.repos.store.list_all("pilot_suggestion_anchors")
    assert len(anchors) == 1
    anchored = anchors[0][1]["rung"]
    assert anchored["rung_ref"] == EXPECTED_RUNG_REF
    assert anchored["track_ref"] == EXPECTED_TRACK_REF
    assert anchored["milestone_text"] == EXPECTED_MILESTONE
    assert [b["family_ref"] for b in anchored["family_bindings"]] == \
        list(EXPECTED_FAMILIES)

    # Lineage: the claim names the projection and the suggestion it produced.
    claims = http.repos.store.list_all("pilot_suggestion_generation_claims")
    assert len(claims) == 1
    suggestions = http.repos.store.list_all("pilot_goal_suggestions")
    assert len(suggestions) == 1
    assert suggestions[0][1]["suggestion_id"] in \
        list(claims[0][1]["suggestion_ids"])

    # And the corrected lineage field stays unset, so no prior-cycle-continuity
    # score was earned.
    assert suggestions[0][1]["evidence"]["prior_month_summary_id"] is None


def test_a_floor_of_thirty_fails_closed_through_the_real_route():
    """The reachable unresolved target, end to end: 30 -> 36 is refused.

    Proves the artifact's explicit representation of the four failures is what
    produces a refusal rather than a silent "top of track".
    """
    from pilot_backend.domain.parent_baseline_projection import (
        ParentBaselineProjection,
    )
    from pilot_backend.domain.source_link import SourceSystem, SourceSystemLink
    from pilot_backend.domain.entities import utc_now
    from pilot_backend.domain.roles import ActorRole
    from pilot_backend.domain.managing_clinician import (
        ManagingClinicianAssignment,
    )
    from pilot_backend.tests.test_integration_identity import build_http, call

    http = build_http(rung_source=build_static_rung_source())
    child_id = http.topo.child_alpha.child_id
    now = utc_now()
    # `build_secure_topology` connects provider_alpha but assigns no managing
    # clinician, and the route requires one — so the assignment is made here
    # from the topology's OWN connection rather than a second fabricated one.
    http.repos.managing_clinicians.create(ManagingClinicianAssignment.create(
        child_id, http.topo.provider_alpha.provider_id,
        http.topo.practice.practice_id,
        provider_connection_id=http.topo.link_alpha_provider.connection_id,
        actor_id=http.topo.caregiver_alpha.caregiver_id, now=now))
    session = "sess-fictional-unresolved"
    http.repos.source_links.create(SourceSystemLink.create(
        child_id, SourceSystem.PARENT, session, actor_id="fixture",
        actor_role=ActorRole.CAREGIVER.value, now=now))
    http.repos.parent_baseline_projections.create(
        ParentBaselineProjection.build(
            child_id=child_id, source_session_id=session,
            source_record_digest="f" * 64,
            projection={"domain": DOMAIN, "area_id": "talking",
                        "entry_choice_id": "two_three_words",
                        "routing_anchor_months": 30,
                        "not_demonstrated_months": 36,
                        "status": "BOUNDED",
                        "baseline_version":
                            "parent-2.4-functional-baseline-v1"},
            now=now))

    status, body, _ = call(
        http.app, f"/pilot/children/{child_id}/goal-suggestions/generate",
        method="POST", bearer="Bearer token-provider-alpha")
    # 403, and deliberately not a distinct status: the frozen route renders
    # EVERY refusal as the same constant so a prober cannot learn which gate
    # stopped them. The refusal here is clinical (the target is unresolved),
    # and it is indistinguishable from an authorization refusal by design.
    assert status == 403, body
    assert http.repos.store.list_all("pilot_suggestion_generation_claims") == []
    assert http.repos.store.list_all("pilot_goal_suggestions") == []


# ---------------------------------------------------------------------------
# 8. STRUCTURAL — lookup, not traversal (mutation O1)
# ---------------------------------------------------------------------------


def _executable_body(name: str) -> str:
    """One method's EXECUTABLE body: no signature, no docstring, no comments.

    All three had to go. The signature carries `->`, the docstring says the
    words "ladder" and "no month comparison", and the comments explain the
    design — so a naive scan of the raw source reports the very prose that
    documents the property as a violation of it.
    """
    import inspect
    import textwrap

    source = inspect.getsource(getattr(StaticRungTableSource, name))
    tree = ast.parse(textwrap.dedent(source))
    function = tree.body[0]
    statements = function.body
    if (statements and isinstance(statements[0], ast.Expr)
            and isinstance(statements[0].value, ast.Constant)
            and isinstance(statements[0].value.value, str)):
        statements = statements[1:]   # drop the docstring
    # Unparsed from the AST, which drops comments entirely.
    return "\n".join(ast.unparse(node) for node in statements)


def test_next_rung_target_contains_no_ladder_traversal():
    """O1. "It is a lookup" is STRUCTURAL and no behavioural test can prove it.

    A correct reimplementation of `_step` inside this module would return the
    same answers for every input, so the mutation sweep found — correctly —
    that no assertion on outputs could catch it. The property being protected
    is that there is no SECOND implementation of the ladder, which means it has
    to be asserted about the code.
    """
    body = _executable_body("next_rung_target")
    for traversal in ("sorted(", "min(", "max(", "ladder", ".keys()",
                      ".values()", ".items()", "for ", ">"):
        assert traversal not in body, traversal
    # The one permitted comparison is the negative-floor guard, not a month
    # ordering over the table.
    assert body.count("<") == 1 and "from_months < 0" in body
    # And the lookup is a direct keyed read.
    assert "self._steps.get(str(from_months))" in body


def test_the_adapter_never_iterates_the_step_table_anywhere():
    """The same property over the whole class, so the logic cannot just move."""
    for method in ("next_rung_target", "rung_for_target"):
        body = _executable_body(method)
        assert "self._steps.items()" not in body, method
        assert "self._steps.values()" not in body, method
        assert "for " not in body, method


def test_a_lying_mappable_flag_is_refused(tmp_path):
    """O7. `mappable: true` on a rung whose family forbids the domain.

    Defence in depth that no honestly generated artifact reaches, so it is
    reachable only by tampering — which is exactly what a hand-edited table is.
    The port's contract is that a rung it RETURNS is usable, so this must refuse
    rather than hand back a rung `allocate_goal` would later reject.
    """
    body = json.loads(DEFAULT_ARTIFACT_PATH.read_text(encoding="utf-8"))
    entry = body["rungs"][EXPECTED_RUNG_REF]
    # A family that permits only an unrelated domain, still flagged mappable.
    entry["family_bindings"] = [{"family_ref": "expressive_vocabulary_growth",
                                 "allowed_domains": ["fine_motor"]}]
    body["artifact_digest"] = GEN.artifact_digest(body)
    path = tmp_path / "lying.json"
    path.write_text(json.dumps(body), encoding="utf-8")

    source = StaticRungTableSource(path)
    with pytest.raises(RungNotMappableError):
        source.rung_for_target(RungTarget(
            domain_key=DOMAIN, source_rung_months=EXPECTED_TARGET_MONTHS,
            milestone_text=EXPECTED_MILESTONE))


def test_a_lying_mappable_flag_is_also_refused_in_the_bulk_read(tmp_path):
    """The same lie through `mappable_rungs_for_domain`, which must not list it."""
    body = json.loads(DEFAULT_ARTIFACT_PATH.read_text(encoding="utf-8"))
    body["rungs"][EXPECTED_RUNG_REF]["family_bindings"] = [
        {"family_ref": "expressive_vocabulary_growth",
         "allowed_domains": ["fine_motor"]}]
    body["artifact_digest"] = GEN.artifact_digest(body)
    path = tmp_path / "lying_bulk.json"
    path.write_text(json.dumps(body), encoding="utf-8")

    source = StaticRungTableSource(path)
    with pytest.raises(RungNotMappableError):
        source.mappable_rungs_for_domain(DOMAIN)
