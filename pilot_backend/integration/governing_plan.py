"""pilot_backend/integration/governing_plan.py — P0-C governing-plan resolution.

Which clinician-approved plan governs the week containing a family's local
date. Pure: it is handed the child's plans and cycles and a clock instant (or a
caller-pinned local date) and decides. No store, no network, no writes.

## Why not the calendar month

Weekly cycles are anchored on the child's first-plan date and run Monday to
Sunday; plans used to be looked up by the server's UTC calendar month. A week
crossing a month boundary (2026-11-30 .. 12-06) therefore fell between plans:
from 12-01 the family saw no week and a release created a December starter week
overlapping the November one. Clinical governance now follows the clinician's
ACTIVATION of a plan (`domain/clinical_period.py`); `cycle_month` remains a
storage, indexing and reporting field and decides nothing here.

## The algorithm

For each plan, "today" is the local date in THAT plan's `timezone_of_record`
(or the caller's pinned `local_date`, which already is a local date).

1. COVERED. If a cycle of the child covers its owning plan's local date, that
   cycle's owning plan governs and that cycle is the current week. Cycles are
   never moved between plans. Two covering cycles is refused, never picked.
2. ACTIVE PERIOD. Otherwise the candidates are ACTIVE plans whose clinical
   period contains their local date, in ACTIVATION order (then id). For each,
   the week is computed on that plan's own cadence: no cycles yet -> a starter
   Week 1 from today; otherwise the starter-anchored sequence covering today.
   The FIRST candidate whose period contains the WHOLE week governs. An earlier
   activated plan is therefore never displaced by a later one while it can
   still hold the week, and a later plan only governs a week the earlier one
   cannot — and only because a clinician activated it.
3. RENEWAL. If a plan was ever activated for this child but none can hold the
   week, the answer is `RENEWAL_REQUIRED`: nothing is created or copied.
4. BOOTSTRAP. A child with no activated plan at all (a draft does not count).
   The release REFUSES it (`ClinicalPlanRequired`): the clinician creates the
   first plan with an explicit `timezone_of_record` and activates it first.
   Nothing ever creates a first plan with a defaulted zone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Sequence

from ..domain.clinical_period import clinical_period_for, local_date_in
from ..domain.weekly_cycle import (
    WeeklyCycleError,
    starter_cycle_bounds,
    starter_sequence_for,
)

WEEK_ONE = 1


class GovernanceOutcome(str, Enum):
    COVERED = "covered"              # an existing cycle covers today
    NEXT_WEEK = "next_week"          # a governing plan, the week not yet created
    RENEWAL_REQUIRED = "renewal_required"
    BOOTSTRAP = "bootstrap"          # the child's first plan ever


class GoverningPlanAmbiguous(ValueError):
    """Two cycles cover one family date. Refused, never picked. PHI-safe."""

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class Governance:
    outcome: GovernanceOutcome
    plan: Optional[object] = None
    #: The local date "today" resolved to, in the governing plan's zone.
    local_date: str = ""
    #: The existing cycle covering today (COVERED only).
    cycle: Optional[object] = None
    sequence: int = 0
    starts_on: str = ""
    ends_on: str = ""
    #: The governing plan's first-plan date; None when Week 1 is still to come.
    first_plan_date: Optional[str] = None
    #: The governing plan's cycles, for the caller's adaptive path.
    plan_cycles: tuple = ()


def _first_plan_date(cycles: Sequence) -> Optional[str]:
    for cycle in cycles:
        if cycle.sequence_in_month == WEEK_ONE:
            return cycle.starts_on
    return None


def _today_for(plan, now: datetime, local_date: Optional[str]) -> str:
    return local_date or local_date_in(plan.timezone_of_record, now)


def resolve_governance(*, plans: Sequence, cycles_by_plan: Dict[str, Sequence],
                       now: datetime,
                       local_date: Optional[str] = None) -> Governance:
    """Decide which plan governs today's week. Pure and deterministic."""
    by_id = {p.focus_plan_id: p for p in plans}

    # 1. COVERED -----------------------------------------------------------
    covering: List = []
    for plan_id, cycles in sorted(cycles_by_plan.items()):
        plan = by_id.get(plan_id)
        if plan is None:
            continue
        today = _today_for(plan, now, local_date)
        covering.extend((plan, c, today) for c in cycles if c.covers(today))
    if len(covering) > 1:
        raise GoverningPlanAmbiguous("more than one week covers this date")
    if covering:
        plan, cycle, today = covering[0]
        cycles = tuple(cycles_by_plan[plan.focus_plan_id])
        return Governance(
            outcome=GovernanceOutcome.COVERED, plan=plan, local_date=today,
            cycle=cycle, sequence=cycle.sequence_in_month,
            starts_on=cycle.starts_on, ends_on=cycle.ends_on,
            first_plan_date=_first_plan_date(cycles), plan_cycles=cycles)

    # 2. ACTIVE PERIOD ---------------------------------------------------------
    candidates = []
    for plan in plans:
        period = clinical_period_for(plan)
        if period is None:
            continue
        today = _today_for(plan, now, local_date)
        if period.contains(today):
            candidates.append((plan.activated_at, plan.focus_plan_id, plan,
                               period, today))
    for _at, _id, plan, period, today in sorted(candidates,
                                                key=lambda c: (c[0], c[1])):
        cycles = tuple(cycles_by_plan.get(plan.focus_plan_id, ()))
        anchor = _first_plan_date(cycles)
        try:
            if anchor is None:
                sequence = WEEK_ONE
                starts_on, ends_on, _p, _r = starter_cycle_bounds(today, 1)
            else:
                sequence = starter_sequence_for(anchor, today)
                starts_on, ends_on, _p, _r = starter_cycle_bounds(
                    anchor, sequence)
        except WeeklyCycleError:
            continue
        if period.contains_span(starts_on, ends_on):
            # A cycle of this plan may already hold THIS sequence without
            # covering today (a draft with other bounds). It is returned so the
            # caller VERIFIES it — and refuses it — exactly as before, rather
            # than racing it for the sequence claim.
            existing = next((c for c in cycles
                             if c.sequence_in_month == sequence), None)
            return Governance(
                outcome=GovernanceOutcome.NEXT_WEEK, plan=plan,
                local_date=today, cycle=existing, sequence=sequence,
                starts_on=starts_on, ends_on=ends_on,
                first_plan_date=anchor, plan_cycles=cycles)

    # 3 / 4 --------------------------------------------------------------------
    ever_activated = any(p.activated_at is not None for p in plans)
    return Governance(outcome=(GovernanceOutcome.RENEWAL_REQUIRED
                               if ever_activated
                               else GovernanceOutcome.BOOTSTRAP))


__all__ = [
    "Governance",
    "GovernanceOutcome",
    "GoverningPlanAmbiguous",
    "resolve_governance",
]
