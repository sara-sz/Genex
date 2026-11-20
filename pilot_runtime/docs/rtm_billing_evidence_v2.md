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
  `timezone_of_record`. It scopes nothing.

## Evidence-gathering algorithm (`RTMService._current_month_events`, `_gather`)

1. All of the child's observations: `observation_events.list_for_child`.
2. `current_observations()` over **all** of them (so a superseded row is
   excluded even if its correction were in another month).
3. Keep those whose own `attribution_month` (local date) is the billing month.
4. One row per `event_id`.
5. Contributing cycles = the surviving events' `owning_cycle_id`s; contributing
   plans = those cycles' `owning_focus_plan_id`s (sorted, unique).
6. Alignments come from the contributing cycles. Reviews, actions, clinician
   time and interactions stay period-scoped and filtered by their own local
   dates, unchanged.

`derive_evidence_summary` applies `current_observations()` and the per-id
dedupe again itself, so no caller can make a superseded row and its correction
— or one row twice — count.

## Review validation

A cited observation must be the child's, CURRENT, and attributed to the billing
month — from any clinical plan. Superseded, other-month, other-child and unknown
ids are refused.

## Provenance and versions

* `RTMEvidenceSummary` and `MonthEndReport` gain `contributing_focus_plan_ids`
  (sorted, unique). `focus_plan_ref` / `focus_plan_id` remain as opening
  provenance.
* Report section A carries each contributing plan's own activation-time goal
  snapshots, keyed `"<plan_id>:<goal_id>"` — never collapsed onto one plan.
* Schema versions: `october-pilot-rtm-period-v2`,
  `october-pilot-rtm-evidence-v2`, `october-pilot-month-end-report-v2`. The
  codec pins each class to its version in BOTH directions; a v1 row is refused
  by name, nothing can be written under another version, exact-field decoding is
  unchanged. `EVIDENCE_RULE_VERSION` → `rtm-evidence-derivation-2026-v2`.

## Timezone authority — OPEN (founder decision)

The model has no timezone independent of a clinical plan: `RTMEpisode` and
`Child` carry none; only `MonthlyFocusPlan.timezone_of_record` exists. Billing
attribution does **not** use the period's timezone — observations and clinician
time carry their own local dates — so the period's timezone is only a label
copied onto time entries and interactions. This slice keeps the pre-existing
rule (timezone from the plan the period is opened against) and invents none.

## Semantics that changed (stated, not hidden)

* `scheduled_opportunities` / `total_scheduled` now count the activity
  instances of the cycles that produced current evidence in the month (was:
  every cycle of the one bound plan, across all months).
* Corrected observations count once (was: original + correction).

## Backward compatibility

No HTTP route opens an episode or a period; `open_period` is service-only. All
callers (tests) now pass `cycle_month`. Staging holds **zero** RTM documents of
any kind, so no migration exists to run.
