"""pilot_backend/identity/service.py — the only writer of longitudinal identity.

Every operation begins with the 0.2 authorization chain and ends with an audit
event. Nothing here bypasses either.

## Uniqueness is won, not checked

Each write claims a deterministic document before creating its record (see
`domain/identity_claims.py`). The active-state read that precedes the claim is
ADVISORY — it produces a clear refusal in the common case. The claim is
AUTHORITATIVE: under contention exactly one writer survives, because they
collide on a single document id.

That ordering matters. A read-then-write check alone would reproduce the 0.3
auth-subject defect at a different layer — correct almost always, and wrong
exactly when two writers arrive together.

## Two claims, and the orphan they can leave

A source link needs two claims — external identity, then child-source — and
two `create` calls are not atomic together. The external-identity claim is
taken FIRST because it is the cross-child constraint: it is the one whose
violation would let a single Parent session drive two children's plans.

If the second claim fails, the first is orphaned. That is deliberate and
harmless: generations count claims, so the next attempt takes generation+1
and proceeds, and no link exists for the orphan, so no active mapping was
created. The cost is one unused generation number.

## Role rules

    PARENT-system links      caregiver or provider (a caregiver may bind
                             their own family's Parent session)
    THERAPIST-system links   provider only (a caregiver has no standing to
                             assert a therapist-system mapping)
    managing clinician       provider only, always

All of them additionally require the standard child-access authorization, so
a provider with no active relationship to the child is refused first.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Tuple

from ..audit.events import AuditAction, AuditResult
from ..authz.decisions import AccessDecision
from ..authz.policy import authorize_child_access
from ..domain.identity_claims import ClaimKind, IdentityClaim, key_digest
from ..domain.managing_clinician import (
    ManagingClinicianAssignment,
    ManagingClinicianStatus,
)
from ..domain.roles import ActorRole
from ..domain.source_link import SourceLinkStatus, SourceSystem, SourceSystemLink
from ..repository.interface import DuplicateRecord, RecordNotFound
from .errors import IdentityAuthorizationError, IdentityConflict, IdentityValidationError

#: Source systems a caregiver may bind on their own child.
_CAREGIVER_WRITABLE_SYSTEMS = frozenset({SourceSystem.PARENT})

RESOURCE_SOURCE_LINK = "source_system_link"
RESOURCE_MANAGING_CLINICIAN = "managing_clinician"


class LongitudinalIdentityService:
    """Authorized reads and writes for source links and clinical ownership."""

    def __init__(self, *, repos, recorder=None, now=None) -> None:
        self._repos = repos
        self._recorder = recorder
        #: Injectable clock for deterministic tests. No request parameter
        #: reaches it.
        self._now = now

    def _stamp(self) -> datetime:
        return self._now() if self._now else datetime.now(timezone.utc)

    # -- shared gate --------------------------------------------------------

    def _authorize(self, principal, child_id: str) -> AccessDecision:
        decision = authorize_child_access(principal, child_id, self._repos)
        if not decision.allowed:
            raise IdentityAuthorizationError(
                f"not permitted for this child ({decision.denial.value})")
        return decision

    @staticmethod
    def _require_provider(principal, what: str) -> None:
        if principal.role is not ActorRole.PROVIDER:
            raise IdentityAuthorizationError(f"{what} requires a provider")

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

    # -- claims -------------------------------------------------------------

    def _win_claim(self, kind: ClaimKind, parts: Tuple[str, ...], *,
                   holder_ref: str, child_id: str, actor_id: Optional[str]
                   ) -> IdentityClaim:
        """Acquire the key at the current generation, or refuse.

        `next_generation` counts RELEASE markers only, so a competitor winning
        this key does not advance it — every contender computes the same
        generation, targets the same document, and exactly one `create`
        survives. A stale read cannot produce a second winner; it produces a
        colliding create, which raises.
        """
        digest = key_digest(*parts)
        generation = self._repos.identity_claims.next_generation(kind, digest)
        claim = IdentityClaim.build(
            kind, parts, generation, holder_ref=holder_ref, child_id=child_id,
            actor_id=actor_id, now=self._stamp())
        try:
            return self._repos.identity_claims.claim(claim)
        except DuplicateRecord:
            raise IdentityConflict(
                f"another writer holds the {kind.value} claim for this key") from None

    def _release_claim(self, claim_id: str, *, actor_id: Optional[str]) -> None:
        """Hand a key back so the next generation can be acquired.

        Tolerant of an already-released key: a duplicate release marker means
        someone released the same generation concurrently, which is the same
        outcome we wanted.
        """
        try:
            claim = self._repos.identity_claims.get_by_id(claim_id)
        except RecordNotFound:
            return
        marker = IdentityClaim.build_release(
            claim.kind, claim.key_digest, claim.generation,
            holder_ref=claim.holder_ref, child_id=claim.child_id,
            actor_id=actor_id, now=self._stamp())
        try:
            self._repos.identity_claims.release(marker)
        except DuplicateRecord:
            pass

    # =====================================================================
    # SourceSystemLink
    # =====================================================================

    def link_source_system(self, principal, child_id: str,
                           source_system: SourceSystem, external_id: str, *,
                           external_owner_ref: str = "",
                           request_id: str = "",
                           _supersedes_link_id: Optional[str] = None,
                           ) -> SourceSystemLink:
        """Bind an external identity to the canonical child.

        Idempotent for an exact repeat (same child, system and external id) —
        that is a retry, not an ambiguity. Every other active conflict is
        refused.
        """
        decision = self._authorize(principal, child_id)
        if (source_system not in _CAREGIVER_WRITABLE_SYSTEMS
                and principal.role is not ActorRole.PROVIDER):
            raise IdentityAuthorizationError(
                f"{source_system.value} links require a provider")

        external = (external_id or "").strip()
        if not external:
            raise IdentityValidationError("external id must not be empty")

        # ---- advisory active-state checks (the claim is authoritative) ----
        for existing in self._repos.source_links.list_for_child(child_id):
            if existing.source_system is not source_system:
                continue
            if existing.external_id == external:
                return existing  # exact repeat: a retry, not a conflict
            self._fail(principal, child_id, AuditAction.SOURCE_LINK_CREATED,
                       RESOURCE_SOURCE_LINK, request_id, source_system,
                       "child already has an active link for this source system")

        for other in self._repos.source_links.list_for_external_id(external):
            if other.source_system is source_system and other.child_id != child_id:
                self._fail(principal, child_id, AuditAction.SOURCE_LINK_CREATED,
                           RESOURCE_SOURCE_LINK, request_id, source_system,
                           "external identity is already active for another child")

        # ---- authoritative claims ----
        link_id_placeholder = f"pending:{child_id}:{source_system.value}"
        external_claim = self._win_claim(
            ClaimKind.EXTERNAL_IDENTITY, (source_system.value, external),
            holder_ref=link_id_placeholder, child_id=child_id,
            actor_id=principal.application_id)
        try:
            child_claim = self._win_claim(
                ClaimKind.CHILD_SOURCE, (child_id, source_system.value),
                holder_ref=link_id_placeholder, child_id=child_id,
                actor_id=principal.application_id)
        except IdentityConflict:
            # Two `create` calls cannot be atomic together. Release the first
            # so the external key is not orphaned at this generation, then
            # refuse — see domain/identity_claims.py.
            self._release_claim(external_claim.claim_id,
                                actor_id=principal.application_id)
            raise

        link = SourceSystemLink.create(
            child_id, source_system, external,
            external_owner_ref=external_owner_ref,
            actor_id=principal.application_id,
            actor_role=principal.role.value,
            child_source_claim_id=child_claim.claim_id,
            external_identity_claim_id=external_claim.claim_id,
            supersedes_link_id=_supersedes_link_id,
            now=self._stamp())
        self._repos.source_links.create(link)

        self._audit(AuditAction.SOURCE_LINK_CREATED, AuditResult.SUCCESS,
                    RESOURCE_SOURCE_LINK, principal=principal, child_id=child_id,
                    resource_id=link.link_id, request_id=request_id,
                    source_system=source_system.value, link_id=link.link_id,
                    claim_id=child_claim.claim_id, claim_kind=ClaimKind.CHILD_SOURCE.value)
        return link

    def _fail(self, principal, child_id, action, resource_type, request_id,
              source_system, message) -> None:
        self._audit(action, AuditResult.FAILURE, resource_type,
                    principal=principal, child_id=child_id, resource_id=None,
                    request_id=request_id,
                    source_system=getattr(source_system, "value", "") or "")
        raise IdentityConflict(message)

    def end_source_link(self, principal, link_id: str, *, reason: str = "",
                        request_id: str = "",
                        status: SourceLinkStatus = SourceLinkStatus.ENDED
                        ) -> SourceSystemLink:
        """End an active link. The row and its claims are retained."""
        try:
            link = self._repos.source_links.get_by_id(link_id)
        except RecordNotFound:
            raise IdentityConflict("no such source link") from None

        self._authorize(principal, link.child_id)
        if (link.source_system not in _CAREGIVER_WRITABLE_SYSTEMS
                and principal.role is not ActorRole.PROVIDER):
            raise IdentityAuthorizationError(
                f"{link.source_system.value} links require a provider")
        if not link.is_active:
            raise IdentityConflict("source link is already ended")

        ended = link.end(status=status, actor_id=principal.application_id,
                         reason=reason, now=self._stamp())
        self._repos.source_links.update(ended)
        # Hand both keys back so a replacement link can be made.
        for claim_id in (link.external_identity_claim_id,
                         link.child_source_claim_id):
            if claim_id:
                self._release_claim(claim_id, actor_id=principal.application_id)

        self._audit(AuditAction.SOURCE_LINK_ENDED, AuditResult.SUCCESS,
                    RESOURCE_SOURCE_LINK, principal=principal,
                    child_id=link.child_id, resource_id=link.link_id,
                    request_id=request_id,
                    source_system=link.source_system.value, link_id=link.link_id)
        return ended

    def replace_source_link(self, principal, link_id: str, new_external_id: str, *,
                            external_owner_ref: str = "", reason: str = "",
                            request_id: str = "") -> SourceSystemLink:
        """End a link and bind a new external identity, preserving lineage."""
        try:
            previous = self._repos.source_links.get_by_id(link_id)
        except RecordNotFound:
            raise IdentityConflict("no such source link") from None

        self.end_source_link(principal, link_id, reason=reason,
                             request_id=request_id,
                             status=SourceLinkStatus.SUPERSEDED)
        successor = self.link_source_system(
            principal, previous.child_id, previous.source_system, new_external_id,
            external_owner_ref=external_owner_ref, request_id=request_id,
            _supersedes_link_id=previous.link_id)

        # Stamp the back-pointer on the predecessor so lineage reads both ways.
        refreshed = self._repos.source_links.get_by_id(previous.link_id)
        self._repos.source_links.update(
            refreshed.with_successor(successor.link_id, now=self._stamp()))

        self._audit(AuditAction.SOURCE_LINK_REPLACED, AuditResult.SUCCESS,
                    RESOURCE_SOURCE_LINK, principal=principal,
                    child_id=previous.child_id, resource_id=successor.link_id,
                    request_id=request_id,
                    source_system=previous.source_system.value,
                    link_id=successor.link_id)
        return successor

    # -- reads --------------------------------------------------------------

    def list_source_links(self, principal, child_id: str, *,
                          include_ended: bool = False) -> List[SourceSystemLink]:
        self._authorize(principal, child_id)
        return self._repos.source_links.list_for_child(
            child_id, include_ended=include_ended)

    def resolve_child_for_external(self, source_system: SourceSystem,
                                   external_id: str) -> Optional[str]:
        """Canonical child for an external identity, or None. Fails closed.

        Deliberately unauthenticated at this layer: it is the lookup a
        transport performs BEFORE it has a child to authorize against. It
        returns an opaque child id and nothing else, and every caller must
        then run the normal authorization chain against that id.
        """
        external = (external_id or "").strip()
        if not external:
            return None
        active = [link for link in self._repos.source_links.list_for_external_id(external)
                  if link.source_system is source_system]
        if len(active) > 1:
            raise IdentityConflict(
                "external identity resolves to more than one canonical child")
        return active[0].child_id if active else None

    # =====================================================================
    # ManagingClinicianAssignment
    # =====================================================================

    def _active_provider_connection(self, provider_id: str, child_id: str):
        for connection in self._repos.provider_child.list_providers_for_child(child_id):
            if connection.provider_id == provider_id:
                return connection
        return None

    def assign_managing_clinician(self, principal, child_id: str, provider_id: str, *,
                                  expected_practice_id: Optional[str] = None,
                                  reason: str = "", request_id: str = "",
                                  _supersedes_assignment_id: Optional[str] = None,
                                  ) -> ManagingClinicianAssignment:
        """Assign clinical ownership. Provider-only; validated against the connection."""
        self._authorize(principal, child_id)
        self._require_provider(principal, "assigning a managing clinician")

        connection = self._active_provider_connection(provider_id, child_id)
        if connection is None:
            self._audit(AuditAction.MANAGING_CLINICIAN_ASSIGNED, AuditResult.FAILURE,
                        RESOURCE_MANAGING_CLINICIAN, principal=principal,
                        child_id=child_id, resource_id=None, request_id=request_id,
                        provider_id=provider_id)
            raise IdentityValidationError(
                "provider has no active connection to this child")

        # Practice of record comes from the CONNECTION, never from the
        # provider's current practice — an employer change must not rewrite
        # who held the relationship.
        practice_id = connection.practice_id
        if expected_practice_id and expected_practice_id != practice_id:
            raise IdentityValidationError(
                "practice does not match the provider's connection of record")

        if self._repos.managing_clinicians.list_for_child(child_id):
            raise IdentityConflict(
                "this child already has an active managing clinician")

        claim = self._win_claim(
            ClaimKind.MANAGING_CLINICIAN, (child_id,),
            holder_ref=f"pending:{child_id}", child_id=child_id,
            actor_id=principal.application_id)

        assignment = ManagingClinicianAssignment.create(
            child_id, provider_id, practice_id,
            provider_connection_id=connection.connection_id,
            actor_id=principal.application_id,
            actor_role=principal.role.value,
            reason=reason, claim_id=claim.claim_id,
            supersedes_assignment_id=_supersedes_assignment_id,
            now=self._stamp())
        self._repos.managing_clinicians.create(assignment)

        self._audit(AuditAction.MANAGING_CLINICIAN_ASSIGNED, AuditResult.SUCCESS,
                    RESOURCE_MANAGING_CLINICIAN, principal=principal,
                    child_id=child_id, resource_id=assignment.assignment_id,
                    request_id=request_id, provider_id=provider_id,
                    practice_id=practice_id, assignment_id=assignment.assignment_id,
                    claim_id=claim.claim_id)
        return assignment

    def end_managing_clinician(self, principal, child_id: str, *, reason: str = "",
                               request_id: str = "",
                               status: ManagingClinicianStatus =
                               ManagingClinicianStatus.ENDED
                               ) -> ManagingClinicianAssignment:
        self._authorize(principal, child_id)
        self._require_provider(principal, "ending a managing clinician assignment")

        active = self._repos.managing_clinicians.list_for_child(child_id)
        if not active:
            raise IdentityConflict("this child has no active managing clinician")
        if len(active) > 1:
            # Should be impossible given the claim; refuse rather than pick.
            raise IdentityConflict("more than one active managing clinician")

        ended = active[0].end(status=status, actor_id=principal.application_id,
                              reason=reason, now=self._stamp())
        self._repos.managing_clinicians.update(ended)
        if ended.claim_id:
            self._release_claim(ended.claim_id, actor_id=principal.application_id)

        self._audit(AuditAction.MANAGING_CLINICIAN_ENDED, AuditResult.SUCCESS,
                    RESOURCE_MANAGING_CLINICIAN, principal=principal,
                    child_id=child_id, resource_id=ended.assignment_id,
                    request_id=request_id, provider_id=ended.provider_id,
                    assignment_id=ended.assignment_id)
        return ended

    def transfer_managing_clinician(self, principal, child_id: str,
                                    new_provider_id: str, *, reason: str = "",
                                    request_id: str = ""
                                    ) -> ManagingClinicianAssignment:
        """End the current assignment and open a new one, with lineage both ways.

        Forward debt, deliberately NOT implemented here: an open RTM episode
        must not silently transfer. When RTM exists, a transfer will require
        the episode to be explicitly closed and a new one opened under the new
        managing clinician. No RTM object exists in this slice.
        """
        self._authorize(principal, child_id)
        self._require_provider(principal, "transferring a managing clinician")

        active = self._repos.managing_clinicians.list_for_child(child_id)
        if not active:
            raise IdentityConflict("this child has no active managing clinician")
        previous = active[0]

        self.end_managing_clinician(
            principal, child_id, reason=reason, request_id=request_id,
            status=ManagingClinicianStatus.TRANSFERRED)
        successor = self.assign_managing_clinician(
            principal, child_id, new_provider_id, reason=reason,
            request_id=request_id, _supersedes_assignment_id=previous.assignment_id)

        refreshed = self._repos.managing_clinicians.get_by_id(previous.assignment_id)
        self._repos.managing_clinicians.update(
            refreshed.with_successor(successor.assignment_id, now=self._stamp()))

        self._audit(AuditAction.MANAGING_CLINICIAN_TRANSFERRED, AuditResult.SUCCESS,
                    RESOURCE_MANAGING_CLINICIAN, principal=principal,
                    child_id=child_id, resource_id=successor.assignment_id,
                    request_id=request_id, provider_id=new_provider_id,
                    assignment_id=successor.assignment_id)
        return successor

    def current_managing_clinician(self, principal, child_id: str
                                   ) -> Optional[ManagingClinicianAssignment]:
        self._authorize(principal, child_id)
        active = self._repos.managing_clinicians.list_for_child(child_id)
        if len(active) > 1:
            raise IdentityConflict("more than one active managing clinician")
        return active[0] if active else None

    def managing_clinician_history(self, principal, child_id: str
                                   ) -> List[ManagingClinicianAssignment]:
        self._authorize(principal, child_id)
        return sorted(
            self._repos.managing_clinicians.list_for_child(child_id, include_ended=True),
            key=lambda a: (a.effective_from, a.assignment_id))
