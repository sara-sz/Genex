"""0.6A-1 — the activity-bank port and the conversion to planning candidates.

Declared here the same way `gold_standard_source` is: a Protocol in
`pilot_backend`, the live adapter in `pilot_runtime`. `pilot_backend` cannot
read the curated pools — they live in `genex-parent`, which is not importable
from the repository root and must never enter the serving image — so templates
cross the boundary as explicit, already-validated input.

## THE BANK IS NOT A PLANNER

It answers one question: which curated templates exist for these families. It
does not know about children, cycles, capacity, allocations or dates, and it
cannot place anything. Placement stays entirely in the frozen
`weekly/allocator.py`, which is untouched by this slice.

## FAIL CLOSED ON AN UNSERVED FAMILY

`templates_for_families` RAISES when a requested family has no admitted
templates, rather than returning the families it happens to have. A goal whose
anchor binds two families is a clinical statement that both matter; quietly
returning only the covered one would schedule a week that practises half the
goal while reporting full coverage.

That is exactly the live case: the pilot's 24m goal binds
`expressive_vocabulary_growth` (13 curated cards) and `two_word_phrases`
(none). Returning the 13 alone would mean single-word practice for a
two-word-combination target.

## VALIDATION ALREADY HAPPENED

Every template in a bank was checked against the Parent activity validator at
BUILD time. Nothing here re-runs it — the validator lives in `genex-parent`,
needs pandas, and must not exist in the serving image. The runtime's guarantee
is weaker and honest: the templates it serves are the ones the build admitted,
proven by the artifact digest and the CI drift gate.
"""

from __future__ import annotations

from typing import Iterable, Protocol, Sequence, Tuple, runtime_checkable

from ..domain.activity_template import ActivityTemplate


class ActivityBankError(Exception):
    """Base for every refusal from an activity bank. PHI-safe."""

    PHI_SAFE_MESSAGE = True


class FamilyNotServed(ActivityBankError):
    """A requested activity family has no admitted curated templates.

    Distinct from "the bank is empty": this names a real family that the
    taxonomy defines and a clinician's goal binds, for which no reviewed
    content has been supplied yet. The correct response is to refuse to plan,
    not to plan around it.
    """


@runtime_checkable
class ActivityBankSource(Protocol):
    """Obtain curated activity templates. No write operation exists."""

    def templates_for_families(self, families: Sequence[str]
                               ) -> Tuple[ActivityTemplate, ...]:
        """Every admitted template for these families, deterministically ordered.

        Raises `FamilyNotServed` if ANY requested family has none — see the
        module docstring on why a partial answer is worse than a refusal.
        """
        ...

    def served_families(self) -> Tuple[str, ...]:
        """The families this bank can serve, for a fail-closed pre-check."""
        ...


def candidates_for_goal(templates: Iterable[ActivityTemplate], goal_ref,
                        *, allocator_module=None) -> Tuple:
    """Convert curated templates into frozen `CandidateActivity` values.

    ## Why the import is deferred

    `CandidateActivity` lives in `weekly/allocator.py`. Importing it at module
    scope would make the activity-bank port depend on the weekly planner, which
    inverts the intended direction: the planner consumes candidates, the bank
    does not belong to the planner. The parameter exists so a test can inject
    the module without patching imports.

    ## Determinism

    Templates are ordered by `activity_template_id`, which is a content digest
    — so the order depends only on the authored content, never on pool
    position, dict iteration or the order a caller happened to pass them.
    `allocate()` re-sorts by `activity_identity_ref` anyway; sorting here means
    the candidate LIST is already stable before it reaches the planner, which
    is what makes a replay comparison meaningful.

    ## What is deliberately not set

    `difficulty_tier` is left at its default. The curated source has no
    difficulty field, and deriving one from pool position would invent a
    clinical ordering nobody authored. `milestone_refs` is likewise empty: a
    template is bound to a FAMILY, not to a milestone, and the milestone
    linkage already lives on the goal's canonical anchor.
    """
    if allocator_module is None:
        from ..weekly import allocator as allocator_module  # noqa: WPS433

    ordered = sorted(templates, key=lambda t: t.activity_template_id)
    return tuple(
        allocator_module.CandidateActivity(
            activity_identity_ref=template.activity_template_id,
            supports=(goal_ref,),
            primary_for=goal_ref,
            activity_family_ref=template.activity_family_ref,
        )
        for template in ordered
    )


def require_all_families_served(bank: ActivityBankSource,
                                families: Sequence[str]) -> None:
    """Refuse before any planning if a goal binds an unserved family.

    Separated from `templates_for_families` so a caller can ask "could this
    goal be planned at all?" without building candidates — which is what the
    release gate needs.
    """
    served = set(bank.served_families())
    missing = sorted({(f or "").strip() for f in families} - served - {""})
    if missing:
        raise FamilyNotServed(
            "no reviewed activity content exists for: " + ", ".join(missing))
