# P0-B1 — repeated-signal adaptation and adaptive Week 2

Status: implemented on `feature/october-pilot-p0b1-adaptive-week2` (from
`99bef26`). **Not deployed. No Week 2 released anywhere.** Founder approval is
required before any staging deployment or live Week 2 release.

Rule version: `weekly-adaptation-rules-2026.11-p0b` (supersedes
`weekly-adaptation-rules-2026.10`).

## What Genex may change on its own

Only WHICH reviewed activity a family is offered next week. Never the clinical
goal, its version, its canonical anchor/rung, the anchor's activity families, a
progression or regression, or a clinical interpretation.

## Policy (per previous-week activity, CURRENT observations only)

`current_observations()` is applied first, so a superseded submission is never
evidence. "Consecutive" means the LAST TWO current observations of ONE activity,
on DIFFERENT practice dates, ordered by date, time, then event id. Signals on
different activities are never pooled toward the goal.

| Last two current observations of one activity | Branch | Reason |
|---|---|---|
| share `didnt_want_to_try`, or share enjoyment `disliked` (Parent "not really") | `CHANGE_THEME` | `repeated_disengagement` |
| both `too_hard` | `NO_CHANGE` | `not_yet_supported:repeated_too_hard` |
| both `too_easy` | `NO_CHANGE` | `not_yet_supported:repeated_too_easy` |
| both `wasnt_ready_yet` | `NO_CHANGE` | `not_yet_supported:repeated_wasnt_ready` |
| anything else (incl. ONE of any signal, or none) | `MAINTAIN_VARY` | `no_trigger` |

* `CHANGE_THEME` is not an inference of inability and never touches the goal.
* `NO_CHANGE` schedules the SAME reviewed activity again and records the reason
  for a clinician. It never falls back to the retired easier/harder behaviour.
* `too_easy` is never mastery or progression.
* The 0.4E engine (`weekly/adaptation.py`) no longer derives any difficulty
  direction from caregiver evidence or family plan edits. Clinician
  FUTURE_CYCLE interventions and caregiver defers are unchanged.

## Selection (reviewed bank only, deterministic)

1. `NO_CHANGE` activities are carried.
2. Fresh templates fill the remaining slots: reviewed, inside the goal anchor's
   families, not scheduled last week. If any `CHANGE_THEME` fired, every fresh
   template must also have a theme not used last week.
3. Remaining slots reuse last week's `MAINTAIN_VARY` templates (never a
   `CHANGE_THEME` one).
4. A `CHANGE_THEME` with no new-theme template available is a refusal.

Ordering is by `activity_template_id` (a content digest) only. No card-level
difficulty ordering exists or is invented. Slots = `min(declared capacity,
cycle day count)`.

## Composition (`POST …/weekly-cycles/current/release`, unchanged URL)

Sequence 1 behaves exactly as before. For sequence ≥ 2 the route runs:

    released Week N + its snapshot
      -> boundary pre-check: same goal ids, same goal versions (from the
         Week N snapshot), same anchor rung, Week N families within the anchor
      -> current observations -> P0-B1 policy
      -> reviewed selection -> reviewed/family gate -> invariant proof
      -> create_cycle(predecessor = Week N) -> existing allocator
      -> AdaptationRecord + WeeklyAdaptationDecision (one transaction)
      -> snapshot -> release

Every boundary check runs before the first write. Refusals (all render as the
route's constant 403): `NonAdaptiveWeekRefused` (the old plain
`create_cycle` path for Week 2+ is closed), `AdaptiveWeekUnavailable` (no
released Week N), `AdaptiveWeekIncomplete` (an unreleased Week N+1 draft
exists — refused, never resumed), `ClinicalBoundaryRefused`.

New create-only collection: `pilot_weekly_adaptation_decisions`, keyed by the
cycle it explains (one decision per week). The decision cannot be constructed
if goal/version/anchor differ or a family leaves the anchor.

## BLOCKING PREREQUISITE BEFORE WEEK 3 — December / month boundary

`this_week` and the release route both resolve the plan by the CURRENT CALENDAR
MONTH. For the positive-path child (first plan 2026-11-17):

* Week 3 is Mon 2026-11-30 – Sun 2026-12-06 and spans the month boundary.
* From **2026-12-01**, `this_week` looks for a December plan, finds none, and
  returns no week — even though a released Week 3 covers that date.
* From 2026-12-01, the release route would create a December plan and,
  finding no cycles in it, a NEW starter Week 1 (Dec 1–6) overlapping Week 3.

P0-B1 does not repair this (explicitly deferred). **It must be fixed before any
Week 3 is released, and no release may be called for this child on or after
2026-12-01 until it is.** Week 2 (2026-11-23 – 2026-11-29) is entirely inside
November and is not affected.

## Deferred from P0-B1

Card-level easier/harder ordering; autonomous dosage changes; free-text
escalation; a mechanism taxonomy; P0-B2 rules 1/2/6 (same-card scaffold /
challenge); the December repair above; gap handling when a week is missed
(currently a fail-closed `AdaptiveWeekUnavailable`).
