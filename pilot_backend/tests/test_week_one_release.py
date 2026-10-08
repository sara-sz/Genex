"""0.6A-2 — approved ClinicalGoal -> released Week 1 -> Parent "This week".

Dependency-pure where it can be: `FirestoreRepositories` over
`FakeDocumentStore`, the port's in-memory rung source, and the REAL reviewed
static activity bank — which is a lookup over a frozen JSON artifact and needs
no SDK, so using the real one costs nothing and makes the activity titles real.

Concurrency is proven on the Firestore emulator in
`pilot_runtime/tests/integration/test_week_one_emulator.py`; a dict is not
thread-safe, so no contention claim is made here.

## The chain under test

    F-B v2 suggestion -> provider approval -> ClinicalGoal + anchor
      -> monthly plan (create, allocate, activate)
      -> Week 1 cycle -> reviewed candidates -> existing allocator
      -> immutable snapshot -> release -> structured Parent projection

Nothing is seeded by hand: the suggestion comes from F-B v2, the goal from the
real approval path, and the activity content from the frozen bank. A hardcoded
activity string anywhere in this file would make the whole proof circular.
"""

from __future__ import annotations

import ast
import json
import pathlib
from datetime import datetime, timezone

import pytest

from pilot_backend.domain.canonical_rung import (
    ActivityFamilyBinding,
    CanonicalRung,
)
from pilot_backend.domain.goals import EditType, GoalKind, GoalRef, GoalStatus
from pilot_backend.domain.weekly_plan_document import (
    FORBIDDEN_PARENT_FIELDS,
    PARENT_ACTIVITY_CONTENT_FIELDS,
    PARENT_ACTIVITY_IDENTITY_FIELDS,
    PARENT_WEEK_VIEW_SCHEMA,
    WEEKLY_PLAN_DOCUMENT_SCHEMA,
    WeeklyPlanDocumentError,
    assert_no_forbidden_fields,
    assign_local_dates,
    parent_week_view,
)
from pilot_backend.integration.gold_standard_source import (
    InMemoryGoldStandardRungSource,
    RungTarget,
)
from pilot_backend.domain.weekly_cycle import WeeklyCycleError
from pilot_backend.integration.week_one_release import (
    CapacityRequired,
    GoalNotReleasable,
    WeekOneReleaseService,
)
from pilot_backend.planning.service import MonthlyPlanService
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
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
CYCLE_MONTH = "2026-10"
TZ = "America/New_York"

#: The founder's demo capacity. An EXPLICIT input at every call site in this
#: file — there is deliberately no module-level default a test could lean on.
DEMO_CAPACITY = 3

#: The real bank's families, bound to the real milestones.
FAMILY_BY_MILESTONE = {
    AT_24: "expressive_vocabulary_growth",
    BOOK: "book_object_naming",
    TWO_WORD: "two_word_phrases",
    VOCAB: "expressive_vocabulary_growth",
}


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


def _real_family_source(*, unserved_for_book=False):
    """A rung source whose anchors bind the REAL reviewed families."""
    book_families = (("a_family_with_no_reviewed_content",)
                     if unserved_for_book else ("book_object_naming",))
    return InMemoryGoldStandardRungSource(
        rungs=(_rung(AT_24, 24, (FAMILY_BY_MILESTONE[AT_24],)),
               _rung(BOOK, 30, book_families),
               _rung(TWO_WORD, 30, (FAMILY_BY_MILESTONE[TWO_WORD],)),
               _rung(VOCAB, 30, (FAMILY_BY_MILESTONE[VOCAB],))),
        unmappable=(RungTarget(domain_key=SUPPORTED_DOMAIN,
                               source_rung_months=30,
                               milestone_text=PRONOUNS),))


class Chain:
    """The whole product path for one fictional child, assembled once."""

    def __init__(self, *, deficits=(BOOK,), unserved_for_book=False,
                 bank=None):
        states = dict(mixed(**{m: "not_demonstrated" for m in deficits}))
        states[PRONOUNS] = "unknown"
        self.world = World(
            source=_real_family_source(unserved_for_book=unserved_for_book),
            projection=_projection(states_30=states))
        self.repos = self.world.repos
        self.goals = self.world.goals
        self.principal = self.world.principal()
        self.plans = MonthlyPlanService(repos=self.repos,
                                        recorder=self.world.recorder,
                                        now=lambda: NOW)
        self.weekly = WeeklyService(repos=self.repos,
                                   recorder=self.world.recorder,
                                   now=lambda: NOW)
        self.bank = bank if bank is not None else StaticActivityBank()
        self.service = WeekOneReleaseService(
            repos=self.repos, goals=self.goals, plans=self.plans,
            weekly=self.weekly, activity_bank=self.bank, now=lambda: NOW)
        self.suggestions = self.world.generate()

    def approve(self, index=0):
        """Hannah approves one F-B v2 suggestion VERBATIM. The real path."""
        suggestion_id = self.suggestions.generated[index].suggestion_ids[0]
        return self.goals.approve_clinical_goal(
            self.principal, CHILD, edit_type=EditType.ACCEPTED_VERBATIM,
            suggestion_id=suggestion_id)

    def release(self, capacity=DEMO_CAPACITY, principal=None,
                local_date=None):
        return self.service.release_week_one(
            principal or self.principal, CHILD,
            family_declared_capacity=capacity,
            timezone_of_record=TZ, cycle_month=CYCLE_MONTH,
            local_date=local_date)

    def this_week(self, principal=None, local_date=None):
        return self.service.this_week(principal or self.principal, CHILD,
                                      local_date=local_date)

    def docs(self, collection):
        return [d for _i, d in self.repos.store.list_all(collection)]


# ===========================================================================
# the happy path: one approved mappable goal -> a released week
# ===========================================================================


def test_an_approved_mappable_goal_releases_week_one_with_three_activities():
    """The founder's demo. Capacity 3 over six reviewed book-naming cards."""
    chain = Chain()
    goal = chain.approve()
    assert goal.status is GoalStatus.ACTIVE

    outcome = chain.release()
    assert outcome.created is True
    assert outcome.activity_count == DEMO_CAPACITY
    assert outcome.goal_ids == (goal.clinical_goal_id,)
    assert outcome.released_at is not None

    week = outcome.parent_week
    assert week["view_schema"] == PARENT_WEEK_VIEW_SCHEMA
    titles = [a["title"] for day in week["days"] for a in day["activities"]]
    assert len(titles) == DEMO_CAPACITY
    assert len(set(titles)) == DEMO_CAPACITY, "an activity was placed twice"

    # Every placement is aligned, and nothing is attempted or observed yet.
    assert len(chain.weekly.list_alignments(chain.principal,
                                            outcome.cycle_id)) == 3
    assert chain.weekly.list_coverage_gaps(chain.principal,
                                           outcome.cycle_id) == []
    assert chain.weekly.list_observations(chain.principal,
                                          outcome.cycle_id) == []


def test_the_count_follows_capacity_and_nothing_else():
    """1 coverage floor + (capacity - 1) emphasis, from the EXISTING allocator.

    The clock is pinned to Wed Oct 7 2026, so the STARTER week is Oct 7-11 —
    five days under the frozen Genex rule. The sweep therefore stops at 5; the
    day-count interaction has its own test below.
    """
    for capacity, expected in ((1, 1), (2, 2), (3, 3), (5, 5)):
        chain = Chain()
        chain.approve()
        outcome = chain.release(capacity=capacity)
        assert outcome.activity_count == expected, (capacity, expected)


def test_capacity_beyond_the_cycles_day_count_refuses_rather_than_doubling_up():
    """A REAL interaction, surfaced rather than papered over.

    A first plan on SUNDAY Oct 11 2026 is a one-day starter week under the frozen
    rule, so a declared capacity of 3 cannot be rendered one-per-day. The release
    REFUSES and writes no snapshot, instead of silently placing three activities
    on one day — which would be a density decision nobody approved.
    """
    chain = Chain()
    chain.approve()
    with pytest.raises(WeeklyPlanDocumentError):
        chain.release(capacity=3, local_date="2026-10-11")
    assert chain.docs("pilot_weekly_plan_snapshots") == []


def test_scheduled_is_not_attempted_or_completed_or_improvement():
    """At release: scheduled > 0 and every evidence count is zero."""
    chain = Chain()
    chain.approve()
    outcome = chain.release()

    assert outcome.activity_count > 0
    assert chain.weekly.list_observations(chain.principal,
                                          outcome.cycle_id) == []
    for collection in ("pilot_observation_events", "pilot_rtm_episodes",
                       "pilot_rtm_periods", "pilot_adaptation_records"):
        assert chain.docs(collection) == [], collection
    # The Parent view carries no outcome vocabulary at all.
    blob = json.dumps(outcome.parent_week)
    for forbidden in ("attempted", "completed", "improvement", "progress",
                      "did_it", "outcome", "score"):
        assert forbidden not in blob, forbidden


def test_every_activity_comes_from_the_approved_goals_family():
    """No generic fallback and no cross-family substitution."""
    chain = Chain()
    goal = chain.approve()
    outcome = chain.release()

    anchor = chain.goals.goal_anchor(
        chain.principal, GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id))
    bound = {b.family_ref for b in anchor.rung.family_bindings}
    assert bound == {"book_object_naming"}

    bank_templates = {t.activity_template_id: t
                      for t in chain.bank.templates_for_families(sorted(bound))}
    # Provenance is read from the INTERNAL snapshot: the Parent view no longer
    # carries `activity_template_id`, so the join goes through the stored
    # document, keyed by the opaque instance ref the family does see.
    document = chain.repos.weekly_plan_snapshots.list_for_cycle(
        outcome.cycle_id)[0].document()
    by_ref = {row["activity_ref"]: row for row in document["activities"]}
    for day in outcome.parent_week["days"]:
        for activity in day["activities"]:
            row = by_ref[activity["activity_ref"]]
            template = bank_templates[row["activity_template_id"]]
            assert template.activity_family_ref in bound
            # The content is the REVIEWED content, byte for byte.
            assert activity["title"] == template.title
            assert activity["instructions"] == template.instructions
            # And it is a curated tier, never a generic fallback.
            assert template.source_tier in ("family_curated", "bucket_curated")


def test_multiple_mapped_deficits_each_get_activities_from_their_own_family():
    """Two approved goals, two families, one week — the allocator's own split."""
    chain = Chain(deficits=(BOOK, TWO_WORD))
    assert len(chain.suggestions.generated) == 2
    first = chain.approve(0)
    second = chain.approve(1)

    outcome = chain.release(capacity=4)
    assert set(outcome.goal_ids) == {first.clinical_goal_id,
                                     second.clinical_goal_id}
    goal_ids = {a["goal_id"] for day in outcome.parent_week["days"]
                for a in day["activities"]}
    # The coverage floor guarantees BOTH goals appear; neither is squeezed out.
    assert goal_ids == set(outcome.goal_ids)


# ===========================================================================
# the goal gate: fail closed
# ===========================================================================


def test_release_refuses_with_no_approved_goal():
    """A GoalSuggestion is never an input. Only approval unlocks a week."""
    chain = Chain()
    assert chain.suggestions.generated, "the fixture produced no suggestion"
    with pytest.raises(GoalNotReleasable):
        chain.release()
    assert chain.docs("pilot_weekly_cycles") == []
    assert chain.docs("pilot_weekly_plan_snapshots") == []


def test_release_refuses_an_unmappable_goal():
    """`pronouns` is canonical but has no reconciled activity family.

    F-B v2 reports it as an unsupported target and never generates a
    suggestion for it, so this test approves a goal with NO anchor at all —
    which is the same fail-closed condition from the release path's point of
    view and is the one a hand-approved goal would hit.
    """
    chain = Chain()
    goal = chain.goals.approve_clinical_goal(
        chain.principal, CHILD, edit_type=EditType.AUTHORED_FRESH,
        text="A goal with no canonical anchor at all.",
        reason="fictional: authored without a suggestion, so no anchor exists")
    ref = GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id)
    assert chain.goals.goal_anchor(chain.principal, ref) is None
    assert chain.goals.is_activity_mappable(chain.principal, ref) is False

    with pytest.raises(GoalNotReleasable):
        chain.release()
    assert chain.docs("pilot_weekly_cycles") == []


def test_release_refuses_when_the_goals_family_has_no_reviewed_content():
    """`require_all_families_served` refuses BEFORE anything is written."""
    chain = Chain(unserved_for_book=True)
    chain.approve()
    from pilot_backend.integration.activity_bank import FamilyNotServed

    with pytest.raises(FamilyNotServed):
        chain.release()
    assert chain.docs("pilot_weekly_plan_snapshots") == []


def test_release_refuses_a_closed_goal():
    """An inactive goal cannot drive a week.

    Closed through the DOMAIN transition plus the repository's own `update`,
    because no service method closes a clinical goal yet. That is a gap in the
    goal lifecycle rather than in this slice, and constructing the state here is
    the only way to prove the release gate reads `status`.
    """
    chain = Chain()
    goal = chain.approve()
    # RETIRED, which is this domain's terminal status — there is no CLOSED.
    chain.repos.clinical_goals.update(
        goal.with_status(GoalStatus.RETIRED, now=NOW))
    assert chain.repos.clinical_goals.get_by_id(
        goal.clinical_goal_id).status is GoalStatus.RETIRED

    with pytest.raises(GoalNotReleasable):
        chain.release()
    assert chain.docs("pilot_weekly_cycles") == []


def test_an_unmappable_goal_does_not_block_a_mappable_sibling():
    """The same rule F-B v2 follows, restated at the release boundary."""
    chain = Chain()
    mappable = chain.approve()
    chain.goals.approve_clinical_goal(
        chain.principal, CHILD, edit_type=EditType.AUTHORED_FRESH,
        text="An unanchored goal that must not block the anchored one.",
        reason="fictional: authored without a suggestion, so no anchor exists")

    outcome = chain.release()
    assert outcome.goal_ids == (mappable.clinical_goal_id,)
    assert outcome.activity_count == DEMO_CAPACITY


# ===========================================================================
# capacity: explicit input, never a default
# ===========================================================================


@pytest.mark.parametrize("capacity", [None, 0, -1, True, 2.5, "3"])
def test_absent_or_invalid_capacity_refuses_and_writes_nothing(capacity):
    """No default, no fallback, no inference.

    `True` matters: `bool` is an `int`, and would otherwise become capacity 1.
    """
    chain = Chain()
    chain.approve()
    with pytest.raises(CapacityRequired):
        chain.release(capacity=capacity)
    assert chain.docs("pilot_weekly_cycles") == []
    assert chain.docs("pilot_capacity_ledgers") == []


def test_no_capacity_default_exists_anywhere_in_the_planning_policy():
    """Structural. The founder's rule: do NOT add a capacity default.

    Checked over the policy module's AST so it cannot be satisfied by a passing
    run, and over the release module's source so no literal sneaks in.
    """
    from pilot_backend.domain import planning_policy

    fields = set(getattr(planning_policy.CURRENT_PLANNING_POLICY,
                         "__dataclass_fields__", {}))
    assert not [f for f in fields if "capacity" in f], fields

    source = (PILOT_ROOT / "integration/week_one_release.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:]
    # No integer literal is ever assigned to a capacity-shaped name, and the
    # parameter has no default.
    import inspect

    signature = inspect.signature(
        WeekOneReleaseService.release_week_one)
    parameter = signature.parameters["family_declared_capacity"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_the_ledger_records_the_declared_capacity_verbatim():
    """`family_declared_capacity` keeps its meaning: what the family can carry."""
    chain = Chain()
    chain.approve()
    outcome = chain.release()
    ledger = chain.weekly.capacity_for(chain.principal, outcome.cycle_id)
    assert ledger.family_declared_capacity == DEMO_CAPACITY
    assert ledger.allocated_by_planner == DEMO_CAPACITY
    assert ledger.clinician_added == 0
    assert ledger.overage == 0
    assert ledger.is_over_capacity is False


# ===========================================================================
# draft invisible, released visible
# ===========================================================================


def test_the_parent_read_returns_nothing_before_release():
    chain = Chain()
    chain.approve()
    assert chain.this_week() is None


def test_a_draft_cycle_is_invisible_to_the_parent_read():
    """A cycle exists and is allocated; it is NOT released, so nothing is shown.

    Built by driving the real transitions up to — and stopping before — release,
    which is the state a crashed or abandoned release leaves behind.
    """
    chain = Chain()
    goals = chain.service.releasable_goals(chain.principal, CHILD)
    chain.approve()
    goals = chain.service.releasable_goals(chain.principal, CHILD)
    plan = chain.service._resolve_plan(
        chain.principal, CHILD, goals, timezone_of_record=TZ,
        cycle_month=CYCLE_MONTH, request_id="")
    cycle = chain.service._resolve_cycle(chain.principal, plan, request_id="")
    templates = chain.service._candidate_templates(goals)
    chain.service._allocate(chain.principal, cycle, goals, templates,
                            capacity=DEMO_CAPACITY, request_id="")

    assert cycle.is_released is False
    assert chain.weekly.list_alignments(chain.principal, cycle.cycle_id)
    # Allocated, but never released: the family sees nothing.
    assert chain.this_week() is None


def test_the_released_week_is_visible_and_structured():
    chain = Chain()
    chain.approve()
    outcome = chain.release()

    week = chain.this_week()
    assert week is not None
    assert week == outcome.parent_week
    assert sorted(week) == ["days", "goals", "view_schema", "week"]
    assert sorted(week["week"]) == ["cycle_id", "ends_on", "released_at",
                                    "starts_on"]
    # Every day of the cycle is listed, including days with no activity, so a
    # family cannot mistake "nothing scheduled" for "the app lost it".
    assert len(week["days"]) >= DEMO_CAPACITY
    assert all(sorted(day) == ["activities", "local_date"]
               for day in week["days"])


def test_the_parent_view_carries_only_reviewed_content_and_identifiers():
    chain = Chain()
    chain.approve()
    week = chain.release().parent_week

    for day in week["days"]:
        for activity in day["activities"]:
            assert sorted(activity) == sorted(
                PARENT_ACTIVITY_IDENTITY_FIELDS
                + PARENT_ACTIVITY_CONTENT_FIELDS)
    # `duration_minutes` and `why` do not exist in the reviewed source and are
    # NOT invented; `theme` and `group_play_line` are authoring metadata.
    blob = json.dumps(week)
    for absent in ("duration_minutes", "why", "theme", "group_play_line"):
        assert absent not in blob, absent
    assert_no_forbidden_fields(week)


def test_the_template_id_is_internal_provenance_and_never_reaches_the_parent():
    """Founder review, 0.6A-2. It stays in the snapshot; it does not cross.

    Three assertions, because the field could leak at three different depths:
    the identity allowlist, the rendered activity, and the serialised payload.
    """
    chain = Chain()
    chain.approve()
    outcome = chain.release()

    assert "activity_template_id" not in PARENT_ACTIVITY_IDENTITY_FIELDS
    assert "activity_template_id" in FORBIDDEN_PARENT_FIELDS

    for day in outcome.parent_week["days"]:
        for activity in day["activities"]:
            assert "activity_template_id" not in activity
            assert "activity_family_ref" not in activity
            # The OPAQUE handle is present — it is the product's reference —
            # and the allocator's raw instance ref, which embeds the template
            # digest, is not.
            assert activity["activity_ref"].startswith("pact1:")
            assert "activity_instance_ref" not in activity

    blob = json.dumps(outcome.parent_week)
    assert "activity_template_id" not in blob
    assert "atpl1:" not in blob, "a template digest leaked into the Parent view"
    # And the same holds for the route-level read.
    assert "atpl1:" not in json.dumps(chain.this_week())

    # It is STILL in the immutable snapshot, which is the point.
    document = chain.repos.weekly_plan_snapshots.list_for_cycle(
        outcome.cycle_id)[0].document()
    assert all(row["activity_template_id"].startswith("atpl1:")
               for row in document["activities"])
    assert all(row["goal_version_id"] for row in document["goals"])


def test_no_internal_planning_data_reaches_the_parent_view():
    chain = Chain()
    goal = chain.approve()
    outcome = chain.release()
    blob = json.dumps(outcome.parent_week)

    for forbidden in FORBIDDEN_PARENT_FIELDS:
        assert f'"{forbidden}"' not in blob, forbidden
    # The canonical anchor's identity in particular.
    anchor = chain.goals.goal_anchor(
        chain.principal, GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id))
    assert anchor.rung.rung_ref not in blob
    assert anchor.rung.track_ref not in blob
    # And the opaque v1-era document key is not the contract.
    assert "resolved_plan_document" not in blob


# ===========================================================================
# provenance and immutability
# ===========================================================================


def test_the_snapshot_keeps_the_template_identity_beside_the_content():
    """Item 9. Materialising the words must not lose which template made them."""
    chain = Chain()
    chain.approve()
    outcome = chain.release()

    snapshots = chain.repos.weekly_plan_snapshots.list_for_cycle(
        outcome.cycle_id)
    assert len(snapshots) == 1
    document = snapshots[0].document()
    assert document["document_schema"] == WEEKLY_PLAN_DOCUMENT_SCHEMA

    served = {t.activity_template_id
              for t in chain.bank.templates_for_families(
                  ["book_object_naming"])}
    for row in document["activities"]:
        assert row["activity_template_id"] in served
        assert row["activity_family_ref"] == "book_object_naming"
        assert row["content"]["title"]
    # The exact GoalVersion in force at release is frozen into the document.
    goal_rows = document["goals"]
    assert goal_rows and all(row["goal_version_id"] for row in goal_rows)


def test_a_later_goal_revision_does_not_rewrite_the_released_week():
    """Item 12. History says what it said."""
    chain = Chain()
    goal = chain.approve()
    outcome = chain.release()
    before = json.dumps(chain.this_week(), sort_keys=True)
    original_version = goal.current_version_id

    ref = GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id)
    chain.goals.revise_goal(
        chain.principal, ref,
        text="A revised wording the released week must not adopt.",
        reason="clinician refinement")
    revised = chain.goals.get_goal(chain.principal, ref)
    assert revised.current_version_id != original_version
    assert "revised wording" in chain.goals.current_text(chain.principal, ref)

    # The released week is byte-identical, and still carries the OLD text.
    after = chain.this_week()
    assert json.dumps(after, sort_keys=True) == before
    assert "revised wording" not in json.dumps(after)
    snapshot = chain.repos.weekly_plan_snapshots.list_for_cycle(
        outcome.cycle_id)[0]
    assert snapshot.document()["goals"][0]["goal_version_id"] == original_version


def test_the_snapshot_repository_offers_no_way_to_rewrite_one():
    repo = Chain().repos.weekly_plan_snapshots
    for forbidden in ("update", "set", "delete", "overwrite", "replace"):
        assert not hasattr(repo, forbidden), forbidden


# ===========================================================================
# replay
# ===========================================================================


def test_a_replay_returns_the_existing_released_week_and_writes_nothing():
    chain = Chain()
    chain.approve()
    first = chain.release()
    counts = (len(chain.docs("pilot_weekly_cycles")),
              len(chain.docs("pilot_weekly_plan_snapshots")),
              len(chain.docs("pilot_suggestion_anchors")),
              len(chain.docs("pilot_capacity_ledgers")))

    second = chain.release()
    assert second.created is False
    assert second.cycle_id == first.cycle_id
    assert second.snapshot_id == first.snapshot_id
    assert second.parent_week == first.parent_week
    assert (len(chain.docs("pilot_weekly_cycles")),
            len(chain.docs("pilot_weekly_plan_snapshots")),
            len(chain.docs("pilot_suggestion_anchors")),
            len(chain.docs("pilot_capacity_ledgers"))) == counts


def test_a_replay_with_a_different_capacity_does_not_rewrite_history():
    """A released week is frozen. A later capacity belongs to a FUTURE cycle."""
    chain = Chain()
    chain.approve()
    first = chain.release(capacity=3)
    assert first.activity_count == 3

    again = chain.release(capacity=6)
    assert again.created is False
    assert again.activity_count == 3, "the released week was rewritten"
    assert again.parent_week == first.parent_week
    ledger = chain.weekly.capacity_for(chain.principal, first.cycle_id)
    assert ledger.family_declared_capacity == 3
    assert len(chain.docs("pilot_capacity_ledgers")) == 1


def test_the_week_is_deterministic_across_two_independent_chains():
    """Same inputs, same selected activities — the whole point of the bank."""
    titles = []
    for _attempt in range(2):
        chain = Chain()
        chain.approve()
        week = chain.release().parent_week
        titles.append([(day["local_date"], activity["title"])
                       for day in week["days"]
                       for activity in day["activities"]])
    assert titles[0] == titles[1]


# ===========================================================================
# the day-rendering rule
# ===========================================================================


def test_the_frozen_genex_starter_week_invariant():
    """RESTORED FROZEN INVARIANT — do not lose this again.

    The Genex starter-week rule, frozen in
    `genex-parent/api/planning_period.py` (`compute_plan_period` /
    `next_week_available_from`) and declared-but-unwired in this layer as
    `PartialReason.PLAN_ACTIVATED_MIDWEEK`:

        the child's FIRST weekly plan starts on the actual first-plan date and
        runs through that SUNDAY; it is an explicit partial starter week when
        onboarding is midweek; week 2 begins the FOLLOWING MONDAY; from week 2
        onward cycles are Monday-Sunday.

    The failure this prevents is specific: anchoring cycle 1 to the first of the
    MONTH backdates a family's activities to before they entered the care loop.
    """
    from pilot_backend.domain.weekly_cycle import (
        PartialReason,
        starter_cycle_bounds,
        starter_sequence_for,
    )

    # The founder's own examples, verbatim.
    assert starter_cycle_bounds("2026-10-07", 1)[:2] == ("2026-10-07",
                                                        "2026-10-11")
    assert starter_cycle_bounds("2026-10-08", 1)[:2] == ("2026-10-08",
                                                        "2026-10-11")
    # A Sunday first plan is a ONE-DAY starter week.
    assert starter_cycle_bounds("2026-10-11", 1)[:2] == ("2026-10-11",
                                                        "2026-10-11")
    # Week 2 begins the following Monday, whatever the starter was.
    for first in ("2026-10-07", "2026-10-08", "2026-10-11"):
        assert starter_cycle_bounds(first, 2)[:2] == ("2026-10-12",
                                                      "2026-10-18")
        assert starter_cycle_bounds(first, 3)[:2] == ("2026-10-19",
                                                      "2026-10-25")

    # A midweek start is PARTIAL, and says why with the declared reason.
    for first in ("2026-10-07", "2026-10-08", "2026-10-11"):
        _s, _e, partial, reason = starter_cycle_bounds(first, 1)
        assert partial is True, first
        assert reason is PartialReason.PLAN_ACTIVATED_MIDWEEK, first

    # A MONDAY first plan is a full week and NOT partial.
    starts, ends, partial, reason = starter_cycle_bounds("2026-10-05", 1)
    assert (starts, ends) == ("2026-10-05", "2026-10-11")
    assert partial is False and reason is None

    # Established Monday-Sunday behaviour thereafter, for several weeks.
    for sequence, expected in ((2, ("2026-10-12", "2026-10-18")),
                               (3, ("2026-10-19", "2026-10-25")),
                               (4, ("2026-10-26", "2026-11-01")),
                               (5, ("2026-11-02", "2026-11-08"))):
        assert starter_cycle_bounds("2026-10-07", sequence)[:2] == expected
        assert starter_cycle_bounds("2026-10-07", sequence)[2] is False

    # Coverage, and the refusal that stops backdating.
    for day, sequence in (("2026-10-07", 1), ("2026-10-09", 1),
                          ("2026-10-11", 1), ("2026-10-12", 2),
                          ("2026-10-18", 2), ("2026-10-19", 3)):
        assert starter_sequence_for("2026-10-07", day) == sequence, day
    with pytest.raises(WeeklyCycleError):
        starter_sequence_for("2026-10-07", "2026-10-06")


def test_no_activity_is_ever_dated_before_the_first_plan():
    """The whole point of the invariant, asserted on a released week."""
    chain = Chain()
    chain.approve()
    outcome = chain.release()      # clock pinned to Wed Oct 7

    week = outcome.parent_week["week"]
    assert (week["starts_on"], week["ends_on"]) == ("2026-10-07",
                                                    "2026-10-11")
    dates = [day["local_date"] for day in outcome.parent_week["days"]
             for _a in day["activities"]]
    assert dates, "no activity was placed"
    assert min(dates) >= "2026-10-07", dates
    assert max(dates) <= "2026-10-11", dates
    # And the old month-anchored answer is gone for good.
    assert "2026-10-05" not in dates and "2026-10-06" not in dates


def test_the_starter_cycle_is_recorded_as_a_partial_plan_activated_midweek():
    from pilot_backend.domain.weekly_cycle import PartialReason

    chain = Chain()
    chain.approve()
    outcome = chain.release()
    cycle = chain.weekly.get_cycle(chain.principal, outcome.cycle_id)
    assert cycle.sequence_in_month == 1
    assert cycle.starts_on == "2026-10-07"
    assert cycle.ends_on == "2026-10-11"
    assert cycle.is_partial is True
    assert cycle.partial_reason is PartialReason.PLAN_ACTIVATED_MIDWEEK
    assert cycle.day_count == 5


def test_a_replay_does_not_shift_the_already_created_starter_cycle():
    """Even when the replay happens on a LATER day of the starter week."""
    chain = Chain()
    chain.approve()
    first = chain.release(local_date="2026-10-07")
    assert first.parent_week["week"]["starts_on"] == "2026-10-07"

    for later in ("2026-10-08", "2026-10-09", "2026-10-11"):
        again = chain.release(local_date=later)
        assert again.created is False
        assert again.cycle_id == first.cycle_id
        assert again.snapshot_id == first.snapshot_id
        assert again.parent_week == first.parent_week
    cycles = chain.weekly.list_cycles(chain.principal, first.focus_plan_id)
    assert [c.starts_on for c in cycles] == ["2026-10-07"]


def test_the_following_cycle_begins_the_next_monday():
    """Week 2 is created Monday-anchored, and the starter is left alone.

    Driven through the orchestrator's own cycle resolution with the clock moved
    into week 2, so this is the real path rather than a bounds calculation.
    """
    chain = Chain()
    chain.approve()
    first = chain.release(local_date="2026-10-07")

    goals = chain.service.releasable_goals(chain.principal, CHILD)
    plan = chain.service._resolve_plan(
        chain.principal, CHILD, goals, timezone_of_record=TZ,
        cycle_month=CYCLE_MONTH, request_id="")
    week_two = chain.service._resolve_cycle(
        chain.principal, plan, request_id="", local_date="2026-10-14")

    assert week_two.sequence_in_month == 2
    assert (week_two.starts_on, week_two.ends_on) == ("2026-10-12",
                                                      "2026-10-18")
    assert week_two.is_partial is False
    assert week_two.partial_reason is None
    assert week_two.cycle_id != first.cycle_id

    # The released starter week is untouched, and still what the family saw.
    starter = chain.weekly.get_cycle(chain.principal, first.cycle_id)
    assert (starter.starts_on, starter.ends_on) == ("2026-10-07",
                                                    "2026-10-11")
    assert starter.is_released is True
    # Week 2 is a DRAFT, so the family still sees only the released starter.
    assert week_two.is_released is False
    assert chain.service.this_week(
        chain.principal, CHILD,
        local_date="2026-10-09") == first.parent_week


def test_activities_are_laid_out_one_per_day_from_the_cycle_start():
    chain = Chain()
    chain.approve()
    week = chain.release().parent_week
    populated = [day for day in week["days"] if day["activities"]]
    assert len(populated) == DEMO_CAPACITY
    assert all(len(day["activities"]) == 1 for day in populated)
    # Consecutive, starting on the first day of the cycle.
    assert [day["local_date"] for day in populated] == [
        day["local_date"] for day in week["days"][:DEMO_CAPACITY]]


def test_more_activities_than_days_refuses_rather_than_doubling_up():
    """A second activity on one day would be a density decision nobody approved."""
    with pytest.raises(WeeklyPlanDocumentError):
        assign_local_dates(("2026-10-01", "2026-10-02"), 3)
    assert assign_local_dates(("2026-10-01", "2026-10-02"), 2) == (
        "2026-10-01", "2026-10-02")
    assert assign_local_dates(("2026-10-01",), 0) == ()


def test_the_parent_view_refuses_a_foreign_document():
    with pytest.raises(WeeklyPlanDocumentError):
        parent_week_view({"document_schema": "someone-elses-plan-v1"})


# ===========================================================================
# no LLM, no plan machinery beyond this slice
# ===========================================================================


def test_the_release_path_reaches_no_model_client():
    for name in ("integration/week_one_release.py",
                 "domain/weekly_plan_document.py"):
        tree = ast.parse((PILOT_ROOT / name).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not imported & {"openai", "anthropic", "requests", "httpx"}, name


def test_release_creates_no_week_two_and_no_rtm():
    chain = Chain()
    chain.approve()
    outcome = chain.release()
    cycles = chain.weekly.list_cycles(chain.principal, outcome.focus_plan_id)
    # EXACTLY ONE cycle — the STARTER one. A first plan on Oct 7 anchors cycle 1
    # at Oct 7-11 under the frozen rule, and no further week is generated.
    assert [c.sequence_in_month for c in cycles] == [1]
    assert cycles[0].starts_on == "2026-10-07"
    assert cycles[0].ends_on == "2026-10-11"
    assert cycles[0].is_partial is True
    for collection in ("pilot_rtm_episodes", "pilot_rtm_periods",
                       "pilot_adaptation_records", "pilot_defer_records"):
        assert chain.docs(collection) == [], collection


def test_goals_this_month_remains_independently_readable():
    """Item 13. Activating a plan must not obscure the goal read."""
    chain = Chain()
    goal = chain.approve()
    before = chain.goals.list_clinical_goals(chain.principal, CHILD)
    assert [g.clinical_goal_id for g in before] == [goal.clinical_goal_id]

    chain.release()
    after = chain.goals.list_clinical_goals(chain.principal, CHILD)
    assert [g.clinical_goal_id for g in after] == [goal.clinical_goal_id]
    assert after[0].status is GoalStatus.ACTIVE
    # And the goal's text is still reachable without going through the week.
    assert chain.goals.current_text(
        chain.principal, GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id))
