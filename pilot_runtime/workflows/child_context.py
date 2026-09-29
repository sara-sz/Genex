"""pilot_runtime/workflows/child_context.py — the fictional pilot workflows.

One child-scoped record, four operations, two actor roles. Its purpose is to
exercise the whole stack against real adapters: verified identity, relationship
authorization, Firestore persistence, audit emission and revision history.

## Every operation begins with the same guard

`_authorize` is the only way into any operation, and it returns the 0.2
`AccessDecision` unchanged. No operation reads or writes anything before that
decision allows it — which is the ordering property the 0.2 counting-repository
tests pin, now extended to real workflows rather than a bare access check.

Caregiver and provider reach the same operations by different relationships.
That is deliberate: the authorization layer already distinguishes them, and
adding a second, role-specific permission model here would be a second place
for the two to disagree.

## Audit is emitted by the workflow, not by the caller

Each operation records its own event through `AuditRecorder`, so a transport
cannot perform an operation without auditing it. Events carry identifiers,
role, outcome and correlation id — never the record's content, which does not
exist in this system to be copied.

## History is the revision chain's job

`finalize` and `amend` delegate to `pilot_backend.revision.records`. This
module never mutates a finalized revision and never implements its own
versioning; it writes a new revision and moves the record's pointer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from pilot_backend.audit.events import AuditAction, AuditResult
from pilot_backend.authz.decisions import AccessDecision, HTTP_FORBIDDEN, HTTP_OK
from pilot_backend.authz.policy import authenticate_and_authorize_child
from pilot_backend.domain.child_context import ChildContextRecord
from pilot_backend.repository.interface import RecordNotFound
from pilot_backend.revision.records import (
    ImmutableRecordError,
    RecordState,
    Revision,
    amend,
    finalize,
    latest,
    start_draft,
)

#: Resource type recorded on every audit event this workflow emits.
RESOURCE_TYPE = "child_context"


@dataclass(frozen=True)
class WorkflowOutcome:
    """What happened, and what HTTP status it corresponds to.

    Carries no record content — only identifiers, versions and state — so a
    transport cannot accidentally serialise something it should not.
    """

    status_code: int
    ok: bool
    child_id: Optional[str] = None
    record_id: Optional[str] = None
    revision_id: Optional[str] = None
    version: int = 0
    state: Optional[str] = None
    detail: str = ""

    @staticmethod
    def denied(decision: AccessDecision) -> "WorkflowOutcome":
        return WorkflowOutcome(
            status_code=decision.status_code, ok=False,
            child_id=decision.child_id, detail=decision.public_reason)

    @staticmethod
    def conflict(child_id: str, detail: str) -> "WorkflowOutcome":
        # 409: the caller is permitted, the record state forbids the change.
        return WorkflowOutcome(status_code=409, ok=False,
                               child_id=child_id, detail=detail)


class ChildContextService:
    """The fictional child-context workflow, over whatever adapters it is given."""

    def __init__(self, *, verifier, repos, recorder, now=None) -> None:
        self._verifier = verifier
        self._repos = repos
        self._recorder = recorder
        #: Injectable clock for deterministic tests. There is no request
        #: parameter that reaches it.
        self._now = now

    def _stamp(self) -> datetime:
        return self._now() if self._now else datetime.now(timezone.utc)

    # -- the single entry gate ---------------------------------------------

    def _authorize(self, bearer: Optional[str], child_id: str,
                   request_id: str) -> AccessDecision:
        decision = authenticate_and_authorize_child(
            bearer, child_id, verifier=self._verifier, repos=self._repos)
        if not decision.allowed:
            # A refusal is the more interesting audit event.
            self._recorder.record_access_decision(
                decision, resource_type=RESOURCE_TYPE, request_id=request_id)
        return decision

    def _audit(self, action: AuditAction, result: AuditResult, decision: AccessDecision,
               *, record_id: Optional[str], request_id: str, **metadata) -> None:
        self._recorder.record_action(
            action, result, RESOURCE_TYPE,
            resource_id=record_id,
            child_id=decision.child_id,
            principal=decision.principal,
            request_id=request_id,
            metadata=metadata,
        )

    # -- operations ---------------------------------------------------------

    def read(self, bearer: Optional[str], child_id: str, *,
             request_id: str = "") -> WorkflowOutcome:
        """Read the current record pointer. No content is returned or stored."""
        decision = self._authorize(bearer, child_id, request_id)
        if not decision.allowed:
            return WorkflowOutcome.denied(decision)

        record = self._current_record(child_id)
        if record is None:
            self._audit(AuditAction.CHILD_ACCESS_GRANTED, AuditResult.SUCCESS,
                        decision, record_id=None, request_id=request_id)
            return WorkflowOutcome(status_code=HTTP_OK, ok=True,
                                   child_id=child_id, detail="no record")

        self._audit(AuditAction.CHILD_ACCESS_GRANTED, AuditResult.SUCCESS,
                    decision, record_id=record.record_id, request_id=request_id,
                    record_version=record.current_version)
        return self._outcome_for(record)

    def create_draft(self, bearer: Optional[str], child_id: str, *,
                     content_ref: str = "", request_id: str = "") -> WorkflowOutcome:
        """Create the record and its first, editable revision."""
        decision = self._authorize(bearer, child_id, request_id)
        if not decision.allowed:
            return WorkflowOutcome.denied(decision)

        if self._current_record(child_id) is not None:
            return WorkflowOutcome.conflict(child_id, "record already exists")

        principal = decision.principal
        stamp = self._stamp()
        record = ChildContextRecord.create(
            child_id, actor_id=principal.application_id,
            actor_role=principal.role, content_ref=content_ref, now=stamp)

        draft = start_draft(record.record_id,
                            actor_application_id=principal.application_id,
                            actor_role=principal.role,
                            content_ref=content_ref, now=stamp)
        self._repos.revisions.append(draft)
        record = record.with_current_revision(
            draft.revision_id, draft.version, content_ref=content_ref,
            actor_role=principal.role, now=stamp)
        self._repos.child_contexts.create(record)

        self._audit(AuditAction.CHILD_CREATED, AuditResult.SUCCESS, decision,
                    record_id=record.record_id, request_id=request_id,
                    record_version=draft.version, revision_id=draft.revision_id)
        return self._outcome_for(record, state=draft.state.value)

    def finalize_current(self, bearer: Optional[str], child_id: str, *,
                         request_id: str = "") -> WorkflowOutcome:
        """Seal the current draft. After this there is no silent overwrite."""
        decision = self._authorize(bearer, child_id, request_id)
        if not decision.allowed:
            return WorkflowOutcome.denied(decision)

        record, current = self._current_pair(child_id)
        if record is None or current is None:
            return WorkflowOutcome.conflict(child_id, "no record to finalize")

        try:
            sealed = finalize(current, now=self._stamp())
        except ImmutableRecordError:
            return WorkflowOutcome.conflict(child_id, "record is already finalized")

        # Sealing keeps the same revision id and version — it is this version
        # becoming immutable, not a new one — so it goes through the guarded
        # `seal` transition rather than `append`, which would collide on the
        # document id. `seal` refuses if the stored revision is already
        # finalized, so the no-silent-overwrite rule holds at the store too.
        self._repos.revisions.seal(sealed)
        record = record.with_current_revision(
            sealed.revision_id, sealed.version,
            actor_role=decision.principal.role, now=self._stamp())
        self._repos.child_contexts.update(record)

        self._audit(AuditAction.RECORD_FINALIZED, AuditResult.SUCCESS, decision,
                    record_id=record.record_id, request_id=request_id,
                    record_version=sealed.version, revision_id=sealed.revision_id)
        return self._outcome_for(record, state=sealed.state.value)

    def amend_current(self, bearer: Optional[str], child_id: str, *, reason: str,
                      content_ref: str = "", request_id: str = "") -> WorkflowOutcome:
        """Amend a finalized record: a new version, with the old one retained."""
        decision = self._authorize(bearer, child_id, request_id)
        if not decision.allowed:
            return WorkflowOutcome.denied(decision)

        record, current = self._current_pair(child_id)
        if record is None or current is None:
            return WorkflowOutcome.conflict(child_id, "no record to amend")

        try:
            next_revision = amend(
                current,
                actor_application_id=decision.principal.application_id,
                actor_role=decision.principal.role,
                reason=reason, content_ref=content_ref, now=self._stamp())
        except ImmutableRecordError as exc:
            # Either not finalized, or no reason given. Both are the caller's
            # state problem, not an authorization problem.
            return WorkflowOutcome.conflict(child_id, str(exc))

        self._repos.revisions.append(next_revision)
        record = record.with_current_revision(
            next_revision.revision_id, next_revision.version,
            content_ref=content_ref, actor_role=decision.principal.role,
            now=self._stamp())
        self._repos.child_contexts.update(record)

        self._audit(AuditAction.RECORD_AMENDED, AuditResult.SUCCESS, decision,
                    record_id=record.record_id, request_id=request_id,
                    record_version=next_revision.version,
                    revision_id=next_revision.revision_id)
        return self._outcome_for(record, state=next_revision.state.value)

    def history(self, bearer: Optional[str], child_id: str, *,
                request_id: str = "") -> List[Revision]:
        """Every version, oldest first. Authorized like any other read."""
        decision = self._authorize(bearer, child_id, request_id)
        if not decision.allowed:
            return []
        record = self._current_record(child_id)
        if record is None:
            return []
        self._audit(AuditAction.CHILD_ACCESS_GRANTED, AuditResult.SUCCESS, decision,
                    record_id=record.record_id, request_id=request_id)
        return self._repos.revisions.list_chain(record.record_id)

    # -- helpers ------------------------------------------------------------

    def _current_record(self, child_id: str) -> Optional[ChildContextRecord]:
        found = self._repos.child_contexts.list_for_child(child_id)
        return found[0] if found else None

    def _current_pair(self, child_id: str):
        record = self._current_record(child_id)
        if record is None:
            return None, None
        chain = self._repos.revisions.list_chain(record.record_id)
        return record, latest(chain)

    @staticmethod
    def _outcome_for(record: ChildContextRecord,
                     state: Optional[str] = None) -> WorkflowOutcome:
        return WorkflowOutcome(
            status_code=HTTP_OK, ok=True,
            child_id=record.child_id, record_id=record.record_id,
            revision_id=record.current_revision_id,
            version=record.current_version, state=state)
