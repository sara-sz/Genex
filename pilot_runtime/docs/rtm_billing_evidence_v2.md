# RTM billing-evidence v2 — billing months are not clinical plans

Status: implemented on `feature/october-pilot-rtm-billing-evidence` (from the
green P0-C commit `c610145`). **Not merged, not deployed, not pushed.**

## Two different periods

| | Clinical plan (P0-C) | RTM treatment-management period |
|---|---|---|
| Identity | a plan id; rolling | `(episode_id, billing calendar month)` |
| Length | 28 days from clinician activation | one calendar month (codes 98979–98981 are calendar-month in `coding/rules.py`) |
| Purpose | governs which weeks are planned | collects evidence attributed to the month |

A billing month may draw on several clinical plans; a clinical plan may feed
several billing months. Neither scopes the other.

## Period identity

`open_period(principal, episode_id, focus_plan_id, *, cycle_month)`:

* `cycle_month` is the **explicit** billing month — never inferred from a
  plan's month label. A month can be opened when no plan carries its label,
  when a plan crosses into it, or when two rolling plans contribute.
* The claim stays `RTM_MONITORING_PERIOD(episode_id, month)`.
* `focus_plan_id` is **opening provenance** and the source of the period's
  `timezone_of_record` (sound only because of the one-zone invariant below).
  It scopes nothing.

## Two independent streams (`RTMService._gather`)

What was **scheduled** and what evidence was **received** are separate facts,
and neither is derived from the other.

### Observed evidence (`_current_month_events`)

1. All of the child's observations: `observation_events.list_for_child`.
2. `current_observations()` over **all** of them (so a superseded row is
   excluded even if its correction were in another month).
3. Keep those whose own `attribution_month` (local date) is the billing month.
4. One row per `event_id`.

Drives attempts, outcome counts, `distinct_observed_local_dates` and per-goal
attributed attempts/completions.

### Scheduled opportunities (`_scheduled_in_month`)

1. Every cycle of the child (`weekly_cycles.list_for_child`) — **every plan**,
   never only the opening plan.
2. Keep RELEASED cycles whose span touches the billing month. An unreleased
   week was never given to the family and schedules nothing.
3. Read the cycle's single released snapshot (exactly one, else refuse). For a
   pilot weekly-plan document (`pilot-weekly-plan-document-v1`), each released
   activity carries its scheduled `local_date`; keep those dated in the month.
   A date outside its own week, a document naming another week, or an
   unreadable document fails closed.
4. A legacy/foreign capture has no dates: it counts (all of the cycle's
   aligned instances) only when the whole week lies inside the month, where
   every activity necessarily does; a legacy week straddling the boundary is
   refused, never guessed.
5. Opportunities are `(cycle_id, activity_instance_ref)` pairs, counted as
   DISTINCT instances (`total_scheduled_opportunities`) and, per goal, through
   the alignments (`scheduled_opportunities`; overlaps across goals by design).

Independent of submission: no observation, correction or supersession can add
or remove one. `derive_evidence_summary` takes them as an explicit input and
never infers them from alignments or observations.

### Record cycles and contributing plans

* `cycle_refs` = cycles of either stream (an in-month current observation OR an
  in-month scheduled activity); the report's adaptations and coverage gaps
  come from exactly these cycles.
* `contributing_focus_plan_ids` = those cycles' owning plans — any plan that
  materially contributes observations OR scheduled opportunities (and thus the
  report provenance drawn from them). Sorted, unique. A plan with scheduled
  activities and zero observations is included.
* Alignments come from those cycles. Reviews, actions, clinician time and
  interactions stay period-scoped and filtered by their own local dates,
  unchanged.

## Review validation

A cited observation must be the child's, CURRENT, and attributed to the billing
month — from any clinical plan. Superseded, other-month, other-child and unknown
ids are refused.

## Provenance and versions

* `RTMEvidenceSummary` and `MonthEndReport` gain `contributing_focus_plan_ids`
  (sorted, unique); `RTMEvidenceSummary` gains `total_scheduled_opportunities`,
  shown in report section B beside the received evidence. `focus_plan_ref` / `focus_plan_id` remain as opening
  provenance.
* Report section A carries each contributing plan's own activation-time goal
  snapshots, keyed `"<plan_id>:<goal_id>"` — never collapsed onto one plan.
* Schema versions: `october-pilot-rtm-period-v2`,
  `october-pilot-rtm-evidence-v2`, `october-pilot-month-end-report-v2`. The
  codec pins each class to its version in BOTH directions; a v1 row is refused
  by name, nothing can be written under another version, exact-field decoding is
  unchanged. `EVIDENCE_RULE_VERSION` → `rtm-evidence-derivation-2026-v2`.

## Timezone authority — one zone per child (founder decision)

**Invariant:** every clinician-approved (ever activated: ACTIVE or CLOSED) plan
of a child shares one `timezone_of_record`. The RTM period takes its zone from
the opening plan only because of this invariant.

* **Enforced where a plan joins the record:** `PlanningService.activate_plan`
  refuses a plan whose zone differs from any approved plan of the child
  (`ClinicalTimezoneMismatch`, a `GoalConflict`; route: the existing constant
  403). The refusal writes nothing. A draft fixes nothing — an abandoned draft
  in another zone does not block a same-zone successor. Activations are already
  serialised by the `CLINICAL_GOVERNANCE` claim and a plan's zone is
  immutable, so the out-of-transaction read cannot admit a second zone.
* **Defence in RTM:** `open_period` and every `_gather` (summary, coding,
  report) refuse (`RTMTimezoneInconsistent`, an `RTMConflict`) if any approved
  plan of the child disagrees with the period's zone — legacy or forged state.
  Nothing is converted, picked between or mixed; a refused report is not
  written.

Observations and clinician time still carry their own local dates; the
period's zone is the label copied onto time entries and interactions.

## Semantics that changed (stated, not hidden)

* Scheduled opportunities are released activities whose **scheduled local
  date** is in the billing month, from every plan, observed or not (was: every
  aligned instance of the one bound plan's cycles, in any month, released or
  not).
* Corrected observations count once (was: original + correction).

## Backward compatibility

The v2 schema strings are unchanged by the scheduled-opportunity and timezone
follow-up: v2 had never been pushed, deployed or persisted, so it is defined
once, by this branch's final state.

No HTTP route opens an episode or a period; `open_period` is service-only. All
callers (tests) now pass `cycle_month`. Staging holds **zero** RTM documents of
any kind, so no migration exists to run.
