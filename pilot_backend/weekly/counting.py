"""pilot_backend/weekly/counting.py — totals that cannot double-count.

The rule from section 6, as code:

    ObservationEvent      is the unit of ATTEMPT COUNT
    ActivityGoalAlignment is the unit of ATTRIBUTION

One activity supporting goals A and B, attempted once:

    total attempts             = 1
    goal-A attributed attempts = 1
    goal-B attributed attempts = 1

and `1 + 1 = 2` is a number that means nothing. `total_attempts` counts
DISTINCT event ids and never consults the per-goal streams, so there is no
code path by which summing them could become a total. `CoverageSummary`
carries the overlap explicitly rather than leaving a caller to notice it.

MonthEndReport is out of scope (F/G). These helpers exist now so that when it
is built, the arithmetic it needs is already proven rather than re-derived
under deadline.

## Attribution is by LOCAL month

`attribution_month` on each event, never the owning cycle's month. A cycle
spanning Oct 26 – Nov 1 contributes its Nov 1 attempt to November. Filtering
by cycle would put it in October and the same attempt would be counted twice
across a two-month report, or dropped from one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

from ..domain.alignment import ActivityGoalAlignment
from ..domain.goals import GoalRef
from ..domain.observation import AttemptOutcome, ObservationEvent


class CountingError(ValueError):
    """Invalid counting input. PHI-safe."""

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class GoalAttribution:
    """What one goal can claim. These numbers DO overlap across goals."""

    goal_ref: GoalRef
    scheduled: int = 0
    attempted: int = 0
    completed: int = 0
    #: Event ids attributed here. Overlaps with other goals by design.
    event_ids: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_ids", tuple(self.event_ids))


@dataclass(frozen=True)
class CoverageSummary:
    """Totals and per-goal attributions, with the overlap made explicit."""

    #: Distinct scheduled activity instances.
    total_scheduled: int
    #: Distinct ObservationEvents. The ONLY correct attempt total.
    total_attempts: int
    #: Distinct events whose outcome was did_it. Not a mastery claim.
    total_completed: int
    per_goal: Tuple[GoalAttribution, ...]
    #: Events attributed to more than one goal. The reason per-goal figures
    #: must never be summed.
    multi_goal_event_ids: Tuple[str, ...] = ()

    @property
    def sum_of_goal_attempts(self) -> int:
        """Deliberately NOT a total.

        Exposed so a test can assert it differs from `total_attempts` when
        overlap exists, and named so nobody reaches for it by accident.
        """
        return sum(g.attempted for g in self.per_goal)

    @property
    def has_overlap(self) -> bool:
        return bool(self.multi_goal_event_ids)


def goals_for_instance(alignments: Iterable[ActivityGoalAlignment]
                       ) -> Mapping[str, Tuple[GoalRef, ...]]:
    """activity_instance_ref -> the goals it is aligned to, deduplicated."""
    index: Dict[str, list] = {}
    for alignment in alignments:
        bucket = index.setdefault(alignment.activity_instance_ref, [])
        if alignment.goal_ref not in bucket:
            bucket.append(alignment.goal_ref)
    return {ref: tuple(sorted(goals, key=lambda g: g.as_key()))
            for ref, goals in index.items()}


def summarize(alignments: Sequence[ActivityGoalAlignment],
              events: Sequence[ObservationEvent], *,
              attribution_month: Optional[str] = None) -> CoverageSummary:
    """Totals and per-goal attributions.

    `attribution_month` filters by each event's OWN local-date attribution,
    never by its cycle — see the module docstring.
    """
    by_instance = goals_for_instance(alignments)

    considered = [e for e in events
                  if attribution_month is None
                  or e.attribution_month == attribution_month]

    # Distinct events. Deduplicated on event_id because the same event must
    # never be counted twice no matter how many alignments reference it.
    seen_events: Dict[str, ObservationEvent] = {}
    for event in considered:
        seen_events[event.event_id] = event

    per_goal: Dict[str, Dict[str, object]] = {}
    multi_goal: list = []

    for instance_ref, goals in by_instance.items():
        for goal in goals:
            bucket = per_goal.setdefault(goal.as_key(), {
                "ref": goal, "scheduled": 0, "attempted": 0,
                "completed": 0, "events": [],
            })
            bucket["scheduled"] = int(bucket["scheduled"]) + 1

    for event in seen_events.values():
        goals = by_instance.get(event.activity_instance_ref, ())
        if len(goals) > 1:
            multi_goal.append(event.event_id)
        for goal in goals:
            bucket = per_goal.setdefault(goal.as_key(), {
                "ref": goal, "scheduled": 0, "attempted": 0,
                "completed": 0, "events": [],
            })
            bucket["attempted"] = int(bucket["attempted"]) + 1
            if event.attempt_outcome is AttemptOutcome.DID_IT:
                bucket["completed"] = int(bucket["completed"]) + 1
            bucket["events"].append(event.event_id)  # type: ignore[union-attr]

    attributions = tuple(
        GoalAttribution(
            goal_ref=bucket["ref"],                     # type: ignore[arg-type]
            scheduled=int(bucket["scheduled"]),
            attempted=int(bucket["attempted"]),
            completed=int(bucket["completed"]),
            event_ids=tuple(sorted(bucket["events"])),  # type: ignore[arg-type]
        )
        for _, bucket in sorted(per_goal.items())
    )

    return CoverageSummary(
        # Distinct INSTANCES, not alignments: a two-goal activity is one
        # scheduled opportunity.
        total_scheduled=len(by_instance),
        total_attempts=len(seen_events),
        total_completed=sum(1 for e in seen_events.values()
                            if e.attempt_outcome is AttemptOutcome.DID_IT),
        per_goal=attributions,
        multi_goal_event_ids=tuple(sorted(multi_goal)),
    )
