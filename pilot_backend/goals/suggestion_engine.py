"""pilot_backend/goals/suggestion_engine.py — deterministic. No model, no egress.

Same observations in, same suggestions out, every time, offline.

## Why this is rules and not a language model

A goal is the thing a child's month is organised around and the thing an RTM
episode is later measured against. Three properties matter more than fluency:

    reproducible   the same snapshot yields the same ranking, forever
    explainable    every suggestion names the evidence that produced it
    offline        no child observation leaves the process

A model satisfies none of them reliably, and the third not at all. There is no
network client in this module, no prompt, and no place for one — `generation_mode`
on `GoalSuggestion` is stamped "deterministic" so that if an LLM-assisted
WORDING mode is ever added it is a visible, auditable difference in the stored
record rather than a silent change of meaning.

## Wording is CHOSEN, never composed

Family-facing text comes from `DOMAIN_TEMPLATES`, a fixed catalogue. No input
value is interpolated into it, so no observation, note, area name or label can
reach the stored text. A domain with no catalogue entry produces no suggestion
rather than a generic sentence — fail closed.

Every template carries `CHILD_PLACEHOLDER` and no name: `pilot_backend` has
been name-blind since 0.1 and a suggestion is not the place to end that.

## The Parent invariants, as code

    chronological age is CONTEXT, not a starting level
        -> age is not a parameter of this module at all. There is nothing to
           misuse. A domain is suggested because something was observed, never
           because of how old the child is.

    observed ability is the starting level
        -> ranking reads `ObservedDomain` fields only.

    diagnosis does not override observation
        -> `EvidenceSource` has no diagnosis member, so a diagnosis has no
           representable slot in the evidence that drives ranking.

    a no-answer is NOT an answer
        -> `answered=False` is skipped entirely. It is never defaulted to a
           level, an age, or a zero score. An unanswered domain is unknown, and
           unknown is not the same as delayed.

    Sensory evidence is never invented
        -> Parent 2.4 shipped `sensory` with no curated milestone content, and
           this engine fabricates nothing: it consumes supplied observations.
           So a sensory suggestion appears only where a human actually observed
           or selected something, and never from milestone evidence that does
           not exist.

## Ranking

A score built from named, documented components, then a stable tie-break. The
score is not a clinical severity measure and is not stored as one — it exists
to make an ORDER reproducible, and the evidence attached to each suggestion is
what a human actually reviews.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional, Tuple

from ..domain.goal_vocabulary import (
    CANONICAL_DOMAIN_KEYS,
    domain_rank,
    require_canonical_domain,
)
from ..domain.goals import (
    CHILD_PLACEHOLDER,
    EvidenceSource,
    GoalError,
    GoalSuggestion,
    GoalSuggestionEvidence,
)
from ..domain.monthly_plan import validate_cycle_month
from ..domain.planning_policy import CURRENT_PLANNING_POLICY, PlanningPolicyVersion

#: Bumped whenever ranking or wording changes. Stored on every suggestion, so
#: a suggestion made in October stays explainable after the rules move on.
SUGGESTION_RULE_VERSION = "suggestion-rules-2026.10"
GENERATOR_VERSION = "goal-suggestion-engine-2026.10"

#: Score components. Named and additive so a ranking can be explained in one
#: sentence rather than reverse-engineered from a magic number.
SCORE_EXPLICIT_SELECTION = 100
SCORE_CLINICIAN_OBSERVATION = 40
SCORE_FUNCTIONAL_BASELINE = 25
SCORE_PER_MILESTONE = 10
SCORE_MILESTONE_CAP = 30
SCORE_PRIOR_CYCLE_CONTINUITY = 15

#: The only text this engine can produce. Fixed strings, no interpolation.
DOMAIN_TEMPLATES: Mapping[str, str] = {
    "talking_and_communicating":
        "Help {child} combine words to ask for what they want during "
        "everyday routines.",
    "social_and_emotional":
        "Help {child} take short back-and-forth turns with a familiar adult "
        "during play.",
    "learning_and_thinking":
        "Help {child} follow a simple two-step instruction in a familiar "
        "routine.",
    "fine_motor":
        "Help {child} use both hands together to manage small objects during "
        "daily activities.",
    "gross_motor":
        "Help {child} move confidently between positions during active play.",
    "daily_living":
        "Help {child} take part in one step of a daily self-care routine with "
        "less help.",
    "sensory":
        "Help {child} stay comfortable and engaged during a familiar daily "
        "routine.",
}


class SuggestionEngineError(ValueError):
    """The engine refused to produce suggestions. PHI-safe."""

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class ObservedDomain:
    """One domain's observed state. The ONLY input to ranking."""

    domain_key: str
    #: False means nobody answered. Skipped, never defaulted to a level.
    answered: bool
    evidence_source: EvidenceSource
    milestone_refs: Tuple[str, ...] = ()
    functional_baseline_area: str = ""
    observed_level: str = ""
    explicitly_selected: bool = False
    prior_month_summary_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain_key",
                           require_canonical_domain(self.domain_key))
        if not isinstance(self.evidence_source, EvidenceSource):
            raise SuggestionEngineError("evidence_source must be an EvidenceSource")
        object.__setattr__(self, "milestone_refs", tuple(self.milestone_refs))

    @property
    def has_support(self) -> bool:
        """Whether anything was actually observed or chosen.

        Mirrors `GoalSuggestionEvidence`'s rule rather than restating a weaker
        one: an unsupported domain is dropped here instead of failing later.
        """
        return bool(self.milestone_refs or self.functional_baseline_area
                    or self.explicitly_selected)


@dataclass(frozen=True)
class ObservationSnapshot:
    """Everything the engine is allowed to see about one child-month.

    Deliberately holds no age, no diagnosis, no name and no free text. What is
    absent from this structure cannot influence a suggestion.
    """

    child_id: str
    cycle_month: str
    domains: Tuple[ObservedDomain, ...]

    def __post_init__(self) -> None:
        if not (self.child_id or "").strip():
            raise SuggestionEngineError("a snapshot requires a child id")
        object.__setattr__(self, "cycle_month",
                           validate_cycle_month(self.cycle_month))
        object.__setattr__(self, "domains", tuple(self.domains))
        seen = set()
        for observed in self.domains:
            if observed.domain_key in seen:
                raise SuggestionEngineError(
                    f"duplicate observation for domain {observed.domain_key}")
            seen.add(observed.domain_key)


def evidence_score(observed: ObservedDomain) -> int:
    """Additive, explainable support score. Not a severity measure."""
    score = 0
    if observed.explicitly_selected:
        score += SCORE_EXPLICIT_SELECTION
    if observed.evidence_source is EvidenceSource.CLINICIAN_OBSERVATION:
        score += SCORE_CLINICIAN_OBSERVATION
    if observed.functional_baseline_area:
        score += SCORE_FUNCTIONAL_BASELINE
    if observed.milestone_refs:
        score += min(len(set(observed.milestone_refs)) * SCORE_PER_MILESTONE,
                     SCORE_MILESTONE_CAP)
    if observed.prior_month_summary_id:
        score += SCORE_PRIOR_CYCLE_CONTINUITY
    return score


def rank_key(observed: ObservedDomain) -> Tuple[int, int, str]:
    """Total order over candidates. Fully determined by the observation.

    Higher score first, then the canonical domain order, then the key itself.
    The final component makes the order total even if the tie-break table ever
    gains two entries at the same index — a sort that is only ALMOST total is
    a sort whose output depends on input order.
    """
    return (-evidence_score(observed), domain_rank(observed.domain_key),
            observed.domain_key)


def eligible_domains(snapshot: ObservationSnapshot) -> Tuple[ObservedDomain, ...]:
    """Answered, supported domains that have catalogue wording, in rank order."""
    candidates = [
        observed for observed in snapshot.domains
        if observed.answered
        and observed.has_support
        and observed.domain_key in DOMAIN_TEMPLATES
    ]
    return tuple(sorted(candidates, key=rank_key))


def generate_suggestions(snapshot: ObservationSnapshot, *,
                         policy: PlanningPolicyVersion = CURRENT_PLANNING_POLICY,
                         count: Optional[int] = None,
                         actor_id: Optional[str] = None,
                         now: Optional[datetime] = None
                         ) -> Tuple[GoalSuggestion, ...]:
    """Ranked candidates for one child-month.

    `count` defaults to the policy's default offer and is NOT a maximum the
    domain imposes — a clinician asking for three goals is exercising
    judgement, not violating a rule. Returning fewer than requested is normal
    and correct: the engine will not pad an offer with a domain nobody
    observed.
    """
    wanted = policy.default_goal_count if count is None else count
    if wanted < 1:
        raise SuggestionEngineError("suggestion count must be at least 1")

    suggestions = []
    for index, observed in enumerate(eligible_domains(snapshot)[:wanted]):
        rank = index + 1
        evidence = GoalSuggestionEvidence(
            domain_key=observed.domain_key,
            evidence_source=observed.evidence_source,
            milestone_refs=observed.milestone_refs,
            functional_baseline_area=observed.functional_baseline_area,
            observed_level=observed.observed_level,
            explicitly_selected=observed.explicitly_selected,
            prior_month_summary_id=observed.prior_month_summary_id,
            rule_version=SUGGESTION_RULE_VERSION,
        )
        suggestions.append(GoalSuggestion.create(
            child_id=snapshot.child_id,
            cycle_month=snapshot.cycle_month,
            template=DOMAIN_TEMPLATES[observed.domain_key],
            evidence=evidence,
            priority_rank=rank,
            emphasis_weight=policy.emphasis_for_rank(rank),
            generator_version=GENERATOR_VERSION,
            actor_id=actor_id,
            now=now,
        ))
    return tuple(suggestions)


def explain(observed: ObservedDomain) -> Tuple[str, ...]:
    """The score components that applied, for founder and clinician review.

    Returns rule names only — never an observed value — so an explanation can
    be logged beside a refusal without becoming a second copy of the data.
    """
    reasons = []
    if observed.explicitly_selected:
        reasons.append("explicit_selection")
    if observed.evidence_source is EvidenceSource.CLINICIAN_OBSERVATION:
        reasons.append("clinician_observation")
    if observed.functional_baseline_area:
        reasons.append("functional_baseline_area")
    if observed.milestone_refs:
        reasons.append("milestone_support")
    if observed.prior_month_summary_id:
        reasons.append("prior_cycle_continuity")
    return tuple(reasons)


def _assert_catalogue_is_complete() -> None:
    """Every canonical domain has wording, and no wording carries a name.

    Checked at import so a domain added to the vocabulary without a template
    fails immediately and loudly, rather than silently disappearing from every
    suggestion the engine ever produces.
    """
    missing = [key for key in CANONICAL_DOMAIN_KEYS if key not in DOMAIN_TEMPLATES]
    if missing:
        raise GoalError(f"domains missing suggestion wording: {sorted(missing)}")
    for key, template in DOMAIN_TEMPLATES.items():
        require_canonical_domain(key)
        if CHILD_PLACEHOLDER not in template:
            raise GoalError(f"template for {key} must use the child placeholder")


_assert_catalogue_is_complete()
