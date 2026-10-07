"""0.6A-1 — the generated activity bank and the lookup-only browser adapter.

What is proved here, hardest-to-get-wrong first:

1. EXCLUSION. The generic tier-3 placeholder cannot enter the artifact, and the
   generator never even reads the structures that hold it.
2. VALIDATION. Every admitted template passes Parent's OWN
   `validate_activity`; a card that fails is recorded as rejected, not quietly
   dropped. Proven non-vacuous: one real curated card IS rejected.
3. DRIFT. Regenerating from the curated source reproduces the committed
   artifact byte for byte.
4. FAIL CLOSED. `two_word_phrases` has no content, so the bank refuses the
   pilot goal's family pair and `release_ready` is false.
5. CONVERSION. Templates become `CandidateActivity` deterministically.
6. PURITY. The adapter's import graph reaches no Parent module, spreadsheet
   library, model client or validator.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pilot_backend.domain.activity_template import (
    ADMITTED_SOURCE_TIERS,
    CARD_FIELDS,
    FORBIDDEN_SOURCE_TIER,
    ActivityTemplate,
    ActivityTemplateError,
    compute_template_id,
)
from pilot_backend.integration.activity_bank import (
    ActivityBankSource,
    FamilyNotServed,
    candidates_for_goal,
    require_all_families_served,
)
from pilot_runtime.integration import activity_bank_generator as GEN
from pilot_runtime.integration.static_activity_bank import (
    DEFAULT_ARTIFACT_PATH,
    StaticActivityBank,
    StaticActivityBankError,
    build_static_activity_bank,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

def _executable_source(path: Path) -> str:
    """A module's EXECUTABLE code: no docstrings, no comments.

    Required rather than cosmetic. These modules DOCUMENT the names they
    refuse to use — "the activity writer is only reached through
    `_v22_make_activity`, which this generator never calls" — so a raw text
    scan reports the very prose that records the exclusion as a violation of
    it. `ast.unparse` drops comments entirely and docstrings are stripped
    explicitly.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def _strip(node) -> None:
        # EVERY level, not just the module: `ast.unparse` preserves nested
        # docstrings, and the generator documents the refused names inside a
        # FUNCTION docstring.
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                node.body = body[1:]
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Module, ast.FunctionDef,
                                  ast.AsyncFunctionDef, ast.ClassDef)):
                _strip(child)

    _strip(tree)
    return ast.unparse(tree)


VOCAB = "expressive_vocabulary_growth"
SENTENCE = "two_word_phrases"

#: Measured, then pinned. 13 curated cards exist in the `expressive_word`
#: bucket; one ("Action Word Match") is REJECTED by Parent's validator for
#: naming a motor action, leaving 12. Plus 6 founder-approved reviewed cards
#: for `two_word_phrases`.
EXPECTED_VOCAB = 12
EXPECTED_SENTENCE = 6
EXPECTED_BUILDING = 6
EXPECTED_ADMITTED = (EXPECTED_VOCAB + EXPECTED_SENTENCE + EXPECTED_BUILDING
                     + 6)   # + book_object_naming
EXPECTED_REJECTED = 1

#: `sentence_building` is SERVED but deliberately NOT in REQUIRED_FAMILIES:
#: the production resolver never selects it (it loses the 48m alphabetical
#: tiebreak to `function_question_answering`). Serving more than the current
#: goal requires is correct; requiring it would gate release on a target the
#: pilot cannot reach.
BUILDING = "sentence_building"
EXPECTED_BOOK = 6
BOOK = "book_object_naming"
#: Still unserved, and REACHABLE (floor 36 -> 48m), so the fail-closed test
#: below exercises a live gap rather than a hypothetical one.
UNSERVED_REACHABLE = "function_question_answering"


@pytest.fixture(scope="module")
def artifact():
    return json.loads(DEFAULT_ARTIFACT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bank():
    return build_static_activity_bank()


# ---------------------------------------------------------------------------
# 1. EXCLUSION — the placeholder tier cannot get in
# ---------------------------------------------------------------------------


def test_the_generator_never_reads_the_generic_fallback():
    """STRUCTURAL, not behavioural.

    A test that merely checked "no placeholder text in the artifact" would pass
    for an artifact that happened to contain none today. This asserts the
    generator's source names neither the generic builder nor the structures it
    uses, so there is no code path by which tier 3 could be admitted at all.
    """
    body = _executable_source(REPO_ROOT / "pilot_runtime" / "integration" /
                              "activity_bank_generator.py")
    for forbidden in ("_v22_fallback_instructions", "_v22_make_activity",
                      "_v22_call_llm_activity_writer", "_variant_theme",
                      "_bucket_title", "overrides"):
        assert forbidden not in body, forbidden
    # Only the two curated literals are read.
    assert "_FAMILY_VARIANTS" in body
    assert "_BUCKET_VARIANTS" in body


def test_the_forbidden_tier_is_never_admitted(artifact):
    assert artifact["provenance"]["admitted_source_tiers"] == \
        list(ADMITTED_SOURCE_TIERS)
    assert artifact["provenance"]["forbidden_source_tier"] == \
        FORBIDDEN_SOURCE_TIER
    for entry in artifact["templates"].values():
        assert entry["source_tier"] in ADMITTED_SOURCE_TIERS


def test_a_template_with_the_forbidden_tier_cannot_be_constructed():
    card = {f: f"authored {f}" for f in CARD_FIELDS}
    with pytest.raises(ActivityTemplateError):
        ActivityTemplate.build(activity_family_ref=VOCAB,
                               source_tier=FORBIDDEN_SOURCE_TIER,
                               source_pool_ref="generic", card=card)


def test_no_placeholder_wording_reached_the_artifact(artifact):
    """Belt and braces on the structural test above, using the real phrases.

    These are the exact strings Parent's validator flags as
    `placeholder_wording`.
    """
    blob = json.dumps(artifact["templates"]).lower()
    for phrase in ("set up a quick", "show your child one small step",
                   "your child tries at least once", "(from around the home)",
                   "with a sibling or friend, take turns"):
        assert phrase not in blob, phrase


def test_the_artifact_records_that_no_llm_ran(artifact):
    assert artifact["provenance"]["llm_used"] is False
    assert "model" not in json.dumps(artifact["provenance"]).lower()


# ---------------------------------------------------------------------------
# 2. VALIDATION — by Parent's own validator, and non-vacuously
# ---------------------------------------------------------------------------


def test_every_admitted_template_passes_the_parent_validator(artifact):
    """Re-runs the REAL validator over every admitted card.

    The generator already did this; running it again here means a future change
    that loosened the generator's gate would be caught by the suite rather than
    only by the artifact's contents.
    """
    sys.path.insert(0, str(REPO_ROOT / "genex-parent"))
    from genex_core.activity_validator import validate_activity

    for template_id, entry in artifact["templates"].items():
        card = {f: entry[f] for f in CARD_FIELDS}
        assembled = GEN._assembled_for_validation(
            card, entry["activity_family_ref"])
        ok, problems = validate_activity(assembled, GEN.DOMAIN_KEY)
        assert ok, (template_id, problems)


def test_the_validator_gate_actually_rejects_something(artifact):
    """NON-VACUITY. One real curated card is refused, and the reason is kept.

    "Action Word Match" is a legitimate naming activity that merely mentions
    `jump` among four actions to mime, so Parent's motor-game rule flags it.
    Whether that is too strict is a clinical judgement — recorded here, not
    worked around.
    """
    rejected = artifact["rejected_cards"]
    assert len(rejected) == EXPECTED_REJECTED
    assert rejected[0]["reason"] == "validator_rejected"
    assert rejected[0]["family"] == VOCAB
    assert any("motor" in d for d in rejected[0]["detail"])
    assert artifact["population"]["rejected"] == EXPECTED_REJECTED


def test_the_admitted_population_covers_four_families(artifact):
    assert artifact["population"]["templates"] == EXPECTED_ADMITTED
    assert artifact["population"]["by_family"] == {
        BOOK: EXPECTED_BOOK, VOCAB: EXPECTED_VOCAB,
        BUILDING: EXPECTED_BUILDING, SENTENCE: EXPECTED_SENTENCE}
    assert artifact["served_families"] == [BOOK, VOCAB, BUILDING, SENTENCE]


def test_book_object_naming_closes_the_reachable_30m_gap(artifact):
    """The resolver selects this family from a 24m floor, so it is the one
    content gap that actually blocked a second pilot month."""
    entries = [e for e in artifact["templates"].values()
               if e["activity_family_ref"] == BOOK]
    assert len(entries) == EXPECTED_BOOK
    for entry in entries:
        assert entry["source_pool_ref"] == f"reviewed_cards/{BOOK}.json"
    # NOT the 12 vocabulary cards, even though its bucket is `expressive_word`.
    assert all(e["source_tier"] == "family_curated" for e in entries)


def test_sentence_building_is_served_but_not_required(artifact):
    """Served > required, and that asymmetry is deliberate.

    `sentence_building` has six reviewed cards, but the production resolver
    never reaches it, so gating `release_ready` on it would block a release for
    a target no baseline can produce.
    """
    assert BUILDING in artifact["served_families"]
    assert BUILDING not in artifact["required_families"]
    assert artifact["release_ready"] is True


def test_every_template_id_recomputes(artifact):
    for template_id, entry in artifact["templates"].items():
        card = {f: entry[f] for f in CARD_FIELDS}
        assert compute_template_id(
            entry["activity_family_ref"], card) == template_id


def test_editing_a_card_changes_its_identity():
    """Content-addressing, stated as a test: an edit is a NEW template."""
    card = {f: f"authored {f}" for f in CARD_FIELDS}
    first = compute_template_id(VOCAB, card)
    edited = dict(card, instructions="authored instructions, but slower")
    assert compute_template_id(VOCAB, edited) != first
    # And the family participates, so the same card under another family is a
    # different template.
    assert compute_template_id(SENTENCE, card) != first


# ---------------------------------------------------------------------------
# 3. DRIFT
# ---------------------------------------------------------------------------


def test_regenerating_reproduces_the_committed_artifact():
    regenerated = GEN.build_artifact()
    committed = json.loads(DEFAULT_ARTIFACT_PATH.read_text(encoding="utf-8"))
    assert GEN.canonical_json(regenerated) == GEN.canonical_json(committed)
    assert regenerated["artifact_digest"] == committed["artifact_digest"]


def test_the_committed_file_is_byte_identical_to_its_renderer():
    assert GEN.render(GEN.build_artifact()) == \
        DEFAULT_ARTIFACT_PATH.read_text(encoding="utf-8")


def test_hand_editing_an_instruction_is_refused(tmp_path):
    body = json.loads(DEFAULT_ARTIFACT_PATH.read_text(encoding="utf-8"))
    victim = sorted(body["templates"])[0]
    body["templates"][victim]["instructions"] = "Do whatever you like."
    body["artifact_digest"] = GEN.artifact_digest(body)
    path = tmp_path / "edited.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(StaticActivityBankError):
        StaticActivityBank(path)


def test_an_unsupported_schema_is_refused(tmp_path):
    body = json.loads(DEFAULT_ARTIFACT_PATH.read_text(encoding="utf-8"))
    body["artifact_schema_version"] = "pilot-activity-bank-v2"
    body["artifact_digest"] = GEN.artifact_digest(body)
    path = tmp_path / "v2.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(StaticActivityBankError):
        StaticActivityBank(path)


def test_a_missing_artifact_raises_rather_than_degrading(tmp_path):
    with pytest.raises(StaticActivityBankError):
        StaticActivityBank(tmp_path / "absent.json")


def test_the_artifact_records_both_source_shas(artifact):
    import hashlib

    root = REPO_ROOT / "genex-parent"
    prov = artifact["provenance"]
    for relpath_key, sha_key in (
            ("activity_engine_relpath", "activity_engine_sha256"),
            ("taxonomy_relpath", "taxonomy_sha256")):
        path = root / prov[relpath_key]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == \
            prov[sha_key], relpath_key
    assert prov["taxonomy_version"] == "activity_family_taxonomy_v1"


# ---------------------------------------------------------------------------
# 4. FAIL CLOSED on the unserved family
# ---------------------------------------------------------------------------


def test_both_required_families_are_now_served(artifact):
    """The release gate OPENED when reviewed content arrived — not before."""
    assert set(artifact["required_families"]) == {VOCAB, SENTENCE}
    assert artifact["unserved_required_families"] == []
    assert artifact["release_ready"] is True
    # The bucket resolution is still recorded, and still shows that
    # `two_word_phrases` has no curated BUCKET pool: its cards come from the
    # reviewed-card file, not from `_BUCKET_VARIANTS["sentence"]`.
    assert artifact["family_bucket_resolution"][SENTENCE] == "sentence"
    assert artifact["family_bucket_resolution"][VOCAB] == "expressive_word"


def test_the_sentence_cards_come_from_the_reviewed_file_not_a_bucket(artifact):
    """Provenance per card, so the content's origin is auditable."""
    for family, expected_titles in (
            (SENTENCE, ["Action + Object", "All Done and Stop", "Help Me",
                        "More Bubbles", "My Turn", "Want + Choice"]),
            (BUILDING, ["Can I Play?", "Help Me Open", "I Need a Break",
                        "I Want Bubbles", "My Turn Please",
                        "Who Is Doing What"]),
            (BOOK, ["Find and Name", "Lift the Flap, Name It",
                    "Photo Album Names", "Point and Tell",
                    "Same Book, Every Day", "What's on This Page?"])):
        entries = [e for e in artifact["templates"].values()
                   if e["activity_family_ref"] == family]
        assert len(entries) == 6, family
        for entry in entries:
            assert entry["source_tier"] == "family_curated"
            assert entry["source_pool_ref"] == \
                f"reviewed_cards/{family}.json"
        assert sorted(e["title"] for e in entries) == expected_titles


def test_the_reviewed_file_sha_is_recorded_in_provenance(artifact):
    """The durable source is identified by digest, not merely named."""
    import hashlib

    expected = {}
    for family in (SENTENCE, BUILDING, BOOK):
        path = REPO_ROOT / GEN.REVIEWED_CARDS_RELPATH / f"{family}.json"
        assert path.is_file(), family
        expected[family] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert artifact["provenance"]["reviewed_card_files"] == expected


def test_the_bank_now_serves_the_pilot_goals_family_pair(bank):
    """The live case, now satisfied: the 24m goal binds BOTH families.

    This test previously asserted a refusal. It is kept as the positive
    statement of the same invariant — the pair is servable only because both
    families have reviewed, validator-passing content — and the refusal path
    is still covered below against a family that genuinely has none.
    """
    templates = bank.templates_for_families([VOCAB, SENTENCE])
    assert len(templates) == EXPECTED_VOCAB + EXPECTED_SENTENCE
    assert {t.activity_family_ref for t in templates} == {VOCAB, SENTENCE}
    require_all_families_served(bank, [VOCAB, SENTENCE])


def test_the_release_gate_opens(bank):
    assert bank.release_ready is True
    assert bank.unserved_required_families == ()
    bank.assert_release_ready()   # must not raise


def test_an_unserved_family_is_still_refused(bank):
    """FAIL-CLOSED is retained, against families the taxonomy does define but
    for which no reviewed content exists — e.g. the 30m/36m targets.

    `pronouns` and `wh_question_asking` are not even taxonomy families, so
    `function_question_answering` is used: it IS canonical, permits the domain,
    has no admitted cards — and the resolver selects it from a 36m floor, so
    this is a live gap rather than a hypothetical one.
    """
    with pytest.raises(FamilyNotServed) as caught:
        bank.templates_for_families([VOCAB, UNSERVED_REACHABLE])
    assert UNSERVED_REACHABLE in str(caught.value)
    assert VOCAB not in str(caught.value)
    with pytest.raises(FamilyNotServed):
        require_all_families_served(bank, [VOCAB, UNSERVED_REACHABLE])


def test_each_served_family_alone_works(bank):
    vocab = bank.templates_for_families([VOCAB])
    assert len(vocab) == EXPECTED_VOCAB
    assert {t.activity_family_ref for t in vocab} == {VOCAB}
    sentence = bank.templates_for_families([SENTENCE])
    assert len(sentence) == EXPECTED_SENTENCE
    assert {t.activity_family_ref for t in sentence} == {SENTENCE}


def test_an_unknown_family_is_refused(bank):
    with pytest.raises(FamilyNotServed):
        bank.templates_for_families(["not_a_real_family"])
    with pytest.raises(FamilyNotServed):
        bank.templates_for_families([])


def test_the_bank_satisfies_the_port(bank):
    assert isinstance(bank, ActivityBankSource)


# ---------------------------------------------------------------------------
# 5. CONVERSION to CandidateActivity
# ---------------------------------------------------------------------------


def test_templates_convert_to_candidate_activities(bank):
    from pilot_backend.domain.goals import GoalKind, GoalRef

    ref = GoalRef(GoalKind.CLINICAL, "clgl_19aed3e4a5814e9b973c8eb352d79e49")
    candidates = candidates_for_goal(
        bank.templates_for_families([VOCAB, SENTENCE]), ref)

    assert len(candidates) == EXPECTED_VOCAB + EXPECTED_SENTENCE
    # BOTH of the goal's canonical families are represented, which is the
    # clinical point: the 24m target is two-word combination.
    assert {c.activity_family_ref for c in candidates} == {VOCAB, SENTENCE}
    for candidate in candidates:
        assert candidate.supports == (ref,)
        assert candidate.primary_for == ref
        assert candidate.activity_family_ref in (VOCAB, SENTENCE)
        assert candidate.activity_identity_ref.startswith("atpl1:")
        # Not derived from the source: the curated cards have no difficulty.
        assert candidate.difficulty_tier == 1
        assert candidate.milestone_refs == ()


def test_candidate_order_is_deterministic_and_content_derived(bank):
    from pilot_backend.domain.goals import GoalKind, GoalRef

    ref = GoalRef(GoalKind.CLINICAL, "clgl_x")
    templates = list(bank.templates_for_families([VOCAB]))
    forward = [c.activity_identity_ref
               for c in candidates_for_goal(templates, ref)]
    reversed_in = [c.activity_identity_ref
                   for c in candidates_for_goal(list(reversed(templates)), ref)]
    assert forward == reversed_in
    assert forward == sorted(forward)


def test_conversion_refuses_a_goalless_candidate():
    """The allocator's own rule, reached through conversion.

    `CandidateActivity` refuses a candidate supporting no goal, so conversion
    cannot produce an activity that serves nothing.
    """
    from pilot_backend.weekly.allocator import AllocationError, CandidateActivity

    with pytest.raises(AllocationError):
        CandidateActivity(activity_identity_ref="atpl1:x", supports=())


# ---------------------------------------------------------------------------
# 6. PURITY — what the browser adapter may not reach
# ---------------------------------------------------------------------------


FORBIDDEN_IMPORTS = ("pandas", "openpyxl", "xlrd", "genex_core",
                     "parent_taxonomy", "openai", "anthropic")


def test_the_adapter_imports_nothing_forbidden():
    """A FRESH interpreter, so another test's import cannot hide one."""
    code = (
        "import sys, json\n"
        "import pilot_runtime.integration.static_activity_bank as M\n"
        "M.build_static_activity_bank()\n"
        "print(json.dumps(sorted(\n"
        "    m for m in sys.modules\n"
        f"    if m.split('.')[0] in {list(FORBIDDEN_IMPORTS)!r})))\n"
    )
    result = subprocess.run([sys.executable, "-c", code],
                            capture_output=True, text=True, cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == []


def test_the_adapter_names_no_forbidden_module_and_no_validator():
    body = _executable_source(REPO_ROOT / "pilot_runtime" / "integration" /
                              "static_activity_bank.py")
    for forbidden in FORBIDDEN_IMPORTS:
        assert f"import {forbidden}" not in body, forbidden
    assert ".xlsx" not in body
    # Validation is build-time ONLY.
    assert "validate_activity" not in body
    assert "activity_validator" not in body


def test_the_adapter_has_no_write_operation():
    body = _executable_source(REPO_ROOT / "pilot_runtime" / "integration" /
                              "static_activity_bank.py")
    for mutating in ("write_text", "write_bytes", "unlink", "mkdir"):
        assert mutating not in body, mutating


# ---------------------------------------------------------------------------
# 7. THE ACTUAL RESOLVER MATRIX — what generation would really attempt
# ---------------------------------------------------------------------------


def test_the_production_resolver_matrix_is_pinned():
    """What `POST .../goal-suggestions/generate` resolves for each floor.

    Pinned because the earlier audit listed every milestone at each month,
    which is NOT the same question. The production resolver picks exactly one
    rung per target month, and the choice is surprising: `sentence_building` is
    never selected at all, and a 30m floor fails closed entirely.

    If any of these change, the pilot's reachable targets changed and the
    coverage matrix must be re-derived.
    """
    from pilot_runtime.integration.static_rung_source import (
        build_static_rung_source,
    )

    rungs = build_static_rung_source()
    matrix = {}
    for floor in (18, 24, 30, 36, 48):
        target = rungs.next_rung_target(GEN.DOMAIN_KEY, floor)
        try:
            rung = rungs.rung_for_target(target)
            matrix[floor] = (target.source_rung_months,
                             tuple(rung.activity_family_refs))
        except Exception as exc:          # noqa: BLE001 - recorded, not raised
            matrix[floor] = (target.source_rung_months, type(exc).__name__)

    assert matrix == {
        18: (24, ("expressive_vocabulary_growth", "two_word_phrases")),
        24: (30, ("book_object_naming",)),
        30: (36, "RungNotMappableError"),
        36: (48, ("function_question_answering",)),
        48: (60, ("narration_storytelling",)),
    }, matrix


def test_sentence_building_is_never_the_resolved_target():
    """The uncomfortable fact, pinned so it cannot be forgotten.

    `sentence_building` has six reviewed cards but loses the 48m tiebreak to
    `function_question_answering`, so no observed floor reaches it. The cards
    are correct content for a target the deterministic resolver cannot select.
    """
    from pilot_runtime.integration.static_rung_source import (
        build_static_rung_source,
    )

    rungs = build_static_rung_source()
    reached = set()
    for floor in range(0, 61):
        target = rungs.next_rung_target(GEN.DOMAIN_KEY, floor)
        if target is None:
            continue
        try:
            reached.update(rungs.rung_for_target(target).activity_family_refs)
        except Exception:                 # noqa: BLE001 - unmappable target
            continue
    assert BUILDING not in reached
    # And the families that ARE reachable, for the record.
    # The complete reachable set across the whole ladder, measured not assumed.
    # 7 families; `sentence_building` is absent and `action_picture_labeling`,
    # `pronouns`, `wh_question_asking` and `expressive_name_response` all lose
    # or are unmapped at 36m.
    assert reached == {
        "early_vocalizations", "expressive_first_words",
        "expressive_vocabulary_growth", "two_word_phrases",
        "book_object_naming", "function_question_answering",
        "narration_storytelling",
    }, sorted(reached)
    assert "action_picture_labeling" not in reached


# ---------------------------------------------------------------------------
# 8. THE BUCKET-REUSE GUARD — a new family cannot inherit vocabulary cards
# ---------------------------------------------------------------------------


def test_only_the_approved_family_reuses_a_bucket(artifact):
    """The allowlist is the ONLY route to bucket-derived cards."""
    assert GEN.BUCKET_REUSE_APPROVED == {
        VOCAB: "expressive_word"}, GEN.BUCKET_REUSE_APPROVED
    assert artifact["provenance"]["bucket_reuse_approved"] == {
        VOCAB: "expressive_word"}
    for template_id, entry in artifact["templates"].items():
        if entry["source_tier"] == "bucket_curated":
            assert entry["activity_family_ref"] == VOCAB, template_id
            assert entry["source_pool_ref"] == \
                "_BUCKET_VARIANTS[expressive_word]", template_id


def test_served_families_other_than_vocabulary_use_only_reviewed_cards(artifact):
    """`book_object_naming` resolves to `expressive_word` too, and is served —
    but every one of its cards comes from its reviewed file, not the bucket."""
    for family in (SENTENCE, BUILDING, BOOK):
        for entry in artifact["templates"].values():
            if entry["activity_family_ref"] == family:
                assert entry["source_tier"] == "family_curated", family
                assert "reviewed_cards/" in entry["source_pool_ref"], family


def test_a_newly_required_family_inherits_nothing(tmp_path):
    """THE GUARD'S REASON FOR EXISTING.

    Simulates the future taxonomy expansion: a clinically distinct family that
    resolves to the broad `expressive_word` bucket is declared REQUIRED, with no
    reviewed cards. Before the guard, being required was enough to hand it the
    12 vocabulary cards. It must now receive ZERO and leave the release closed.
    """
    import sys as _sys

    _sys.path.insert(0, str(REPO_ROOT / "genex-parent"))
    from genex_core import activity_engine as AE

    newcomer = "narration_storytelling"          # a real taxonomy family
    assert AE._family_bucket(newcomer, GEN.DOMAIN_KEY) == "expressive_word"
    assert newcomer not in GEN.BUCKET_REUSE_APPROVED
    assert len(AE._BUCKET_VARIANTS["expressive_word"]) == 13   # the temptation

    original = GEN.REQUIRED_FAMILIES
    GEN.REQUIRED_FAMILIES = original + (newcomer,)
    try:
        body = GEN.build_artifact()
    finally:
        GEN.REQUIRED_FAMILIES = original

    assert body["population"]["by_family"].get(newcomer, 0) == 0
    assert newcomer not in body["served_families"]
    assert body["unserved_required_families"] == [newcomer]
    assert body["release_ready"] is False
    # And the artifact still records what the resolver WOULD have given it, so
    # the near-miss is visible to a reviewer.
    assert body["family_bucket_resolution"][newcomer] == "expressive_word"


def test_a_stale_bucket_approval_fails_the_build():
    """An approval is for a specific pool, not for whatever the regex says.

    If Parent's resolver ever moves an approved family to a different bucket,
    following the approval would silently serve a different pool of cards. The
    build refuses instead.
    """
    original = dict(GEN.BUCKET_REUSE_APPROVED)
    GEN.BUCKET_REUSE_APPROVED[VOCAB] = "social_turn"
    try:
        with pytest.raises(GEN.ActivityBankBuildError) as caught:
            GEN.build_artifact()
        assert "social_turn" in str(caught.value)
    finally:
        GEN.BUCKET_REUSE_APPROVED.clear()
        GEN.BUCKET_REUSE_APPROVED.update(original)
