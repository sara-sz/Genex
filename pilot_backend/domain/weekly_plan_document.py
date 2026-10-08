"""pilot_backend/domain/weekly_plan_document.py — the released week's content.

0.6A-2. An EXPLICIT, versioned schema for what a family was given in a week,
plus the minimum-necessary Parent projection derived from it.

## WHY THIS EXISTS AND WHAT IT REPLACES

`WeeklyPlanSnapshot.resolved_plan_document` is a JSON STRING, and its docstring
says why: it was an OPAQUE capture of ANOTHER system's document — Parent's
unversioned weekly plan — and "giving it a schema here would be a claim this
layer cannot keep".

That reasoning was right for a foreign document and is wrong for this one. The
Pilot now AUTHORS the week: the content comes from our own reviewed static bank,
the placements come from our own allocator, and the shape is ours to version. So
the snapshot still stores a JSON string — the storage contract is unchanged, and
no codec or repository moves — but the document inside it has a declared schema
that this module owns and validates.

The founder's rule was explicit: the opaque document must NOT become the real
product contract. It does not. `parent_week_view` is the contract, it is derived
from this schema, and it is a strict allowlist.

## TWO VIEWS, ONE STORED TRUTH

    build_document(...)  -> the INTERNAL record, frozen into the snapshot.
                            Carries `activity_template_id` and `goal_id` so a
                            clinician can later ask which approved template
                            produced a released activity.

    parent_week_view(..) -> what a family may read. Derived from the stored
                            document by an allowlist, never re-derived from the
                            allocator — so what the Parent sees is exactly what
                            was frozen at release.

Deriving the Parent view from the SNAPSHOT rather than from live records is the
whole point of item 12: a later goal revision changes the goal, and the released
week keeps saying what it said.

## WHAT THE PARENT VIEW OMITS, AND WHY EACH IS OMITTED

    capacity ledger / declared capacity   a planning constraint, not content
    emphasis weights / priority rank      internal ordering, meaningless alone
    canonical rung + track refs           Gold Standard identity, not family-facing
    generation claims                     provenance of a decision, not of content
    ActivityGoalAlignment rows            attribution bookkeeping
    coverage gaps                         a PLANNER condition; shown to a family
                                          it would read as something they failed
    clinician-only notes / RTM fields     not theirs

## `duration_minutes` AND `why` ARE ABSENT ON PURPOSE

Neither exists in the reviewed activity source, and no approved policy derives
them. Inventing a duration would be a clinical dosage statement authored by a
serialiser, so the fields are simply not in the schema. `group_play_line` and
`theme` are likewise omitted: they are authoring metadata rather than
instructions to a family.

## THE ONE RENDERING DECISION, STATED PLAINLY

The allocator produces placements with NO dates — it decides what, never when.
Rendering `days[]` therefore needs a rule, and this module uses the smallest one
available: activities are laid out ONE PER DAY in allocator order, starting on
the cycle's first local date.

That introduces no count (the count is the family's declared capacity) and no
repetition (no activity is placed twice). It is a presentation rule, NOT a
treatment frequency: nothing here says a family must do exactly one activity a
day, and days beyond the placed activities simply carry none.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: The schema of the document frozen into `WeeklyPlanSnapshot`. Versioned
#: because it is OURS: a later shape change must be visible rather than inferred.
WEEKLY_PLAN_DOCUMENT_SCHEMA = "pilot-weekly-plan-document-v1"

#: The Parent-facing projection's own version, distinct from the stored
#: document's. The two can legitimately move apart: a new internal field must not
#: force a client change, and a client contract change must not rewrite history.
PARENT_WEEK_VIEW_SCHEMA = "pilot-parent-week-v1"

#: Exactly the reviewed template fields a family may read. An ALLOWLIST, so a
#: field added to `ActivityTemplate` stays internal until someone adds it here.
#:
#: `theme` and `group_play_line` are deliberately excluded as authoring metadata;
#: `duration_minutes` and `why` do not exist in the source at all.
PARENT_ACTIVITY_CONTENT_FIELDS: Tuple[str, ...] = (
    "title",
    "materials",
    "instructions",
    "success_criteria",
    "make_easier",
    "make_harder",
    "what_to_avoid",
)

#: Identifiers the product needs on a Parent-facing activity. `activity_template_id`
#: is included because the founder named template identity as a permitted product
#: identifier, and because it is what makes a released activity traceable.
PARENT_ACTIVITY_IDENTITY_FIELDS: Tuple[str, ...] = (
    "activity_instance_ref",
    "activity_template_id",
    "goal_id",
)

#: Keys that must never appear anywhere in a Parent-facing week.
FORBIDDEN_PARENT_FIELDS: Tuple[str, ...] = (
    "family_declared_capacity", "declared_capacity", "capacity",
    "allocated_by_planner", "emphasis_weight", "priority_rank",
    "rung_ref", "track_ref", "taxonomy_version", "baseline_version",
    "generation_key", "claim_id", "alignment_id", "coverage_gap",
    "coverage_gaps", "gap_id", "ledger_id", "rule_version",
    "milestone_refs", "difficulty_tier", "placed_in_stage",
    "alignment_source", "role", "theme", "group_play_line",
    "source_tier", "source_pool_ref", "duration_minutes", "why",
)


class WeeklyPlanDocumentError(ValueError):
    """The weekly plan document is malformed. PHI-safe."""

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class ReleasedActivity:
    """One placed activity, with its content materialised and its lineage kept.

    `activity_template_id` is retained ALONGSIDE the copied content, which is
    item 9's requirement: materialising the words into an immutable snapshot
    must not lose which approved template produced them.
    """

    activity_instance_ref: str
    activity_template_id: str
    activity_family_ref: str
    goal_id: str
    local_date: str
    content: Mapping[str, str]

    def __post_init__(self) -> None:
        for label in ("activity_instance_ref", "activity_template_id",
                      "goal_id", "local_date"):
            if not (getattr(self, label) or "").strip():
                raise WeeklyPlanDocumentError(
                    f"a released activity requires {label}")
        missing = [f for f in PARENT_ACTIVITY_CONTENT_FIELDS
                   if f not in self.content]
        if missing:
            raise WeeklyPlanDocumentError(
                "a released activity is missing reviewed content")

    def to_document(self) -> Dict[str, Any]:
        return {
            "activity_instance_ref": self.activity_instance_ref,
            "activity_template_id": self.activity_template_id,
            "activity_family_ref": self.activity_family_ref,
            "goal_id": self.goal_id,
            "local_date": self.local_date,
            "content": {name: str(self.content[name])
                        for name in PARENT_ACTIVITY_CONTENT_FIELDS},
        }


def assign_local_dates(local_dates: Sequence[str], count: int) -> Tuple[str, ...]:
    """One activity per day, in order, from the cycle's first local date.

    The smallest rule that can render `days[]` at all — see the module
    docstring. A PRESENTATION rule, not a treatment frequency.

    Refuses when there are more activities than days rather than doubling up: a
    second activity on one day would be a density decision nobody approved.
    """
    if count < 0:
        raise WeeklyPlanDocumentError("an activity count cannot be negative")
    if count > len(local_dates):
        raise WeeklyPlanDocumentError(
            "more activities were placed than the cycle has days")
    return tuple(local_dates[index] for index in range(count))


def build_document(*, cycle, placements: Sequence[Any],
                   templates_by_id: Mapping[str, Any],
                   goal_text_by_id: Mapping[str, str],
                   goal_version_by_id: Mapping[str, str],
                   released_at: Optional[datetime] = None) -> Dict[str, Any]:
    """The document frozen into the snapshot. Explicit, versioned, ours.

    `placements` are the allocator's `PlannedActivity` values, in allocator
    order — which is deterministic, so two builds of one allocation produce the
    same document.

    `goal_version_by_id` records the EXACT version in force at release (item
    12). It lives in the stored document rather than being looked up later,
    because a later revision must not be able to change what the week says.
    """
    local_dates = cycle.local_dates()
    dates = assign_local_dates(local_dates, len(placements))

    activities: List[ReleasedActivity] = []
    for index, placement in enumerate(placements):
        template = templates_by_id.get(placement.activity_identity_ref)
        if template is None:
            # Every placement MUST come from a reviewed template. A placement
            # with no template would be content from nowhere.
            raise WeeklyPlanDocumentError(
                "a placement does not correspond to a reviewed template")
        goal_ids = sorted({ref.goal_id for ref, _role in placement.aligned_goals})
        if not goal_ids:
            raise WeeklyPlanDocumentError(
                "a placement is not aligned to any goal")
        activities.append(ReleasedActivity(
            activity_instance_ref=placement.activity_instance_ref,
            activity_template_id=template.activity_template_id,
            activity_family_ref=template.activity_family_ref,
            # One goal per activity in this slice; the first by id keeps the
            # document deterministic if a future activity serves several.
            goal_id=goal_ids[0],
            local_date=dates[index],
            content=template.as_card()))

    goals = [{"goal_id": goal_id,
              "goal_version_id": goal_version_by_id.get(goal_id, ""),
              "text": goal_text_by_id.get(goal_id, "")}
             for goal_id in sorted({a.goal_id for a in activities})]

    return {
        "document_schema": WEEKLY_PLAN_DOCUMENT_SCHEMA,
        "cycle_id": cycle.cycle_id,
        "child_id": cycle.child_id,
        "owning_focus_plan_id": cycle.owning_focus_plan_id,
        "sequence_in_month": int(cycle.sequence_in_month),
        "starts_on": cycle.starts_on,
        "ends_on": cycle.ends_on,
        "released_at": released_at.isoformat() if released_at else "",
        "goals": goals,
        "activities": [activity.to_document() for activity in activities],
    }


def parent_week_view(document: Mapping[str, Any], *,
                     released_at: Optional[datetime] = None) -> Dict[str, Any]:
    """The family-facing week. A strict ALLOWLIST over the stored document.

    Built by naming every field rather than by copying and deleting: a
    subtractive filter ships whatever is added to the document later, which is
    how internal planning data leaks into a family's view.

    Days are listed for the WHOLE cycle, including days with no activity. A
    family seeing Thursday absent entirely cannot tell "nothing scheduled" from
    "the app lost it".
    """
    if document.get("document_schema") != WEEKLY_PLAN_DOCUMENT_SCHEMA:
        raise WeeklyPlanDocumentError(
            "this is not a pilot weekly plan document")

    from datetime import date, timedelta

    start = date.fromisoformat(str(document["starts_on"]))
    end = date.fromisoformat(str(document["ends_on"]))
    span = (end - start).days + 1
    all_dates = [(start + timedelta(days=offset)).isoformat()
                 for offset in range(span)]

    by_date: Dict[str, List[Dict[str, Any]]] = {d: [] for d in all_dates}
    for row in document.get("activities") or ():
        local_date = str(row["local_date"])
        if local_date not in by_date:
            raise WeeklyPlanDocumentError(
                "a released activity falls outside its cycle")
        content = row["content"]
        activity: Dict[str, Any] = {
            "activity_instance_ref": str(row["activity_instance_ref"]),
            "activity_template_id": str(row["activity_template_id"]),
            "goal_id": str(row["goal_id"]),
        }
        for name in PARENT_ACTIVITY_CONTENT_FIELDS:
            activity[name] = str(content[name])
        by_date[local_date].append(activity)

    released = released_at.isoformat() if released_at else str(
        document.get("released_at") or "")
    return {
        "view_schema": PARENT_WEEK_VIEW_SCHEMA,
        "week": {
            "cycle_id": str(document["cycle_id"]),
            "starts_on": str(document["starts_on"]),
            "ends_on": str(document["ends_on"]),
            "released_at": released,
        },
        # Goal TEXT and id only — no rung ref, no anchor, no version lineage.
        "goals": [{"goal_id": str(goal["goal_id"]),
                   "text": str(goal.get("text") or "")}
                  for goal in (document.get("goals") or ())],
        "days": [{"local_date": local_date,
                  "activities": by_date[local_date]}
                 for local_date in all_dates],
    }


def assert_no_forbidden_fields(payload: Any) -> None:
    """Walk a Parent-facing payload and refuse any internal key.

    Defence in depth over the allowlist above. Recursive, because the leak that
    matters would be nested inside an activity rather than at the top level.
    """
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if key in FORBIDDEN_PARENT_FIELDS:
                raise WeeklyPlanDocumentError(
                    "an internal planning field reached a Parent view")
            assert_no_forbidden_fields(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            assert_no_forbidden_fields(item)


__all__ = [
    "FORBIDDEN_PARENT_FIELDS",
    "PARENT_ACTIVITY_CONTENT_FIELDS",
    "PARENT_ACTIVITY_IDENTITY_FIELDS",
    "PARENT_WEEK_VIEW_SCHEMA",
    "ReleasedActivity",
    "WEEKLY_PLAN_DOCUMENT_SCHEMA",
    "WeeklyPlanDocumentError",
    "assert_no_forbidden_fields",
    "assign_local_dates",
    "build_document",
    "parent_week_view",
]
