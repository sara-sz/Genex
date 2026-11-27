# P0-B2a — constrained developmental recalibration

**Not full P0-B2.** A deliberately narrow first version: one five-node Pilot
neighbourhood, ONE automatic pair, everything else a clinician exception.

## The five nodes (generated, drift-gated)

`pilot_runtime/data/developmental_ladder_talking_v1.json`, built by
`pilot_runtime/integration/developmental_ladder_generator.py` from the frozen
Gold Standard bridge rows, the deployed rung table and the reviewed bank. The
generator names no milestone, family or rung — nodes are selected by structure
(the rung binding exactly the bank's required families; its bridge rows; the one
TEXT_LINKED predecessor endpoint). Node refs are identical to the P0-B2 review
package's `node1:` refs.

| Therapist Dev | Node | Reviewed pool |
|---|---|---|
| Step 1 | lower endpoint | none (0 reviewed cards; the 7 candidates are never read) |
| Step 2 | Bridge 1 | vocabulary family (12) |
| Step 3 | Bridge 2 | vocabulary family (12) |
| Step 4 | Bridge 3 | two-word family (6) |
| Step 5 | anchor endpoint | two-word family (6) |

## The one rule for automatic movement

An adjacent pair moves automatically iff both nodes have a non-empty reviewed
pool AND the pools are disjoint. Today: Step 3 ↔ Step 4 only. The loader
recomputes the rule and refuses an artifact that enables anything else.
A later bridge-specific card mapping (disjoint `pool_template_ids`, or reviewed
lower-endpoint cards) unlocks Step 2 ↔ 3, Step 4 ↔ 5 or Step 1 ↔ 2 by
regenerating the artifact. No record schema changes.

## Records (create-only sidecars; the P0-B1 decision schema is unchanged)

* `BridgeBaselineAnswers`: the caregiver's three answers (append-only).
* `DevelopmentalPlacement`: one per plan (derived id), set on a DRAFT plan
  with no weeks. Holds the starting node, the proposal, the baseline status,
  the source (confirmed proposal or clinician choice) and the five-node envelope.
* `DevelopmentalMovement`: one per adaptive week of a placed plan (derived id),
  moved or not. Written in the SAME transaction as the P0-B1 record and decision,
  and verified on resume. Holds the rule version, the node before and after
  (with indexes), the outcome, the reason, and the exact evidence ids.
  A move whose indexes are not exactly one apart cannot be constructed.

The current node = the placement start, folded over the plan's movement records.

## Evidence and outcomes

The P0-B1 threshold is reused, not re-implemented. A trigger exists only when
the last two CURRENT observations of ONE activity fall on different practice
dates. Observations are not pooled, and superseded rows are excluded.

* `too_easy` twice → one step up, if that move is enabled. Otherwise
  `requires_clinician_review`: either `transition_not_enabled` or
  `outside_envelope`.
* `too_hard` twice → one step down, under the same rule.
* `wasnt_ready` twice → no move; `requires_clinician_review` (`wasnt_ready`).
* Up and down together, or a difficulty signal together with disengagement →
  no move; `requires_clinician_review` (`conflicting_signals`).
* Disengagement → the same node, with the P0-B1 theme change.
* Evidence that an earlier move already used → no move;
  `requires_clinician_review` (`evidence_already_used`).

After a move, Week N+1 is chosen fresh from the new node's reviewed pool.
Otherwise P0-B1 selection runs inside the current node's pool.

## Surfaces

| Route | Who | What |
|---|---|---|
| `GET/POST /pilot/children/{child}/bridge-baseline` | caregiver | Bridge wording, the prompt and four answers. The response is only `recorded`. |
| `GET /pilot/children/{child}/developmental-start` | managing clinician | The steps, which have reviewed activities, the automatic pair, the baseline status and proposal, and placements with plain-language review notes. |
| `POST /pilot/monthly-plans/{plan}/developmental-placement` | managing clinician | `{step_key}` on a draft plan. |

None of these shows the Parent a node ref, bridge number, age, "moved" wording
or a reason. `parent_week_view` is an allowlist, and the snapshot carries no
node data.

## Unchanged

Plans without a placement keep P0-B1 exactly, with no inference or backfill.
RTM is untouched. A placed plan with no ladder configured fails closed.
P0-B2a is single-goal: a child with another releasable goal is not placeable.

## Bridge questions: when they are asked (deterministic)

`GET /pilot/children/{child}/bridge-baseline` (caregiver only) returns
`{"applicable": false}` and nothing else unless the child's target set is
exactly this ladder's anchor. The target set is the canonical anchors of the
child's active approved clinical goals, or, when there are none yet, of the
currently offered goal suggestions. With no target, an unanchored target,
another milestone, or this milestone plus another, the questions are not
asked and a POST is refused. When they apply, the response holds the prompt,
the four answers, the three bridge wordings verbatim, and `submitted`.

Parent Dev (`sara-sz/dear-journey`, route `/pilot`) asks the questions one at a
time. **Yes** continues; **Sometimes**, **Not yet** or **Not sure** stops, so an
out-of-order set cannot be produced. It posts exactly `{"answers": [a1, a2, a3]}`,
with `null` for a question that wasn't asked. It never computes or shows a step.

## Placement audit event

The first placement writes ONE `developmental_placement_confirmed` audit event
in the same transaction as the placement row. Both are create-only, and the
event id is derived from the placement. Metadata holds opaque provenance only:
plan, placement, goal, envelope `ldr1:` ref, rule version, starting `node1:` ref
and index, proposed node, `placement_source`, `proposal_basis`, baseline status
and previous placement. No caregiver answers, bridge wording or age. An
identical replay returns the stored placement and writes nothing. A different
step is refused and writes nothing. On the emulator, 8 concurrent identical
placements produce 1 placement and 1 event.

`placement_source` is derived, never asserted. The record refuses a
clinician-adjusted step labelled as a confirmed proposal, and the reverse.

## Successor-plan rule (founder decision)

At renewal, the final derived node of the previous placed plan becomes the
**proposed** starting node for the successor (`proposal_basis: previous_plan`).
The clinician must confirm it (`confirmed_previous_plan_step`) or choose
another allowed step (`clinician_choice`) on the successor's draft. The prior
plan's envelope is never the successor's approval: the successor gets its own
placement, envelope and audit event. A successor activated without a placement
runs as any unplaced plan does (P0-B1). Nothing creates, carries or infers a
placement. Tests: `test_p0b2a_audit_successor_scope.py` §3. The full renewal
UI belongs to the clinician review/renewal slice.

## Intentional Pilot limitation: single goal

P0-B2a refuses multi-goal plans: placement and release need exactly one
approved goal, at the ladder's milestone. This is deliberate for the Pilot,
not an open defect. Multi-goal developmental movement is out of scope.

## Clinician-review records (for the next slice's exception UI)

Every `requires_clinician_review` movement carries what that UI needs:
`clinician_review_item()` gives child, plan, week, current step (key and
wording), reason, the attempted direction where there is one, and the exact
evidence and activity refs. The provider `developmental-start` read exposes
them as `placements[].review_items`. None of it reaches a caregiver response.
Reasons: `transition_not_enabled`, `outside_envelope`, `wasnt_ready`,
`conflicting_signals`, `evidence_already_used`. Missing or ambiguous placement
or content is **not** a record: the release is refused (fail closed) and no
week is written.
