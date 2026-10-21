"""Slice 5 — CHANNEL B: broader communication-profile probes.

Canonical design: `pilot_runtime/docs/parent_baseline_v2_design.md`.

## WHAT CHANNEL B IS FOR

Channel A (`api/baseline_traversal.py`) walks the DECLARED track and produces at
most one clinical target. It is deliberately narrow — an expressive-language
ladder. A caregiver who finishes it has told us where expressive language sits
and nothing about whether the child understands what is said to them, uses
gesture to get needs met, or can hold a conversation.

Channel B spends the remaining normal interview slots on that breadth. It
produces a PROFILE, never a target: its evidence lives in
`record.profile_probes`, a different mapping from `record.skills`, and the A2 v2
projection iterates `skills` and `bands_entered` only. The isolation is
structural rather than conventional — profile evidence cannot generate a goal
because the Pilot is never sent it.

## THE FIRST-PROBE RULE, AND WHY IT IS NOT age + 6

`normal_upper_lookahead_months = 6` is an UPPER ROUTING GUARDRAIL. It is not
the first-probe anchor, and an earlier draft of this module that treated it as
one was wrong in a way the frozen above-age rule makes expensive.

`api/baseline_eligibility.py` holds that a failure on a genuinely above-age
skill is never a deficit and never a concern. So an above-age probe can only
ever return a strength or nothing: `no` is inert by construction. Anchoring the
first probe at `chronological + 6` would therefore spend a scarce normal slot on
a question that CANNOT tell us whether the area is concerning — the single thing
a profile is for.

Measured against the frozen workbook, this is not hypothetical. For the talking
domain:

    receptive_language        2, 4, 12, 15, 18, 24, 60
    gestural_communication    9, 12, 15, 24
    conversation_narrative    36, 48, 60
    speech_intelligibility    36

A 30-month-old under the age+6 reading draws `conversation_narrative` at 36m —
above age, inert on failure — and loses a slot that `receptive_language` at 24m
would have used to find a real concern.

So the normal first probe is the NEAREST CANONICAL SKILL AT OR BELOW
chronological age. The guardrail still binds, but only upward (rule 3).

## THE FOUR-STEP ROUTING ORDER

    1. age-appropriate first probe   highest canonical skill <= chronological
    2. downward clarification        concern or uncertainty descends, NO floor
    3. optional upward probe         demonstrated may ascend, never past +6
    4. no at/below-age skill         the area stays `not_assessed`

Step 4 is a REFUSAL TO MANUFACTURE COVERAGE. `conversation_narrative` begins at
36 months; for a 24-month-old there is no age-appropriate social-communication
skill in the inventory, and the honest report is that the area was not assessed.
Reporting it as "clear" would be a claim about the child derived from a gap in
our question bank.

## NORMAL COLLECTION STOPS AT SIX

Slots come from `policy.profile_slots_for(asked_total)` — whatever is left of
`normal_question_target` after Channel A, nothing more. Breadth across the
required areas first, then depth within them. Q7-Q9 belong to Slice 6's
reason-priority logic and this module never reaches for them: `plan_profile_step`
takes the budget it is given and stops.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from api import baseline_eligibility as eligibility
from api import baseline_policy as policy_mod
from genex_core import functional_baseline as v1
from genex_core import functional_baseline_v2 as v2

# ---------------------------------------------------------------------------
# the profile areas
# ---------------------------------------------------------------------------

AREA_RECEPTIVE = "receptive"
AREA_FUNCTIONAL = "functional"
AREA_SOCIAL = "social"
AREA_INTELLIGIBILITY = "intelligibility"

#: Each profile area is exactly ONE workbook subdomain. A one-to-one mapping is
#: chosen over a cluster because "the nearest skill at or below age IN THAT
#: AREA" has to be well defined, and because an area that turns out to be
#: unprobeable (step 4) must be identifiable as such rather than silently
#: absorbing a sibling subdomain's rung.
AREA_SUBDOMAIN: Dict[str, str] = {
    AREA_RECEPTIVE: "receptive_language",
    AREA_FUNCTIONAL: "gestural_communication",
    AREA_SOCIAL: "conversation_narrative",
    AREA_INTELLIGIBILITY: "speech_intelligibility",
}

#: Reported per area. There is deliberately no third value: an area is either
#: assessed or it is not, and "no concern found" is a conclusion about assessed
#: evidence, not a status an unprobed area can hold.
ASSESSED = "assessed"
NOT_ASSESSED = "not_assessed"

#: WHY an area is `not_assessed`. Slice 5 measured these as two genuinely
#: different causes over 3072 paths — social was unassessed 1920 times because
#: the inventory begins at 36 months and 556 times because Channel A had spent
#: the budget — and only the second is ever recoverable by an additional
#: question. Collapsing them would make Slice 6 spend a scarce question on a
#: future-only probe that cannot indicate concern.
#:
#: Derivational. Whether either becomes a Parent-facing label is Slice 9's
#: decision, not this module's.
NOT_ASSESSED_NO_AGE_APPROPRIATE_SKILL = "not_assessed_no_age_appropriate_skill"
NOT_ASSESSED_BUDGET_CONSUMED = "not_assessed_budget_consumed"

#: Step kinds.
ASK = "ASK"
STOP = "STOP"

#: Why a probe was selected. Carried on the step so a trajectory can be audited
#: against the four-step order rather than inferred from the months alone.
PROBE_FIRST = "AGE_APPROPRIATE_FIRST_PROBE"
PROBE_DOWNWARD = "DOWNWARD_CLARIFICATION"
PROBE_UPWARD = "UPWARD_STRENGTH_PROBE"

#: Why normal profile collection stopped.
STOP_NORMAL_BUDGET_SPENT = "NORMAL_BUDGET_SPENT"
STOP_NO_COVERAGE_AVAILABLE = "NO_PROFILE_COVERAGE_AVAILABLE"

#: Direction of a depth probe within an already-opened area.
DESCEND = "DESCEND"
ASCEND = "ASCEND"


class BaselineProfileError(Exception):
    """A profile probe could not be planned. PHI-safe: no child data."""

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class ProfileProbeRef:
    """One canonical Channel-B skill, addressed by the identity triple."""

    profile_area: str
    subdomain: str
    months: int
    milestone: str

    def skill_key(self, domain: str) -> str:
        return v2.skill_key(domain, self.subdomain, self.months, self.milestone)


@dataclass(frozen=True)
class ProfileStep:
    """ASK one probe, or STOP with a reason."""

    kind: str
    profile_area: Optional[str] = None
    question: Optional[Dict[str, Any]] = None
    probe_reason: Optional[str] = None
    direction: Optional[str] = None
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# the inventory
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=32)
def area_probes(area_id: str, profile_area: str) -> Tuple[ProfileProbeRef, ...]:
    """Every canonical skill in one profile area, ordered by `(months, text)`.

    Deduplicated: the workbook carries duplicate rows per rung (the same reason
    `_raw_family_index` unions families in `api/baseline_micro_tracks.py`), and
    a duplicate would otherwise look like a second probeable skill at the same
    band.
    """
    subdomain = AREA_SUBDOMAIN.get(profile_area)
    if subdomain is None:
        raise BaselineProfileError(f"unknown profile area: {profile_area!r}")
    area = v1.get_area(area_id)
    if subdomain in area.track_subdomains:
        # Would put the same skill on both channels, so a profile answer could
        # change a Channel-A band's completeness.
        raise BaselineProfileError(
            "a profile area must not be a declared-track subdomain")
    seen: Dict[Tuple[int, str], ProfileProbeRef] = {}
    for row in v1._rows_for_domain(area.domain, (subdomain,), ()):
        key = (int(row["months"]), str(row["milestone"]))
        seen.setdefault(key, ProfileProbeRef(
            profile_area=profile_area,
            subdomain=subdomain,
            months=int(row["months"]),
            milestone=str(row["milestone"]),
        ))
    return tuple(sorted(seen.values(), key=lambda p: (p.months, p.milestone)))


@functools.lru_cache(maxsize=32)
def area_bands(area_id: str, profile_area: str) -> Tuple[int, ...]:
    """The distinct bands this profile area can probe, ascending."""
    return tuple(sorted({p.months for p in area_probes(area_id, profile_area)}))


def band_representative(area_id: str, profile_area: str,
                        months: int) -> Optional[ProfileProbeRef]:
    """ONE probe for a band — the first in canonical `(months, text)` order.

    Several areas carry more than one milestone at a band: `receptive_language`
    has two at 15m and two at 24m, `conversation_narrative` two at 48m and two
    at 60m. Channel B asks ONE of them.

    That is not the v1 defect. v1 retired a whole band of the DECLARED track on
    one answer while claiming the band was settled, and Channel A exists to fix
    exactly that. Channel B makes no completeness claim at all: an area reports
    how many probes it ran and which bands showed concern, never that a band
    was assessed. With at most six questions in the whole interview, one probe
    in four areas is worth more as a screen than two probes in one band, and
    the sibling milestone is deliberately left unasked rather than silently
    treated as covered.
    """
    at_band = [p for p in area_probes(area_id, profile_area)
               if p.months == int(months)]
    return at_band[0] if at_band else None


def _row_for(area_id: str, probe: ProfileProbeRef) -> Dict[str, Any]:
    area = v1.get_area(area_id)
    for row in v1._rows_for_domain(area.domain, (probe.subdomain,), ()):
        if int(row["months"]) == probe.months and \
                str(row["milestone"]) == probe.milestone:
            return row
    raise BaselineProfileError("profile probe is not in the canonical sheet")


def probe_question(record: v2.BaselineRecordV2,
                   probe: ProfileProbeRef) -> Dict[str, Any]:
    """The caregiver-facing question for one profile probe.

    Carries `parent_explanation` because the UI needs it, and deliberately NOT
    `activity_family`. Channel-B evidence must never bind to an activity
    family: a profile probe cannot become a goal, and the cheapest way to keep
    that true is for the payload never to contain the field a goal would be
    built from.
    """
    row = _row_for(record.area_id, probe)
    return {
        "skill_key": probe.skill_key(record.domain),
        "baseline_version": v2.BASELINE_VERSION_V2,
        "domain": record.domain,
        "channel": "profile",
        "profile_area": probe.profile_area,
        "months": probe.months,
        "milestone": probe.milestone,
        "subdomain": probe.subdomain,
        "parent_explanation": row.get("parent_explanation", ""),
    }


# ---------------------------------------------------------------------------
# the four routing steps
# ---------------------------------------------------------------------------

def first_probe(area_id: str, profile_area: str, *,
                chronological_months: int) -> Optional[ProfileProbeRef]:
    """RULE 1. The nearest canonical skill AT OR BELOW chronological age.

    Returns None when the area has no age-appropriate skill, which is rule 4's
    trigger. Note the bound is inclusive: a rung at exactly the child's age is
    age-appropriate, matching `eligibility.is_above_age`.
    """
    at_or_below = [m for m in area_bands(area_id, profile_area)
                   if m <= int(chronological_months)]
    if not at_or_below:
        return None
    return band_representative(area_id, profile_area, at_or_below[-1])


def downward_probe(area_id: str, profile_area: str, *, below_months: int,
                   asked_months: Sequence[int] = ()
                   ) -> Optional[ProfileProbeRef]:
    """RULE 2. The next skill strictly below, with NO lower age bound.

    Unbounded by design. Once an area shows concern or uncertainty, how far
    down the child's competence actually sits is the clinical question, and a
    floor derived from chronological age would stop the descent exactly where a
    significant delay becomes interesting.
    """
    done = set(int(m) for m in asked_months)
    below = [m for m in area_bands(area_id, profile_area)
             if m < int(below_months) and m not in done]
    if not below:
        return None
    return band_representative(area_id, profile_area, below[-1])


def upward_probe(area_id: str, profile_area: str, *, above_months: int,
                 chronological_months: int, lookahead_months: int,
                 asked_months: Sequence[int] = ()
                 ) -> Optional[ProfileProbeRef]:
    """RULE 3. An optional strength probe, never past the guardrail.

    The ceiling is `chronological + lookahead`, INCLUSIVE, and is the only
    place the lookahead participates in Channel-B routing.
    """
    ceiling = int(chronological_months) + int(lookahead_months)
    done = set(int(m) for m in asked_months)
    above = [m for m in area_bands(area_id, profile_area)
             if int(above_months) < m <= ceiling and m not in done]
    if not above:
        return None
    return band_representative(area_id, profile_area, above[0])


# ---------------------------------------------------------------------------
# recorded profile state
# ---------------------------------------------------------------------------

def area_evidence(record: v2.BaselineRecordV2, profile_area: str
                  ) -> Tuple[v2.BaselineSkillEvidence, ...]:
    """This area's recorded probes, lowest band first."""
    subdomain = AREA_SUBDOMAIN[profile_area]
    found = [e for e in record.profile_probes.values()
             if e.subdomain == subdomain]
    return tuple(sorted(found, key=lambda e: (e.months, e.milestone)))


def area_status(record: v2.BaselineRecordV2, profile_area: str) -> str:
    return ASSESSED if area_evidence(record, profile_area) else NOT_ASSESSED


def not_assessed_cause(record: v2.BaselineRecordV2,
                       profile_area: str) -> Optional[str]:
    """Why this area holds no evidence, or `None` if it holds some.

    The discriminator is simply whether an age-appropriate canonical skill
    EXISTS. If one does, normal breadth routing would have taken it the moment
    a slot was free — breadth precedes depth and precedes every fallback tier —
    so the only way an area with available coverage stays empty is that no slot
    was ever free. If one does not, no number of additional questions can
    change the answer, because rule 4 forbids manufacturing coverage.
    """
    if area_evidence(record, profile_area):
        return None
    if first_probe(record.area_id, profile_area,
                   chronological_months=int(record.chronological_months)) is None:
        return NOT_ASSESSED_NO_AGE_APPROPRIATE_SKILL
    return NOT_ASSESSED_BUDGET_CONSUMED


def recoverable_areas(record: v2.BaselineRecordV2, *,
                      policy: policy_mod.BaselinePolicy) -> Tuple[str, ...]:
    """Required areas an additional question could still legitimately sample.

    Exactly the `not_assessed_budget_consumed` ones, in the policy's order.
    """
    return tuple(
        a for a in policy.required_profile_areas
        if not_assessed_cause(record, a) == NOT_ASSESSED_BUDGET_CONSUMED)


def area_direction(record: v2.BaselineRecordV2, profile_area: str,
                   *, chronological_months: int) -> Optional[str]:
    """DESCEND on concern or uncertainty, ASCEND when everything is clear.

    `unknown` descends alongside the two unresolved states. The caregiver could
    not answer an age-appropriate question, and a lower question they CAN answer
    is more informative than a higher one they also cannot.

    An above-age probe never drives the direction — it cannot indicate concern,
    so it must not be able to trigger a descent either.
    """
    evidence = area_evidence(record, profile_area)
    if not evidence:
        return None
    age_appropriate = [e for e in evidence
                       if not eligibility.is_above_age(e.months,
                                                       chronological_months)]
    if any(e.state != v2.STATE_DEMONSTRATED for e in age_appropriate):
        return DESCEND
    return ASCEND


def _depth_probe(record: v2.BaselineRecordV2, profile_area: str, *,
                 chronological_months: int, lookahead_months: int
                 ) -> Optional[Tuple[ProfileProbeRef, str]]:
    """One more probe inside an already-opened area, per its own direction."""
    evidence = area_evidence(record, profile_area)
    if not evidence:
        return None
    asked = [e.months for e in evidence]
    direction = area_direction(record, profile_area,
                               chronological_months=chronological_months)
    if direction == DESCEND:
        unresolved = [e.months for e in evidence
                      if e.state != v2.STATE_DEMONSTRATED
                      and not eligibility.is_above_age(e.months,
                                                       chronological_months)]
        anchor = min(unresolved) if unresolved else min(asked)
        probe = downward_probe(record.area_id, profile_area,
                               below_months=anchor, asked_months=asked)
        return (probe, PROBE_DOWNWARD) if probe else None
    probe = upward_probe(record.area_id, profile_area,
                         above_months=max(asked),
                         chronological_months=chronological_months,
                         lookahead_months=lookahead_months,
                         asked_months=asked)
    return (probe, PROBE_UPWARD) if probe else None


# ---------------------------------------------------------------------------
# the planner
# ---------------------------------------------------------------------------

def _depth_order(policy: policy_mod.BaselinePolicy) -> Tuple[str, ...]:
    """Fallback tiers as profile areas, preserving the policy's order.

    `"<area>_plus"` means depth within that area; a bare area name means
    breadth into a non-required area. The policy stays declarative and the
    interpretation lives here.
    """
    order: List[str] = []
    for tier in policy.profile_fallback_tiers:
        area = tier[:-len("_plus")] if tier.endswith("_plus") else tier
        if area not in AREA_SUBDOMAIN:
            raise BaselineProfileError(f"unknown profile fallback tier: {tier!r}")
        order.append(area)
    return tuple(order)


def plan_profile_step(record: v2.BaselineRecordV2, *,
                      policy: policy_mod.BaselinePolicy,
                      asked_total: int,
                      allow_future_only_probe: bool = False) -> ProfileStep:
    """The next Channel-B question, or a STOP.

    `asked_total` is CHANNEL A PLUS CHANNEL B. The budget is joint, so the
    normal target of six is reached by the interview as a whole and a profile
    probe cannot be smuggled in beside a long Channel-A traversal.

    `allow_future_only_probe` stays False for normal collection. Rule 4 prefers
    `not_assessed` over spending a normal slot on a question that cannot show a
    concern; Slice 6 owns the decision to spend an ADDITIONAL question that way.
    """
    chronological = int(record.chronological_months)
    lookahead = int(policy.normal_upper_lookahead_months)

    # Eagerly, before the budget check. A policy naming an area that does not
    # exist is a configuration fault, and discovering it only on the paths that
    # happen to reach the depth phase would let it ship.
    depth_order = _depth_order(policy)
    for profile_area in policy.required_profile_areas:
        if profile_area not in AREA_SUBDOMAIN:
            raise BaselineProfileError(
                f"unknown required profile area: {profile_area!r}")

    if policy.profile_slots_for(int(asked_total)) <= 0:
        return ProfileStep(kind=STOP, reason=STOP_NORMAL_BUDGET_SPENT)

    # 1. BREADTH. Every required area gets its age-appropriate first probe
    #    before any area gets a second question. One probe in three areas
    #    answers "is anything else going on" better than three in one.
    for profile_area in policy.required_profile_areas:
        if area_status(record, profile_area) == ASSESSED:
            continue
        probe = first_probe(record.area_id, profile_area,
                            chronological_months=chronological)
        if probe is not None:
            return ProfileStep(kind=ASK, profile_area=profile_area,
                               question=probe_question(record, probe),
                               probe_reason=PROBE_FIRST)

    # 2. DEPTH, in the policy's fallback order.
    for profile_area in depth_order:
        if area_status(record, profile_area) == NOT_ASSESSED:
            # A non-required area reached by a fallback tier — still rule 1.
            probe = first_probe(record.area_id, profile_area,
                                chronological_months=chronological)
            if probe is not None:
                return ProfileStep(kind=ASK, profile_area=profile_area,
                                   question=probe_question(record, probe),
                                   probe_reason=PROBE_FIRST)
            continue
        found = _depth_probe(record, profile_area,
                             chronological_months=chronological,
                             lookahead_months=lookahead)
        if found is not None:
            probe, probe_reason = found
            return ProfileStep(
                kind=ASK, profile_area=profile_area,
                question=probe_question(record, probe),
                probe_reason=probe_reason,
                direction=(DESCEND if probe_reason == PROBE_DOWNWARD
                           else ASCEND))

    # 3. Rule 4's explicit opt-in, never taken by normal routing.
    if allow_future_only_probe:
        for profile_area in tuple(policy.required_profile_areas) + depth_order:
            if area_status(record, profile_area) == ASSESSED:
                continue
            probe = upward_probe(record.area_id, profile_area,
                                 above_months=chronological,
                                 chronological_months=chronological,
                                 lookahead_months=lookahead)
            if probe is not None:
                return ProfileStep(kind=ASK, profile_area=profile_area,
                                   question=probe_question(record, probe),
                                   probe_reason=PROBE_UPWARD,
                                   direction=ASCEND)

    return ProfileStep(kind=STOP, reason=STOP_NO_COVERAGE_AVAILABLE)


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def profile_summary(record: v2.BaselineRecordV2, *,
                    policy: Optional[policy_mod.BaselinePolicy] = None
                    ) -> Dict[str, Any]:
    """Per-area profile report. PHI-safe: no milestone text, no raw answers.

    An unprobed area is `not_assessed` with `concern=False` AND
    `assessed=False`. A consumer that reads only `concern` would misread that
    as reassurance, so `areas_not_assessed` is reported explicitly rather than
    left to be derived.
    """
    policy = policy or policy_mod.default_policy()
    chronological = int(record.chronological_months)
    areas: Dict[str, Any] = {}
    for profile_area in AREA_SUBDOMAIN:
        evidence = area_evidence(record, profile_area)
        inventory = area_probes(record.area_id, profile_area)
        age_appropriate = [p for p in inventory if p.months <= chronological]
        concerns = [e for e in evidence
                    if eligibility.counts_as_concern(e.state, e.months,
                                                     chronological)]
        areas[profile_area] = {
            "status": area_status(record, profile_area),
            "not_assessed_cause": not_assessed_cause(record, profile_area),
            "assessed": bool(evidence),
            "probes": len(evidence),
            "concern": bool(concerns),
            "concern_bands": sorted(e.months for e in concerns),
            "uncertain_bands": sorted(e.months for e in evidence
                                      if e.state == v2.STATE_UNKNOWN),
            "strength_bands": sorted(
                e.months for e in evidence
                if eligibility.is_strength(e.state, e.months, chronological)),
            "lowest_probe_months": min((e.months for e in evidence),
                                       default=None),
            "age_appropriate_inventory": len(age_appropriate),
            "required": profile_area in policy.required_profile_areas,
        }
    return {
        "areas": areas,
        "areas_assessed": sorted(a for a, v in areas.items() if v["assessed"]),
        "areas_not_assessed": sorted(a for a, v in areas.items()
                                     if not v["assessed"]),
        "areas_with_concern": sorted(a for a, v in areas.items()
                                     if v["concern"]),
        "areas_recoverable": list(recoverable_areas(record, policy=policy)),
        "areas_unprobeable": sorted(
            a for a, v in areas.items()
            if v["not_assessed_cause"] == NOT_ASSESSED_NO_AGE_APPROPRIATE_SKILL),
        "probes_total": len(record.profile_probes),
    }


__all__ = [
    "AREA_RECEPTIVE", "AREA_FUNCTIONAL", "AREA_SOCIAL", "AREA_INTELLIGIBILITY",
    "AREA_SUBDOMAIN", "ASSESSED", "NOT_ASSESSED",
    "NOT_ASSESSED_NO_AGE_APPROPRIATE_SKILL", "NOT_ASSESSED_BUDGET_CONSUMED",
    "not_assessed_cause", "recoverable_areas", "ASK", "STOP",
    "PROBE_FIRST", "PROBE_DOWNWARD", "PROBE_UPWARD", "DESCEND", "ASCEND",
    "STOP_NORMAL_BUDGET_SPENT", "STOP_NO_COVERAGE_AVAILABLE",
    "BaselineProfileError", "ProfileProbeRef", "ProfileStep",
    "area_probes", "area_bands", "band_representative", "probe_question", "first_probe", "downward_probe",
    "upward_probe", "area_evidence", "area_status", "area_direction",
    "plan_profile_step", "profile_summary",
]
