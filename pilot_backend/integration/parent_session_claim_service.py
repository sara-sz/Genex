"""pilot_backend/integration/parent_session_claim_service.py — registration.

The Pilot side of Phase A. Accepts ONE pending Parent-session claim from the
authenticated Parent service and stores it. That is all it does.

## IT CREATES NO IDENTITY

Registering a claim creates no `Child`, no `Caregiver`, no connection and no
source link. It records that a capability exists. Canonical identity is minted
later, in Phase B, by the EXISTING 0.5A bridge, under a Pilot caregiver
principal — because that is where caregiver authority is established, and a
service account has none.

Keeping the two phases apart is what lets the registration endpoint live on a
service with no browser surface and no caregiver authentication at all.

## WHY REGISTRATION IS PARENT -> PILOT AND NOT PILOT -> PARENT

An earlier design had the Pilot call Parent to redeem the token. That was wrong
in two ways, and both matter:

  * it would make canonical child creation depend on Parent being reachable at
    that moment, so a Parent outage would block Pilot identity;
  * it would create a Pilot -> Parent service dependency, inverting the
    one-way Parent -> Pilot direction every other boundary in this system
    maintains.

Parent pushing the pending claim keeps the arrow pointing one way. The Pilot
never calls Parent, and `ParentSessionSource` stays unconfigured.

## IDEMPOTENCY AND THE ONE CONFLICT

The claim document id IS the token digest, so:

    same digest, same session   -> the stored claim is returned, created=False
    same digest, OTHER session  -> ClaimRegistrationConflict

The second case cannot happen by accident — it needs the same 256-bit token to
be minted twice for different sessions — so it is treated as an integrity
failure rather than quietly reconciled. Nothing is overwritten either way.

## REFUSALS ARE PHI-SAFE AND NON-ENUMERATING

Every message is a constant. A claim body carries a session id, and the
registration response says only whether it was accepted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from ..domain.parent_session_claim import (
    CLAIM_TTL_SECONDS,
    ParentSessionClaim,
    ParentSessionClaimError,
)
from ..domain.source_link import SourceSystem
from ..repository.interface import DuplicateRecord

#: Exactly the keys the registration payload may carry. No uid of either
#: system, no clinical field, and no Pilot child id.
REGISTRATION_FIELDS = ("claim_digest", "source_session_id", "ttl_seconds")

#: Field names that must NEVER appear in a registration payload. Checked by
#: name so the refusal is explicit for a reviewer, and separately guaranteed by
#: the exact-key-set check — these are all outside `REGISTRATION_FIELDS`.
FORBIDDEN_REGISTRATION_FIELDS = (
    "claim_token", "token", "raw_token", "secret",
    "parent_uid", "owner_uid", "uid", "auth_subject", "subject", "email",
    "child_id", "caregiver_id", "pilot_uid", "external_owner_ref",
    "child_name", "chronological_months", "age_in_months",
    "diagnosis", "concern", "parent_concern", "qna", "asked",
    "activities", "schedules", "baseline", "functional_baseline",
    "routing_anchor_months", "status",
)


class ClaimRegistrationError(Exception):
    """A pending claim could not be registered. PHI-safe."""

    PHI_SAFE_MESSAGE = True


class ClaimRegistrationConflict(ClaimRegistrationError):
    """A different session is already registered under this digest."""

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class ClaimRegistrationResult:
    """The stored claim, and whether THIS call created it."""

    claim: ParentSessionClaim
    created: bool


def validate_registration_payload(payload: Mapping[str, Any]) -> dict:
    """The registration payload, or a refusal. Exact key set, both ways.

    A missing key and an unexpected key are both refused, for the reason the
    A2 projection validator gives: a payload that is silently tolerated is a
    payload whose shape nobody is actually checking.
    """
    if not isinstance(payload, Mapping):
        raise ClaimRegistrationError("a claim registration object is required")

    present = set(payload)
    forbidden = present & set(FORBIDDEN_REGISTRATION_FIELDS)
    if forbidden:
        # Named as a class, never individually: the names themselves are
        # harmless, but a per-field message would invite a caller to probe.
        raise ClaimRegistrationError(
            "the claim registration contains forbidden fields")
    unexpected = present - set(REGISTRATION_FIELDS)
    if unexpected:
        raise ClaimRegistrationError(
            "the claim registration contains unexpected fields")
    missing = {"claim_digest", "source_session_id"} - present
    if missing:
        raise ClaimRegistrationError("the claim registration is incomplete")

    digest = payload.get("claim_digest")
    session_id = payload.get("source_session_id")
    if not isinstance(digest, str) or not isinstance(session_id, str):
        raise ClaimRegistrationError("the claim registration is malformed")
    digest = digest.strip().lower()
    session_id = session_id.strip()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        # The digest is a sha256 hex string. Anything else cannot have come
        # from `claim_digest`, so it is refused before touching storage.
        raise ClaimRegistrationError("the claim registration is malformed")
    if not session_id:
        raise ClaimRegistrationError("the claim registration is incomplete")

    ttl = payload.get("ttl_seconds", CLAIM_TTL_SECONDS)
    if isinstance(ttl, bool) or not isinstance(ttl, int):
        # `bool` is an `int` in Python, and `True` would become a 1-second TTL.
        raise ClaimRegistrationError("the claim registration is malformed")
    if ttl <= 0 or ttl > CLAIM_TTL_SECONDS:
        # The ceiling is enforced HERE as well as in the domain, so the Pilot
        # never depends on Parent having applied it.
        raise ClaimRegistrationError("the claim ttl is not permitted")

    return {"claim_digest": digest, "source_session_id": session_id,
            "ttl_seconds": ttl}


class ParentSessionClaimRegistrationService:
    """Register one pending Parent-session claim. Create-only."""

    def __init__(self, *, repos: Any,
                 now: Optional[Callable[[], datetime]] = None) -> None:
        self._repos = repos
        self._now = now or (lambda: datetime.now(timezone.utc))

    def register(self, payload: Mapping[str, Any]) -> ClaimRegistrationResult:
        validated = validate_registration_payload(payload)
        digest = validated["claim_digest"]
        session_id = validated["source_session_id"]

        # --- idempotent replay -------------------------------------------
        #
        # Checked before constructing anything: Parent retrying a registration
        # it already completed is a normal outcome of a network timeout, and it
        # must not become a conflict.
        existing = self._repos.parent_session_claims.find(digest)
        if existing is not None:
            if existing.source_session_id != session_id:
                raise ClaimRegistrationConflict(
                    "a claim is already registered for a different session")
            return ClaimRegistrationResult(claim=existing, created=False)

        try:
            claim = ParentSessionClaim.issue(
                digest, session_id, now=self._now(),
                ttl_seconds=validated["ttl_seconds"])
        except ParentSessionClaimError as exc:
            raise ClaimRegistrationError(
                "the claim registration is malformed") from exc

        try:
            stored = self._repos.parent_session_claims.create(claim)
        except DuplicateRecord:
            # Another registration of the same token landed between the read
            # and the create. Re-read and converge rather than assume it
            # matches: a different session under this digest must still fail.
            winner = self._repos.parent_session_claims.find(digest)
            if winner is None:  # pragma: no cover - defensive
                raise ClaimRegistrationError(
                    "the claim could not be registered") from None
            if winner.source_session_id != session_id:
                raise ClaimRegistrationConflict(
                    "a claim is already registered for a different session"
                ) from None
            return ClaimRegistrationResult(claim=winner, created=False)

        return ClaimRegistrationResult(claim=stored, created=True)
