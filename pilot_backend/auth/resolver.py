"""pilot_backend/auth/resolver.py — verified auth subject → application identity.

The BACKEND 0.1 invariant, now load-bearing: authentication identity is not
application identity. A `VerifiedToken.subject` is what the identity provider
says; a `caregiver_id` or `provider_id` is what this system says. This module
is the ONLY place the two meet.

## Why not email

Email is not a durable primary key. People change them, providers reassign
them, two records can normalise to the same address, and a mistyped one silently
grants someone else's caseload. `auth_subject` is opaque, provider-issued and
stable, so it is the only join key used here. `VerifiedToken.email` is carried
for contact purposes and is never read by this module.

## Why the role is derived

The role comes from WHICH repository matched the subject, not from a claim or a
request field. A caller therefore cannot assert `role=provider`; they can only
be a provider if a provider record already carries their verified subject.

A subject matching BOTH a caregiver and a provider record is a data-integrity
fault, not a dual role: it would make the effective permissions depend on
lookup order. It fails closed.

## Inactive accounts resolve to nothing

A retired caregiver or offboarded provider must stop being an identity, not
merely lose their connections — otherwise a stale record keeps resolving and
every later check has to remember to re-test status.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..domain.enums import EntityStatus
from ..repository.interface import AmbiguousAuthSubject
from ..domain.roles import ActorRole
from .interface import VerifiedToken


class PrincipalResolutionError(Exception):
    """The verified subject does not map to exactly one active application record.

    Distinct from `AuthError`: the credential IS valid. The caller is
    authenticated but not provisioned, which is a 403 — see `authz.decisions`.
    """

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class Principal:
    """An authenticated caller, resolved to an application record.

    This is what authorization consumes. It is constructed only from a
    `VerifiedToken` plus a repository lookup, so there is no code path that
    builds one from request input.
    """

    role: ActorRole
    #: `caregiver_id` or `provider_id` — the durable application identity.
    application_id: str
    #: The provider-issued subject this resolved from. Retained for audit.
    auth_subject: str
    #: Providers only. Present so audit can record the practice of record.
    practice_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not (self.application_id or "").strip():
            raise PrincipalResolutionError("principal has no application id")
        if not (self.auth_subject or "").strip():
            raise PrincipalResolutionError("principal has no auth subject")


def resolve_principal(verified: VerifiedToken, repos) -> Principal:
    """Map a verified token onto exactly one active Caregiver or Provider.

    `repos` is any object exposing `.caregivers` and `.providers` repositories
    (the in-memory topology or the Firestore-backed set — both satisfy the
    BACKEND 0.1 protocols).
    """
    subject = (verified.subject or "").strip()
    if not subject:
        raise PrincipalResolutionError("verified token has no subject")

    try:
        caregiver = repos.caregivers.get_by_auth_subject(subject)
        provider = repos.providers.get_by_auth_subject(subject)
    except AmbiguousAuthSubject:
        # Two records share this subject. Authenticated, but not resolvable to
        # a single identity, so it is a 403 rather than a 401 — and never a
        # silent pick of whichever sorted first.
        raise PrincipalResolutionError(
            "auth subject resolves to more than one application record") from None

    if caregiver is not None and provider is not None:
        raise PrincipalResolutionError(
            "auth subject resolves to both a caregiver and a provider record")

    if caregiver is not None:
        if caregiver.status is not EntityStatus.ACTIVE:
            raise PrincipalResolutionError("caregiver record is not active")
        return Principal(
            role=ActorRole.CAREGIVER,
            application_id=caregiver.caregiver_id,
            auth_subject=subject,
        )

    if provider is not None:
        if provider.status is not EntityStatus.ACTIVE:
            raise PrincipalResolutionError("provider record is not active")
        return Principal(
            role=ActorRole.PROVIDER,
            application_id=provider.provider_id,
            auth_subject=subject,
            practice_id=provider.practice_id,
        )

    raise PrincipalResolutionError("no application record for this auth subject")
