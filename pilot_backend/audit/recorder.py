"""pilot_backend/audit/recorder.py — turning access decisions into audit events.

Imported directly (`from pilot_backend.audit.recorder import AuditRecorder`)
rather than re-exported from the package `__init__`, to keep the audit package
importable without pulling in persistence. See `audit/__init__.py`.

## Why the recorder owns the mapping

Every call site that checks authorization would otherwise decide for itself
what to record, and the one that forgets is invisible — a missing audit event
looks exactly like an event that never happened. `record_access_decision` takes
the `AccessDecision` the authorization layer already produced and derives the
whole event from it, so recording cannot drift from the decision it describes.

## Failures are recorded too

A denied request is the more interesting audit event. Both outcomes are
recorded, with the denial reason carried as allowlisted metadata rather than
free text.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, Optional

from ..authz.decisions import AccessDecision
from .events import AuditAction, AuditEvent, AuditResult


class AuditRecorder:
    """Writes audit events to the append-only audit repository."""

    def __init__(self, repository, *, environment: str = "") -> None:
        self._repository = repository
        self._environment = environment

    def record(self, event: AuditEvent) -> AuditEvent:
        return self._repository.append(event)

    def record_access_decision(self, decision: AccessDecision, *,
                               resource_type: str = "child",
                               request_id: str = "",
                               now: Optional[datetime] = None) -> AuditEvent:
        """Derive and persist the audit event for one authorization outcome."""
        principal = decision.principal
        metadata: dict = {"http_status": decision.status_code}
        if self._environment:
            metadata["environment"] = self._environment
        if decision.denial is not None:
            metadata["denial_reason"] = decision.denial.value

        if decision.allowed:
            action, result = AuditAction.CHILD_ACCESS_GRANTED, AuditResult.SUCCESS
        elif decision.status_code == 401:
            action, result = AuditAction.AUTHENTICATION_FAILURE, AuditResult.FAILURE
        else:
            action, result = AuditAction.AUTHORIZATION_FAILURE, AuditResult.FAILURE

        event = AuditEvent.build(
            action,
            result,
            resource_type,
            resource_id=decision.child_id,
            child_id=decision.child_id,
            actor_application_id=principal.application_id if principal else None,
            actor_auth_subject=principal.auth_subject if principal else None,
            actor_role=principal.role if principal else None,
            request_id=request_id,
            metadata=metadata,
            now=now,
        )
        return self.record(event)

    def record_action(self, action: AuditAction, result: AuditResult,
                      resource_type: str, *, resource_id: Optional[str] = None,
                      child_id: Optional[str] = None, principal=None,
                      request_id: str = "",
                      metadata: Optional[Mapping[str, object]] = None,
                      now: Optional[datetime] = None) -> AuditEvent:
        """Record a business action. Used by 0.3 workflows; no callers in 0.2."""
        merged = dict(metadata or {})
        if self._environment:
            merged.setdefault("environment", self._environment)
        event = AuditEvent.build(
            action, result, resource_type,
            resource_id=resource_id,
            child_id=child_id,
            actor_application_id=principal.application_id if principal else None,
            actor_auth_subject=principal.auth_subject if principal else None,
            actor_role=principal.role if principal else None,
            request_id=request_id,
            metadata=merged,
            now=now,
        )
        return self.record(event)
