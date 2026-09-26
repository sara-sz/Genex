"""PARENT-0.4 — deterministic functional baseline.

The baseline asks a parent one fixed structured question, uses the answer to
pick a starting rung on the EXISTING validated Gold Standard ladder, then asks
2–4 real milestone questions to bracket the child.

Two properties matter more than the rest:

  * it is LLM-free and deterministic — same inputs, same questions, same result
    (section M), with no network and no randomness;
  * unknown stays unknown — "Not sure" and a missing answer must never become
    the historical six-month dev_age (sections D and E). That default is the
    reason this phase exists, so it is tested from several directions.

All fixtures are fictional. No real data.
"""

from __future__ import annotations

import pytest

from genex_core.activity_engine import DAILY_LIVING_CLINICAL_HOLD
from genex_core.functional_baseline import (
    AREAS,
    BASELINE_VERSION,
    LEGACY_NO_ANSWER_DEV_AGE,
    MAX_QUESTIONS,
    BaselineError,
    BaselineStatus,
    apply_baseline_to_state,
    area_for_domain,
    entry_screen,
    get_area,
    ladder_months,
    question_at,
    record_answer,
    run_baseline,
    start_baseline,
)
from parent_taxonomy.domains import BY_KEY

LEGACY_DOMAINS = {"language_and_communication", "cognitive", "movement_and_physical"}


# ===========================================================================
# Structure — four areas, canonical domains, mobile-first screen
# ===========================================================================

def test_four_parent_facing_areas():
    assert [a.area_id for a in AREAS] == ["talking", "hand_finger", "movement", "daily_skills"]


def test_areas_map_to_canonical_domains():
    assert {a.area_id: a.domain for a in AREAS} == {
        "talking": "talking_and_communicating",
        "hand_finger": "fine_motor",
        "movement": "gross_motor",
        "daily_skills": "daily_living",
    }
    for area in AREAS:
        assert area.domain in BY_KEY


def test_no_discipline_is_treated_as_a_domain():
    """OT/PT/SLP are disciplines, never developmental domains."""
    for area in AREAS:
        assert area.domain not in {"ot", "pt", "slp", "ot_pt"}


def test_untouched_domains_have_no_baseline_area():
    """Social/Learning are not redesigned here; Sensory stays content-pending."""
    for domain in ("social_and_emotional", "learning_and_thinking", "sensory"):
        assert area_for_domain(domain) is None


@pytest.mark.parametrize("area", AREAS, ids=lambda a: a.area_id)
def test_entry_screen_is_short_enough_for_a_phone(area):
    screen = entry_screen(area.area_id)
    assert screen["question"] == "Which best describes your child now?"
    assert len(screen["choices"]) == 6, "5 descriptors + Not sure"
    assert screen["choices"][-1]["choice_id"] == "not_sure"
    assert screen["baseline_version"] == BASELINE_VERSION
    for choice in screen["choices"]:
        assert len(choice["label"]) <= 40, choice["label"]


@pytest.mark.parametrize("area", AREAS, ids=lambda a: a.area_id)
def test_every_anchor_is_a_real_ladder_rung(area):
    """No invented milestones — each anchor must exist in the Gold Standard."""
    rungs = set(ladder_months(area.domain))
    for choice in area.choices:
        if choice.anchor_months is None:
            continue
        assert choice.anchor_months in rungs, f"{choice.choice_id}: {choice.anchor_months}"
        assert question_at(area.domain, choice.anchor_months) is not None


@pytest.mark.parametrize("area", AREAS, ids=lambda a: a.area_id)
def test_anchors_increase_with_the_descriptor(area):
    """Descriptors are ordered easiest to hardest; anchors must follow."""
    anchors = [c.anchor_months for c in area.choices if c.anchor_months is not None]
    assert anchors == sorted(anchors), anchors
    assert len(set(anchors)) == len(anchors), "two descriptors share an anchor"


@pytest.mark.parametrize("area", AREAS, ids=lambda a: a.area_id)
def test_ladder_is_monotonic(area):
    rungs = ladder_months(area.domain)
    assert rungs == sorted(set(rungs))
    assert len(rungs) >= 5, rungs


# ===========================================================================
# A / B / C — 48-month Talking
# ===========================================================================

def test_case_a_no_words_yet_does_not_start_at_sentence_questions():
    record = run_baseline("talking", "no_words_yet", 48, ["yes", "no"])
    first = record.asked[0]
    assert first["months"] <= 12, first
    assert first["months"] != 48
    # ...and it must not have been dragged to the child's chronological band.
    assert all(a["months"] < 36 for a in record.asked), record.asked


def test_case_b_many_single_words_starts_near_word_combination():
    record = run_baseline("talking", "many_single_words", 48, ["yes", "no"])
    assert record.asked[0]["months"] == 18
    assert "three or more words" in record.asked[0]["milestone"]
    # The next rung is the two-word-combination milestone.
    assert record.asked[1]["months"] == 24
    assert "two words together" in record.asked[1]["milestone"]


def test_case_c_short_sentences_does_not_push_backward():
    record = run_baseline("talking", "short_sentences", 48, ["yes", "no"])
    assert record.asked[0]["months"] == 36
    assert min(a["months"] for a in record.asked) >= 36


def test_entry_choice_is_not_the_result():
    """A 48m child choosing 'no words yet' must not be assigned dev_age 12."""
    record = run_baseline("talking", "no_words_yet", 48, ["yes", "no"])
    assert record.entry_anchor_months == 9
    # The RESULT comes from the answers, and is a routing anchor, not an age.
    assert record.routing_anchor_months == 9
    assert not hasattr(record, "dev_age")


# ===========================================================================
# D / E — unknown must never become six months
# ===========================================================================

def test_case_d_not_sure_entry_choice_is_unresolved():
    record = run_baseline("talking", "not_sure", 48, ["not_sure", "not_sure"])
    assert record.status == BaselineStatus.UNRESOLVED
    assert record.routing_anchor_months is None


@pytest.mark.parametrize("area", AREAS, ids=lambda a: a.area_id)
def test_case_d_all_not_sure_never_yields_six_months(area):
    record = run_baseline(area.area_id, "not_sure", 48, ["not_sure"] * MAX_QUESTIONS)
    assert record.routing_anchor_months is None
    assert record.routing_anchor_months != LEGACY_NO_ANSWER_DEV_AGE


def test_case_e_no_answers_at_all_is_unresolved():
    record = run_baseline("talking", "many_single_words", 48, [])
    assert record.asked == []
    assert record.status == BaselineStatus.UNRESOLVED
    assert record.routing_anchor_months is None


def test_unresolved_baseline_writes_no_dev_age():
    """The six-month default must not reach state through the baseline."""
    state = {"dev_age": {}}
    record = run_baseline("talking", "not_sure", 48, ["not_sure"])
    apply_baseline_to_state(state, record)
    assert "talking_and_communicating" not in state["dev_age"]
    assert LEGACY_NO_ANSWER_DEV_AGE not in state["dev_age"].values()


def test_unresolved_baseline_clears_a_stale_dev_age():
    state = {"dev_age": {"talking_and_communicating": 6}}
    record = run_baseline("talking", "not_sure", 48, ["not_sure"])
    apply_baseline_to_state(state, record)
    assert "talking_and_communicating" not in state["dev_age"]


def test_resolved_baseline_writes_the_routing_anchor():
    state = {"dev_age": {}}
    record = run_baseline("talking", "many_single_words", 48, ["yes", "no"])
    apply_baseline_to_state(state, record)
    assert state["dev_age"]["talking_and_communicating"] == record.routing_anchor_months
    assert state["functional_baseline"]["talking_and_communicating"]["status"] == record.status


def test_not_sure_is_not_a_failed_milestone():
    """Unknown must not be scored as absence of the skill."""
    unknown = run_baseline("talking", "many_single_words", 48, ["not_sure", "not_sure"])
    negative = run_baseline("talking", "many_single_words", 48, ["no", "no"])
    assert unknown.status == BaselineStatus.UNRESOLVED
    assert unknown.not_demonstrated_months is None
    assert negative.not_demonstrated_months is not None


def test_unknown_steps_toward_an_answerable_question():
    record = run_baseline("talking", "many_single_words", 48, ["not_sure", "yes"])
    assert record.asked[1]["months"] < record.asked[0]["months"]
    assert record.routing_anchor_months == record.asked[1]["months"]


# ===========================================================================
# F / 13 — diagnosis and concern text cannot set ability
# ===========================================================================

def test_case_f_observed_ability_outranks_diagnosis():
    """Diagnosis cannot override observed ability because it is not an input.

    Proven structurally rather than by comparing two runs: no public baseline
    function accepts a diagnosis, so there is no channel through which one could
    reach the result. A same-vs-same comparison would pass even if diagnosis
    were wired in, so it is not used as the evidence here.
    """
    import inspect

    from genex_core import functional_baseline

    public = [
        getattr(functional_baseline, name)
        for name in dir(functional_baseline)
        if not name.startswith("_") and callable(getattr(functional_baseline, name))
    ]
    for fn in public:
        try:
            params = set(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            continue
        assert not params & {"diagnosis", "condition", "diagnosis_or_condition"}, fn

    # And the state the baseline produces carries no diagnosis-derived field.
    stored = run_baseline("talking", "few_sounds_or_words", 48, ["yes", "no"]).to_state()
    assert not any("diagnos" in k.lower() for k in stored)


def test_concern_text_is_not_an_input():
    """Free text must never select the functional level."""
    import inspect

    from genex_core import functional_baseline

    for fn in (functional_baseline.start_baseline, functional_baseline.run_baseline):
        params = set(inspect.signature(fn).parameters)
        assert not params & {"concern", "parent_concern", "diagnosis", "text"}, params


def test_chronological_age_does_not_change_an_anchored_baseline():
    """Age is context only once a structured choice exists."""
    young = run_baseline("talking", "many_single_words", 24, ["yes", "no"])
    old = run_baseline("talking", "many_single_words", 60, ["yes", "no"])
    assert [a["months"] for a in young.asked] == [a["months"] for a in old.asked]
    assert young.routing_anchor_months == old.routing_anchor_months


# ===========================================================================
# G / H / I — the other three areas enter sensible neighbourhoods
# ===========================================================================

EXPECTED_FIRST_RUNG = {
    ("hand_finger", "trouble_holding"): 4,
    ("hand_finger", "whole_hand"): 9,
    ("hand_finger", "finger_pickup"): 12,
    ("hand_finger", "stacks_scribbles"): 18,
    ("hand_finger", "crayons_tools"): 36,
    ("movement", "needs_help_sitting"): 6,
    ("movement", "moves_not_walking"): 12,
    ("movement", "walks_independently"): 15,
    ("movement", "runs_climbs"): 24,
    ("movement", "jumps_balances"): 30,
    ("daily_skills", "needs_help_most"): 15,
    ("daily_skills", "helps_with_parts"): 18,
    ("daily_skills", "some_steps_independent"): 24,
    ("daily_skills", "many_routines_with_help"): 30,
    ("daily_skills", "mostly_independent"): 48,
}


@pytest.mark.parametrize("key,expected", sorted(EXPECTED_FIRST_RUNG.items()))
def test_descriptor_enters_its_deterministic_neighbourhood(key, expected):
    area_id, choice_id = key
    record = run_baseline(area_id, choice_id, 48, ["yes", "no"])
    assert record.asked[0]["months"] == expected


def test_fine_motor_baseline_excludes_daily_living_skills():
    """Dressing and utensils are Daily Living, not Fine Motor."""
    for choice_id in ("trouble_holding", "whole_hand", "finger_pickup",
                      "stacks_scribbles", "crayons_tools"):
        record = run_baseline("hand_finger", choice_id, 48, ["yes", "no"])
        for asked in record.asked:
            assert asked["subdomain"] == "fine_motor_hand_use", asked


def test_gross_motor_baseline_uses_only_motor_subdomains():
    for choice_id in ("needs_help_sitting", "moves_not_walking", "walks_independently",
                      "runs_climbs", "jumps_balances"):
        record = run_baseline("movement", choice_id, 48, ["yes", "no"])
        for asked in record.asked:
            assert asked["subdomain"] in {
                "gross_motor_mobility_and_coordination",
                "postural_control_and_transitions",
            }, asked


# ===========================================================================
# J — clinical holds preserved
# ===========================================================================

def test_case_j_held_families_never_become_baseline_questions():
    """cup_drinking and feeding_self_regulation stay out of the baseline."""
    for months in ladder_months("daily_living"):
        question = question_at("daily_living", months)
        assert question["activity_family"] not in DAILY_LIVING_CLINICAL_HOLD, question


def test_case_j_holds_are_unchanged():
    assert DAILY_LIVING_CLINICAL_HOLD == frozenset({"cup_drinking", "feeding_self_regulation"})


def test_daily_living_ladder_drops_the_held_rungs():
    """12m is cup_drinking only, so it must not appear as a rung."""
    assert 12 not in ladder_months("daily_living")
    assert 6 not in ladder_months("daily_living")


def test_no_feeding_or_swallowing_advice_in_baseline_questions():
    for months in ladder_months("daily_living"):
        text = question_at("daily_living", months)["milestone"].lower()
        for phrase in ("swallow", "choke", "aspirat", "thicken"):
            assert phrase not in text, text


# ===========================================================================
# K — contradictions are reported, never averaged
# ===========================================================================

def _contradictory_record():
    """Build a record where a HARDER skill is demonstrated and an EASIER one is not.

    Constructed directly rather than driven through `run_baseline`, because the
    router brackets and stops before it can produce this ordering — it steps
    easier after a "no" and never back up. The branch is a safety net for
    answers that arrive out of order (a resumed session, a future router
    change), so it is tested where it actually lives instead of through a path
    that cannot reach it. Driving it through the router would give a test that
    passes without ever executing the logic.
    """
    from genex_core.functional_baseline import finalize

    record = start_baseline("talking", "many_single_words", 48)
    record_answer(record, question_at("talking_and_communicating", 24), "no")
    record_answer(record, question_at("talking_and_communicating", 30), "yes")
    return finalize(record)


def test_case_k_contradictory_answers_are_flagged():
    """Harder skill yes, easier skill no — reported, not silently averaged."""
    record = _contradictory_record()
    assert record.status == BaselineStatus.CONTRADICTORY
    assert record.routing_anchor_months is None


def test_contradiction_never_averages_to_a_middle_value():
    record = _contradictory_record()
    months = [a["months"] for a in record.asked]
    assert record.routing_anchor_months is None
    assert record.routing_anchor_months != sum(months) // len(months)


def test_contradiction_writes_no_dev_age():
    state = {"dev_age": {}}
    apply_baseline_to_state(state, _contradictory_record())
    assert "talking_and_communicating" not in state["dev_age"]


def test_router_brackets_rather_than_producing_contradictions():
    """The normal path should not generate out-of-order evidence at all."""
    for answers in (["no", "yes", "yes"], ["yes", "no", "yes"], ["sometimes", "yes", "no"]):
        record = run_baseline("talking", "many_single_words", 48, answers)
        demonstrated = [a["months"] for a in record.asked if a["classification"] == "demonstrated"]
        refused = [a["months"] for a in record.asked if a["classification"] == "not_demonstrated"]
        if demonstrated and refused:
            assert max(demonstrated) < min(refused), (answers, record.asked)


def test_bracket_is_reported_when_available():
    record = run_baseline("talking", "many_single_words", 48, ["yes", "no"])
    assert record.status == BaselineStatus.BOUNDED
    assert record.demonstrated_months == 18
    assert record.not_demonstrated_months == 24
    assert record.demonstrated_months < record.not_demonstrated_months


def test_emerging_answers_are_not_treated_as_demonstrated():
    record = run_baseline("talking", "many_single_words", 48, ["sometimes", "no"])
    assert record.demonstrated_months is None
    assert record.routing_anchor_months == 18


# ===========================================================================
# L — no legacy domain written
# ===========================================================================

@pytest.mark.parametrize("area", AREAS, ids=lambda a: a.area_id)
def test_case_l_no_legacy_domain_is_written(area):
    state = {"dev_age": {}}
    record = run_baseline(area.area_id, area.choices[2].choice_id, 48, ["yes", "no"])
    apply_baseline_to_state(state, record)
    assert not set(state["dev_age"]) & LEGACY_DOMAINS
    assert not set(state["functional_baseline"]) & LEGACY_DOMAINS
    assert record.domain in BY_KEY


# ===========================================================================
# M / 19 — deterministic, LLM-free, no network, no randomness
# ===========================================================================

@pytest.mark.parametrize("area", AREAS, ids=lambda a: a.area_id)
def test_case_m_identical_input_gives_identical_output(area):
    answers = ["yes", "sometimes", "no", "yes"]
    runs = [run_baseline(area.area_id, area.choices[1].choice_id, 42, answers).to_state()
            for _ in range(5)]
    assert all(r == runs[0] for r in runs)


def test_module_makes_no_llm_or_network_or_random_call():
    """Structural: the baseline must not import any of these."""
    import ast
    import pathlib

    source = pathlib.Path(
        __file__
    ).resolve().parent.parent / "genex_core" / "functional_baseline.py"
    tree = ast.parse(source.read_text())
    banned = {"openai", "requests", "httpx", "random", "urllib", "socket", "secrets"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned, alias.name
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned, node.module


def test_question_selection_is_pure():
    first = [question_at("talking_and_communicating", m) for m in ladder_months("talking_and_communicating")]
    second = [question_at("talking_and_communicating", m) for m in ladder_months("talking_and_communicating")]
    assert first == second


def test_question_sequence_is_bounded():
    for area in AREAS:
        record = run_baseline(area.area_id, "not_sure", 48, ["not_sure"] * 10)
        assert len(record.asked) <= MAX_QUESTIONS, len(record.asked)


def test_typical_sequence_is_two_or_three_questions():
    record = run_baseline("talking", "many_single_words", 48, ["yes", "no", "yes", "no"])
    assert 2 <= len(record.asked) <= 3, [a["months"] for a in record.asked]


# ===========================================================================
# 18 — stored provenance is enough to reproduce the baseline
# ===========================================================================

def test_state_record_carries_full_provenance():
    record = run_baseline("daily_skills", "some_steps_independent", 40, ["yes", "no"])
    stored = record.to_state()
    for key in (
        "baseline_version", "area_id", "domain",
        "entry_choice_id", "entry_choice_label", "entry_anchor_months",
        "chronological_months", "asked", "status", "routing_anchor_months",
    ):
        assert key in stored, key
    for asked in stored["asked"]:
        for key in ("question_id", "months", "milestone", "answer", "classification"):
            assert key in asked, key


def test_stored_record_is_reproducible():
    args = ("movement", "walks_independently", 30, ["yes", "sometimes"])
    assert run_baseline(*args).to_state() == run_baseline(*args).to_state()


def test_routing_anchor_is_not_named_developmental_age():
    """The semantic boundary matters more than cosmetics — keep the name apart."""
    stored = run_baseline("talking", "short_sentences", 48, ["yes", "no"]).to_state()
    assert "routing_anchor_months" in stored
    assert "developmental_age" not in stored
    assert "dev_age" not in stored


def test_attach_does_not_mutate_history():
    state = {"dev_age": {"fine_motor": 18}, "functional_baseline": {}}
    record = run_baseline("talking", "short_sentences", 48, ["yes", "no"])
    apply_baseline_to_state(state, record)
    assert state["dev_age"]["fine_motor"] == 18, "unrelated domain was modified"


# ===========================================================================
# input validation
# ===========================================================================

def test_unknown_area_raises():
    with pytest.raises(BaselineError):
        get_area("sensory")


def test_unknown_choice_raises():
    with pytest.raises(BaselineError):
        start_baseline("talking", "not_a_choice", 36)


def test_unsupported_answer_raises():
    with pytest.raises(BaselineError):
        run_baseline("talking", "many_single_words", 48, ["definitely"])


@pytest.mark.parametrize("answer", ["yes", "sometimes", "with_help", "no", "not_sure"])
def test_every_supported_answer_is_accepted(answer):
    record = run_baseline("talking", "many_single_words", 48, [answer])
    assert len(record.asked) == 1


# ===========================================================================
# Skill-track coherence (founder review)
# ===========================================================================
#
# Stepping one rung by month is only safe when neighbouring rungs sit on one
# developmental chain. Two defects were found by enumerating every reachable
# route and are pinned here:
#
#   * Daily Living could ask a 48-month-old's parent a NEWBORN feeding-cue
#     question. With the held families removed, the 4m adaptive_feeding_cues row
#     became the next rung below 15m, so one "easier" step reached it.
#   * Daily Living bracketed ACROSS routines — eating against dressing against
#     fasteners — which are not one chain, so an uneven-but-normal profile could
#     be read as evidence of a level.

import itertools

ALL_ANSWERS = ("yes", "sometimes", "no", "not_sure")


def _reachable(area_id, choice_id, age=48):
    """Every question reachable from a descriptor, over all answer paths."""
    seen = []
    for combo in itertools.product(ALL_ANSWERS, repeat=MAX_QUESTIONS):
        for asked in run_baseline(area_id, choice_id, age, list(combo)).asked:
            seen.append(asked)
    return seen


@pytest.mark.parametrize("area", AREAS, ids=lambda a: a.area_id)
def test_every_area_declares_a_skill_track(area):
    assert area.track_subdomains, f"{area.area_id} has no declared track"


@pytest.mark.parametrize("area", AREAS, ids=lambda a: a.area_id)
def test_routing_never_leaves_the_declared_track(area):
    for choice in area.choices:
        for asked in _reachable(area.area_id, choice.choice_id):
            assert asked["subdomain"] in area.track_subdomains, (choice.choice_id, asked)


def test_talking_stays_on_the_expressive_track():
    """Descriptors are about spoken output, so routing must not jump elsewhere."""
    allowed = {"expressive_language", "early_vocalization_and_babbling"}
    for choice in get_area("talking").choices:
        for asked in _reachable("talking", choice.choice_id):
            assert asked["subdomain"] in allowed, asked


def test_talking_never_asks_receptive_or_conversation_questions():
    """A strong-receptive / weak-expressive child must not be bracketed on it."""
    excluded = {"receptive_language", "gestural_communication",
                "conversation_narrative", "speech_intelligibility"}
    for choice in get_area("talking").choices:
        for asked in _reachable("talking", choice.choice_id):
            assert asked["subdomain"] not in excluded, asked


def test_strong_receptive_weak_expressive_is_not_contradictory():
    """The uneven profile the founder flagged. Expressive-only evidence brackets."""
    record = run_baseline("talking", "many_single_words", 48, ["no", "yes"])
    assert record.status != BaselineStatus.CONTRADICTORY, record.asked
    assert all(a["subdomain"] in {"expressive_language",
                                  "early_vocalization_and_babbling"} for a in record.asked)


def test_fine_motor_never_enters_daily_living():
    """Dressing and utensils use hands but are Daily Living, not Fine Motor."""
    for choice in get_area("hand_finger").choices:
        for asked in _reachable("hand_finger", choice.choice_id):
            assert asked["subdomain"] == "fine_motor_hand_use", asked


def test_gross_motor_transition_is_a_prerequisite_chain_not_month_adjacency():
    """Postural control and mobility partition cleanly and hand off once."""
    from genex_core.functional_baseline import _rows_for_domain

    area = get_area("movement")
    postural, mobility = set(), set()
    for row in _rows_for_domain("gross_motor", area.track_subdomains):
        if row["subdomain"] == "postural_control_and_transitions":
            postural.add(row["months"])
        else:
            mobility.add(row["months"])
    # Postural precedes mobility, overlapping only at the 12m handoff where
    # pulling to stand meets walking while holding on.
    assert max(postural) <= min(mobility), (sorted(postural), sorted(mobility))
    assert postural & mobility == {12}


def test_daily_living_never_asks_a_newborn_feeding_cue():
    """The defect: one easier step from 15m used to reach the 4m cue row."""
    for choice in get_area("daily_skills").choices:
        for asked in _reachable("daily_skills", choice.choice_id):
            assert "breast or bottle" not in asked["milestone"], asked
            assert asked["months"] >= 15, asked


def test_daily_living_excludes_feeding_cue_and_safety_subdomains():
    area = get_area("daily_skills")
    assert area.track_subdomains == ("self_help_motor_skills",)
    assert 4 not in ladder_months("daily_living", area.track_subdomains)


@pytest.mark.parametrize("choice_id,families", [
    ("needs_help_most", {"finger_feeding", "spoon_use", "fork_use", "serving_pouring_transfer"}),
    ("helps_with_parts", {"finger_feeding", "spoon_use", "fork_use", "serving_pouring_transfer"}),
    ("some_steps_independent", {"finger_feeding", "spoon_use", "fork_use", "serving_pouring_transfer"}),
    ("many_routines_with_help", {"dressing_off", "dressing_on", "buttoning_fasteners"}),
    ("mostly_independent", {"dressing_off", "dressing_on", "buttoning_fasteners"}),
])
def test_daily_living_brackets_within_one_routine(choice_id, families):
    """Eating, dressing and fastening are different routines, not one ladder."""
    area = get_area("daily_skills")
    choice = area.choice(choice_id)
    assert set(choice.track_families) == families
    for month in ladder_months("daily_living", area.track_subdomains, choice.track_families):
        question = question_at("daily_living", month, "", area.track_subdomains,
                               choice.track_families)
        assert question["activity_family"] in families, question


def test_daily_living_uneven_routines_are_not_contradictory():
    """Independent eating with dependent dressing is uneven, not contradictory."""
    eating = run_baseline("daily_skills", "some_steps_independent", 48, ["yes", "no"])
    dressing = run_baseline("daily_skills", "many_routines_with_help", 48, ["no", "no"])
    for record in (eating, dressing):
        assert record.status != BaselineStatus.CONTRADICTORY, record.asked


def test_contradiction_can_only_compare_same_track_rungs():
    """Every question in one baseline comes from one declared track."""
    for area in AREAS:
        for choice in area.choices:
            for combo in itertools.product(ALL_ANSWERS, repeat=3):
                record = run_baseline(area.area_id, choice.choice_id, 48, list(combo))
                subdomains = {a["subdomain"] for a in record.asked}
                assert subdomains <= set(area.track_subdomains), (choice.choice_id, subdomains)


def test_track_restriction_keeps_every_anchor_reachable():
    """The fix must not strand a descriptor off its own ladder."""
    for area in AREAS:
        for choice in area.choices:
            if choice.anchor_months is None:
                continue
            rungs = ladder_months(area.domain, area.track_subdomains, choice.track_families)
            assert choice.anchor_months in rungs, (choice.choice_id, rungs)


def test_track_restriction_preserves_determinism():
    for area in AREAS:
        answers = ["yes", "no", "sometimes"]
        runs = [run_baseline(area.area_id, area.choices[0].choice_id, 40, answers).to_state()
                for _ in range(3)]
        assert all(r == runs[0] for r in runs)


def test_track_restriction_preserves_unknown_handling():
    for area in AREAS:
        record = run_baseline(area.area_id, "not_sure", 48, ["not_sure"] * MAX_QUESTIONS)
        assert record.routing_anchor_months is None
        assert record.routing_anchor_months != LEGACY_NO_ANSWER_DEV_AGE


# ===========================================================================
# Daily Living + "Not sure" — no routine is chosen on the parent's behalf
# ===========================================================================
#
# self_help_motor_skills still contains several INDEPENDENT routines
# (self-feeding, dressing/fastening) that the descriptors disambiguate via
# track_families. "Not sure" supplies no descriptor, so walking the whole
# subdomain would reintroduce the cross-routine bracketing the track
# restriction just removed. It therefore calibrates nothing.

DL_FEEDING = {"finger_feeding", "spoon_use", "fork_use", "serving_pouring_transfer"}
DL_DRESSING = {"dressing_off", "dressing_on", "buttoning_fasteners"}


def test_daily_living_not_sure_asks_nothing():
    for answers in ([], ["not_sure"], ["yes", "no"], ["yes", "no", "yes", "no"]):
        record = run_baseline("daily_skills", "not_sure", 48, answers)
        assert record.asked == [], (answers, record.asked)


def test_daily_living_not_sure_is_unresolved():
    record = run_baseline("daily_skills", "not_sure", 48, ["yes", "no"])
    assert record.status == BaselineStatus.UNRESOLVED
    assert record.routing_anchor_months is None


def test_daily_living_not_sure_writes_no_dev_age():
    state = {"dev_age": {}}
    apply_baseline_to_state(state, run_baseline("daily_skills", "not_sure", 48, ["yes"]))
    assert "daily_living" not in state["dev_age"]
    assert state["dev_age"] == {}


def test_daily_living_not_sure_infers_no_routine():
    """Neither feeding nor dressing may be selected for the parent."""
    for age in (18, 24, 36, 48, 60):
        record = run_baseline("daily_skills", "not_sure", age, ["yes", "no"])
        families = {a.get("activity_family") for a in record.asked}
        assert not families & DL_FEEDING, families
        assert not families & DL_DRESSING, families
    choice = get_area("daily_skills").choice("not_sure")
    assert choice.track_families == (), choice.track_families


def test_daily_living_not_sure_does_not_vary_with_age():
    """Chronological age must not be used to pick a routine."""
    results = {run_baseline("daily_skills", "not_sure", age, ["yes", "no"]).to_state()["status"]
               for age in (12, 24, 36, 48, 60)}
    assert results == {BaselineStatus.UNRESOLVED}


def test_daily_living_not_sure_reaches_no_held_family():
    for age in (18, 36, 60):
        for asked in _reachable("daily_skills", "not_sure", age):
            assert asked.get("activity_family") not in DAILY_LIVING_CLINICAL_HOLD


def test_daily_living_not_sure_is_still_recorded_for_provenance():
    """Unresolved is a real outcome, not a missing record."""
    stored = run_baseline("daily_skills", "not_sure", 48, ["yes"]).to_state()
    assert stored["entry_choice_id"] == "not_sure"
    assert stored["domain"] == "daily_living"
    assert stored["status"] == BaselineStatus.UNRESOLVED
    assert stored["routing_anchor_months"] is None
    assert stored["asked"] == []


def test_only_daily_living_suppresses_not_sure_calibration():
    """The other three areas are one chain end to end, so they still calibrate."""
    for area in AREAS:
        record = run_baseline(area.area_id, "not_sure", 48, ["yes", "no"])
        if area.area_id == "daily_skills":
            assert record.asked == []
            assert area.not_sure_requires_a_routine is True
        else:
            assert record.asked, area.area_id
            assert area.not_sure_requires_a_routine is False


def test_descriptor_choices_still_calibrate_in_daily_living():
    """The suppression must apply to 'Not sure' only."""
    for choice in get_area("daily_skills").choices:
        if choice.choice_id == "not_sure":
            continue
        record = run_baseline("daily_skills", choice.choice_id, 48, ["yes", "no"])
        assert record.asked, choice.choice_id
        assert record.routing_anchor_months is not None, choice.choice_id
