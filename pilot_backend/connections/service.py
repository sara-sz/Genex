"""pilot_backend/connections/service.py — the provider-child relationship.

    invite_provider      caregiver -> PENDING connection to a KNOWN provider
    accept_invitation    provider  -> ACTIVE
    decline_invitation   provider  -> DECLINED, key released
    pause_connection     caregiver -> PAUSED   (+ managing assignment ended)
    resume_connection    caregiver -> ACTIVE
    revoke_connection    caregiver -> REVOKED  (+ managing assignment ended)
    assign_managing_clinician / current_managing_clinician   caregiver-facing
    connected_children   provider  -> only ACTIVE-connected children

## One direction only, in 0.5B

A caregiver invites a clinician they already know; the clinician accepts or
declines. There is no provider-to-family path, no family search and no
provider directory. That is a scope decision, not an omission: every one of
those is a surface that discloses which families or clinicians exist, and none
is needed to connect one clinician to one child for the pilot.

## How a caregiver names a clinician without being able to enumerate them

The caregiver supplies the opaque `provider_id` (`prov_...`), obtained out of
band — the practice tells the family. There is deliberately NO lookup route,
no search and no listing, so the id is the entire addressing contract.

That is safe because knowing the id grants nothing by itself. It permits only
OFFERING a connection, which creates a PENDING row that confers no access
whatsoever, and the clinician must then accept. So a guessed id cannot produce
access; it can at most produce an invitation somebody has to agree to. And
absent, retired and wrong-practice ids all raise the SAME
`ProviderNotConnectable`, so the endpoint cannot be used to confirm that a
given id exists.

## The no-duplicate rule is a write-time claim, not a read-then-write check

`PROVIDER_CONNECTION` is keyed on `(provider_id, child_id)` and held while the
connection is LIVE — pending, active or paused. A caregiver double-tapping
"connect" produces two writers computing the same key, so they collide on one
document and exactly one survives. A read-then-write guard would be correct
almost always and wrong exactly when it matters, which is the 0.3 auth-subject
defect and the 0.5A orphan-child defect both.

Declining, revoking and ending RELEASE the key, so a family that refused a
clinician can invite them again and acquire generation + 1.

## PAUSED keeps the key; that is the point of it

A paused relationship still occupies the (provider, child) slot, so the same
clinician cannot be invited again alongside it. Resuming restores the SAME row
— same `connection_id`, same `activated_at` — which matters structurally
because `ManagingClinicianAssignment.provider_connection_id` points at it.

## Only ACTIVE authorizes, and that is enforced one layer down

Nothing in this module grants child access. `authorize_child_access` reads the
connection rows and requires `is_active`, which means status ACTIVE *and*
`ended_at` unset. PENDING, DECLINED, PAUSED, REVOKED and ENDED therefore all
deny without this module being consulted at all — and without a single change
to the authz package, which is why DECLINED and PAUSED were added as statuses
rather than as flags.

## The stale managing-clinician assignment, and why the cascade is atomic

Found by inspection of the frozen 0.4A code during 0.5B. Revoking a connection
left its `ManagingClinicianAssignment` ACTIVE. That granted nothing at the
time, because every clinical write path calls `authorize_child_access` BEFORE
`_require_managing_clinician` — verified at every call site in goals, rtm and
weekly. But the assignment is keyed on the child, not the connection, so:

    revoke connection -> assignment stays ACTIVE
    later: invite same provider again -> ACTIVE connection
    -> authorize_child_access passes, and the STALE assignment names this
       provider -> managing-clinician status is restored with no explicit
       assignment, pointing at a connection that was revoked

So the cascade runs in the SAME transaction as the status change: pausing,
revoking or ending a connection ends any ACTIVE assignment for that
(provider, child) and releases its claim together with the connection write.
Atomic in both directions — a crash cannot leave an inactive connection with a
live assignment, nor release a key whose assignment is still active.

PAUSE ends the assignment too, rather than suspending it. A paused clinician
who resumes must be re-assigned deliberately: "you still clinically own this
child" is not something to restore silently after an interruption of unknown
length.

## The RTM invariant is preserved, not re-implemented

`RTMEpisode` pins `managing_provider_id` at open time, and
`_require_episode_owner` refuses any write once the active assignment names
someone else (`EpisodeTransferRefused`). Ending an assignment here therefore
cannot silently transfer an open episode: with no active assignment the
episode's writes fail closed on "no active managing clinician", and if a new
clinician is assigned later the episode refuses them by name. Nothing in this
module touches an episode.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import List, Optional

from ..audit.events import AuditAction, AuditResult
from ..authz.policy import authorize_child_access
from ..domain.connections import ProviderChildConnection
from ..domain.enums import (
    ConnectionInitiator,
    ConnectionStatus,
    EntityStatus,
)
from ..domain.identity_claims import (
    ClaimKind,
    IdentityClaim,
    claim_document_id,
    key_digest,
)
from ..domain.managing_clinician import (
    ManagingClinicianAssignment,
    ManagingClinicianStatus,
)
from ..domain.roles import ActorRole
from ..persistence.document_store import DocumentStoreError
from ..repository.interface import DuplicateRecord, RecordNotFound
from .errors import (
    ConnectionNotFound,
    ConnectionStateConflict,
    DuplicateLiveConnection,
    ProviderNotConnectable,
)

RESOURCE_PROVIDER_CONNECTION = "provider_child_connection"
RESOURCE_MANAGING_CLINICIAN = "managing_clinician_assignment"

#: Statuses that hold the PROVIDER_CONNECTION key.
LIVE_CONNECTION_STATUSES = frozenset({
    ConnectionStatus.PENDING,
    ConnectionStatus.ACTIVE,
    ConnectionStatus.PAUSED,
})


def _default_repos_factory(store):
    from ..persistence.firestore_repos import FirestoreRepositories

    return FirestoreRepositories(store)


@dataclass(frozen=True)
class ConnectedChild:
    """What a provider sees in their caseload list.

    Identity and relationship facts only. There is no name, no date of birth,
    no diagnosis, no concern, no plan and no note — a `Child` in this system
    carries none of those anyway, and this shape is what the Therapist UI
    renders a list from. `is_managing_clinician` is included because the UI
    must distinguish a child this clinician clinically owns from one they are
    merely connected to, and computing it client-side would mean shipping the
    assignment table to the browser.
    """

    child_id: str
    connection_id: str
    practice_id: str
    connected_since: Optional[datetime]
    is_managing_clinician: bool

    def as_payload(self) -> dict:
        return {
            "child_id": self.child_id,
            "connection_id": self.connection_id,
            "practice_id": self.practice_id,
            "connected_since": (self.connected_since.isoformat()
                                if self.connected_since else None),
            "is_managing_clinician": self.is_managing_clinician,
        }


class ProviderConnectionService:
    """Caregiver-initiated provider connections and managing-clinician state."""

    def __init__(self, *, repos, recorder=None, now=None,
                 repos_factory=None) -> None:
        self._repos = repos
        self._recorder = recorder
        self._now = now
        self._repos_factory = repos_factory or _default_repos_factory

    def _stamp(self) -> datetime:
        return self._now() if self._now else datetime.now(timezone.utc)

    def _audit(self, action: AuditAction, result: AuditResult,
               resource_type: str, *, principal=None, child_id=None,
               resource_id=None, request_id: str = "", **metadata) -> None:
        if self._recorder is None:
            return
        self._recorder.record_action(
            action, result, resource_type, resource_id=resource_id,
            child_id=child_id, principal=principal, request_id=request_id,
            metadata=metadata)

    # =====================================================================
    # authorization helpers — server-derived identity only
    # =====================================================================

    def _require_caregiver_for_child(self, principal, child_id: str) -> str:
        """The caller must be a caregiver with ACTIVE access to this child.

        Delegates to `authorize_child_access`, which is the same deny-by-default
        function every other service uses. Not re-implemented here: a second
        relationship test would be a second thing to keep correct.
        """
        if principal.role is not ActorRole.CAREGIVER:
            raise ConnectionNotFound(
                "no such child connection for this account")
        decision = authorize_child_access(principal, child_id, self._repos)
        if not decision.allowed:
            # Collapsed to the non-enumerating error on purpose: a caregiver
            # probing child ids must not learn which exist.
            raise ConnectionNotFound(
                "no such child connection for this account")
        return (child_id or "").strip()

    def _require_provider(self, principal) -> str:
        if principal.role is not ActorRole.PROVIDER:
            raise ConnectionNotFound("no such connection for this account")
        return principal.application_id

    def _load_connection(self, connection_id: str) -> ProviderChildConnection:
        try:
            return self._repos.provider_child.get_by_id(
                (connection_id or "").strip())
        except RecordNotFound:
            raise ConnectionNotFound(
                "no such connection for this account") from None

    def _connection_for_provider(self, principal, connection_id: str
                                 ) -> ProviderChildConnection:
        """Load a connection the AUTHENTICATED provider owns."""
        provider_id = self._require_provider(principal)
        connection = self._load_connection(connection_id)
        if connection.provider_id != provider_id:
            # Someone else's connection. Same error as absent — no oracle.
            raise ConnectionNotFound("no such connection for this account")
        return connection

    def _connection_for_caregiver(self, principal, connection_id: str
                                  ) -> ProviderChildConnection:
        """Load a connection on a child the AUTHENTICATED caregiver holds."""
        connection = self._load_connection(connection_id)
        self._require_caregiver_for_child(principal, connection.child_id)
        return connection

    # =====================================================================
    # caregiver invites a KNOWN provider
    # =====================================================================

    def invite_provider(self, principal, child_id: str, provider_id: str, *,
                        request_id: str = "") -> ProviderChildConnection:
        """Create a PENDING connection to a provider named by opaque id.

        The caregiver supplies only `child_id` and `provider_id`. The practice
        of record is read from the PROVIDER and denormalised onto the
        connection; it is never accepted from the caller.
        """
        child = self._require_caregiver_for_child(principal, child_id)
        wanted = (provider_id or "").strip()
        if not wanted:
            raise ProviderNotConnectable("no such provider")

        provider = self._connectable_provider(wanted)

        draft = ProviderChildConnection.create(
            provider.provider_id, child, provider.practice_id,
            initiated_by=ConnectionInitiator.CAREGIVER,
            actor_id=principal.application_id, now=self._stamp())

        # Generation read OUTSIDE the transaction, for the reason measured in
        # 0.4A: a query inside a Firestore transaction takes read locks and
        # contending writers then retry with backoff. A stale generation
        # cannot create a second winner — it can only collide, which is the
        # refusal wanted.
        digest = key_digest(provider.provider_id, child)
        generation = self._repos.identity_claims.next_generation(
            ClaimKind.PROVIDER_CONNECTION, digest)

        def _acquire(store) -> ProviderChildConnection:
            tx = self._repos_factory(store)
            claim = IdentityClaim.build(
                ClaimKind.PROVIDER_CONNECTION,
                (provider.provider_id, child), generation,
                holder_ref=draft.connection_id, child_id=child,
                actor_id=principal.application_id, now=self._stamp())
            # Claim FIRST, so a loser writes nothing at all.
            tx.identity_claims.claim(claim)
            tx.provider_child.connect(draft)
            return draft

        try:
            connection = self._repos.store.run_in_transaction(_acquire)
        except (DuplicateRecord, DocumentStoreError):
            self._audit(AuditAction.PROVIDER_CONNECTION_INVITED,
                        AuditResult.FAILURE, RESOURCE_PROVIDER_CONNECTION,
                        principal=principal, child_id=child,
                        request_id=request_id, provider_id=provider.provider_id,
                        integration_state=DuplicateLiveConnection.code)
            raise DuplicateLiveConnection(
                "this provider already has a live connection to this child"
            ) from None

        self._audit(AuditAction.PROVIDER_CONNECTION_INVITED,
                    AuditResult.SUCCESS, RESOURCE_PROVIDER_CONNECTION,
                    principal=principal, child_id=child,
                    resource_id=connection.connection_id,
                    request_id=request_id,
                    provider_id=connection.provider_id,
                    practice_id=connection.practice_id,
                    connection_id=connection.connection_id,
                    connection_status=connection.status.value,
                    initiated_by=connection.initiated_by.value)
        return connection

    def _connectable_provider(self, provider_id: str):
        """A provider a caregiver may be offered a connection to.

        Absent, retired, and belonging to an inactive practice all raise the
        SAME error — see `errors.py` for why that matters.
        """
        try:
            provider = self._repos.providers.get_by_id(provider_id)
        except RecordNotFound:
            raise ProviderNotConnectable("no such provider") from None
        if provider.status is not EntityStatus.ACTIVE:
            raise ProviderNotConnectable("no such provider")
        try:
            practice = self._repos.practices.get_by_id(provider.practice_id)
        except RecordNotFound:  # pragma: no cover - defensive
            raise ProviderNotConnectable("no such provider") from None
        if practice.status is not EntityStatus.ACTIVE:
            raise ProviderNotConnectable("no such provider")
        return provider

    # =====================================================================
    # provider responds
    # =====================================================================

    def accept_invitation(self, principal, connection_id: str, *,
                          request_id: str = "") -> ProviderChildConnection:
        """PENDING -> ACTIVE, by the invited provider only.

        Accepting grants connection-scoped access and NOTHING else. It does not
        make the provider this child's managing clinician — that is a separate,
        explicit, caregiver-authorized act, and conflating them is how a
        clinician would end up clinically owning a child nobody assigned them
        to.
        """
        connection = self._connection_for_provider(principal, connection_id)
        if connection.status is not ConnectionStatus.PENDING:
            raise ConnectionStateConflict(
                "only a pending invitation can be accepted")

        accepted = self._repos.provider_child.activate(
            connection.connection_id, now=self._stamp())

        self._audit(AuditAction.PROVIDER_CONNECTED, AuditResult.SUCCESS,
                    RESOURCE_PROVIDER_CONNECTION, principal=principal,
                    child_id=accepted.child_id,
                    resource_id=accepted.connection_id,
                    request_id=request_id, provider_id=accepted.provider_id,
                    practice_id=accepted.practice_id,
                    connection_id=accepted.connection_id,
                    connection_status=accepted.status.value,
                    initiated_by=accepted.initiated_by.value)
        return accepted

    def decline_invitation(self, principal, connection_id: str, *,
                           request_id: str = "") -> ProviderChildConnection:
        """PENDING -> DECLINED, by the invited provider, releasing the key.

        The key is released so the family can invite this clinician again. The
        row is kept: "this provider was offered and refused" is a fact worth
        being able to answer later, which is why DECLINED is not ENDED.
        """
        connection = self._connection_for_provider(principal, connection_id)
        if connection.status is not ConnectionStatus.PENDING:
            raise ConnectionStateConflict(
                "only a pending invitation can be declined")

        declined = self._terminate(
            connection, ConnectionStatus.DECLINED,
            actor_id=principal.application_id)

        self._audit(AuditAction.PROVIDER_CONNECTION_DECLINED,
                    AuditResult.SUCCESS, RESOURCE_PROVIDER_CONNECTION,
                    principal=principal, child_id=declined.child_id,
                    resource_id=declined.connection_id,
                    request_id=request_id, provider_id=declined.provider_id,
                    connection_id=declined.connection_id,
                    connection_status=declined.status.value,
                    initiated_by=declined.initiated_by.value)
        return declined

    # =====================================================================
    # caregiver controls an existing relationship
    # =====================================================================

    def pause_connection(self, principal, connection_id: str, *,
                         request_id: str = "") -> ProviderChildConnection:
        """ACTIVE -> PAUSED, keeping the key, ending any managing assignment."""
        connection = self._connection_for_caregiver(principal, connection_id)
        if not connection.is_active:
            raise ConnectionStateConflict(
                "only an active connection can be paused")

        paused, ended_assignment = self._suspend(
            connection, ConnectionStatus.PAUSED,
            actor_id=principal.application_id)

        self._audit(AuditAction.PROVIDER_CONNECTION_PAUSED,
                    AuditResult.SUCCESS, RESOURCE_PROVIDER_CONNECTION,
                    principal=principal, child_id=paused.child_id,
                    resource_id=paused.connection_id, request_id=request_id,
                    provider_id=paused.provider_id,
                    connection_id=paused.connection_id,
                    connection_status=paused.status.value)
        self._audit_cascade(principal, ended_assignment, request_id)
        return paused

    def resume_connection(self, principal, connection_id: str, *,
                          request_id: str = "") -> ProviderChildConnection:
        """PAUSED -> ACTIVE, same row.

        Does NOT restore a managing-clinician assignment. The pause ended it,
        and re-assigning is an explicit act — see the module docstring.
        """
        connection = self._connection_for_caregiver(principal, connection_id)
        if not connection.is_resumable:
            raise ConnectionStateConflict(
                "only a paused connection that was once active can be resumed")

        resumed = self._repos.provider_child.resume(
            connection.connection_id, now=self._stamp())

        self._audit(AuditAction.PROVIDER_CONNECTION_RESUMED,
                    AuditResult.SUCCESS, RESOURCE_PROVIDER_CONNECTION,
                    principal=principal, child_id=resumed.child_id,
                    resource_id=resumed.connection_id, request_id=request_id,
                    provider_id=resumed.provider_id,
                    connection_id=resumed.connection_id,
                    connection_status=resumed.status.value)
        return resumed

    def revoke_connection(self, principal, connection_id: str, *,
                          status: ConnectionStatus = ConnectionStatus.REVOKED,
                          request_id: str = "") -> ProviderChildConnection:
        """-> REVOKED (or ENDED), releasing the key and the assignment."""
        if status not in (ConnectionStatus.REVOKED, ConnectionStatus.ENDED):
            raise ConnectionStateConflict(
                "a connection is revoked or ended, not declined, by a caregiver")
        connection = self._connection_for_caregiver(principal, connection_id)
        if connection.status not in LIVE_CONNECTION_STATUSES:
            raise ConnectionStateConflict(
                "this connection is already closed")

        revoked, ended_assignment = self._suspend(
            connection, status, actor_id=principal.application_id,
            release_key=True)

        self._audit(AuditAction.PROVIDER_DISCONNECTED, AuditResult.SUCCESS,
                    RESOURCE_PROVIDER_CONNECTION, principal=principal,
                    child_id=revoked.child_id,
                    resource_id=revoked.connection_id, request_id=request_id,
                    provider_id=revoked.provider_id,
                    connection_id=revoked.connection_id,
                    connection_status=revoked.status.value)
        self._audit_cascade(principal, ended_assignment, request_id)
        return revoked

    def _audit_cascade(self, principal, assignment, request_id: str) -> None:
        if assignment is None:
            return
        self._audit(AuditAction.MANAGING_CLINICIAN_ENDED, AuditResult.SUCCESS,
                    RESOURCE_MANAGING_CLINICIAN, principal=principal,
                    child_id=assignment.child_id,
                    resource_id=assignment.assignment_id,
                    request_id=request_id,
                    provider_id=assignment.provider_id,
                    assignment_id=assignment.assignment_id,
                    integration_state="ENDED_BY_CONNECTION_CHANGE")

    # =====================================================================
    # the atomic transitions
    # =====================================================================

    def _active_assignment_for(self, child_id: str, provider_id: str):
        """The ACTIVE managing assignment for this (child, provider), if any."""
        for assignment in self._repos.managing_clinicians.list_for_child(child_id):
            if assignment.provider_id == provider_id:
                return assignment
        return None

    def _release_marker(self, claim_id: str, *, actor_id: Optional[str]):
        """Build the release marker for a held claim, or None if it is gone."""
        if not claim_id:
            return None
        try:
            claim = self._repos.identity_claims.get_by_id(claim_id)
        except RecordNotFound:
            return None
        return IdentityClaim.build_release(
            claim.kind, claim.key_digest, claim.generation,
            holder_ref=claim.holder_ref, child_id=claim.child_id,
            actor_id=actor_id, now=self._stamp())

    def _connection_claim_marker(self, connection: ProviderChildConnection, *,
                                 actor_id: Optional[str]):
        """Release marker for the PROVIDER_CONNECTION key this row holds.

        The connection does not store its claim id, so the key is recomputed
        from (provider_id, child_id) — the same derivation `invite_provider`
        used. Recomputing rather than storing keeps the row shape unchanged for
        pre-0.5B documents.

        The generation is `next_generation` with NO offset. That reads wrongly
        at first glance, so: `next_generation` counts RELEASE markers, and a
        live claim has not been released, so the number of releases so far IS
        the generation currently held. Subtracting one would name the
        previous, already-released generation and release it twice — which
        `release()` would reject as a duplicate, silently leaving the live key
        held forever.

        `_release_marker` then re-reads that document, so a key that is already
        gone yields None instead of a marker for something that never existed.
        """
        digest = key_digest(connection.provider_id, connection.child_id)
        generation = self._repos.identity_claims.next_generation(
            ClaimKind.PROVIDER_CONNECTION, digest)
        return self._release_marker(
            claim_document_id(ClaimKind.PROVIDER_CONNECTION, digest, generation),
            actor_id=actor_id)

    def _terminate(self, connection: ProviderChildConnection,
                   status: ConnectionStatus, *, actor_id: Optional[str]
                   ) -> ProviderChildConnection:
        """Close a PENDING connection and release its key, atomically."""
        marker = self._connection_claim_marker(connection, actor_id=actor_id)
        stamp = self._stamp()

        def _close(store) -> ProviderChildConnection:
            tx = self._repos_factory(store)
            # Read before writing — see `_suspend` for why.
            current = tx.provider_child.get_by_id(connection.connection_id)
            closed = (current.decline(now=stamp)
                      if status is ConnectionStatus.DECLINED
                      else current.end(status=status, now=stamp))
            tx.provider_child.overwrite(closed)
            if marker is not None:
                try:
                    tx.identity_claims.release(marker)
                except DuplicateRecord:
                    # Already released by a concurrent writer: the key is free,
                    # which is the outcome wanted.
                    pass
            return closed

        return self._repos.store.run_in_transaction(_close)

    def _suspend(self, connection: ProviderChildConnection,
                 status: ConnectionStatus, *, actor_id: Optional[str],
                 release_key: bool = False):
        """Pause/revoke/end a connection AND end its managing assignment.

        ONE transaction. This is the fix for the stale-assignment defect: a
        crash between the two writes would otherwise leave exactly the state
        the defect describes — an inactive connection with a live assignment
        that a later reconnect silently honours.
        """
        stamp = self._stamp()

        def _apply(store):
            tx = self._repos_factory(store)

            # ---- EVERY READ FIRST, AND THE ASSIGNMENT LOOKUP IS ONE ----
            #
            # Two Firestore rules shape this block, and the emulator enforced
            # both against earlier revisions:
            #
            # 1. All reads must precede all writes. Updating the connection and
            #    then reading the assignment raised ReadAfterWriteError.
            #
            # 2. A document only participates in conflict detection if THIS
            #    transaction read it. An earlier revision looked the assignment
            #    up before opening the transaction, so when a concurrent
            #    `assign_managing_clinician` committed, this transaction had
            #    nothing to conflict with and happily revoked the connection
            #    while leaving the brand-new assignment ACTIVE — the exact
            #    stale state the cascade exists to prevent. Caught by
            #    `test_revoke_racing_assign_never_leaves_a_stale_assignment`.
            #
            # So the QUERY runs here. A query inside a Firestore transaction
            # takes read locks and makes contending writers retry with backoff
            # — the cost measured in 0.4A — which is the right trade for an
            # operation that revokes a clinician's access.
            current = tx.provider_child.get_by_id(connection.connection_id)
            live = [a for a in tx.managing_clinicians.list_for_child(
                connection.child_id)
                if a.provider_id == connection.provider_id and a.is_active]

            assignment_markers = []
            for found in live:
                marker = self._release_marker_from(
                    tx, found.claim_id, actor_id=actor_id)
                if marker is not None:
                    assignment_markers.append(marker)

            connection_marker = (
                self._connection_claim_marker_in(tx, connection, actor_id=actor_id)
                if release_key else None)

            # ---- THEN EVERY WRITE -------------------------------------
            changed = (current.pause(now=stamp)
                       if status is ConnectionStatus.PAUSED
                       else current.end(status=status, now=stamp))
            tx.provider_child.overwrite(changed)

            ended = None
            for found in live:
                ended = found.end(
                    status=ManagingClinicianStatus.ENDED, actor_id=actor_id,
                    reason="provider connection no longer active", now=stamp)
                tx.managing_clinicians.overwrite(ended)
            for marker in assignment_markers:
                try:
                    tx.identity_claims.release(marker)
                except DuplicateRecord:
                    pass
            if connection_marker is not None:
                try:
                    tx.identity_claims.release(connection_marker)
                except DuplicateRecord:
                    pass
            return changed, ended

        return self._repos.store.run_in_transaction(_apply)

    def _release_marker_from(self, repos, claim_id: str, *,
                             actor_id: Optional[str]):
        """`_release_marker`, but reading through a supplied repository set.

        Exists so the cascade can perform this read INSIDE its transaction;
        the module-level version reads through `self._repos` and would escape
        the transaction's read set.
        """
        if not claim_id:
            return None
        try:
            claim = repos.identity_claims.get_by_id(claim_id)
        except RecordNotFound:
            return None
        return IdentityClaim.build_release(
            claim.kind, claim.key_digest, claim.generation,
            holder_ref=claim.holder_ref, child_id=claim.child_id,
            actor_id=actor_id, now=self._stamp())

    def _connection_claim_marker_in(self, repos,
                                    connection: ProviderChildConnection, *,
                                    actor_id: Optional[str]):
        """`_connection_claim_marker` reading through `repos`."""
        digest = key_digest(connection.provider_id, connection.child_id)
        generation = repos.identity_claims.next_generation(
            ClaimKind.PROVIDER_CONNECTION, digest)
        return self._release_marker_from(
            repos,
            claim_document_id(ClaimKind.PROVIDER_CONNECTION, digest, generation),
            actor_id=actor_id)

    # =====================================================================
    # managing clinician — caregiver-facing
    # =====================================================================

    def assign_managing_clinician(self, principal, child_id: str,
                                  provider_id: str, *, reason: str = "",
                                  request_id: str = ""
                                  ) -> ManagingClinicianAssignment:
        """A caregiver names an ACTIVE-connected provider as managing clinician.

        The frozen 0.4A `LongitudinalIdentityService.assign_managing_clinician`
        requires a PROVIDER principal, so it cannot serve this flow — the pilot
        needs the family to make this decision. Rather than widen frozen code,
        this acquires the SAME `MANAGING_CLINICIAN` claim with the SAME key
        derivation (`key_digest(child_id)`), so both paths contend on one
        document and the "exactly one active" invariant holds across them. A
        test pins that the derivation matches.
        """
        child = self._require_caregiver_for_child(principal, child_id)
        wanted = (provider_id or "").strip()

        connection = None
        for candidate in self._repos.provider_child.list_providers_for_child(child):
            if candidate.provider_id == wanted:
                connection = candidate
                break
        if connection is None or not connection.is_active:
            # `list_providers_for_child` already excludes non-active rows, so
            # this covers never-connected, pending, paused and revoked alike.
            raise ConnectionStateConflict(
                "this provider has no active connection to this child")

        if self._repos.managing_clinicians.list_for_child(child):
            raise ConnectionStateConflict(
                "this child already has an active managing clinician")

        draft = ManagingClinicianAssignment.create(
            child, wanted, connection.practice_id,
            provider_connection_id=connection.connection_id,
            actor_id=principal.application_id,
            actor_role=principal.role.value, reason=reason,
            now=self._stamp())
        digest = key_digest(child)
        generation = self._repos.identity_claims.next_generation(
            ClaimKind.MANAGING_CLINICIAN, digest)

        def _acquire(store) -> ManagingClinicianAssignment:
            tx = self._repos_factory(store)

            # ---- READS FIRST, and the connection check is one of them ----
            #
            # The ACTIVE-connection test above ran outside this transaction and
            # is therefore only advisory. It MUST be repeated here, because a
            # concurrent `revoke_connection` that commits in between would
            # otherwise be invisible: the claim would be acquired, the
            # assignment created, and the result would be a revoked connection
            # with a live managing assignment — precisely the stale state this
            # slice exists to eliminate, reached from the other side.
            #
            # Reading the connection inside the transaction also enrolls it in
            # conflict detection, so whichever of the two commits second
            # retries and then sees the other's effect. Caught by
            # `test_revoke_racing_assign_never_leaves_a_stale_assignment`.
            try:
                live = tx.provider_child.get_by_id(connection.connection_id)
            except RecordNotFound:  # pragma: no cover - defensive
                raise ConnectionStateConflict(
                    "this provider has no active connection to this child"
                ) from None
            if not live.is_active:
                raise ConnectionStateConflict(
                    "this provider has no active connection to this child")

            claim = IdentityClaim.build(
                ClaimKind.MANAGING_CLINICIAN, (child,), generation,
                holder_ref=draft.assignment_id, child_id=child,
                actor_id=principal.application_id, now=self._stamp())

            # ---- THEN WRITES ------------------------------------------
            tx.identity_claims.claim(claim)
            persisted = replace(draft, claim_id=claim.claim_id)
            tx.managing_clinicians.create(persisted)
            return persisted

        try:
            assignment = self._repos.store.run_in_transaction(_acquire)
        except (DuplicateRecord, DocumentStoreError):
            raise ConnectionStateConflict(
                "another writer holds the managing-clinician claim") from None

        self._audit(AuditAction.MANAGING_CLINICIAN_ASSIGNED,
                    AuditResult.SUCCESS, RESOURCE_MANAGING_CLINICIAN,
                    principal=principal, child_id=child,
                    resource_id=assignment.assignment_id,
                    request_id=request_id, provider_id=wanted,
                    practice_id=connection.practice_id,
                    assignment_id=assignment.assignment_id,
                    claim_id=assignment.claim_id)
        return assignment

    def current_managing_clinician(self, principal, child_id: str
                                   ) -> Optional[ManagingClinicianAssignment]:
        """The child's active managing clinician, for a caregiver who holds it."""
        child = self._require_caregiver_for_child(principal, child_id)
        active = self._repos.managing_clinicians.list_for_child(child)
        if len(active) > 1:
            # The claim makes this impossible; refuse rather than pick one.
            raise ConnectionStateConflict(
                "more than one active managing clinician")
        return active[0] if active else None

    def end_managing_clinician(self, principal, child_id: str, *,
                               reason: str = "", request_id: str = ""
                               ) -> ManagingClinicianAssignment:
        """End the assignment without touching the connection."""
        child = self._require_caregiver_for_child(principal, child_id)
        active = self._repos.managing_clinicians.list_for_child(child)
        if not active:
            raise ConnectionStateConflict(
                "this child has no active managing clinician")
        if len(active) > 1:
            raise ConnectionStateConflict(
                "more than one active managing clinician")

        assignment = active[0]
        marker = self._release_marker(
            assignment.claim_id, actor_id=principal.application_id)
        stamp = self._stamp()

        def _close(store) -> ManagingClinicianAssignment:
            tx = self._repos_factory(store)
            # Read before writing — see `_suspend` for why.
            current = tx.managing_clinicians.get_by_id(assignment.assignment_id)
            if not current.is_active:
                # Ended by a concurrent writer; that is the outcome wanted,
                # and `end()` would raise on an already-ended assignment.
                return current
            ended = current.end(
                status=ManagingClinicianStatus.ENDED,
                actor_id=principal.application_id, reason=reason, now=stamp)
            tx.managing_clinicians.overwrite(ended)
            if marker is not None:
                try:
                    tx.identity_claims.release(marker)
                except DuplicateRecord:
                    pass
            return ended

        ended = self._repos.store.run_in_transaction(_close)
        self._audit(AuditAction.MANAGING_CLINICIAN_ENDED, AuditResult.SUCCESS,
                    RESOURCE_MANAGING_CLINICIAN, principal=principal,
                    child_id=child, resource_id=ended.assignment_id,
                    request_id=request_id, provider_id=ended.provider_id,
                    assignment_id=ended.assignment_id)
        return ended

    # =====================================================================
    # reads
    # =====================================================================

    def connected_children(self, principal) -> List[ConnectedChild]:
        """The AUTHENTICATED provider's ACTIVE-connected children.

        There is no provider_id parameter, so there is no shape in which this
        returns somebody else's caseload. Only ACTIVE rows appear: pending,
        declined, paused, revoked and ended connections are invisible here,
        which is the same `is_active` test `authorize_child_access` applies —
        so a child in this list is exactly a child the provider can actually
        open, with no third state in between.

        A caregiver is refused outright rather than served their own children:
        this is the provider caseload route, and reusing it for families would
        make one endpoint answer two different authorization questions.
        """
        provider_id = self._require_provider(principal)
        rows = self._repos.provider_child.list_children_for_provider(provider_id)

        found = []
        for connection in sorted(rows, key=lambda c: c.child_id):
            if not connection.is_active:  # pragma: no cover - repo filters
                continue
            assignment = self._active_assignment_for(
                connection.child_id, provider_id)
            found.append(ConnectedChild(
                child_id=connection.child_id,
                connection_id=connection.connection_id,
                practice_id=connection.practice_id,
                connected_since=connection.activated_at,
                is_managing_clinician=assignment is not None))
        return found

    def list_child_connections(self, principal, child_id: str
                               ) -> List[ProviderChildConnection]:
        """Every connection on a child, for the caregiver who holds it.

        Includes closed rows: a family deciding whether to re-invite a
        clinician needs to see that they previously declined.
        """
        child = self._require_caregiver_for_child(principal, child_id)
        return sorted(
            self._repos.provider_child.list_providers_for_child(
                child, include_ended=True),
            key=lambda c: (c.created_at, c.connection_id))
