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
4. **First plan required** — no plan ever activated (a draft does not count)
   → `ClinicalPlanRequired` (route: constant 403), nothing written. *First-plan
   timezone gate:* the release no longer creates/activates a first plan with a
   defaulted UTC zone; the clinician creates it with an explicit
   `timezone_of_record`, allocates and activates it, then releases Week 1.

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

## Rolling plans and renewal (P0-C renewal)

Plans are rolling clinician-approved periods, **not** one per calendar month.
`cycle_month` is a label/index/reporting field; two plans may carry the same
label and be activated in the same month.

Lifecycle, using only the existing states `draft → active → closed`:

1. The incumbent is ACTIVE and governing.
2. A successor may be **prepared as a DRAFT** at any time (no month lock; a
   draft has no clinical period and governs nothing).
3. **Activation is refused** (`SuccessorActivationPremature`, a `GoalConflict`;
   route: constant 403) while any other ACTIVE plan can still hold the child's
   current or upcoming week — the same resolution the reads and release use.
   A refusal writes nothing.
4. At the renewal point (the incumbent can no longer fit the next whole week),
   the clinician activates the successor explicitly. In **one transaction**:
   release the `CLINICAL_GOVERNANCE` holder's generation and claim the next,
   write the goal snapshots, **retire** (close) the expired incumbent, and set
   the successor ACTIVE. A still-governing incumbent is never closed this way.
5. The successor's own 28-day period starts at its own `activated_at`; it
   governs its own Week 1. Nothing is copied, activated or renewed automatically.

**Activation authority:** `ClaimKind.CLINICAL_GOVERNANCE`, keyed by `child_id`
alone, replaces the `(child_id, cycle_month)` claim. Two concurrent successors
both target the same release marker and next claim; exactly one commits (forced
interleaving in `pilot_backend`; eight real writers on the emulator).

**Explicit close** stays service-only. The pilot workflow needs no close route:
the successor's activation retires an expired incumbent. Ending a still-valid
plan early is a clinician exception workflow, out of scope.

**Month-addressed reads** (`monthly-plan?cycle_month=`,
`current-cycle?cycle_month=`) return the one ACTIVE plan with that label; if a
forged/legacy state ever had two, they refuse (constant 403) rather than pick.

### Known coupling NOT changed: RTM periods

`RtmService.open_period` claims `(episode, plan.cycle_month)` and binds the
period to ONE `focus_plan_id`; `_gather` reads only that plan's cycles before
filtering by `attribution_month`. With rolling plans (and already with a
cross-month week), a calendar month's billing evidence can span two plans, so a
period bound to one plan would miss the other's observations. Attribution
itself (observation local date) is unchanged. Founder decision required.
