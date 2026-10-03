"""0.5E-A — the canonical goal anchor and activity mappability.

Self-contained: builds its own family, clinician and child so it does not
inherit any pre-0.5E-A fixture's assumption that an unanchored goal may be
allocated.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth.resolver import resolve_principal
from pilot_backend.auth.verifiers import VerifiedToken
from pilot_backend.connections import ProviderConnectionService
from pilot_backend.domain.canonical_rung import (
    ActivityFamilyBinding,
    CanonicalRung,
    RungError,
    compute_rung_ref,
    compute_track_ref,
    normalize_milestone_text,
)
from pilot_backend.domain.connections import CaregiverChildConnection
from pilot_backend.domain.entities import Caregiver, Child, Practice
from pilot_backend.domain.enums import CaregiverRelationship, ProviderDiscipline
from pilot_backend.domain.goal_anchor import (
    ClinicalGoalAnchor,
    SuggestionCanonicalAnchor,
)
from pilot_backend.domain.goals import EditType, GoalKind, GoalRef
from pilot_backend.goals.errors import GoalValidationError
from pilot_backend.goals.service import GoalService
from pilot_backend.goals.suggestion_engine import (
    EvidenceSource,
    ObservationSnapshot,
    ObservedDomain,
)
from pilot_backend.persistence import FakeDocumentStore, FirestoreRepositories
from pilot_backend.planning.service import MonthlyPlanService
from pilot_backend.provisioning import provision_provider_record

MONTH = datetime.now(timezone.utc).strftime("%Y-%m")
TAXONOMY = "activity_family_taxonomy_v1"
BASELINE = "parent-2.4-functional-baseline-v1"

#: A canonical rung standing in for a real Gold Standard row. Values are
#: shaped like the frozen workbook's (an 18-month talking rung with two valid
#: families) without copying its text into this package.
RUNG_DOMAIN = "talking_and_communicating"
RUNG_MONTHS = 18
RUNG_TEXT = "Fictional rung text for anchor tests"
RUNG_SUBDOMAIN = "expressive_language"


def a_rung(*, domain=RUNG_DOMAIN, months=RUNG_MONTHS, text=RUNG_TEXT,
           families=None, track=(RUNG_SUBDOMAIN,), track_families=()):
    if families is None:
        families = [ActivityFamilyBinding("expressive_three_words", (domain,)),
                    ActivityFamilyBinding("expressive_first_words", (domain,))]
    return CanonicalRung.build(
        domain_key=domain, source_rung_months=months, milestone_text=text,
        subdomain=RUNG_SUBDOMAIN, family_bindings=families,
        track_subdomains=track, track_families=track_families,
        taxonomy_version=TAXONOMY, baseline_version=BASELINE)


@pytest.fixture()
def world():
    repos = FirestoreRepositories(FakeDocumentStore())
    recorder = AuditRecorder(repos.audit_events, environment="test")
    practice = repos.practices.create(Practice.create("Practice-Anchor"))
    caregiver = repos.caregivers.create(
        Caregiver.create("Caregiver-Anchor", auth_subject="subj-cg"))
    child = repos.children.create(Child.create(actor_id=caregiver.caregiver_id))
    repos.caregiver_child.connect(CaregiverChildConnection.create(
        caregiver.caregiver_id, child.child_id, CaregiverRelationship.PARENT,
        actor_id=caregiver.caregiver_id))
    provider = provision_provider_record(
        repos, auth_subject="subj-prov", practice_id=practice.practice_id,
        discipline=ProviderDiscipline.SLP,
        display_name="Provider-Anchor").provider

    def principal(subject):
        return resolve_principal(VerifiedToken(subject=subject), repos)

    connections = ProviderConnectionService(repos=repos, recorder=recorder)
    pending = connections.invite_provider(
        principal("subj-cg"), child.child_id, provider.provider_id)
    connections.accept_invitation(principal("subj-prov"), pending.connection_id)
    connections.assign_managing_clinician(
        principal("subj-cg"), child.child_id, provider.provider_id)

    class Bundle:
        pass

    b = Bundle()
    b.repos = repos
    b.child = child.child_id
    b.clinician = principal("subj-prov")
    b.caregiver = principal("subj-cg")
    b.goals = GoalService(repos=repos, recorder=recorder)
    b.plans = MonthlyPlanService(repos=repos, recorder=recorder)
    return b


def generate(world, *, rung=None, domain=RUNG_DOMAIN):
    """Generate one suggestion, optionally carrying canonical provenance."""
    snapshot = ObservationSnapshot(
        child_id=world.child, cycle_month=MONTH,
        domains=(ObservedDomain(
            domain_key=domain, answered=True,
            evidence_source=EvidenceSource.EXPLICIT_SELECTION,
            explicitly_selected=True, functional_baseline_area="area",
            observed_level="level", canonical_rung=rung),))
    return world.goals.generate_suggestions(
        world.clinician, world.child, snapshot)[0]


def approve(world, suggestion, *, edit_type=EditType.ACCEPTED_VERBATIM,
            text="", reason=""):
    return world.goals.approve_clinical_goal(
        world.clinician, world.child, edit_type=edit_type,
        suggestion_id=suggestion.suggestion_id if suggestion else None,
        text=text, reason=reason)


# ===========================================================================
# the rung identifier
# ===========================================================================

def test_the_rung_ref_is_deterministic():
    first = compute_rung_ref(RUNG_DOMAIN, RUNG_MONTHS, RUNG_TEXT)
    second = compute_rung_ref(RUNG_DOMAIN, RUNG_MONTHS, RUNG_TEXT)
    assert first == second
    assert first.startswith("rung1:")
    assert len(first) == len("rung1:") + 32


@pytest.mark.parametrize("variant", [
    "  Fictional rung text for anchor tests  ",      # surrounding whitespace
    "Fictional  rung   text for anchor tests",       # collapsed runs
    "FICTIONAL RUNG TEXT FOR ANCHOR TESTS",          # case
    "Fictional\trung text for anchor tests",         # tab
    "Fictional rung text for anchor tests",     # non-breaking space
])
def test_the_rung_ref_survives_display_formatting(variant):
    assert (compute_rung_ref(RUNG_DOMAIN, RUNG_MONTHS, variant)
            == compute_rung_ref(RUNG_DOMAIN, RUNG_MONTHS, RUNG_TEXT))


def test_typographic_punctuation_folds_to_ascii():
    """A copy-edit swapping a hyphen for an en dash must not move the id."""
    hyphen = "Pays attention for 2-3 minutes"
    en_dash = "Pays attention for 2–3 minutes"
    assert (compute_rung_ref("learning_and_thinking", 24, hyphen)
            == compute_rung_ref("learning_and_thinking", 24, en_dash))


def test_semantic_rewording_is_a_NEW_identity():
    """Approved and deliberate: the text is the only semantic identity."""
    assert (compute_rung_ref(RUNG_DOMAIN, RUNG_MONTHS, "says two words")
            != compute_rung_ref(RUNG_DOMAIN, RUNG_MONTHS, "says three words"))


def test_the_rung_ref_is_order_independent_and_field_separated():
    """No row order, and the field join cannot be made ambiguous.

    Without a separator that cannot occur in the fields, ("a","bc") and
    ("ab","c") would hash alike. The unit separator makes the join injective.
    """
    assert (compute_rung_ref(RUNG_DOMAIN, 1, "8 words")
            != compute_rung_ref(RUNG_DOMAIN, 18, "words"))


def test_months_and_domain_participate_in_identity():
    base = compute_rung_ref(RUNG_DOMAIN, RUNG_MONTHS, RUNG_TEXT)
    assert compute_rung_ref(RUNG_DOMAIN, 24, RUNG_TEXT) != base
    assert compute_rung_ref("fine_motor", RUNG_MONTHS, RUNG_TEXT) != base


def test_a_non_canonical_domain_is_refused():
    with pytest.raises(Exception):
        compute_rung_ref("not_a_domain", 12, "x")


@pytest.mark.parametrize("months", ["12", 12.0, True, -1])
def test_a_non_integer_month_is_refused(months):
    with pytest.raises(RungError):
        compute_rung_ref(RUNG_DOMAIN, months, "x")


def test_blank_milestone_text_is_refused():
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(RungError):
            normalize_milestone_text(blank)


def test_the_frozen_canonical_rung_set_has_no_normalized_collisions():
    """Pins the normalization against the REAL workbook.

    Reads the Gold Standard through the Parent brain — in a TEST, which may
    import it; `pilot_backend` itself never does. If a future workbook edit
    made two distinct rungs normalize alike, the ids would collide silently
    and this is where that surfaces.
    """
    import sys
    from pathlib import Path

    parent = Path(__file__).resolve().parents[2] / "genex-parent"
    if not parent.exists():  # pragma: no cover - defensive
        pytest.skip("genex-parent not present in this checkout")
    sys.path.insert(0, str(parent))
    try:
        from genex_core.milestones import get_cdc_df
    except Exception:  # pragma: no cover - optional dependency
        pytest.skip("the Gold Standard workbook is not loadable here")

    rows = get_cdc_df().to_dict(orient="records")
    identities, refs = {}, {}
    for row in rows:
        domain = str(row.get("category_key") or "").strip()
        months = row.get("months")
        text = str(row.get("milestone") or "").strip()
        if not domain or months is None or not text:
            continue
        identity = (domain, int(months), text)
        ref = compute_rung_ref(domain, int(months), text)
        previous = refs.setdefault(ref, identity)
        assert previous == identity, (
            f"two distinct canonical rungs share one ref: {previous} vs {identity}")
        identities[identity] = ref

    assert len(identities) >= 150, "expected the full canonical rung set"
    assert len(set(identities.values())) == len(identities)


# ===========================================================================
# mappability is DERIVED, never stored
# ===========================================================================

def test_mappability_requires_at_least_one_family():
    """No families is UNMAPPABLE, not an error.

    A rung with no activity family is a legitimate canonical target — it just
    cannot drive activity generation. Refusing to construct it would stop a
    clinician approving a real goal; refusing to ALLOCATE it is the correct
    place for the restriction, which `test_an_anchor_whose_family_is_invalid_
    is_REFUSED_by_allocation` covers.
    """
    rung = a_rung(families=[])
    assert rung.activity_family_refs == ()
    assert rung.is_activity_mappable is False


def test_mappability_requires_every_family_to_permit_the_domain():
    """One invalid family makes the whole rung unmappable.

    Dropping it instead would silently narrow a mapping nobody reviewed.
    """
    rung = a_rung(families=[
        ActivityFamilyBinding("expressive_three_words", (RUNG_DOMAIN,)),
        ActivityFamilyBinding("gross_motor_only", ("gross_motor",)),
    ])
    assert rung.is_activity_mappable is False


def test_a_family_valid_for_a_secondary_domain_is_permitted():
    rung = a_rung(families=[
        ActivityFamilyBinding("shared", (RUNG_DOMAIN, "daily_living"))])
    assert rung.is_activity_mappable is True


def test_mappability_is_not_a_stored_field():
    """It is a property, so there is nothing that could drift from the rung."""
    from dataclasses import fields

    from pilot_backend.persistence.codecs import encode

    names = {f.name for f in fields(CanonicalRung)}
    assert "is_activity_mappable" not in names
    assert "is_activity_mappable" not in encode(a_rung())
    anchor = ClinicalGoalAnchor(
        clinical_goal_id="clgl_x", child_id="child_x",
        source_suggestion_id="gsug_x", rung=a_rung())
    assert "is_activity_mappable" not in encode(anchor)


def test_families_are_deduplicated_and_sorted():
    rung = a_rung(families=[
        ActivityFamilyBinding("zeta", (RUNG_DOMAIN,)),
        ActivityFamilyBinding("alpha", (RUNG_DOMAIN,)),
        ActivityFamilyBinding("zeta", (RUNG_DOMAIN,)),
    ])
    assert rung.activity_family_refs == ("alpha", "zeta")


def test_two_bindings_disagreeing_about_one_family_are_refused():
    """Merging would invent a permission nobody granted."""
    with pytest.raises(RungError):
        a_rung(families=[
            ActivityFamilyBinding("same", (RUNG_DOMAIN,)),
            ActivityFamilyBinding("same", (RUNG_DOMAIN, "daily_living")),
        ])


def test_no_primary_family_is_designated():
    """0.5E-A refuses to choose; the weekly planner must choose explicitly."""
    from dataclasses import fields

    names = {f.name for f in fields(CanonicalRung)}
    assert not any("primary" in name for name in names)


def test_a_mismatched_rung_ref_is_refused():
    rung = a_rung()
    from dataclasses import replace
    with pytest.raises(RungError):
        replace(rung, rung_ref="rung1:" + "0" * 32)


def test_a_mismatched_track_ref_is_refused():
    rung = a_rung()
    from dataclasses import replace
    with pytest.raises(RungError):
        replace(rung, track_ref="track1:" + "0" * 32)


# ===========================================================================
# the track
# ===========================================================================

def test_the_track_ref_is_order_independent():
    assert (compute_track_ref(RUNG_DOMAIN, ["b", "a"], [])
            == compute_track_ref(RUNG_DOMAIN, ["a", "b"], []))


def test_daily_living_families_distinguish_two_tracks_on_one_subdomain():
    """The Daily Living requirement, structurally.

    Eating and dressing share a declared subdomain but are independent
    routines. A track id over subdomains alone would collapse them.
    """
    eating = compute_track_ref("daily_living", ["self_help_motor_skills"],
                               ["self_feeding"])
    dressing = compute_track_ref("daily_living", ["self_help_motor_skills"],
                                 ["dressing"])
    assert eating != dressing


def test_a_track_requires_a_subdomain():
    with pytest.raises(RungError):
        compute_track_ref(RUNG_DOMAIN, [], [])


# ===========================================================================
# approval: atomic goal + version + anchor
# ===========================================================================

def test_approval_from_an_anchored_suggestion_creates_all_three(world):
    rung = a_rung()
    suggestion = generate(world, rung=rung)
    assert world.repos.suggestion_anchors.find(suggestion.suggestion_id)

    goal = approve(world, suggestion)
    ref = GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id)

    assert world.repos.clinical_goals.get_by_id(goal.clinical_goal_id)
    assert world.repos.goal_versions.get_by_id(goal.current_version_id)
    anchor = world.goals.goal_anchor(world.clinician, ref)
    assert anchor is not None
    assert anchor.rung == rung
    assert anchor.source_suggestion_id == suggestion.suggestion_id
    assert world.goals.is_activity_mappable(world.clinician, ref) is True


def test_an_unanchored_suggestion_yields_an_unmappable_goal(world):
    """Fail closed. Every pre-0.5E-A suggestion is in exactly this state."""
    suggestion = generate(world, rung=None)
    assert world.repos.suggestion_anchors.find(suggestion.suggestion_id) is None

    goal = approve(world, suggestion)
    ref = GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id)
    assert world.goals.goal_anchor(world.clinician, ref) is None
    assert world.goals.is_activity_mappable(world.clinician, ref) is False


def test_authored_fresh_is_unmappable(world):
    goal = world.goals.approve_clinical_goal(
        world.clinician, world.child, edit_type=EditType.AUTHORED_FRESH,
        text="A fictional clinician-authored target for {child}.",
        reason="Fictional rationale")
    ref = GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id)
    assert world.goals.goal_anchor(world.clinician, ref) is None
    assert world.goals.is_activity_mappable(world.clinician, ref) is False


def test_the_anchor_is_never_derived_from_goal_text(world):
    """The text names a domain and a milestone. It changes nothing.

    This is the invariant the whole slice exists for: wording is not evidence.
    """
    suggestion = generate(world, rung=None)
    goal = approve(world, suggestion, edit_type=EditType.MODIFIED,
                   text="fine_motor Picks things up between thumb and finger "
                        "at 12 months for {child}",
                   reason="Fictional rationale")
    ref = GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id)
    assert world.goals.goal_anchor(world.clinician, ref) is None


@pytest.mark.parametrize("edit_type,text,reason", [
    (EditType.ACCEPTED_VERBATIM, "", ""),
    (EditType.MODIFIED, "A fictional revised target for {child}.", "why"),
    (EditType.REPLACED, "A fictional replacement target for {child}.", "why"),
])
def test_every_suggestion_derived_edit_type_anchors(world, edit_type, text,
                                                    reason):
    """Accepting, modifying and replacing all preserve the anchor."""
    suggestion = generate(world, rung=a_rung())
    goal = approve(world, suggestion, edit_type=edit_type, text=text,
                   reason=reason)
    ref = GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id)
    assert world.goals.is_activity_mappable(world.clinician, ref) is True


def test_a_rung_for_the_wrong_domain_is_refused_at_generation(world):
    """A mismatched anchor is worse than none — it looks authoritative."""
    wrong = a_rung(domain="fine_motor")
    with pytest.raises(GoalValidationError):
        generate(world, rung=wrong, domain=RUNG_DOMAIN)


# ===========================================================================
# wording revisions leave the anchor byte-for-byte unchanged
# ===========================================================================

def test_a_wording_revision_leaves_the_anchor_identical(world):
    from pilot_backend.persistence.codecs import encode

    suggestion = generate(world, rung=a_rung())
    goal = approve(world, suggestion)
    ref = GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id)
    before = encode(world.goals.goal_anchor(world.clinician, ref))

    for index in range(3):
        world.goals.revise_goal(
            world.clinician, ref,
            f"A fictional revision {index} for {{child}}.",
            edit_type=EditType.MODIFIED, reason="Fictional rationale")

    after = encode(world.goals.goal_anchor(world.clinician, ref))
    assert after == before, "a wording revision changed the canonical anchor"
    assert world.goals.is_activity_mappable(world.clinician, ref) is True


def test_the_anchor_repository_cannot_rewrite(world):
    """Immutability is structural: there is no method that could."""
    repo = world.repos.clinical_goal_anchors
    for forbidden in ("update", "set", "overwrite", "delete"):
        assert not hasattr(repo, forbidden), forbidden


# ===========================================================================
# the allocation guard
# ===========================================================================

def _plan(world):
    return world.plans.create_plan(world.clinician, world.child, MONTH, "UTC")


def test_a_mapped_goal_allocates_normally(world):
    suggestion = generate(world, rung=a_rung())
    goal = approve(world, suggestion)
    plan = _plan(world)
    allocation = world.plans.allocate_goal(
        world.clinician, plan.focus_plan_id,
        GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id),
        priority_rank=1, emphasis_weight=2)
    assert allocation.allocation_id


def test_an_unanchored_goal_is_REFUSED_by_allocation(world):
    suggestion = generate(world, rung=None)
    goal = approve(world, suggestion)
    plan = _plan(world)
    with pytest.raises(GoalValidationError):
        world.plans.allocate_goal(
            world.clinician, plan.focus_plan_id,
            GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id),
            priority_rank=1)


def test_an_authored_fresh_goal_is_REFUSED_by_allocation(world):
    goal = world.goals.approve_clinical_goal(
        world.clinician, world.child, edit_type=EditType.AUTHORED_FRESH,
        text="A fictional authored target for {child}.",
        reason="Fictional rationale")
    plan = _plan(world)
    with pytest.raises(GoalValidationError):
        world.plans.allocate_goal(
            world.clinician, plan.focus_plan_id,
            GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id),
            priority_rank=1)


def test_an_anchor_whose_family_is_invalid_is_REFUSED_by_allocation(world):
    """An anchor exists but does not permit activity generation."""
    rung = a_rung(families=[
        ActivityFamilyBinding("wrong_domain_family", ("gross_motor",))])
    suggestion = generate(world, rung=rung)
    goal = approve(world, suggestion)
    ref = GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id)
    assert world.goals.goal_anchor(world.clinician, ref) is not None
    assert world.goals.is_activity_mappable(world.clinician, ref) is False

    plan = _plan(world)
    with pytest.raises(GoalValidationError):
        world.plans.allocate_goal(world.clinician, plan.focus_plan_id, ref,
                                  priority_rank=1)


def test_the_guard_does_not_change_caregiver_goal_behaviour(world):
    """Caregiver-approved goals are out of scope and must be unaffected."""
    import inspect

    from pilot_backend.planning.service import MonthlyPlanService

    source = inspect.getsource(MonthlyPlanService._require_activity_mappable)
    assert "GoalKind.CLINICAL" in source
    assert "return" in source.split("GoalKind.CLINICAL")[1][:80]
