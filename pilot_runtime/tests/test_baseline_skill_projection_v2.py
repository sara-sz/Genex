"""0.6A-1E — A2 v2 skill-evidence projection, end to end from a real baseline.

Lives in `pilot_runtime` rather than `pilot_backend` because it drives a REAL
Parent v2 baseline through the REAL static rung source. The point is that the
two sides agree on identity, which cannot be shown with a fake on either end.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "genex-parent"))
sys.modules.setdefault("openai", None)
os.environ.pop("OPENAI_API_KEY", None)

from genex_core import functional_baseline as v1              # noqa: E402
from genex_core import functional_baseline_v2 as PV2          # noqa: E402

from pilot_backend.domain.parent_baseline_projection import (  # noqa: E402
    ParentBaselineProjection,
)
from pilot_backend.domain.parent_baseline_projection_v2 import (  # noqa: E402
    FORBIDDEN_FIELDS,
    PROJECTION_SCHEMA_V2,
    ParentBaselineProjectionV2,
    ProjectedBandTotal,
    ProjectedSkillEvidence,
    ProjectionV2Error,
    legacy_projection_has_skill_evidence,
    projection_v2_id_for,
)
from pilot_backend.integration.baseline_skill_projection import (  # noqa: E402
    SkillCanonicalisationError,
    canonicalise_skills,
    project_baseline_v2,
)
from pilot_runtime.integration.static_rung_source import (  # noqa: E402
    build_static_rung_source,
)

DOMAIN = "talking_and_communicating"
CHILD = "chld_16a0d958bedd43b6b073ce3cc393a8f1"
SESSION = "sess-fictional-v2"
DIGEST = "a" * 64

PRONOUNS = "says words like I me or we"
BOOK = "name things in a book"
VOCAB_50 = "says about 50 words"
WH = "ask who or what or where or why"


@pytest.fixture(scope="module")
def rungs():
    return build_static_rung_source()


def parent_baseline(overrides=None, choice="two_three_words", chrono=30):
    """A REAL Parent v2 baseline, driven to completion."""
    record = PV2.start_baseline_v2("talking", choice,
                                   chronological_months=chrono)
    question = PV2.next_question_v2(record)
    while question is not None:
        answer = "yes"
        for fragment, value in (overrides or {}).items():
            if fragment in question["milestone"]:
                answer = value
        record = PV2.record_answer_v2(record, question, answer)
        question = PV2.next_question_v2(record)
    return PV2.finalize_v2(record)


def summary_of(record):
    return {
        "domain": record.domain,
        "area_id": record.area_id,
        "entry_choice_id": record.entry_choice_id,
        "routing_anchor_months": record.routing_anchor_months,
        "not_demonstrated_months": record.not_demonstrated_months,
        "status": record.status,
        "baseline_version": record.baseline_version,
    }


def band_totals_of(record):
    return [(m, PV2.band_assessment(record, m).total_skills)
            for m in record.bands_entered]


def project(record, rungs, **kw):
    return project_baseline_v2(
        rung_source=rungs, child_id=kw.get("child_id", CHILD),
        source_session_id=kw.get("session", SESSION),
        source_record_digest=kw.get("digest", DIGEST),
        summary=kw.get("summary", summary_of(record)),
        skills=kw.get("skills", list(record.skills.values())),
        band_totals=kw.get("band_totals", band_totals_of(record)))


# ---------------------------------------------------------------------------
# Complete and mixed 30m bands survive projection distinctly
# ---------------------------------------------------------------------------


def test_a_complete_30m_band_projects_four_distinct_skills(rungs):
    record = parent_baseline()
    projection = project(record, rungs)

    at_30 = projection.assessed_in_band(30)
    assert len(at_30) == 4
    assert len({e.rung_ref for e in at_30}) == 4          # distinct identities
    assert projection.assessment_complete(30) is True
    assert projection.band_mastered(30) is True


def test_a_mixed_30m_band_preserves_every_state_independently(rungs):
    """The founder's exact example. No state overwrites another."""
    record = parent_baseline({BOOK: "no", PRONOUNS: "not_sure"})
    projection = project(record, rungs)

    at_30 = {e.rung_ref: e.state for e in projection.assessed_in_band(30)}
    assert len(at_30) == 4
    assert sorted(at_30.values()) == ["demonstrated", "demonstrated",
                                      "not_demonstrated", "unknown"]
    assert projection.assessment_complete(30) is True
    assert projection.band_mastered(30) is False

    # The unknown does not suppress the known deficit.
    unresolved = projection.unresolved_skills()
    assert len(unresolved) == 1
    assert unresolved[0].state == "not_demonstrated"


def test_same_month_skills_resolve_to_distinct_canonical_identities(rungs):
    record = parent_baseline()
    refs = [e.rung_ref for e in project(record, rungs).assessed_in_band(30)]
    assert len(refs) == len(set(refs)) == 4
    for ref in refs:
        assert ref.startswith("rung1:")


# ---------------------------------------------------------------------------
# Canonicalisable is not activity-mappable
# ---------------------------------------------------------------------------


def test_unmapped_activity_families_still_project(rungs):
    """`pronouns` has NO canonical activity family and must still survive.

    Baseline evidence is about the child; activity mappability is about our
    content. Dropping the skill would make the projection quietly smaller than
    the assessment.
    """
    record = parent_baseline({PRONOUNS: "no"})
    projection = project(record, rungs)

    pronoun_ref, _ = rungs.canonical_identity(DOMAIN, 30, PRONOUNS)
    states = {e.rung_ref: e.state for e in projection.skill_evidence}
    assert pronoun_ref in states
    assert states[pronoun_ref] == "not_demonstrated"

    # And the rung is genuinely NOT activity-mappable, so the test is not
    # vacuous.
    artifact = json.loads(
        (REPO_ROOT / "pilot_runtime/data/rung_table_talking_v1.json")
        .read_text(encoding="utf-8"))
    assert artifact["rungs"][pronoun_ref]["mappable"] is False


def test_wh_asking_and_own_name_also_project(rungs):
    """Both 36m unmapped skills, carried as evidence."""
    record = parent_baseline({WH: "no"}, choice="short_sentences", chrono=40)
    projection = project(record, rungs)
    wh_ref, _ = rungs.canonical_identity(
        DOMAIN, 36,
        "ask who or what or where or why questions like where is mommy "
        "or where is daddy")
    own_ref, _ = rungs.canonical_identity(DOMAIN, 36,
                                          "says first name when asked")
    refs = {e.rung_ref for e in projection.skill_evidence}
    assert wh_ref in refs
    assert own_ref in refs


# ---------------------------------------------------------------------------
# Minimum necessary — no raw Parent leakage
# ---------------------------------------------------------------------------


def test_the_projection_carries_no_forbidden_parent_content(rungs):
    import dataclasses

    record = parent_baseline({PRONOUNS: "no"})
    projection = project(record, rungs)

    evidence_fields = {f.name for f in
                       dataclasses.fields(ProjectedSkillEvidence)}
    assert evidence_fields == {"rung_ref", "months", "state"}

    blob = json.dumps({
        "projection": {f.name: str(getattr(projection, f.name))
                       for f in dataclasses.fields(projection)},
    }).lower()
    for forbidden in ("asked", "diagnosis", "concern", "qna", "parent_uid",
                      "chronological", "answer", "question_id", "skill_key"):
        assert forbidden not in blob, forbidden
    # The milestone PROSE is consumed at the boundary and never forwarded.
    assert "says words like i me or we" not in blob
    assert "name things in a book" not in blob


def test_a_forbidden_summary_field_is_refused(rungs):
    record = parent_baseline()
    bad = dict(summary_of(record), asked=[{"months": 30}])
    with pytest.raises(ProjectionV2Error):
        project(record, rungs, summary=bad)


def test_every_v2_forbidden_field_is_rejected_by_name(rungs):
    record = parent_baseline()
    for forbidden in FORBIDDEN_FIELDS:
        bad = dict(summary_of(record))
        bad[forbidden] = "x"
        with pytest.raises(ProjectionV2Error):
            project(record, rungs, summary=bad)


def test_an_incomplete_summary_is_refused(rungs):
    record = parent_baseline()
    partial = dict(summary_of(record))
    partial.pop("status")
    with pytest.raises(ProjectionV2Error):
        project(record, rungs, summary=partial)


# ---------------------------------------------------------------------------
# Band completeness is DERIVED against Parent's denominator
# ---------------------------------------------------------------------------


def test_completeness_is_derived_from_parents_declared_total(rungs):
    """And a short batch reads as INCOMPLETE, never as mastery."""
    record = parent_baseline()
    skills = list(record.skills.values())
    dropped = [s for s in skills if PRONOUNS not in s.milestone]
    assert len(dropped) == len(skills) - 1

    projection = project(record, rungs, skills=dropped)
    assert len(projection.assessed_in_band(30)) == 3
    assert projection.assessment_complete(30) is False   # 3 of Parent's 4
    assert projection.band_mastered(30) is False


def test_more_skills_than_parent_declared_is_refused(rungs):
    record = parent_baseline()
    with pytest.raises(ProjectionV2Error):
        project(record, rungs, band_totals=[(m, 1) for m in record.bands_entered])


def test_evidence_for_an_undeclared_band_is_refused(rungs):
    record = parent_baseline()
    totals = [(m, n) for m, n in band_totals_of(record) if m != 30]
    with pytest.raises(SkillCanonicalisationError):
        project(record, rungs, band_totals=totals)


# ---------------------------------------------------------------------------
# Fail-closed canonicalisation
# ---------------------------------------------------------------------------


def test_a_skill_with_no_canonical_match_refuses_the_whole_projection(rungs):
    record = parent_baseline()
    skills = list(record.skills.values())
    import dataclasses
    skills[0] = dataclasses.replace(skills[0],
                                    milestone="a milestone nobody authored")
    with pytest.raises(SkillCanonicalisationError):
        project(record, rungs, skills=skills)


def test_a_mismatched_subdomain_fails_closed(rungs):
    import dataclasses

    record = parent_baseline()
    skills = list(record.skills.values())
    skills[0] = dataclasses.replace(skills[0], subdomain="receptive_language")
    with pytest.raises(SkillCanonicalisationError) as caught:
        project(record, rungs, skills=skills)
    assert "subdomain" in str(caught.value)


def test_a_mismatched_month_fails_closed(rungs):
    import dataclasses

    record = parent_baseline()
    skills = list(record.skills.values())
    skills[0] = dataclasses.replace(skills[0], months=99)
    with pytest.raises(SkillCanonicalisationError):
        project(record, rungs, skills=skills)


def test_two_skills_resolving_to_one_rung_is_refused(rungs):
    record = parent_baseline()
    skills = list(record.skills.values())
    duplicate = skills[0]
    with pytest.raises(SkillCanonicalisationError) as caught:
        canonicalise_skills(rungs, [duplicate, duplicate])
    assert "one canonical rung" in str(caught.value)


def test_an_empty_skill_batch_is_refused(rungs):
    with pytest.raises(SkillCanonicalisationError):
        canonicalise_skills(rungs, [])


def test_a_v1_baseline_cannot_produce_a_v2_projection(rungs):
    record = parent_baseline()
    bad = dict(summary_of(record),
               baseline_version="parent-2.4-functional-baseline-v1")
    with pytest.raises(SkillCanonicalisationError):
        project(record, rungs, summary=bad)


def test_unassessed_is_not_a_projectable_state():
    with pytest.raises(ProjectionV2Error):
        ProjectedSkillEvidence(rung_ref="rung1:" + "0" * 32, months=30,
                               state="unassessed")


def test_a_parent_side_identity_cannot_pose_as_a_canonical_ref():
    with pytest.raises(ProjectionV2Error):
        ProjectedSkillEvidence(rung_ref="talking\x1f30\x1fsomething",
                               months=30, state="demonstrated")


# ---------------------------------------------------------------------------
# Determinism, replay, immutability
# ---------------------------------------------------------------------------


def test_the_same_baseline_projects_to_the_same_id(rungs):
    record = parent_baseline()
    first = project(record, rungs)
    second = project(record, rungs)
    assert first.projection_id == second.projection_id
    assert first.skill_evidence == second.skill_evidence
    assert first.band_totals == second.band_totals


def test_changed_evidence_yields_a_different_document(rungs):
    """A changed baseline must NOT silently overwrite the existing projection."""
    unchanged = project(parent_baseline(), rungs, digest="a" * 64)
    changed = project(parent_baseline({PRONOUNS: "no"}), rungs,
                      digest="b" * 64)
    assert unchanged.projection_id != changed.projection_id


def test_the_projection_id_is_recomputed_and_compared(rungs):
    import dataclasses

    record = parent_baseline()
    projection = project(record, rungs)
    with pytest.raises(ProjectionV2Error):
        dataclasses.replace(projection, projection_id="pbp2_" + "0" * 32)


def test_the_id_prefix_distinguishes_v2_from_v1(rungs):
    record = parent_baseline()
    projection = project(record, rungs)
    assert projection.projection_id.startswith("pbp2_")
    assert projection.projection_schema == PROJECTION_SCHEMA_V2
    # v1's deterministic id for the same source is a DIFFERENT document.
    from pilot_backend.domain.parent_baseline_projection import (
        projection_id_for,
    )
    assert projection_id_for(SESSION, DOMAIN, DIGEST) != projection.projection_id


def test_the_projection_is_frozen(rungs):
    import dataclasses

    projection = project(parent_baseline(), rungs)
    assert dataclasses.is_dataclass(projection)
    with pytest.raises(dataclasses.FrozenInstanceError):
        projection.status = "CHANGED"


# ---------------------------------------------------------------------------
# v1 isolation
# ---------------------------------------------------------------------------


def test_v1_projections_remain_constructible_and_unchanged():
    """v1's seven-field wire contract is untouched."""
    from pilot_backend.domain.parent_baseline_projection import (
        PROJECTION_FIELDS,
        ParentBaselineProjection as V1,
    )

    assert PROJECTION_FIELDS == (
        "domain", "area_id", "entry_choice_id", "routing_anchor_months",
        "not_demonstrated_months", "status", "baseline_version")
    legacy = V1.build(
        child_id=CHILD, source_session_id="sess-v1",
        source_record_digest="c" * 64,
        projection={"domain": DOMAIN, "area_id": "talking",
                    "entry_choice_id": "many_single_words",
                    "routing_anchor_months": 18,
                    "not_demonstrated_months": 24, "status": "BOUNDED",
                    "baseline_version": "parent-2.4-functional-baseline-v1"})
    assert legacy.projection_id.startswith("pbpj_")


def test_a_v1_projection_never_has_skill_evidence():
    from pilot_backend.domain.parent_baseline_projection import (
        ParentBaselineProjection as V1,
    )

    legacy = V1.build(
        child_id=CHILD, source_session_id="sess-v1",
        source_record_digest="c" * 64,
        projection={"domain": DOMAIN, "area_id": "talking",
                    "entry_choice_id": "many_single_words",
                    "routing_anchor_months": 18,
                    "not_demonstrated_months": 24, "status": "BOUNDED",
                    "baseline_version": "parent-2.4-functional-baseline-v1"})
    assert legacy_projection_has_skill_evidence(legacy) is False
    assert not hasattr(legacy, "skill_evidence")


def test_the_legacy_helper_refuses_a_v2_projection(rungs):
    projection = project(parent_baseline(), rungs)
    with pytest.raises(ProjectionV2Error):
        legacy_projection_has_skill_evidence(projection)
