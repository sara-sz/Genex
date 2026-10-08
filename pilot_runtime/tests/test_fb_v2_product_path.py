"""F-B v2 against the REAL Parent v2 engine and the REAL frozen rung table.

0.6A-1G. The `pilot_backend` suite drives the same service over a fixture rung
source, which keeps that suite dependency-pure. This file is the cross-system
half: a REAL Parent v2 baseline, projected through the REAL canonicalisation
boundary, classified against the REAL frozen artifact.

Neither side can be a fake agreeing with itself here. The milestone wording, the
band rosters, the four-skill 30-month band and which rungs have no reconciled
activity family all come from the frozen source rather than from this file — so
a drift between Parent's declared track and the pilot's table shows up as a
failure instead of being absorbed by a matching fixture.

It writes NOTHING: only `band_views` and `resolve_targets` are exercised, both
of which are pure. Persistence, claims and concurrency belong to the
`pilot_backend` suite and the emulator suite respectively.
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

from api import functional_baseline_v2_api as PARENT_API      # noqa: E402

from pilot_backend.domain.suggestion_generation_v2 import (   # noqa: E402
    BAND_MASTERED,
    BAND_UNRESOLVED,
    OUTCOME_GENERATED,
    OUTCOME_INSUFFICIENT_KNOWN_EVIDENCE,
    OUTCOME_NO_SUPPORTED_TARGET,
    OUTCOME_NO_UNRESOLVED_BAND,
    BandAssessmentIncomplete,
)
from pilot_backend.integration.baseline_skill_projection import (  # noqa: E402
    project_baseline_v2,
)
from pilot_backend.integration.baseline_suggestion_generation_v2 import (  # noqa: E402
    BaselineSuggestionGenerationV2Service,
)
from pilot_runtime.integration.static_rung_source import (    # noqa: E402
    build_static_rung_source,
)

DOMAIN = "talking_and_communicating"
CHILD = "chld_" + "a" * 32
SESSION = "sess-fbv2-product"
DIGEST = "e" * 64

#: Milestone FRAGMENTS, so the tests state clinical intent rather than
#: re-pinning the frozen wording, which is the taxonomy's to own.
BOOK = "name things in a book"
TWO_WORD = "say two or more words together"
VOCAB = "says about 50 words"
PRONOUNS = "I me or we"
WH = "ask who or what or where or why"


@pytest.fixture(scope="module")
def rungs():
    return build_static_rung_source()


@pytest.fixture(scope="module")
def artifact():
    return json.loads(
        (REPO_ROOT / "pilot_runtime/data/rung_table_talking_v1.json")
        .read_text(encoding="utf-8"))


def parent_projection(overrides, *, choice="two_three_words", chrono=30,
                      rung_source):
    """A REAL Parent v2 baseline, driven to completion, then projected."""
    doc = {"owner_uid": "u",
           "brain_state": {"child": {"chronological_months": chrono}}}
    PARENT_API.start(doc, DOMAIN, choice)
    view = PARENT_API.view(doc, DOMAIN)
    asked = []
    while view["current_question"]:
        question = view["current_question"]
        answer = "yes"
        for fragment, value in overrides.items():
            if fragment in question["milestone"]:
                answer = value
        asked.append((question["months"], question["milestone"], answer))
        view = PARENT_API.answer(doc, DOMAIN, question["skill_key"], answer)
    PARENT_API.finalize_baseline(doc, DOMAIN)

    payload = PARENT_API.projection_payload(doc, DOMAIN)
    projection = project_baseline_v2(
        rung_source=rung_source, child_id=CHILD, source_session_id=SESSION,
        source_record_digest=DIGEST, summary=payload["summary"],
        skills=payload["skills"],
        band_totals=[(b["months"], b["total_skills"])
                     for b in payload["band_totals"]])
    return projection, asked


def service(rung_source):
    """Repos and goals are never touched by the pure methods under test."""
    return BaselineSuggestionGenerationV2Service(
        repos=None, goals=None, rung_source=rung_source)


def ref_of(rung_source, asked, fragment, months=30):
    milestone = next(m for mo, m, _a in asked
                     if mo == months and fragment in m)
    return rung_source.canonical_identity(DOMAIN, months, milestone)[0]


# ===========================================================================
# 11. the canonical mixed 30-month case, end to end
# ===========================================================================


def test_the_real_mixed_30m_case_targets_only_book_naming(rungs, artifact):
    """book=ND, two-word=D, vocab=D, pronouns=unknown, on the real ladder."""
    projection, asked = parent_projection(
        {BOOK: "no", PRONOUNS: "not_sure"}, rung_source=rungs)
    svc = service(rungs)

    views = {view.months: view for view in svc.band_views(projection)}
    assert views[24].classification == BAND_MASTERED
    assert views[30].classification == BAND_UNRESOLVED
    # The real 30-month band genuinely holds four declared-track skills.
    assert len(rungs.declared_band_roster(DOMAIN, 30)) == 4
    assert len(views[30].demonstrated_refs) == 2
    assert len(views[30].target_refs) == 1
    assert len(views[30].unknown_refs) == 1

    band, mappable, unsupported = svc.resolve_targets(projection)
    assert band.months == 30
    assert [r.rung_ref for r in mappable] == [ref_of(rungs, asked, BOOK)]
    assert unsupported == ()
    # pronouns is UNKNOWN evidence, never an unsupported target.
    assert band.unknown_refs == (ref_of(rungs, asked, PRONOUNS),)

    # The anchor metadata comes from the frozen table for the ref the EVIDENCE
    # named — not from a rung re-derived from a month.
    rung = mappable[0]
    assert rung.source_rung_months == 30
    assert BOOK in rung.milestone_text
    assert rung.is_activity_mappable
    assert rung.family_bindings
    assert artifact["rungs"][rung.rung_ref]["mappable"] is True


def test_the_real_multi_deficit_case_yields_two_targets_and_one_unsupported(
        rungs, artifact):
    """Item 12 on the real ladder.

    book=ND, two-word=emerging, vocab=D, pronouns=ND -> two anchored targets
    plus `pronouns` as an unsupported canonical target.
    """
    projection, asked = parent_projection(
        {BOOK: "no", TWO_WORD: "sometimes", PRONOUNS: "no"},
        rung_source=rungs)
    svc = service(rungs)

    band, mappable, unsupported = svc.resolve_targets(projection)
    assert band.months == 30
    assert len(band.target_refs) == 3          # three known deficits
    assert len(mappable) == 2                  # two are activity-mappable
    assert set(r.rung_ref for r in mappable) == {
        ref_of(rungs, asked, BOOK), ref_of(rungs, asked, TWO_WORD)}
    assert unsupported == (ref_of(rungs, asked, PRONOUNS),)
    assert band.unknown_refs == ()

    # The test is not vacuous: `pronouns` really has no reconciled family in the
    # frozen artifact, and the two generated ones really do.
    assert artifact["rungs"][unsupported[0]]["mappable"] is False
    for rung in mappable:
        assert artifact["rungs"][rung.rung_ref]["mappable"] is True

    # And the vocabulary skill, which was demonstrated, is in neither list.
    vocab_ref = ref_of(rungs, asked, VOCAB)
    assert vocab_ref not in {r.rung_ref for r in mappable}
    assert vocab_ref not in unsupported
    assert vocab_ref in band.demonstrated_refs


def test_an_unknown_only_real_band_generates_nothing(rungs):
    projection, asked = parent_projection({PRONOUNS: "not_sure"},
                                          rung_source=rungs)
    svc = service(rungs)
    band, mappable, unsupported = svc.resolve_targets(projection)
    assert band.months == 30
    assert band.target_refs == ()
    assert mappable == () and unsupported == ()
    assert band.unknown_refs == (ref_of(rungs, asked, PRONOUNS),)


def test_an_all_unmappable_real_band_yields_only_unsupported_targets(rungs,
                                                                     artifact):
    """`pronouns` is the only deficit, and it has no reconciled family.

    Zero targets, one unsupported ref, and NO step to a higher band — even
    though higher bands on the real ladder contain mappable rungs.
    """
    projection, asked = parent_projection({PRONOUNS: "no"}, rung_source=rungs)
    svc = service(rungs)
    band, mappable, unsupported = svc.resolve_targets(projection)
    assert band.months == 30
    assert band.target_refs == (ref_of(rungs, asked, PRONOUNS),)
    assert mappable == ()
    assert unsupported == (ref_of(rungs, asked, PRONOUNS),)
    assert artifact["rungs"][unsupported[0]]["mappable"] is False

    # Proof the refusal is a decision rather than an absence of options: the
    # real table has mappable rungs above 30 months.
    higher = [ref for ref, entry in artifact["rungs"].items()
              if int(entry["source_rung_months"]) > 30 and entry["mappable"]]
    assert higher, "the artifact has no mappable rung above 30m"
    assert not set(higher) & set(unsupported)


def test_a_fully_mastered_real_assessment_selects_no_band(rungs):
    projection, _asked = parent_projection({}, rung_source=rungs)
    svc = service(rungs)
    for view in svc.band_views(projection):
        assert view.classification == BAND_MASTERED
    band, mappable, unsupported = svc.resolve_targets(projection)
    assert band is None and mappable == () and unsupported == ()


def test_the_other_unmappable_declared_rungs_also_report_as_unsupported(
        rungs, artifact):
    """`wh_question_asking` at 36 months, through the real 36m band."""
    projection, asked = parent_projection(
        {WH: "no"}, choice="short_sentences", chrono=40, rung_source=rungs)
    svc = service(rungs)
    band, mappable, unsupported = svc.resolve_targets(projection)

    wh_ref = ref_of(rungs, asked, WH, months=36)
    assert artifact["rungs"][wh_ref]["mappable"] is False
    if band is not None and band.months == 36:
        assert wh_ref in band.target_refs
        assert wh_ref in unsupported
        assert wh_ref not in {r.rung_ref for r in mappable}
    else:  # pragma: no cover - the traversal entered a lower band first
        pytest.fail(f"expected the 36m band, got {band and band.months}")


# ===========================================================================
# band denominators and completeness, against the real roster
# ===========================================================================


def test_every_real_band_denominator_matches_the_canonical_roster(rungs):
    """F-B v2 re-derives rather than inheriting A2's conclusion."""
    projection, _asked = parent_projection({BOOK: "no"}, rung_source=rungs)
    for band in projection.band_totals:
        roster = rungs.declared_band_roster(DOMAIN, band.months)
        assert band.total_skills == len(roster), band.months
    # And every band therefore classifies without refusing.
    views = service(rungs).band_views(projection)
    assert views and all(view.is_complete for view in views)


def test_a_real_band_short_of_its_roster_refuses(rungs):
    """Fail closed, on the real artifact rather than a fixture.

    One 30-month evidence row is dropped and the declared total is left at the
    TRUE roster size, which isolates the incompleteness guard from the
    denominator guard.
    """
    import dataclasses

    projection, _asked = parent_projection({BOOK: "no"}, rung_source=rungs)
    # Drop exactly one DEMONSTRATED 30-month row, so the band is short of its
    # roster while the deficit that would otherwise be targeted is still there.
    dropped = next(e for e in projection.skill_evidence
                   if e.months == 30 and e.state == "demonstrated")
    kept = tuple(e for e in projection.skill_evidence if e != dropped)
    assert len(kept) == len(projection.skill_evidence) - 1

    short = dataclasses.replace(projection, skill_evidence=kept)
    assert short.assessment_complete(30) is False
    # The band still classifies — as INCOMPLETE — and SELECTION is what refuses.
    views = {v.months: v for v in service(rungs).band_views(short)}
    assert views[30].classification == "incomplete"
    assert views[30].missing_refs == (dropped.rung_ref,)
    with pytest.raises(BandAssessmentIncomplete):
        service(rungs).resolve_targets(short)


# ===========================================================================
# 17. no alphabetical dependency, on the real ladder
# ===========================================================================


def test_the_real_target_is_the_failed_skill_not_the_first_by_wording(rungs):
    """The v1 defect, shown against the real 30-month band.

    All four real 30-month milestones are tried in turn as the sole deficit. In
    every case the target is THAT skill — so the answer tracks the evidence, and
    no single milestone can be the winner by virtue of its wording.
    """
    svc = service(rungs)
    seen = set()
    for fragment in (BOOK, TWO_WORD, VOCAB, PRONOUNS):
        projection, asked = parent_projection({fragment: "no"},
                                              rung_source=rungs)
        band, mappable, unsupported = svc.resolve_targets(projection)
        expected = ref_of(rungs, asked, fragment)
        assert band.target_refs == (expected,), fragment
        # Mappable ones generate; `pronouns` reports as unsupported.
        if mappable:
            assert [r.rung_ref for r in mappable] == [expected], fragment
        else:
            assert unsupported == (expected,), fragment
        seen.add(expected)
    # Four DISTINCT targets for four distinct deficits — not one winner.
    assert len(seen) == 4


def test_the_real_outcome_vocabulary_covers_every_observed_case(rungs):
    """Each of the four outcomes is reachable from a real baseline."""
    cases = {
        OUTCOME_GENERATED: {BOOK: "no"},
        OUTCOME_NO_SUPPORTED_TARGET: {PRONOUNS: "no"},
        OUTCOME_INSUFFICIENT_KNOWN_EVIDENCE: {PRONOUNS: "not_sure"},
        OUTCOME_NO_UNRESOLVED_BAND: {},
    }
    svc = service(rungs)
    for expected, overrides in cases.items():
        projection, _asked = parent_projection(overrides, rung_source=rungs)
        band, mappable, unsupported = svc.resolve_targets(projection)
        if expected == OUTCOME_GENERATED:
            assert mappable, expected
        elif expected == OUTCOME_NO_SUPPORTED_TARGET:
            assert not mappable and unsupported, expected
        elif expected == OUTCOME_INSUFFICIENT_KNOWN_EVIDENCE:
            assert band is not None and not band.target_refs, expected
        else:
            assert band is None, expected
