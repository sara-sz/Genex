"""pilot_backend/domain/rtm_enrollment.py — what must exist before monitoring.

    RTMConsentAttestation  the caregiver's recorded consent. Append-only.
    RTMSetupEducation      what was configured and taught. Append-only.
    RTMReadiness           a derived verdict: may monitoring begin yet?

Slice 3C. These attach to the EPISODE, not to a monitoring period: consent and
setup establish a course of treatment, and recording them per month would
invite two months of one episode to disagree about whether the family ever
consented.

## THE ORDER IS ENROLLMENT -> CONSENT -> SETUP -> MONITORING

`RTMEpisode` can be opened before consent and setup exist, because the episode
IS the enrollment record those two attach to. What cannot happen is monitoring
beginning without them: `RTMReadiness` is the gate, and `open_period` consults
it, so no monitoring month — and therefore no minute, review, action or
contact — can exist before the prerequisites do.

## RTM READINESS IS NOT ACCOUNT ACCESS

An episode that is not RTM-ready blocks RTM monitoring and nothing else. The
family keeps using Genex exactly as before: plans, activities, observations and
reads are all untouched by anything in this module. The two states are
deliberately independent, and a test asserts ordinary caregiver use continues
while readiness is incomplete.

## ATTESTATION, NOT DOCUMENT STORAGE

A consent record carries the document's IDENTIFIER and VERSION plus what was
acknowledged. It does not carry the document. Genex records that a named
version was accepted, by whom, under what authority, at what time — the legal
artefact itself lives wherever the practice keeps it, and copying it here would
make this a document store with none of the controls one needs.

## CONSENT IS THE CAREGIVER'S, OR IT SAYS WHOSE PROXY IT IS

`attested_by` has exactly two values. `CAREGIVER_ATTESTED` means the authorized
caregiver themselves recorded it. `PROVIDER_RECORDED_PROXY` means a provider
recorded it on their behalf — and that value cannot be set without naming the
recording provider, the proxy documentation reference and a reason. There is no
third value and no default, so a provider assertion can never be mistaken for
the caregiver's own act.

## CORRECTIONS NEVER DESTROY AN ATTESTED FACT

Both records supersede rather than mutate, the same shape as `TimeEntry` and
`MonthEndReport`. A correction is a NEW row with a stated reason; the original
keeps its own timestamp, actor and document version. Readiness reads only
records nothing supersedes, so a correction cannot double-count and a
superseded row cannot keep a family ready on wording nobody accepted.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Optional, Sequence, Tuple

from .entities import SCHEMA_VERSION, utc_now
from .enums import Visibility
from .ids import new_rtm_consent_id, new_rtm_setup_id


class EnrollmentError(ValueError):
    """Invalid enrollment evidence. PHI-safe: names the rule, never content."""

    PHI_SAFE_MESSAGE = True


class ConsentAuthority(str, Enum):
    """The basis on which this person may consent for the child.

    Recorded rather than assumed. "The caregiver consented" is not a complete
    fact about a minor: who they are to the child is part of whether the
    consent is valid, and a system that stored only the signature would have
    thrown that away.
    """

    PARENT = "parent"
    LEGAL_GUARDIAN = "legal_guardian"
    AUTHORIZED_REPRESENTATIVE = "authorized_representative"


class ConsentAttestedBy(str, Enum):
    """Who performed the act of attesting.

    TWO members, no default. A provider recording consent on a caregiver's
    behalf is a different fact from the caregiver recording it themselves, and
    the second cannot be dressed as the first.
    """

    CAREGIVER_ATTESTED = "caregiver_attested"
    PROVIDER_RECORDED_PROXY = "provider_recorded_proxy"


#: The acknowledgements the pilot requires before monitoring may begin. Named
#: as a constant so the record, the readiness check and the tests all agree on
#: what "consented" means, rather than three places listing booleans.
REQUIRED_ACKNOWLEDGEMENTS: Tuple[str, ...] = (
    "acknowledged_monitoring",
    "acknowledged_billing_and_cost_sharing",
    "acknowledged_withdrawal_right",
    "acknowledged_communication_expectations",
    "acknowledged_response_expectations",
)


@dataclass(frozen=True)
class RTMConsentAttestation:
    """A caregiver's recorded RTM consent. Immutable; corrected by succession."""

    consent_id: str
    episode_id: str
    child_id: str
    #: WHO consented. The authorized caregiver on the episode.
    caregiver_id: str
    authority: ConsentAuthority
    #: WHICH document, and which version of it. Both required: "they consented"
    #: is not auditable without saying to what.
    consent_document_id: str
    consent_document_version: str
    accepted_at: datetime
    attested_by: ConsentAttestedBy
    acknowledged_monitoring: bool = False
    acknowledged_billing_and_cost_sharing: bool = False
    acknowledged_withdrawal_right: bool = False
    acknowledged_communication_expectations: bool = False
    acknowledged_response_expectations: bool = False
    #: Only for PROVIDER_RECORDED_PROXY, and all three are then required.
    proxy_recorded_by_provider_id: str = ""
    proxy_documentation_ref: str = ""
    proxy_reason: str = ""
    recorded_at: datetime = field(default_factory=utc_now)
    recorded_by_actor_id: str = ""
    #: Correction lineage. The predecessor row is RETAINED.
    supersedes_consent_id: Optional[str] = None
    superseded_by_consent_id: Optional[str] = None
    correction_reason: str = ""
    schema_version: str = SCHEMA_VERSION

    #: Consent is the family's own act and they may read it.
    VISIBILITY = Visibility.PARENT_VISIBLE

    def __post_init__(self) -> None:
        if not isinstance(self.authority, ConsentAuthority):
            raise EnrollmentError("authority must be a ConsentAuthority")
        if not isinstance(self.attested_by, ConsentAttestedBy):
            raise EnrollmentError("attested_by must be a ConsentAttestedBy")
        for label, value in (("episode_id", self.episode_id),
                             ("child_id", self.child_id),
                             ("caregiver_id", self.caregiver_id),
                             ("consent_document_id", self.consent_document_id),
                             ("consent_document_version",
                              self.consent_document_version)):
            if not (value or "").strip():
                raise EnrollmentError(f"a consent record requires {label}")
        if self.accepted_at.tzinfo is None:
            raise EnrollmentError("refusing a naive acceptance timestamp")
        if self.attested_by is ConsentAttestedBy.PROVIDER_RECORDED_PROXY:
            # A proxy with no provenance is a provider assertion wearing the
            # caregiver's name. Refused rather than recorded.
            for label, value in (
                    ("proxy_recorded_by_provider_id",
                     self.proxy_recorded_by_provider_id),
                    ("proxy_documentation_ref", self.proxy_documentation_ref),
                    ("proxy_reason", self.proxy_reason)):
                if not (value or "").strip():
                    raise EnrollmentError(
                        f"proxy-recorded consent requires {label}")
        else:
            for label, value in (
                    ("proxy_recorded_by_provider_id",
                     self.proxy_recorded_by_provider_id),
                    ("proxy_documentation_ref", self.proxy_documentation_ref),
                    ("proxy_reason", self.proxy_reason)):
                if (value or "").strip():
                    raise EnrollmentError(
                        f"{label} is only for proxy-recorded consent")
        if (self.supersedes_consent_id
                and not (self.correction_reason or "").strip()):
            raise EnrollmentError(
                "a correcting consent record requires a stated reason")

    @property
    def is_current(self) -> bool:
        """Whether this record still stands. Superseded rows do not."""
        return self.superseded_by_consent_id is None

    @property
    def missing_acknowledgements(self) -> Tuple[str, ...]:
        """Required acknowledgements this record does not carry."""
        return tuple(name for name in REQUIRED_ACKNOWLEDGEMENTS
                     if not getattr(self, name))

    @property
    def is_complete(self) -> bool:
        """Every required acknowledgement present. Partial consent is not
        consent, and nothing here infers one acknowledgement from another."""
        return not self.missing_acknowledgements

    @staticmethod
    def record(episode_id: str, child_id: str, caregiver_id: str, *,
               authority: ConsentAuthority,
               consent_document_id: str, consent_document_version: str,
               accepted_at: datetime,
               attested_by: ConsentAttestedBy,
               acknowledged_monitoring: bool = False,
               acknowledged_billing_and_cost_sharing: bool = False,
               acknowledged_withdrawal_right: bool = False,
               acknowledged_communication_expectations: bool = False,
               acknowledged_response_expectations: bool = False,
               proxy_recorded_by_provider_id: str = "",
               proxy_documentation_ref: str = "", proxy_reason: str = "",
               recorded_by_actor_id: str = "",
               supersedes_consent_id: Optional[str] = None,
               correction_reason: str = "",
               now: Optional[datetime] = None) -> "RTMConsentAttestation":
        return RTMConsentAttestation(
            consent_id=new_rtm_consent_id(),
            episode_id=episode_id,
            child_id=child_id,
            caregiver_id=caregiver_id,
            authority=authority,
            consent_document_id=consent_document_id,
            consent_document_version=consent_document_version,
            accepted_at=accepted_at,
            attested_by=attested_by,
            acknowledged_monitoring=acknowledged_monitoring,
            acknowledged_billing_and_cost_sharing=(
                acknowledged_billing_and_cost_sharing),
            acknowledged_withdrawal_right=acknowledged_withdrawal_right,
            acknowledged_communication_expectations=(
                acknowledged_communication_expectations),
            acknowledged_response_expectations=(
                acknowledged_response_expectations),
            proxy_recorded_by_provider_id=proxy_recorded_by_provider_id,
            proxy_documentation_ref=proxy_documentation_ref,
            proxy_reason=proxy_reason,
            recorded_at=now or utc_now(),
            recorded_by_actor_id=recorded_by_actor_id,
            supersedes_consent_id=supersedes_consent_id,
            correction_reason=correction_reason,
        )

    def with_successor(self, successor_id: str) -> "RTMConsentAttestation":
        """Mark this record corrected. The row is retained, never edited."""
        return replace(self, superseded_by_consent_id=successor_id)


@dataclass(frozen=True)
class RTMSetupEducation:
    """What was configured and taught before monitoring. Append-only.

    DISTINCT from `ClinicalActionType.EDUCATION_OR_COACHING`, which documents
    coaching a clinician decided on DURING a monitored month as part of
    treatment. This records the one-time setup that made monitoring possible at
    all. Collapsing them would make "was this family ever set up?" unanswerable
    from a month in which no coaching happened to be documented.

    Deliberately FACTUAL. It records what was configured, what was taught and
    that the caregiver confirmed they can use the system. It carries no
    assessment of how well they can use it, no competence score and no clinical
    judgement — those would be clinical findings, and this is a setup log.
    """

    setup_id: str
    episode_id: str
    child_id: str
    #: The staff member or clinician who performed the setup.
    performed_by_actor_id: str
    #: The caregiver who was present and taught.
    caregiver_id: str
    performed_at: datetime
    #: What was configured, and what was taught. Short factual descriptions.
    configuration_summary: str
    education_summary: str
    #: The caregiver's own confirmation that they can use the system. A FACT
    #: that they said so, never an assessment that they can.
    caregiver_confirmed_able_to_use: bool = False
    recorded_at: datetime = field(default_factory=utc_now)
    recorded_by_actor_id: str = ""
    supersedes_setup_id: Optional[str] = None
    superseded_by_setup_id: Optional[str] = None
    correction_reason: str = ""
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.PARENT_VISIBLE

    def __post_init__(self) -> None:
        for label, value in (("episode_id", self.episode_id),
                             ("child_id", self.child_id),
                             ("performed_by_actor_id",
                              self.performed_by_actor_id),
                             ("caregiver_id", self.caregiver_id),
                             ("configuration_summary",
                              self.configuration_summary),
                             ("education_summary", self.education_summary)):
            if not (value or "").strip():
                raise EnrollmentError(f"a setup record requires {label}")
        if self.performed_at.tzinfo is None:
            raise EnrollmentError("refusing a naive setup timestamp")
        if (self.supersedes_setup_id
                and not (self.correction_reason or "").strip()):
            raise EnrollmentError(
                "a correcting setup record requires a stated reason")

    @property
    def is_current(self) -> bool:
        return self.superseded_by_setup_id is None

    @staticmethod
    def record(episode_id: str, child_id: str, *,
               performed_by_actor_id: str, caregiver_id: str,
               performed_at: datetime, configuration_summary: str,
               education_summary: str,
               caregiver_confirmed_able_to_use: bool = False,
               recorded_by_actor_id: str = "",
               supersedes_setup_id: Optional[str] = None,
               correction_reason: str = "",
               now: Optional[datetime] = None) -> "RTMSetupEducation":
        return RTMSetupEducation(
            setup_id=new_rtm_setup_id(),
            episode_id=episode_id,
            child_id=child_id,
            performed_by_actor_id=performed_by_actor_id,
            caregiver_id=caregiver_id,
            performed_at=performed_at,
            configuration_summary=configuration_summary,
            education_summary=education_summary,
            caregiver_confirmed_able_to_use=caregiver_confirmed_able_to_use,
            recorded_at=now or utc_now(),
            recorded_by_actor_id=recorded_by_actor_id,
            supersedes_setup_id=supersedes_setup_id,
            correction_reason=correction_reason,
        )

    def with_successor(self, successor_id: str) -> "RTMSetupEducation":
        return replace(self, superseded_by_setup_id=successor_id)


#: Readiness reason codes. Short, stable, PHI-free strings so a refusal can say
#: WHICH prerequisite is missing without a clinician having to guess.
MISSING_ENROLLMENT_METADATA = "enrollment_metadata_incomplete"
MISSING_GOAL_VERSION_PIN = "approved_goal_versions_not_pinned"
MISSING_CONSENT = "caregiver_consent_absent"
MISSING_CONSENT_ACKNOWLEDGEMENTS = "caregiver_consent_incomplete"
MISSING_SETUP = "setup_and_education_absent"
MISSING_SETUP_CONFIRMATION = "caregiver_did_not_confirm_able_to_use"
EPISODE_CLOSED = "episode_closed"


@dataclass(frozen=True)
class RTMReadiness:
    """Whether monitoring may begin. Derived; the records remain the truth.

    Carries the reasons rather than a bare boolean: a clinician told only
    "not ready" has to go looking, and a refusal that cannot say why is a
    support ticket.
    """

    episode_id: str
    missing: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "missing", tuple(self.missing))

    @property
    def is_ready(self) -> bool:
        return not self.missing


#: FROZEN DESIGN FOR SLICE 3D — WITHDRAWAL. Recorded here, beside the object
#: a future implementer would be tempted to misuse.
#:
#: Caregiver withdrawal resolves to an EFFECTIVE EPISODE CLOSURE. A withdrawal
#: record may carry its own reason, effective timestamp and provenance, and it
#: then INVOKES the existing episode-closure transition. Once closure is
#: effective:
#:
#:     slice 3A's single episode-open gate blocks new time, reviews, actions
#:     and interactions;
#:     `open_period` refuses a new RTM month;
#:     ordinary Genex use continues, untouched.
#:
#: WHAT 3D MUST NOT DO: make every accrual path independently re-check
#: readiness. `assess_readiness` returns a value derived from mutable records,
#: and scattering that check across five writers would create five places for
#: the rule to drift and would make withdrawal enforcement depend on a
#: recomputation rather than on a state transition. The episode-open
#: chokepoint stays the enforcement mechanism — one gate, already proven.
#:
#: This is also why `assess_readiness` is deliberately NOT consulted by the
#: accrual paths today: adding it there now would pre-build the wrong shape.


def assess_readiness(episode, consents: Sequence[RTMConsentAttestation] = (),
                     setups: Sequence[RTMSetupEducation] = ()
                     ) -> RTMReadiness:
    """May RTM monitoring begin for this episode?

    Pure. Reads only CURRENT records — a superseded consent cannot keep a
    family ready on wording nobody accepted, and a superseded setup cannot
    stand in for one that was corrected.

    Enrollment completeness is checked HERE rather than only in the record's
    constructor, because episodes persisted before slice 3C carry none of the
    new metadata. Those decode cleanly and are simply not RTM-ready, which is
    the honest state for an episode nobody enrolled under these rules.
    """
    missing = []

    if not episode.is_open:
        missing.append(EPISODE_CLOSED)

    if not episode.enrollment_complete:
        missing.append(MISSING_ENROLLMENT_METADATA)
    if not episode.pinned_goal_version_ids:
        missing.append(MISSING_GOAL_VERSION_PIN)

    current_consents = [c for c in consents
                        if c.is_current and c.episode_id == episode.episode_id]
    if not current_consents:
        missing.append(MISSING_CONSENT)
    elif not any(c.is_complete for c in current_consents):
        missing.append(MISSING_CONSENT_ACKNOWLEDGEMENTS)

    current_setups = [s for s in setups
                      if s.is_current and s.episode_id == episode.episode_id]
    if not current_setups:
        missing.append(MISSING_SETUP)
    elif not any(s.caregiver_confirmed_able_to_use for s in current_setups):
        missing.append(MISSING_SETUP_CONFIRMATION)

    return RTMReadiness(episode_id=episode.episode_id,
                        missing=tuple(missing))
