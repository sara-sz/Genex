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
