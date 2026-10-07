"""Parent functional baseline v2 — band-complete, skill-level assessment.

The governing rule under test: a month is a routing BAND, not a developmental
age, and skills inside a band are independent. Demonstrating one skill must
never carry its siblings.

v1's defect, for contrast and measured in `test_v1_retires_a_whole_band`: it
keys state on MONTHS and caps a whole domain at four questions, so one answer
retires a band.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.modules.setdefault("openai", None)
os.environ.pop("OPENAI_API_KEY", None)

from genex_core import functional_baseline as v1          # noqa: E402
from genex_core import functional_baseline_v2 as V2        # noqa: E402

AREA = "talking"
DOMAIN = "talking_and_communicating"

PRONOUNS = "says words like I me or we"
VOCAB_50 = "says about 50 words"
BOOK_NAMING = "name things in a book"
TWO_WORD_ACTION = "say two or more words together with one action"
WH_ASKING = "ask who or what or where or why"
OWN_NAME = "says first name when asked"
ACTION_PICTURE = "says what action is happening"


def run(choice, chrono, overrides=None, default="yes"):
    """Drive a full v2 baseline. `overrides` maps a milestone fragment -> answer."""
    record = V2.start_baseline_v2(AREA, choice, chronological_months=chrono)
    asked = []
    question = V2.next_question_v2(record)
    while question is not None:
        answer = default
        for fragment, value in (overrides or {}).items():
            if fragment in question["milestone"]:
                answer = value
        asked.append((question["months"], question["milestone"]))
        record = V2.record_answer_v2(record, question, answer)
        question = V2.next_question_v2(record)
    return V2.finalize_v2(record), asked


# ---------------------------------------------------------------------------
# Same-band independence — the core correction
# ---------------------------------------------------------------------------


def test_the_30m_band_has_four_independent_skills():
    """Precondition. Without four skills the tests below prove nothing."""
    record = V2.start_baseline_v2(AREA, "two_three_words", chronological_months=30)
    rows = V2.band_skill_rows(record, 30)
    assert len(rows) == 4
    families = {r["activity_family"] for r in rows}
    assert families == {"book_object_naming", "expressive_two_word_phrase",
                        "expressive_vocabulary_growth", "pronouns"}


@pytest.mark.parametrize("answer,expect_unresolved", [
    ("yes", 0),
    ("no", 1),
    ("not_sure", 0),      # unknown is NOT a target
    ("sometimes", 1),     # emerging IS a target
])
def test_one_answer_at_30m_never_skips_the_siblings(answer, expect_unresolved):
    """YES, NO, UNKNOWN and EMERGING all leave the other three skills asked.

    This is the single most important property of v2. In v1 any of these four
    answers retired the whole band.
    """
    record, asked = run("two_three_words", 30, {PRONOUNS: answer})
    at_30 = [m for m, _ in asked if m == 30]
    assert len(at_30) == 4, asked

    band = V2.band_assessment(record, 30)
    assert band.total_skills == 4
    assert len(band.assessed) == 4
    assert band.assessment_complete is True
    assert band.unassessed_count == 0
    assert len(band.unresolved) == expect_unresolved


def test_a_no_at_30m_does_not_prevent_assessing_the_rest():
    record, asked = run("two_three_words", 30, {PRONOUNS: "no"})
    milestones = [ms for m, ms in asked if m == 30]
    for expected in (BOOK_NAMING, TWO_WORD_ACTION, VOCAB_50, PRONOUNS):
        assert any(expected in ms for ms in milestones), expected


def test_the_36m_band_assesses_all_three_siblings():
    record, asked = run("short_sentences", 40, {WH_ASKING: "no"})
    at_36 = [ms for m, ms in asked if m == 36]
    assert len(at_36) == 3
    for expected in (WH_ASKING, OWN_NAME, ACTION_PICTURE):
        assert any(expected in ms for ms in at_36), expected


def test_the_48m_band_assesses_both_siblings():
    """48m holds function-question answering AND four-word sentences.

    In v1 the second never got asked, which is why `sentence_building` was
    unreachable.
    """
    record = V2.start_baseline_v2(AREA, "short_sentences", chronological_months=50)
    rows = V2.band_skill_rows(record, 48)
    assert len(rows) == 2
    assert {r["activity_family"] for r in rows} == {
        "object_function_questions", "four_word_sentences"}


# ---------------------------------------------------------------------------
# assessment_complete vs band_mastered
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("answer,complete,mastered", [
    ("yes", True, True),
    ("no", True, False),
    ("sometimes", True, False),
    ("not_sure", True, False),
])
def test_assessment_complete_and_mastered_are_separate(answer, complete,
                                                        mastered):
    """One boolean cannot carry both meanings.

    An assessed `unknown` makes the band COMPLETE (the question was put) and
    NOT mastered (no evidence of mastery).
    """
    record, _ = run("two_three_words", 30, {PRONOUNS: answer})
    band = V2.band_assessment(record, 30)
    assert band.assessment_complete is complete
    assert band.band_mastered is mastered


def test_an_incompletely_assessed_band_is_neither():
    record = V2.start_baseline_v2(AREA, "two_three_words", chronological_months=30)
    question = V2.next_question_v2(record)        # 24m
    record = V2.record_answer_v2(record, question, "yes")
    question = V2.next_question_v2(record)        # first 30m skill
    record = V2.record_answer_v2(record, question, "yes")
    band = V2.band_assessment(record, 30)
    assert band.assessment_complete is False
    assert band.band_mastered is False
    assert band.unassessed_count == 3


# ---------------------------------------------------------------------------
# unknown vs unassessed
# ---------------------------------------------------------------------------


def test_unknown_and_unassessed_are_distinguishable():
    """`unknown` is a stored state; `unassessed` is the absence of a record."""
    record, _ = run("two_three_words", 30, {PRONOUNS: "not_sure"})
    band = V2.band_assessment(record, 30)
    assert len(band.unknown) == 1
    assert band.unknown[0].state == V2.STATE_UNKNOWN
    assert band.unassessed_count == 0
    # And a band never entered has no evidence at all.
    assert V2.band_assessment(record, 48).assessed == ()
    assert V2.band_assessment(record, 48).unassessed_count == 2
    assert len(V2.unassessed_skills_in_band(record, 48)) == 2


def test_unassessed_is_not_a_storable_state():
    with pytest.raises(V2.BaselineV2Error):
        V2.BaselineSkillEvidence(domain=DOMAIN, months=30,
                                 milestone=PRONOUNS, subdomain="x",
                                 state="unassessed")


def test_an_unknown_does_not_suppress_a_known_deficit():
    """The founder's approved future F-B rule, provable at the Parent layer.

    book naming = NO, pronouns = UNKNOWN -> the NO is still a target and the
    unknown merely prevents calling the band mastered.
    """
    record, _ = run("two_three_words", 30,
                    {BOOK_NAMING: "no", PRONOUNS: "not_sure"})
    band = V2.band_assessment(record, 30)
    assert band.assessment_complete is True
    assert band.band_mastered is False
    assert [e.state for e in band.unresolved] == [V2.STATE_NOT_DEMONSTRATED]
    assert len(band.unknown) == 1
    targets = V2.unresolved_skills(record)
    assert len(targets) == 1
    assert BOOK_NAMING in targets[0].milestone


# ---------------------------------------------------------------------------
# Advancement and stopping
# ---------------------------------------------------------------------------


def test_advance_only_when_every_band_skill_is_demonstrated():
    record, asked = run("many_single_words", 24)
    assert record.bands_entered == [18, 24, 30]
    # Every entered band fully assessed.
    for months in record.bands_entered:
        assert V2.band_assessment(record, months).assessment_complete


@pytest.mark.parametrize("answer", ["no", "sometimes", "not_sure"])
def test_a_non_demonstrated_skill_stops_the_ascent(answer):
    record, _ = run("two_three_words", 30, {PRONOUNS: answer})
    assert record.bands_entered == [24, 30]
    assert 36 not in record.bands_entered


def test_emerging_prevents_mastery_and_is_retained_as_a_target():
    record, _ = run("two_three_words", 30, {VOCAB_50: "sometimes"})
    band = V2.band_assessment(record, 30)
    assert band.band_mastered is False
    targets = V2.unresolved_skills(record)
    assert [e.state for e in targets] == [V2.STATE_EMERGING]
    assert VOCAB_50 in targets[0].milestone


def test_a_deficit_at_the_entry_band_triggers_downward_confirmation():
    """Lower ground is CONFIRMED, never assumed demonstrated."""
    record, asked = run("short_sentences", 40, {WH_ASKING: "no"})
    assert record.bands_entered == [36, 30]
    assert V2.band_assessment(record, 30).band_mastered is True
    assert V2.band_assessment(record, 36).band_mastered is False
    # And the lower band was fully assessed, not inferred.
    assert len([m for m, _ in asked if m == 30]) == 4


def test_multiple_unresolved_skills_are_all_retained():
    """Two deficits in one band are two clinical facts, not one."""
    record, _ = run("two_three_words", 30,
                    {PRONOUNS: "no", VOCAB_50: "sometimes"})
    targets = V2.unresolved_skills(record)
    assert len(targets) == 2
    assert {e.state for e in targets} == {V2.STATE_NOT_DEMONSTRATED,
                                          V2.STATE_EMERGING}


def test_the_band_budget_bounds_the_baseline():
    record, asked = run("no_words_yet", 9)
    assert len(record.bands_entered) <= V2.MAX_BANDS
    # And every band it DID enter is complete — the budget never truncates a
    # band mid-way, which is the v1 failure it replaces.
    for months in record.bands_entered:
        assert V2.band_assessment(record, months).assessment_complete


# ---------------------------------------------------------------------------
# Entry anchors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("choice,anchor", [
    ("no_words_yet", 9),
    ("few_sounds_or_words", 12),
    ("many_single_words", 18),
    ("two_three_words", 24),
    ("short_sentences", 36),
])
def test_each_entry_choice_starts_at_its_band(choice, anchor):
    record = V2.start_baseline_v2(AREA, choice, chronological_months=30)
    assert record.entry_anchor_months == anchor
    question = V2.next_question_v2(record)
    assert question["months"] == anchor


def test_not_sure_routes_by_chronological_age_without_asserting_ability():
    """Chronological age is a ROUTING fallback, never evidence."""
    record = V2.start_baseline_v2(AREA, "not_sure", chronological_months=30)
    assert record.entry_anchor_months is None
    question = V2.next_question_v2(record)
    assert question["months"] == 30
    # Nothing below is assumed demonstrated.
    assert record.skills == {}
    assert V2.band_assessment(record, 24).assessed == ()


# ---------------------------------------------------------------------------
# No alphabetical clinical selection
# ---------------------------------------------------------------------------


def test_milestone_order_does_not_decide_which_skills_exist():
    """Ordering may fix the UX sequence; it must not select the band's skill.

    In v1 `question_at` sorted by milestone text and took `candidates[0]`. Here
    every row is returned, so the sort cannot drop a sibling — proven by
    comparing against the unsorted row set.
    """
    record = V2.start_baseline_v2(AREA, "two_three_words", chronological_months=30)
    subdomains, families = record.track()
    raw = {r["milestone"] for r in v1._rows_for_domain(DOMAIN, subdomains, families)
           if r["months"] == 30}
    ordered = {r["milestone"] for r in V2.band_skill_rows(record, 30)}
    assert ordered == raw


def test_the_alphabetically_last_30m_skill_is_still_assessed():
    """`says words like I me or we` sorts last and was never asked in v1."""
    record, asked = run("two_three_words", 30)
    assert any(PRONOUNS in ms for _, ms in asked)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_skill_identity_is_the_canonical_triple_not_truncated_text():
    """v1's `question_id` embedded `milestone[:48]` and could collide."""
    long_a = "a" * 48 + " first tail"
    long_b = "a" * 48 + " second tail"
    assert v1.BASELINE_VERSION not in V2.BASELINE_VERSION_V2
    assert V2.skill_key(DOMAIN, 30, long_a) != V2.skill_key(DOMAIN, 30, long_b)
    # The triple participates in full.
    assert V2.skill_key(DOMAIN, 30, PRONOUNS) != V2.skill_key(DOMAIN, 36, PRONOUNS)
    assert V2.skill_key("fine_motor", 30, PRONOUNS) != \
        V2.skill_key(DOMAIN, 30, PRONOUNS)


def test_skill_evidence_stores_no_raw_caregiver_answer():
    """`state` is the classification; the raw string adds nothing clinical."""
    import dataclasses

    names = {f.name for f in dataclasses.fields(V2.BaselineSkillEvidence)}
    assert names == {"domain", "months", "milestone", "subdomain", "state"}
    for forbidden in ("answer", "question_id", "uid", "diagnosis", "concern",
                      "qna", "child_id"):
        assert forbidden not in names


# ---------------------------------------------------------------------------
# v1 compatibility
# ---------------------------------------------------------------------------


def test_v1_retires_a_whole_band_and_v2_does_not():
    """The defect, measured side by side on the same ladder."""
    record = v1.start_baseline("talking", "two_three_words",
                               chronological_months=30)
    question = v1.first_question(record)
    while question is not None:
        record = v1.record_answer(record, question, "yes")
        question = v1.next_question(record)
    v1_at_30 = [a for a in record.asked if a["months"] == 30]
    assert len(record.asked) == 4          # the whole domain
    assert len(v1_at_30) == 1              # one of four skills

    v2_record, asked = run("two_three_words", 30)
    assert len([m for m, _ in asked if m == 30]) == 4


def test_a_v1_record_is_never_band_complete():
    record = v1.start_baseline("talking", "two_three_words",
                               chronological_months=30)
    assert V2.legacy_record_is_band_complete(record) is False
    record = v1.finalize(record)
    assert V2.legacy_record_is_band_complete(record) is False


def test_the_helper_refuses_a_v2_record():
    """A v2 record must be asked about a specific band, not blanket-judged."""
    record = V2.start_baseline_v2(AREA, "two_three_words", chronological_months=30)
    with pytest.raises(V2.BaselineV2Error):
        V2.legacy_record_is_band_complete(record)


def test_v1_is_not_mutated():
    assert v1.BASELINE_VERSION == "parent-2.4-functional-baseline-v1"
    assert V2.BASELINE_VERSION_V2 == "parent-2.4-functional-baseline-v2"
    assert v1.MAX_QUESTIONS == 4            # unchanged
    assert V2.MAX_BANDS == 3


# ---------------------------------------------------------------------------
# Derived month fields
# ---------------------------------------------------------------------------


def test_month_fields_are_derived_from_skill_evidence():
    record, _ = run("two_three_words", 30, {PRONOUNS: "no"})
    assert record.demonstrated_months == 24       # the only MASTERED band
    assert record.not_demonstrated_months == 30   # lowest band with a deficit
    assert record.routing_anchor_months == 24
    assert record.status == "BOUNDED"


def test_a_band_that_is_merely_complete_is_not_demonstrated():
    """An unknown leaves the band complete but NOT counted as mastery."""
    record, _ = run("two_three_words", 30, {PRONOUNS: "not_sure"})
    assert V2.band_assessment(record, 30).assessment_complete is True
    assert V2.band_assessment(record, 30).band_mastered is False
    assert record.demonstrated_months == 24       # NOT 30
