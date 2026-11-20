# P0-C — clinical-plan governance across calendar boundaries

Status: implemented on `feature/october-pilot-p0c-month-boundary` (from
`50a3598`). **Not merged, not deployed.** Isolated from the live P0-B1 staging
proof.

## The defect it closes

Weekly cycles are anchored on the child's first-plan date (Monday–Sunday after a
starter week); plans were looked up by the server's **UTC calendar month**. A
week crossing a month boundary (positive path: Week 3, 2026-11-30 .. 12-06)
therefore fell between plans: from 12-01 the family's `this-week` was empty and
a release created a December plan with a non-adaptive starter week overlapping
Week 3. The UTC date also flipped the boundary up to five hours early for US
families.

## Clinical period

* **Anchor:** `MonthlyFocusPlan.activated_at` — written only by
  `activate_plan`, which only the child's managing clinician may call. Converted
  to a local date in the plan's own `timezone_of_record`. Not `starts_on`
  (calendar month bounds), not cycle 1 (first release), not `created_at`
  (a draft), never Parent signup.
* **Length:** `CLINICAL_PERIOD_DAYS = 28` local days, first day inclusive
  (`domain/clinical_period.py`). Only an ACTIVE plan has a period.
* **Containment:** a week belongs to a plan only if **every** day of it lies in
  the period. Activated Tue 11-17 → period 11-17 .. 12-14 → Weeks 1–4
  (to 12-13) fit; Week 5 (12-14 .. 12-20) does not.
* **No schema change.** Every active plan already stores both fields.

## Governing plan (`integration/governing_plan.py`, pure)

"Today" is evaluated per plan in that plan's zone (or a caller-pinned local
date).

1. **Covered** — a cycle of the child covers today → its owning plan governs;
   that cycle is the current week. Cycles never move between plans. Two
   covering cycles → refused (`GoverningPlanConflict`), never picked.
2. **Active period** — active plans whose period contains today, in activation
   order; the first whose period holds the **whole** next week on its own
   cadence governs (no cycles yet → starter Week 1 from today). An earlier plan
   is never displaced while it can hold the week.
3. **Renewal required** — the child has had a plan but none can hold the week
   → `ClinicalRenewalRequired` (route: constant 403). Nothing is created,
   renewed or copied forward.
4. **Bootstrap** — no plan ever activated → the existing first-release path
   (create, allocate, activate, Week 1).

`cycle_month` is a label/index/reporting field only. An explicit `cycle_month`
on the release service must AGREE with the governing plan
(`GoverningPlanConflict` otherwise) and cannot bypass renewal.

The caregiver `this-week`, the provider `current-cycle` (no `?cycle_month=`) and
the provider release all use this one resolution. `current-cycle?cycle_month=`
remains a month-addressed inspection.

## No overlapping weeks

* **Authoritative:** `create_cycle` claims `CHILD_CARE_DAY(child_id, local_date)`
  for every day of the cycle **in the same transaction** as the cycle and its
  `(plan, sequence)` claim. Writers under different plans collide on the first
  shared date; exactly one commits (proved by forced interleaving in
  `pilot_backend` and by eight real concurrent writers on the emulator).
* **Advisory:** a child-wide read refuses overlap with cycles written before
  P0-C (which carry no day claims).
* **Write-layer backstop:** a starter-anchored cycle outside its plan's period
  is refused (`OutsideClinicalPeriod`).

## Unchanged

P0-B1 policy and selection, the reviewed bank, goal/version/anchor proofs,
Parent feedback, observation and correction semantics, and RTM attribution
(observation local date → `attribution_month`; a Week 3 observation on 12-02 is
December for billing while the week stays under the November-activated plan).

## Renewal is a clinician action (not built here)

A renewal today = the managing clinician creates and activates a new plan
through the existing monthly-plan routes. Known limit: those routes still allow
only one ACTIVE plan per child **calendar month**, so renewing inside the same
calendar month as the expiring plan requires closing it first.
