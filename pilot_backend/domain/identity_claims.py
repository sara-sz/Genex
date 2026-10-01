"""pilot_backend/domain/identity_claims.py — write-time uniqueness primitives.

The 0.3 auth-subject defect was an ambiguous identity mapping discovered only
at READ time, after two records already shared one subject. This module exists
so the same class of defect cannot occur for longitudinal identity: the
uniqueness constraint is enforced by the WRITE itself.

## How, without a transaction

`DocumentStore` offers no transaction — only `create`, `get`, `set`,
`query_equals`, `list_all`. That is sufficient, because `create` is a genuine
atomic compare-and-set from "absent" to "present": Firestore raises
`AlreadyExists`, which the adapter translates to `DuplicateRecord`.

So each uniqueness constraint gets a CLAIM DOCUMENT whose id is derived
deterministically from the constraint key. Two concurrent writers computing the
same key produce the same document id and race on a single document; exactly
one `create` succeeds. No read-then-write, and therefore no window.

## Generation must come from RELEASES, never from a live claim count

`create` is a non-releasable mutex: once the document exists, nobody else can
take it. Making it releasable needs a generation suffix that every contender
computes IDENTICALLY — otherwise they target different documents and all
succeed.

An earlier version of this module derived the generation from the number of
existing claims. A real Firestore emulator with genuinely concurrent threads
proved that wrong within one test run: thread A read count 0 and claimed
generation 0; thread B read count 1 — because A had already written — and
claimed generation 1. They never collided, and BOTH acquired the same logical
key. Two winners is precisely the defect this module exists to prevent.

The generation therefore counts RELEASE markers, which only ever advance when
a holder deliberately releases. A competitor winning a race does not move the
counter, so every contender computes the same generation and collides on one
document. Exactly one survives.

    generation(key) = number of release markers for that key
    claim id        = <kind>__<digest>__g<generation>
    release id      = release__<kind>__<digest>__g<generation>

Both are create-only and deterministic, so a double release is as impossible
as a double claim.

## Releases also recover the two-claim orphan

A source link needs two claims and two `create` calls cannot be atomic
together. If the second fails, the service releases the first. That advances
only the first key's generation, so the next attempt proceeds normally and no
key is ever permanently blocked.

## Nothing is deleted

Releasing does not remove a claim; it adds a marker beside it. The claim and
release series together are the permanent record of how a key was contended
and handed on.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Sequence

from .entities import SCHEMA_VERSION, utc_now


class ClaimRecordKind(str, Enum):
    """A claim acquires a key; a release hands it back."""

    CLAIM = "claim"
    RELEASE = "release"


class ClaimKind(str, Enum):
    """The uniqueness constraints that are enforced at write time."""

    #: One ACTIVE source link per (child_id, source_system).
    CHILD_SOURCE = "child_source"
    #: One ACTIVE canonical child per (source_system, external_id).
    EXTERNAL_IDENTITY = "external_identity"
    #: One ACTIVE managing clinician per child_id.
    MANAGING_CLINICIAN = "managing_clinician"
    #: 0.4F. One active/finalized monitoring period per
    #: (episode_id, cycle_month). Two periods for one episode-month would
    #: give the month two sets of documented minutes and two coding
    #: summaries, with nothing saying which the clinician confirmed.
    RTM_MONITORING_PERIOD = "rtm_monitoring_period"
    #: 0.4C. One ACTIVE monthly focus plan per (child_id, cycle_month).
    #: The same mechanism as the 0.4A constraints and for the same reason:
    #: two clinicians activating October's plan in the same second must not
    #: both succeed, or the child has two competing months of direction and
    #: nothing in the record says which one the weekly plans followed.
    MONTHLY_FOCUS_PLAN = "monthly_focus_plan"
    #: 0.4D. One weekly cycle per (focus_plan_id, sequence_in_month), and one
    #: allocation per cycle_id.
    #:
    #: Both were read-then-write guards in a first pass, which is the 0.3
    #: auth-subject defect one layer up: right almost always, wrong exactly
    #: when two planners act together. Two cycle-1s for one month, or two
    #: allocations for one cycle, would give the family a duplicated week with
    #: nothing in the record saying which one was real.
    #:
    #: The allocation claim is stronger than the 0.4C plan claim because every
    #: write it guards is a `create` — claim, alignments, gaps and ledger all
    #: commit in ONE transaction, with no `set` and therefore no boundary to
    #: recover across.
    WEEKLY_CYCLE = "weekly_cycle"
    WEEKLY_ALLOCATION = "weekly_allocation"


class ClaimError(ValueError):
    """A claim could not be constructed. PHI-safe: names the kind, not values."""

    PHI_SAFE_MESSAGE = True


def key_digest(*parts: str) -> str:
    """Stable digest of a normalized constraint key.

    Parts are stripped, lower-cased and NUL-joined. NUL is used because it
    cannot appear in any identifier we accept, so ("a", "bc") and ("ab", "c")
    can never collide — a plain concatenation or a hyphen join would let them.
    """
    normalized = []
    for part in parts:
        value = (part or "").strip().lower()
        if not value:
            raise ClaimError("claim key parts must be non-empty")
        normalized.append(value)
    joined = "\x00".join(normalized).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()[:32]


def claim_document_id(kind: ClaimKind, digest: str, generation: int) -> str:
    """Deterministic claim id. The same key and generation always collide.

    That determinism is the whole mechanism: it is what makes two concurrent
    writers target one document instead of creating two.
    """
    if generation < 0:
        raise ClaimError("claim generation must not be negative")
    return f"{kind.value}__{digest}__g{generation}"


def release_document_id(kind: ClaimKind, digest: str, generation: int) -> str:
    """Deterministic release id for the claim at this generation."""
    return f"release__{claim_document_id(kind, digest, generation)}"


@dataclass(frozen=True)
class IdentityClaim:
    """Proof that exactly one writer won a uniqueness race."""

    claim_id: str
    record_kind: ClaimRecordKind
    kind: ClaimKind
    key_digest: str
    generation: int
    #: The record this claim was won for — a link id or an assignment id.
    holder_ref: str
    child_id: str
    created_at: datetime = field(default_factory=utc_now)
    created_by_actor_id: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    @staticmethod
    def build(kind: ClaimKind, parts: Sequence[str], generation: int, *,
              holder_ref: str, child_id: str,
              actor_id: Optional[str] = None,
              now: Optional[datetime] = None) -> "IdentityClaim":
        """A claim on `parts` at `generation`."""
        digest = key_digest(*parts)
        return IdentityClaim(
            claim_id=claim_document_id(kind, digest, generation),
            record_kind=ClaimRecordKind.CLAIM,
            kind=kind,
            key_digest=digest,
            generation=generation,
            holder_ref=holder_ref,
            child_id=child_id,
            created_at=now or utc_now(),
            created_by_actor_id=actor_id,
        )

    @staticmethod
    def build_release(kind: ClaimKind, digest: str, generation: int, *,
                      holder_ref: str, child_id: str,
                      actor_id: Optional[str] = None,
                      now: Optional[datetime] = None) -> "IdentityClaim":
        """The release marker that frees `generation` and opens the next one."""
        return IdentityClaim(
            claim_id=release_document_id(kind, digest, generation),
            record_kind=ClaimRecordKind.RELEASE,
            kind=kind,
            key_digest=digest,
            generation=generation,
            holder_ref=holder_ref,
            child_id=child_id,
            created_at=now or utc_now(),
            created_by_actor_id=actor_id,
        )
