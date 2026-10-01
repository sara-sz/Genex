"""pilot_backend/persistence/firestore_repos.py — production-capable repositories.

Implements the BACKEND 0.1 repository protocols over a `DocumentStore`. The
protocols are unchanged: the domain layer does not know a database exists, and
swapping `InMemoryRepositories` for `FirestoreRepositories` changes no business
logic. That was the point of keeping them narrow.

## Still no delete

Same guarantee as 0.1, now at the storage layer: no method here deletes a
document, and the `DocumentStore` port exposes no delete operation at all. A
repository cannot erase history because the storage contract it is written
against has no way to.

## Ordering

Listings sort by `created_at` then the record's OWN id. Firestore returns
query results in its own order, not insertion order, so the sort is applied
after reading rather than assumed. The tiebreak uses `connection_id` first for
connection records — a foreign key would tie every row belonging to the same
caregiver and silently fall back to whatever order the store returned, which is
the intermittent flake BACKEND 0.1 fixed.

## Not connected to anything yet

There is no Firestore client here and no credential. A composition root binds a
real one to the `DocumentStore` port once the HIPAA workstream approves a
production project, Identity Platform configuration, BAA coverage, IAM, logging
and backup posture. Until then these run against `FakeDocumentStore` or the
Firestore emulator, both of which satisfy the same port.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional, Sequence, Type

from ..audit.events import AuditEvent
from ..domain.adaptation import AdaptationRecord
from ..domain.alignment import (
    ActivityGoalAlignment,
    CapacityLedger,
    CoverageGap,
)
from ..domain.child_context import ChildContextRecord
from ..domain.intervention import TherapistIntervention
from ..domain.month_end import MonthEndReport
from ..domain.rtm import RTMEpisode, RTMMonitoringPeriod, RTMTechnology
from ..domain.rtm_documentation import (
    ClinicalAction,
    SynchronousInteraction,
    TherapistReview,
    TimeEntry,
)
from ..domain.rtm_summary import CodingAssistanceSummary, RTMEvidenceSummary
from ..domain.observation import (
    DeferRecord,
    ObservationEvent,
    ParentCustomizationSignal,
)
from ..domain.weekly_cycle import (
    WeeklyCycle,
    WeeklyPlanLink,
    WeeklyPlanSnapshot,
)
from ..domain.goals import (
    CaregiverApprovedGoal,
    ClinicalGoal,
    GoalSuggestion,
    GoalVersion,
)
from ..domain.identity_claims import ClaimKind, ClaimRecordKind, IdentityClaim
from ..domain.managing_clinician import ManagingClinicianAssignment
from ..domain.monthly_plan import (
    MonthlyFocusPlan,
    MonthlyGoalAllocation,
    MonthlyGoalSnapshot,
)
from ..domain.source_link import SourceSystemLink
from ..domain.connections import CaregiverChildConnection, ProviderChildConnection
from ..domain.entities import Caregiver, Child, Practice, Provider
from ..domain.enums import ConnectionStatus, EntityStatus
from ..repository.interface import (
    AmbiguousAuthSubject,
    AmbiguousRecordState,
    DuplicateRecord,
    RecordNotFound,
)
from ..revision.records import ImmutableRecordError, Revision
from .codecs import decode, encode
from .collections import collection_for
from .document_store import DocumentStore, DocumentStoreError


def _own_id(record: Any) -> str:
    """The record's OWN identifier, for a deterministic sort tiebreak.

    `connection_id` is checked first and that order is load-bearing — see the
    module docstring.
    """
    for attr in ("connection_id",
                 # 0.4B/C own-ids go BEFORE child_id and focus_plan_id for the
                 # reason in the module docstring: every suggestion, goal,
                 # allocation and snapshot for one child shares its child_id,
                 # and every allocation in a plan shares its focus_plan_id, so
                 # a foreign key here would tie the whole set and hand ordering
                 # back to the store.
                 # snapshot_id precedes allocation_id: a snapshot CARRIES the
                 # allocation it froze, so the looser order made every
                 # snapshot sort under its allocation's id instead of its own.
                 "suggestion_id", "version_id",
                 "clinical_goal_id", "caregiver_goal_id",
                 "snapshot_id",
                 # 0.4D/E. Each record's OWN id, and all of them before the
                 # foreign keys below, because every alignment, gap, event and
                 # signal in one cycle shares `cycle_id` and `child_id`.
                 #
                 # Two orderings here are load-bearing and were wrong in a
                 # first pass:
                 #   revision_id BEFORE record_id — Revision.record_id is a
                 #     FOREIGN key, while ChildContextRecord and
                 #     AdaptationRecord own theirs.
                 #   record_id BEFORE intervention_id — AdaptationRecord
                 #     CARRIES intervention_id, so the looser order sorted
                 #     every adaptation under the intervention it cites.
                 "revision_id", "record_id",
                 "alignment_id", "gap_id", "ledger_id", "defer_id",
                 "intervention_id", "signal_id", "event_id",
                 # 0.4F/G. Own-ids again precede the foreign keys they
                 # share, and three orderings inside this block are
                 # load-bearing:
                 #   report_id BEFORE coding_summary_id — MonthEndReport
                 #     CARRIES coding_summary_id, so the looser order would
                 #     sort every report under the coding summary it cites.
                 #   action_id BEFORE review_id — ClinicalAction carries
                 #     review_id; TherapistReview owns it.
                 #   period_id BEFORE episode_id — RTMMonitoringPeriod owns
                 #     period_id and carries episode_id.
                 "report_id", "coding_summary_id", "summary_id",
                 "action_id", "review_id", "time_entry_id",
                 "interaction_id", "technology_id",
                 "period_id", "episode_id",
                 "allocation_id", "link_id", "cycle_id", "focus_plan_id",
                 "practice_id", "provider_id",
                 "caregiver_id", "child_id"):
        value = getattr(record, attr, None)
        if value:
            return str(value)
    raise AttributeError("record has no known identifier field")


#: The record's primary time field, tried in order. `snapshot_at` is listed
#: because `MonthlyGoalSnapshot` has no `created_at`: it is not created, it is
#: taken. An absent fallback here used to be an AttributeError at query time.
#: `captured_at` and `linked_at` are the same situation for 0.4D.
#: `generated_at` and `entered_at` are the 0.4F/G cases: a derived summary is
#: GENERATED and a time entry is ENTERED, and naming either `created_at` would
#: make the field mean something different from every other `created_at`.
_TIME_ATTRS = ("created_at", "occurred_at", "snapshot_at", "captured_at",
               "linked_at", "generated_at", "entered_at", "opened_at",
               "started_at")


def _record_time(record: Any) -> Any:
    for attr in _TIME_ATTRS:
        value = getattr(record, attr, None)
        if value is not None:
            return value
    raise AttributeError("record has no known timestamp field")


def _sorted(records: Sequence[Any]) -> List[Any]:
    return sorted(records, key=lambda r: (_record_time(r), _own_id(r)))


class _BaseRepo:
    """Shared document plumbing. One collection, one record type."""

    record_type: str = ""
    model: Type[Any] = object

    def __init__(self, store: DocumentStore) -> None:
        self._store = store

    @property
    def _collection(self) -> str:
        return collection_for(self.record_type)

    def _create(self, doc_id: str, record: Any) -> Any:
        try:
            self._store.create(self._collection, doc_id, encode(record))
        except DocumentStoreError:
            raise DuplicateRecord(f"record already exists: {doc_id}")
        return record

    def _get(self, doc_id: str) -> Any:
        document = self._store.get(self._collection, doc_id)
        if document is None:
            raise RecordNotFound(doc_id)
        return decode(self.model, document)

    def _set(self, doc_id: str, record: Any) -> Any:
        self._store.set(self._collection, doc_id, encode(record))
        return record

    def _query(self, field: str, value: Any) -> List[Any]:
        return _sorted([decode(self.model, data)
                        for _, data in self._store.query_equals(self._collection, field, value)])

    def _all(self) -> List[Any]:
        return _sorted([decode(self.model, data)
                        for _, data in self._store.list_all(self._collection)])


class FirestorePracticeRepository(_BaseRepo):
    record_type, model = "practice", Practice

    def create(self, practice: Practice) -> Practice:
        return self._create(practice.practice_id, practice)

    def get_by_id(self, practice_id: str) -> Practice:
        return self._get(practice_id)

    def update_status(self, practice_id: str, status: EntityStatus,
                      *, now: Optional[datetime] = None) -> Practice:
        return self._set(practice_id, self._get(practice_id).with_status(status, now=now))


class FirestoreProviderRepository(_BaseRepo):
    record_type, model = "provider", Provider

    def create(self, provider: Provider) -> Provider:
        return self._create(provider.provider_id, provider)

    def get_by_id(self, provider_id: str) -> Provider:
        return self._get(provider_id)

    def update_status(self, provider_id: str, status: EntityStatus,
                      *, now: Optional[datetime] = None) -> Provider:
        return self._set(provider_id, self._get(provider_id).with_status(status, now=now))

    def list_by_practice(self, practice_id: str) -> List[Provider]:
        return self._query("practice_id", practice_id)

    def get_by_auth_subject(self, auth_subject: str) -> Optional[Provider]:
        """Equality query on the auth subject — never on email.

        An empty subject returns None without querying: an unbound provider
        record stores `auth_subject` as null, and a blank lookup must not match
        it. Matching null to "" would hand an unclaimed clinician record to
        anyone whose token failed to carry a subject.
        """
        subject = (auth_subject or "").strip()
        if not subject:
            return None
        found = self._query("auth_subject", subject)
        if len(found) > 1:
            raise AmbiguousAuthSubject("auth subject matches more than one provider")
        return found[0] if found else None


class FirestoreCaregiverRepository(_BaseRepo):
    record_type, model = "caregiver", Caregiver

    def create(self, caregiver: Caregiver) -> Caregiver:
        return self._create(caregiver.caregiver_id, caregiver)

    def get_by_id(self, caregiver_id: str) -> Caregiver:
        return self._get(caregiver_id)

    def update_status(self, caregiver_id: str, status: EntityStatus,
                      *, now: Optional[datetime] = None) -> Caregiver:
        return self._set(caregiver_id, self._get(caregiver_id).with_status(status, now=now))

    def get_by_auth_subject(self, auth_subject: str) -> Optional[Caregiver]:
        subject = (auth_subject or "").strip()
        if not subject:
            return None
        found = self._query("auth_subject", subject)
        if len(found) > 1:
            raise AmbiguousAuthSubject("auth subject matches more than one caregiver")
        return found[0] if found else None


class FirestoreChildRepository(_BaseRepo):
    record_type, model = "child", Child

    def create(self, child: Child) -> Child:
        return self._create(child.child_id, child)

    def get_by_id(self, child_id: str) -> Child:
        return self._get(child_id)

    def update_status(self, child_id: str, status: EntityStatus,
                      *, now: Optional[datetime] = None) -> Child:
        return self._set(child_id, self._get(child_id).with_status(status, now=now))


class FirestoreCaregiverChildConnectionRepository(_BaseRepo):
    record_type, model = "caregiver_child_connection", CaregiverChildConnection

    def connect(self, connection: CaregiverChildConnection) -> CaregiverChildConnection:
        return self._create(connection.connection_id, connection)

    def get_by_id(self, connection_id: str) -> CaregiverChildConnection:
        return self._get(connection_id)

    def end_connection(self, connection_id: str, *,
                       status: ConnectionStatus = ConnectionStatus.ENDED,
                       now: Optional[datetime] = None) -> CaregiverChildConnection:
        ended = self._get(connection_id).end(status=status, now=now)
        return self._set(connection_id, ended)

    def list_children_for_caregiver(self, caregiver_id: str, *, include_ended: bool = False
                                    ) -> List[CaregiverChildConnection]:
        found = self._query("caregiver_id", caregiver_id)
        return found if include_ended else [c for c in found if c.is_active]

    def list_caregivers_for_child(self, child_id: str, *, include_ended: bool = False
                                  ) -> List[CaregiverChildConnection]:
        found = self._query("child_id", child_id)
        return found if include_ended else [c for c in found if c.is_active]


class FirestoreProviderChildConnectionRepository(_BaseRepo):
    record_type, model = "provider_child_connection", ProviderChildConnection

    def connect(self, connection: ProviderChildConnection) -> ProviderChildConnection:
        return self._create(connection.connection_id, connection)

    def get_by_id(self, connection_id: str) -> ProviderChildConnection:
        return self._get(connection_id)

    def activate(self, connection_id: str,
                 *, now: Optional[datetime] = None) -> ProviderChildConnection:
        return self._set(connection_id, self._get(connection_id).activate(now=now))

    def end_connection(self, connection_id: str, *,
                       status: ConnectionStatus = ConnectionStatus.ENDED,
                       now: Optional[datetime] = None) -> ProviderChildConnection:
        ended = self._get(connection_id).end(status=status, now=now)
        return self._set(connection_id, ended)

    def list_children_for_provider(self, provider_id: str, *, include_ended: bool = False
                                   ) -> List[ProviderChildConnection]:
        found = self._query("provider_id", provider_id)
        return found if include_ended else [c for c in found if c.is_active]

    def list_providers_for_child(self, child_id: str, *, include_ended: bool = False
                                 ) -> List[ProviderChildConnection]:
        found = self._query("child_id", child_id)
        return found if include_ended else [c for c in found if c.is_active]


class FirestoreAuditEventRepository(_BaseRepo):
    """Append-only audit log.

    There is no update and no delete — not as policy, but because no such
    method exists and the storage port offers no delete. An audit trail that
    can be rewritten by the system it audits is not evidence of anything.
    """

    record_type, model = "audit_event", AuditEvent

    def append(self, event: AuditEvent) -> AuditEvent:
        return self._create(event.event_id, event)

    def get_by_id(self, event_id: str) -> AuditEvent:
        return self._get(event_id)

    def list_for_child(self, child_id: str) -> List[AuditEvent]:
        return self._query("child_id", child_id)

    def list_for_actor(self, actor_application_id: str) -> List[AuditEvent]:
        return self._query("actor_application_id", actor_application_id)

    def list_all(self) -> List[AuditEvent]:
        return self._all()


class FirestoreRevisionRepository(_BaseRepo):
    """Append-only version chains. No update method exists, by design."""

    record_type, model = "revision", Revision

    def append(self, revision: Revision) -> Revision:
        return self._create(revision.revision_id, revision)

    def get_by_id(self, revision_id: str) -> Revision:
        return self._get(revision_id)

    def list_chain(self, record_id: str) -> List[Revision]:
        """Every version of one logical record, oldest first."""
        return sorted(self._query("record_id", record_id), key=lambda r: r.version)

    def seal(self, sealed: Revision) -> Revision:
        """Persist the DRAFT -> FINALIZED transition of one revision.

        Finalizing keeps the same `revision_id` and the same version number —
        sealing a draft is not a new version, it is the same version becoming
        immutable. So this is the one whole-document write the chain permits,
        and it is guarded in the direction that matters: the stored revision
        must currently be a DRAFT.

        The append-only property the chain actually needs is that a FINALIZED
        revision never changes and no version is ever removed. Both hold: this
        refuses to touch an already-finalized document, `amend` writes a NEW
        document with the next version, and nothing deletes. A draft being
        editable until it is sealed is the definition of a draft, not a hole
        in the guarantee.
        """
        existing = self._get(sealed.revision_id)
        if existing.is_finalized:
            raise ImmutableRecordError(
                f"revision {sealed.revision_id} is already finalized")
        if not sealed.is_finalized:
            raise ImmutableRecordError("seal() requires a finalized revision")
        if existing.version != sealed.version or existing.record_id != sealed.record_id:
            raise ImmutableRecordError("seal() must not change identity or version")
        return self._set(sealed.revision_id, sealed)


class FirestoreChildContextRepository(_BaseRepo):
    """The one mutable pilot record. Update is whole-document, never partial.

    `update` exists here where the entity repositories only expose
    `update_status`, because the record's current-revision pointer genuinely
    moves. It is still a whole-document write of a record the caller has
    already read, so a concurrent amendment is a lost update rather than a
    silently merged one — acceptable for a pilot with one writer per record,
    and recorded as carried debt rather than papered over with a transaction
    this phase does not need.
    """

    record_type, model = "child_context", ChildContextRecord

    def create(self, record: ChildContextRecord) -> ChildContextRecord:
        return self._create(record.record_id, record)

    def get_by_id(self, record_id: str) -> ChildContextRecord:
        return self._get(record_id)

    def update(self, record: ChildContextRecord) -> ChildContextRecord:
        return self._set(record.record_id, record)

    def list_for_child(self, child_id: str) -> List[ChildContextRecord]:
        return self._query("child_id", child_id)


class FirestoreIdentityClaimRepository(_BaseRepo):
    """Write-time uniqueness claims. Create-only, by design.

    There is no update and no delete. A claim is a record that one writer won
    a race for a deterministic document id; rewriting it would erase the proof
    and re-open the race.
    """

    record_type, model = "identity_claim", IdentityClaim

    def claim(self, claim: IdentityClaim) -> IdentityClaim:
        """Atomically win the claim, or raise DuplicateRecord.

        This single call IS the uniqueness enforcement: the document id is
        derived from the constraint key, so a competing writer computing the
        same key targets the same document and exactly one `create` survives.
        """
        return self._create(claim.claim_id, claim)

    def get_by_id(self, claim_id: str) -> IdentityClaim:
        return self._get(claim_id)

    def exists(self, claim_id: str) -> bool:
        try:
            self._get(claim_id)
            return True
        except RecordNotFound:
            return False

    def release(self, marker: IdentityClaim) -> IdentityClaim:
        """Hand a key back, opening the next generation.

        Also create-only and deterministic, so two concurrent releases of the
        same generation cannot both succeed and skip a generation.
        """
        if marker.record_kind is not ClaimRecordKind.RELEASE:
            raise ValueError("release() requires a release marker")
        return self._create(marker.claim_id, marker)

    def next_generation(self, kind: ClaimKind, key_digest: str) -> int:
        """The generation every contender must target for this key.

        Counts RELEASE markers only. A competitor winning a claim does not
        move this number, which is exactly why all contenders compute the same
        generation and collide on one document — see the module docstring in
        domain/identity_claims.py for the emulator-proven failure that an
        earlier claim-count version produced.
        """
        return sum(1 for record in self._query("key_digest", key_digest)
                   if record.record_kind is ClaimRecordKind.RELEASE
                   and record.kind is kind)

    def count_claims_for_key(self, key_digest: str) -> int:
        """Diagnostics/tests: acquisitions ever made for this key."""
        return sum(1 for record in self._query("key_digest", key_digest)
                   if record.record_kind is ClaimRecordKind.CLAIM)

    def list_for_key(self, key_digest: str) -> List[IdentityClaim]:
        return sorted(self._query("key_digest", key_digest),
                      key=lambda c: (c.generation, c.record_kind.value))


class FirestoreSourceSystemLinkRepository(_BaseRepo):
    """Canonical child <-> source-system identity bridges. No delete."""

    record_type, model = "source_system_link", SourceSystemLink

    def create(self, link: SourceSystemLink) -> SourceSystemLink:
        return self._create(link.link_id, link)

    def get_by_id(self, link_id: str) -> SourceSystemLink:
        return self._get(link_id)

    def update(self, link: SourceSystemLink) -> SourceSystemLink:
        """Whole-document write of an EXISTING link (end / lineage stamp)."""
        return self._set(link.link_id, link)

    def list_for_child(self, child_id: str, *, include_ended: bool = False
                       ) -> List[SourceSystemLink]:
        found = self._query("child_id", child_id)
        return found if include_ended else [x for x in found if x.is_active]

    def list_for_external_id(self, external_id: str, *, include_ended: bool = False
                             ) -> List[SourceSystemLink]:
        found = self._query("external_id", external_id)
        return found if include_ended else [x for x in found if x.is_active]


class FirestoreManagingClinicianRepository(_BaseRepo):
    """Managing-clinician assignment history. Append-only plus lineage stamps."""

    record_type, model = "managing_clinician", ManagingClinicianAssignment

    def create(self, assignment: ManagingClinicianAssignment) -> ManagingClinicianAssignment:
        return self._create(assignment.assignment_id, assignment)

    def get_by_id(self, assignment_id: str) -> ManagingClinicianAssignment:
        return self._get(assignment_id)

    def update(self, assignment: ManagingClinicianAssignment) -> ManagingClinicianAssignment:
        return self._set(assignment.assignment_id, assignment)

    def list_for_child(self, child_id: str, *, include_ended: bool = False
                       ) -> List[ManagingClinicianAssignment]:
        found = self._query("child_id", child_id)
        return found if include_ended else [x for x in found if x.is_active]


class FirestoreGoalSuggestionRepository(_BaseRepo):
    """Genex-authored candidates. Create plus a status stamp. No delete.

    A declined suggestion is kept, not removed. "What did Genex propose that
    the clinician rejected, and why?" is a question the pilot exists to answer,
    and it is unanswerable if declining erases the row.
    """

    record_type, model = "goal_suggestion", GoalSuggestion

    def create(self, suggestion: GoalSuggestion) -> GoalSuggestion:
        return self._create(suggestion.suggestion_id, suggestion)

    def get_by_id(self, suggestion_id: str) -> GoalSuggestion:
        return self._get(suggestion_id)

    def update(self, suggestion: GoalSuggestion) -> GoalSuggestion:
        """Whole-document write of an EXISTING suggestion (status stamp)."""
        return self._set(suggestion.suggestion_id, suggestion)

    def list_for_child(self, child_id: str) -> List[GoalSuggestion]:
        return self._query("child_id", child_id)

    def list_for_cycle(self, child_id: str, cycle_month: str) -> List[GoalSuggestion]:
        """One child-month's offer.

        Filtered in Python after a single-field query because `DocumentStore`
        exposes equality on ONE field. A composite query would need a
        Firestore index, and the port deliberately does not promise one — the
        alternative is a store-specific method the fake could not honour.
        """
        return [s for s in self._query("child_id", child_id)
                if s.cycle_month == cycle_month]


class FirestoreGoalVersionRepository(_BaseRepo):
    """Immutable wording history. Append-only: there is no update method."""

    record_type, model = "goal_version", GoalVersion

    def append(self, version: GoalVersion) -> GoalVersion:
        return self._create(version.version_id, version)

    def get_by_id(self, version_id: str) -> GoalVersion:
        return self._get(version_id)

    def list_chain(self, goal_id: str) -> List[GoalVersion]:
        """Every wording of one goal, oldest first."""
        return sorted(self._query("goal_id", goal_id), key=lambda v: v.version_number)

    def latest_version_number(self, goal_id: str) -> int:
        chain = self.list_chain(goal_id)
        return chain[-1].version_number if chain else 0


class FirestoreClinicalGoalRepository(_BaseRepo):
    """Clinician-approved goals. Separate collection from caregiver goals."""

    record_type, model = "clinical_goal", ClinicalGoal

    def create(self, goal: ClinicalGoal) -> ClinicalGoal:
        return self._create(goal.clinical_goal_id, goal)

    def get_by_id(self, clinical_goal_id: str) -> ClinicalGoal:
        return self._get(clinical_goal_id)

    def update(self, goal: ClinicalGoal) -> ClinicalGoal:
        return self._set(goal.clinical_goal_id, goal)

    def list_for_child(self, child_id: str, *, include_closed: bool = False
                       ) -> List[ClinicalGoal]:
        found = self._query("child_id", child_id)
        return found if include_closed else [g for g in found if g.is_active]


class FirestoreCaregiverGoalRepository(_BaseRepo):
    """Caregiver-approved goals. NEVER RTM-eligible, and never co-located."""

    record_type, model = "caregiver_goal", CaregiverApprovedGoal

    def create(self, goal: CaregiverApprovedGoal) -> CaregiverApprovedGoal:
        return self._create(goal.caregiver_goal_id, goal)

    def get_by_id(self, caregiver_goal_id: str) -> CaregiverApprovedGoal:
        return self._get(caregiver_goal_id)

    def update(self, goal: CaregiverApprovedGoal) -> CaregiverApprovedGoal:
        return self._set(goal.caregiver_goal_id, goal)

    def list_for_child(self, child_id: str, *, include_closed: bool = False
                       ) -> List[CaregiverApprovedGoal]:
        found = self._query("child_id", child_id)
        return found if include_closed else [g for g in found if g.is_active]


class FirestoreMonthlyFocusPlanRepository(_BaseRepo):
    """One month of direction per child. No delete."""

    record_type, model = "monthly_focus_plan", MonthlyFocusPlan

    def create(self, plan: MonthlyFocusPlan) -> MonthlyFocusPlan:
        return self._create(plan.focus_plan_id, plan)

    def get_by_id(self, focus_plan_id: str) -> MonthlyFocusPlan:
        return self._get(focus_plan_id)

    def update(self, plan: MonthlyFocusPlan) -> MonthlyFocusPlan:
        return self._set(plan.focus_plan_id, plan)

    def list_for_child(self, child_id: str) -> List[MonthlyFocusPlan]:
        return self._query("child_id", child_id)

    def list_for_cycle(self, child_id: str, cycle_month: str) -> List[MonthlyFocusPlan]:
        return [p for p in self._query("child_id", child_id)
                if p.cycle_month == cycle_month]

    def active_for_cycle(self, child_id: str,
                         cycle_month: str) -> Optional[MonthlyFocusPlan]:
        """The ACTIVE plan for one child-month, if any.

        Raises rather than picking when two are active. Uniqueness is enforced
        at write time by the activation claim, so two active plans means the
        claim mechanism was bypassed — and the 0.3 auth-subject defect is
        precisely what silently returning `found[0]` looks like.
        """
        active = [p for p in self.list_for_cycle(child_id, cycle_month) if p.is_active]
        if len(active) > 1:
            raise AmbiguousRecordState(
                "more than one active focus plan for this child-month")
        return active[0] if active else None


class FirestoreMonthlyGoalAllocationRepository(_BaseRepo):
    """Append-only allocation history. Update only stamps lineage/status."""

    record_type, model = "monthly_goal_allocation", MonthlyGoalAllocation

    def create(self, allocation: MonthlyGoalAllocation) -> MonthlyGoalAllocation:
        return self._create(allocation.allocation_id, allocation)

    def get_by_id(self, allocation_id: str) -> MonthlyGoalAllocation:
        return self._get(allocation_id)

    def update(self, allocation: MonthlyGoalAllocation) -> MonthlyGoalAllocation:
        return self._set(allocation.allocation_id, allocation)

    def list_for_plan(self, focus_plan_id: str, *, include_inactive: bool = False
                      ) -> List[MonthlyGoalAllocation]:
        found = self._query("focus_plan_id", focus_plan_id)
        rows = found if include_inactive else [a for a in found if a.is_active]
        return sorted(rows, key=lambda a: (a.priority_rank, a.allocation_id))


class FirestoreMonthlyGoalSnapshotRepository(_BaseRepo):
    """What the month was working toward. Create-only — never updated."""

    record_type, model = "monthly_goal_snapshot", MonthlyGoalSnapshot

    def create(self, snapshot: MonthlyGoalSnapshot) -> MonthlyGoalSnapshot:
        return self._create(snapshot.snapshot_id, snapshot)

    def get_by_id(self, snapshot_id: str) -> MonthlyGoalSnapshot:
        return self._get(snapshot_id)

    def list_for_plan(self, focus_plan_id: str) -> List[MonthlyGoalSnapshot]:
        return sorted(self._query("focus_plan_id", focus_plan_id),
                      key=lambda s: (s.priority_rank, s.snapshot_id))


class FirestoreWeeklyCycleRepository(_BaseRepo):
    """The monthly layer's weekly cycles. No delete."""

    record_type, model = "weekly_cycle", WeeklyCycle

    def create(self, cycle: WeeklyCycle) -> WeeklyCycle:
        return self._create(cycle.cycle_id, cycle)

    def get_by_id(self, cycle_id: str) -> WeeklyCycle:
        return self._get(cycle_id)

    def update(self, cycle: WeeklyCycle) -> WeeklyCycle:
        """Whole-document write of an EXISTING cycle (release, adaptation)."""
        return self._set(cycle.cycle_id, cycle)

    def list_for_plan(self, focus_plan_id: str) -> List[WeeklyCycle]:
        return sorted(self._query("owning_focus_plan_id", focus_plan_id),
                      key=lambda c: (c.sequence_in_month, c.cycle_id))

    def list_for_child(self, child_id: str) -> List[WeeklyCycle]:
        return self._query("child_id", child_id)


class FirestoreWeeklyPlanLinkRepository(_BaseRepo):
    """Bindings to source-system plans. Create-only."""

    record_type, model = "weekly_plan_link", WeeklyPlanLink

    def create(self, link: WeeklyPlanLink) -> WeeklyPlanLink:
        return self._create(link.link_id, link)

    def get_by_id(self, link_id: str) -> WeeklyPlanLink:
        return self._get(link_id)

    def list_for_cycle(self, cycle_id: str) -> List[WeeklyPlanLink]:
        return self._query("cycle_id", cycle_id)


class FirestoreWeeklyPlanSnapshotRepository(_BaseRepo):
    """What the parent-facing plan contained. Create-only, by design.

    There is no `update` and no `set`. A snapshot that could be rewritten
    answers nothing — it exists precisely because Parent's customization
    overlay is unversioned.
    """

    record_type, model = "weekly_plan_snapshot", WeeklyPlanSnapshot

    def create(self, snapshot: WeeklyPlanSnapshot) -> WeeklyPlanSnapshot:
        return self._create(snapshot.snapshot_id, snapshot)

    def get_by_id(self, snapshot_id: str) -> WeeklyPlanSnapshot:
        return self._get(snapshot_id)

    def list_for_cycle(self, cycle_id: str) -> List[WeeklyPlanSnapshot]:
        return self._query("cycle_id", cycle_id)


class FirestoreAlignmentRepository(_BaseRepo):
    """Activity-to-goal attribution. Create-only: alignments are immutable.

    A past cycle's attribution must stay readable after a clinician
    reprioritises (section 26), so there is no update path.
    """

    record_type, model = "activity_goal_alignment", ActivityGoalAlignment

    def create(self, alignment: ActivityGoalAlignment) -> ActivityGoalAlignment:
        return self._create(alignment.alignment_id, alignment)

    def get_by_id(self, alignment_id: str) -> ActivityGoalAlignment:
        return self._get(alignment_id)

    def list_for_cycle(self, cycle_id: str) -> List[ActivityGoalAlignment]:
        return self._query("cycle_id", cycle_id)

    def list_for_child(self, child_id: str) -> List[ActivityGoalAlignment]:
        return self._query("child_id", child_id)


class FirestoreCoverageGapRepository(_BaseRepo):
    """Planner conditions. Create-only."""

    record_type, model = "coverage_gap", CoverageGap

    def create(self, gap: CoverageGap) -> CoverageGap:
        return self._create(gap.gap_id, gap)

    def get_by_id(self, gap_id: str) -> CoverageGap:
        return self._get(gap_id)

    def list_for_cycle(self, cycle_id: str) -> List[CoverageGap]:
        return self._query("cycle_id", cycle_id)


class FirestoreCapacityLedgerRepository(_BaseRepo):
    """Family capacity per cycle. Updated as activity is placed."""

    record_type, model = "capacity_ledger", CapacityLedger

    def create(self, ledger: CapacityLedger) -> CapacityLedger:
        return self._create(ledger.ledger_id, ledger)

    def get_by_id(self, ledger_id: str) -> CapacityLedger:
        return self._get(ledger_id)

    def update(self, ledger: CapacityLedger) -> CapacityLedger:
        return self._set(ledger.ledger_id, ledger)

    def list_for_cycle(self, cycle_id: str) -> List[CapacityLedger]:
        return self._query("cycle_id", cycle_id)


class FirestoreObservationEventRepository(_BaseRepo):
    """Caregiver-recorded attempts. Create-only: evidence is not edited."""

    record_type, model = "observation_event", ObservationEvent

    def create(self, event: ObservationEvent) -> ObservationEvent:
        return self._create(event.event_id, event)

    def get_by_id(self, event_id: str) -> ObservationEvent:
        return self._get(event_id)

    def list_for_cycle(self, cycle_id: str) -> List[ObservationEvent]:
        return self._query("owning_cycle_id", cycle_id)

    def list_for_child(self, child_id: str) -> List[ObservationEvent]:
        return self._query("child_id", child_id)

    def list_for_attribution_month(self, child_id: str, month: str
                                   ) -> List[ObservationEvent]:
        """Events counting toward a calendar month, by LOCAL date.

        Filtered on the event's own `attribution_month`, never on its cycle:
        a cycle spanning Oct 26 – Nov 1 contributes its Nov 1 attempt to
        November, and filtering by cycle would put it in October.
        """
        return [e for e in self._query("child_id", child_id)
                if e.attribution_month == month]


class FirestoreCustomizationSignalRepository(_BaseRepo):
    """Family plan edits. Create-only. Never child performance."""

    record_type, model = "customization_signal", ParentCustomizationSignal

    def create(self, signal: ParentCustomizationSignal) -> ParentCustomizationSignal:
        return self._create(signal.signal_id, signal)

    def get_by_id(self, signal_id: str) -> ParentCustomizationSignal:
        return self._get(signal_id)

    def list_for_cycle(self, cycle_id: str) -> List[ParentCustomizationSignal]:
        return self._query("cycle_id", cycle_id)


class FirestoreDeferRecordRepository(_BaseRepo):
    """Save for Later. Update exists ONLY to stamp a clinician override."""

    record_type, model = "defer_record", DeferRecord

    def create(self, record: DeferRecord) -> DeferRecord:
        return self._create(record.defer_id, record)

    def get_by_id(self, defer_id: str) -> DeferRecord:
        return self._get(defer_id)

    def update(self, record: DeferRecord) -> DeferRecord:
        return self._set(record.defer_id, record)

    def list_for_child(self, child_id: str) -> List[DeferRecord]:
        return self._query("child_id", child_id)

    def list_for_cycle(self, cycle_id: str) -> List[DeferRecord]:
        return self._query("from_cycle_id", cycle_id)


class FirestoreTherapistInterventionRepository(_BaseRepo):
    """Clinician planning decisions. Create-only."""

    record_type, model = "therapist_intervention", TherapistIntervention

    def create(self, intervention: TherapistIntervention) -> TherapistIntervention:
        return self._create(intervention.intervention_id, intervention)

    def get_by_id(self, intervention_id: str) -> TherapistIntervention:
        return self._get(intervention_id)

    def list_for_cycle(self, cycle_id: str) -> List[TherapistIntervention]:
        return self._query("cycle_id", cycle_id)

    def list_for_child(self, child_id: str) -> List[TherapistIntervention]:
        return self._query("child_id", child_id)


class FirestoreAdaptationRecordRepository(_BaseRepo):
    """Why Week N+1 differed from Week N. Create-only."""

    record_type, model = "adaptation_record", AdaptationRecord

    def create(self, record: AdaptationRecord) -> AdaptationRecord:
        return self._create(record.record_id, record)

    def get_by_id(self, record_id: str) -> AdaptationRecord:
        return self._get(record_id)

    def list_for_plan(self, focus_plan_id: str) -> List[AdaptationRecord]:
        return self._query("focus_plan_id", focus_plan_id)

    def list_for_child(self, child_id: str) -> List[AdaptationRecord]:
        return self._query("child_id", child_id)


class _PeriodScopedRepo(_BaseRepo):
    """Records belonging to exactly one monitoring period."""

    def list_for_period(self, period_id: str):
        return self._query("period_id", period_id)


class FirestoreRTMEpisodeRepository(_BaseRepo):
    """Clinical episodes. Append plus a terminal stamp. No delete."""

    record_type, model = "rtm_episode", RTMEpisode

    def create(self, episode): return self._create(episode.episode_id, episode)

    def get_by_id(self, episode_id): return self._get(episode_id)

    def update(self, episode):
        """Whole-document write of an EXISTING episode (close/lineage)."""
        return self._set(episode.episode_id, episode)

    def list_for_child(self, child_id, *, include_closed: bool = True):
        found = self._query("child_id", child_id)
        return found if include_closed else [e for e in found if e.is_open]


class FirestoreRTMPeriodRepository(_BaseRepo):
    """Monitoring periods. Finalizing stamps the row; nothing deletes."""

    record_type, model = "rtm_period", RTMMonitoringPeriod

    def create(self, period): return self._create(period.period_id, period)

    def get_by_id(self, period_id): return self._get(period_id)

    def update(self, period): return self._set(period.period_id, period)

    def list_for_episode(self, episode_id):
        return self._query("episode_id", episode_id)

    def list_for_child(self, child_id):
        return self._query("child_id", child_id)


class FirestoreRTMTechnologyRepository(_BaseRepo):
    """Technology declarations, append-only with revision lineage."""

    record_type, model = "rtm_technology", RTMTechnology

    def create(self, technology):
        return self._create(technology.technology_id, technology)

    def get_by_id(self, technology_id): return self._get(technology_id)

    def update(self, technology):
        return self._set(technology.technology_id, technology)

    def list_for_episode(self, episode_id):
        return self._query("episode_id", episode_id)


class FirestoreTherapistReviewRepository(_PeriodScopedRepo):
    """Clinician interpretation. Create-only: there is no update method."""

    record_type, model = "therapist_review", TherapistReview

    def create(self, review): return self._create(review.review_id, review)

    def get_by_id(self, review_id): return self._get(review_id)


class FirestoreClinicalActionRepository(_PeriodScopedRepo):
    """Documented clinician decisions. Create-only."""

    record_type, model = "clinical_action", ClinicalAction

    def create(self, action): return self._create(action.action_id, action)

    def get_by_id(self, action_id): return self._get(action_id)

    def list_for_review(self, review_id):
        return self._query("review_id", review_id)


class FirestoreTimeEntryRepository(_PeriodScopedRepo):
    """Manually entered minutes.

    `update` exists ONLY to stamp a supersession pointer on a predecessor.
    Minutes are never edited in place: a correction is a new row and the
    original is retained, so the monthly total can exclude it rather than
    pretend it never existed.
    """

    record_type, model = "time_entry", TimeEntry

    def create(self, entry): return self._create(entry.time_entry_id, entry)

    def get_by_id(self, time_entry_id): return self._get(time_entry_id)

    def update(self, entry): return self._set(entry.time_entry_id, entry)

    def list_current_for_period(self, period_id):
        return [e for e in self.list_for_period(period_id) if e.is_current]


class FirestoreSynchronousInteractionRepository(_PeriodScopedRepo):
    """Real-time contacts. Create-only."""

    record_type, model = "synchronous_interaction", SynchronousInteraction

    def create(self, interaction):
        return self._create(interaction.interaction_id, interaction)

    def get_by_id(self, interaction_id): return self._get(interaction_id)


class FirestoreEvidenceSummaryRepository(_PeriodScopedRepo):
    """Derived monthly summaries. Create-only — regenerating adds a row.

    No update, deliberately. A clinician who read a summary must be able to
    find the one they read.
    """

    record_type, model = "rtm_evidence_summary", RTMEvidenceSummary

    def create(self, summary): return self._create(summary.summary_id, summary)

    def get_by_id(self, summary_id): return self._get(summary_id)

    def latest_for_period(self, period_id):
        found = self.list_for_period(period_id)
        return found[-1] if found else None


class FirestoreCodingSummaryRepository(_PeriodScopedRepo):
    """Coding assistance.

    `update` exists ONLY so a clinician decision can be stamped beside the
    generated candidates. `with_decision` moves only the decision fields and
    a test asserts the rest is byte-identical afterwards.
    """

    record_type, model = "coding_assistance_summary", CodingAssistanceSummary

    def create(self, summary):
        return self._create(summary.coding_summary_id, summary)

    def get_by_id(self, coding_summary_id): return self._get(coding_summary_id)

    def update(self, summary):
        return self._set(summary.coding_summary_id, summary)

    def latest_for_period(self, period_id):
        found = self.list_for_period(period_id)
        return found[-1] if found else None


class FirestoreMonthEndReportRepository(_PeriodScopedRepo):
    """Month-end reports. Append-only chain with amendment lineage.

    `update` stamps the forward pointer on a superseded predecessor. A
    finalized report's CONTENT is never rewritten — an amendment is a new row
    at the next version.
    """

    record_type, model = "month_end_report", MonthEndReport

    def create(self, report): return self._create(report.report_id, report)

    def get_by_id(self, report_id): return self._get(report_id)

    def update(self, report): return self._set(report.report_id, report)

    def current_for_period(self, period_id):
        current = [r for r in self.list_for_period(period_id) if r.is_current]
        return current[-1] if current else None

    def chain_for_period(self, period_id):
        return sorted(self.list_for_period(period_id), key=lambda r: r.version)


class FirestoreRepositories:
    """All repositories over one document store — the production composition."""

    def __init__(self, store: DocumentStore) -> None:
        self.store = store
        self.practices = FirestorePracticeRepository(store)
        self.providers = FirestoreProviderRepository(store)
        self.caregivers = FirestoreCaregiverRepository(store)
        self.children = FirestoreChildRepository(store)
        self.caregiver_child = FirestoreCaregiverChildConnectionRepository(store)
        self.provider_child = FirestoreProviderChildConnectionRepository(store)
        self.audit_events = FirestoreAuditEventRepository(store)
        self.child_contexts = FirestoreChildContextRepository(store)
        self.identity_claims = FirestoreIdentityClaimRepository(store)
        self.source_links = FirestoreSourceSystemLinkRepository(store)
        self.managing_clinicians = FirestoreManagingClinicianRepository(store)
        self.goal_suggestions = FirestoreGoalSuggestionRepository(store)
        self.goal_versions = FirestoreGoalVersionRepository(store)
        self.clinical_goals = FirestoreClinicalGoalRepository(store)
        self.caregiver_goals = FirestoreCaregiverGoalRepository(store)
        self.focus_plans = FirestoreMonthlyFocusPlanRepository(store)
        self.goal_allocations = FirestoreMonthlyGoalAllocationRepository(store)
        self.goal_snapshots = FirestoreMonthlyGoalSnapshotRepository(store)
        self.weekly_cycles = FirestoreWeeklyCycleRepository(store)
        self.weekly_plan_links = FirestoreWeeklyPlanLinkRepository(store)
        self.weekly_plan_snapshots = FirestoreWeeklyPlanSnapshotRepository(store)
        self.alignments = FirestoreAlignmentRepository(store)
        self.coverage_gaps = FirestoreCoverageGapRepository(store)
        self.capacity_ledgers = FirestoreCapacityLedgerRepository(store)
        self.observation_events = FirestoreObservationEventRepository(store)
        self.customization_signals = FirestoreCustomizationSignalRepository(store)
        self.defer_records = FirestoreDeferRecordRepository(store)
        self.interventions = FirestoreTherapistInterventionRepository(store)
        self.adaptation_records = FirestoreAdaptationRecordRepository(store)
        self.rtm_episodes = FirestoreRTMEpisodeRepository(store)
        self.rtm_periods = FirestoreRTMPeriodRepository(store)
        self.rtm_technologies = FirestoreRTMTechnologyRepository(store)
        self.therapist_reviews = FirestoreTherapistReviewRepository(store)
        self.clinical_actions = FirestoreClinicalActionRepository(store)
        self.time_entries = FirestoreTimeEntryRepository(store)
        self.synchronous_interactions = \
            FirestoreSynchronousInteractionRepository(store)
        self.evidence_summaries = FirestoreEvidenceSummaryRepository(store)
        self.coding_summaries = FirestoreCodingSummaryRepository(store)
        self.month_end_reports = FirestoreMonthEndReportRepository(store)
        self.revisions = FirestoreRevisionRepository(store)
