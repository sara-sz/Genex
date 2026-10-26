"""A2 v2.1 — the Pilot is the sole content authority at the write path.

Parent derives a served-family set and uses it for caregiver UX and to withhold
a projection upstream. That set is a MIRROR. The approved activity bank lives
here, so generation asks the bank immediately before it writes rather than
trusting what Parent said when it projected.

The gates this file installs permanently:

  * Parent supported + Pilot supported  -> generation proceeds
  * Parent supported + Pilot unsupported -> fail closed, NOTHING written
  * no content authority on the v2 path  -> fail closed
  * the verdict is taken immediately before the first side effect, so a stale
    earlier check cannot authorise a later write
  * no near-mapping and no substitution: the exact target or a refusal
  * a refusal carries both digests and no clinical prose
"""

from __future__ import annotations

import ast
import inspect

import pytest

from pilot_backend.domain.content_authority import (
    REASON_FAMILY_NOT_SERVED,
    REASON_NOT_MAPPABLE,
    SUPPORT_NOT_APPLICABLE,
    SUPPORT_SUPPORTED,
    SUPPORT_UNSUPPORTED,
    ActivityBankContentAuthority,
    ContentAuthorityUnavailable,
)
from pilot_backend.domain.suggestion_generation_v2 import (
    GENERATION_POLICY_VERSION_V2,
)
from pilot_backend.goals.errors import GoalValidationError
from pilot_backend.goals.service import GoalService

from .test_evidence_driven_generation_v2 import (  # noqa: E402
    BOOK, CHILD, FIXTURE_SERVED_FAMILIES, FakeActivityBank,
    World, _projection, content_authority, mixed,
)


class ParentEvaluation:
    """The advisory Parent content evaluation, as v2.1 transmits it."""

    def __init__(self, serviceability, digest="p" * 64,
                 content_set_id="talking-served-families-v1"):
        self.target_serviceability = serviceability
        self.content_digest = digest
        self.content_set_id = content_set_id


def _deficit_world(**kwargs):
    return World(projection=_projection(
        states_30=mixed(**{BOOK: "not_demonstrated"})), **kwargs)


# ---------------------------------------------------------------------------
# 1. the authority itself
# ---------------------------------------------------------------------------

def test_the_authority_requires_a_bank():
    with pytest.raises(ContentAuthorityUnavailable):
        ActivityBankContentAuthority(None)


def test_the_authority_reports_supported_for_a_served_family():
    authority = content_authority()
    world = _deficit_world()
    # Resolved through the service's own path, so the rung is the one
    # generation would actually use rather than one this test shaped.
    _band, mappable, _unsupported = world.service.resolve_targets(
        world.projection)
    assert mappable
    support = authority.support_for_target(mappable[0])
    assert support.state == SUPPORT_SUPPORTED
    assert support.supported is True
    assert support.unsupported_families == ()
    assert support.reason is None
    assert len(support.content_digest) == 64


def test_the_authority_reports_unsupported_for_an_unserved_family():
    authority = ActivityBankContentAuthority(FakeActivityBank(served=()))
    world = _deficit_world()
    _band, mappable, _unsupported = world.service.resolve_targets(
        world.projection)
    support = authority.support_for_target(mappable[0])
    assert support.state == SUPPORT_UNSUPPORTED
    assert support.supported is False
    assert support.reason == REASON_FAMILY_NOT_SERVED
    assert support.unsupported_families, "the gap is NAMED"


def test_the_authority_reports_unsupported_for_an_unmappable_target():
    class NotMappable:
        is_activity_mappable = False
        family_bindings = ()

    support = content_authority().support_for_target(NotMappable())
    assert support.state == SUPPORT_UNSUPPORTED
    assert support.reason == REASON_NOT_MAPPABLE


def test_every_bound_family_must_be_served():
    """Partial support is UNSUPPORTED, never a licence to use the served half."""
    class TwoFamilies:
        is_activity_mappable = True

        class _B:
            def __init__(self, ref):
                self.family_ref = ref

        family_bindings = (_B(FIXTURE_SERVED_FAMILIES[0]),
                           _B("a_family_with_no_reviewed_content"))

    support = content_authority().support_for_target(TwoFamilies())
    assert support.state == SUPPORT_UNSUPPORTED
    assert support.unsupported_families == (
        "a_family_with_no_reviewed_content",)


def test_the_authority_owns_no_data_of_its_own():
    """No second family registry. Every answer comes from the bank."""
    source = inspect.getsource(ActivityBankContentAuthority)
    assert "served_families" in source
    assert "artifact_digest" in source
    # No hardcoded family list and no mirrored support table.
    for forbidden in ("SERVED_FAMILIES = ", "_FAMILIES = (", "frozenset({"):
        assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# 2. Parent supported + Pilot supported -> eligible
# ---------------------------------------------------------------------------

def test_agreement_generates():
    world = _deficit_world()
    outcome = world.generate()
    assert outcome.generated
    assert outcome.generated[0].created is True
    assert world.suggestions()


def test_agreement_generates_with_an_explicit_parent_evaluation():
    world = _deficit_world()
    _band, mappable, _unsupported = world.service.resolve_targets(
        world.projection)
    suggestions, created = world.goals.generate_anchored_suggestion(
        world.principal(), CHILD,
        projection=world.projection, canonical_rung=mappable[0],
        observed=world.service._observed_domain(world.projection, mappable[0]),
        generation_policy=GENERATION_POLICY_VERSION_V2,
        parent_content_evaluation=ParentEvaluation(SUPPORT_SUPPORTED))
    assert created is True and suggestions


@pytest.mark.parametrize("declared", [SUPPORT_SUPPORTED,
                                      SUPPORT_NOT_APPLICABLE, None])
def test_a_non_contradicting_parent_evaluation_does_not_block(declared):
    """`not_applicable` and absent are not disagreements."""
    world = _deficit_world()
    _band, mappable, _unsupported = world.service.resolve_targets(
        world.projection)
    evaluation = None if declared is None else ParentEvaluation(declared)
    suggestions, created = world.goals.generate_anchored_suggestion(
        world.principal(), CHILD,
        projection=world.projection, canonical_rung=mappable[0],
        observed=world.service._observed_domain(world.projection, mappable[0]),
        generation_policy=GENERATION_POLICY_VERSION_V2,
        parent_content_evaluation=evaluation)
    assert created is True and suggestions


# ---------------------------------------------------------------------------
# 3. Parent supported + Pilot unsupported -> FAIL CLOSED
# ---------------------------------------------------------------------------

def test_direction_one_parent_supported_pilot_unsupported_fails_closed():
    """DIRECTION 1 of the symmetric rule."""
    world = _deficit_world(
        authority=ActivityBankContentAuthority(FakeActivityBank(served=())))
    _band, mappable, _unsupported = world.service.resolve_targets(
        world.projection)
    with pytest.raises(GoalValidationError) as caught:
        world.goals.generate_anchored_suggestion(
            world.principal(), CHILD,
            projection=world.projection, canonical_rung=mappable[0],
            observed=world.service._observed_domain(world.projection,
                                                    mappable[0]),
            generation_policy=GENERATION_POLICY_VERSION_V2,
            parent_content_evaluation=ParentEvaluation(SUPPORT_SUPPORTED))
    message = str(caught.value)
    assert "content_state_mismatch" in message
    assert "supported" in message
    assert "source_digest" in message and "pilot_digest" in message
    assert world._docs("pilot_goal_suggestions") == []


def test_direction_two_parent_unsupported_pilot_supported_fails_closed():
    """DIRECTION 2. The Pilot is authoritative, but a plan must not be built
    while the Parent product may still be telling a family this is
    unavailable."""
    world = _deficit_world()
    _band, mappable, _unsupported = world.service.resolve_targets(
        world.projection)
    with pytest.raises(GoalValidationError) as caught:
        world.goals.generate_anchored_suggestion(
            world.principal(), CHILD,
            projection=world.projection, canonical_rung=mappable[0],
            observed=world.service._observed_domain(world.projection,
                                                    mappable[0]),
            generation_policy=GENERATION_POLICY_VERSION_V2,
            parent_content_evaluation=ParentEvaluation(SUPPORT_UNSUPPORTED))
    message = str(caught.value)
    assert "content_state_mismatch" in message
    assert "source_digest" in message and "pilot_digest" in message
    # Nothing written in this direction either.
    for collection in ("pilot_goal_suggestions",
                       "pilot_suggestion_generation_claims",
                       "pilot_suggestion_canonical_anchors"):
        assert world._docs(collection) == [], collection


def test_the_four_direction_matrix_is_exactly_as_specified():
    """One table, both directions, stated once."""
    from pilot_backend.goals.service import ContentReconciliation
    from pilot_backend.domain.content_authority import TargetSupport

    def reconcile(declared, pilot_state):
        return ContentReconciliation(
            support=TargetSupport(state=pilot_state, content_digest="x" * 64),
            declared=declared, source_digest="y" * 64)

    assert reconcile(SUPPORT_SUPPORTED, SUPPORT_UNSUPPORTED).disagrees is True
    assert reconcile(SUPPORT_UNSUPPORTED, SUPPORT_SUPPORTED).disagrees is True
    assert reconcile(SUPPORT_SUPPORTED, SUPPORT_SUPPORTED).disagrees is False
    assert reconcile(SUPPORT_UNSUPPORTED,
                     SUPPORT_UNSUPPORTED).disagrees is False
    # `not_applicable` and absent are not disagreements.
    assert reconcile(SUPPORT_NOT_APPLICABLE,
                     SUPPORT_SUPPORTED).disagrees is False
    assert reconcile(None, SUPPORT_SUPPORTED).disagrees is False


def test_both_unsupported_still_refuses_on_the_pilots_own_verdict():
    """Agreement that a target is unservable is not permission."""
    world = _deficit_world(
        authority=ActivityBankContentAuthority(FakeActivityBank(served=())))
    _band, mappable, _unsupported = world.service.resolve_targets(
        world.projection)
    with pytest.raises(GoalValidationError) as caught:
        world.goals.generate_anchored_suggestion(
            world.principal(), CHILD,
            projection=world.projection, canonical_rung=mappable[0],
            observed=world.service._observed_domain(world.projection,
                                                    mappable[0]),
            generation_policy=GENERATION_POLICY_VERSION_V2,
            parent_content_evaluation=ParentEvaluation(SUPPORT_UNSUPPORTED))
    message = str(caught.value)
    assert "not currently supported" in message
    assert REASON_FAMILY_NOT_SERVED in message


def test_a_differing_global_digest_with_agreeing_serviceability_is_allowed():
    """THE DIGEST NUANCE.

    A global content digest moves whenever UNRELATED content changes. Failing
    on inequality would refuse a generation because some other family gained a
    card. Both digests are preserved and the staleness is reported; the
    condition is the semantic serviceability of THIS target.
    """
    world = _deficit_world(
        authority=ActivityBankContentAuthority(
            FakeActivityBank(digest="1" * 64)))
    _band, mappable, _unsupported = world.service.resolve_targets(
        world.projection)
    evaluation = ParentEvaluation(SUPPORT_SUPPORTED, digest="2" * 64)
    assert evaluation.content_digest != "1" * 64

    reconciliation = world.goals._require_current_content_support(
        mappable[0], generation_policy=GENERATION_POLICY_VERSION_V2,
        parent_content_evaluation=evaluation)
    # Detected and reported...
    assert reconciliation.digests_match is False
    assert reconciliation.stale_mirror is True
    assert reconciliation.source_digest == "2" * 64
    assert reconciliation.support.content_digest == "1" * 64
    # ...but NOT a refusal, and generation proceeds.
    assert reconciliation.disagrees is False
    suggestions, created = world.goals.generate_anchored_suggestion(
        world.principal(), CHILD, projection=world.projection,
        canonical_rung=mappable[0],
        observed=world.service._observed_domain(world.projection, mappable[0]),
        generation_policy=GENERATION_POLICY_VERSION_V2,
        parent_content_evaluation=evaluation)
    assert created is True and suggestions


def test_identical_digests_report_no_staleness():
    world = _deficit_world(
        authority=ActivityBankContentAuthority(
            FakeActivityBank(digest="3" * 64)))
    _band, mappable, _unsupported = world.service.resolve_targets(
        world.projection)
    reconciliation = world.goals._require_current_content_support(
        mappable[0], generation_policy=GENERATION_POLICY_VERSION_V2,
        parent_content_evaluation=ParentEvaluation(SUPPORT_SUPPORTED,
                                                  digest="3" * 64))
    assert reconciliation.digests_match is True
    assert reconciliation.stale_mirror is False


def test_the_digest_is_never_the_fail_condition():
    """Structural: `disagrees` does not consult a digest at all."""
    import ast
    import inspect
    import textwrap

    from pilot_backend.goals.service import ContentReconciliation

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(ContentReconciliation.disagrees.fget)))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.Module)) and \
                ast.get_docstring(node):
            node.body = node.body[1:]
    code = ast.unparse(tree)
    assert "digest" not in code


def test_nothing_is_written_when_the_content_gate_refuses():
    """No claim, no suggestion, no anchor. The refusal precedes every write."""
    world = _deficit_world(
        authority=ActivityBankContentAuthority(FakeActivityBank(served=())))
    _band, mappable, _unsupported = world.service.resolve_targets(
        world.projection)
    with pytest.raises(GoalValidationError):
        world.goals.generate_anchored_suggestion(
            world.principal(), CHILD,
            projection=world.projection, canonical_rung=mappable[0],
            observed=world.service._observed_domain(world.projection,
                                                    mappable[0]),
            generation_policy=GENERATION_POLICY_VERSION_V2)
    for collection in ("pilot_goal_suggestions",
                       "pilot_suggestion_generation_claims",
                       "pilot_suggestion_canonical_anchors",
                       "pilot_clinical_goals"):
        assert world._docs(collection) == [], collection


def test_a_stale_parent_claim_of_support_is_detected_not_trusted():
    """Parent says supported, the bank says otherwise: surfaced, with digests."""
    world = _deficit_world(
        authority=ActivityBankContentAuthority(
            FakeActivityBank(served=(), digest="b" * 64)))
    _band, mappable, _unsupported = world.service.resolve_targets(
        world.projection)
    with pytest.raises(GoalValidationError) as caught:
        world.goals.generate_anchored_suggestion(
            world.principal(), CHILD,
            projection=world.projection, canonical_rung=mappable[0],
            observed=world.service._observed_domain(world.projection,
                                                    mappable[0]),
            generation_policy=GENERATION_POLICY_VERSION_V2,
            parent_content_evaluation=ParentEvaluation(SUPPORT_SUPPORTED,
                                                      digest="a" * 64))
    message = str(caught.value)
    assert "b" * 64 in message, "the Pilot digest is diagnosable"


# `test_parent_being_more_conservative_does_not_block` lived here and was
# MISNAMED: it asserted a refusal while its name and the implementation's
# docstring both claimed the direction was permitted. The code was right and
# the prose was wrong. It is now
# `test_direction_two_parent_unsupported_pilot_supported_fails_closed` above.


def test_no_other_family_is_substituted_when_the_target_is_unsupported():
    """The exact target or a refusal. Never a near-map."""
    served_other = ("sentence_building",)
    world = _deficit_world(
        authority=ActivityBankContentAuthority(
            FakeActivityBank(served=served_other)))
    _band, mappable, _unsupported = world.service.resolve_targets(
        world.projection)
    target_ref = mappable[0].rung_ref
    with pytest.raises(GoalValidationError):
        world.goals.generate_anchored_suggestion(
            world.principal(), CHILD,
            projection=world.projection, canonical_rung=mappable[0],
            observed=world.service._observed_domain(world.projection,
                                                    mappable[0]),
            generation_policy=GENERATION_POLICY_VERSION_V2)
    # No suggestion for ANY target, let alone a served one.
    assert world._docs("pilot_goal_suggestions") == []
    assert world._docs("pilot_suggestion_canonical_anchors") == []
    assert target_ref


# ---------------------------------------------------------------------------
# 3b. content changing changes SERVICEABILITY, never the projection
# ---------------------------------------------------------------------------

def test_content_changing_changes_the_verdict_not_the_projection():
    """HISTORICAL vs CURRENT, on the Pilot side of the boundary.

    The same stored projection, asked twice under two content states, gets two
    verdicts — and the projection document is byte-identical both times. The
    child's evidence is historical; serviceability is current.
    """
    import dataclasses

    world = _deficit_world()
    _band, mappable, _unsupported = world.service.resolve_targets(
        world.projection)
    rung = mappable[0]
    before_docs = world._docs("pilot_parent_baseline_projections_v2")

    rich = content_authority()
    poor = ActivityBankContentAuthority(
        FakeActivityBank(served=(), digest="0" * 64))

    assert rich.support_for_target(rung).state == SUPPORT_SUPPORTED
    assert poor.support_for_target(rung).state == SUPPORT_UNSUPPORTED
    # Different content states, different digests — attributable.
    assert rich.support_for_target(rung).content_digest != \
        poor.support_for_target(rung).content_digest

    # The stored projection did not move.
    assert world._docs("pilot_parent_baseline_projections_v2") == before_docs
    # And the rung — the canonical target — is the same object either way.
    assert dataclasses.asdict(rung) == dataclasses.asdict(mappable[0])


def test_content_returning_makes_the_same_record_generatable_again():
    """Track 2 landing a card must reach a child already projected.

    No migration, no re-interview: the same stored projection, the same
    canonical target, a different content state, and now a suggestion.
    """
    blocked = _deficit_world(
        authority=ActivityBankContentAuthority(FakeActivityBank(served=())))
    _band, mappable, _unsupported = blocked.service.resolve_targets(
        blocked.projection)
    with pytest.raises(GoalValidationError):
        blocked.goals.generate_anchored_suggestion(
            blocked.principal(), CHILD, projection=blocked.projection,
            canonical_rung=mappable[0],
            observed=blocked.service._observed_domain(blocked.projection,
                                                      mappable[0]),
            generation_policy=GENERATION_POLICY_VERSION_V2)
    assert blocked._docs("pilot_goal_suggestions") == []

    # The SAME projection and the SAME target, under a stocked bank.
    blocked.goals._content_authority = content_authority()
    suggestions, created = blocked.goals.generate_anchored_suggestion(
        blocked.principal(), CHILD, projection=blocked.projection,
        canonical_rung=mappable[0],
        observed=blocked.service._observed_domain(blocked.projection,
                                                  mappable[0]),
        generation_policy=GENERATION_POLICY_VERSION_V2)
    assert created is True and suggestions


# ---------------------------------------------------------------------------
# 4. an absent authority fails closed on the v2 path
# ---------------------------------------------------------------------------

def test_the_v2_path_refuses_without_a_content_authority():
    world = _deficit_world(authority=None)
    _band, mappable, _unsupported = world.service.resolve_targets(
        world.projection)
    with pytest.raises(GoalValidationError) as caught:
        world.goals.generate_anchored_suggestion(
            world.principal(), CHILD,
            projection=world.projection, canonical_rung=mappable[0],
            observed=world.service._observed_domain(world.projection,
                                                    mappable[0]),
            generation_policy=GENERATION_POLICY_VERSION_V2)
    assert "no content authority is wired" in str(caught.value)
    assert world._docs("pilot_goal_suggestions") == []


def test_the_v1_anchored_lineage_is_unchanged_without_an_authority():
    """SCOPED DECISION, recorded.

    The v1 anchored path predates this dependency, is gated by its own
    mappability check, and is not a real-family path, so it keeps its existing
    behaviour. Only the v2 generation policy requires the authority.
    """
    service = GoalService(repos=None, content_authority=None)
    assert service._require_current_content_support(
        object(), generation_policy="some-v1-policy") is None


def test_the_v21_path_cannot_fall_back_to_v1_generation():
    """THE SCOPED-DECISION GATE.

    Requiring the content authority only on the v2 policy is approved, so the
    v1 anchored lineage keeps its behaviour. The risk that creates is a
    real-family generation silently taking the v1 path and skipping the content
    check. It cannot:

      * the v2 generation service passes `GENERATION_POLICY_VERSION_V2`
        EXPLICITLY on every call — it never relies on the default
      * the policy is part of the CLAIM KEY, so a v1-policy claim and a
        v2-policy claim for the same target are different documents and
        neither can be mistaken for the other
      * the default is v1's constant, so a caller that forgot to pass one
        would be visibly generating under the v1 policy rather than invisibly
        skipping a check on the v2 one
    """
    import ast
    import inspect
    import textwrap

    from pilot_backend.domain.suggestion_generation import (
        GENERATION_POLICY_VERSION,
    )
    from pilot_backend.integration.baseline_suggestion_generation_v2 import (
        BaselineSuggestionGenerationV2Service,
    )

    # 1. the v2 service always names the v2 policy at the call site.
    source = inspect.getsource(
        BaselineSuggestionGenerationV2Service.generate_for_child)
    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.Module)) and \
                ast.get_docstring(node):
            node.body = node.body[1:]
    code = ast.unparse(tree)
    assert "generation_policy=GENERATION_POLICY_VERSION_V2" in code

    # 2. the two policies are different values, so the gate can tell them apart.
    assert GENERATION_POLICY_VERSION != GENERATION_POLICY_VERSION_V2

    # 3. the policy is part of the claim key, so the paths cannot share a claim.
    from pilot_backend.domain.suggestion_generation import (
        GoalSuggestionGenerationClaim,
    )
    claim_source = inspect.getsource(GoalSuggestionGenerationClaim)
    assert "generation_policy" in claim_source

    # 4. and a v2-policy call with no authority REFUSES rather than degrading.
    service = GoalService(repos=None, content_authority=None)
    with pytest.raises(GoalValidationError):
        service._require_current_content_support(
            object(), generation_policy=GENERATION_POLICY_VERSION_V2)
    # ...while the v1 policy is untouched, which is the approved carve-out.
    assert service._require_current_content_support(
        object(), generation_policy=GENERATION_POLICY_VERSION) is None


def test_the_v2_policy_constant_matches_the_domain():
    """So the gated path cannot drift from the real v2 policy."""
    assert GoalService._V2_GENERATION_POLICY == GENERATION_POLICY_VERSION_V2


# ---------------------------------------------------------------------------
# 5. the gate is the LAST thing before the first side effect
# ---------------------------------------------------------------------------

def test_the_gate_runs_immediately_before_the_claim():
    """A verdict taken earlier could be stale by the time anything is written."""
    source = inspect.getsource(GoalService.generate_anchored_suggestion)
    import textwrap
    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.Module)) and \
                ast.get_docstring(node):
            node.body = node.body[1:]
    code = ast.unparse(tree)
    gate = code.index("_require_current_content_support")
    claim = code.index("GoalSuggestionGenerationClaim.build")
    assert gate < claim
    # And nothing writes between them.
    between = code[gate:claim]
    for forbidden in (".create(", ".update(", "tx."):
        assert forbidden not in between, forbidden


def test_the_production_composition_builds_the_authority_from_its_bank():
    """The lesson of the activity-bank composition defect, applied."""
    from pilot_backend.transport import wsgi_app

    source = inspect.getsource(wsgi_app.PilotWSGIApplication)
    assert "ActivityBankContentAuthority" in source
    assert "content_authority=authority" in source
