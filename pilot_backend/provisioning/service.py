"""pilot_backend/provisioning/service.py — atomic Provider provisioning.

    provision_provider_record   the ONE function that creates a Provider
    ProviderProvisioningService the authorized, audited wrapper around it

## Why this module exists: PRE-PHI Blocker 4

0.5A closed write-time `auth_subject` uniqueness for CAREGIVERS and left the
provider side open, because `providers.create` keys its document on the RANDOM
`provider_id`. Two consequences, both demonstrated by execution before this
module existed:

    providers.create twice with one subject   -> two Providers, one subject
    providers.create for a caregiver-held one -> resolve_principal refuses
                                                 that subject forever

The second is the unrecoverable one. `resolve_principal` finds both a caregiver
and a provider record for the subject and fails closed — correctly — so the
person can never authenticate again, and nothing deletes.

## A deterministic key is only a mutex for writers that TAKE it

That was the whole lesson of 0.5A. So this module does not add a second
uniqueness mechanism; it makes the provider path acquire the SAME claim, in the
same transaction as the record it protects:

    transaction:
        auth_subject_claims.claim(deterministic claim)
        providers.create(provider)

and `providers.create` is called from nowhere else in the codebase. That
exclusivity is the actual guarantee — a structural CI gate asserts it, because
a future service that calls the repository directly would silently reopen the
blocker and no behavioural test would notice.

## `provision_provider_record` is a module function, not a method

Fixtures build provider topologies, and they must go through this path too.
Were this only a service method, fixtures would keep calling
`repos.providers.create` and the invariant would hold for the deployed surface
while being false of the system as a whole — which is exactly the kind of
"closed, except where it isn't" that the 0.5A review refused to accept.

A module-level function needs no principal, no recorder and no audit context,
so a fixture can call it as cheaply as the repository method it replaces.

## An unbound Provider needs no claim

`Provider.auth_subject` is `Optional`, and `get_by_auth_subject` returns None
for a blank subject without querying, so a record with no subject cannot be
matched by anyone. Provisioning therefore REQUIRES a subject: there is no
legitimate reason for this path to create an unclaimable identity, and
permitting it would reintroduce the bypass through the front door.

## Five outcomes, mirroring the caregiver side

    no caregiver, no provider, no claim  -> claim + Provider, one transaction
    subject held by a CAREGIVER          -> SubjectAlreadyHeld, nothing written
    one legacy provider, no claim        -> claim backfilled, that one returned
    claim names a different provider     -> AmbiguousSubjectState, never repaired
    claim holder absent                  -> AmbiguousSubjectState, no twin minted

The fourth is the one worth stating. A claim pointing somewhere other than the
provider the subject resolves to is inconsistent and either record could be the
wrong one; choosing would silently decide whose caseload a clinician sees. So it
refuses and leaves both records exactly as they are. No repair by guessing.

## The raw subject is used transiently and never stored here

It is hashed to a fingerprint for the claim key and for audit provenance. The
account identifier itself reaches no claim document, no audit record and no log
line — the same rule the caregiver path follows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..audit.events import AuditAction, AuditResult
from ..domain.auth_identity import AuthSubjectIdentityClaim, subject_fingerprint
from ..domain.entities import Provider
from ..domain.enums import EntityStatus, ProviderDiscipline
from ..domain.roles import ActorRole
from ..integration.errors import AmbiguousSubjectState, SubjectAlreadyHeld
from ..persistence.document_store import DocumentStoreError
from ..repository.interface import (
    AmbiguousAuthSubject,
    DuplicateRecord,
    RecordNotFound,
)
from .errors import ProviderProvisioningConflict, ProviderProvisioningError

RESOURCE_PROVIDER_IDENTITY = "provider_identity"


def _default_repos_factory(store):
    from ..persistence.firestore_repos import FirestoreRepositories

    return FirestoreRepositories(store)


@dataclass(frozen=True)
class ProvisionedProvider:
    """The outcome of a provisioning call.

    `created` distinguishes a new identity from convergence on an existing one,
    so a caller — and an audit reader — can tell a first provision from a
    retry without comparing timestamps.
    """

    provider: Provider
    created: bool
    claim_backfilled: bool = False


def provision_provider_record(
    repos, *, auth_subject: str, practice_id: str,
    discipline: ProviderDiscipline, display_name: str,
    actor_id: Optional[str] = None,
    now: Optional[datetime] = None,
    repos_factory=None,
) -> ProvisionedProvider:
    """Bind an auth subject to exactly one Provider, atomically.

    Idempotent, retry-safe and concurrency-safe. Raises rather than guessing
    whenever the existing state is ambiguous.
    """
    factory = repos_factory or _default_repos_factory
    stamp = now or datetime.now(timezone.utc)

    if not isinstance(discipline, ProviderDiscipline):
        raise ProviderProvisioningError("discipline must be a ProviderDiscipline")
    if not (display_name or "").strip():
        raise ProviderProvisioningError("a provider requires a display name")

    # Raises AuthIdentityError on a blank subject. Provisioning an identity
    # nobody can authenticate as is not a thing this path may do.
    fingerprint = subject_fingerprint(auth_subject)

    practice = _require_active_practice(repos, practice_id)

    # --- a CAREGIVER holds this subject: refuse before anything else -------
    #
    # Checked first and never repaired. This is the mirror of the caregiver
    # path's provider check, and together they close the ordering in both
    # directions: whichever actor type arrives second is refused.
    try:
        caregiver = repos.caregivers.get_by_auth_subject(auth_subject)
    except AmbiguousAuthSubject:
        raise AmbiguousSubjectState(
            "this subject resolves to more than one caregiver") from None
    if caregiver is not None:
        raise SubjectAlreadyHeld(
            "this subject already belongs to a caregiver identity")

    try:
        existing = repos.providers.get_by_auth_subject(auth_subject)
    except AmbiguousAuthSubject:
        raise AmbiguousSubjectState(
            "this subject resolves to more than one provider") from None

    # --- advisory idempotency read (NOT the uniqueness check) --------------
    held = repos.auth_subject_claims.find_for_subject(auth_subject)
    if held is not None:
        return _converge_on_holder(repos, held, legacy=existing,
                                   practice_id=practice.practice_id)

    if existing is not None:
        # A provider record predating write-time uniqueness — fixture-created
        # or admin-provisioned before this module existed. It gains a claim;
        # a second provider is never minted and the record is never rewritten.
        return _backfill_claim(repos, existing, auth_subject,
                               practice_id=practice.practice_id, now=stamp)

    draft = Provider.create(practice.practice_id, discipline, display_name,
                            auth_subject=auth_subject, actor_id=actor_id,
                            now=stamp)
    claim = AuthSubjectIdentityClaim.build(
        auth_subject, holder_actor_id=draft.provider_id,
        holder_actor_type=ActorRole.PROVIDER, now=stamp)

    def _acquire(store) -> Provider:
        tx = factory(store)
        tx.auth_subject_claims.claim(claim)
        tx.providers.create(draft)
        return draft

    try:
        provider = repos.store.run_in_transaction(_acquire)
    except (DuplicateRecord, DocumentStoreError):
        # Another writer won the subject between the advisory read and here.
        # Converge on THEIR provider — both callers must end up with the same
        # identity, which is the entire point of the claim.
        #
        # `legacy=None` is correct and not an omission: this branch is only
        # reachable when `get_by_auth_subject` found no provider.
        winner = repos.auth_subject_claims.find_for_subject(auth_subject)
        if winner is None:  # pragma: no cover - defensive
            raise AmbiguousSubjectState(
                "subject claim contention could not be resolved") from None
        return _converge_on_holder(repos, winner, legacy=None,
                                   practice_id=practice.practice_id)

    return ProvisionedProvider(provider=provider, created=True)


def _require_active_practice(repos, practice_id: str):
    """The practice of record must exist and be active.

    A provider is only meaningful inside a practice — `ProviderChildConnection`
    denormalises `practice_id` from it, and the managing-clinician assignment
    copies it again. Admitting a dangling practice id here would put an
    unresolvable reference into both.
    """
    wanted = (practice_id or "").strip()
    if not wanted:
        raise ProviderProvisioningError("a provider requires a practice")
    try:
        practice = repos.practices.get_by_id(wanted)
    except RecordNotFound:
        raise ProviderProvisioningError("no such practice") from None
    if practice.status is not EntityStatus.ACTIVE:
        raise ProviderProvisioningError("that practice is not active")
    return practice


def _converge_on_holder(repos, claim: AuthSubjectIdentityClaim, *,
                        legacy: Optional[Provider],
                        practice_id: str) -> ProvisionedProvider:
    """Resolve an EXISTING subject claim to the provider it names.

    Every path that discovers a claim it did not itself write comes through
    here — the advisory repeat read, the race-loser path and the backfill
    collision — so the refusals below hold identically in all three instead of
    being restated and drifting apart.

    Nothing is written. A claim is never re-pointed: there is no transfer,
    overwrite or automatic repair in this function.
    """
    if claim.is_held_by_caregiver:
        raise SubjectAlreadyHeld(
            "this subject already belongs to a caregiver identity")

    if legacy is not None and claim.holder_actor_id != legacy.provider_id:
        raise AmbiguousSubjectState(
            "this subject's claim names a different provider than the record "
            "it resolves to")

    try:
        provider = repos.providers.get_by_id(claim.holder_actor_id)
    except RecordNotFound:
        # A claim whose holder does not exist. The repository is create-only,
        # so nothing can clear it and no identity can be minted behind it.
        raise AmbiguousSubjectState(
            "this subject's claim names a provider that does not exist") from None

    # A retry that names a DIFFERENT practice is not a retry. Returning the
    # existing provider anyway would answer a question the caller did not ask
    # and hide a provisioning mistake.
    if provider.practice_id != practice_id:
        raise ProviderProvisioningConflict(
            "this subject is already provisioned in a different practice")

    return ProvisionedProvider(provider=provider, created=False)


def _backfill_claim(repos, provider: Provider, auth_subject: str, *,
                    practice_id: str,
                    now: datetime) -> ProvisionedProvider:
    """Protect a pre-uniqueness provider without disturbing it.

    `claim` is a single create-only write on a deterministically-keyed
    document, so it IS the uniqueness enforcement here too — the
    `get_by_auth_subject` lookup that found `provider` is advisory and is never
    relied on as the guard. One write needs no transaction.

    The provider record is never rewritten and no second provider is minted: a
    legacy identity gains a claim, nothing else.
    """
    if provider.practice_id != practice_id:
        raise ProviderProvisioningConflict(
            "this subject is already provisioned in a different practice")

    claim = AuthSubjectIdentityClaim.build(
        auth_subject, holder_actor_id=provider.provider_id,
        holder_actor_type=ActorRole.PROVIDER, now=now)
    try:
        repos.auth_subject_claims.claim(claim)
    except (DuplicateRecord, DocumentStoreError):
        # A concurrent backfill got there first. Re-read and converge rather
        # than assume it agrees: usually it names this same legacy provider and
        # both callers return it, but a claim naming anyone else must fail
        # closed instead of being swallowed.
        winner = repos.auth_subject_claims.find_for_subject(auth_subject)
        if winner is None:  # pragma: no cover - defensive
            raise AmbiguousSubjectState(
                "subject claim contention could not be resolved") from None
        return _converge_on_holder(repos, winner, legacy=provider,
                                   practice_id=practice_id)

    return ProvisionedProvider(provider=provider, created=False,
                               claim_backfilled=True)


class ProviderProvisioningService:
    """Authorized, audited provider provisioning.

    Thin by design: every uniqueness decision lives in
    `provision_provider_record` so the fixture path and the request path cannot
    diverge. What this class adds is authorization and an audit trail.
    """

    def __init__(self, *, repos, recorder=None, now=None,
                 repos_factory=None) -> None:
        self._repos = repos
        self._recorder = recorder
        self._now = now
        self._repos_factory = repos_factory

    def _stamp(self) -> datetime:
        return self._now() if self._now else datetime.now(timezone.utc)

    def _audit(self, result: AuditResult, *, principal=None,
               resource_id=None, request_id: str = "", **metadata) -> None:
        """PHI-safe audit. The raw auth subject is NEVER a parameter here."""
        if self._recorder is None:
            return
        self._recorder.record_action(
            AuditAction.PROVIDER_IDENTITY_PROVISIONED, result,
            RESOURCE_PROVIDER_IDENTITY, resource_id=resource_id,
            child_id=None, principal=principal, request_id=request_id,
            metadata=metadata)

    def provision_provider(self, auth_subject: str, *, practice_id: str,
                           discipline: ProviderDiscipline, display_name: str,
                           principal=None,
                           request_id: str = "") -> ProvisionedProvider:
        """Provision one clinician.

        `provider_id` is generated server-side, `practice_id` is validated
        against a real active practice, the role is PROVIDER by construction
        and the status comes from `Provider.create`. None of the four is read
        from a caller.
        """
        fingerprint = subject_fingerprint(auth_subject)
        try:
            outcome = provision_provider_record(
                self._repos, auth_subject=auth_subject,
                practice_id=practice_id, discipline=discipline,
                display_name=display_name,
                actor_id=principal.application_id if principal else None,
                now=self._stamp(), repos_factory=self._repos_factory)
        except Exception as exc:
            self._audit(AuditResult.FAILURE, principal=principal,
                        request_id=request_id,
                        subject_fingerprint=fingerprint,
                        integration_state=type(exc).__name__)
            raise

        state = ("LEGACY_CLAIM_BACKFILLED" if outcome.claim_backfilled
                 else "PROVISIONED" if outcome.created else "CONVERGED")
        self._audit(AuditResult.SUCCESS, principal=principal,
                    resource_id=outcome.provider.provider_id,
                    request_id=request_id,
                    provider_id=outcome.provider.provider_id,
                    practice_id=outcome.provider.practice_id,
                    subject_fingerprint=fingerprint,
                    holder_actor_type=ActorRole.PROVIDER.value,
                    integration_state=state)
        return outcome
