"""Slice 6 — the shared additional-question allowance (Q7-Q9).

Canonical design: `pilot_runtime/docs/parent_baseline_v2_design.md`.

## ONE ALLOWANCE, NOT THREE BUCKETS, AND NOT AN OBLIGATION

`additional_question_allowance` is `hard_question_max - normal_question_target`
= 3 under the current policy. Those three questions are a SHARED pool drawn on
by whichever reason still has informational value, and an interview that runs
out of reasons at seven questions stops at seven. Nothing here tries to reach
nine.

That is the whole point of a reason-coded allowance rather than a larger normal
target: raising the normal target to nine would make every interview nine
questions long. Keeping the target at six and gating Q7-Q9 behind a priority
list means the long interviews are the ones that needed to be long.

## ACCOUNTING IS IN QUESTIONS, NOT REASON EVENTS

A reason does not buy a phase; it buys ONE question. A band completion needing
three questions appears three times in the ledger and costs three. If reasons
were the unit, a single "finish the band" event could justify any number of
questions and the hard maximum would stop binding — the same leak the
three-tier budget correction closed on the Channel-A side.

## PRIORITY, AND WHY TARGET INTEGRITY IS FIRST

    R1  target/floor band completion     a band Channel A has already entered
    R2  target/floor resolution          one more whole band, if it fits
    R3  required profile-area recovery   budget caused the gap AND a
                                         difficulty has already been found
    R4  multi-area differentiation       >=2 sampled areas already concerning
    R5  uncertainty clarification        a downward clarifier that still
                                         helps, breadth before depth

A target is what the interview is FOR: it is the only output that reaches the
Pilot and becomes a goal. Profile breadth informs a clinician but generates
nothing. So a half-characterised target is a worse outcome than an unsampled
profile area, and R1/R2 take the questions first. A Channel A that has spent
seven establishing a valid target leaves Q8-Q9 for everything else, and one
that has spent nine leaves nothing — correctly.

## R3 IS NOT "FILL THE EMPTY AREAS"

Recovery applies ONLY to `not_assessed_budget_consumed`. An area whose
canonical inventory begins above the child's age is `not_assessed` for a reason
no question can fix: an above-age probe cannot indicate concern, so spending a
scarce additional question there would buy a guaranteed non-finding. Measured
on the frozen workbook, that is the majority of the unsampled social cases —
`conversation_narrative` starts at 36 months — and all of them must stay
honestly unassessed.

## THERE IS NO COMPLETENESS REASON

No reason code means "the questionnaire looks unfinished". Every reason here
names a specific thing the next answer could change. When none applies the
planner stops immediately, and at the hard maximum it stops regardless of how
much uncertainty remains. Preserving honest uncertainty is an outcome, not a
failure to collect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from api import baseline_eligibility as eligibility
from api import baseline_policy as policy_mod
from api import baseline_profile as profile
from api import baseline_traversal as traversal
from genex_core import functional_baseline_v2 as v2

# ---------------------------------------------------------------------------
# reason codes
# ---------------------------------------------------------------------------

#: R1. A band Channel A has ALREADY ENTERED is not finished. Not discretionary:
#: never truncate an entered band is a frozen rule, and the questions it costs
#: are real questions against the hard maximum.
R1_TARGET_BAND_COMPLETION = "R1_TARGET_BAND_COMPLETION"

#: R2. Channel A can resolve an unestablished floor or an unbracketed target by
#: entering ONE more band. Admitted only if the WHOLE band fits — a half-asked
#: band is worse than an honest "floor not established".
R2_TARGET_FLOOR_RESOLUTION = "R2_TARGET_FLOOR_RESOLUTION"

#: R3. A required profile area went unsampled because Channel A consumed the
#: normal budget, and an age-appropriate canonical skill exists.
R3_PROFILE_AREA_RECOVERY = "R3_PROFILE_AREA_RECOVERY"

#: R4. Two or more genuinely sampled areas already show concern and the profile
#: cannot yet distinguish them.
R4_MULTI_AREA_DIFFERENTIATION = "R4_MULTI_AREA_DIFFERENTIATION"

#: R5. A downward clarifier that would materially reduce recorded uncertainty.
R5_UNCERTAINTY_CLARIFICATION = "R5_UNCERTAINTY_CLARIFICATION"

#: The order is the policy. Evaluated strictly top to bottom, first match wins.
REASON_PRIORITY: Tuple[str, ...] = (
    R1_TARGET_BAND_COMPLETION,
    R2_TARGET_FLOOR_RESOLUTION,
    R3_PROFILE_AREA_RECOVERY,
    R4_MULTI_AREA_DIFFERENTIATION,
    R5_UNCERTAINTY_CLARIFICATION,
)

#: Which reasons spend their question on which channel.
CHANNEL_A_REASONS: Tuple[str, ...] = (R1_TARGET_BAND_COMPLETION,
                                      R2_TARGET_FLOOR_RESOLUTION)
CHANNEL_B_REASONS: Tuple[str, ...] = (R3_PROFILE_AREA_RECOVERY,
                                      R4_MULTI_AREA_DIFFERENTIATION,
                                      R5_UNCERTAINTY_CLARIFICATION)

ASK = "ASK"
STOP = "STOP"

#: Why the interview stopped.
STOP_HARD_MAXIMUM_REACHED = "HARD_MAXIMUM_REACHED"
STOP_NO_QUALIFYING_REASON = "NO_QUALIFYING_REASON"
STOP_NORMAL_TARGET_NOT_REACHED = "NORMAL_TARGET_NOT_REACHED"

#: Phases of one interview, in the order they can occur.
PHASE_CHANNEL_A = "channel_a"
PHASE_PROFILE_NORMAL = "profile_normal"
PHASE_ADDITIONAL = "additional"


class BaselineAdditionalError(Exception):
    """An additional question could not be planned. PHI-safe: no child data."""

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class AdditionalStep:
    """ASK one question with a reason code, or STOP."""

    kind: str
    reason_code: Optional[str] = None
    channel: Optional[str] = None
    question: Optional[Dict[str, Any]] = None
    enters_band: Optional[int] = None
    profile_area: Optional[str] = None
    reason: Optional[str] = None

    @property
    def is_ask(self) -> bool:
        return self.kind == ASK


# ---------------------------------------------------------------------------
# R1 / R2 — Channel A
# ---------------------------------------------------------------------------

def _channel_a_step(record, *, policy: policy_mod.BaselinePolicy,
                    asked_total: int) -> Optional[AdditionalStep]:
    """R1 or R2, or `None` if Channel A has nothing left to ask.

    The band arithmetic is NOT re-implemented here. `plan_next` already owns
    "finish the entered band first", "never enter a band that does not fit"
    and the tier assignment, and a second copy of those rules would be a second
    thing to keep in agreement with the frozen traversal. This function only
    CLASSIFIES what `plan_next` decided:

        enters_band is None   it is finishing a band already entered   -> R1
        enters_band is set    it is opening one more whole band        -> R2
    """
    budget = traversal.InterviewBudget.from_policy(
        policy, asked_total=int(asked_total))
    step = traversal.plan_next(
        record, lookahead_months=policy.normal_upper_lookahead_months,
        budget=budget)
    if not step.is_ask:
        return None
    reason_code = (R2_TARGET_FLOOR_RESOLUTION if step.enters_band is not None
                   else R1_TARGET_BAND_COMPLETION)
    return AdditionalStep(kind=ASK, reason_code=reason_code,
                          channel=v2.CHANNEL_A, question=step.question,
                          enters_band=step.enters_band)


# ---------------------------------------------------------------------------
# R3 — required profile-area recovery
# ---------------------------------------------------------------------------

def difficulty_found(record, *, policy: policy_mod.BaselinePolicy) -> bool:
    """Has the interview found ANY age-appropriate difficulty yet?

    R3's materiality test. Channel A's eligible unresolved evidence, or a
    concern in any genuinely sampled profile area.
    """
    if eligibility.eligible_unresolved(record):
        return True
    return bool(concerning_areas(record, policy=policy))


def _recovery_step(record, *, policy: policy_mod.BaselinePolicy
                   ) -> Optional[AdditionalStep]:
    """Sample a required area the normal budget could not reach.

    GATED ON A DIFFICULTY ALREADY BEING FOUND, and that gate is the difference
    between R3 and the completeness reason this design forbids.

    Without it R3 fires on essentially every path where Channel A spent five or
    six questions and two required areas remain — which is most of them — so
    Q7 would become automatic. It is not: "profile recovery may begin at Q7
    only if justified".

    What justifies it is a difficulty needing to be placed. If the interview
    has found a concern, whether that concern is isolated to expressive
    language or spans comprehension and gesture changes the clinical picture,
    and one question can tell us which. If it has found nothing anywhere, a
    further area adds breadth to a clean profile and nothing actionable — so
    the area stays honestly `not_assessed_budget_consumed` and the interview
    ends at six.
    """
    if not difficulty_found(record, policy=policy):
        return None
    for area in profile.recoverable_areas(record, policy=policy):
        probe = profile.first_probe(
            record.area_id, area,
            chronological_months=int(record.chronological_months))
        if probe is None:
            # Unreachable by construction — `recoverable_areas` is defined as
            # the areas that HAVE an age-appropriate probe. Kept as a refusal
            # rather than an assert so a future inventory change degrades to
            # "no recovery" instead of to a future-only probe.
            continue
        return AdditionalStep(kind=ASK, reason_code=R3_PROFILE_AREA_RECOVERY,
                              channel=v2.CHANNEL_B, profile_area=area,
                              question=profile.probe_question(record, probe))
    return None


# ---------------------------------------------------------------------------
# R4 — multi-area differentiation
# ---------------------------------------------------------------------------

def concerning_areas(record, *, policy: policy_mod.BaselinePolicy
                     ) -> Tuple[str, ...]:
    """Areas GENUINELY SAMPLED that show at least one age-appropriate concern.

    "Genuinely sampled" excludes an area holding only above-age probes: those
    cannot indicate concern, so an area built from them is not a data point
    about the child's difficulties.
    """
    chronological = int(record.chronological_months)
    out = []
    for area in profile.AREA_SUBDOMAIN:
        evidence = profile.area_evidence(record, area)
        if not evidence:
            continue
        if any(eligibility.counts_as_concern(e.state, e.months, chronological)
               for e in evidence):
            out.append(area)
    return tuple(out)


def _differentiation_step(record, *, policy: policy_mod.BaselinePolicy
                          ) -> Optional[AdditionalStep]:
    """One question that would distinguish between two concerning areas.

    MATERIALITY, concretely: an area sampled ONCE tells us it is concerning and
    nothing about how far down the difficulty goes. Two such areas are
    indistinguishable — both read "concern at one band". A descent in one of
    them changes that. An area already sampled twice has a characterised depth,
    so a third question no longer distinguishes it from anything and R4 does
    not fire.

    Without that bound R4 would be a completeness reason wearing a different
    name, and could consume Q7-Q9 on any profile with two concerns.
    """
    areas = concerning_areas(record, policy=policy)
    if len(areas) < 2:
        return None
    for area in _area_priority(policy):
        if area not in areas:
            continue
        evidence = profile.area_evidence(record, area)
        if len(evidence) != 1:
            continue
        probe = profile.downward_probe(
            record.area_id, area, below_months=evidence[0].months,
            asked_months=[e.months for e in evidence])
        if probe is None:
            continue
        return AdditionalStep(kind=ASK,
                              reason_code=R4_MULTI_AREA_DIFFERENTIATION,
                              channel=v2.CHANNEL_B, profile_area=area,
                              question=profile.probe_question(record, probe))
    return None


# ---------------------------------------------------------------------------
# R5 — uncertainty clarification
# ---------------------------------------------------------------------------

def _area_priority(policy: policy_mod.BaselinePolicy) -> Tuple[str, ...]:
    """Required areas in policy order, then any remaining area."""
    return tuple(policy.required_profile_areas) + tuple(
        a for a in profile.AREA_SUBDOMAIN
        if a not in policy.required_profile_areas)


def unresolved_sampled_areas(record, *, policy: policy_mod.BaselinePolicy
                             ) -> Tuple[str, ...]:
    """SAMPLED areas whose lowest evidence is still `unknown`, in priority order.

    Sampled is the operative word: an area with no evidence is not unresolved,
    it is unassessed, and R3 owns that case. An area whose lowest answer is a
    real answer is bracketed by it and needs nothing.
    """
    out = []
    for area in _area_priority(policy):
        evidence = profile.area_evidence(record, area)
        if not evidence:
            continue
        if evidence[0].state == v2.STATE_UNKNOWN:
            out.append(area)
    return tuple(out)


def _clarification_step(record, *, policy: policy_mod.BaselinePolicy
                        ) -> Optional[AdditionalStep]:
    """A downward clarifier beneath a recorded `unknown`. BREADTH BEFORE DEPTH.

    Scoped to CHANNEL B. The Channel-A equivalent is R2, which owns target and
    floor uncertainty and outranks this; giving R5 a Channel-A path too would
    let a lower-priority reason spend a question on the thing a higher-priority
    reason had already declined. And R5 never SAMPLES an area — an unassessed
    area is R3's case, and an unprobeable one is nobody's.

    Material only beneath an `unknown` at the area's LOWEST sampled band. An
    unknown above an answered band is already bracketed by that answer.

    ## ONE CLARIFICATION EACH BEFORE ANY SECOND

    An earlier draft took the first unresolved area in priority order every
    time, so an all-`not_sure` interview spent the whole allowance descending
    receptive — 12, then 4, then 2 — while functional and social kept their
    unknowns untouched. Three questions bought depth in one area and nothing
    anywhere else.

    The selection is therefore the unresolved area with the FEWEST probes,
    tie-broken by area priority. Applied repeatedly that gives each unresolved
    sampled area one clarification before any gets a second, and once they are
    level the tie-break deepens the highest-priority one — which is the
    approved order, derived from recorded state rather than from a counter this
    module would have to persist.
    """
    candidates = []
    for area in unresolved_sampled_areas(record, policy=policy):
        evidence = profile.area_evidence(record, area)
        probe = profile.downward_probe(
            record.area_id, area, below_months=evidence[0].months,
            asked_months=[e.months for e in evidence])
        if probe is None:
            continue
        candidates.append((len(evidence), area, probe))
    if not candidates:
        return None
    priority = _area_priority(policy)
    depth, area, probe = min(candidates,
                             key=lambda c: (c[0], priority.index(c[1])))
    return AdditionalStep(kind=ASK,
                          reason_code=R5_UNCERTAINTY_CLARIFICATION,
                          channel=v2.CHANNEL_B, profile_area=area,
                          question=profile.probe_question(record, probe))


# ---------------------------------------------------------------------------
# the additional-question planner
# ---------------------------------------------------------------------------

_REASON_PLANNERS = (
    (R1_TARGET_BAND_COMPLETION, None),      # both served by _channel_a_step
    (R2_TARGET_FLOOR_RESOLUTION, None),
    (R3_PROFILE_AREA_RECOVERY, _recovery_step),
    (R4_MULTI_AREA_DIFFERENTIATION, _differentiation_step),
    (R5_UNCERTAINTY_CLARIFICATION, _clarification_step),
)


def plan_additional_step(record, *, policy: policy_mod.BaselinePolicy,
                         asked_total: int) -> AdditionalStep:
    """The next question beyond the normal target, or a STOP.

    `asked_total` is the JOINT count — Channel A plus Channel B. The hard
    maximum binds the interview, not either channel.
    """
    asked_total = int(asked_total)
    if asked_total >= int(policy.hard_question_max):
        # At the hard maximum, stop regardless of remaining uncertainty.
        return AdditionalStep(kind=STOP, reason=STOP_HARD_MAXIMUM_REACHED)
    if asked_total < int(policy.normal_question_target):
        # Not this planner's phase. Returned rather than raised so a caller
        # sequencing the phases does not need to know the boundary twice.
        return AdditionalStep(kind=STOP,
                              reason=STOP_NORMAL_TARGET_NOT_REACHED)

    step = _channel_a_step(record, policy=policy, asked_total=asked_total)
    if step is not None:
        return step
    for _reason_code, planner in _REASON_PLANNERS:
        if planner is None:
            continue
        step = planner(record, policy=policy)
        if step is not None:
            return step
    return AdditionalStep(kind=STOP, reason=STOP_NO_QUALIFYING_REASON)


# ---------------------------------------------------------------------------
# the whole interview, in phases
# ---------------------------------------------------------------------------

def plan_interview_step(record, *, policy: policy_mod.BaselinePolicy,
                        asked_total: int) -> AdditionalStep:
    """The next question of the WHOLE interview, whichever phase it is in.

    Three phases, strictly sequential:

        1. Channel A      until `plan_next` stops
        2. Channel B      normal breadth/depth, while a normal slot remains
        3. additional     Q7-Q9 by reason priority

    The order cannot cycle back. Channel B writes to `profile_probes` and
    Channel A reads `skills` and `bands_entered`, so no profile answer can
    reopen a Channel-A decision — the separation Slice 5 made structural is
    what makes this sequencing safe to assume rather than merely intended.

    Phase 1 may itself reach into Q7-Q9: `plan_next` admits a band at
    `TIER_ADDITIONAL` when the whole band fits, which is reason R1/R2 claiming
    its first priority. A reason code is attached to every question beyond the
    normal target no matter which phase produced it.
    """
    asked_total = int(asked_total)
    normal_target = int(policy.normal_question_target)
    hard_max = int(policy.hard_question_max)

    if asked_total >= hard_max:
        return AdditionalStep(kind=STOP, reason=STOP_HARD_MAXIMUM_REACHED)

    step = _channel_a_step(record, policy=policy, asked_total=asked_total)
    if step is not None:
        if asked_total < normal_target:
            # Within the normal target a Channel-A question needs no
            # justification — it IS the interview. The reason code is recorded
            # only beyond Q6, which is exactly what Slice 7/9 has to explain.
            return AdditionalStep(kind=ASK, channel=v2.CHANNEL_A,
                                  question=step.question,
                                  enters_band=step.enters_band)
        return step

    if asked_total < normal_target:
        profile_step = profile.plan_profile_step(
            record, policy=policy, asked_total=asked_total)
        if profile_step.kind == profile.ASK:
            return AdditionalStep(kind=ASK, channel=v2.CHANNEL_B,
                                  question=profile_step.question,
                                  profile_area=profile_step.profile_area)
        # Normal profile routing is exhausted. Fall through: a reason may still
        # justify an additional question, but `plan_additional_step` refuses
        # below the normal target, so stop honestly instead.
        return AdditionalStep(kind=STOP,
                              reason=profile.STOP_NO_COVERAGE_AVAILABLE)

    return plan_additional_step(record, policy=policy, asked_total=asked_total)


def phase_for(asked_total: int, *, policy: policy_mod.BaselinePolicy) -> str:
    """Which phase question `asked_total + 1` belongs to, by budget alone."""
    return (PHASE_ADDITIONAL
            if int(asked_total) >= int(policy.normal_question_target)
            else PHASE_PROFILE_NORMAL)


def additional_summary(record, *, policy: Optional[
        policy_mod.BaselinePolicy] = None) -> Dict[str, Any]:
    """What the allowance was spent on. PHI-safe: ordinals and codes only."""
    policy = policy or policy_mod.default_policy()
    ledger: List[Dict[str, Any]] = list(record.additional_reasons)
    by_reason: Dict[str, int] = {}
    for entry in ledger:
        by_reason[entry["reason_code"]] = by_reason.get(
            entry["reason_code"], 0) + 1
    asked_total = len(record.skills) + len(record.profile_probes)
    return {
        "questions_total": asked_total,
        "normal_question_target": int(policy.normal_question_target),
        "hard_question_max": int(policy.hard_question_max),
        "additional_questions_used": len(ledger),
        "additional_question_allowance": int(
            policy.additional_question_allowance),
        "exceeded_normal_target": asked_total > int(
            policy.normal_question_target),
        "reasons_in_order": [e["reason_code"] for e in ledger],
        "questions_by_reason": by_reason,
        "channel_a_questions": len(record.skills),
        "channel_b_questions": len(record.profile_probes),
    }


__all__ = [
    "R1_TARGET_BAND_COMPLETION", "R2_TARGET_FLOOR_RESOLUTION",
    "R3_PROFILE_AREA_RECOVERY", "R4_MULTI_AREA_DIFFERENTIATION",
    "R5_UNCERTAINTY_CLARIFICATION", "REASON_PRIORITY",
    "CHANNEL_A_REASONS", "CHANNEL_B_REASONS",
    "ASK", "STOP", "STOP_HARD_MAXIMUM_REACHED", "STOP_NO_QUALIFYING_REASON",
    "STOP_NORMAL_TARGET_NOT_REACHED",
    "PHASE_CHANNEL_A", "PHASE_PROFILE_NORMAL", "PHASE_ADDITIONAL",
    "BaselineAdditionalError", "AdditionalStep",
    "concerning_areas", "difficulty_found", "unresolved_sampled_areas",
    "plan_additional_step", "plan_interview_step",
    "phase_for", "additional_summary",
]
