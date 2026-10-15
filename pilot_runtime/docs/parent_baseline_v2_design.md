# Parent Functional Baseline v2 — canonical design note

**This file is the source of truth for Parent Functional Baseline v2 behaviour.**
Founder decisions recorded 2026-10-13, after the first real Parent browser run
on deployed staging.

Every statement below is labelled:

| Label | Meaning |
|---|---|
| **IMPLEMENTED** | true of the deployed code today |
| **TARGET** | founder-approved for the current pilot; **not yet implemented** |
| **DEFERRED** | deliberately out of scope for the pilot |

Do not restate these rules elsewhere. Other documents and code comments should
point here. Where this note and any other description disagree, **this note
wins** — except on observable HTTP shapes, which
[`october_pilot_v2_frontend_contract.md`](./october_pilot_v2_frontend_contract.md)
owns.

> This note names no milestone text, no canonical activity-family identifier and
> no rung or track reference. That is not squeamishness: the
> `0.5E-B — the bridge is not a mirror` CI gate scans every non-`.py` file under
> `pilot_runtime/`, and workbook content must cross the Parent→Pilot boundary at
> runtime rather than being pasted into a document. For the live skill and
> family inventory, read the generated artifacts named in
> [§10](#10-current-state-and-known-gaps).

---

## 1. Purpose of Baseline v2

**FOUNDER DECISION.** The Parent baseline is **not** a comprehensive
developmental, speech, or language assessment. Its purpose is exactly three
things:

1. identify a reasonable **starting focus** for personalization;
2. detect meaningful **secondary areas** that should influence clinician review
   and future personalization;
3. keep onboarding **short enough that parents will finish it**.

**Never** describe the result as a developmental age, a diagnosis, or a
comprehensive assessment — not in the API, not in the UI, not in clinician-facing
copy.

**IMPLEMENTED.** There is no `dev_age` field anywhere in the system and none may
be synthesised. The baseline read deliberately withholds its internal anchor and
month fields; the band months it does expose are ladder labels, not ages. See
the frontend contract for the exact field-level rules.

---

## 2. Parent UX question budget

**FOUNDER DECISION — TARGET.** Per domain:

- **normally 5** skill questions;
- **maximum 6**;
- the **sixth** is used *only* when it resolves meaningful uncertainty,
  contradictory evidence, or a secondary concern;
- **never** extend onboarding past 6 questions merely to characterise every
  concern fully.

The initial Parent routing choice (the entry screen) **does not count** as one of
the 5 skill questions. It selects where to start asking and asserts nothing about
ability.

**IMPLEMENTED today:** a band budget of 3 *bands* (not questions), and a stop
rule that can end the baseline after **2** questions. See
[§10](#10-current-state-and-known-gaps).

---

## 3. Talking & Communicating pilot structure

**FOUNDER DECISION — TARGET.** The five questions are spent as follows:

| Questions | Purpose |
|---|---|
| **1–2** | adaptive target finding and confirmation around the likely actionable communication focus |
| **3–5** | broader communication-profile sampling, using canonical reviewed skill evidence where available — including receptive/comprehension, functional communication, and social/pragmatic communication or another appropriate communication subarea |
| **6** | optional clarification only (see [§5](#5-sixth-question-behaviour)) |

**Hard constraint.** Exact questions must come from the **canonical Genex skill
taxonomy / reviewed evidence**. Do **not** invent skills, rephrase milestones, or
add filler items to satisfy the question count. If a subarea has no canonical
reviewed skill at an appropriate level, that slot is simply not spent — the
budget is a ceiling, not a quota.

---

## 4. Multi-area rule

**FOUNDER DECISION — TARGET.** If two or more communication subareas show
concern, the child must **not** be forced into a single-area interpretation. The
profile is recorded as **multi-area**.

The system may still name one **primary starter focus**, because a week has to
start somewhere. Secondary areas remain **explicit signals** for clinician review
and longitudinal personalization — they are not discarded, and they are not
averaged away.

Worked example of the required outcome:

| Observed | |
|---|---|
| expressive | emerging |
| receptive | concern |
| functional | demonstrated |
| social | concern |

| Recorded profile | |
|---|---|
| primary starter focus | expressive |
| secondary concerns | receptive **+** social |
| profile | **multi-area** |

**IMPLEMENTED today:** no multi-area concept exists. The record derives a single
bracket (one demonstrated level, one unresolved level) within one subarea.

---

## 5. Sixth-question behaviour

**FOUNDER DECISION — TARGET.**

- If a secondary area answers **No**, **Sometimes**, **With help**, **Not sure**,
  or is otherwise uncertain, the sixth question **may** clarify the single most
  important unresolved area.
- If the sixth response is **still** No / Sometimes / With help / Not sure,
  **stop**.
- Do **not** continue to questions 7+.

The sixth question exists to disambiguate, not to pursue certainty. An unresolved
answer after six questions is a legitimate, reportable outcome.

`Not sure` additionally **never** becomes a target — an unanswerable question is
not evidence that a skill is absent. That rule is **IMPLEMENTED** and must be
preserved by any change to the budget.

---

## 6. Activity-content safety boundary

**FOUNDER DECISION.** Baseline **evidence coverage** and **activity capability**
are different things and must never be conflated.

A Parent response may identify receptive, functional, or social communication as
a concern **even when Genex has no reviewed activity content for that subarea**.
Such evidence **may**:

- inform the communication profile;
- be surfaced to the clinician;
- influence future personalization.

Such evidence **must not**:

- automatically generate a clinical goal;
- bind an activity family that lacks reviewed content.

**Unsupported areas must fail closed for activity generation.**

**IMPLEMENTED.** Generation reports canonically-unsupported deficits separately
and never renders them as suggestions or near-maps them onto a neighbouring
family; release refuses before writing anything if a bound family has no reviewed
content. A deficit with no content is therefore visible and inert, which is the
required behaviour.

---

## 7. Clinician gate

**FOUNDER DECISION — preserve as-is.** For the current RTM pilot the chain is:

```
Parent baseline → projection → GoalSuggestion → clinician review/edit/approval
  → ClinicalGoal → monthly allocation → weekly release
```

**Parent baseline completion alone does not mean the weekly plan is clinically
approved.**

**IMPLEMENTED.** Nothing downstream of the baseline is automatic. Projection is a
separate explicit caregiver action; suggestion generation, goal approval,
allocation and release are all provider-gated and additionally require the acting
provider to be the child's managing clinician. No scheduler, cron or job
auto-advances any step. Exact triggers and gates per route are in the frontend
contract.

---

## 8. Parent-facing completion principle

**FOUNDER DECISION — TARGET.** Do **not** assume every Parent already has a care
team.

Completion copy should say that **Genex is preparing a starting point based on
what the Parent shared**. If a clinician *is* connected, the UI may *separately*
explain that the clinician will review the suggested goal before the clinical
weekly plan is released.

Two failure modes this rule exists to prevent:

- telling an unconnected parent that a "care team" will review their answers;
- implying that completing 5 questions produced a clinically sufficient
  assessment.

**IMPLEMENTED today:** completion states that responses are ready for the care
team, unconditionally. That is accurate about the pipeline and wrong on both
counts above.

---

## 9. Longitudinal philosophy

**FOUNDER DECISION.** Genex should not try to learn everything through
onboarding. The short baseline establishes the **starting profile**. Accuracy
improves over time through:

- Parent activity feedback;
- observed success / help-needed signals;
- repeated weekly evidence;
- clinician input;
- targeted follow-up questions when useful.

**This is intentional product architecture, not an incomplete assessment.** Any
future proposal to lengthen onboarding should be read against this section first.

---

## 10. Current state and known gaps

**These are current-state limitations, not intended final behaviour.** All four
were confirmed by read-only audit of the deployed code and artifacts on
2026-10-13.

### 10.1 The baseline can terminate after two questions — **IMPLEMENTED (defect)**

The traversal finishes a band before moving, then stops once it holds one fully
demonstrated band below and one complete-but-unmastered band above — it treats
that bracket as sufficient. On the pilot's declared track the relevant bands hold
**exactly one skill each**, so band-completeness is satisfied by a single answer
and the bracket closes after two.

Observed live: entry choice "many single words" → one `yes` → one `sometimes` →
`ready_to_finalize`. Two skills assessed.

Note that the band budget was **not** the limiter — only two bands were entered.
Any fix must address the **stop rule**, not the budget.

`ready_to_finalize` is currently `(not finalized) and question is None`. It
carries no evidence threshold of its own; it reports the traversal. The TARGET
behaviour in [§2](#2-parent-ux-question-budget)–[§5](#5-sixth-question-behaviour)
requires an explicit sufficiency predicate that this flag depends on.

### 10.2 Only the expressive track is reachable — **IMPLEMENTED (scope limit)**

The declared track for the Talking & Communicating area admits two subdomains,
both expressive: the expressive-language chain and the early
vocalization/babbling chain. Of **83** canonical rows for the domain, **21** are
reachable at any entry choice.

The documented rationale is ladder coherence — a rung should only be reachable
when it sits on the same developmental chain as its neighbours, and one ladder
cannot interleave chains. This is a **declared-track scoping decision, not
missing data.**

### 10.3 Receptive, functional and pragmatic skills are not reachable — **IMPLEMENTED (gap)**

The canonical Brain **does** carry them, already family-tagged:

| Subarea | Canonical rows | Reachable in Baseline v2 |
|---|---|---|
| expressive language | 34 | 14 |
| early vocalization & babbling | 11 | 7 |
| receptive language / comprehension | 17 | **0** |
| gestural / functional communication | 8 | **0** |
| social / pragmatic (conversation & narrative) | 10 | **0** |
| speech intelligibility | 3 | **0** |

So **38 rows across four non-expressive subareas exist and cannot be asked.**
Delivering [§3](#3-talking--communicating-pilot-structure) requires making a
bounded selection of them reachable — the data is there.

### 10.4 Reviewed activity content covers a limited set of expressive families — **IMPLEMENTED (content gap)**

Reviewed activity content exists for only **four** families, totalling **30**
templates, all expressive. Five reachable families have rungs but **zero**
reviewed cards — including the family bound by the bands a "many single words"
child most often demonstrates — and four reachable rungs cannot be mapped to any
family at all.

Consequence: only a narrow slice of the ladder can currently produce a releasable
week. A child bracketed lower anchors on a family with no content, and release
**fails closed** (correct behaviour per [§6](#6-activity-content-safety-boundary),
but it is why content curation gates pilot coverage).

There is **no** reviewed content for receptive, functional, or social/pragmatic
activities. Per [§6](#6-activity-content-safety-boundary), evidence in those areas
may still be recorded and surfaced; it simply cannot drive activities.

**Live inventory, authoritative, do not transcribe into docs:**
`pilot_runtime/data/rung_table_talking_v1.json` (reachable rungs, family bindings,
unmappable reasons) and `pilot_runtime/data/activity_bank_talking_v1.json`
(served families, per-family counts, release-readiness). Both are generated and
drift-gated in CI.

---

## 11. Intentionally deferred

**DEFERRED — not pilot scope.** Recorded so they are not mistaken for oversights:

- **Per-subarea coverage as a hard sufficiency requirement.** Cannot be satisfied
  while the declared track admits one subarea ([§10.2](#102-only-the-expressive-track-is-reachable--implemented-scope-limit)).
  Encoding it now would create a rule that can never pass. It belongs with the
  track-scope change.
- **Multi-chain declared tracks** — parallel receptive / expressive / pragmatic
  ladders with per-chain brackets.
- **Information-gain-driven stopping** — stop when the target is identified to a
  confidence threshold, rather than on a structural rule.
- **Curated activity content for receptive, functional and social/pragmatic
  families**, and resolution of the currently unmappable rungs.
- **Parent-visible coverage display**, and letting a parent volunteer more detail
  before projecting.
- **Week 2+ adaptation, activity feedback ingestion and RTM** — tracked
  elsewhere; [§9](#9-longitudinal-philosophy) is the philosophy they will serve,
  not a commitment in this slice.

---

## 12. Where authority lives

| Question | Authority |
|---|---|
| Baseline v2 purpose, budget, sufficiency, multi-area, completion copy | **this note** |
| Observable HTTP routes, payload shapes, status codes, field audience | [`october_pilot_v2_frontend_contract.md`](./october_pilot_v2_frontend_contract.md) |
| Which skills and rungs exist and what they bind | the generated artifacts named in [§10.4](#104-reviewed-activity-content-covers-a-limited-set-of-expressive-families--implemented-content-gap) |
| Clinical content of an activity | the reviewed card pools under `pilot_runtime/data/reviewed_cards/` |
| Whether a change preserves the no-mirror property | the `0.5E-B — the bridge is not a mirror` CI gate |

Implementation of the TARGET behaviour in
[§2](#2-parent-ux-question-budget)–[§5](#5-sixth-question-behaviour) is **not yet
authorized to be written**. This note exists so that when it is, the decisions do
not have to be re-derived.
