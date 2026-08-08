"""Canonical current-weekly-plan resolution (Phase 1B.2F.0).

**Definition.** A child's current weekly plan is *exactly one* `WeeklyPlan` for
that child whose lifecycle `status` is `CURRENT`. Nothing else makes a plan
current.

## Why this module exists

Seven services previously answered "which plan is current?" with:

```python
plans = repo.query(C.WEEKLY_PLANS, child_id=child_id)
current_plan_id = plans[0]["id"] if plans else None
```

That is not under-determined — it is **wrong in the direction that matters**.
`query` iterates the collection in insertion order, so `plans[0]` is the
FIRST-SEEDED plan. Once a child has a second plan, a newly created one is
appended and can therefore *never* become current: the rule pins currency to
whatever was stored earliest, permanently. On Firestore it degrades further,
because a query without `order_by` has no defined order at all.

Two derived alternatives were considered and rejected:

* **"the week containing today"** — makes behavior depend on the wall clock, and
  would classify every fictional fixture plan as historical.
* **`max(week_start_date)`** — means *latest*, not *current*: a drafted future
  week would become current the moment it is written, which is precisely the
  stale-write hazard this resolver exists to prevent.

So currency is an explicit stored fact, exactly like `AssignmentStatus` and
`ProposalStatus` already are in this domain.

## Fail-closed contract

`current_weekly_plan` returns `None` when **zero** plans are `CURRENT` **or when
more than one is**. An ambiguous lifecycle is never resolved by picking one.
Callers map `None` onto their own existing typed error (writes) or onto their
existing "no current plan" behavior (reads) — no caller may guess, and none may
repair the state.

This module identifies lifecycle. It does **not** implement lifecycle
transitions: there is no activate-draft, no complete-current, no weekly rollover
and no scheduled transition anywhere in the service yet.

Pure and read-only. Nothing here mutates, promotes, completes or creates a plan.
"""

from __future__ import annotations

from typing import List, Optional

from ..domain.audit_state import plain
from ..domain.enums import WeeklyPlanStatus
from ..repository import collections as C
from ..repository.interface import CollaborationRepository


def _is_current(plan: dict) -> bool:
    """Whether a stored plan record claims the CURRENT lifecycle status.

    A record with no `status` key at all reads as CURRENT, matching the model
    default, so plans written before this field existed keep their meaning.
    """
    return plain(plan.get("status", WeeklyPlanStatus.CURRENT.value)) == \
        WeeklyPlanStatus.CURRENT.value


def current_weekly_plans(
    repo: CollaborationRepository, child_id: str
) -> List[dict]:
    """Every plan for `child_id` claiming CURRENT. Normally exactly one."""
    return [p for p in repo.query(C.WEEKLY_PLANS, child_id=child_id) if _is_current(p)]


def current_weekly_plan(
    repo: CollaborationRepository, child_id: str
) -> Optional[dict]:
    """The child's single CURRENT weekly plan, or None.

    Returns None when zero plans are CURRENT **and** when several are. Both are
    ambiguous lifecycles that must fail closed at the call site rather than be
    resolved here by picking one.

    Independent of insertion order, query order and `week_start_date`.
    """
    plans = current_weekly_plans(repo, child_id)
    if len(plans) != 1:
        return None
    # Unpacked rather than indexed: after the length check the two are identical,
    # but this module must contain no positional plan selection at all, so the
    # source-level pin forbidding `plans[0]` can be exact rather than carve out an
    # exception here.
    only_plan, = plans
    return only_plan


def current_weekly_plan_id(
    repo: CollaborationRepository, child_id: str
) -> Optional[str]:
    """Convenience for the many call sites that only compare the plan id."""
    plan = current_weekly_plan(repo, child_id)
    return plan["id"] if plan else None
