"""pilot_backend/domain/auth_identity.py — one app identity per auth subject.

    AuthSubjectIdentityClaim  a SUBJECT-scoped write-time uniqueness claim

## Why this is not a ClaimKind on IdentityClaim

`IdentityClaim` is CHILD-scoped: its `child_id` is a required field and means
a real canonical child. An auth-subject binding is won BEFORE any child
exists, so reusing that primitive would mean passing an empty or borrowed
`child_id` — making a field lie about what it holds, and weakening the
invariant that `child_id` names a child. A later reader would reasonably
treat it as a child reference.

So this is a separate, narrow primitive with its own collection. It borrows
the MECHANISM — a deterministically-keyed document that `create` turns into an
atomic compare-and-set — without borrowing a shape that does not fit.

## The gap this closes, and why it was not belt-and-braces

`caregivers.create()` keys the document on the RANDOM `caregiver_id`, so
`auth_subject` had no write-time protection at all. Two concurrent bootstraps
for one subject both succeeded and neither collided, because the mutex was on
the wrong field. Demonstrated by execution, not inferred.

The resulting state is NOT transient. `get_by_auth_subject` then raises
`AmbiguousAuthSubject` forever — the 0.3 fail-closed rule correctly refusing
to guess which identity is real — so that person can never authenticate
again, and nothing deletes, so there is no self-healing path. A read-then-write
guard would narrow the window and leave exactly this outcome reachable.

## No generation suffix, deliberately

The 0.4A claims carry a generation because their keys can be RELEASED and
handed on: a source link ends, a managing clinician transfers. An app identity
is permanent — a subject is bound to one actor for the lifetime of the record,
and there is no product operation that unbinds it.

A generation counter with nothing to advance it would be dead structure that
future readers would assume means something. So the claim id is derived from
the fingerprint alone, and the repository is create-only with no release path.

If subject re-binding is ever needed it must be designed deliberately, with
its own review — not enabled by a counter that happened to be there.

## The stored value is a fingerprint, never the raw subject

A Firebase `auth_subject` is an account identifier. `subject_fingerprint`
stores `sha256(subject)[:32]` so the uniqueness key is derivable but the
document holds no account identifier, and the deterministic claim id contains
only the fingerprint. The raw subject never reaches this collection, an audit
record, or a log line.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .entities import SCHEMA_VERSION, utc_now
from .enums import Visibility
from .roles import ActorRole


class AuthIdentityError(ValueError):
    """Invalid auth-subject claim. PHI-safe, and never echoes the subject."""

    PHI_SAFE_MESSAGE = True


def subject_fingerprint(auth_subject: str) -> str:
    """Stable, non-reversible fingerprint of an auth subject.

    Trimmed but NOT lower-cased: a Firebase uid is case-sensitive, and
    folding case would map two distinct accounts onto one identity — the
    opposite of what this primitive is for. (`key_digest` in 0.4A lower-cases
    because its inputs are application ids and short enum values, which are
    already normalised; that reasoning does not transfer here.)
    """
    subject = (auth_subject or "").strip()
    if not subject:
        raise AuthIdentityError("an auth subject is required")
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:32]


def auth_subject_claim_id(fingerprint: str) -> str:
    """Deterministic document id. The whole uniqueness mechanism.

    Two concurrent bootstraps for one subject compute the same fingerprint,
    therefore the same document id, therefore collide on a single `create` —
    and exactly one survives.
    """
    value = (fingerprint or "").strip()
    if len(value) != 32:
        raise AuthIdentityError("a subject fingerprint must be 32 hex chars")
    return f"authsubj__{value}"


@dataclass(frozen=True)
class AuthSubjectIdentityClaim:
    """Proof that exactly one app identity holds this auth subject."""

    claim_id: str
    #: sha256(auth_subject)[:32]. The raw subject is never stored.
    subject_fingerprint: str
    #: The app identity that won the subject — a cgvr_ or prov_ id.
    holder_actor_id: str
    #: Which KIND of app identity holds it. A subject already held by a
    #: provider must not be bootstrappable as a caregiver, and this is the
    #: field that makes that refusal possible without a second query.
    holder_actor_type: ActorRole
    created_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION

    VISIBILITY = Visibility.SYSTEM_AUDIT

    def __post_init__(self) -> None:
        if not isinstance(self.holder_actor_type, ActorRole):
            raise AuthIdentityError("holder_actor_type must be an ActorRole")
        if not (self.holder_actor_id or "").strip():
            raise AuthIdentityError("a subject claim requires a holder")
        # Recompute rather than trust: a claim whose id does not match its
        # fingerprint would be a document that cannot be found by the lookup
        # that is supposed to enforce uniqueness.
        if self.claim_id != auth_subject_claim_id(self.subject_fingerprint):
            raise AuthIdentityError(
                "claim_id must be derived from the subject fingerprint")

    @property
    def is_held_by_caregiver(self) -> bool:
        return self.holder_actor_type is ActorRole.CAREGIVER

    @property
    def is_held_by_provider(self) -> bool:
        return self.holder_actor_type is ActorRole.PROVIDER

    @staticmethod
    def build(auth_subject: str, *, holder_actor_id: str,
              holder_actor_type: ActorRole,
              now: Optional[datetime] = None) -> "AuthSubjectIdentityClaim":
        """A claim on `auth_subject` for one app identity."""
        fingerprint = subject_fingerprint(auth_subject)
        return AuthSubjectIdentityClaim(
            claim_id=auth_subject_claim_id(fingerprint),
            subject_fingerprint=fingerprint,
            holder_actor_id=holder_actor_id,
            holder_actor_type=holder_actor_type,
            created_at=now or utc_now(),
        )
