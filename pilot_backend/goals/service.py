"""pilot_backend/goals/service.py — the only writer of suggestions and goals.

Every operation starts with the unchanged 0.2 child-access gate and ends with
an audit event, exactly as `identity/service.py` does. Nothing here bypasses
either, and no method takes a role, uid or actor id as a parameter — the
principal is the only source of identity, asserted structurally by the same
test that guards 0.4A.

## Genex suggests. A human approves. Those are different verbs.

`generate_suggestions` writes `GoalSuggestion` rows and NOTHING else. No goal
comes into existence without a separate, recorded human action naming the
suggestion it came from — or declaring that it came from none
(`AUTHORED_FRESH`). There is no code path from a suggestion to an approved
goal that does not pass through `approve_clinical_goal` or
`approve_caregiver_goal`.

## Who may approve what

    ClinicalGoal           the ACTIVE managing clinician for that child, only
    CaregiverApprovedGoal  a caregiver authorized for that child, only

A provider who is connected to the child but is NOT the managing clinician is
refused. Clinical ownership is a single, recorded assignment (0.4A) precisely
so "who is responsible for this child's treatment goals?" has one answer, and
letting any connected provider author a clinical goal would give it several.

A caregiver cannot author, revise, retire or even reach a `ClinicalGoal`, and
a clinician cannot silently adopt a caregiver's goal as clinical: they must
author one, with its own provenance. `require_clinical_goal_ref` is the single
structural gate, and `_authorize_for_ref` is the single behavioural one.

## Consuming a suggestion is a one-time transition

A suggestion leaves OFFERED exactly once. Approving from an already-consumed
suggestion is refused rather than allowed to mint a second goal, so the
suggestion-to-goal relationship stays one-to-at-most-one and a duplicated
request is a conflict, not two goals that look like a clinician changed their
mind.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import List, Optional, Tuple, Union

from ..audit.events import AuditAction, AuditResult
from ..authz.decisions import AccessDecision
from ..authz.policy import authorize_child_access
from ..domain.goal_anchor import ClinicalGoalAnchor, SuggestionCanonicalAnchor
from ..domain.goals import (
    CaregiverApprovedGoal,
    ClinicalGoal,
    EditType,
    GoalKind,
    GoalRef,
    GoalStatus,
    GoalSuggestion,
    GoalVersion,
    SuggestionStatus,
    require_clinical_goal_ref,
)
from ..domain.planning_policy import CURRENT_PLANNING_POLICY, PlanningPolicyVersion
from ..domain.suggestion_generation import (
    GoalSuggestionGenerationClaim,
    projection_cycle_month,
)
from ..domain.roles import ActorRole
from ..persistence.document_store import DocumentStoreError
from ..repository.interface import DuplicateRecord, RecordNotFound
from .errors import GoalAuthorizationError, GoalConflict, GoalValidationError


def _default_repos_factory(store):
    """Repositories over a transaction-bound store. 0.5C.

    Imported lazily, inside the function, for the reason the whole package is
    arranged this way: `pilot_backend` must import no storage SDK, and a
    module-level import of the Firestore repository set would put one on the
    import graph of every module that imports this service. A CI gate asserts
    that over the AST.
    """
    from ..persistence.firestore_repos import FirestoreRepositories

    return FirestoreRepositories(store)
from .suggestion_engine import (
    GENERATOR_VERSION,
    SUGGESTION_RULE_VERSION,
    ObservationSnapshot,
    generate_suggestions as build_suggestions,
)

RESOURCE_SUGGESTION = "goal_suggestion"
RESOURCE_CLINICAL_GOAL = "clinical_goal"
RESOURCE_CAREGIVER_GOAL = "caregiver_goal"

#: How an approval edit marks the suggestion it consumed. AUTHORED_FRESH is
#: absent on purpose: authoring fresh consumes no suggestion.
_EDIT_TO_SUGGESTION_STATUS = {
    EditType.ACCEPTED_VERBATIM: SuggestionStatus.ACCEPTED,
    EditType.MODIFIED: SuggestionStatus.MODIFIED,
    EditType.REPLACED: SuggestionStatus.REPLACED,
}

ApprovedGoal = Union[ClinicalGoal, CaregiverApprovedGoal]


class GoalService:
    """Authorized reads and writes for suggestions, goals and goal versions."""

    def __init__(self, *, repos, recorder=None, now=None,
                 repos_factory=None) -> None:
        self._repos = repos
        self._recorder = recorder
        #: Injectable clock for deterministic tests. No request parameter
        #: reaches it.
        self._now = now
        #: Builds repositories over a TRANSACTION-BOUND store. Load-bearing in
        #: production — `_commit_goal_with_version` cannot be atomic without
        #: it — and defaulted so every existing caller is unaffected. 0.5C.
        self._repos_factory = repos_factory or _default_repos_factory

    def _stamp(self) -> datetime:
        return self._now() if self._now else datetime.now(timezone.utc)

    # -- shared gates -------------------------------------------------------

    def _authorize(self, principal, child_id: str) -> AccessDecision:
        decision = authorize_child_access(principal, child_id, self._repos)
        if not decision.allowed:
            raise GoalAuthorizationError(
                f"not permitted for this child ({decision.denial.value})")
        return decision

    def _require_managing_clinician(self, principal, child_id: str):
        """The caller must BE this child's active managing clinician.

        Not merely a provider, and not merely connected. Returns the
        assignment so the goal can record which one authorised it.
        """
        if principal.role is not ActorRole.PROVIDER:
            raise GoalAuthorizationError(
                "a clinical goal requires the managing clinician")
        active = self._repos.managing_clinicians.list_for_child(child_id)
        if not active:
            raise GoalConflict("this child has no active managing clinician")
        if len(active) > 1:
            # The 0.4A claim makes this impossible; refuse rather than pick.
            raise GoalConflict("more than one active managing clinician")
        assignment = active[0]
        if assignment.provider_id != principal.application_id:
            raise GoalAuthorizationError(
                "only this child's managing clinician may author a clinical goal")
        return assignment

    def _require_caregiver(self, principal, what: str) -> None:
        if principal.role is not ActorRole.CAREGIVER:
            raise GoalAuthorizationError(f"{what} requires a caregiver")

    def _audit(self, action: AuditAction, result: AuditResult, resource_type: str,
               *, principal, child_id: str, resource_id: Optional[str],
               request_id: str, **metadata) -> None:
        if self._recorder is None:
            return
        self._recorder.record_action(
            action, result, resource_type,
            resource_id=resource_id, child_id=child_id, principal=principal,
            request_id=request_id, metadata=metadata,
        )

    # -- goal lookup --------------------------------------------------------

    def _load_goal(self, ref: GoalRef) -> ApprovedGoal:
        repo = (self._repos.clinical_goals if ref.kind is GoalKind.CLINICAL
                else self._repos.caregiver_goals)
        try:
            return repo.get_by_id(ref.goal_id)
        except RecordNotFound:
            raise GoalConflict("no such goal") from None

    def _save_goal(self, goal: ApprovedGoal) -> ApprovedGoal:
        repo = (self._repos.clinical_goals if isinstance(goal, ClinicalGoal)
                else self._repos.caregiver_goals)
        return repo.update(goal)

    def _authorize_for_ref(self, principal, ref: GoalRef) -> ApprovedGoal:
        """Load a goal and enforce who may WRITE to it.

        The single behavioural gate that keeps the two goal types apart at
        runtime, next to `require_clinical_goal_ref`'s structural one. A
        caregiver reaching a clinical goal fails here, whatever endpoint
        called in.
        """
        goal = self._load_goal(ref)
        self._authorize(principal, goal.child_id)
        if ref.kind is GoalKind.CLINICAL:
            self._require_managing_clinician(principal, goal.child_id)
        else:
            self._require_caregiver(principal, "editing a caregiver-approved goal")
            if goal.approved_by_caregiver_id != principal.application_id:
                raise GoalAuthorizationError(
                    "only the approving caregiver may edit this goal")
        return goal

    # =====================================================================
    # Suggestions
    # =====================================================================

    def generate_suggestions(self, principal, child_id: str,
                             snapshot: ObservationSnapshot, *,
                             policy: PlanningPolicyVersion = CURRENT_PLANNING_POLICY,
                             count: Optional[int] = None,
                             request_id: str = "") -> Tuple[GoalSuggestion, ...]:
        """Produce and persist candidates. Creates NO goal.

        The snapshot's `child_id` must match the authorized one. They are
        separate parameters so the gate runs on the id the caller claims to be
        acting for, and a mismatch is a refusal rather than a quiet write
        against whichever id happened to be in the payload.
        """
        self._authorize(principal, child_id)
        if snapshot.child_id != child_id:
            raise GoalValidationError(
                "observation snapshot is for a different child")

        suggestions = build_suggestions(
            snapshot, policy=policy, count=count,
            actor_id=principal.application_id, now=self._stamp())

        # 0.5E-A: this method IS the canonical boundary. It is the only place a
        # `SuggestionCanonicalAnchor` is ever written, which is what makes the
        # approval path's "copy, never derive" rule meaningful.
        #
        # Matched by DOMAIN KEY rather than by list position. The engine emits
        # at most one suggestion per domain and stamps rank from its own sort
        # order, so zipping the two lists would couple this method to that
        # ordering — a coupling that would break silently if the engine ever
        # emitted two suggestions for one domain.
        observed_by_domain = {observed.domain_key: observed
                              for observed in snapshot.domains}
        for suggestion in suggestions:
            self._repos.goal_suggestions.create(suggestion)
            observed = observed_by_domain.get(suggestion.evidence.domain_key)
            rung = getattr(observed, "canonical_rung", None)
            if rung is None:
                # No canonical provenance for this domain. The suggestion is
                # still valid and approvable; the goal it produces will simply
                # be unmappable. This is the fail-closed default, and it is
                # the state EVERY pre-0.5E-A suggestion is already in.
                continue
            if rung.domain_key != suggestion.evidence.domain_key:
                # A rung for a different domain than the suggestion it would
                # anchor. Refused rather than stored: a mismatched anchor is
                # worse than no anchor, because it looks authoritative.
                raise GoalValidationError(
                    "canonical rung does not match its suggestion's domain")
            self._repos.suggestion_anchors.create(
                SuggestionCanonicalAnchor(
                    suggestion_id=suggestion.suggestion_id,
                    child_id=child_id,
                    rung=rung,
                    created_at=self._stamp()))

        self._audit(AuditAction.GOAL_SUGGESTIONS_GENERATED, AuditResult.SUCCESS,
                    RESOURCE_SUGGESTION, principal=principal, child_id=child_id,
                    resource_id=None, request_id=request_id,
                    cycle_month=snapshot.cycle_month,
                    suggestion_count=len(suggestions),
                    generator_version=GENERATOR_VERSION,
                    rule_version=SUGGESTION_RULE_VERSION,
                    policy_version=policy.policy_version)
        return suggestions

    def generate_anchored_suggestion(self, principal, child_id: str, *,
                                     projection, canonical_rung,
                                     observed,
                                     policy: PlanningPolicyVersion = CURRENT_PLANNING_POLICY,
                                     request_id: str = "") -> Tuple[Tuple[GoalSuggestion, ...], bool]:
        """0.5F-B. ONE deterministic generation, claim-first and atomic.

        Returns `(suggestions, created)`. `created` is False when another writer
        already generated this exact (projection, policy, taxonomy, Gold
        Standard, domain, target) tuple — the caller then gets THAT result, not
        a second equivalent one.

        ## Why this is a new method and not a flag on `generate_suggestions`

        `generate_suggestions` is the 0.4B/0.5E-A boundary and is correct for
        what it does: it creates unconditionally, from a snapshot the caller
        assembled, with no idempotency of its own. 0.5F-B needs the opposite
        default — at most once per immutable projection, under concurrency — so
        bolting a mode onto that method would give one function two contradictory
        contracts. The frozen one is left exactly as it is, and every existing
        caller with it.

        ## Claims FIRST, and all three writes together

        The generation claim, the suggestion and its canonical anchor commit in
        ONE transaction, claim first. That ordering is the same one
        `link_parent_session` had to adopt after the emulator produced eight
        orphan children: acquiring the mutex last means the earlier writes are
        already committed when the loser discovers it lost.

        Here the equivalent failure would be eight suggestions for one baseline,
        or a suggestion with no anchor — which `allocate_goal` would later refuse,
        surfacing as a mysterious dead goal rather than as a refused generation.

        ## The suggestion is still Genex-authored

        `actor_id` on the suggestion records WHO TRIGGERED generation, because
        the audit trail needs it. It does NOT make the provider the author of the
        developmental target: the target came from the frozen algorithm, the
        suggestion's status stays OFFERED, and no ClinicalGoal is created here.
        Approval remains the only path to a goal.
        """
        self._authorize(principal, child_id)
        if getattr(projection, "child_id", None) != child_id:
            # A projection for a different child than the authorized one.
            # Refused rather than trusted: the caller's authorization was
            # checked against `child_id`, so generating from a projection
            # naming someone else would write across that boundary.
            raise GoalValidationError(
                "the baseline projection is for a different child")
        if canonical_rung.domain_key != observed.domain_key:
            raise GoalValidationError(
                "canonical rung does not match its observed domain")
        if not canonical_rung.is_activity_mappable:
            # Belt and braces: the generation service already refused this.
            # Restated here because this method is the only place an anchor is
            # written, and an unmappable anchor is worse than none.
            raise GoalValidationError(
                "the canonical target is not activity-mappable")

        claim = GoalSuggestionGenerationClaim.build(
            projection_id=projection.projection_id,
            child_id=child_id,
            domain_key=canonical_rung.domain_key,
            target_rung_ref=canonical_rung.rung_ref,
            target_rung_months=canonical_rung.source_rung_months,
            taxonomy_version=canonical_rung.taxonomy_version,
            gold_standard_version=canonical_rung.baseline_version,
            requested_by_actor_id=principal.application_id,
            now=self._stamp())

        # --- idempotent replay, BEFORE building anything ------------------
        #
        # A repeat is the common case: the Therapist workspace triggers
        # generation every time Hannah opens the child. Resolving an existing
        # claim turns that into one read instead of a guaranteed collision.
        existing = self._repos.suggestion_generation_claims.find(claim.claim_id)
        if existing is not None:
            return self._suggestions_for_claim(existing), False

        snapshot = ObservationSnapshot(
            child_id=child_id,
            cycle_month=projection_cycle_month(projection),
            domains=(observed,))
        built = build_suggestions(
            snapshot, policy=policy, actor_id=principal.application_id,
            now=self._stamp())
        if not built:
            # The engine found nothing to offer. No claim is written, so a
            # later run with better evidence is still free to generate.
            raise GoalValidationError(
                "the generation engine produced no suggestion")

        persisted = replace(claim,
                            suggestion_ids=tuple(s.suggestion_id for s in built))

        def _commit(store):
            tx = self._repos_factory(store)
            # CLAIM FIRST, so a loser writes nothing at all.
            tx.suggestion_generation_claims.create(persisted)
            for suggestion in built:
                tx.goal_suggestions.create(suggestion)
                tx.suggestion_anchors.create(SuggestionCanonicalAnchor(
                    suggestion_id=suggestion.suggestion_id,
                    child_id=child_id,
                    rung=canonical_rung,
                    created_at=self._stamp()))
            return built

        try:
            created = self._repos.store.run_in_transaction(_commit)
        except (DuplicateRecord, DocumentStoreError):
            # Another writer won between the advisory read and here. NOTHING
            # was written by us; converge on their result rather than adding a
            # second equivalent set.
            winner = self._repos.suggestion_generation_claims.find(
                claim.claim_id)
            if winner is None:  # pragma: no cover - defensive
                raise GoalConflict(
                    "generation contention could not be resolved") from None
            return self._suggestions_for_claim(winner), False

        self._audit(AuditAction.GOAL_SUGGESTIONS_GENERATED, AuditResult.SUCCESS,
                    RESOURCE_SUGGESTION, principal=principal, child_id=child_id,
                    resource_id=persisted.claim_id, request_id=request_id,
                    # `suggestion_count` only. Audit metadata keys are
                    # allowlisted, and widening that allowlist for a version
                    # string is not worth it — the generation claim itself
                    # records the policy, and `resource_id` points at it.
                    suggestion_count=len(created))
        return tuple(created), True

    def _suggestions_for_claim(self, claim) -> Tuple[GoalSuggestion, ...]:
        """The suggestions a generation claim names, in its own order.

        Read by ID from the claim's own lineage rather than by querying the
        child's suggestions, so a replay cannot pick up a suggestion some other
        generation created.
        """
        found = []
        for suggestion_id in claim.suggestion_ids:
            try:
                found.append(self._repos.goal_suggestions.get_by_id(
                    suggestion_id))
            except RecordNotFound:  # pragma: no cover - defensive
                continue
        return tuple(found)

    def list_suggestions(self, principal, child_id: str, *,
                         cycle_month: Optional[str] = None
                         ) -> List[GoalSuggestion]:
        self._authorize(principal, child_id)
        if cycle_month is None:
            return self._repos.goal_suggestions.list_for_child(child_id)
        return self._repos.goal_suggestions.list_for_cycle(child_id, cycle_month)

    def decline_suggestion(self, principal, suggestion_id: str, *,
                           request_id: str = "") -> GoalSuggestion:
        """Record that a human rejected a candidate. The row is kept."""
        suggestion = self._load_suggestion(suggestion_id)
        self._authorize(principal, suggestion.child_id)
        if suggestion.status is not SuggestionStatus.OFFERED:
            raise GoalConflict("this suggestion has already been acted on")

        declined = suggestion.with_status(SuggestionStatus.DECLINED)
        self._repos.goal_suggestions.update(declined)
        self._audit(AuditAction.GOAL_SUGGESTION_DECLINED, AuditResult.SUCCESS,
                    RESOURCE_SUGGESTION, principal=principal,
                    child_id=suggestion.child_id, resource_id=suggestion_id,
                    request_id=request_id, suggestion_id=suggestion_id,
                    cycle_month=suggestion.cycle_month)
        return declined

    def _load_suggestion(self, suggestion_id: str) -> GoalSuggestion:
        try:
            return self._repos.goal_suggestions.get_by_id(suggestion_id)
        except RecordNotFound:
            raise GoalConflict("no such suggestion") from None

    def _consume_suggestion(self, suggestion_id: Optional[str], child_id: str,
                            edit_type: EditType) -> Optional[GoalSuggestion]:
        """Validate and stamp the suggestion an approval came from.

        Returns None for `AUTHORED_FRESH`, which consumes nothing. Refuses a
        suggestion belonging to another child, or one already acted on: a
        suggestion leaves OFFERED exactly once, so a repeated request is a
        conflict rather than a second goal.
        """
        if edit_type is EditType.AUTHORED_FRESH:
            if suggestion_id:
                raise GoalValidationError(
                    "authoring fresh must not name a suggestion")
            return None
        if not suggestion_id:
            raise GoalValidationError(
                f"{edit_type.value} requires the suggestion it came from")

        suggestion = self._load_suggestion(suggestion_id)
        if suggestion.child_id != child_id:
            raise GoalValidationError("suggestion belongs to a different child")
        if suggestion.status is not SuggestionStatus.OFFERED:
            raise GoalConflict("this suggestion has already been acted on")

        self._repos.goal_suggestions.update(
            suggestion.with_status(_EDIT_TO_SUGGESTION_STATUS[edit_type]))
        return suggestion

    @staticmethod
    def _approved_text(edit_type: EditType, text: str,
                       suggestion: Optional[GoalSuggestion]) -> str:
        """The wording to store.

        Accepting verbatim takes the suggestion's OWN template rather than
        whatever text the caller echoed back — otherwise "accepted verbatim"
        would be a claim the record cannot support.
        """
        if edit_type is EditType.ACCEPTED_VERBATIM:
            if suggestion is None:  # pragma: no cover - guarded upstream
                raise GoalValidationError(
                    "accepting verbatim requires a suggestion")
            return suggestion.family_facing_text_template
        if not (text or "").strip():
            raise GoalValidationError(f"{edit_type.value} requires goal text")
        return text.strip()

    # =====================================================================
    # Approval
    # =====================================================================

    def _commit_goal_with_version(self, goal, version, *, kind: GoalKind,
                                  anchor=None):
        """Write a goal and its FIRST version atomically. PRE-PHI blocker 3.

        Returns the goal with `current_version_id` filled in.

        One transaction containing exactly two `create` calls. Both are
        creates, so unlike a status change there is no read-after-write and no
        `set` — the whole operation is a single all-or-nothing batch with no
        boundary to recover across, which is the same shape 0.4D/E's weekly
        allocation has.

        No claim is acquired here and none is needed. Uniqueness is not the
        property at stake: a goal id is freshly minted and cannot collide, and
        a child may legitimately hold many goals. What was missing was
        ATOMICITY between two records that reference each other, and a
        transaction is exactly that.

        Immutable `GoalVersion` semantics are untouched. The version is still
        appended by `create` on its own id, so it can never be rewritten;
        putting it in a transaction changes when it becomes visible, not
        whether it can change afterwards.

        0.5E-A adds an optional THIRD create: the canonical anchor. It joins
        the SAME transaction rather than following it, because a goal that is
        briefly anchored-without-a-goal, or approved-without-its-anchor, is
        exactly the half-landed state blocker 3 was closed to prevent. Three
        creates, still all-or-nothing, still no read-after-write.
        """
        collection = ("clinical_goals" if kind is GoalKind.CLINICAL
                      else "caregiver_goals")
        persisted = replace(goal, current_version_id=version.version_id)

        def _write(store):
            tx = self._repos_factory(store)
            tx.goal_versions.append(version)
            getattr(tx, collection).create(persisted)
            if anchor is not None:
                tx.clinical_goal_anchors.create(anchor)
            return persisted

        return self._repos.store.run_in_transaction(_write)

    def _anchor_for_approval(self, goal_id: str,
                             suggestion: Optional[GoalSuggestion]):
        """The canonical anchor for a new clinical goal, or None.

        THE TRUST BOUNDARY of 0.5E-A, and the whole reason the feature is
        safe. An anchor is only ever COPIED from a `SuggestionCanonicalAnchor`
        that the generation boundary already persisted. Three consequences,
        each deliberate:

        - nothing a clinician's approval request contains can influence it.
          The request carries `suggestion_id`, `edit_type`, `text` and
          `reason`; no domain, no milestone, no months, no family, no rung
          ref. A browser cannot author provenance.
        - goal TEXT is never consulted. Not to infer a domain, not to match a
          milestone, not at all. The wording a clinician chose and the target
          the goal is anchored to are independent facts.
        - insufficient provenance FAILS CLOSED to None, which makes the goal
          unmappable. Every suggestion generated before 0.5E-A has no anchor
          record, so every goal approved from one is unmappable — the approved
          behaviour, not a migration bug.

        `authored_fresh` reaches here with `suggestion is None` and therefore
        gets no anchor, which is the explicit requirement rather than a
        side effect.
        """
        if suggestion is None:
            return None
        stored = self._repos.suggestion_anchors.find(suggestion.suggestion_id)
        if stored is None:
            return None
        return ClinicalGoalAnchor.from_suggestion_anchor(
            goal_id, stored, now=self._stamp())

    def goal_anchor(self, principal, ref: GoalRef):
        """The canonical anchor for a goal, or None. Authorized read.

        None means unmappable. Callers must treat absence as a refusal to
        generate activities, never as a reason to derive one.
        """
        goal = self._load_goal(ref)
        self._authorize(principal, goal.child_id)
        if ref.kind is not GoalKind.CLINICAL:
            return None
        return self._repos.clinical_goal_anchors.find(ref.goal_id)

    def is_activity_mappable(self, principal, ref: GoalRef) -> bool:
        """Whether a goal may drive weekly activity generation.

        Derived from the stored anchor on every call. There is no cached or
        separately stored boolean anywhere in this slice, so there is nothing
        that could disagree with the anchor it describes.
        """
        anchor = self.goal_anchor(principal, ref)
        return bool(anchor and anchor.is_activity_mappable)

    def approve_clinical_goal(self, principal, child_id: str, *,
                              edit_type: EditType,
                              suggestion_id: Optional[str] = None,
                              text: str = "", reason: str = "",
                              request_id: str = "") -> ClinicalGoal:
        """Create a clinician-approved, RTM-eligible goal at version 1."""
        self._authorize(principal, child_id)
        assignment = self._require_managing_clinician(principal, child_id)

        suggestion = self._consume_suggestion(suggestion_id, child_id, edit_type)
        approved_text = self._approved_text(edit_type, text, suggestion)

        # PRE-PHI blocker 3, CLOSED in 0.5C: the goal and its first version
        # commit in ONE transaction.
        #
        # Both ids are minted BEFORE either write, which is what makes a single
        # transaction possible at all: the version must name the goal and the
        # goal must name the version, so neither can be written first unless
        # both identifiers already exist. 0.4B/C minted them in exactly this
        # order and then issued two separate writes, ordered version-first so a
        # crash left an INERT orphan rather than a goal pointing at nothing.
        # That was the accepted shape, and the remaining objection was that a
        # clinical record store should not accumulate unreferenced clinical
        # text even when it is unreachable.
        #
        # Now neither exists unless both do. The ordering inside the
        # transaction is therefore no longer load-bearing — it is retained as
        # documentation of the dependency.
        goal = ClinicalGoal.create(
            child_id, principal.application_id, assignment.practice_id,
            managing_assignment_id=assignment.assignment_id,
            current_version_id="", actor_id=principal.application_id,
            now=self._stamp())
        version = GoalVersion.create(
            goal.ref, 1, approved_text, edit_type,
            actor_id=principal.application_id, actor_role=principal.role,
            derived_from_suggestion_id=(suggestion.suggestion_id
                                        if suggestion else None),
            reason=reason, now=self._stamp())
        # Copied from stored canonical provenance, or None. Never derived
        # from the request, never from the goal's text.
        anchor = self._anchor_for_approval(goal.clinical_goal_id, suggestion)
        persisted = self._commit_goal_with_version(
            goal, version, kind=GoalKind.CLINICAL, anchor=anchor)

        self._audit(AuditAction.CLINICAL_GOAL_APPROVED, AuditResult.SUCCESS,
                    RESOURCE_CLINICAL_GOAL, principal=principal,
                    child_id=child_id, resource_id=persisted.clinical_goal_id,
                    request_id=request_id,
                    goal_kind=GoalKind.CLINICAL.value,
                    goal_id=persisted.clinical_goal_id,
                    goal_version_id=version.version_id,
                    edit_type=edit_type.value,
                    assignment_id=assignment.assignment_id,
                    practice_id=assignment.practice_id,
                    **({"suggestion_id": suggestion.suggestion_id}
                       if suggestion else {}))
        return persisted

    def approve_caregiver_goal(self, principal, child_id: str, *,
                               edit_type: EditType,
                               suggestion_id: Optional[str] = None,
                               text: str = "", reason: str = "",
                               request_id: str = "") -> CaregiverApprovedGoal:
        """Create a caregiver-approved goal at version 1. NEVER RTM-eligible."""
        self._authorize(principal, child_id)
        self._require_caregiver(principal, "approving a caregiver goal")

        suggestion = self._consume_suggestion(suggestion_id, child_id, edit_type)
        approved_text = self._approved_text(edit_type, text, suggestion)

        goal = CaregiverApprovedGoal.create(
            child_id, principal.application_id, current_version_id="",
            actor_id=principal.application_id, now=self._stamp())
        version = GoalVersion.create(
            goal.ref, 1, approved_text, edit_type,
            actor_id=principal.application_id, actor_role=principal.role,
            derived_from_suggestion_id=(suggestion.suggestion_id
                                        if suggestion else None),
            reason=reason, now=self._stamp())
        # Same atomicity as the clinician path — see `approve_clinical_goal`.
        # A caregiver-approved goal is not RTM-eligible, but it carries the
        # family's own words and an unreferenced orphan of those is no more
        # acceptable than an orphan of a clinician's.
        persisted = self._commit_goal_with_version(
            goal, version, kind=GoalKind.CAREGIVER_APPROVED)

        self._audit(AuditAction.CAREGIVER_GOAL_APPROVED, AuditResult.SUCCESS,
                    RESOURCE_CAREGIVER_GOAL, principal=principal,
                    child_id=child_id, resource_id=persisted.caregiver_goal_id,
                    request_id=request_id,
                    goal_kind=GoalKind.CAREGIVER_APPROVED.value,
                    goal_id=persisted.caregiver_goal_id,
                    goal_version_id=version.version_id,
                    edit_type=edit_type.value,
                    **({"suggestion_id": suggestion.suggestion_id}
                       if suggestion else {}))
        return persisted

    # =====================================================================
    # Revision and lifecycle
    # =====================================================================

    def revise_goal(self, principal, ref: GoalRef, text: str, *,
                    edit_type: EditType = EditType.MODIFIED, reason: str,
                    request_id: str = "") -> GoalVersion:
        """Append a new immutable wording and move the goal's pointer.

        `reason` is keyword-ONLY and has no default: every edit type this
        method accepts requires one, and a defaulted reason is how "modified"
        becomes a change nobody can explain.
        """
        if edit_type is EditType.ACCEPTED_VERBATIM:
            raise GoalValidationError(
                "accepting verbatim creates a goal; it does not revise one")
        goal = self._authorize_for_ref(principal, ref)
        if not goal.is_active:
            raise GoalConflict("a closed goal cannot be revised")

        try:
            previous = self._repos.goal_versions.get_by_id(goal.current_version_id)
        except RecordNotFound:
            raise GoalConflict("goal has no readable current version") from None
        version = GoalVersion.create(
            ref, previous.version_number + 1, text, edit_type,
            actor_id=principal.application_id, actor_role=principal.role,
            reason=reason, supersedes_version_id=previous.version_id,
            now=self._stamp())
        self._repos.goal_versions.append(version)
        self._save_goal(goal.with_current_version(version.version_id,
                                                  now=self._stamp()))

        self._audit(AuditAction.GOAL_VERSION_ADDED, AuditResult.SUCCESS,
                    self._resource_for(ref), principal=principal,
                    child_id=goal.child_id, resource_id=ref.goal_id,
                    request_id=request_id, goal_kind=ref.kind.value,
                    goal_id=ref.goal_id, goal_version_id=version.version_id,
                    edit_type=edit_type.value,
                    record_version=version.version_number)
        return version

    def set_goal_status(self, principal, ref: GoalRef, status: GoalStatus, *,
                        request_id: str = "") -> ApprovedGoal:
        """Pause, resume or retire. There is no delete and no DELETED status."""
        goal = self._authorize_for_ref(principal, ref)
        updated = self._save_goal(goal.with_status(status, now=self._stamp()))
        self._audit(AuditAction.GOAL_STATUS_CHANGED, AuditResult.SUCCESS,
                    self._resource_for(ref), principal=principal,
                    child_id=goal.child_id, resource_id=ref.goal_id,
                    request_id=request_id, goal_kind=ref.kind.value,
                    goal_id=ref.goal_id, goal_status=status.value)
        return updated

    @staticmethod
    def _resource_for(ref: GoalRef) -> str:
        return (RESOURCE_CLINICAL_GOAL if ref.kind is GoalKind.CLINICAL
                else RESOURCE_CAREGIVER_GOAL)

    # =====================================================================
    # Reads
    # =====================================================================

    def list_goals(self, principal, child_id: str, *,
                   include_closed: bool = False) -> Tuple[GoalRef, ...]:
        """Every approved goal for the child, as typed references.

        References rather than merged records, so a caller cannot iterate a
        mixed list and forget which kind it is holding.
        """
        self._authorize(principal, child_id)
        clinical = self._repos.clinical_goals.list_for_child(
            child_id, include_closed=include_closed)
        caregiver = self._repos.caregiver_goals.list_for_child(
            child_id, include_closed=include_closed)
        return tuple([g.ref for g in clinical] + [g.ref for g in caregiver])

    def get_goal(self, principal, ref: GoalRef) -> ApprovedGoal:
        goal = self._load_goal(ref)
        self._authorize(principal, goal.child_id)
        return goal

    def current_text(self, principal, ref: GoalRef) -> str:
        """The goal's wording right now, as a template. Never rendered here.

        `pilot_backend` holds no child name to render WITH — substitution is
        the caller's job, at presentation time.
        """
        goal = self.get_goal(principal, ref)
        return self._repos.goal_versions.get_by_id(goal.current_version_id).text

    def goal_history(self, principal, ref: GoalRef) -> List[GoalVersion]:
        goal = self._load_goal(ref)
        self._authorize(principal, goal.child_id)
        return self._repos.goal_versions.list_chain(ref.goal_id)

    def list_clinical_goals(self, principal, child_id: str, *,
                            include_closed: bool = False) -> List[ClinicalGoal]:
        """RTM-eligible goals only. The caller cannot receive anything else."""
        self._authorize(principal, child_id)
        return self._repos.clinical_goals.list_for_child(
            child_id, include_closed=include_closed)

    def require_rtm_eligible(self, principal, ref: GoalRef) -> ClinicalGoal:
        """Resolve a reference that a future RTM caller may rely on.

        Exists now, unused by this slice, so the eventual RTM code has one
        place to go rather than re-deriving the rule. It is the structural
        gate plus a real load, not a comment saying "check the kind".
        """
        require_clinical_goal_ref(ref)
        goal = self.get_goal(principal, ref)
        if not isinstance(goal, ClinicalGoal):  # pragma: no cover - defensive
            raise GoalValidationError("reference did not resolve to a clinical goal")
        return goal
