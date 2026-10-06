"""pilot_backend/domain/suggestion_generation.py — the generation claim.

0.5F-B turns an immutable `ParentBaselineProjection` into a deterministic
anchored `GoalSuggestion`. This module holds the two things that make that
repeatable: the POLICY VERSION the algorithm is pinned to, and the CLAIM that
makes one generation happen exactly once.

## WHY A DEDICATED CLAIM AND NOT `IdentityClaim`

`IdentityClaim` is the write-time mutex for IDENTITY uniqueness — one canonical
child per external id, one managing clinician per child. Its `ClaimKind`
members are all identity or plan constraints, and every one carries a
`child_id` and a `holder_ref` meaning "the record this claim was won for".

Generation is not an identity constraint. Borrowing that abstraction purely for
its collide-on-create behaviour would make `ClaimKind` a grab-bag of anything
that wanted a mutex, and the next reader would have no way to tell which
members are identity invariants and which are merely convenient. So this is its
own create-only record, implementing the same proven claims-first pattern
without overloading the meaning of the other one.

## THE KEY IS THE CLINICAL INPUTS, NEVER THE REQUESTER

    projection_id          the immutable baseline that caused it
    generation_policy      this module's version — the algorithm itself
    taxonomy_version       which activity-family taxonomy resolved the families
    gold_standard_version  which Gold Standard baseline version supplied the rung
    domain_key            the canonical domain
    target_rung_ref       the resolved canonical target

The provider who triggered generation is DELIBERATELY absent. Hannah requests
generation; she does not decide its clinical target. If her identity were in
the key, two authorized clinicians opening the same child would generate two
parallel candidate sets for one baseline — and whichever she saw first would
look like Genex's recommendation.

The target rung ref IS included, and that is not redundant: it makes the key
describe the decision rather than only its inputs, so a future change in how a
target is chosen produces a visibly different key instead of silently reusing
an old generation.

## A NEW VERSION LEGITIMATELY REGENERATES

Because the policy, taxonomy and Gold Standard versions are all in the key, a
later version of any of them yields a different key and may generate again. That
is intended: a changed algorithm SHOULD be able to offer a changed suggestion.
What is forbidden is the same inputs producing two results, which is what the
create-only document id prevents.

## IMMUTABLE, AND NO `generated` FLAG

There is no mutable boolean here and no setter. A `generated=true` field would
be a read-then-write guard — right almost always, wrong exactly when two
requests arrive together, which is the case that matters. The claim's existence
IS the record, and it is written in the same transaction as the suggestion and
the anchor it authorises.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple

from .entities import SCHEMA_VERSION, utc_now

#: The pinned version of the 0.5F-B generation algorithm: validate the
#: projection, step one rung up the declared track from the observed floor,
#: refuse above the not-demonstrated ceiling, require activity-mappability.
#:
#: Explicit rather than implicit so a change to any of those rules is a visible
#: version bump that produces a different generation key.
GENERATION_POLICY_VERSION = "goal-suggestion-generation-policy-v1"

#: Baseline statuses 0.5F-B may generate from.
#:
#: Derived from the FROZEN Parent semantics, not chosen here: `finalize()` sets
#: `routing_anchor_months = None` for exactly UNRESOLVED and CONTRADICTORY, and
#: those are the two states `baseline_is_unresolved()` reports. Every other
#: valid status resolved to a floor, which is the only input target selection
#: needs.
GENERATABLE_STATUSES: Tuple[str, ...] = ("BOUNDED", "AGE_RELEVANT", "EMERGING")

#: Statuses that must fail closed. Listed explicitly so the refusal is a stated
#: decision rather than the absence of an entry above.
NON_GENERATABLE_STATUSES: Tuple[str, ...] = ("UNRESOLVED", "CONTRADICTORY")

#: Domain separation for the generation key, so a value from this namespace can
#: never collide with a rung ref, a claim digest or a baseline record digest.
GENERATION_KEY_DOMAIN = "genex-suggestion-generation-v1"

CLAIM_ID_PREFIX = "gsgc"


class SuggestionGenerationError(Exception):
    """Generation could not proceed. PHI-safe: never quotes a milestone."""

    PHI_SAFE_MESSAGE = True


class BaselineNotGeneratable(SuggestionGenerationError):
    """The projected baseline cannot produce a target.

    UNRESOLVED or CONTRADICTORY, or no routing anchor. Refused rather than
    planned from: the whole point of the Parent 0.4 work was that these states
    must not silently become a starting level.
    """


class TargetNotResolvable(SuggestionGenerationError):
    """No safe next rung exists.

    The top of the declared track, or a step that would land above the
    not-demonstrated ceiling. Refused rather than nudged: skipping forward to
    find a convenient rung is exactly what the frozen rules forbid.
    """


class TargetNotMappable(SuggestionGenerationError):
    """The resolved target has no usable activity-family binding.

    One of the intentionally unresolved declared-SLP cases, or a rung whose
    families the taxonomy does not define. A suggestion generated here would
    become an unmappable ClinicalGoal after approval, so it is refused at
    generation instead of discovered at allocation.
    """


class ProjectionLineageInvalid(SuggestionGenerationError):
    """The projection cannot be tied to this child through a source link.

    Zero or ambiguous active Parent source links, zero or ambiguous applicable
    projections, or a projection naming a different child. Never resolved by
    picking one.
    """


def generation_key(*, projection_id: str, domain_key: str,
                   target_rung_ref: str, taxonomy_version: str,
                   gold_standard_version: str,
                   generation_policy: str = GENERATION_POLICY_VERSION) -> str:
    """The deterministic identity of one generation. No actor, no timestamp.

    NUL-joined after a domain tag, the same discipline `key_digest` and
    `claim_digest` use: the separator cannot occur in any of these values, so
    two different input tuples cannot render to the same string.
    """
    parts = (projection_id, generation_policy, taxonomy_version,
             gold_standard_version, domain_key, target_rung_ref)
    for value in parts:
        if not (value or "").strip():
            raise SuggestionGenerationError(
                "a generation key requires every immutable input")
    joined = "\x00".join([GENERATION_KEY_DOMAIN, *(p.strip() for p in parts)])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def generation_claim_id(key: str) -> str:
    """The deterministic document id. Two writers compute the same one."""
    if len(key or "") != 64:
        raise SuggestionGenerationError("a generation key is required")
    return f"{CLAIM_ID_PREFIX}_{key[:32]}"


@dataclass(frozen=True)
class GoalSuggestionGenerationClaim:
    """Proof that one deterministic generation happened. Create-only.

    Frozen, with no mutator: there is no `with_result`, `complete` or
    `invalidate`. The claim is written in the same transaction as the
    suggestion and the anchor, so its presence already means they exist.

    `suggestion_ids` is the lineage the founder asked for — it lets a reader go
    from the immutable projection to the exact suggestions it caused without
    copying any part of the Parent baseline.
    """

    claim_id: str
    generation_key: str
    #: The immutable projection that caused this generation.
    projection_id: str
    child_id: str
    domain_key: str
    #: The canonical target the frozen algorithm resolved.
    target_rung_ref: str
    target_rung_months: int
    generation_policy: str
    taxonomy_version: str
    gold_standard_version: str
    #: The suggestions this generation produced. Minimum-necessary lineage.
    suggestion_ids: Tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utc_now)
    #: WHO ASKED, recorded for audit only. Never part of the key — see the
    #: module docstring. A different authorized provider asking for the same
    #: generation converges on this same claim.
    requested_by_actor_id: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if len(self.generation_key or "") != 64:
            raise SuggestionGenerationError("a generation key is required")
        if self.claim_id != generation_claim_id(self.generation_key):
            # Recomputed and compared, so a hand-built claim with a mismatched
            # id cannot be stored and then found by a key it does not belong to.
            raise SuggestionGenerationError(
                "the claim id is not derived from its generation key")
        for name in ("projection_id", "child_id", "domain_key",
                     "target_rung_ref", "generation_policy",
                     "taxonomy_version", "gold_standard_version"):
            if not (getattr(self, name) or "").strip():
                raise SuggestionGenerationError(
                    "a generation claim requires its immutable inputs")
        if not isinstance(self.target_rung_months, int) or \
                isinstance(self.target_rung_months, bool):
            raise SuggestionGenerationError(
                "a generation claim requires integer target months")
        if self.target_rung_months <= 0:
            raise SuggestionGenerationError(
                "a generation claim requires positive target months")
        object.__setattr__(self, "suggestion_ids", tuple(self.suggestion_ids))

    @staticmethod
    def build(*, projection_id: str, child_id: str, domain_key: str,
              target_rung_ref: str, target_rung_months: int,
              taxonomy_version: str, gold_standard_version: str,
              suggestion_ids: Tuple[str, ...] = (),
              requested_by_actor_id: Optional[str] = None,
              generation_policy: str = GENERATION_POLICY_VERSION,
              now: Optional[datetime] = None) -> "GoalSuggestionGenerationClaim":
        key = generation_key(
            projection_id=projection_id, domain_key=domain_key,
            target_rung_ref=target_rung_ref, taxonomy_version=taxonomy_version,
            gold_standard_version=gold_standard_version,
            generation_policy=generation_policy)
        return GoalSuggestionGenerationClaim(
            claim_id=generation_claim_id(key),
            generation_key=key,
            projection_id=projection_id,
            child_id=child_id,
            domain_key=domain_key,
            target_rung_ref=target_rung_ref,
            target_rung_months=target_rung_months,
            generation_policy=generation_policy,
            taxonomy_version=taxonomy_version,
            gold_standard_version=gold_standard_version,
            suggestion_ids=tuple(suggestion_ids),
            created_at=now or utc_now(),
            requested_by_actor_id=requested_by_actor_id)


def is_generatable_status(status: str) -> bool:
    """Whether a projected baseline status may produce a target at all."""
    return (status or "").strip() in GENERATABLE_STATUSES


def target_within_ceiling(target_months: int,
                          not_demonstrated_months: Optional[int]) -> bool:
    """Whether a candidate target is at or below the known ceiling.

    INCLUSIVE, and that is the frozen Parent semantics rather than a choice
    made here: `_ceiling_months()` returns the LOWEST rung explicitly NOT
    demonstrated, so the ceiling IS the first unachieved rung and is therefore
    the legitimate next target. A target ABOVE it would skip a rung the child
    has already been shown not to have.

    No ceiling (AGE_RELEVANT, EMERGING without a failed rung) means nothing to
    bound against, so any resolvable step is allowed.
    """
    if not_demonstrated_months is None:
        return True
    return target_months <= not_demonstrated_months


def projection_cycle_month(projection) -> str:
    """The cycle month a generation belongs to: the PROJECTION's own month.

    Derived from `projected_at`, never from "now". A container clock crossing a
    month boundary would otherwise file the same immutable baseline under two
    different months depending on when the clinician happened to open the child
    — and the two generations would then both look legitimate.

    The cycle month is deliberately NOT part of the generation key. It is a
    function of an input that already is, so including it would add nothing and
    would make the key look as though timing mattered.
    """
    stamp = getattr(projection, "projected_at", None)
    if stamp is None:
        raise SuggestionGenerationError(
            "a projection must carry its own timestamp")
    return f"{stamp.year:04d}-{stamp.month:02d}"
