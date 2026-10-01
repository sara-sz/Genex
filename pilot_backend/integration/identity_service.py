"""pilot_backend/integration/identity_service.py — authenticated identity.

    whoami              resolve the app identity behind a verified token
    bootstrap_caregiver atomically bind an auth subject to ONE Caregiver
    link_parent_session Parent session_id -> canonical Child
    my_children         the caller's own canonical children

## Bootstrap is one transaction, not a check-then-write

The subject claim AND the Caregiver commit together:

    transaction:
        auth_subject_claims.claim(deterministic claim)
        caregivers.create(caregiver)

`claim`'s document id is derived from the subject fingerprint, so concurrent
writers collide on one document and exactly one survives. A read-then-write
guard was explicitly ruled out, and would not have helped: the window it leaves
produces two Caregivers sharing a subject, which `get_by_auth_subject` then
refuses forever.

Atomicity matters in BOTH directions. A claim without a caregiver would strand
the subject with no identity to resolve and no release path; a caregiver
without a claim would leave the subject unprotected for the next writer.

## Why the advisory read comes first anyway

`find_for_subject` runs BEFORE the transaction, and that is not the uniqueness
check — it is the idempotency path. A repeat bootstrap is overwhelmingly the
common case (every app launch), and resolving an existing claim turns it into
one read instead of a guaranteed transaction failure. The claim remains
authoritative: a race that slips past the advisory read still collides.

## A caregiver predating the claim gains one; it is never duplicated

Fixtures and admin provisioning created caregivers before write-time
uniqueness existed, so a subject can resolve to exactly one caregiver with no
claim behind it. That identity is returned and the claim is BACKFILLED onto
it — never a second caregiver, and never an edit to the existing one.

The backfill is a single create-only write on the same deterministic key, so
it is the uniqueness enforcement in that path too. Five outcomes are pinned:

    one legacy caregiver, no claim   -> claim created, that caregiver returned
    two concurrent backfills         -> one claim, both get the same caregiver
    >1 caregiver for the subject     -> AmbiguousSubjectState, NO claim written
    claim held by a provider         -> SubjectAlreadyHeld
    claim names a different actor    -> AmbiguousSubjectState, never repaired

The last is the one worth stating explicitly. A claim pointing somewhere other
than the caregiver the subject resolves to is inconsistent, and either record
could be the wrong one. Overwriting, transferring or "repairing" it would
silently decide whose data a person sees, so the bootstrap refuses and leaves
both records exactly as they are.

## The bridge is ONE transaction, and why 0.4A's service is not called

An earlier revision created the `Child`, then the `CaregiverChildConnection`,
then called `LongitudinalIdentityService.link_source_system` — reusing the
frozen 0.4A service exactly as intended. The REAL emulator refuted it. Eight
concurrent links of one session produced:

    succeeded: 1   failed: 7 (IdentityConflict)
    source links for session: 1      <- the 0.4A claim worked perfectly
    Child docs: 8, with ACTIVE connections: 8
    my_children: 8 canonical children, 7 of them orphans

The 0.4A mutex did its job; it was simply acquired LAST, after two writes that
were already committed. And these orphans are NOT the inert kind: each had an
active caregiver relationship, so all eight appeared in `/pilot/me/children`. A
caregiver double-tapping "link my child" would see eight children.

Reordering cannot fix it. `link_source_system` begins with
`authorize_child_access`, which requires an ACTIVE caregiver-child
relationship — so the connection must already exist before it can be called,
and the orphan window is structural rather than incidental.

So the three writes and both 0.4A claims commit together here. The primitives
are reused unchanged (`IdentityClaim.build`, `ClaimKind`, `key_digest`,
`SourceSystemLink.create`, `next_generation`) and the frozen service is left
untouched; only the composition differs, because the port refuses a nested
transaction and there is no way to enlist that service in this one.

Authorization is not weakened by skipping `authorize_child_access`: this call
CREATES the relationship it would have checked, and ownership of the Parent
session was already proven server-side against the verified subject.

## Parent is read-only, and session_id is never canonical

The bridge reads `ParentSessionFacts`, proves ownership against the VERIFIED
subject, and writes only into the pilot store. `session_id` is stored once, as
`SourceSystemLink.external_id`. It never becomes a `child_id`.

## The second session fails closed

Parent is single-child today. A different, previously unseen session for a
caregiver who already has one is genuinely ambiguous — the same child
re-onboarded, or a second child — and both guesses are unrecoverable. Nothing
is persisted; `SecondSessionUnresolved` says so explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import List, Optional

from ..audit.events import AuditAction, AuditResult
from ..domain.auth_identity import (
    AuthSubjectIdentityClaim,
    auth_subject_claim_id,
    subject_fingerprint,
)
from ..domain.connections import CaregiverChildConnection
from ..domain.entities import Caregiver, Child
from ..domain.enums import CaregiverRelationship
from ..domain.identity_claims import ClaimKind, IdentityClaim, key_digest
from ..domain.roles import ActorRole
from ..domain.source_link import SourceSystem, SourceSystemLink
from ..persistence.document_store import DocumentStoreError
from ..repository.interface import (
    AmbiguousAuthSubject,
    DuplicateRecord,
    RecordNotFound,
)
from .errors import (
    AmbiguousSubjectState,
    ParentSessionLinkContended,
    ParentSessionUnavailable,
    SecondSessionUnresolved,
    SubjectAlreadyHeld,
)
from .parent_source import ParentSessionFacts, ParentSessionSource

RESOURCE_IDENTITY = "app_identity"
RESOURCE_PARENT_LINK = "parent_session_link"


def _default_repos_factory(store):
    from ..persistence.firestore_repos import FirestoreRepositories

    return FirestoreRepositories(store)


@dataclass(frozen=True)
class AppIdentity:
    """What `/pilot/me` returns. Identifiers and a role, nothing more."""

    actor_id: str
    role: ActorRole
    caregiver_id: Optional[str] = None
    provider_id: Optional[str] = None
    practice_id: Optional[str] = None

    def as_payload(self) -> dict:
        """Response body. Omits absent fields rather than sending nulls."""
        body = {"actor_id": self.actor_id, "role": self.role.value}
        for name, value in (("caregiver_id", self.caregiver_id),
                            ("provider_id", self.provider_id),
                            ("practice_id", self.practice_id)):
            if value:
                body[name] = value
        return body


@dataclass(frozen=True)
class ParentLinkResult:
    """The outcome of a session link. `created` distinguishes retry from new."""

    child_id: str
    source_link_id: str
    connection_id: str
    created: bool


class IntegrationIdentityService:
    """Authenticated identity and the Parent canonical-child bridge."""

    def __init__(self, *, repos, parent_source: Optional[ParentSessionSource] = None,
                 recorder=None, now=None, repos_factory=None) -> None:
        self._repos = repos
        self._parent = parent_source
        self._recorder = recorder
        self._repos_factory = repos_factory or _default_repos_factory
        self._now = now

    def _stamp(self) -> datetime:
        return self._now() if self._now else datetime.now(timezone.utc)

    def _audit(self, action: AuditAction, result: AuditResult,
               resource_type: str, *, principal=None, child_id=None,
               resource_id=None, request_id: str = "", **metadata) -> None:
        """PHI-safe audit. The raw auth subject is NEVER a parameter here.

        Callers pass `subject_fingerprint` when provenance is needed. There is
        no code path that puts the subject itself into an audit record.
        """
        if self._recorder is None:
            return
        self._recorder.record_action(
            action, result, resource_type,
            resource_id=resource_id, child_id=child_id, principal=principal,
            request_id=request_id, metadata=metadata,
        )

    # =====================================================================
    # /pilot/me
    # =====================================================================

    def whoami(self, principal) -> AppIdentity:
        """The app identity behind a verified token. Creates NOTHING.

        `principal` is already resolved server-side by `resolve_principal`,
        which derives the role from which repository matched the auth subject
        — so a client cannot assert a role by any route into this method.
        """
        if principal.role is ActorRole.CAREGIVER:
            return AppIdentity(actor_id=principal.application_id,
                               role=ActorRole.CAREGIVER,
                               caregiver_id=principal.application_id)

        provider = self._repos.providers.get_by_id(principal.application_id)
        return AppIdentity(actor_id=principal.application_id,
                           role=ActorRole.PROVIDER,
                           provider_id=provider.provider_id,
                           practice_id=provider.practice_id)

    # =====================================================================
    # Caregiver self-bootstrap
    # =====================================================================

    def bootstrap_caregiver(self, auth_subject: str, *,
                            display_name: str = "",
                            request_id: str = "") -> Caregiver:
        """Bind a verified auth subject to exactly one Caregiver.

        `auth_subject` comes from the VERIFIED token at the transport layer and
        is never read from a request body. It is used transiently to compute a
        fingerprint and is not stored in the claim or in audit metadata.

        Idempotent, retry-safe and concurrency-safe.
        """
        fingerprint = subject_fingerprint(auth_subject)

        # --- fail closed on pre-existing damage -------------------------
        #
        # Checked FIRST and never repaired. Choosing which of two identities
        # is real decides whose data a person sees; a bootstrap endpoint is
        # the wrong place to make that call silently.
        try:
            existing = self._repos.caregivers.get_by_auth_subject(auth_subject)
        except AmbiguousAuthSubject:
            self._audit(AuditAction.CAREGIVER_IDENTITY_BOOTSTRAPPED,
                        AuditResult.FAILURE, RESOURCE_IDENTITY,
                        request_id=request_id,
                        subject_fingerprint=fingerprint)
            raise AmbiguousSubjectState(
                "this subject resolves to more than one caregiver") from None

        # --- a PRE-PROVISIONED provider holds this subject ---------------
        #
        # The claim check below catches a provider that HAS a claim. A provider
        # record created before this primitive existed — which is every
        # provider today, since nothing mints provider claims yet — has none,
        # so without this the bootstrap would create a caregiver and leave the
        # subject resolving to BOTH. `resolve_principal` then refuses that
        # subject forever ("resolves to both a caregiver and a provider
        # record"), with no release path. Same unrecoverable shape the subject
        # claim exists to prevent, reached from the provider side.
        try:
            provider = self._repos.providers.get_by_auth_subject(auth_subject)
        except AmbiguousAuthSubject:
            self._audit(AuditAction.CAREGIVER_IDENTITY_BOOTSTRAPPED,
                        AuditResult.FAILURE, RESOURCE_IDENTITY,
                        request_id=request_id, subject_fingerprint=fingerprint)
            raise AmbiguousSubjectState(
                "this subject resolves to more than one provider") from None
        if provider is not None:
            self._audit(AuditAction.CAREGIVER_IDENTITY_BOOTSTRAPPED,
                        AuditResult.FAILURE, RESOURCE_IDENTITY,
                        request_id=request_id, subject_fingerprint=fingerprint,
                        holder_actor_type=ActorRole.PROVIDER.value)
            raise SubjectAlreadyHeld(
                "this subject already belongs to a provider identity")

        # --- advisory idempotency read (NOT the uniqueness check) --------
        held = self._repos.auth_subject_claims.find_for_subject(auth_subject)
        if held is not None:
            return self._converge_on_holder(held, legacy=existing,
                                            fingerprint=fingerprint,
                                            request_id=request_id)

        if existing is not None:
            # A caregiver predating write-time uniqueness — fixture-created or
            # admin-provisioned. Return it rather than minting a second
            # identity; the claim is backfilled so the next writer collides.
            return self._backfill_claim(existing, auth_subject,
                                        fingerprint=fingerprint,
                                        request_id=request_id)

        # --- authoritative: claim + caregiver, atomically ---------------
        draft = Caregiver.create(display_name or "Caregiver",
                                 auth_subject=auth_subject, now=self._stamp())
        claim = AuthSubjectIdentityClaim.build(
            auth_subject, holder_actor_id=draft.caregiver_id,
            holder_actor_type=ActorRole.CAREGIVER, now=self._stamp())

        def _acquire(store) -> Caregiver:
            tx = self._repos_factory(store)
            tx.auth_subject_claims.claim(claim)
            tx.caregivers.create(draft)
            return draft

        try:
            caregiver = self._repos.store.run_in_transaction(_acquire)
        except (DuplicateRecord, DocumentStoreError):
            # Another writer won the subject between the advisory read and
            # here. Converge on THEIR caregiver — the whole point is that both
            # callers end up with the same identity.
            #
            # `legacy=None` is correct and not an omission: this path is only
            # reachable when the earlier `get_by_auth_subject` found no
            # caregiver, so there is no legacy record to reconcile against.
            winner = self._repos.auth_subject_claims.find_for_subject(auth_subject)
            if winner is None:  # pragma: no cover - defensive
                raise AmbiguousSubjectState(
                    "subject claim contention could not be resolved") from None
            return self._converge_on_holder(winner, legacy=None,
                                            fingerprint=fingerprint,
                                            request_id=request_id)

        self._audit(AuditAction.CAREGIVER_IDENTITY_BOOTSTRAPPED,
                    AuditResult.SUCCESS, RESOURCE_IDENTITY,
                    resource_id=caregiver.caregiver_id, request_id=request_id,
                    caregiver_id=caregiver.caregiver_id,
                    subject_fingerprint=fingerprint,
                    holder_actor_type=ActorRole.CAREGIVER.value)
        return caregiver

    # -- the ONE place a claim's holder is turned into an outcome -----------

    def _converge_on_holder(self, claim: AuthSubjectIdentityClaim, *,
                            legacy: Optional[Caregiver],
                            fingerprint: str,
                            request_id: str) -> Caregiver:
        """Resolve an EXISTING subject claim to the caregiver it names.

        Every path that discovers a claim it did not itself write goes through
        here — the advisory repeat read, the race-loser path, and the legacy
        backfill collision — so the three invariants below hold identically in
        all three rather than being restated and drifting apart.

        Nothing is written. In particular a claim is never re-pointed: there is
        no transfer, overwrite or automatic repair anywhere in this method.
        """
        # (5) A subject held by a provider is never bootstrappable as a
        # caregiver. One person, one app identity.
        if claim.is_held_by_provider:
            self._audit(AuditAction.CAREGIVER_IDENTITY_BOOTSTRAPPED,
                        AuditResult.FAILURE, RESOURCE_IDENTITY,
                        request_id=request_id,
                        subject_fingerprint=fingerprint,
                        holder_actor_type=claim.holder_actor_type.value)
            raise SubjectAlreadyHeld(
                "this subject already belongs to a provider identity")

        # (6) The claim names a DIFFERENT caregiver than the one this subject
        # actually resolves to. Inconsistent, and not ours to adjudicate: the
        # claim could be stale, or the caregiver could be. Repairing it either
        # way silently decides whose data this person sees.
        if legacy is not None and claim.holder_actor_id != legacy.caregiver_id:
            self._audit(AuditAction.CAREGIVER_IDENTITY_BOOTSTRAPPED,
                        AuditResult.FAILURE, RESOURCE_IDENTITY,
                        request_id=request_id,
                        subject_fingerprint=fingerprint,
                        holder_actor_type=claim.holder_actor_type.value,
                        integration_state="CLAIM_HOLDER_MISMATCH")
            raise AmbiguousSubjectState(
                "this subject's claim names a different caregiver than the "
                "record it resolves to")

        try:
            return self._repos.caregivers.get_by_id(claim.holder_actor_id)
        except RecordNotFound:
            # A claim whose holder does not exist. The repository is
            # create-only, so nothing can clear it and no identity can be
            # minted behind it — report it rather than mint a second one.
            self._audit(AuditAction.CAREGIVER_IDENTITY_BOOTSTRAPPED,
                        AuditResult.FAILURE, RESOURCE_IDENTITY,
                        request_id=request_id,
                        subject_fingerprint=fingerprint,
                        integration_state="CLAIM_HOLDER_ABSENT")
            raise AmbiguousSubjectState(
                "this subject's claim names a caregiver that does not "
                "exist") from None

    def _backfill_claim(self, caregiver: Caregiver, auth_subject: str, *,
                        fingerprint: str, request_id: str = "") -> Caregiver:
        """Protect a pre-uniqueness caregiver without disturbing it.

        `claim` is a single create-only write on a deterministically-keyed
        document, so it IS the uniqueness enforcement here too — the
        `get_by_auth_subject` lookup that found `caregiver` is advisory and is
        never relied on to be the guard. One write needs no transaction; a
        transaction around it would add no atomicity.

        The caregiver record itself is never rewritten, and NO new caregiver is
        minted: a legacy identity gains a claim, nothing else.
        """
        claim = AuthSubjectIdentityClaim.build(
            auth_subject, holder_actor_id=caregiver.caregiver_id,
            holder_actor_type=ActorRole.CAREGIVER, now=self._stamp())
        try:
            self._repos.auth_subject_claims.claim(claim)
        except (DuplicateRecord, DocumentStoreError):
            # A concurrent backfill got there first. Re-read and converge
            # rather than assume it agrees with us: usually it names the same
            # legacy caregiver and both callers return it, but a claim naming
            # anyone else must fail closed, not be swallowed.
            winner = self._repos.auth_subject_claims.find_for_subject(
                auth_subject)
            if winner is None:  # pragma: no cover - defensive
                raise AmbiguousSubjectState(
                    "subject claim contention could not be resolved") from None
            return self._converge_on_holder(winner, legacy=caregiver,
                                            fingerprint=fingerprint,
                                            request_id=request_id)

        self._audit(AuditAction.CAREGIVER_IDENTITY_BOOTSTRAPPED,
                    AuditResult.SUCCESS, RESOURCE_IDENTITY,
                    resource_id=caregiver.caregiver_id, request_id=request_id,
                    caregiver_id=caregiver.caregiver_id,
                    subject_fingerprint=fingerprint,
                    holder_actor_type=ActorRole.CAREGIVER.value,
                    integration_state="LEGACY_CLAIM_BACKFILLED")
        return caregiver

    # =====================================================================
    # Parent session -> canonical child
    # =====================================================================

    def link_parent_session(self, principal, session_id: str, *,
                            request_id: str = "") -> ParentLinkResult:
        """Bridge an OWNED Parent session to a canonical child.

        The caller supplies only `session_id`. The canonical child id, the
        caregiver id, the owner uid and the relationship status are all
        server-derived.
        """
        if principal.role is not ActorRole.CAREGIVER:
            raise SubjectAlreadyHeld(
                "linking a Parent session requires a caregiver")
        if self._parent is None:  # pragma: no cover - composition error
            raise ParentSessionUnavailable("no Parent source is configured")

        caregiver_id = principal.application_id
        external = (session_id or "").strip()
        if not external:
            raise ParentSessionUnavailable("a session id is required")

        # --- idempotent retry: this exact session already linked? -------
        #
        # Checked BEFORE ownership so a retry costs no Parent read, and before
        # the second-session rule so a repeat never trips it.
        for link in self._repos.source_links.list_for_external_id(external):
            if link.source_system is not SourceSystem.PARENT:
                continue
            if not self._caregiver_owns(caregiver_id, link.child_id):
                # Someone else's session. Same error as "absent" — no oracle.
                raise ParentSessionUnavailable(
                    "no such Parent session for this account")
            connection = self._active_connection(caregiver_id, link.child_id)
            return ParentLinkResult(
                child_id=link.child_id, source_link_id=link.link_id,
                connection_id=connection.connection_id if connection else "",
                created=False)

        # --- ownership, against the VERIFIED subject --------------------
        facts = self._parent.fetch_session_facts(
            external, requesting_subject=principal.auth_subject)
        if facts is None or not facts.is_owned_by(principal.auth_subject):
            # Absent and not-owned collapse to ONE outcome, deliberately.
            self._audit(AuditAction.PARENT_SESSION_LINKED, AuditResult.FAILURE,
                        RESOURCE_PARENT_LINK, principal=principal,
                        request_id=request_id, source_system=SourceSystem.PARENT.value)
            raise ParentSessionUnavailable(
                "no such Parent session for this account")

        # --- second-session rule: fail closed, persist nothing ----------
        already = self._linked_parent_sessions(caregiver_id)
        if already:
            self._audit(AuditAction.PARENT_SESSION_LINKED, AuditResult.FAILURE,
                        RESOURCE_PARENT_LINK, principal=principal,
                        request_id=request_id,
                        source_system=SourceSystem.PARENT.value,
                        integration_state=SecondSessionUnresolved.code)
            raise SecondSessionUnresolved(
                "this account already has a linked Parent session; "
                "multi-child selection is required")

        # --- create: child, relationship and source link, ATOMICALLY ----
        #
        # A Child carries NO name, age, diagnosis, concern, plan or note. The
        # only field written is provenance of who created it.
        child = Child.create(actor_id=caregiver_id, now=self._stamp())
        connection = CaregiverChildConnection.create(
            caregiver_id, child.child_id, CaregiverRelationship.PARENT,
            actor_id=caregiver_id, now=self._stamp())
        draft = SourceSystemLink.create(
            child.child_id, SourceSystem.PARENT, external,
            actor_id=caregiver_id, actor_role=principal.role.value,
            now=self._stamp())

        # Generations are read OUTSIDE the transaction, for the reason measured
        # in 0.4A: a query inside a Firestore transaction takes read locks, and
        # contending writers then retry with backoff (194s versus 2s for the
        # same suite). A stale generation cannot create a second winner — it can
        # only collide, which is the refusal wanted.
        external_parts = (SourceSystem.PARENT.value, external)
        child_parts = (child.child_id, SourceSystem.PARENT.value)
        external_generation = self._repos.identity_claims.next_generation(
            ClaimKind.EXTERNAL_IDENTITY, key_digest(*external_parts))
        child_generation = self._repos.identity_claims.next_generation(
            ClaimKind.CHILD_SOURCE, key_digest(*child_parts))

        def _bridge(store) -> tuple:
            tx = self._repos_factory(store)
            external_claim = IdentityClaim.build(
                ClaimKind.EXTERNAL_IDENTITY, external_parts,
                external_generation, holder_ref=draft.link_id,
                child_id=child.child_id, actor_id=caregiver_id,
                now=self._stamp())
            child_claim = IdentityClaim.build(
                ClaimKind.CHILD_SOURCE, child_parts, child_generation,
                holder_ref=draft.link_id, child_id=child.child_id,
                actor_id=caregiver_id, now=self._stamp())

            # Claims FIRST, so a loser does nothing else at all.
            tx.identity_claims.claim(external_claim)
            tx.identity_claims.claim(child_claim)
            persisted = replace(
                draft,
                external_identity_claim_id=external_claim.claim_id,
                child_source_claim_id=child_claim.claim_id)
            tx.children.create(child)
            tx.caregiver_child.connect(connection)
            tx.source_links.create(persisted)
            return persisted, child_claim

        try:
            link, child_claim = self._repos.store.run_in_transaction(_bridge)
        except (DuplicateRecord, DocumentStoreError):
            # Another writer won this session between the advisory read and
            # here. NOTHING was written — see the module docstring for the
            # orphan-child defect that made this transaction necessary.
            self._audit(AuditAction.PARENT_SESSION_LINKED, AuditResult.FAILURE,
                        RESOURCE_PARENT_LINK, principal=principal,
                        request_id=request_id,
                        source_system=SourceSystem.PARENT.value)
            raise ParentSessionLinkContended(
                "another writer is linking this Parent session") from None

        self._audit(AuditAction.CHILD_CREATED, AuditResult.SUCCESS, "child",
                    principal=principal, child_id=child.child_id,
                    resource_id=child.child_id, request_id=request_id)
        self._audit(AuditAction.CAREGIVER_CHILD_LINKED, AuditResult.SUCCESS,
                    "caregiver_child_connection", principal=principal,
                    child_id=child.child_id,
                    resource_id=connection.connection_id,
                    request_id=request_id,
                    connection_id=connection.connection_id)
        self._audit(AuditAction.SOURCE_LINK_CREATED, AuditResult.SUCCESS,
                    "source_system_link", principal=principal,
                    child_id=child.child_id, resource_id=link.link_id,
                    request_id=request_id, link_id=link.link_id,
                    source_system=SourceSystem.PARENT.value,
                    claim_id=child_claim.claim_id,
                    claim_kind=ClaimKind.CHILD_SOURCE.value)
        self._audit(AuditAction.PARENT_SESSION_LINKED, AuditResult.SUCCESS,
                    RESOURCE_PARENT_LINK, principal=principal,
                    child_id=child.child_id, resource_id=link.link_id,
                    request_id=request_id, link_id=link.link_id,
                    source_system=SourceSystem.PARENT.value)
        return ParentLinkResult(child_id=child.child_id,
                                source_link_id=link.link_id,
                                connection_id=connection.connection_id,
                                created=True)

    def _linked_parent_sessions(self, caregiver_id: str) -> List[str]:
        """External ids of Parent sessions already linked for this caregiver."""
        found = []
        for connection in self._repos.caregiver_child.list_children_for_caregiver(
                caregiver_id):
            for link in self._repos.source_links.list_for_child(
                    connection.child_id):
                if link.source_system is SourceSystem.PARENT:
                    found.append(link.external_id)
        return found

    def _caregiver_owns(self, caregiver_id: str, child_id: str) -> bool:
        return any(c.child_id == child_id and c.is_active
                   for c in self._repos.caregiver_child
                   .list_children_for_caregiver(caregiver_id))

    def _active_connection(self, caregiver_id: str, child_id: str):
        for c in self._repos.caregiver_child.list_children_for_caregiver(
                caregiver_id):
            if c.child_id == child_id and c.is_active:
                return c
        return None

    # =====================================================================
    # /pilot/me/children
    # =====================================================================

    def my_children(self, principal) -> List[str]:
        """Canonical child ids for the AUTHENTICATED caregiver only.

        There is no caregiver_id parameter, so there is no shape in which this
        enumerates somebody else's children. A provider is refused outright —
        provider child listing is a later slice with its own authorization
        review, and reusing this route would make it an enumeration shortcut.
        """
        if principal.role is not ActorRole.CAREGIVER:
            raise SubjectAlreadyHeld(
                "this listing is for caregivers; provider child access is "
                "authorized separately")
        return sorted(
            c.child_id for c in self._repos.caregiver_child
            .list_children_for_caregiver(principal.application_id)
            if c.is_active)
