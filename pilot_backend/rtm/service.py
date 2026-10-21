"""pilot_backend/rtm/service.py — the only writer of the RTM layer.

Every operation begins with the unchanged 0.2 child-access gate and ends with
an audit event, exactly as every prior slice does. No method takes a role, uid
or actor id as a parameter — the principal is the only source of identity,
asserted structurally by the same test that has guarded every slice since
0.4A.

## One owner, throughout

Every clinician-facing write requires the caller to BE the child's ACTIVE
managing clinician. A connected but non-managing provider is refused; a
caregiver is refused. Caregivers own observation evidence (0.4D/E) and never
clinical documentation, review, action or time.

## An open episode never follows a change of managing clinician

Each operation re-checks that the episode's `managing_provider_id` is still
the active assignment holder. If the assignment has moved, every write
against that episode raises `EpisodeTransferRefused` — the clinician must
close it explicitly and open a new one. Silently continuing would move a
course of treatment between clinicians with nothing in the record saying so.

## Finalizing a month does not close an episode

`finalize_period` writes only to the period. The episode is not read for
mutation and not written. Two separate records, two separate terminal
actions, and a test asserts the episode is still OPEN afterwards.

## Derived summaries are regenerated, never edited

`generate_evidence_summary` and `generate_coding_assistance` mint a new record
each time. Confirming or rejecting coding assistance records a decision beside
the generated candidates and leaves them byte-identical.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from ..audit.events import AuditAction, AuditResult
from ..authz.decisions import AccessDecision
from ..authz.policy import authorize_child_access
from ..coding.rules import (
    CODING_RULE_SET_ID,
    CODING_RULE_VERSION,
    CodingInputs,
    ConfirmationStatus,
    evaluate,
)
from ..domain.goals import GoalRef, require_clinical_goal_ref
from ..domain.identity_claims import ClaimKind, IdentityClaim, key_digest
from ..domain.month_end import MonthEndReport, ReportSection, SectionContent
from ..domain.roles import ActorRole
from ..domain.rtm_enrollment import (
    REQUIRED_ACKNOWLEDGEMENTS,
    ConsentAttestedBy,
    ConsentAuthority,
    EnrollmentError,
    RTMConsentAttestation,
    RTMSetupEducation,
    assess_readiness,
)
from ..domain.rtm import (
    EpisodeStatus,
    RTMError,
    PeriodStatus,
    RegulatoryStatus,
    RTMEpisode,
    RTMMonitoringPeriod,
    RTMTechnology,
)
from ..domain.rtm_documentation import (
    ClinicalAction,
    ClinicalActionType,
    InteractionModality,
    ParticipantType,
    SynchronousInteraction,
    TherapistReview,
    TimeEntry,
    documented_minutes,
)
from ..domain.rtm_summary import CodingAssistanceSummary, RTMEvidenceSummary
from ..persistence.document_store import DocumentStoreError
from ..repository.interface import DuplicateRecord, RecordNotFound
from .errors import (
    EpisodeClosedForAccrual,
    EpisodeTransferRefused,
    FinalizedRecordImmutable,
    RTMAuthorizationError,
    RTMConflict,
    RTMPrerequisitesIncomplete,
    RTMValidationError,
)
from .evidence import derive_evidence_summary
from .reporting import build_sections

RESOURCE_EPISODE = "rtm_episode"
RESOURCE_CONSENT = "rtm_consent_attestation"
RESOURCE_SETUP = "rtm_setup_education"
RESOURCE_PERIOD = "rtm_monitoring_period"
RESOURCE_TECHNOLOGY = "rtm_technology"
RESOURCE_REVIEW = "therapist_review"
RESOURCE_ACTION = "clinical_action"
RESOURCE_TIME = "time_entry"
RESOURCE_INTERACTION = "synchronous_interaction"
RESOURCE_EVIDENCE = "rtm_evidence_summary"
RESOURCE_CODING = "coding_assistance_summary"
RESOURCE_REPORT = "month_end_report"


def _default_repos_factory(store):
    from ..persistence.firestore_repos import FirestoreRepositories

    return FirestoreRepositories(store)


class RTMService:
    """Authorized reads and writes for RTM evidence and reporting."""

    def __init__(self, *, repos, recorder=None, now=None,
                 repos_factory=None) -> None:
        self._repos = repos
        self._recorder = recorder
        self._repos_factory = repos_factory or _default_repos_factory
        self._now = now

    def _stamp(self) -> datetime:
        return self._now() if self._now else datetime.now(timezone.utc)

    # -- gates --------------------------------------------------------------

    def _authorize(self, principal, child_id: str) -> AccessDecision:
        decision = authorize_child_access(principal, child_id, self._repos)
        if not decision.allowed:
            raise RTMAuthorizationError(
                f"not permitted for this child ({decision.denial.value})")
        return decision

    def _require_managing_clinician(self, principal, child_id: str, what: str):
        """The caller must BE this child's active managing clinician."""
        if principal.role is not ActorRole.PROVIDER:
            raise RTMAuthorizationError(f"{what} requires a provider")
        active = self._repos.managing_clinicians.list_for_child(child_id)
        if len(active) > 1:
            raise RTMConflict("more than one active managing clinician")
        if not active:
            raise RTMConflict("this child has no active managing clinician")
        assignment = active[0]
        if assignment.provider_id != principal.application_id:
            raise RTMAuthorizationError(
                f"{what} requires this child's managing clinician")
        return assignment

    def _require_episode_owner(self, principal, episode: RTMEpisode, what: str):
        """Authorize AND refuse a silently transferred episode.

        An episode whose managing clinician has changed cannot be written to
        at all. The clinician must close it and open a new one.
        """
        self._authorize(principal, episode.child_id)
        assignment = self._require_managing_clinician(
            principal, episode.child_id, what)
        if episode.managing_provider_id != assignment.provider_id:
            raise EpisodeTransferRefused(
                "this episode belongs to a previous managing clinician; close "
                "it explicitly and open a new episode")
        return assignment

    def _audit(self, action: AuditAction, result: AuditResult,
               resource_type: str, *, principal, child_id: str,
               resource_id: Optional[str], request_id: str, **metadata) -> None:
        if self._recorder is None:
            return
        self._recorder.record_action(
            action, result, resource_type,
            resource_id=resource_id, child_id=child_id, principal=principal,
            request_id=request_id, metadata=metadata,
        )

    # -- loads --------------------------------------------------------------

    def _load_episode(self, episode_id: str) -> RTMEpisode:
        try:
            return self._repos.rtm_episodes.get_by_id(episode_id)
        except RecordNotFound:
            raise RTMConflict("no such RTM episode") from None

    def _load_period(self, period_id: str) -> RTMMonitoringPeriod:
        try:
            return self._repos.rtm_periods.get_by_id(period_id)
        except RecordNotFound:
            raise RTMConflict("no such monitoring period") from None

    def _open_period_for_write(self, principal, period_id: str, what: str, *,
                               require_open_episode: bool = True
                               ) -> Tuple[RTMMonitoringPeriod, RTMEpisode]:
        """Load a period that may still accept evidence, with its episode.

        TWO independent lifecycle gates, and both are needed.

        `period.is_finalized` closes the MONTH. `episode.is_open` closes the
        COURSE OF TREATMENT. Before slice 3A only the first was checked here —
        the episode was loaded and its status discarded — so `open_period`
        refused a closed episode a new month while every evidence writer
        happily added clinician minutes, reviews, actions and real-time
        contacts to a month that was already open. Closure stopped the calendar
        and not the accrual.

        `require_open_episode` defaults to the STRICT value so a future write
        path is gated by omission rather than by remembering. Exactly one
        caller relaxes it — `finalize_period`, which adds no evidence and must
        stay possible after closure; see the note there. A test asserts both
        the default and that the relaxation appears exactly once in this file.
        """
        period = self._load_period(period_id)
        episode = self._load_episode(period.episode_id)
        self._require_episode_owner(principal, episode, what)
        if require_open_episode and not episode.is_open:
            # Checked BEFORE the finalized-period gate on purpose. When an
            # episode is closed AND its last month is finalized, the episode
            # is the more fundamental refusal and the more useful message:
            # amending the month would not make new evidence belong to a
            # course of treatment that has ended.
            raise EpisodeClosedForAccrual(
                "this RTM episode is closed; no further RTM evidence or time "
                "may accrue to it")
        if period.is_finalized:
            raise FinalizedRecordImmutable(
                "this period is finalized; amend rather than add evidence")
        return period, episode

    # =====================================================================
    # Episode
    # =====================================================================

    def open_episode(self, principal, child_id: str,
                     goal_refs: Sequence[GoalRef], *,
                     authorized_caregiver_id: str = "",
                     clinical_indication: str = "",
                     clinical_rationale: str = "",
                     billing_provider_id: str = "",
                     expected_duration_months: Optional[int] = None,
                     order_reference: str = "",
                     request_id: str = "") -> RTMEpisode:
        """Open an episode. Clinical goals only, all for this child.

        Slice 3C: this is the ENROLLMENT step, and it is the first of four.
        Consent and setup attach to the episode afterwards, and monitoring
        cannot begin until all three exist — see `open_period`.

        ## GOAL VERSIONS ARE PINNED HERE, READ FROM THE GOAL ITSELF

        Each goal's `current_version_id` is captured as the version this
        episode was opened against. Read from the stored goal rather than
        accepted from the caller: a caller-supplied version could name wording
        the clinician never approved, and the whole point of the pin is that
        the episode records what was actually in force.

        A later revision moves the goal's `current_version_id` and leaves this
        pin untouched, which is what makes "what was this episode opened
        against?" answerable after an edit.
        """
        self._authorize(principal, child_id)
        assignment = self._require_managing_clinician(
            principal, child_id, "opening an RTM episode")

        if not goal_refs:
            raise RTMValidationError(
                "an RTM episode requires at least one clinical goal")
        pinned = []
        for ref in goal_refs:
            # A caregiver-approved goal cannot satisfy this. Different TYPES.
            require_clinical_goal_ref(ref)
            try:
                goal = self._repos.clinical_goals.get_by_id(ref.goal_id)
            except RecordNotFound:
                raise RTMValidationError("no such clinical goal") from None
            if goal.child_id != child_id:
                raise RTMValidationError(
                    "clinical goal belongs to a different child")
            if not (goal.current_version_id or "").strip():
                raise RTMValidationError(
                    "this clinical goal has no approved version to pin")
            pinned.append((ref.goal_id, goal.current_version_id))

        if authorized_caregiver_id:
            # The caregiver must actually be connected to this child. Without
            # this an episode could name a caregiver who cannot consent, and
            # the consent gate would then refuse forever with no explanation
            # of why. `list_caregivers_for_child` already excludes ended
            # connections, so the active filter is not restated here.
            connected = {c.caregiver_id for c in
                         self._repos.caregiver_child.list_caregivers_for_child(
                             child_id)}
            if authorized_caregiver_id not in connected:
                raise RTMValidationError(
                    "the authorized caregiver is not actively connected to "
                    "this child")

        for existing in self._repos.rtm_episodes.list_for_child(child_id):
            if existing.is_open:
                raise RTMConflict("this child already has an open RTM episode")

        try:
            episode = RTMEpisode.open(
                child_id, principal.application_id, assignment.practice_id,
                tuple(goal_refs), actor_id=principal.application_id,
                managing_assignment_id=assignment.assignment_id,
                authorized_caregiver_id=authorized_caregiver_id,
                clinical_indication=clinical_indication,
                clinical_rationale=clinical_rationale,
                pinned_goal_version_ids=tuple(pinned),
                billing_provider_id=billing_provider_id,
                expected_duration_months=expected_duration_months,
                order_reference=order_reference,
                now=self._stamp())
        except RTMError as exc:
            # The record's own enrollment rules, surfaced as a validation
            # failure rather than leaking a domain exception type through the
            # service boundary.
            raise RTMValidationError(str(exc)) from None
        self._repos.rtm_episodes.create(episode)

        self._audit(AuditAction.RTM_EPISODE_OPENED, AuditResult.SUCCESS,
                    RESOURCE_EPISODE, principal=principal, child_id=child_id,
                    resource_id=episode.episode_id, request_id=request_id,
                    episode_id=episode.episode_id,
                    goal_count=len(goal_refs),
                    provider_id=principal.application_id,
                    assignment_id=assignment.assignment_id,
                    practice_id=assignment.practice_id,
                    # Opaque ids and a count. The indication and rationale are
                    # clinical text and deliberately never enter the trail.
                    caregiver_id=episode.authorized_caregiver_id,
                    billing_provider_id=episode.billing_provider_id)
        return episode

    def close_episode(self, principal, episode_id: str, *, reason: str,
                      request_id: str = "") -> RTMEpisode:
        """Close an episode. Explicit clinician action, with a reason.

        `reason` is a required keyword with no default. Note that this is the
        ONLY thing that closes an episode: finalizing a month does not.
        """
        episode = self._load_episode(episode_id)
        self._authorize(principal, episode.child_id)
        # Deliberately NOT _require_episode_owner: closing is the sanctioned
        # route out of a transferred episode, so the new managing clinician
        # must be able to close one opened by their predecessor.
        self._require_managing_clinician(principal, episode.child_id,
                                         "closing an RTM episode")

        closed = episode.close(actor_id=principal.application_id,
                               reason=reason, now=self._stamp())
        self._repos.rtm_episodes.update(closed)
        self._audit(AuditAction.RTM_EPISODE_CLOSED, AuditResult.SUCCESS,
                    RESOURCE_EPISODE, principal=principal,
                    child_id=episode.child_id, resource_id=episode_id,
                    request_id=request_id, episode_id=episode_id,
                    provider_id=principal.application_id)
        return closed

    # =====================================================================
    # Enrollment evidence — slice 3C
    # =====================================================================

    def _readiness_for(self, episode: RTMEpisode):
        """Readiness for one episode, from its CURRENT enrollment records.

        A thin read plus the pure `assess_readiness`. The verdict is never
        stored: it is a function of records that can change, and a cached
        readiness flag would be a second truth to keep in step.
        """
        return assess_readiness(
            episode,
            consents=self._repos.rtm_consents.list_for_episode(
                episode.episode_id),
            setups=self._repos.rtm_setups.list_for_episode(
                episode.episode_id))

    def episode_readiness(self, principal, episode_id: str):
        """May monitoring begin? Readable before anyone tries to open a month.

        Exposed so a clinician can see WHICH prerequisite is outstanding
        without provoking a refusal to find out.
        """
        episode = self._load_episode(episode_id)
        self._authorize(principal, episode.child_id)
        self._require_managing_clinician(principal, episode.child_id,
                                         "reading RTM readiness")
        return self._readiness_for(episode)

    def record_consent(self, principal, episode_id: str, *,
                       authority: ConsentAuthority,
                       consent_document_id: str,
                       consent_document_version: str,
                       acknowledged_monitoring: bool = False,
                       acknowledged_billing_and_cost_sharing: bool = False,
                       acknowledged_withdrawal_right: bool = False,
                       acknowledged_communication_expectations: bool = False,
                       acknowledged_response_expectations: bool = False,
                       accepted_at: Optional[datetime] = None,
                       proxy_documentation_ref: str = "",
                       proxy_reason: str = "",
                       request_id: str = "") -> RTMConsentAttestation:
        """Record the caregiver's RTM consent.

        ## WHO MAY DO THIS, AND WHAT IT THEN MEANS

        The AUTHORIZED CAREGIVER on the episode may record their own consent;
        the record is then `CAREGIVER_ATTESTED`. The managing clinician may
        record it on their behalf, and the record is then
        `PROVIDER_RECORDED_PROXY` and REQUIRES a documentation reference and a
        reason — a provider assertion can never be stored as the caregiver's
        own act, because the two use different enum values and the proxy value
        cannot exist without its provenance.

        Any other caregiver is refused even if connected to the child: consent
        belongs to the person the episode names, not to anyone with access.
        """
        episode = self._load_episode(episode_id)
        self._authorize(principal, episode.child_id)

        if principal.role is ActorRole.CAREGIVER:
            if not episode.authorized_caregiver_id:
                raise RTMValidationError(
                    "this episode names no authorized caregiver")
            if principal.application_id != episode.authorized_caregiver_id:
                raise RTMAuthorizationError(
                    "only this episode's authorized caregiver may consent")
            attested_by = ConsentAttestedBy.CAREGIVER_ATTESTED
            proxy_provider = ""
        else:
            # Provider proxy. Full managing-clinician + no-transfer gate.
            self._require_episode_owner(principal, episode,
                                        "recording RTM consent by proxy")
            if not episode.authorized_caregiver_id:
                raise RTMValidationError(
                    "this episode names no authorized caregiver")
            attested_by = ConsentAttestedBy.PROVIDER_RECORDED_PROXY
            proxy_provider = principal.application_id

        if not episode.is_open:
            raise EpisodeClosedForAccrual(
                "this RTM episode is closed; enrollment evidence cannot be "
                "added to it")

        try:
            consent = RTMConsentAttestation.record(
                episode_id, episode.child_id, episode.authorized_caregiver_id,
                authority=authority,
                consent_document_id=consent_document_id,
                consent_document_version=consent_document_version,
                accepted_at=accepted_at or self._stamp(),
                attested_by=attested_by,
                acknowledged_monitoring=acknowledged_monitoring,
                acknowledged_billing_and_cost_sharing=(
                    acknowledged_billing_and_cost_sharing),
                acknowledged_withdrawal_right=acknowledged_withdrawal_right,
                acknowledged_communication_expectations=(
                    acknowledged_communication_expectations),
                acknowledged_response_expectations=(
                    acknowledged_response_expectations),
                proxy_recorded_by_provider_id=proxy_provider,
                proxy_documentation_ref=proxy_documentation_ref,
                proxy_reason=proxy_reason,
                recorded_by_actor_id=principal.application_id,
                now=self._stamp())
        except EnrollmentError as exc:
            raise RTMValidationError(str(exc)) from None
        self._repos.rtm_consents.create(consent)

        self._audit(AuditAction.RTM_CONSENT_RECORDED, AuditResult.SUCCESS,
                    RESOURCE_CONSENT, principal=principal,
                    child_id=episode.child_id,
                    resource_id=consent.consent_id, request_id=request_id,
                    episode_id=episode_id, consent_id=consent.consent_id,
                    caregiver_id=consent.caregiver_id,
                    consent_document_id=consent_document_id,
                    consent_document_version=consent_document_version,
                    consent_authority=authority.value,
                    consent_attested_by=attested_by.value,
                    acknowledgement_count=len(
                        [1 for name in REQUIRED_ACKNOWLEDGEMENTS
                         if getattr(consent, name)]))
        return consent

    def correct_consent(self, principal, consent_id: str, *, reason: str,
                        request_id: str = "", **fields
                        ) -> RTMConsentAttestation:
        """Supersede a consent record. The attested original is RETAINED.

        The predecessor keeps its own `accepted_at`, actor and document
        version — the point of a correction is that the earlier attestation
        happened, not that it can be unsaid. Readiness reads only current
        records, so a correction cannot leave a family ready on superseded
        wording.
        """
        try:
            original = self._repos.rtm_consents.get_by_id(consent_id)
        except RecordNotFound:
            raise RTMConflict("no such consent record") from None
        episode = self._load_episode(original.episode_id)
        self._authorize(principal, episode.child_id)
        if principal.role is ActorRole.CAREGIVER:
            if principal.application_id != episode.authorized_caregiver_id:
                raise RTMAuthorizationError(
                    "only this episode's authorized caregiver may correct "
                    "their consent")
        else:
            self._require_episode_owner(principal, episode,
                                        "correcting RTM consent")
        if not original.is_current:
            raise RTMConflict("this consent record has already been corrected")
        if not (reason or "").strip():
            raise RTMValidationError("a consent correction requires a reason")

        unknown = sorted(set(fields) - set(REQUIRED_ACKNOWLEDGEMENTS)
                         - {"authority", "consent_document_id",
                            "consent_document_version", "accepted_at"})
        if unknown:
            raise RTMValidationError(
                f"a consent correction cannot set: {unknown}")

        try:
            correction = RTMConsentAttestation.record(
                original.episode_id, original.child_id, original.caregiver_id,
                authority=fields.get("authority", original.authority),
                consent_document_id=fields.get(
                    "consent_document_id", original.consent_document_id),
                consent_document_version=fields.get(
                    "consent_document_version",
                    original.consent_document_version),
                accepted_at=fields.get("accepted_at", original.accepted_at),
                attested_by=original.attested_by,
                proxy_recorded_by_provider_id=(
                    original.proxy_recorded_by_provider_id),
                proxy_documentation_ref=original.proxy_documentation_ref,
                proxy_reason=original.proxy_reason,
                recorded_by_actor_id=principal.application_id,
                supersedes_consent_id=consent_id,
                correction_reason=reason, now=self._stamp(),
                **{name: fields.get(name, getattr(original, name))
                   for name in REQUIRED_ACKNOWLEDGEMENTS})
        except EnrollmentError as exc:
            raise RTMValidationError(str(exc)) from None
        self._repos.rtm_consents.create(correction)
        self._repos.rtm_consents.update(
            original.with_successor(correction.consent_id))

        self._audit(AuditAction.RTM_CONSENT_CORRECTED, AuditResult.SUCCESS,
                    RESOURCE_CONSENT, principal=principal,
                    child_id=episode.child_id,
                    resource_id=correction.consent_id, request_id=request_id,
                    episode_id=original.episode_id,
                    consent_id=correction.consent_id,
                    supersedes_consent_id=consent_id,
                    consent_document_version=(
                        correction.consent_document_version))
        return correction

    def record_setup(self, principal, episode_id: str, *,
                     configuration_summary: str, education_summary: str,
                     caregiver_confirmed_able_to_use: bool = False,
                     performed_at: Optional[datetime] = None,
                     request_id: str = "") -> RTMSetupEducation:
        """Record the one-time setup and education for this episode.

        Provider-performed, so it takes the full managing-clinician gate.
        DISTINCT from `ClinicalActionType.EDUCATION_OR_COACHING`, which is
        coaching decided on during a monitored month as part of treatment:
        this is the setup that made monitoring possible at all.
        """
        episode = self._load_episode(episode_id)
        self._require_episode_owner(principal, episode,
                                    "recording RTM setup and education")
        if not episode.is_open:
            raise EpisodeClosedForAccrual(
                "this RTM episode is closed; enrollment evidence cannot be "
                "added to it")
        if not episode.authorized_caregiver_id:
            raise RTMValidationError(
                "this episode names no authorized caregiver")

        try:
            setup = RTMSetupEducation.record(
                episode_id, episode.child_id,
                performed_by_actor_id=principal.application_id,
                caregiver_id=episode.authorized_caregiver_id,
                performed_at=performed_at or self._stamp(),
                configuration_summary=configuration_summary,
                education_summary=education_summary,
                caregiver_confirmed_able_to_use=(
                    caregiver_confirmed_able_to_use),
                recorded_by_actor_id=principal.application_id,
                now=self._stamp())
        except EnrollmentError as exc:
            raise RTMValidationError(str(exc)) from None
        self._repos.rtm_setups.create(setup)

        self._audit(AuditAction.RTM_SETUP_RECORDED, AuditResult.SUCCESS,
                    RESOURCE_SETUP, principal=principal,
                    child_id=episode.child_id, resource_id=setup.setup_id,
                    request_id=request_id, episode_id=episode_id,
                    setup_id=setup.setup_id,
                    caregiver_id=setup.caregiver_id,
                    provider_id=principal.application_id,
                    caregiver_confirmed=(
                        setup.caregiver_confirmed_able_to_use))
        return setup

    def correct_setup(self, principal, setup_id: str, *, reason: str,
                      configuration_summary: str = "",
                      education_summary: str = "",
                      caregiver_confirmed_able_to_use: Optional[bool] = None,
                      request_id: str = "") -> RTMSetupEducation:
        """Supersede a setup record. The original is RETAINED, with its time."""
        try:
            original = self._repos.rtm_setups.get_by_id(setup_id)
        except RecordNotFound:
            raise RTMConflict("no such setup record") from None
        episode = self._load_episode(original.episode_id)
        self._require_episode_owner(principal, episode,
                                    "correcting RTM setup and education")
        if not original.is_current:
            raise RTMConflict("this setup record has already been corrected")
        if not (reason or "").strip():
            raise RTMValidationError("a setup correction requires a reason")

        try:
            correction = RTMSetupEducation.record(
                original.episode_id, original.child_id,
                # The original PERFORMER and time are preserved: a correction
                # restates what was recorded, it does not re-perform the setup.
                performed_by_actor_id=original.performed_by_actor_id,
                caregiver_id=original.caregiver_id,
                performed_at=original.performed_at,
                configuration_summary=(configuration_summary
                                       or original.configuration_summary),
                education_summary=(education_summary
                                   or original.education_summary),
                caregiver_confirmed_able_to_use=(
                    original.caregiver_confirmed_able_to_use
                    if caregiver_confirmed_able_to_use is None
                    else caregiver_confirmed_able_to_use),
                recorded_by_actor_id=principal.application_id,
                supersedes_setup_id=setup_id,
                correction_reason=reason, now=self._stamp())
        except EnrollmentError as exc:
            raise RTMValidationError(str(exc)) from None
        self._repos.rtm_setups.create(correction)
        self._repos.rtm_setups.update(
            original.with_successor(correction.setup_id))

        self._audit(AuditAction.RTM_SETUP_CORRECTED, AuditResult.SUCCESS,
                    RESOURCE_SETUP, principal=principal,
                    child_id=episode.child_id,
                    resource_id=correction.setup_id, request_id=request_id,
                    episode_id=original.episode_id,
                    setup_id=correction.setup_id,
                    supersedes_setup_id=setup_id,
                    caregiver_confirmed=(
                        correction.caregiver_confirmed_able_to_use))
        return correction

    def list_consents(self, principal, episode_id: str):
        episode = self._load_episode(episode_id)
        self._authorize(principal, episode.child_id)
        return list(self._repos.rtm_consents.list_for_episode(episode_id))

    def list_setups(self, principal, episode_id: str):
        episode = self._load_episode(episode_id)
        self._authorize(principal, episode.child_id)
        return list(self._repos.rtm_setups.list_for_episode(episode_id))

    def declare_technology(self, principal, episode_id: str,
                           product_descriptor: str, *,
                           technology_version: str = "",
                           attestation_ref: str = "",
                           request_id: str = "") -> RTMTechnology:
        """Record the monitoring technology. Status is always UNDER_REVIEW."""
        episode = self._load_episode(episode_id)
        self._require_episode_owner(principal, episode,
                                    "declaring RTM technology")

        technology = RTMTechnology.declare(
            episode_id, episode.child_id, product_descriptor,
            technology_version=technology_version,
            attestation_ref=attestation_ref, now=self._stamp())
        self._repos.rtm_technologies.create(technology)
        self._audit(AuditAction.RTM_TECHNOLOGY_DECLARED, AuditResult.SUCCESS,
                    RESOURCE_TECHNOLOGY, principal=principal,
                    child_id=episode.child_id,
                    resource_id=technology.technology_id,
                    request_id=request_id, episode_id=episode_id,
                    technology_id=technology.technology_id,
                    regulatory_status=technology.regulatory_status.value)
        return technology

    # =====================================================================
    # Monitoring period
    # =====================================================================

    def open_period(self, principal, episode_id: str, focus_plan_id: str, *,
                    request_id: str = "") -> RTMMonitoringPeriod:
        """Open the month. Wins a claim on (episode, cycle_month).

        The focus plan supplies the cycle month AND the timezone of record, so
        the period cannot disagree with the plan it monitors.
        """
        episode = self._load_episode(episode_id)
        self._require_episode_owner(principal, episode,
                                    "opening a monitoring period")
        if not episode.is_open:
            raise RTMConflict("a closed episode cannot gain a new period")
        # Slice 3C: THE PREREQUISITE GATE, and it sits here deliberately.
        #
        # Monitoring "begins" when a month exists, because every accruing
        # write needs a period id. Refusing the month therefore refuses the
        # minutes, reviews, actions and contacts in one place, instead of five
        # gates that could fall out of step.
        #
        # It refuses RTM monitoring and nothing else: no caregiver path, plan
        # path, observation path or read consults readiness.
        readiness = self._readiness_for(episode)
        if not readiness.is_ready:
            raise RTMPrerequisitesIncomplete(
                "RTM monitoring cannot begin until the enrollment "
                "prerequisites exist: " + ", ".join(readiness.missing))

        try:
            plan = self._repos.focus_plans.get_by_id(focus_plan_id)
        except RecordNotFound:
            raise RTMValidationError("no such monthly focus plan") from None
        if plan.child_id != episode.child_id:
            raise RTMValidationError(
                "focus plan belongs to a different child")

        draft = RTMMonitoringPeriod.create(
            episode_id, episode.child_id, focus_plan_id, plan.cycle_month,
            plan.timezone_of_record, now=self._stamp())

        # Write-time uniqueness, the 0.4A mechanism. Generation read OUTSIDE
        # the transaction: a stale value cannot create a second winner, only
        # a collision, and a collision is the refusal we want.
        parts = (episode_id, plan.cycle_month)
        generation = self._repos.identity_claims.next_generation(
            ClaimKind.RTM_MONITORING_PERIOD, key_digest(*parts))

        def _acquire(store):
            tx = self._repos_factory(store)
            claim = IdentityClaim.build(
                ClaimKind.RTM_MONITORING_PERIOD, parts, generation,
                holder_ref=draft.period_id, child_id=episode.child_id,
                actor_id=principal.application_id, now=self._stamp())
            tx.identity_claims.claim(claim)
            persisted = draft.activate(claim_id=claim.claim_id,
                                       now=self._stamp())
            tx.rtm_periods.create(persisted)
            return persisted

        try:
            period = self._repos.store.run_in_transaction(_acquire)
        except (DuplicateRecord, DocumentStoreError):
            self._audit(AuditAction.RTM_PERIOD_OPENED, AuditResult.FAILURE,
                        RESOURCE_PERIOD, principal=principal,
                        child_id=episode.child_id, resource_id=None,
                        request_id=request_id, episode_id=episode_id,
                        cycle_month=plan.cycle_month)
            raise RTMConflict(
                "a monitoring period already exists for this episode-month"
            ) from None

        self._audit(AuditAction.RTM_PERIOD_OPENED, AuditResult.SUCCESS,
                    RESOURCE_PERIOD, principal=principal,
                    child_id=episode.child_id, resource_id=period.period_id,
                    request_id=request_id, episode_id=episode_id,
                    period_id=period.period_id, cycle_month=plan.cycle_month,
                    focus_plan_id=focus_plan_id,
                    claim_id=period.uniqueness_claim_id,
                    claim_kind=ClaimKind.RTM_MONITORING_PERIOD.value)
        return period

    def finalize_period(self, principal, period_id: str, *,
                        request_id: str = "") -> RTMMonitoringPeriod:
        """Finalize the MONTH. The episode is deliberately left untouched.

        THE ONE CALLER THAT MAY RUN AFTER THE EPISODE IS CLOSED, and the only
        one that passes `require_open_episode=False`.

        Finalizing adds no evidence: it records that a month is complete and
        changes no minute, review, action or contact. Refusing it after closure
        would strand the final month of every course of treatment in ACTIVE
        forever — and because `coding.rules` reads `period_finalized` as a
        missing-documentation flag, that month's report would permanently carry
        a defect the clinician had no way to clear. The founder requirement is
        that the final month stays reportable, so this stays open.

        Still fully authorized, and still refused once the month itself is
        finalized: the `FinalizedRecordImmutable` gate is untouched.
        """
        period, episode = self._open_period_for_write(
            principal, period_id, "finalizing a monitoring period",
            require_open_episode=False)

        finalized = period.finalize(actor_id=principal.application_id,
                                    now=self._stamp())
        self._repos.rtm_periods.update(finalized)
        self._audit(AuditAction.RTM_PERIOD_FINALIZED, AuditResult.SUCCESS,
                    RESOURCE_PERIOD, principal=principal,
                    child_id=period.child_id, resource_id=period_id,
                    request_id=request_id, period_id=period_id,
                    episode_id=period.episode_id,
                    cycle_month=period.cycle_month,
                    period_status=finalized.status.value)
        return finalized

    # =====================================================================
    # Clinical documentation
    # =====================================================================

    def record_review(self, principal, period_id: str, *,
                      clinical_interpretation: str,
                      reviewed_event_ids: Sequence[str] = (),
                      reviewed_cycle_ids: Sequence[str] = (),
                      request_id: str = "") -> TherapistReview:
        """Record clinician interpretation. May stand alone, with no action."""
        period, _ = self._open_period_for_write(
            principal, period_id, "recording a therapist review")
        self._require_events_in_period(period, reviewed_event_ids)

        review = TherapistReview.create(
            period_id, period.child_id, principal.application_id,
            clinical_interpretation=clinical_interpretation,
            reviewed_event_ids=tuple(reviewed_event_ids),
            reviewed_cycle_ids=tuple(reviewed_cycle_ids),
            now=self._stamp())
        self._repos.therapist_reviews.create(review)
        self._audit(AuditAction.THERAPIST_REVIEW_RECORDED, AuditResult.SUCCESS,
                    RESOURCE_REVIEW, principal=principal,
                    child_id=period.child_id, resource_id=review.review_id,
                    request_id=request_id, period_id=period_id,
                    review_id=review.review_id,
                    reviewed_event_count=len(reviewed_event_ids),
                    provider_id=principal.application_id)
        return review

    def _require_events_in_period(self, period: RTMMonitoringPeriod,
                                  event_ids: Sequence[str]) -> None:
        """Every referenced event must be this child's, in this month.

        Fails closed on a cross-child or cross-month reference. Without this,
        a clinician could attach another child's evidence to a review by
        guessing an id.
        """
        if not event_ids:
            return
        wanted = set(event_ids)
        found = {}
        for cycle in self._repos.weekly_cycles.list_for_plan(
                period.focus_plan_id):
            for event in self._repos.observation_events.list_for_cycle(
                    cycle.cycle_id):
                if event.event_id in wanted:
                    found[event.event_id] = event
        missing = wanted - set(found)
        if missing:
            raise RTMValidationError(
                "referenced observation events do not belong to this period")
        for event in found.values():
            if event.child_id != period.child_id:
                raise RTMValidationError(
                    "referenced event belongs to a different child")
            if event.attribution_month != period.cycle_month:
                raise RTMValidationError(
                    "referenced event is attributed to a different month")

    def record_clinical_action(self, principal, review_id: str, *,
                               action_type: ClinicalActionType,
                               narrative: str,
                               request_id: str = "") -> ClinicalAction:
        try:
            review = self._repos.therapist_reviews.get_by_id(review_id)
        except RecordNotFound:
            raise RTMConflict("no such therapist review") from None
        period, _ = self._open_period_for_write(
            principal, review.period_id, "recording a clinical action")

        action = ClinicalAction.create(
            review_id, period.period_id, period.child_id,
            principal.application_id, action_type=action_type,
            narrative=narrative, now=self._stamp())
        self._repos.clinical_actions.create(action)
        self._audit(AuditAction.CLINICAL_ACTION_RECORDED, AuditResult.SUCCESS,
                    RESOURCE_ACTION, principal=principal,
                    child_id=period.child_id, resource_id=action.action_id,
                    request_id=request_id, period_id=period.period_id,
                    review_id=review_id, action_id=action.action_id,
                    clinical_action_type=action_type.value,
                    provider_id=principal.application_id)
        return action

    def record_time(self, principal, period_id: str, *, local_date: str,
                    minutes: int, activity_description: str,
                    source_review_id: Optional[str] = None,
                    source_action_id: Optional[str] = None,
                    request_id: str = "") -> TimeEntry:
        """Record MANUALLY entered treatment-management minutes.

        There is no automatic source anywhere in this path: `TimeEntryMethod`
        has one member and the service never infers a duration.
        """
        period, _ = self._open_period_for_write(
            principal, period_id, "recording treatment-management time")
        self._require_date_in_period(period, local_date)

        entry = TimeEntry.record(
            period_id, period.child_id, principal.application_id,
            local_date=local_date,
            timezone_of_record=period.timezone_of_record,
            minutes=minutes, activity_description=activity_description,
            source_review_id=source_review_id,
            source_action_id=source_action_id, now=self._stamp())
        self._repos.time_entries.create(entry)
        self._audit(AuditAction.TIME_ENTRY_RECORDED, AuditResult.SUCCESS,
                    RESOURCE_TIME, principal=principal,
                    child_id=period.child_id, resource_id=entry.time_entry_id,
                    request_id=request_id, period_id=period_id,
                    time_entry_id=entry.time_entry_id, minutes=minutes,
                    entry_method=entry.entry_method.value,
                    provider_id=principal.application_id)
        return entry

    def correct_time(self, principal, time_entry_id: str, *, minutes: int,
                     reason: str, activity_description: str = "",
                     request_id: str = "") -> TimeEntry:
        """Supersede a time entry. The original row is RETAINED.

        Totals count only entries nothing supersedes, so a correction cannot
        double-count the minutes it was meant to fix.
        """
        try:
            original = self._repos.time_entries.get_by_id(time_entry_id)
        except RecordNotFound:
            raise RTMConflict("no such time entry") from None
        period, _ = self._open_period_for_write(
            principal, original.period_id, "correcting a time entry")
        if not original.is_current:
            raise RTMConflict("this time entry has already been corrected")

        correction = TimeEntry.record(
            original.period_id, original.child_id, principal.application_id,
            local_date=original.local_date,
            timezone_of_record=original.timezone_of_record,
            minutes=minutes,
            activity_description=(activity_description
                                  or original.activity_description),
            source_review_id=original.source_review_id,
            source_action_id=original.source_action_id,
            supersedes_time_entry_id=time_entry_id,
            correction_reason=reason, now=self._stamp())
        self._repos.time_entries.create(correction)
        self._repos.time_entries.update(
            original.with_successor(correction.time_entry_id))

        self._audit(AuditAction.TIME_ENTRY_CORRECTED, AuditResult.SUCCESS,
                    RESOURCE_TIME, principal=principal,
                    child_id=period.child_id,
                    resource_id=correction.time_entry_id,
                    request_id=request_id, period_id=original.period_id,
                    time_entry_id=correction.time_entry_id,
                    supersedes_time_entry_id=time_entry_id, minutes=minutes,
                    provider_id=principal.application_id)
        return correction

    def _require_date_in_period(self, period: RTMMonitoringPeriod,
                                local_date: str) -> None:
        from ..domain.observation import attribution_month_for

        if attribution_month_for(local_date) != period.cycle_month:
            raise RTMValidationError(
                "local date falls outside this monitoring period's month")

    def record_synchronous_interaction(
            self, principal, period_id: str, *, local_date: str,
            modality: InteractionModality, participant_type: ParticipantType,
            occurred_at_utc: Optional[datetime] = None,
            duration_minutes: Optional[int] = None,
            real_time_affirmed: bool = True, note_ref: str = "",
            request_id: str = "") -> SynchronousInteraction:
        """Record a real-time contact that actually happened.

        No asynchronous exchange can reach this method: `InteractionModality`
        has no MESSAGE, EMAIL or NOTE member, and duration is never inferred.
        """
        period, _ = self._open_period_for_write(
            principal, period_id, "recording a synchronous interaction")
        self._require_date_in_period(period, local_date)

        interaction = SynchronousInteraction.record(
            period_id, period.child_id, principal.application_id,
            occurred_at_utc=occurred_at_utc or self._stamp(),
            local_date=local_date,
            timezone_of_record=period.timezone_of_record,
            modality=modality, participant_type=participant_type,
            duration_minutes=duration_minutes,
            real_time_affirmed=real_time_affirmed, note_ref=note_ref,
            now=self._stamp())
        self._repos.synchronous_interactions.create(interaction)
        self._audit(AuditAction.SYNCHRONOUS_INTERACTION_RECORDED,
                    AuditResult.SUCCESS, RESOURCE_INTERACTION,
                    principal=principal, child_id=period.child_id,
                    resource_id=interaction.interaction_id,
                    request_id=request_id, period_id=period_id,
                    interaction_id=interaction.interaction_id,
                    interaction_modality=modality.value,
                    participant_type=participant_type.value,
                    provider_id=principal.application_id)
        return interaction

    # =====================================================================
    # Derived summaries
    # =====================================================================

    def _gather(self, period: RTMMonitoringPeriod):
        cycles = self._repos.weekly_cycles.list_for_plan(period.focus_plan_id)
        alignments, events = [], []
        for cycle in cycles:
            alignments.extend(self._repos.alignments.list_for_cycle(cycle.cycle_id))
            events.extend(
                self._repos.observation_events.list_for_cycle(cycle.cycle_id))
        technologies = self._repos.rtm_technologies.list_for_episode(
            period.episode_id)
        return {
            "cycles": cycles,
            "alignments": alignments,
            "events": events,
            "reviews": self._repos.therapist_reviews.list_for_period(
                period.period_id),
            "actions": self._repos.clinical_actions.list_for_period(
                period.period_id),
            "time_entries": self._repos.time_entries.list_for_period(
                period.period_id),
            "interactions": self._repos.synchronous_interactions.list_for_period(
                period.period_id),
            "technology": technologies[-1] if technologies else None,
        }

    def generate_evidence_summary(self, principal, period_id: str, *,
                                  request_id: str = "") -> RTMEvidenceSummary:
        """Derive the factual monthly summary. Regenerable; mints a new id."""
        period = self._load_period(period_id)
        episode = self._load_episode(period.episode_id)
        self._require_episode_owner(principal, episode,
                                    "generating an RTM evidence summary")

        gathered = self._gather(period)
        summary = derive_evidence_summary(
            period, clinical_goal_refs=episode.clinical_goal_refs,
            now=self._stamp(), **gathered)
        self._repos.evidence_summaries.create(summary)

        self._audit(AuditAction.RTM_EVIDENCE_SUMMARY_GENERATED,
                    AuditResult.SUCCESS, RESOURCE_EVIDENCE,
                    principal=principal, child_id=period.child_id,
                    resource_id=summary.summary_id, request_id=request_id,
                    period_id=period_id, summary_id=summary.summary_id,
                    observation_event_count=summary.total_distinct_observation_events,
                    distinct_observed_local_dates=summary.distinct_observed_local_dates,
                    documented_minutes=summary.documented_management_minutes,
                    rule_version=summary.rule_version)
        return summary

    def generate_coding_assistance(self, principal, period_id: str, *,
                                   request_id: str = ""
                                   ) -> CodingAssistanceSummary:
        """Produce POTENTIAL code candidates. Never an authorisation."""
        period = self._load_period(period_id)
        episode = self._load_episode(period.episode_id)
        self._require_episode_owner(principal, episode,
                                    "generating coding assistance")

        gathered = self._gather(period)
        month_entries = [t for t in gathered["time_entries"]
                         if t.attribution_month == period.cycle_month]
        minutes = documented_minutes(month_entries)
        month_interactions = [i for i in gathered["interactions"]
                              if i.attribution_month == period.cycle_month]
        qualifying = [i for i in month_interactions
                      if i.counts_as_real_time_communication]
        technology = gathered["technology"]

        outcome = evaluate(CodingInputs(
            documented_management_minutes=minutes,
            real_time_interactive_communication_present=bool(qualifying),
            technology_eligibility_established=(
                technology.eligibility_established if technology else False),
            period_finalized=period.is_finalized))

        summary = CodingAssistanceSummary.create(
            period_id, period.child_id, now=self._stamp(),
            coding_rule_set_id=outcome.rule_set_id,
            coding_rule_version=outcome.rule_version,
            documented_management_minutes=minutes,
            real_time_interactive_communication_present=bool(qualifying),
            synchronous_interaction_refs=tuple(
                sorted(i.interaction_id for i in qualifying)),
            time_entry_refs=tuple(sorted(t.time_entry_id
                                         for t in month_entries
                                         if t.is_current)),
            potential_code_candidates=tuple(
                (c.code, c.units) for c in outcome.candidates),
            rule_explanations=outcome.rule_explanations,
            missing_requirement_flags=outcome.missing_requirements,
            technology_regulatory_status=(
                technology.regulatory_status if technology
                else RegulatoryStatus.UNDER_REVIEW))
        self._repos.coding_summaries.create(summary)

        self._audit(AuditAction.CODING_ASSISTANCE_GENERATED,
                    AuditResult.SUCCESS, RESOURCE_CODING, principal=principal,
                    child_id=period.child_id,
                    resource_id=summary.coding_summary_id,
                    request_id=request_id, period_id=period_id,
                    coding_summary_id=summary.coding_summary_id,
                    documented_minutes=minutes,
                    candidate_count=len(outcome.candidates),
                    missing_flag_count=len(outcome.missing_requirements),
                    coding_rule_set_id=outcome.rule_set_id,
                    coding_rule_version=outcome.rule_version)
        return summary

    def decide_coding_assistance(self, principal, coding_summary_id: str,
                                 status: ConfirmationStatus, *,
                                 note: str = "", request_id: str = ""
                                 ) -> CodingAssistanceSummary:
        """Record CONFIRMED or REJECTED beside the generated candidates.

        The candidates, explanations, flags and rule version are left exactly
        as produced, so the record answers what the clinician was looking at.
        """
        try:
            summary = self._repos.coding_summaries.get_by_id(coding_summary_id)
        except RecordNotFound:
            raise RTMConflict("no such coding assistance summary") from None
        period = self._load_period(summary.period_id)
        episode = self._load_episode(period.episode_id)
        self._require_episode_owner(principal, episode,
                                    "deciding coding assistance")

        decided = summary.with_decision(
            status, actor_id=principal.application_id, note=note,
            now=self._stamp())
        self._repos.coding_summaries.update(decided)
        self._audit(AuditAction.CODING_ASSISTANCE_DECIDED, AuditResult.SUCCESS,
                    RESOURCE_CODING, principal=principal,
                    child_id=period.child_id, resource_id=coding_summary_id,
                    request_id=request_id,
                    coding_summary_id=coding_summary_id,
                    confirmation_status=status.value,
                    provider_id=principal.application_id)
        return decided

    # =====================================================================
    # Month-end report
    # =====================================================================

    def generate_report(self, principal, period_id: str, *,
                        request_id: str = "") -> MonthEndReport:
        """Assemble sections A-K as a DRAFT. Nothing is finalized here."""
        period = self._load_period(period_id)
        episode = self._load_episode(period.episode_id)
        self._require_episode_owner(principal, episode,
                                    "generating a month-end report")

        evidence = self._repos.evidence_summaries.latest_for_period(period_id)
        coding = self._repos.coding_summaries.latest_for_period(period_id)
        if evidence is None or coding is None:
            raise RTMValidationError(
                "generate the evidence and coding summaries before the report")

        gathered = self._gather(period)
        month = period.cycle_month
        month_events = [e for e in gathered["events"]
                        if e.attribution_month == month]
        month_entries = [t for t in gathered["time_entries"]
                         if t.attribution_month == month]
        adaptations, gaps = [], []
        for cycle in gathered["cycles"]:
            if cycle.adaptation_record_id:
                try:
                    adaptations.append(
                        self._repos.adaptation_records.get_by_id(
                            cycle.adaptation_record_id))
                except RecordNotFound:  # pragma: no cover - defensive
                    pass
            gaps.extend(self._repos.coverage_gaps.list_for_cycle(cycle.cycle_id))

        sections = build_sections(
            evidence, coding,
            goal_snapshots=self._repos.goal_snapshots.list_for_plan(
                period.focus_plan_id),
            events=month_events,
            adaptations=adaptations,
            coverage_gaps=gaps,
            reviews=gathered["reviews"],
            actions=gathered["actions"],
            time_entries=month_entries,
            minutes=documented_minutes(month_entries),
            interactions=[i for i in gathered["interactions"]
                          if i.attribution_month == month])

        report = MonthEndReport.create(
            period_id, period.child_id, period.focus_plan_id, month,
            sections=sections, evidence_summary_id=evidence.summary_id,
            coding_summary_id=coding.coding_summary_id, now=self._stamp())
        self._repos.month_end_reports.create(report)
        return report

    def finalize_report(self, principal, report_id: str, *,
                        request_id: str = "") -> MonthEndReport:
        """Issue the report. After this it is amended, never edited."""
        report = self._load_report(report_id)
        period = self._load_period(report.period_id)
        episode = self._load_episode(period.episode_id)
        self._require_episode_owner(principal, episode,
                                    "finalizing a month-end report")
        if report.is_finalized:
            raise FinalizedRecordImmutable(
                "this report is already finalized; amend it instead")

        finalized = report.finalize(actor_id=principal.application_id,
                                    now=self._stamp())
        self._repos.month_end_reports.update(finalized)
        self._audit(AuditAction.MONTH_END_REPORT_FINALIZED,
                    AuditResult.SUCCESS, RESOURCE_REPORT, principal=principal,
                    child_id=report.child_id, resource_id=report_id,
                    request_id=request_id, report_id=report_id,
                    period_id=report.period_id,
                    cycle_month=report.cycle_month,
                    report_version=finalized.version,
                    report_state=finalized.state.value)
        return finalized

    def amend_report(self, principal, report_id: str, *, reason: str,
                     request_id: str = "") -> MonthEndReport:
        """Write an AMENDED successor. The predecessor is NOT edited.

        `reason` is a required keyword with no default. The successor is
        rebuilt from current evidence, so an amendment reflects what changed
        rather than a hand-edited copy of what was issued.
        """
        predecessor = self._load_report(report_id)
        period = self._load_period(predecessor.period_id)
        episode = self._load_episode(period.episode_id)
        self._require_episode_owner(principal, episode,
                                    "amending a month-end report")
        if not predecessor.is_finalized:
            raise RTMConflict("only a finalized report can be amended")
        if not predecessor.is_current:
            raise RTMConflict("this report has already been superseded")
        # Validate the reason BEFORE rebuilding. `MonthEndReport.amend` checks
        # it too, but that check runs after `generate_report` has already
        # persisted a draft — so a refused amendment used to leave an orphan
        # draft row behind. Found by a mutation sweep follow-up.
        if not (reason or "").strip():
            raise RTMValidationError("an amendment requires a stated reason")

        rebuilt = self.generate_report(principal, predecessor.period_id,
                                       request_id=request_id)
        successor = predecessor.amend(
            actor_id=principal.application_id, reason=reason,
            sections=rebuilt.sections,
            evidence_summary_id=rebuilt.evidence_summary_id,
            coding_summary_id=rebuilt.coding_summary_id, now=self._stamp())
        self._repos.month_end_reports.create(successor)
        self._repos.month_end_reports.update(
            predecessor.with_successor(successor.report_id, now=self._stamp()))

        self._audit(AuditAction.MONTH_END_REPORT_AMENDED, AuditResult.SUCCESS,
                    RESOURCE_REPORT, principal=principal,
                    child_id=predecessor.child_id,
                    resource_id=successor.report_id, request_id=request_id,
                    report_id=successor.report_id,
                    supersedes_report_id=report_id,
                    period_id=predecessor.period_id,
                    report_version=successor.version,
                    report_state=successor.state.value)
        return successor

    def _load_report(self, report_id: str) -> MonthEndReport:
        try:
            return self._repos.month_end_reports.get_by_id(report_id)
        except RecordNotFound:
            raise RTMConflict("no such month-end report") from None

    # =====================================================================
    # Reads
    # =====================================================================

    def get_episode(self, principal, episode_id: str) -> RTMEpisode:
        episode = self._load_episode(episode_id)
        self._authorize(principal, episode.child_id)
        return episode

    def get_period(self, principal, period_id: str) -> RTMMonitoringPeriod:
        period = self._load_period(period_id)
        self._authorize(principal, period.child_id)
        return period

    def list_episodes(self, principal, child_id: str) -> List[RTMEpisode]:
        self._authorize(principal, child_id)
        return self._repos.rtm_episodes.list_for_child(child_id)

    def list_time_entries(self, principal, period_id: str) -> List[TimeEntry]:
        period = self._load_period(period_id)
        self._authorize(principal, period.child_id)
        return self._repos.time_entries.list_for_period(period_id)

    def documented_minutes_for(self, principal, period_id: str) -> int:
        """Current effective minutes. Superseded entries are excluded."""
        period = self._load_period(period_id)
        self._authorize(principal, period.child_id)
        return documented_minutes(
            [t for t in self._repos.time_entries.list_for_period(period_id)
             if t.attribution_month == period.cycle_month])
