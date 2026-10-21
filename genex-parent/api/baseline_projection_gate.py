"""Slice 8 — withhold the goal-generating projection, keep the finding.

Canonical design: `pilot_runtime/docs/parent_baseline_v2_design.md`.

## WHAT IS BLOCKED, AND WHAT IS NOT

`TARGET_UNSUPPORTED_BY_CONTENT` means the interview identified a clinically
appropriate target and no approved pilot activity family serves it. Sending
that to A2 v2 would ask the Pilot to generate a goal it cannot build, and the
Pilot's own `require_all_families_served` would be the only thing standing
between the child and a week of activities practising the half of the target we
happen to have content for. Parent refuses first, at the one place every
projection passes through.

What is NOT blocked is the finding. The outcome stays
`TARGET_UNSUPPORTED_BY_CONTENT`, the target band stays the band the evidence
selected, and the unserved families stay named, so the A2 v2.1 clinician
summary can tell a clinician:

    a clinically appropriate target was identified, but there is no approved
    pilot content for it

Four things this module must never do, each of which would destroy that
sentence:

    * manufacture a different, served target
    * silently report `NO_TARGET_IDENTIFIED`
    * silently report `INSUFFICIENT_EVIDENCE`
    * drop the target band or the family names

## ONLY ONE OUTCOME IS WITHHELD

`NO_TARGET_IDENTIFIED` and `INSUFFICIENT_EVIDENCE` project exactly as they do
today. That is deliberate and narrow: the existing A2 v2 contract already
accepts an unresolved record and lets the Pilot decide — `select_target_band`
refuses an incomplete band itself — and there is proven coverage asserting an
unresolved baseline can still be projected. Widening the gate to those two
would change frozen, working behaviour for no clinical gain, since neither can
produce a goal anyway.

## THE DECISION IS DERIVED, NEVER FROZEN

Nothing here is written to the record. `SERVED_FAMILIES` is content state, and
Track 2 is actively adding cards: a target unsupported today becomes supported
the moment its family is served, and the child becomes projectable with no
migration, no re-interview and no backfill.

Freezing the decision would invert that — the record would carry a permanent
"not projectable" stamp that outlived the gap causing it, and Track 2's work
would land without reaching the children waiting for it. The cost is that a
clinician summary for a past session changes when content arrives, which is
correct: it is the same child, the same target, and now we can help.

`served_family_set_digest` travels on every summary and on every refusal, so
which content state produced a given decision is always attributable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from api import baseline_micro_tracks as micro_tracks
from api import baseline_outcome as outcome_mod
from api import baseline_policy as policy_mod

# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------

PROJECTION_ALLOWED = "ALLOWED"
PROJECTION_WITHHELD = "WITHHELD"

#: The only reason a projection is withheld in this slice.
WITHHELD_UNSUPPORTED_BY_CONTENT = "unsupported_by_content"

#: Outcomes that must never reach the goal-generating projection.
WITHHELD_OUTCOMES: Tuple[str, ...] = (
    outcome_mod.OUTCOME_TARGET_UNSUPPORTED_BY_CONTENT,
)


class ProjectionWithheld(Exception):
    """The outcome is real and is not projectable. PHI-safe.

    Carries the clinical finding rather than merely refusing, so a caller can
    render the clinician sentence from the exception alone and has no reason to
    re-derive it — or to guess.
    """

    PHI_SAFE_MESSAGE = True

    def __init__(self, decision: "ProjectionDecision") -> None:
        super().__init__(
            "the baseline outcome is not projectable: "
            f"{decision.withheld_because}")
        self.decision = decision


@dataclass(frozen=True)
class ProjectionDecision:
    """Whether this record may be projected, and the finding either way."""

    status: str
    outcome: str
    withheld_because: Optional[str] = None
    target_band: Optional[int] = None
    target_support: Optional[str] = None
    unsupported_families: Tuple[str, ...] = ()
    unmappable_skills: int = 0
    served_family_set_id: str = micro_tracks.SERVED_FAMILY_SET_ID
    served_family_set_digest: str = ""

    @property
    def allowed(self) -> bool:
        return self.status == PROJECTION_ALLOWED

    def as_dict(self) -> Dict[str, Any]:
        """PHI-safe. Bands, counts, family identifiers and states only."""
        return {
            "status": self.status,
            "outcome": self.outcome,
            "withheld_because": self.withheld_because,
            "target_band": self.target_band,
            "target_support": self.target_support,
            "unsupported_families": list(self.unsupported_families),
            "unmappable_skills": self.unmappable_skills,
            "target_identified": self.target_band is not None,
            "served_family_set_id": self.served_family_set_id,
            "served_family_set_digest": self.served_family_set_digest,
        }


def projection_decision(record, *, policy: Optional[
        policy_mod.BaselinePolicy] = None) -> ProjectionDecision:
    """Derive whether this record may be projected. Mutates nothing.

    The outcome is taken from `derive_outcome` and REPORTED UNCHANGED. This
    function has no path that rewrites it, which is what makes "do not silently
    convert the outcome" structural rather than a promise.
    """
    result = outcome_mod.derive_outcome(record)
    withheld = result.outcome in WITHHELD_OUTCOMES
    return ProjectionDecision(
        status=PROJECTION_WITHHELD if withheld else PROJECTION_ALLOWED,
        outcome=result.outcome,
        withheld_because=(WITHHELD_UNSUPPORTED_BY_CONTENT if withheld
                          else None),
        target_band=result.target_band,
        target_support=result.support,
        unsupported_families=result.unsupported_families,
        unmappable_skills=result.unmappable_skills,
        served_family_set_digest=micro_tracks.served_family_set_digest(),
    )


def assert_projectable(record, *, policy: Optional[
        policy_mod.BaselinePolicy] = None) -> ProjectionDecision:
    """Raise `ProjectionWithheld` if this record must not be projected."""
    decision = projection_decision(record, policy=policy)
    if not decision.allowed:
        raise ProjectionWithheld(decision)
    return decision


__all__ = [
    "PROJECTION_ALLOWED", "PROJECTION_WITHHELD",
    "WITHHELD_UNSUPPORTED_BY_CONTENT", "WITHHELD_OUTCOMES",
    "ProjectionWithheld", "ProjectionDecision",
    "projection_decision", "assert_projectable",
]
