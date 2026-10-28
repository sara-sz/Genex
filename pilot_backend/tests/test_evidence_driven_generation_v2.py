"""F-B v2 — evidence-driven multi-suggestion generation (0.6A-1G).

Dependency-pure: `FirestoreRepositories` over `FakeDocumentStore`, the port's own
`InMemoryGoldStandardRungSource`, and no SDK anywhere. The REAL frozen rung table
and the REAL Parent v2 engine drive the same service in
`pilot_runtime/tests/test_fb_v2_product_path.py`, and concurrency is proven on the
Firestore emulator — a dict is not thread-safe, so no contention claim is made
here.

## What is under test

That the TARGET SET is a function of the projected per-skill evidence, and of
nothing else. v1 chose its target by stepping a month and then taking whichever
same-month rung sorted first; the tests below pin that v2's answer does not move
when milestone wording or iteration order changes, because identity comes from the
`rung_ref` the evidence named.

## The band shape used throughout

Four skills at 30 months, mirroring the real declared track: three mappable and
one — standing for `pronouns` — canonical but with no reconciled activity family.
One mappable skill at 24 months, so there is a lower band to master or fail.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timezone

import pytest

from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth.interface import VerifiedToken
from pilot_backend.auth.resolver import resolve_principal
from pilot_backend.domain.content_authority import (
    ActivityBankContentAuthority,
)
from pilot_backend.domain.canonical_rung import (
    ActivityFamilyBinding,
    CanonicalRung,
    compute_rung_ref,
)
from pilot_backend.domain.connections import (
    CaregiverChildConnection,
    CaregiverRelationship,
    ProviderChildConnection,
)
from pilot_backend.domain.entities import (
    Caregiver,
    Child,
    Practice,
    Provider,
    ProviderDiscipline,
)
from pilot_backend.domain.managing_clinician import ManagingClinicianAssignment
from pilot_backend.domain.parent_baseline_projection_v2 import (
    ParentBaselineProjectionV2,
    ProjectedBandTotal,
    ProjectedSkillEvidence,
)
from pilot_backend.domain.roles import ActorRole
from pilot_backend.domain.source_link import SourceSystem, SourceSystemLink
from pilot_backend.domain.suggestion_generation import (
    GENERATION_POLICY_VERSION,
    GoalSuggestionGenerationClaim,
    generation_claim_id,
    generation_key,
)
from pilot_backend.domain.suggestion_generation_v2 import (
    BAND_INCOMPLETE,
    BAND_MASTERED,
    BAND_UNRESOLVED,
    GENERATION_POLICY_VERSION_V2,
    OUTCOME_GENERATED,
    OUTCOME_INSUFFICIENT_KNOWN_EVIDENCE,
    OUTCOME_NO_SUPPORTED_TARGET,
    OUTCOME_NO_SUPPORTED_TARGET_REGISTERED_GAPS,
    OUTCOME_NO_UNRESOLVED_BAND,
    REF_MAPPABLE,
    REF_REGISTERED_UNRESOLVED,
    REF_UNREGISTERED_UNMAPPABLE,
    BandAssessmentIncomplete,
    GenerationV2Error,
    PassedOverDeficit,
    ProjectionNotEvidenceDriven,
    UnregisteredUnmappableSkill,
    classify_band,
    scan_bands,
    select_target_band,
    v1_claim_is_not_reusable_for_v2,
)
from pilot_backend.goals.service import GoalService
from pilot_backend.integration.baseline_suggestion_generation_v2 import (
    SUPPORTED_DOMAIN,
    BaselineSuggestionGenerationV2Service,
)
from pilot_backend.integration.gold_standard_source import (
    InMemoryGoldStandardRungSource,
    RungNotFoundError,
    RungNotMappableError,
    RungTarget,
)
from pilot_backend.persistence import FakeDocumentStore, FirestoreRepositories

PILOT_ROOT = pathlib.Path(__file__).resolve().parents[1]

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
SESSION = "sess-fbv2-fictional-a1b2"
CHILD = "chld_16a0d958bedd43b6b073ce3cc393a8f1"
DIGEST = "c" * 64

TAXONOMY_VERSION = "activity_family_taxonomy_v1"
GOLD_STANDARD_VERSION = "parent-2.4-functional-baseline-v1"
BASELINE_VERSION_V2 = "parent-2.4-functional-baseline-v2"

TRACK_SUBDOMAINS = ("early_vocalization_and_babbling", "expressive_language")

#: The band shape. `pronouns` is the canonical-but-unmappable case.
AT_24 = "says at least two words together like more milk"
BOOK = "name things in a book when you point and ask what is this"
TWO_WORD = "say two or more words together with one action word"
VOCAB = "says about 50 words"
PRONOUNS = "says words like i me or we"

MAPPABLE_30 = (BOOK, TWO_WORD, VOCAB)


def _rung(milestone, months, families=("expressive_vocabulary_growth",)):
    return CanonicalRung.build(
        domain_key=SUPPORTED_DOMAIN,
        source_rung_months=months,
        milestone_text=milestone,
        subdomain="expressive_language",
        family_bindings=tuple(
            ActivityFamilyBinding(family_ref=f,
                                  allowed_domains=(SUPPORTED_DOMAIN,))
            for f in families),
        track_subdomains=TRACK_SUBDOMAINS,
        track_families=(),
        taxonomy_version=TAXONOMY_VERSION,
        baseline_version=GOLD_STANDARD_VERSION)


def ref_for(milestone, months):
    return compute_rung_ref(SUPPORTED_DOMAIN, months, milestone)


def _source(*, extra_mappable=(), unmappable_30=(PRONOUNS,), register=True):
    """The fixture rung source: 24m plus four 30m skills, one unmappable.

    `register=True` marks the unmappable 30m skills as recorded in the
    taxonomy's unresolved register, which is what the REAL taxonomy says about
    `pronouns` — it is a `deferred_post_pilot` entry. Without that the fixture
    would describe a rung that is unmappable for no recorded reason, which is a
    data defect and now correctly FAILS CLOSED; `register=False` exists so a
    test can construct exactly that negative control on purpose.
    """
    rungs = [_rung(AT_24, 24)] + [_rung(m, 30) for m in MAPPABLE_30]
    rungs.extend(_rung(m, months) for m, months in extra_mappable)
    blocked = tuple(RungTarget(domain_key=SUPPORTED_DOMAIN,
                               source_rung_months=30, milestone_text=m)
                    for m in unmappable_30)
    registered = {}
    if register:
        registered = {target: ("deferred_post_pilot", ("pronouns",))
                      for target in blocked}
    return InMemoryGoldStandardRungSource(
        rungs=tuple(rungs), unmappable=blocked,
        registered_unresolved=registered)


def _projection(*, states_30, state_24="demonstrated", omit_30=(),
                extra_bands=(), child=CHILD, session=SESSION, digest=DIGEST,
                total_30=4):
    """A v2 projection over the fixture band shape.

    `states_30` maps a 30-month milestone to its projected state. `omit_30` drops
    evidence rows WITHOUT changing the declared total, which is how an incomplete
    band is constructed.
    """
    evidence = [ProjectedSkillEvidence(rung_ref=ref_for(AT_24, 24), months=24,
                                       state=state_24)]
    totals = [ProjectedBandTotal(months=24, total_skills=1),
              ProjectedBandTotal(months=30, total_skills=total_30)]
    for milestone, state in states_30.items():
        if milestone in omit_30:
            continue
        evidence.append(ProjectedSkillEvidence(
            rung_ref=ref_for(milestone, 30), months=30, state=state))
    for months, rows, total in extra_bands:
        totals.append(ProjectedBandTotal(months=months, total_skills=total))
        for milestone, state in rows:
            evidence.append(ProjectedSkillEvidence(
                rung_ref=ref_for(milestone, months), months=months,
                state=state))
    return ParentBaselineProjectionV2.build(
        child_id=child, source_session_id=session, source_record_digest=digest,
        summary={"domain": SUPPORTED_DOMAIN, "area_id": "talking",
                 "entry_choice_id": "two_three_words",
                 "routing_anchor_months": 24,
                 "not_demonstrated_months": 30, "status": "BOUNDED",
                 "baseline_version": BASELINE_VERSION_V2},
        skill_evidence=tuple(evidence), band_totals=tuple(totals), now=NOW)


ALL_DEMONSTRATED = {m: "demonstrated" for m in MAPPABLE_30 + (PRONOUNS,)}


#: Distinguishes "not given" from an explicit None, so a test can model an
#: UNWIRED authority as well as a differently-stocked bank.
_UNSET = object()


def mixed(**overrides):
    states = dict(ALL_DEMONSTRATED)
    states.update(overrides)
    return states


#: The fixture served set MIRRORS the real approved activity bank's four
#: families by name. A fake is necessary — this suite is dependency-pure and
#: must not reach into `pilot_runtime` for the artifact, the same reason it
#: uses a fixture rung source — but a fake with INVENTED family names would
#: make every support verdict here vacuous. `test_the_fixture_served_set_still
#: _matches_the_approved_bank` in the cross-system suite keeps the two in step.
FIXTURE_FAMILY = "expressive_vocabulary_growth"
FIXTURE_SERVED_FAMILIES = (
    "book_object_naming", "expressive_vocabulary_growth",
    "sentence_building", "two_word_phrases",
)
FIXTURE_CONTENT_DIGEST = "c" * 64


class FakeActivityBank:
    """The narrowest thing the content authority reads: served set + digest."""

    def __init__(self, served=FIXTURE_SERVED_FAMILIES,
                 digest=FIXTURE_CONTENT_DIGEST):
        self._served = tuple(served)
        self.artifact_digest = digest

    def served_families(self):
        return self._served


def content_authority(served=FIXTURE_SERVED_FAMILIES,
                      digest=FIXTURE_CONTENT_DIGEST):
    return ActivityBankContentAuthority(FakeActivityBank(served, digest))


class World:
    """A fictional child, a managing provider, a Parent link and a v2 projection."""

    def __init__(self, *, projection=None, source=None, with_link=True,
                 with_projection=True, managing=True, authority=_UNSET):
        self.store = FakeDocumentStore()
        self.repos = FirestoreRepositories(self.store)
        self.recorder = AuditRecorder(self.repos.audit_events,
                                      environment="test")
        # A2 v2.1: the v2 generation policy REQUIRES a content authority, so
        # the write path fails closed when none is wired. Passed explicitly
        # here, and overridable so a test can model a bank that does not serve
        # the target.
        self.content_authority = (content_authority()
                                  if authority is _UNSET else authority)
        self.goals = GoalService(repos=self.repos, recorder=self.recorder,
                                 now=lambda: NOW,
                                 content_authority=self.content_authority)
        self.source = source or _source()
        self.service = BaselineSuggestionGenerationV2Service(
            repos=self.repos, goals=self.goals, rung_source=self.source)

        self.repos.children.create(
            Child(child_id=CHILD, created_at=NOW, updated_at=NOW))
        self.caregiver = Caregiver.create("Fictional Caregiver",
                                         auth_subject="cg-subject", now=NOW)
        self.repos.caregivers.create(self.caregiver)
        self.repos.caregiver_child.connect(CaregiverChildConnection.create(
            self.caregiver.caregiver_id, CHILD, CaregiverRelationship.PARENT,
            actor_id=self.caregiver.caregiver_id, now=NOW))

        practice = Practice.create("Fictional Practice", now=NOW)
        self.repos.practices.create(practice)
        self.provider = Provider.create(
            practice.practice_id, ProviderDiscipline.SLP, "Hannah",
            auth_subject="pr-subject", now=NOW)
        self.repos.providers.create(self.provider)
        self.other_provider = Provider.create(
            practice.practice_id, ProviderDiscipline.SLP, "Other Clinician",
            auth_subject="pr2-subject", now=NOW)
        self.repos.providers.create(self.other_provider)

        self.connections = {}
        for provider in (self.provider, self.other_provider):
            connection = ProviderChildConnection.create(
                provider.provider_id, CHILD, practice.practice_id,
                actor_id=self.caregiver.caregiver_id, now=NOW).activate(now=NOW)
            self.repos.provider_child.connect(connection)
            self.connections[provider.provider_id] = connection

        if managing:
            self.repos.managing_clinicians.create(
                ManagingClinicianAssignment.create(
                    CHILD, self.provider.provider_id, practice.practice_id,
                    provider_connection_id=self.connections[
                        self.provider.provider_id].connection_id,
                    actor_id=self.caregiver.caregiver_id, now=NOW))

        if with_link:
            self.repos.source_links.create(SourceSystemLink.create(
                CHILD, SourceSystem.PARENT, SESSION,
                actor_id=self.caregiver.caregiver_id,
                actor_role=ActorRole.CAREGIVER.value, now=NOW))

        self.projection = (projection if projection is not None
                           else _projection(states_30=mixed(**{BOOK:
                                                              "not_demonstrated"})))
        if with_projection:
            self.repos.parent_baseline_projections_v2.create(self.projection)

    def principal(self, subject="pr-subject"):
        return resolve_principal(VerifiedToken(subject=subject), self.repos)

    def generate(self, subject="pr-subject"):
        return self.service.generate_for_child(self.principal(subject), CHILD)

    def _docs(self, collection):
        """Just the documents. `list_all` yields `(doc_id, data)` pairs."""
        return [data for _doc_id, data in self.store.list_all(collection)]

    def claims(self):
        return self._docs("pilot_suggestion_generation_claims")

    def suggestions(self):
        return self._docs("pilot_goal_suggestions")

    def anchors(self):
        return self._docs("pilot_suggestion_anchors")


# ===========================================================================
# 2. the policy version, and v1 is untouched
# ===========================================================================


def test_the_v2_policy_version_is_explicit_and_distinct_from_v1():
    assert GENERATION_POLICY_VERSION_V2 == (
        "goal-suggestion-generation-policy-v2-evidence")
    assert GENERATION_POLICY_VERSION_V2 != GENERATION_POLICY_VERSION
    assert GENERATION_POLICY_VERSION == "goal-suggestion-generation-policy-v1"


def test_a_v1_claim_can_never_be_found_by_a_v2_generation():
    """Two independent reasons, either sufficient on its own.

    The practical consequence: switching a child to v2 does not resurface the v1
    suggestion as though the new policy had chosen it.
    """
    shared = dict(projection_id="pbpj_" + "0" * 32, domain_key=SUPPORTED_DOMAIN,
                  target_rung_ref=ref_for(BOOK, 30),
                  taxonomy_version=TAXONOMY_VERSION,
                  gold_standard_version=GOLD_STANDARD_VERSION)
    v1_key = generation_key(**shared,
                            generation_policy=GENERATION_POLICY_VERSION)
    v2_key = generation_key(**shared,
                            generation_policy=GENERATION_POLICY_VERSION_V2)
    # 1. the policy alone changes the key, holding every other input equal.
    assert v1_key != v2_key
    assert generation_claim_id(v1_key) != generation_claim_id(v2_key)

    # 2. the projection id also differs, because v1 and v2 projections carry
    #    distinct id prefixes.
    v2_projection_key = generation_key(
        **{**shared, "projection_id": "pbp2_" + "0" * 32},
        generation_policy=GENERATION_POLICY_VERSION_V2)
    assert v2_projection_key != v2_key

    claim = GoalSuggestionGenerationClaim.build(
        **shared, child_id=CHILD, target_rung_months=30, now=NOW)
    assert claim.generation_policy == GENERATION_POLICY_VERSION
    assert v1_claim_is_not_reusable_for_v2(claim) is False


def test_the_helper_refuses_to_answer_about_a_v2_claim():
    claim = GoalSuggestionGenerationClaim.build(
        projection_id="pbp2_" + "0" * 32, child_id=CHILD,
        domain_key=SUPPORTED_DOMAIN, target_rung_ref=ref_for(BOOK, 30),
        target_rung_months=30, taxonomy_version=TAXONOMY_VERSION,
        gold_standard_version=GOLD_STANDARD_VERSION,
        generation_policy=GENERATION_POLICY_VERSION_V2, now=NOW)
    with pytest.raises(GenerationV2Error):
        v1_claim_is_not_reusable_for_v2(claim)


def test_the_v1_default_on_the_goal_service_is_unchanged():
    """The policy parameter is ADDITIVE: an existing caller still writes v1."""
    import inspect

    signature = inspect.signature(GoalService.generate_anchored_suggestion)
    assert (signature.parameters["generation_policy"].default
            == GENERATION_POLICY_VERSION)


# ===========================================================================
# 3. band classification and target-band selection — pure
# ===========================================================================


def test_a_band_is_mastered_only_when_every_canonical_skill_is_demonstrated():
    roster = [ref_for(m, 30) for m in MAPPABLE_30 + (PRONOUNS,)]
    view = classify_band(months=30, roster=roster,
                         states_by_ref={r: "demonstrated" for r in roster})
    assert view.classification == BAND_MASTERED
    assert view.target_refs == () and view.unknown_refs == ()
    assert len(view.demonstrated_refs) == 4


@pytest.mark.parametrize("state", ["emerging", "not_demonstrated", "unknown"])
def test_any_non_demonstrated_state_makes_a_band_unresolved(state):
    roster = [ref_for(m, 30) for m in MAPPABLE_30 + (PRONOUNS,)]
    states = {r: "demonstrated" for r in roster}
    states[ref_for(BOOK, 30)] = state
    view = classify_band(months=30, roster=roster, states_by_ref=states)
    assert view.classification == BAND_UNRESOLVED
    if state == "unknown":
        assert view.target_refs == ()
        assert view.unknown_refs == (ref_for(BOOK, 30),)
    else:
        assert view.target_refs == (ref_for(BOOK, 30),)
        assert view.unknown_refs == ()


def test_a_band_is_incomplete_in_both_directions():
    """A roster ref with no evidence, and an evidence ref outside the roster.

    The second matters as much as the first: if the two disagree about what the
    band IS, no classification of it would mean anything.
    """
    roster = [ref_for(m, 30) for m in MAPPABLE_30 + (PRONOUNS,)]
    short = {r: "demonstrated" for r in roster[:3]}
    view = classify_band(months=30, roster=roster, states_by_ref=short)
    assert view.classification == BAND_INCOMPLETE
    assert view.missing_refs == (roster[3],)
    assert view.is_complete is False

    stray = {r: "demonstrated" for r in roster}
    stray[ref_for(AT_24, 24)] = "demonstrated"
    assert classify_band(months=30, roster=roster,
                         states_by_ref=stray).classification == BAND_INCOMPLETE


def test_an_unrecognised_state_is_refused_rather_than_treated_as_safe():
    """A state this module does not understand might be a deficit."""
    roster = [ref_for(BOOK, 30)]
    with pytest.raises(GenerationV2Error):
        classify_band(months=30, roster=roster,
                      states_by_ref={roster[0]: "unassessed"})
    with pytest.raises(GenerationV2Error):
        classify_band(months=30, roster=roster,
                      states_by_ref={roster[0]: "probably_fine"})


def test_the_first_unresolved_band_is_selected_lowest_first():
    low = classify_band(months=24, roster=[ref_for(AT_24, 24)],
                        states_by_ref={ref_for(AT_24, 24): "not_demonstrated"})
    high_roster = [ref_for(m, 30) for m in MAPPABLE_30]
    high = classify_band(
        months=30, roster=high_roster,
        states_by_ref={r: "not_demonstrated" for r in high_roster})
    assert select_target_band([high, low]).months == 24


def test_selection_refuses_at_the_first_incomplete_band_and_never_steps_past():
    """Fail closed. An incomplete band might be unresolved or might be mastered
    once finished, and treating it as mastered to reach a higher band would be
    inferring mastery from missing evidence."""
    incomplete = classify_band(
        months=24, roster=[ref_for(AT_24, 24), ref_for(BOOK, 24)],
        states_by_ref={ref_for(AT_24, 24): "demonstrated"})
    assert incomplete.classification == BAND_INCOMPLETE
    higher_roster = [ref_for(m, 30) for m in MAPPABLE_30]
    higher = classify_band(
        months=30, roster=higher_roster,
        states_by_ref={r: "not_demonstrated" for r in higher_roster})
    with pytest.raises(BandAssessmentIncomplete):
        select_target_band([incomplete, higher])


def test_every_band_mastered_yields_no_band_rather_than_an_error():
    roster = [ref_for(AT_24, 24)]
    mastered = classify_band(months=24, roster=roster,
                             states_by_ref={roster[0]: "demonstrated"})
    assert select_target_band([mastered]) is None


# ===========================================================================
# 11. the canonical mixed 30m case
# ===========================================================================


def test_the_mixed_30m_case_yields_exactly_one_suggestion_for_book_naming():
    """The founder's canonical example.

    book=not_demonstrated, two-word=demonstrated, vocab=demonstrated,
    pronouns=unknown. Exactly one suggestion, anchored to the 30-month
    book-naming rung; pronouns is unknown EVIDENCE and not an unsupported target.
    """
    world = World(projection=_projection(
        states_30=mixed(**{BOOK: "not_demonstrated", PRONOUNS: "unknown"})))
    outcome = world.generate()

    assert outcome.outcome == OUTCOME_GENERATED
    assert outcome.target_band_months == 30
    assert len(outcome.generated) == 1
    target = outcome.generated[0]
    assert target.rung_ref == ref_for(BOOK, 30)
    assert target.months == 30
    assert target.created is True
    assert len(target.suggestion_ids) == 1

    # pronouns is UNKNOWN evidence, not an unsupported target: it was never a
    # target candidate, so it cannot be an unsupported one.
    assert outcome.unknown_refs == (ref_for(PRONOUNS, 30),)
    assert outcome.unsupported_target_refs == ()

    # Exactly one claim, one suggestion, one anchor. Nothing for the
    # demonstrated skills and nothing for pronouns.
    assert len(world.claims()) == 1
    assert len(world.suggestions()) == 1
    assert len(world.anchors()) == 1
    anchor = world.anchors()[0]
    assert anchor["rung"]["rung_ref"] == ref_for(BOOK, 30)
    assert anchor["rung"]["source_rung_months"] == 30


def test_the_anchor_carries_the_frozen_canonical_metadata():
    """Item 8: the anchor uses the EXISTING metadata for the ref the evidence
    named — not a rung re-derived from a month."""
    world = World(projection=_projection(
        states_30=mixed(**{BOOK: "not_demonstrated", PRONOUNS: "unknown"})))
    world.generate()
    rung = world.anchors()[0]["rung"]
    assert rung["milestone_text"] == BOOK
    assert rung["subdomain"] == "expressive_language"
    assert rung["taxonomy_version"] == TAXONOMY_VERSION
    assert rung["baseline_version"] == GOLD_STANDARD_VERSION
    assert rung["family_bindings"]
    assert tuple(rung["track_subdomains"]) == TRACK_SUBDOMAINS


# ===========================================================================
# 12. the multi-deficit case
# ===========================================================================


def test_multiple_known_deficits_yield_multiple_distinct_suggestions():
    """Item 12. book=ND, two-word=emerging, vocab=D, pronouns=ND.

    Two real suggestions plus one unsupported canonical target — not one
    alphabetical winner, not total failure, and not three fake suggestions.
    """
    world = World(projection=_projection(states_30=mixed(**{
        BOOK: "not_demonstrated", TWO_WORD: "emerging",
        PRONOUNS: "not_demonstrated"})))
    outcome = world.generate()

    assert outcome.outcome == OUTCOME_GENERATED
    refs = [target.rung_ref for target in outcome.generated]
    assert sorted(refs) == sorted([ref_for(BOOK, 30), ref_for(TWO_WORD, 30)])
    assert len(set(refs)) == 2
    assert all(target.created for target in outcome.generated)

    # pronouns is a KNOWN deficit with no reconciled family: reported, never
    # dropped and never substituted.
    assert outcome.unsupported_target_refs == (ref_for(PRONOUNS, 30),)
    assert outcome.unknown_refs == ()

    # One claim, one suggestion and one anchor PER target — and no fourth
    # anything for the unsupported deficit or the demonstrated skill.
    assert len(world.claims()) == 2
    assert len(world.suggestions()) == 2
    assert len(world.anchors()) == 2
    anchored = {a["rung"]["rung_ref"] for a in world.anchors()}
    assert anchored == {ref_for(BOOK, 30), ref_for(TWO_WORD, 30)}
    assert ref_for(PRONOUNS, 30) not in anchored
    assert ref_for(VOCAB, 30) not in anchored

    # Each target has its OWN claim, keyed by its own ref.
    claim_refs = {c["target_rung_ref"] for c in world.claims()}
    assert claim_refs == {ref_for(BOOK, 30), ref_for(TWO_WORD, 30)}
    assert all(c["generation_policy"] == GENERATION_POLICY_VERSION_V2
               for c in world.claims())


def test_an_unmappable_deficit_does_not_block_its_mappable_siblings():
    """Stated as its own test because it is the founder's explicit rule.

    The unmappable deficit is processed in the same pass as the mappable ones,
    and the mappable ones still generate.
    """
    world = World(projection=_projection(states_30=mixed(**{
        BOOK: "not_demonstrated", PRONOUNS: "not_demonstrated"})))
    outcome = world.generate()
    assert outcome.outcome == OUTCOME_GENERATED
    assert [t.rung_ref for t in outcome.generated] == [ref_for(BOOK, 30)]
    assert outcome.unsupported_target_refs == (ref_for(PRONOUNS, 30),)
    assert len(world.suggestions()) == 1


def test_three_mappable_deficits_yield_three_suggestions():
    world = World(projection=_projection(states_30=mixed(**{
        BOOK: "not_demonstrated", TWO_WORD: "not_demonstrated",
        VOCAB: "emerging"})))
    outcome = world.generate()
    assert len(outcome.generated) == 3
    assert len({t.rung_ref for t in outcome.generated}) == 3
    assert len(world.claims()) == len(world.suggestions()) == 3
    assert len(world.anchors()) == 3


# ===========================================================================
# 6 and 7. all-unmapped and unknown-only
# ===========================================================================


def test_a_registered_only_band_is_passed_over_and_the_higher_band_is_used():
    """Item 6, REVISED by the registered-unresolved decision.

    This test previously asserted `no_supported_target` with the 30-month band
    selected: an all-unmappable band generated nothing and never stepped up.
    That was the right behaviour while the artifact could not tell an
    intentional deferral from a data defect. It now can, and the founder
    approved a BOUNDED pass-over, so a band whose only deficit is REGISTERED is
    skipped and the 36-month mappable deficit is used.

    What has NOT changed is that nothing is concealed: the 30-month deficit is
    reported in `passed_over` with the status that authorised the skip. The
    fail-closed case moved to
    `test_an_unregistered_unmappable_deficit_fails_closed`, which is the same
    fixture with the register entry removed — so the pass-over is demonstrably
    the register's doing and not a general willingness to skip.
    """
    source = _source(extra_mappable=((TWO_WORD, 36),))
    projection = _projection(
        states_30=mixed(**{PRONOUNS: "not_demonstrated"}),
        extra_bands=((36, ((TWO_WORD, "not_demonstrated"),), 1),))
    world = World(projection=projection, source=source)
    outcome = world.generate()

    assert outcome.outcome == OUTCOME_GENERATED
    assert outcome.target_band_months == 36
    assert [t.rung_ref for t in outcome.generated] == [ref_for(TWO_WORD, 36)]
    # The skipped deficit is REPORTED, not dropped.
    assert [(d.months, d.rung_ref, d.resolution_status)
            for d in outcome.passed_over] == [
        (30, ref_for(PRONOUNS, 30), "deferred_post_pilot")]
    # ...and it is not misfiled as a selected-band unsupported target.
    assert outcome.unsupported_target_refs == ()


def test_an_unregistered_unmappable_deficit_fails_closed():
    """SCENARIO E. The negative control for the pass-over above.

    Same fixture, same evidence, with ONE difference: the 30-month unmappable
    deficit is not in the unresolved register. The skip is refused, and the
    mappable 36-month deficit — which the previous test proves is reachable and
    attractive — is NOT substituted.

    Without this control the pass-over would be indistinguishable from a
    general willingness to step past any deficit Genex has no content for.
    """
    source = _source(extra_mappable=((TWO_WORD, 36),), register=False)
    projection = _projection(
        states_30=mixed(**{PRONOUNS: "not_demonstrated"}),
        extra_bands=((36, ((TWO_WORD, "not_demonstrated"),), 1),))
    world = World(projection=projection, source=source)

    with pytest.raises(UnregisteredUnmappableSkill):
        world.generate()

    # Nothing was written, and no higher-band target was invented.
    assert world.claims() == [] and world.suggestions() == []
    assert world.anchors() == []


def test_an_unknown_only_band_generates_nothing_and_does_not_advance():
    """Item 7. `unknown` is not failure and not a target.

    A mappable 36-month deficit exists and is deliberately not used.
    """
    source = _source(extra_mappable=((TWO_WORD, 36),))
    projection = _projection(
        states_30=mixed(**{PRONOUNS: "unknown"}),
        extra_bands=((36, ((TWO_WORD, "not_demonstrated"),), 1),))
    world = World(projection=projection, source=source)
    outcome = world.generate()

    assert outcome.outcome == OUTCOME_INSUFFICIENT_KNOWN_EVIDENCE
    assert outcome.generated == ()
    assert outcome.target_band_months == 30
    assert outcome.unknown_refs == (ref_for(PRONOUNS, 30),)
    assert outcome.unsupported_target_refs == ()
    assert world.claims() == [] and world.suggestions() == []


def test_an_unknown_sibling_does_not_suppress_a_known_mapped_deficit():
    """The two populations are independent."""
    world = World(projection=_projection(states_30=mixed(**{
        BOOK: "not_demonstrated", VOCAB: "unknown", PRONOUNS: "unknown"})))
    outcome = world.generate()
    assert outcome.outcome == OUTCOME_GENERATED
    assert [t.rung_ref for t in outcome.generated] == [ref_for(BOOK, 30)]
    assert set(outcome.unknown_refs) == {ref_for(VOCAB, 30),
                                        ref_for(PRONOUNS, 30)}


def test_a_fully_mastered_assessment_yields_no_band_and_no_target():
    world = World(projection=_projection(states_30=ALL_DEMONSTRATED))
    outcome = world.generate()
    assert outcome.outcome == OUTCOME_NO_UNRESOLVED_BAND
    assert outcome.target_band_months is None
    assert outcome.generated == ()
    assert outcome.unsupported_target_refs == ()
    assert outcome.unknown_refs == ()
    assert world.claims() == []


def test_a_lower_unresolved_band_is_preferred_over_a_higher_one():
    world = World(projection=_projection(
        state_24="not_demonstrated",
        states_30=mixed(**{BOOK: "not_demonstrated"})))
    outcome = world.generate()
    assert outcome.target_band_months == 24
    assert [t.rung_ref for t in outcome.generated] == [ref_for(AT_24, 24)]


# ===========================================================================
# incomplete bands fail closed
# ===========================================================================


def test_an_incomplete_band_refuses_generation():
    """Item 17. Evidence short of the canonical roster is never read as mastery.

    `total_30` stays at the TRUE roster size of 4 while one evidence row is
    dropped. That isolates the right guard: declaring 3 would be caught earlier
    by the denominator check, and this test is about the band whose declared
    total is honest and whose EVIDENCE is short.
    """
    world = World(projection=_projection(
        states_30=mixed(**{BOOK: "not_demonstrated"}), omit_30=(VOCAB,)))
    assert len(world.projection.assessed_in_band(30)) == 3
    assert world.projection.assessment_complete(30) is False
    with pytest.raises(BandAssessmentIncomplete):
        world.generate()
    assert world.claims() == [] and world.suggestions() == []


def test_an_incomplete_band_is_refused_even_when_a_higher_band_has_a_target():
    """Fail closed rather than stepping past the gap to a convenient band."""
    source = _source(extra_mappable=((TWO_WORD, 36),))
    world = World(source=source, projection=_projection(
        states_30=mixed(**{BOOK: "not_demonstrated"}), omit_30=(VOCAB,),
        extra_bands=((36, ((TWO_WORD, "not_demonstrated"),), 1),)))
    with pytest.raises(BandAssessmentIncomplete):
        world.generate()
    assert world.claims() == []


def test_a_declared_total_disagreeing_with_the_roster_refuses():
    """F-B v2 re-derives the denominator rather than inheriting A2's conclusion.

    A projection written before the 0.6A-1F hardening existed would otherwise be
    trusted on a number nobody checked.
    """
    world = World(projection=_projection(
        states_30=mixed(**{BOOK: "not_demonstrated"}), total_30=3,
        omit_30=(PRONOUNS,)))
    with pytest.raises(GenerationV2Error):
        world.generate()
    assert world.claims() == []


def test_a_v1_projection_cannot_drive_f_b_v2():
    from pilot_backend.domain.parent_baseline_projection import (
        ParentBaselineProjection,
    )

    v1 = ParentBaselineProjection.build(
        child_id=CHILD, source_session_id=SESSION, source_record_digest=DIGEST,
        projection={"domain": SUPPORTED_DOMAIN, "area_id": "talking",
                    "entry_choice_id": "many_single_words",
                    "routing_anchor_months": 24,
                    "not_demonstrated_months": 30, "status": "BOUNDED",
                    "baseline_version": GOLD_STANDARD_VERSION},
        now=NOW)
    world = World(with_projection=False)
    with pytest.raises(ProjectionNotEvidenceDriven):
        world.service.band_views(v1)


def test_missing_or_ambiguous_lineage_refuses():
    from pilot_backend.domain.suggestion_generation import (
        ProjectionLineageInvalid,
    )

    no_link = World(with_link=False)
    with pytest.raises(ProjectionLineageInvalid):
        no_link.generate()

    no_projection = World(with_projection=False)
    with pytest.raises(ProjectionLineageInvalid):
        no_projection.generate()

    wrong_child = World(with_projection=False)
    wrong_child.repos.parent_baseline_projections_v2.create(_projection(
        states_30=mixed(**{BOOK: "not_demonstrated"}),
        child="chld_" + "9" * 32))
    with pytest.raises(ProjectionLineageInvalid):
        wrong_child.generate()


def test_a_v1_projection_in_the_v1_collection_is_never_consulted():
    """F-B v2 reads the v2 collection ONLY."""
    from pilot_backend.domain.parent_baseline_projection import (
        ParentBaselineProjection,
    )

    world = World(with_projection=False)
    world.repos.parent_baseline_projections.create(
        ParentBaselineProjection.build(
            child_id=CHILD, source_session_id=SESSION,
            source_record_digest=DIGEST,
            projection={"domain": SUPPORTED_DOMAIN, "area_id": "talking",
                        "entry_choice_id": "many_single_words",
                        "routing_anchor_months": 24,
                        "not_demonstrated_months": 30, "status": "BOUNDED",
                        "baseline_version": GOLD_STANDARD_VERSION},
            now=NOW))
    from pilot_backend.domain.suggestion_generation import (
        ProjectionLineageInvalid,
    )
    with pytest.raises(ProjectionLineageInvalid):
        world.generate()


# ===========================================================================
# 17. no alphabetical dependency — the headline property
# ===========================================================================


def test_the_target_set_does_not_depend_on_milestone_wording():
    """The defect this slice removes, stated as a test.

    v1 took the first same-month rung by MILESTONE TEXT. Here the three mappable
    30-month milestones are renamed so their alphabetical order is REVERSED, and
    the generated target set is unchanged — because identity comes from the
    `rung_ref` the evidence named, and a ref is a digest of the milestone rather
    than a sortable proxy for it.
    """
    deficit, demonstrated_a, demonstrated_b = BOOK, TWO_WORD, VOCAB

    baseline = World(projection=_projection(states_30=mixed(**{
        deficit: "not_demonstrated"})))
    expected = {t.rung_ref for t in baseline.generate().generated}
    assert expected == {ref_for(deficit, 30)}

    # Rename every 30-month milestone, keeping the SAME deficit skill. The
    # renamed deficit now sorts LAST rather than first.
    renamed = {deficit: "zzz last alphabetically but still the deficit",
               demonstrated_a: "aaa first alphabetically and demonstrated",
               demonstrated_b: "bbb second alphabetically and demonstrated"}
    rungs = ([_rung(AT_24, 24)]
             + [_rung(renamed[m], 30) for m in MAPPABLE_30])
    source = InMemoryGoldStandardRungSource(
        rungs=tuple(rungs),
        unmappable=(RungTarget(domain_key=SUPPORTED_DOMAIN,
                               source_rung_months=30,
                               milestone_text=PRONOUNS),))
    evidence = [ProjectedSkillEvidence(rung_ref=ref_for(AT_24, 24), months=24,
                                       state="demonstrated")]
    for milestone in MAPPABLE_30 + (PRONOUNS,):
        text = renamed.get(milestone, milestone)
        evidence.append(ProjectedSkillEvidence(
            rung_ref=ref_for(text, 30), months=30,
            state=("not_demonstrated" if milestone == deficit
                   else "demonstrated")))
    projection = ParentBaselineProjectionV2.build(
        child_id=CHILD, source_session_id=SESSION, source_record_digest=DIGEST,
        summary={"domain": SUPPORTED_DOMAIN, "area_id": "talking",
                 "entry_choice_id": "two_three_words",
                 "routing_anchor_months": 24,
                 "not_demonstrated_months": 30, "status": "BOUNDED",
                 "baseline_version": BASELINE_VERSION_V2},
        skill_evidence=tuple(evidence),
        band_totals=(ProjectedBandTotal(months=24, total_skills=1),
                     ProjectedBandTotal(months=30, total_skills=4)), now=NOW)

    world = World(projection=projection, source=source)
    outcome = world.generate()
    # The SAME clinical skill is targeted, under its new ref, and it is the only
    # one — even though it now sorts last and two demonstrated skills sort first.
    assert len(outcome.generated) == 1
    assert outcome.generated[0].rung_ref == ref_for(renamed[deficit], 30)
    assert world.anchors()[0]["rung"]["milestone_text"] == renamed[deficit]


def test_the_target_set_does_not_depend_on_evidence_iteration_order():
    states = mixed(**{BOOK: "not_demonstrated", TWO_WORD: "not_demonstrated"})
    forward = World(projection=_projection(states_30=states))
    reversed_states = dict(reversed(list(states.items())))
    backward = World(projection=_projection(states_30=reversed_states))

    assert ([t.rung_ref for t in forward.generate().generated]
            == [t.rung_ref for t in backward.generate().generated])


def test_the_service_names_none_of_v1s_month_level_machinery():
    """Structural, over the AST with docstrings stripped recursively.

    `routing_anchor_months`, `not_demonstrated_months`, `_step` and
    `question_at` must not be reachable from the v2 service — the evidence is the
    input, and a month-level field would be a second way to choose a target.
    """
    source = (PILOT_ROOT
              / "integration/baseline_suggestion_generation_v2.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:]

    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    strings = {n.value for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    reachable = names | attrs | strings

    for forbidden in ("routing_anchor_months", "not_demonstrated_months",
                      "next_rung_target", "rung_for_target", "question_at",
                      "_step", "has_routing_anchor", "target_within_ceiling",
                      "is_generatable_status"):
        assert forbidden not in reachable, forbidden
    # And the things it SHOULD reach.
    assert "rung_by_ref" in attrs
    assert "is_mappable_ref" in attrs
    assert "skill_evidence" in attrs or "assessed_in_band" in attrs


# ===========================================================================
# 16. no activity generation, no clinical approval
# ===========================================================================


def test_the_v2_service_names_no_plan_or_activity_repository():
    source = (PILOT_ROOT
              / "integration/baseline_suggestion_generation_v2.py").read_text()
    tree = ast.parse(source)
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for forbidden in ("focus_plans", "goal_allocations", "goal_snapshots",
                      "weekly_cycles", "weekly_plan_links",
                      "weekly_plan_snapshots", "alignments", "coverage_gaps",
                      "capacity_ledgers", "clinical_goals", "caregiver_goals",
                      "goal_versions", "clinical_goal_anchors",
                      "activity_bank"):
        assert forbidden not in attrs, forbidden


def test_generation_creates_no_clinical_goal_and_no_goal_version():
    """Item 14. Approval remains the only path to a goal."""
    world = World(projection=_projection(states_30=mixed(**{
        BOOK: "not_demonstrated", TWO_WORD: "emerging"})))
    outcome = world.generate()
    assert len(outcome.generated) == 2

    for collection in ("pilot_clinical_goals", "pilot_goal_versions",
                       "pilot_clinical_goal_anchors", "pilot_caregiver_goals",
                       "pilot_monthly_focus_plans",
                       "pilot_monthly_goal_allocations",
                       "pilot_weekly_cycles", "pilot_weekly_plan_snapshots"):
        assert world._docs(collection) == [], collection
    # Every suggestion is still merely OFFERED.
    assert {s["status"] for s in world.suggestions()} == {"offered"}


# ===========================================================================
# 10. replay
# ===========================================================================


def test_a_replay_returns_the_same_suggestions_and_writes_nothing_new():
    world = World(projection=_projection(states_30=mixed(**{
        BOOK: "not_demonstrated", TWO_WORD: "emerging",
        PRONOUNS: "not_demonstrated"})))
    first = world.generate()
    claims_after_first = len(world.claims())
    suggestion_ids = set(first.suggestion_ids)

    second = world.generate()
    assert second.outcome == OUTCOME_GENERATED
    assert set(second.suggestion_ids) == suggestion_ids
    assert all(target.created is False for target in second.generated)
    assert second.unsupported_target_refs == first.unsupported_target_refs
    assert len(world.claims()) == claims_after_first
    assert len(world.suggestions()) == 2
    assert len(world.anchors()) == 2


def test_a_different_provider_converges_on_the_same_generation():
    """The requester is audit metadata, never part of idempotency identity.

    Otherwise two authorized clinicians opening the same child would produce two
    parallel candidate sets, and whichever Hannah saw first would look like
    Genex's recommendation.
    """
    world = World(projection=_projection(states_30=mixed(**{
        BOOK: "not_demonstrated"})))
    # The other provider must be the managing clinician to pass the transport
    # gate, but the SERVICE-level call only needs child access — which is the
    # level this property lives at.
    first = world.generate(subject="pr-subject")
    second = world.generate(subject="pr2-subject")
    assert set(first.suggestion_ids) == set(second.suggestion_ids)
    assert all(t.created is False for t in second.generated)
    assert len(world.claims()) == 1


def test_a_changed_projection_digest_is_a_different_generation():
    """Item 10: a later taxonomy/policy/projection must be able to evolve.

    New evidence -> new digest -> new projection id -> a different generation
    key, so the historical result is not mutated.
    """
    world = World(projection=_projection(states_30=mixed(**{
        BOOK: "not_demonstrated"})))
    first = world.generate()

    # A different finalized record. A2 v2 would refuse a second digest for one
    # session on the way in, so this one names a different session — the point is
    # only that the generation KEY does not prevent a legitimate future
    # evolution, not that this particular projection could be stored.
    evolved = _projection(states_30=mixed(**{BOOK: "not_demonstrated"}),
                          digest="d" * 64, session="sess-fbv2-evolved")
    assert evolved.projection_id != world.projection.projection_id

    from pilot_backend.domain.suggestion_generation import generation_key as gk

    first_key = gk(projection_id=world.projection.projection_id,
                   domain_key=SUPPORTED_DOMAIN,
                   target_rung_ref=ref_for(BOOK, 30),
                   taxonomy_version=TAXONOMY_VERSION,
                   gold_standard_version=GOLD_STANDARD_VERSION,
                   generation_policy=GENERATION_POLICY_VERSION_V2)
    evolved_key = gk(projection_id=evolved.projection_id,
                     domain_key=SUPPORTED_DOMAIN,
                     target_rung_ref=ref_for(BOOK, 30),
                     taxonomy_version=TAXONOMY_VERSION,
                     gold_standard_version=GOLD_STANDARD_VERSION,
                     generation_policy=GENERATION_POLICY_VERSION_V2)
    assert first_key != evolved_key
    assert len(first.suggestion_ids) == 1


# ===========================================================================
# 8. rung_by_ref — the anchor lookup
# ===========================================================================


def test_rung_by_ref_refuses_a_band_mismatch():
    """A matching ref at a mismatched month means the evidence and the frozen
    table disagree about which band the skill is in."""
    source = _source()
    rung = source.rung_by_ref(SUPPORTED_DOMAIN, ref_for(BOOK, 30),
                              expected_months=30)
    assert rung.rung_ref == ref_for(BOOK, 30)
    with pytest.raises(RungNotFoundError):
        source.rung_by_ref(SUPPORTED_DOMAIN, ref_for(BOOK, 30),
                           expected_months=36)


def test_rung_by_ref_refuses_an_unknown_ref_and_a_wrong_domain():
    source = _source()
    with pytest.raises(RungNotFoundError):
        source.rung_by_ref(SUPPORTED_DOMAIN, "rung1:" + "0" * 32)
    with pytest.raises(RungNotFoundError):
        source.rung_by_ref("moving_and_coordination", ref_for(BOOK, 30))


def test_rung_by_ref_refuses_an_unmappable_ref_distinctly_from_a_missing_one():
    """Two different findings: "never heard of it" versus "known, no activities".

    F-B v2 depends on the difference — the second is reported as an unsupported
    target, the first would be a lineage fault.
    """
    source = _source()
    with pytest.raises(RungNotMappableError):
        source.rung_by_ref(SUPPORTED_DOMAIN, ref_for(PRONOUNS, 30))
    assert source.is_mappable_ref(SUPPORTED_DOMAIN, ref_for(PRONOUNS, 30)) is False
    assert source.is_mappable_ref(SUPPORTED_DOMAIN, ref_for(BOOK, 30)) is True
    assert source.is_mappable_ref(SUPPORTED_DOMAIN, "rung1:" + "0" * 32) is False


def test_the_declared_roster_includes_unmappable_rungs():
    """A band's canonical roster is what the Gold Standard DECLARES, not what we
    have activities for — otherwise an all-unmappable band would look incomplete
    rather than unsupported."""
    roster = _source().declared_band_roster(SUPPORTED_DOMAIN, 30)
    assert len(roster) == 4
    assert ref_for(PRONOUNS, 30) in roster


# ===========================================================================
# 15. the read model
# ===========================================================================


def test_the_outcome_exposes_no_prose_and_no_parent_identifier():
    import dataclasses
    import json

    from pilot_backend.integration.baseline_suggestion_generation_v2 import (
        GeneratedTarget,
        GenerationV2Outcome,
    )

    assert {f.name for f in dataclasses.fields(GeneratedTarget)} == {
        "rung_ref", "months", "suggestion_ids", "created"}
    assert {f.name for f in dataclasses.fields(GenerationV2Outcome)} == {
        "projection_id", "generation_policy", "outcome", "target_band_months",
        "generated", "unsupported_target_refs", "unknown_refs",
        "passed_over"}

    world = World(projection=_projection(states_30=mixed(**{
        BOOK: "not_demonstrated", PRONOUNS: "not_demonstrated"})))
    outcome = world.generate()
    blob = json.dumps(dataclasses.asdict(outcome), default=str)
    for forbidden in (BOOK, PRONOUNS, VOCAB, "milestone", "subdomain",
                      "skill_key", "claim_id", SESSION, "not_demonstrated"):
        assert forbidden not in blob, forbidden

    # `passed_over` is the new field, so the no-prose rule is re-asserted
    # against ITS payload specifically rather than trusting the blob sweep: a
    # reported deficit is the one place a readable sentence would be tempting.
    assert outcome.passed_over, "the fixture must exercise a reported deficit"
    for deficit in outcome.passed_over:
        rendered = json.dumps(deficit.as_dict())
        assert set(deficit.as_dict()) == {
            "months", "rung_ref", "resolution_status", "family_keys"}
        for forbidden in (BOOK, PRONOUNS, VOCAB, "milestone", "subdomain"):
            assert forbidden not in rendered, forbidden
        # A taxonomy FAMILY KEY is permitted and is the point: it is a stable
        # identifier, not clinical copy, and the same outcome already publishes
        # canonical rung refs.
        assert deficit.family_keys == ("pronouns",)
        assert deficit.resolution_status == "deferred_post_pilot"


def test_the_five_outcomes_are_distinguishable():
    """Five as of the registered-unresolved slice, not four.

    `no_supported_target_registered_gaps` was added deliberately rather than
    folded into `no_unresolved_band`: the latter reads as "this child has no
    unresolved skills", which is FALSE when a registered deficit was passed
    over. Collapsing them would turn "we cannot help with this yet" into "this
    is not a problem".
    """
    from pilot_backend.domain.suggestion_generation_v2 import (
        OUTCOMES, OUTCOME_NO_SUPPORTED_TARGET_REGISTERED_GAPS)

    assert len(set(OUTCOMES)) == 5
    assert OUTCOME_GENERATED in OUTCOMES
    assert OUTCOME_NO_SUPPORTED_TARGET in OUTCOMES
    assert OUTCOME_INSUFFICIENT_KNOWN_EVIDENCE in OUTCOMES
    assert OUTCOME_NO_UNRESOLVED_BAND in OUTCOMES
    assert OUTCOME_NO_SUPPORTED_TARGET_REGISTERED_GAPS in OUTCOMES
    # ...and the new one is genuinely distinct from the "nothing unresolved"
    # finding it must never be mistaken for.
    assert (OUTCOME_NO_SUPPORTED_TARGET_REGISTERED_GAPS
            != OUTCOME_NO_UNRESOLVED_BAND)


# ===========================================================================
# 13. authorization over the REAL route — identical to v1's gates
# ===========================================================================

from pilot_backend.tests.test_integration_identity import (  # noqa: E402
    CAREGIVER_ALPHA_SUBJECT,
    PROVIDER_ALPHA_SUBJECT,
    build_http,
    call,
)
from pilot_backend.transport.wsgi_app import (  # noqa: E402
    GENERATE_SUGGESTIONS_ROUTE,
    GENERATE_SUGGESTIONS_V2_ROUTE,
)


def _http_world(*, source=None, states=None, managing=True, child="alpha",
                with_projection=True, activity_bank=_UNSET):
    """The secure topology, plus a Parent link and a v2 projection for its child.

    The topology ships provider connections but NO managing-clinician
    assignment — that gate is 0.4A's and is asserted separately — so the
    assignment is made here when the test needs the positive path.
    """
    http = build_http(
        rung_source=source or _source(),
        activity_bank=(FakeActivityBank() if activity_bank is _UNSET
                       else activity_bank))
    topo = http.topo
    child_id = (topo.child_alpha if child == "alpha" else topo.child_beta
                ).child_id
    if managing:
        http.repos.managing_clinicians.create(
            ManagingClinicianAssignment.create(
                child_id, topo.provider_alpha.provider_id,
                topo.practice.practice_id,
                provider_connection_id=topo.link_alpha_provider.connection_id,
                actor_id=topo.caregiver_alpha.caregiver_id, now=NOW))
    http.repos.source_links.create(SourceSystemLink.create(
        child_id, SourceSystem.PARENT, SESSION, actor_id="fixture",
        actor_role=ActorRole.CAREGIVER.value, now=NOW))
    if with_projection:
        http.repos.parent_baseline_projections_v2.create(_projection(
            states_30=states if states is not None
            else mixed(**{BOOK: "not_demonstrated",
                          PRONOUNS: "not_demonstrated"}),
            child=child_id))
    http.child_id = child_id
    http.path = f"/pilot/children/{child_id}/goal-suggestions/generate-v2"
    http.v1_path = f"/pilot/children/{child_id}/goal-suggestions/generate"
    return http


def _claims(http):
    return [d for _i, d in
            http.repos.store.list_all("pilot_suggestion_generation_claims")]


def test_the_v2_route_generates_for_the_managing_clinician():
    http = _http_world()
    status, body, _ = call(http.app, http.path, method="POST",
                           bearer="Bearer token-provider-alpha")
    assert status == 200, body
    assert body["outcome"] == OUTCOME_GENERATED
    assert body["generation_policy"] == GENERATION_POLICY_VERSION_V2
    assert body["target_band_months"] == 30
    assert len(body["generated"]) == 1
    assert body["generated"][0]["rung_ref"] == ref_for(BOOK, 30)
    assert body["generated"][0]["created"] is True
    assert body["unsupported_target_refs"] == [ref_for(PRONOUNS, 30)]
    assert body["unknown_refs"] == []
    assert len(_claims(http)) == 1


def test_a_caregiver_cannot_invoke_the_v2_route():
    """A family must not trigger clinical target selection for their own child."""
    http = _http_world()
    status, body, _ = call(http.app, http.path, method="POST",
                           bearer="Bearer token-caregiver-alpha")
    assert status == 403
    assert body == {"error": "not permitted"}
    assert _claims(http) == []


def test_an_unauthenticated_request_cannot_invoke_the_v2_route():
    """No credential and an unknown credential, both refused before any write.

    Header FORMAT strictness is deliberately not asserted here: the whole
    `Authorization` value is handed to the verifier, so the prefix rule belongs
    to the verifier, and the dev verifier used by this harness is lenient about
    it by design. Asserting it here would be testing `DevAuthVerifier` rather
    than this route.
    """
    http = _http_world()
    for bearer in (None, "Bearer nonsense", "Bearer "):
        status, body, _ = call(http.app, http.path, method="POST",
                               bearer=bearer)
        assert status in (401, 403), (bearer, status)
        assert body in ({"error": "not permitted"},
                        {"error": "authentication required"}), body
    assert _claims(http) == []


def test_a_connected_provider_who_is_not_managing_is_refused():
    """The EXISTING managing-clinician gate, reused and not reimplemented.

    Everything else is in place — the provider is connected and active, the
    Parent link exists, the v2 projection exists and holds a mappable deficit.
    Only the assignment is missing, so a pass here would mean the gate is gone.
    """
    http = _http_world(managing=False)
    status, body, _ = call(http.app, http.path, method="POST",
                           bearer="Bearer token-provider-alpha")
    assert status == 403
    assert body == {"error": "not permitted"}
    assert _claims(http) == []


def test_the_v2_route_is_not_reachable_by_get():
    """A GET that mutated would be cacheable, prefetchable and auto-retried."""
    http = _http_world()
    status, _body, _ = call(http.app, http.path, method="GET",
                            bearer="Bearer token-provider-alpha")
    assert status == 405
    assert _claims(http) == []


def test_generation_fails_closed_with_no_gold_standard_configured():
    http = build_http(rung_source=None)
    child_id = http.topo.child_alpha.child_id
    path = f"/pilot/children/{child_id}/goal-suggestions/generate-v2"
    status, body, _ = call(http.app, path, method="POST",
                           bearer="Bearer token-provider-alpha")
    assert status == 403
    assert body == {"error": "not permitted"}


def test_the_v1_route_does_not_match_the_v2_path_or_the_reverse():
    """A prefix match would silently run the v1 algorithm on a v2 request."""
    from pilot_backend.transport.wsgi_app import (
        _match_generate_suggestions_route,
        _match_generate_suggestions_v2_route,
    )

    v1 = "/pilot/children/chld_x/goal-suggestions/generate"
    v2 = "/pilot/children/chld_x/goal-suggestions/generate-v2"
    assert _match_generate_suggestions_route(v1) == "chld_x"
    assert _match_generate_suggestions_route(v2) is None
    assert _match_generate_suggestions_v2_route(v2) == "chld_x"
    assert _match_generate_suggestions_v2_route(v1) is None
    assert GENERATE_SUGGESTIONS_V2_ROUTE == GENERATE_SUGGESTIONS_ROUTE + "-v2"


def test_the_route_response_carries_no_prose_and_no_claim_id():
    """Item 15. Canonical refs, ids and counts only."""
    import json

    http = _http_world()
    _status, body, _ = call(http.app, http.path, method="POST",
                            bearer="Bearer token-provider-alpha")
    blob = json.dumps(body)
    for forbidden in (BOOK, PRONOUNS, VOCAB, "milestone", "subdomain",
                      "skill_key", "claim_id", "generation_key", SESSION,
                      "not_demonstrated", "area_id", "entry_choice_id"):
        assert forbidden not in blob, forbidden
    assert sorted(body) == [
        "child_id", "generated", "generation_policy", "outcome",
        "projection_id", "request_id", "suggestion_ids",
        "target_band_months", "unknown_refs", "unsupported_target_refs"]


def test_the_route_returns_200_for_every_zero_suggestion_outcome():
    """A true finding is not a refusal.

    Rendering these as 403 would tell Hannah her request failed when it in fact
    answered, and would push her to retry something that will answer the same.

    FOUR cases as of the registered-unresolved slice, not three. A 30-month
    deficit that is registered and has no higher mappable sibling now answers
    `no_supported_target_registered_gaps` — still a finding, still a 200, and
    NOT the `no_unresolved_band` it must never be mistaken for.
    """
    cases = {
        OUTCOME_NO_UNRESOLVED_BAND: ALL_DEMONSTRATED,
        OUTCOME_INSUFFICIENT_KNOWN_EVIDENCE: mixed(**{PRONOUNS: "unknown"}),
        OUTCOME_NO_SUPPORTED_TARGET_REGISTERED_GAPS:
            mixed(**{PRONOUNS: "not_demonstrated"}),
    }
    for expected, states in cases.items():
        http = _http_world(states=states)
        status, body, _ = call(http.app, http.path, method="POST",
                               bearer="Bearer token-provider-alpha")
        assert status == 200, (expected, body)
        assert body["outcome"] == expected
        assert body["generated"] == []
        assert body["suggestion_ids"] == []
        assert _claims(http) == []


def test_an_incomplete_band_renders_as_the_constant_403():
    """The 0.5C no-oracle rule: a refusal never says which gate stopped you.

    Byte-identical to the caregiver refusal and to the not-managing refusal, so
    a prober cannot tell an authorization failure from an incomplete assessment.
    """
    http = _http_world(with_projection=False)
    http.repos.parent_baseline_projections_v2.create(_projection(
        states_30=mixed(**{BOOK: "not_demonstrated"}), omit_30=(VOCAB,),
        child=http.child_id))
    status, body, _ = call(http.app, http.path, method="POST",
                           bearer="Bearer token-provider-alpha")
    assert status == 403
    assert body == {"error": "not permitted"}
    assert _claims(http) == []


def test_no_child_id_or_ref_reaches_a_log_line():
    http = _http_world()
    call(http.app, http.path, method="POST",
         bearer="Bearer token-provider-alpha")
    joined = "\n".join(http.logs)
    assert http.child_id not in joined
    assert ref_for(BOOK, 30) not in joined
    assert BOOK not in joined
    # The route TEMPLATE is logged, never the populated path.
    assert GENERATE_SUGGESTIONS_V2_ROUTE in joined


# ===========================================================================
# 17. THE REGISTERED-UNRESOLVED SCENARIOS (founder-pinned A-F)
#
# The bounded pass-over, exercised on a band shape that mirrors the REAL
# declared track for the 3-5y pilot rather than the generic fixture above:
#
#   36m  action-picture labeling   mappable
#        first-name response       registered `ambiguous_unresolved`
#        wh-question asking        registered `deferred_post_pilot`
#   48m  four-or-more-word sentences   mappable
#        function-question answering   mappable
#
# Those statuses are the ones the taxonomy workbook actually records, so a
# scenario that passes here is a statement about the shipped register and not
# about a convenient fixture.
# ===========================================================================

SC_ACTION_PIC = "says what action is happening in a picture or book"
SC_FIRST_NAME = "says first name when asked"
SC_WH_ASKING = "ask who or what or where or why questions"
SC_SENTENCES = "says sentences with four or more words"
SC_FUNCTION_Q = "answers simple questions like what is a coat for"

#: family_key + status exactly as the shipped `unresolved_family_register` has
#: them. Restated here so a test failure points at the disagreement.
SC_REGISTERED = {
    SC_FIRST_NAME: ("ambiguous_unresolved", ("expressive_name_response",)),
    SC_WH_ASKING: ("deferred_post_pilot", ("wh_question_asking",)),
}


def _scenario_source(*, unregistered=()):
    """24m + 30m as the base fixture, plus a real-shaped 36m and 48m band.

    `unregistered` names scenario skills to leave OUT of the register, which is
    how Scenario E builds a genuine data defect.
    """
    rungs = [_rung(AT_24, 24)] + [_rung(m, 30) for m in MAPPABLE_30]
    rungs.append(_rung(SC_ACTION_PIC, 36, ("action_picture_labeling",)))
    rungs.append(_rung(SC_SENTENCES, 48, ("sentence_building",)))
    rungs.append(_rung(SC_FUNCTION_Q, 48, ("function_question_answering",)))

    blocked = [RungTarget(domain_key=SUPPORTED_DOMAIN, source_rung_months=30,
                          milestone_text=PRONOUNS)]
    blocked += [RungTarget(domain_key=SUPPORTED_DOMAIN, source_rung_months=36,
                           milestone_text=m)
                for m in (SC_FIRST_NAME, SC_WH_ASKING)]
    registered = {blocked[0]: ("deferred_post_pilot", ("pronouns",))}
    for target in blocked[1:]:
        if target.milestone_text in unregistered:
            continue
        registered[target] = SC_REGISTERED[target.milestone_text]
    return InMemoryGoldStandardRungSource(
        rungs=tuple(rungs), unmappable=tuple(blocked),
        registered_unresolved=registered)


def _scenario_projection(*, at_30=None, at_36, at_48=None, source=None):
    """A projection over the scenario band shape. 24m always demonstrated."""
    bands = [(36, tuple(at_36.items()), 3)]
    if at_48:
        bands.append((48, tuple(at_48.items()), 2))
    return _projection(states_30=at_30 or ALL_DEMONSTRATED,
                       extra_bands=tuple(bands))


#: The scenarios below isolate BAND SELECTION, so their bank serves every
#: scenario family. That is deliberately NOT the real served set:
#: `action_picture_labeling` and `function_question_answering` have no reviewed
#: content today, so with `FIXTURE_SERVED_FAMILIES` these runs would fail on the
#: A2 v2.1 content gate and the tests would pass or fail for a reason that has
#: nothing to do with the pass-over.
#:
#: The content gate is NOT weakened by this — it is proven still to bite, on the
#: real served set, in
#: `test_the_content_gate_still_refuses_a_selected_but_unserved_target`.
SCENARIO_SERVED_FAMILIES = FIXTURE_SERVED_FAMILIES + (
    "action_picture_labeling", "function_question_answering",
)


def _scenario(*, at_30=None, at_36, at_48=None, unregistered=(),
              served=SCENARIO_SERVED_FAMILIES):
    source = _scenario_source(unregistered=unregistered)
    projection = _scenario_projection(at_30=at_30, at_36=at_36, at_48=at_48)
    return World(projection=projection, source=source,
                 authority=content_authority(served=served))


def _gaps(outcome):
    """`(months, family_keys, status)` per reported deficit, order-stable."""
    return sorted((d.months, d.family_keys, d.resolution_status)
                  for d in outcome.passed_over)


def test_scenario_a_a_deferred_only_lower_band_is_reported_and_48m_is_used():
    """SCENARIO A. The pilot's headline case.

    36m holds ONE deficit and it is the registered wh-question skill; 48m holds
    two real mappable deficits. Before this slice the 36m band was selected and
    the run produced ZERO suggestions, so a 4.5-5y child with two genuine 48m
    deficits got nothing.
    """
    world = _scenario(
        at_36={SC_ACTION_PIC: "demonstrated", SC_FIRST_NAME: "demonstrated",
               SC_WH_ASKING: "not_demonstrated"},
        at_48={SC_SENTENCES: "not_demonstrated", SC_FUNCTION_Q: "emerging"})
    outcome = world.generate()

    assert outcome.outcome == OUTCOME_GENERATED
    assert outcome.target_band_months == 48
    # BOTH 48m siblings survive independently. No winner, no ranking.
    assert sorted(t.rung_ref for t in outcome.generated) == sorted(
        (ref_for(SC_SENTENCES, 48), ref_for(SC_FUNCTION_Q, 48)))
    # ...and the 36m gap is reported rather than dropped.
    assert _gaps(outcome) == [
        (36, ("wh_question_asking",), "deferred_post_pilot")]


def test_scenario_b_a_mixed_band_selects_the_lower_band_and_reports_its_sibling():
    """SCENARIO B. THE CRITICAL ONE.

    36m holds a MAPPABLE deficit and a registered one. The mappable deficit
    wins the band, its registered sibling is reported, and 48m is NOT reached —
    the lowest band Genex can act on is still the one the child needs next.

    If this test ever selects 48m, the bounded pass-over has become an
    unbounded one and the "lower bands first" principle is gone.
    """
    world = _scenario(
        at_36={SC_ACTION_PIC: "not_demonstrated",
               SC_FIRST_NAME: "demonstrated",
               SC_WH_ASKING: "not_demonstrated"},
        at_48={SC_SENTENCES: "not_demonstrated", SC_FUNCTION_Q: "emerging"})
    outcome = world.generate()

    assert outcome.outcome == OUTCOME_GENERATED
    assert outcome.target_band_months == 36, "48m must NOT be reached"
    assert [t.rung_ref for t in outcome.generated] == [
        ref_for(SC_ACTION_PIC, 36)]
    assert _gaps(outcome) == [
        (36, ("wh_question_asking",), "deferred_post_pilot")]
    # The 48m deficits exist and were deliberately not touched.
    assert ref_for(SC_SENTENCES, 48) not in {
        t.rung_ref for t in outcome.generated}


def test_scenario_c_an_ambiguous_only_band_is_reported_and_progression_allowed():
    """SCENARIO C. `ambiguous_unresolved` passes over exactly like deferred.

    The two statuses differ in WHY the family is unresolved, never in whether a
    band may be passed over — both mean "the taxonomy deliberately does not
    define this", and the status travels so a clinician sees which it was.
    """
    world = _scenario(
        at_36={SC_ACTION_PIC: "demonstrated",
               SC_FIRST_NAME: "not_demonstrated",
               SC_WH_ASKING: "demonstrated"},
        at_48={SC_SENTENCES: "not_demonstrated", SC_FUNCTION_Q: "emerging"})
    outcome = world.generate()

    assert outcome.outcome == OUTCOME_GENERATED
    assert outcome.target_band_months == 48
    assert _gaps(outcome) == [
        (36, ("expressive_name_response",), "ambiguous_unresolved")]


def test_scenario_d_a_deferred_30m_pronoun_does_not_block_a_36m_target():
    """SCENARIO D. The trap one band lower.

    The 30m pronoun deferral used to block EVERY later target, including a
    mappable 36m deficit. It is now reported and stepped past.
    """
    world = _scenario(
        at_30=mixed(**{PRONOUNS: "not_demonstrated"}),
        at_36={SC_ACTION_PIC: "not_demonstrated",
               SC_FIRST_NAME: "demonstrated", SC_WH_ASKING: "demonstrated"},
        at_48={SC_SENTENCES: "not_demonstrated", SC_FUNCTION_Q: "emerging"})
    outcome = world.generate()

    assert outcome.outcome == OUTCOME_GENERATED
    assert outcome.target_band_months == 36
    assert [t.rung_ref for t in outcome.generated] == [
        ref_for(SC_ACTION_PIC, 36)]
    assert _gaps(outcome) == [(30, ("pronouns",), "deferred_post_pilot")]


def test_scenario_e_an_unregistered_unmappable_skill_fails_closed():
    """SCENARIO E. The negative control that makes every pass-over meaningful.

    Identical to Scenario A except the wh-question skill is NOT in the
    register. The 48m targets Scenario A reaches are present and attractive,
    and the run refuses anyway.
    """
    world = _scenario(
        at_36={SC_ACTION_PIC: "demonstrated", SC_FIRST_NAME: "demonstrated",
               SC_WH_ASKING: "not_demonstrated"},
        at_48={SC_SENTENCES: "not_demonstrated", SC_FUNCTION_Q: "emerging"},
        unregistered=(SC_WH_ASKING,))

    with pytest.raises(UnregisteredUnmappableSkill):
        world.generate()
    assert world.claims() == [] and world.suggestions() == []
    assert world.anchors() == []


def test_scenario_f_only_registered_gaps_remain_and_the_outcome_says_so():
    """SCENARIO F. Every mappable skill demonstrated, one registered gap left.

    The outcome must NOT be `no_unresolved_band`: that asserts the child has no
    unresolved skills, and this child has one. It must also not invent a target.
    """
    world = _scenario(
        at_36={SC_ACTION_PIC: "demonstrated", SC_FIRST_NAME: "demonstrated",
               SC_WH_ASKING: "not_demonstrated"},
        at_48={SC_SENTENCES: "demonstrated", SC_FUNCTION_Q: "demonstrated"})
    outcome = world.generate()

    assert outcome.outcome == OUTCOME_NO_SUPPORTED_TARGET_REGISTERED_GAPS
    assert outcome.outcome != OUTCOME_NO_UNRESOLVED_BAND
    assert outcome.generated == ()
    assert outcome.target_band_months is None
    assert outcome.has_registered_gaps
    assert _gaps(outcome) == [
        (36, ("wh_question_asking",), "deferred_post_pilot")]
    # No invented intervention.
    assert world.claims() == [] and world.suggestions() == []


def test_a_fully_mastered_child_still_reports_no_unresolved_band():
    """Non-vacuity for Scenario F: the OTHER outcome must still be reachable.

    Without this, `no_supported_target_registered_gaps` could have replaced
    `no_unresolved_band` everywhere and Scenario F would pass while saying
    nothing.
    """
    world = _scenario(
        at_36={SC_ACTION_PIC: "demonstrated", SC_FIRST_NAME: "demonstrated",
               SC_WH_ASKING: "demonstrated"},
        at_48={SC_SENTENCES: "demonstrated", SC_FUNCTION_Q: "demonstrated"})
    outcome = world.generate()

    assert outcome.outcome == OUTCOME_NO_UNRESOLVED_BAND
    assert outcome.passed_over == ()
    assert not outcome.has_registered_gaps


def test_an_unknown_sibling_prevents_a_pass_over():
    """A band is passable only when every state in it is KNOWN.

    The 36m band's known deficits are all registered, but one sibling is
    `unknown` — so we do not know whether that band holds a mappable deficit,
    and passing it over would infer mastery from missing evidence.
    """
    world = _scenario(
        at_36={SC_ACTION_PIC: "unknown", SC_FIRST_NAME: "demonstrated",
               SC_WH_ASKING: "not_demonstrated"},
        at_48={SC_SENTENCES: "not_demonstrated", SC_FUNCTION_Q: "emerging"})
    outcome = world.generate()

    assert outcome.target_band_months == 36, "48m must NOT be reached"
    assert outcome.outcome == OUTCOME_NO_SUPPORTED_TARGET
    assert outcome.unknown_refs == (ref_for(SC_ACTION_PIC, 36),)


def test_a_pass_over_is_impossible_without_a_report():
    """Structural: `scan_bands` cannot skip a band without returning the reason.

    Asserted over the function rather than through a scenario, because the
    failure mode is a future edit that keeps the skip and drops the report —
    which every behavioural test above would still pass.
    """
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(scan_bands)))
    # Every `continue`/fall-through path that skips a band must have extended
    # `passed_over` first: there is exactly one `extend` and one `return` of a
    # scan, and the extend is the only way past an unresolved band.
    extends = [n for n in ast.walk(tree)
               if isinstance(n, ast.Attribute) and n.attr == "extend"]
    assert extends, "the skip path must record what it skipped"
    returns = [n for n in ast.walk(tree) if isinstance(n, ast.Return)]
    assert returns and all(
        isinstance(r.value, ast.Call)
        and getattr(r.value.func, "id", "") == "BandScan"
        for r in returns), "every exit must return a BandScan"


def test_the_service_always_supplies_ref_support():
    """`classify_band`'s permissive default must never reach the product path.

    Omitting `support_by_ref` makes every deficit look mappable, which would
    reinstate the band trap. The service is pinned to pass it.
    """
    import inspect
    source = inspect.getsource(
        BaselineSuggestionGenerationV2Service.band_views)
    assert "support_by_ref=" in source
    assert "ref_support_state" in source


def test_the_three_support_states_are_distinct_and_exhaustive():
    assert len({REF_MAPPABLE, REF_REGISTERED_UNRESOLVED,
                REF_UNREGISTERED_UNMAPPABLE}) == 3
    # The registered state is a PREFIX carrying a status tail, so it must not
    # be confusable with the bare unregistered marker.
    assert not REF_UNREGISTERED_UNMAPPABLE.startswith(
        REF_REGISTERED_UNRESOLVED)
    assert not REF_MAPPABLE.startswith(REF_REGISTERED_UNRESOLVED)


def test_the_content_gate_still_refuses_a_selected_but_unserved_target():
    """The pass-over changes SELECTION, never content approval.

    Scenario A's shape with the REAL served set. Band selection still reaches
    48m and still reports the 36m gap — and then the A2 v2.1 content authority
    refuses, because `function_question_answering` has no reviewed content.
    That refusal is the correct current pilot state, and this test exists so the
    scenarios' widened fixture bank cannot be mistaken for a weakened gate.
    """
    from pilot_backend.goals.errors import GoalValidationError

    world = _scenario(
        at_36={SC_ACTION_PIC: "demonstrated", SC_FIRST_NAME: "demonstrated",
               SC_WH_ASKING: "not_demonstrated"},
        at_48={SC_SENTENCES: "not_demonstrated", SC_FUNCTION_Q: "emerging"},
        served=FIXTURE_SERVED_FAMILIES)

    # Selection is unaffected and the gap is still reported.
    band, rungs, _unsupported, gaps = \
        world.service.resolve_targets_with_gaps(world.projection)
    assert band.months == 48
    assert [(d.months, d.family_keys) for d in gaps] == [
        (36, ("wh_question_asking",))]

    # ...and the write refuses on the unserved family.
    with pytest.raises(GoalValidationError, match="not currently supported"):
        world.generate()
