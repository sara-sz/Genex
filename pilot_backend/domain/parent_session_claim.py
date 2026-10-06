"""pilot_backend/domain/parent_session_claim.py — the handoff capability.

A `ParentSessionClaim` is a PENDING, single-use capability that says one thing:

    "the holder of the token behind this digest owns Parent session S"

It is how a Parent session reaches a canonical Pilot child WITHOUT the Pilot
ever reading Parent storage and WITHOUT either system learning the other's user
identifier.

## WHY A CAPABILITY AND NOT AN IDENTITY MAPPING

Parent authenticates in one Firebase directory, the Pilot in another, and a
Firebase uid is scoped to a single project's user directory. So the 0.5A bridge
cannot work: it compares the Parent document's `owner_uid` against the Pilot's
verified subject, and those two values are drawn from different directories and
can never be equal.

Federating them would mean storing "Parent uid X is Pilot uid Y", which is
exactly the cross-system identity join this architecture forbids.

A capability sidesteps it. The token names a SESSION, never a person. Parent
proves ownership in its own directory before minting; the Pilot establishes
caregiver authority in its own directory when redeeming. Neither uid crosses,
and nothing stored here could reconstruct one.

## WHAT IS STORED, AND WHAT IS DELIBERATELY NOT

Stored: the token's DIGEST, the source system, the session id, the issued and
expiry stamps, and a schema version. Six fields.

NOT stored, and not accepted:

  * the raw token         - a stolen database read would otherwise be a stolen
                            capability; only a preimage can be redeemed
  * any Parent uid        - it never crosses the boundary
  * any Pilot uid         - the redeemer is authenticated separately, and
                            binding the claim to one in advance would require
                            knowing it at mint time, which is the federation
                            this design exists to avoid
  * external_owner_ref    - provenance-only, everywhere
  * any clinical content  - this is identity bootstrap; a baseline, diagnosis,
                            concern or answer has no business in it

The digest is domain-separated so a value from this namespace can never be
confused with a `key_digest`, a rung ref or a baseline record digest, even if
some future code hashes the same bytes.

## SINGLE USE IS ENFORCED BY A CLAIM, NOT BY A FLAG

Consumption does NOT mutate this record. A `ClaimKind.PARENT_SESSION_CLAIM`
identity claim is created instead, in the same transaction as the child, the
connection and the source link — so the capability is spent exactly when, and
only when, the bridge actually commits.

A mutable `consumed` boolean would be a read-then-write guard, which is the
defect `identity_claims.py` documents at length: right almost always, wrong
exactly when two writers act at once. Two redemptions of one token would then
produce two canonical children.

## EXPIRY IS A CEILING ON THE HANDOFF, NOT A SESSION LIFETIME

`CLAIM_TTL_SECONDS` covers one browser handoff: the caregiver finishes in the
Parent app and arrives in the Pilot app, possibly signing in on the way. Ten
minutes allows for that sign-in detour and still leaves a stolen token useful
for only a few minutes. It is a compiled-in constant rather than configuration,
so no deployment can widen it.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .entities import SCHEMA_VERSION, utc_now
from .source_link import SourceSystem

#: Domain separation for the token digest. Any change invalidates every
#: outstanding token, which is the correct behaviour for a credential format.
CLAIM_DIGEST_DOMAIN = "genex-parent-session-claim-v1"

#: The schema stamp written on every stored claim.
CLAIM_SCHEMA_VERSION = "parent-session-claim-v1"

#: Bytes of entropy in a raw token. 32 bytes = 256 bits, which `token_urlsafe`
#: renders as 43 URL-safe characters. Far beyond guessable for a credential
#: that is also single-use and expires in minutes.
CLAIM_TOKEN_BYTES = 32

#: The handoff window. See the module docstring: one browser handoff including a
#: possible Pilot sign-in. NOT configurable — a deployment must not be able to
#: widen a credential's lifetime.
CLAIM_TTL_SECONDS = 600

#: A redeemable token is at least this long. A shorter string cannot be one of
#: ours, so it is refused before any storage lookup happens.
MIN_TOKEN_LENGTH = 32


class ParentSessionClaimError(ValueError):
    """A claim could not be constructed. PHI-safe: never quotes a token."""

    PHI_SAFE_MESSAGE = True


def generate_claim_token() -> str:
    """A fresh opaque capability token from the OS CSPRNG.

    `secrets.token_urlsafe` draws from `os.urandom`. Not `random`, which is a
    Mersenne Twister and reconstructible from its output.

    Lives in the domain so BOTH systems share one definition of what a token
    is, even though only Parent calls it.
    """
    return secrets.token_urlsafe(CLAIM_TOKEN_BYTES)


def claim_digest(token: str) -> str:
    """The stored, transmittable digest of a raw token.

    Domain-separated with a NUL, the same discipline `key_digest` uses: the
    separator cannot appear in a token, so no other digest in the system can
    collide with one from this namespace.

    Full 64 hex characters, not truncated. There is no document-id length
    pressure here, and a credential lookup key is the last place to shorten a
    hash.
    """
    raw = (token or "").strip()
    if len(raw) < MIN_TOKEN_LENGTH:
        # Checked here rather than at the call site so every entry point —
        # mint, register and consume — applies the same rule.
        raise ParentSessionClaimError("a claim token is required")
    joined = f"{CLAIM_DIGEST_DOMAIN}\x00{raw}".encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


@dataclass(frozen=True)
class ParentSessionClaim:
    """A pending, single-use Parent-session capability. Immutable.

    Frozen and never updated in place: `issue` is the only constructor, and
    there is no `with_consumed`, `expire` or `revoke` method. The absence of a
    mutator is what makes the consumption claim the only possible record of
    redemption.
    """

    claim_digest: str
    source_system: SourceSystem
    source_session_id: str
    issued_at: datetime
    expires_at: datetime
    schema_version: str = CLAIM_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if len(self.claim_digest or "") != 64:
            raise ParentSessionClaimError("a claim digest is required")
        if any(c not in "0123456789abcdef" for c in self.claim_digest):
            # Lower-case hex only. A digest that is not the output of
            # `claim_digest` cannot address a document this system wrote.
            raise ParentSessionClaimError("a claim digest is required")
        if not (self.source_session_id or "").strip():
            raise ParentSessionClaimError("a source session id is required")
        if self.source_system is not SourceSystem.PARENT:
            # 0.5F-A3 is the Parent handoff only. Another source system would
            # need its own ownership proof, so it is refused rather than
            # assumed to work the same way.
            raise ParentSessionClaimError("only Parent claims are supported")
        if self.expires_at <= self.issued_at:
            raise ParentSessionClaimError("a claim must expire after issuance")

    @staticmethod
    def issue(token_digest: str, source_session_id: str, *,
              now: Optional[datetime] = None,
              ttl_seconds: int = CLAIM_TTL_SECONDS) -> "ParentSessionClaim":
        """A pending claim over an ALREADY-HASHED token.

        Takes the digest, never the token: this constructor cannot be the place
        a raw token is accidentally persisted, because it never sees one.
        """
        if ttl_seconds <= 0 or ttl_seconds > CLAIM_TTL_SECONDS:
            # A caller may shorten the window but never extend it. An unbounded
            # or longer TTL is the one mistake that turns a handoff credential
            # into a durable one.
            raise ParentSessionClaimError(
                "a claim ttl must be positive and no longer than the maximum")
        stamp = now or utc_now()
        return ParentSessionClaim(
            claim_digest=token_digest,
            source_system=SourceSystem.PARENT,
            source_session_id=(source_session_id or "").strip(),
            issued_at=stamp,
            expires_at=stamp + timedelta(seconds=ttl_seconds),
            schema_version=CLAIM_SCHEMA_VERSION,
        )

    def is_expired_at(self, now: datetime) -> bool:
        """Expiry is INCLUSIVE of the boundary: `expires_at` is already too late.

        A credential whose validity ends at T should not be redeemable at
        exactly T. The strict comparison is on the valid side, so the test for
        expiry is `>=`.
        """
        return now >= self.expires_at
