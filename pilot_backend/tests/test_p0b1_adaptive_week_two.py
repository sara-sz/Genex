"""P0-B1 — repeated-signal adaptation -> reviewed-bank selection -> Week 2.

Dependency-pure: `FirestoreRepositories` over `FakeDocumentStore`, the port's
in-memory rung source, and the REAL reviewed static activity bank. Nothing is
seeded by hand: the goal comes from F-B v2 generation and verbatim approval,
Week 1 from the real release path, observations from the real caregiver
recording path (opaque `pact1:` ref -> instance), and Week 2 from the same
release entry point the HTTP route calls.

The world mirrors the live positive-path child: a 24-month target whose anchor
binds `expressive_vocabulary_growth` AND `two_word_phrases`, a Week 1 starter
released on Tue 2026-11-17 (six days, capacity 3), and Week 2 released with the
local date pinned to Mon 2026-11-23.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timezone

import pytest

from pilot_backend.domain.activity_template import ActivityTemplate
from pilot_backend.domain.adaptation_decision import (
    P0B_RULE_VERSION,
    ActivityAdaptationDecision,
    AdaptationBranch,
    AdaptationDecisionError,
    DecisionReason,
    GoalInvariantProof,
    weekly_adaptation_decision_id,
)
from pilot_backend.domain.canonical_rung import (
    ActivityFamilyBinding,
    CanonicalRung,
)
from pilot_backend.domain.goals import EditType, GoalKind, GoalRef, GoalStatus
from pilot_backend.domain.observation import (
    AttemptOutcome,
    Difficulty,
    Enjoyment,
)
from pilot_backend.domain.weekly_plan_document import (
    FORBIDDEN_PARENT_FIELDS,
    assert_no_forbidden_fields,
)
from pilot_backend.integration.adaptive_week import (
    AdaptiveSelectionRefused,
    ClinicalBoundaryViolation,
    select_week_templates,
    verify_reviewed_and_bounded,
)
from pilot_backend.integration.gold_standard_source import (
    InMemoryGoldStandardRungSource,
    RungTarget,
)
from pilot_backend.integration.week_one_release import (
    AdaptiveWeekIncomplete,
    AdaptiveWeekUnavailable,
    ClinicalBoundaryRefused,
    NonAdaptiveWeekRefused,
    WeekOneReleaseError,
    WeekOneReleaseService,
)
from pilot_backend.planning.service import MonthlyPlanService
from pilot_backend.weekly.adaptation import ADAPTATION_RULE_VERSION
from pilot_backend.weekly.repeated_signal_policy import decide
from pilot_backend.weekly.service import WeeklyService
from pilot_runtime.integration.static_activity_bank import StaticActivityBank

from .test_evidence_driven_generation_v2 import (
    AT_24,
    BOOK,
    CHILD,
    GOLD_STANDARD_VERSION,
    PRONOUNS,
    SUPPORTED_DOMAIN,
    TAXONOMY_VERSION,
    TRACK_SUBDOMAINS,
    TWO_WORD,
    VOCAB,
    World,
    _projection,
    mixed,
)

PILOT_ROOT = pathlib.Path(__file__).resolve().parents[1]
TZ = "America/New_York"
MONTH = "2026-11"
WEEK_ONE_DAY = "2026-11-17"      # Tuesday: a six-day starter week
WEEK_TWO_DAY = "2026-11-23"      # Monday: the founder's Week 2 proof date
CAPACITY = 3
FAMILIES_24 = ("expressive_vocabulary_growth", "two_word_phrases")


class Clock:
    """One mutable clock shared by every service, so "today" moves together."""

    def __init__(self, day: str):
        self.set(day)

    def set(self, day: str):
        y, m, d = (int(p) for p in day.split("-"))
        self.value = datetime(y, m, d, 15, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


def _rung(milestone, months, families):
    return CanonicalRung.build(
        domain_key=SUPPORTED_DOMAIN, source_rung_months=months,
        milestone_text=milestone, subdomain="expressive_language",
        family_bindings=tuple(
            ActivityFamilyBinding(family_ref=f,
                                  allowed_domains=(SUPPORTED_DOMAIN,))
            for f in families),
        track_subdomains=TRACK_SUBDOMAINS, track_families=(),
        taxonomy_version=TAXONOMY_VERSION,
        baseline_version=GOLD_STANDARD_VERSION)


def _source():
    """The 24-month rung binds BOTH reviewed families, as `feb590cf` does."""
    return InMemoryGoldStandardRungSource(
        rungs=(_rung(AT_24, 24, FAMILIES_24),
               _rung(BOOK, 30, ("book_object_naming",)),
               _rung(TWO_WORD, 30, ("two_word_phrases",)),
               _rung(VOCAB, 30, ("expressive_vocabulary_growth",))),
        unmappable=(RungTarget(domain_key=SUPPORTED_DOMAIN,
                               source_rung_months=30,
                               milestone_text=PRONOUNS),))


class Adaptive:
    """Approved 24-month goal -> released Week 1 on 2026-11-17."""

    def __init__(self):
        self.clock = Clock(WEEK_ONE_DAY)
        self.world = World(source=_source(),
                           projection=_projection(states_30=mixed(),
                                                  state_24="emerging"))
        self.repos = self.world.repos
        self.goals = self.world.goals
        self.provider = self.world.principal()
        self.caregiver = self.world.principal("cg-subject")
        self.plans = MonthlyPlanService(repos=self.repos,
                                        recorder=self.world.recorder,
                                        now=self.clock)
        self.weekly = WeeklyService(repos=self.repos,
                                   recorder=self.world.recorder,
                                   now=self.clock)
        self.bank = StaticActivityBank()
        self.service = WeekOneReleaseService(
            repos=self.repos, goals=self.goals, plans=self.plans,
            weekly=self.weekly, activity_bank=self.bank, now=self.clock)
        generated = self.world.generate()
        assert generated.target_band_months == 24
        self.goal = self.goals.approve_clinical_goal(
            self.provider, CHILD, edit_type=EditType.ACCEPTED_VERBATIM,
            suggestion_id=generated.generated[0].suggestion_ids[0])
        self.week_one = self.release(WEEK_ONE_DAY)
        self._feedback = 0

    # -- the product path ---------------------------------------------------

    def release(self, day, capacity=CAPACITY):
        self.clock.set(day)
        return self.service.release_week_one(
            self.provider, CHILD, family_declared_capacity=capacity,
            timezone_of_record=TZ, cycle_month=MONTH, local_date=day)

    def week_one_refs(self):
        """Opaque refs in the family's own Week 1 view, by activity title."""
        return {a["title"]: a["activity_ref"]
                for day in self.week_one.parent_week["days"]
                for a in day["activities"]}

    def observe(self, title, day, *, outcome=AttemptOutcome.DID_IT,
                difficulty=None, enjoyment=None, supersedes=""):
        """Record feedback exactly as the HTTP route does: opaque ref in."""
        self.clock.set(day)
        cycle_id = self.week_one.cycle_id
        instance = self.weekly.resolve_parent_activity_ref(
            self.caregiver, cycle_id, self.week_one_refs()[title])
        self._feedback += 1
        feedback_id = f"fb_p0b1_{self._feedback:03d}"
        event = self.weekly.record_observation(
            self.caregiver, cycle_id, instance, local_date=day,
            attempt_outcome=outcome, difficulty=difficulty,
            enjoyment=enjoyment, source_feedback_id=feedback_id,
            supersedes_source_feedback_id=supersedes)
        return feedback_id, event

    # -- reads --------------------------------------------------------------

    def docs(self, collection):
        return [d for _i, d in self.repos.store.list_all(collection)]

    def themes(self, template_ids):
        pool = {t.activity_template_id: t
                for t in self.bank.templates_for_families(FAMILIES_24)}
        return {" ".join(pool[r].theme.split()).casefold()
                for r in template_ids}

    def placed_ids(self, outcome):
        snapshot = self.repos.weekly_plan_snapshots.list_for_cycle(
            outcome.cycle_id)[0]
        return sorted({a["activity_template_id"]
                       for a in snapshot.document()["activities"]})

    def decision(self, outcome):
        return self.repos.adaptation_decisions.find_for_cycle(outcome.cycle_id)


MY_TURN = "My Turn"


@pytest.fixture
def a():
    return Adaptive()


# ===========================================================================
# 1. the starting point mirrors the live positive-path child
# ===========================================================================


def test_week_one_mirrors_the_live_child(a):
    assert a.week_one.created is True
    assert a.week_one.activity_count == CAPACITY
    titles = set(a.week_one_refs())
    assert titles == {"My Turn", "More Bubbles", "All Done and Stop"}
    assert all(ref.startswith("pact1:") for ref in a.week_one_refs().values())
    # Week 1 itself writes no adaptation of any kind.
    assert a.docs("pilot_adaptation_records") == []
    assert a.docs("pilot_weekly_adaptation_decisions") == []


# ===========================================================================
# 2. repeated disengagement -> CHANGE_THEME, dependency-pure on 2026-11-23
# ===========================================================================


def test_repeated_didnt_want_changes_theme_in_a_released_week_two(a):
    """THE proof: two signals -> one decision -> one Week 2 -> same goal."""
    a.observe(MY_TURN, "2026-11-17", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
    a.observe(MY_TURN, "2026-11-18", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)

    week_two = a.release(WEEK_TWO_DAY)
    assert week_two.created is True
    assert week_two.cycle_id != a.week_one.cycle_id
    assert week_two.released_at is not None

    cycle = a.weekly.get_cycle(a.provider, week_two.cycle_id)
    assert cycle.sequence_in_month == 2
    assert (cycle.starts_on, cycle.ends_on) == ("2026-11-23", "2026-11-29")
    assert cycle.predecessor_cycle_id == a.week_one.cycle_id
    assert cycle.is_released

    decision = a.decision(week_two)
    assert decision is not None
    assert decision.rule_version == P0B_RULE_VERSION
    by_ref = {d.activity_identity_ref: d for d in decision.activity_decisions}
    changed = [d for d in by_ref.values()
               if d.branch is AdaptationBranch.CHANGE_THEME]
    assert len(changed) == 1
    assert changed[0].reason is DecisionReason.REPEATED_DISENGAGEMENT
    assert changed[0].signal == "didnt_want_to_try"
    assert len(changed[0].trigger_event_ids) == 2
    others = [d for d in by_ref.values() if d is not changed[0]]
    assert {d.branch for d in others} == {AdaptationBranch.MAINTAIN_VARY}

    week_one_ids = set(a.placed_ids(a.week_one))
    week_two_ids = set(a.placed_ids(week_two))
    assert len(week_two_ids) == CAPACITY
    assert not week_two_ids & week_one_ids, "a disengaged week was repeated"
    assert not a.themes(week_two_ids) & a.themes(week_one_ids), \
        "CHANGE_THEME reused a theme from the previous week"
    assert set(decision.excluded_themes) == a.themes(week_one_ids)
    assert decision.repeated_template_ids == ()
    assert set(by_ref) == week_one_ids, "every Week 1 activity got a decision"


def test_repeated_not_really_changes_theme(a):
    """Parent's "not really" is the canonical `disliked` enjoyment."""
    a.observe(MY_TURN, "2026-11-17", enjoyment=Enjoyment.DISLIKED)
    a.observe(MY_TURN, "2026-11-19", enjoyment=Enjoyment.DISLIKED)
    decision = a.decision(a.release(WEEK_TWO_DAY))
    changed = [d for d in decision.activity_decisions
               if d.branch is AdaptationBranch.CHANGE_THEME]
    assert [d.signal for d in changed] == ["disliked"]


def test_different_disengagement_signals_do_not_pair(a):
    """`didnt_want_to_try` then `disliked` is not the SAME signal twice."""
    a.observe(MY_TURN, "2026-11-17", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
    a.observe(MY_TURN, "2026-11-18", enjoyment=Enjoyment.DISLIKED)
    decision = a.decision(a.release(WEEK_TWO_DAY))
    assert AdaptationBranch.CHANGE_THEME not in decision.branches


# ===========================================================================
# 3. only CURRENT observations count
# ===========================================================================


def test_a_superseded_disengagement_does_not_count(a):
    first, _ = a.observe(MY_TURN, "2026-11-17",
                         outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
    _corr, correction = a.observe(MY_TURN, "2026-11-17",
                                  outcome=AttemptOutcome.DID_IT,
                                  enjoyment=Enjoyment.ENJOYED,
                                  supersedes=first)
    a.observe(MY_TURN, "2026-11-18", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)

    week_two = a.release(WEEK_TWO_DAY)
    decision = a.decision(week_two)
    assert AdaptationBranch.CHANGE_THEME not in decision.branches
    stored = a.repos.observation_events.list_for_cycle(a.week_one.cycle_id)
    superseded = [e.event_id for e in stored if e.source_feedback_id == first]
    assert decision.superseded_event_ids == tuple(superseded)
    assert not set(superseded) & set(decision.current_event_ids)
    assert correction.event_id in decision.current_event_ids


def test_a_correction_into_disengagement_does_count(a):
    first, _ = a.observe(MY_TURN, "2026-11-17", outcome=AttemptOutcome.DID_IT)
    a.observe(MY_TURN, "2026-11-17", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY,
              supersedes=first)
    a.observe(MY_TURN, "2026-11-18", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
    decision = a.decision(a.release(WEEK_TWO_DAY))
    assert AdaptationBranch.CHANGE_THEME in decision.branches
    assert len(decision.superseded_event_ids) == 1


# ===========================================================================
# 4. what must NOT trigger, or must not be adapted yet
# ===========================================================================


def test_one_wasnt_ready_does_not_adapt(a):
    a.observe(MY_TURN, "2026-11-17", outcome=AttemptOutcome.WASNT_READY_YET)
    decision = a.decision(a.release(WEEK_TWO_DAY))
    assert set(decision.branches) == {AdaptationBranch.MAINTAIN_VARY}
    assert all(d.reason is DecisionReason.NO_TRIGGER
               for d in decision.activity_decisions)


@pytest.mark.parametrize("kwargs,reason", [
    ({"outcome": AttemptOutcome.WASNT_READY_YET},
     DecisionReason.NOT_YET_SUPPORTED_REPEATED_WASNT_READY),
    ({"difficulty": Difficulty.TOO_HARD},
     DecisionReason.NOT_YET_SUPPORTED_REPEATED_TOO_HARD),
    ({"difficulty": Difficulty.TOO_EASY},
     DecisionReason.NOT_YET_SUPPORTED_REPEATED_TOO_EASY),
])
def test_p0b2_triggers_are_no_change_not_yet_supported(a, kwargs, reason):
    """Recognised, RECORDED, and answered with the same activity again —
    never with the retired easier/harder behaviour."""
    a.observe(MY_TURN, "2026-11-17", **kwargs)
    a.observe(MY_TURN, "2026-11-18", **kwargs)
    week_two = a.release(WEEK_TWO_DAY)
    decision = a.decision(week_two)
    (held,) = [d for d in decision.activity_decisions
               if d.branch is AdaptationBranch.NO_CHANGE]
    assert held.reason is reason and reason.is_not_yet_supported
    assert held.activity_identity_ref in a.placed_ids(week_two), \
        "a not-yet-supported trigger must keep the same reviewed activity"
    assert decision.repeated_template_ids == (held.activity_identity_ref,)


def test_too_easy_is_never_mastery_or_progression(a):
    a.observe(MY_TURN, "2026-11-17", difficulty=Difficulty.TOO_EASY)
    a.observe(MY_TURN, "2026-11-18", difficulty=Difficulty.TOO_EASY)
    before_goals = a.docs("pilot_clinical_goals")
    before_suggestions = a.docs("pilot_goal_suggestions")
    a.release(WEEK_TWO_DAY)

    assert a.docs("pilot_clinical_goals") == before_goals
    assert a.docs("pilot_goal_suggestions") == before_suggestions
    goal = a.goals.get_goal(a.provider,
                            GoalRef(GoalKind.CLINICAL, a.goal.clinical_goal_id))
    assert goal.status is GoalStatus.ACTIVE
    assert goal.current_version_id == a.goal.current_version_id
    record = a.docs("pilot_adaptation_records")[0]
    assert "harder" not in record["resulting_change"]
    assert "progress" not in record["resulting_change"]
    assert "master" not in record["resulting_change"]


def test_two_signals_on_the_same_practice_date_do_not_trigger(a):
    a.observe(MY_TURN, "2026-11-17", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
    a.observe(MY_TURN, "2026-11-17", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
    decision = a.decision(a.release(WEEK_TWO_DAY))
    assert AdaptationBranch.CHANGE_THEME not in decision.branches


def test_something_different_in_between_breaks_the_repeat(a):
    a.observe(MY_TURN, "2026-11-17", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
    a.observe(MY_TURN, "2026-11-18", outcome=AttemptOutcome.DID_IT,
              enjoyment=Enjoyment.ENJOYED)
    a.observe(MY_TURN, "2026-11-19", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
    decision = a.decision(a.release(WEEK_TWO_DAY))
    assert AdaptationBranch.CHANGE_THEME not in decision.branches


def test_signals_on_two_different_activities_are_not_pooled(a):
    a.observe(MY_TURN, "2026-11-17", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
    a.observe("More Bubbles", "2026-11-18",
              outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
    decision = a.decision(a.release(WEEK_TWO_DAY))
    assert AdaptationBranch.CHANGE_THEME not in decision.branches


def test_no_feedback_maintains_and_varies(a):
    week_two = a.release(WEEK_TWO_DAY)
    decision = a.decision(week_two)
    assert set(decision.branches) == {AdaptationBranch.MAINTAIN_VARY}
    assert not set(a.placed_ids(week_two)) & set(a.placed_ids(a.week_one)), \
        "variation must prefer reviewed unused alternatives"


# ===========================================================================
# 5. the clinical boundary: same goal, version, anchor and families
# ===========================================================================


def test_goal_version_and_anchor_are_unchanged_and_proven(a):
    a.observe(MY_TURN, "2026-11-17", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
    a.observe(MY_TURN, "2026-11-18", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
    anchor_before = a.repos.clinical_goal_anchors.find(a.goal.clinical_goal_id)
    week_two = a.release(WEEK_TWO_DAY)

    assert week_two.goal_ids == a.week_one.goal_ids
    (proof,) = a.decision(week_two).goal_proofs
    assert proof.goal_id == a.goal.clinical_goal_id
    assert proof.goal_version_before == proof.goal_version_after \
        == a.goal.current_version_id
    assert proof.anchor_rung_ref_before == proof.anchor_rung_ref_after \
        == anchor_before.rung.rung_ref
    assert proof.allowed_family_refs == tuple(sorted(FAMILIES_24))
    assert set(proof.selected_family_refs) <= set(FAMILIES_24)
    assert (proof.goal_unchanged and proof.goal_version_unchanged
            and proof.anchor_unchanged and proof.families_within_anchor)

    snap_one = a.repos.weekly_plan_snapshots.list_for_cycle(
        a.week_one.cycle_id)[0].document()
    snap_two = a.repos.weekly_plan_snapshots.list_for_cycle(
        week_two.cycle_id)[0].document()
    assert snap_one["goals"] == snap_two["goals"]
    assert a.repos.clinical_goal_anchors.find(
        a.goal.clinical_goal_id) == anchor_before


def test_a_revised_goal_version_refuses_before_any_write(a):
    a.goals.revise_goal(
        a.provider, GoalRef(GoalKind.CLINICAL, a.goal.clinical_goal_id),
        "Help {child} combine two words during play.",
        reason="clinician reworded between weeks")
    cycles_before = len(a.docs("pilot_weekly_cycles"))
    with pytest.raises(ClinicalBoundaryRefused):
        a.release(WEEK_TWO_DAY)
    assert len(a.docs("pilot_weekly_cycles")) == cycles_before
    assert a.docs("pilot_weekly_adaptation_decisions") == []
    assert a.docs("pilot_adaptation_records") == []


def test_a_proof_cannot_even_be_constructed_across_a_change():
    with pytest.raises(AdaptationDecisionError):
        GoalInvariantProof(goal_id="clgl_1", goal_version_before="gver_1",
                           goal_version_after="gver_2",
                           anchor_rung_ref_before="rung1:a",
                           anchor_rung_ref_after="rung1:a",
                           allowed_family_refs=("two_word_phrases",),
                           selected_family_refs=("two_word_phrases",))
    with pytest.raises(AdaptationDecisionError):
        GoalInvariantProof(goal_id="clgl_1", goal_version_before="gver_1",
                           goal_version_after="gver_1",
                           anchor_rung_ref_before="rung1:a",
                           anchor_rung_ref_after="rung1:b",
                           allowed_family_refs=("two_word_phrases",),
                           selected_family_refs=("two_word_phrases",))
    with pytest.raises(AdaptationDecisionError):
        GoalInvariantProof(goal_id="clgl_1", goal_version_before="gver_1",
                           goal_version_after="gver_1",
                           anchor_rung_ref_before="rung1:a",
                           anchor_rung_ref_after="rung1:a",
                           allowed_family_refs=("two_word_phrases",),
                           selected_family_refs=("sentence_building",))
    with pytest.raises(AdaptationDecisionError):
        GoalInvariantProof(goal_id="clgl_1", goal_version_before="gver_1",
                           goal_version_after="gver_1",
                           anchor_rung_ref_before="rung1:a",
                           anchor_rung_ref_after="rung1:a",
                           allowed_family_refs=("two_word_phrases",),
                           selected_family_refs=("two_word_phrases",),
                           anchor_unchanged=False)


def test_a_decision_branch_cannot_carry_the_wrong_reason():
    with pytest.raises(AdaptationDecisionError):
        ActivityAdaptationDecision(
            activity_identity_ref="atpl1:x",
            branch=AdaptationBranch.CHANGE_THEME,
            reason=DecisionReason.NOT_YET_SUPPORTED_REPEATED_TOO_HARD,
            trigger_event_ids=("e1", "e2"), considered_event_ids=("e1", "e2"))
    with pytest.raises(AdaptationDecisionError):
        ActivityAdaptationDecision(
            activity_identity_ref="atpl1:x",
            branch=AdaptationBranch.CHANGE_THEME,
            reason=DecisionReason.REPEATED_DISENGAGEMENT,
            trigger_event_ids=("e1",), considered_event_ids=("e1",))


# ===========================================================================
# 6. reviewed content only
# ===========================================================================


def test_every_week_two_activity_is_reviewed_bank_content(a):
    a.observe(MY_TURN, "2026-11-17", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
    a.observe(MY_TURN, "2026-11-18", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
    week_two = a.release(WEEK_TWO_DAY)
    pool = {t.activity_template_id: t
            for t in a.bank.templates_for_families(FAMILIES_24)}
    document = a.repos.weekly_plan_snapshots.list_for_cycle(
        week_two.cycle_id)[0].document()
    for row in document["activities"]:
        template = pool[row["activity_template_id"]]
        assert template.source_tier in ("family_curated", "bucket_curated")
        assert template.activity_family_ref in FAMILIES_24
        assert row["content"] == {k: getattr(template, k)
                                  for k in row["content"]}, \
            "released content differs from the reviewed card"
    view = week_two.parent_week
    assert_no_forbidden_fields(view)
    for day in view["days"]:
        for activity in day["activities"]:
            assert activity["activity_ref"].startswith("pact1:")
            assert "activity_instance_ref" not in activity
            assert "activity_template_id" not in activity


def test_an_unreviewed_template_cannot_be_selected():
    bank = StaticActivityBank()
    pool = bank.templates_for_families(FAMILIES_24)
    with pytest.raises(ClinicalBoundaryViolation):
        verify_reviewed_and_bounded(["atpl1:" + "0" * 32], pool, FAMILIES_24)
    sentence = bank.templates_for_families(("sentence_building",))[0]
    with pytest.raises(ClinicalBoundaryViolation):
        verify_reviewed_and_bounded([sentence.activity_template_id],
                                    tuple(pool) + (sentence,), FAMILIES_24)
    # A generic-fallback card cannot even become a template.
    with pytest.raises(Exception):
        ActivityTemplate.build(
            activity_family_ref="two_word_phrases",
            source_tier="generic_fallback", source_pool_ref="x",
            card=pool[0].as_card())


def test_change_theme_refuses_rather_than_repeat_the_disengaged_theme():
    pool = StaticActivityBank().templates_for_families(("two_word_phrases",))
    previous = [t.activity_template_id for t in pool]   # every theme used
    decision = ActivityAdaptationDecision(
        activity_identity_ref=previous[0],
        branch=AdaptationBranch.CHANGE_THEME,
        reason=DecisionReason.REPEATED_DISENGAGEMENT,
        signal="didnt_want_to_try", trigger_event_ids=("e1", "e2"),
        considered_event_ids=("e1", "e2"))
    with pytest.raises(AdaptiveSelectionRefused):
        select_week_templates(decisions=[decision],
                              previous_template_ids=previous,
                              pool=pool, slots=3)


def test_change_theme_excludes_an_unused_card_that_shares_last_weeks_theme():
    """The reviewed bank has exactly one shared theme in these families —
    two `expressive_vocabulary_growth` cards are both "two-word carrier
    phrase". With one of them disengaged last week, the OTHER is unused and
    would be a fresh candidate, so only the theme rule can keep it out.

    Added after a mutation (theme exclusion removed) survived the composition
    tests: Week 1's real themes are all unique, so they could not tell.
    """
    pool = StaticActivityBank().templates_for_families(
        ("expressive_vocabulary_growth",))
    sharing = sorted(t.activity_template_id for t in pool
                     if " ".join(t.theme.split()).casefold()
                     == "two-word carrier phrase")
    assert len(sharing) == 2, "fixture assumption about the reviewed bank"
    disengaged, sibling = sharing
    decision = ActivityAdaptationDecision(
        activity_identity_ref=disengaged,
        branch=AdaptationBranch.CHANGE_THEME,
        reason=DecisionReason.REPEATED_DISENGAGEMENT,
        signal="didnt_want_to_try", trigger_event_ids=("e1", "e2"),
        considered_event_ids=("e1", "e2"))
    selection = select_week_templates(
        decisions=[decision], previous_template_ids=[disengaged],
        pool=pool, slots=len(pool))
    assert sibling not in selection.selected_template_ids
    assert disengaged not in selection.selected_template_ids
    assert selection.excluded_themes == ("two-word carrier phrase",)

    # Without a CHANGE_THEME decision the sibling IS eligible variation.
    maintained = select_week_templates(
        decisions=[], previous_template_ids=[disengaged],
        pool=pool, slots=len(pool))
    assert sibling in maintained.selected_template_ids


def test_maintain_vary_reuses_only_when_no_unused_alternative_exists():
    pool = StaticActivityBank().templates_for_families(("two_word_phrases",))
    ids = sorted(t.activity_template_id for t in pool)
    selection = select_week_templates(decisions=[],
                                      previous_template_ids=ids[:5],
                                      pool=pool, slots=3)
    assert ids[5] in selection.fresh_template_ids
    assert len(selection.selected_template_ids) == 3
    assert len(selection.reused_template_ids) == 2


# ===========================================================================
# 7. determinism
# ===========================================================================


def test_week_two_is_deterministic_across_independent_worlds():
    results = []
    for _ in range(2):
        world = Adaptive()
        world.observe(MY_TURN, "2026-11-17",
                      outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
        world.observe(MY_TURN, "2026-11-18",
                      outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
        week_two = world.release(WEEK_TWO_DAY)
        decision = world.decision(week_two)
        results.append((world.placed_ids(week_two),
                        [(d.activity_identity_ref, d.branch, d.reason)
                         for d in decision.activity_decisions],
                        decision.excluded_themes))
    assert results[0] == results[1]


def test_the_policy_is_independent_of_input_order(a):
    a.observe(MY_TURN, "2026-11-17", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
    a.observe(MY_TURN, "2026-11-18", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
    alignments = a.repos.alignments.list_for_cycle(a.week_one.cycle_id)
    events = a.repos.observation_events.list_for_cycle(a.week_one.cycle_id)
    forward = decide(alignments, events)
    backward = decide(list(reversed(alignments)), list(reversed(events)))
    assert forward == backward


# ===========================================================================
# 8. composition: snapshot, release, records, replay
# ===========================================================================


def test_week_two_composition_writes_everything_once_and_replays(a):
    a.observe(MY_TURN, "2026-11-17", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
    a.observe(MY_TURN, "2026-11-18", outcome=AttemptOutcome.DIDNT_WANT_TO_TRY)
    week_two = a.release(WEEK_TWO_DAY)

    cycle = a.weekly.get_cycle(a.provider, week_two.cycle_id)
    assert len(a.repos.weekly_plan_snapshots.list_for_cycle(cycle.cycle_id)) == 1
    (record,) = a.docs("pilot_adaptation_records")
    (decision_doc,) = a.docs("pilot_weekly_adaptation_decisions")
    assert record["rule_version"] == ADAPTATION_RULE_VERSION == P0B_RULE_VERSION
    assert record["from_cycle_id"] == a.week_one.cycle_id
    assert record["to_cycle_id"] == cycle.cycle_id
    assert cycle.adaptation_record_id == record["record_id"]
    assert decision_doc["adaptation_record_id"] == record["record_id"]
    assert decision_doc["decision_id"] == \
        weekly_adaptation_decision_id(cycle.cycle_id)
    assert sorted(record["evidence_event_ids"]) == \
        sorted(decision_doc["current_event_ids"])
    assert "p0b: change_theme=1; maintain_vary=2" in record["resulting_change"]

    # The family's view moves to Week 2 on a Week 2 date only.
    assert a.service.this_week(a.caregiver, CHILD,
                               local_date="2026-11-24") == week_two.parent_week
    assert a.service.this_week(a.caregiver, CHILD,
                               local_date="2026-11-18") == \
        a.week_one.parent_week

    # Replay: same week back, nothing new written, capacity cannot rewrite it.
    again = a.release(WEEK_TWO_DAY, capacity=1)
    assert again.created is False
    assert again.cycle_id == week_two.cycle_id
    assert again.parent_week == week_two.parent_week
    assert len(a.docs("pilot_adaptation_records")) == 1
    assert len(a.docs("pilot_weekly_adaptation_decisions")) == 1
    assert len(a.docs("pilot_weekly_cycles")) == 2


def test_the_decision_round_trips_through_the_codec(a):
    week_two = a.release(WEEK_TWO_DAY)
    stored = a.repos.adaptation_decisions.find_for_cycle(week_two.cycle_id)
    assert stored == a.repos.adaptation_decisions.get_by_id(stored.decision_id)
    assert a.repos.adaptation_decisions.list_for_child(CHILD) == [stored]


# ===========================================================================
# 9. Week 1 unchanged; the non-adaptive path is closed
# ===========================================================================


def test_week_one_replay_is_unchanged(a):
    again = a.release(WEEK_ONE_DAY)
    assert again.created is False
    assert again.parent_week == a.week_one.parent_week
    assert a.docs("pilot_adaptation_records") == []


def test_the_non_adaptive_week_two_path_is_closed(a):
    goals = a.service.releasable_goals(a.provider, CHILD)
    plan = a.service._resolve_plan(a.provider, CHILD, goals,
                                   timezone_of_record=TZ, cycle_month=MONTH,
                                   request_id="")
    with pytest.raises(NonAdaptiveWeekRefused):
        a.service._resolve_cycle(a.provider, plan, request_id="",
                                 local_date=WEEK_TWO_DAY)
    assert len(a.docs("pilot_weekly_cycles")) == 1


def test_an_unreleased_week_two_draft_is_refused_not_resumed(a):
    goals = a.service.releasable_goals(a.provider, CHILD)
    plan = a.service._resolve_plan(a.provider, CHILD, goals,
                                   timezone_of_record=TZ, cycle_month=MONTH,
                                   request_id="")
    a.clock.set(WEEK_TWO_DAY)
    a.weekly.create_cycle(a.provider, plan.focus_plan_id, sequence_in_month=2,
                          first_plan_date=WEEK_ONE_DAY,
                          predecessor_cycle_id=a.week_one.cycle_id)
    with pytest.raises(AdaptiveWeekIncomplete):
        a.release(WEEK_TWO_DAY)


def test_week_three_without_a_released_week_two_is_refused(a):
    with pytest.raises(AdaptiveWeekUnavailable):
        a.release("2026-11-30")
    assert len(a.docs("pilot_weekly_cycles")) == 1


def test_every_new_refusal_is_a_route_handled_release_error():
    """The release route renders `WeekOneReleaseError` as its constant 403."""
    for error in (NonAdaptiveWeekRefused, AdaptiveWeekUnavailable,
                  AdaptiveWeekIncomplete, ClinicalBoundaryRefused):
        assert issubclass(error, WeekOneReleaseError)


def test_the_policy_module_reaches_no_network_or_model():
    banned = {"requests", "httpx", "urllib", "urllib3", "http", "socket",
              "openai", "anthropic", "google", "vertexai", "aiohttp", "grpc"}
    for module in ("weekly/repeated_signal_policy.py",
                   "integration/adaptive_week.py",
                   "domain/adaptation_decision.py"):
        tree = ast.parse((PILOT_ROOT / module).read_text())
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(n.name.split(".")[0] for n in node.names)
            elif (isinstance(node, ast.ImportFrom) and node.module
                  and node.level == 0):
                found.add(node.module.split(".")[0])
        assert not found & banned, (module, found & banned)
