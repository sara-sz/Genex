# Therapist API — Idempotent Modify-Activity Proposal (Phase 1B.2A)

**Fictional, in-memory development behavior. No Firestore transaction exists
yet** (atomic critical section via `repo.run_in_transaction`, which now also
rolls back all writes on any exception). **No frontend is connected. No real
therapist, parent, or child data is used. Parent acceptance/decline is NOT
implemented in this phase** — the proposal enters `pending_parent_acceptance`
and the original assignment remains the active family activity.

## Endpoint

```
POST /api/v1/children/{child_id}/weekly-plan/assignments/{assignment_id}/proposals/modify
Headers: Idempotency-Key: <client-generated-key>   (required)
Body:    { expected_assignment_version, activity{…}, change_reason, save_scope }
```

`activity` carries the full modified payload (title, `developmental_domain`
snake key, `milestone_id`, skill_focus, duration_minutes, difficulty, materials[],
materials_type, setup, parent_instructions[], what_to_say[], how_to_help[],
success_signals[], variations[], routine_tags[], theme_tags[],
safety_risk_flags[]). **No chronological age range** — activities match by
milestone id + developmental domain only.

### Success (200) — abbreviated

```json
{
  "proposal": {
    "proposal_id": "prop_…", "proposal_type": "modify",
    "proposal_status": "pending_parent_acceptance",
    "child_id": "child_noah", "weekly_plan_id": "wp_noah",
    "current_assignment_id": "assign_noah_turntake",
    "original_activity_template_id": "tmpl_turn_taking_ball",
    "original_activity_version_id": "ver_turn_taking_v1",
    "proposed_activity_version_id": "ver_…", "created_by_user_id": "ther_hannah",
    "created_at": "…", "version": 1
  },
  "proposed_activity_version": { "…": "immutable derived version" },
  "current_assignment": { "…": "original", "plan_approval_status": "approved",
                          "pending_proposal_id": "prop_…", "version": 2 },
  "child_summary": { "child_id": "child_noah", "plan_review_count": 0,
                     "pending_proposal_count": 1 },
  "audit_event_id": "aud_…", "idempotent_replay": false
}
```

An exact replay (same key + same operation + same body) returns the same logical
result with `"idempotent_replay": true` — no second proposal, activity version,
or audit event, and the assignment version is not incremented again.

## Core product behavior (before parent acceptance)

- Original assignment stays **current + active**; `plan_approval_status`
  **unchanged** (e.g. `approved` stays `approved` — never reset to
  `needs_plan_review`; the pending proposal is **not** a plan-review item, so
  `plan_review_count` does not increase).
- Original activity **template and version are unchanged** (never mutated in
  place). The proposed modification is a **separate immutable ActivityVersion**.
- The `PlanChangeProposal` references **both** the original and proposed versions.
- The current assignment gains `pending_proposal_id`; **no** replacement,
  archival, retirement, or second current assignment occurs. The family keeps
  seeing the original activity.

## Derived ActivityVersion provenance

Immutable, `is_derived=true`, `created_by_type=therapist`, with
`created_by_user_id/display_name`, `original_activity_template_id`,
`original_activity_version_id`, `modified_by_user_id/display_name`, `save_scope`,
`created_at`, plus the full activity payload. `save_scope` ∈ {`child_only`,
`therapist_library`, `submitted_for_genex_review`} is **provenance metadata
only** — nothing is published to any marketplace/global library. The derived
version links to the same canonical template. Milestone is validated to exist and
to match the selected developmental domain.

## Authorization matrix

| Principal / connection / state | Result |
|---|---|
| Therapist, active connection, **approved** current assignment, no pending proposal, version matches | create proposal (200) |
| Parent principal | 403 `forbidden` |
| Unconnected therapist / pending / paused / ended | 404 (existence-blind) |
| Unknown child / assignment / child mismatch | 404 (existence-blind) |
| Assignment `needs_plan_review` / `replaced` / `archived` / not current-plan | 409 `invalid_modify_proposal_transition` |
| Assignment already has a pending proposal | 409 `pending_proposal_exists` |
| `expected_assignment_version` mismatch | 409 `assignment_version_conflict` (no side effects) |
| Unknown milestone / milestone-domain mismatch | 422 `milestone_domain_mismatch` |

## Optimistic concurrency & atomicity

Success requires `expected_assignment_version == current`, then (in one atomic
critical section): create exactly one immutable ActivityVersion + one
PlanChangeProposal + one AuditEvent, set `pending_proposal_id`, increment the
assignment version **once**, update `updated_at`, leave `plan_approval_status`
unchanged, and store one idempotency result. **On any failure, all collections
roll back** (in-memory snapshot/restore; maps to a future Firestore transaction).
A version mismatch creates no version/proposal/audit/idempotency record.

## Idempotency

Reuses the `IdempotencyRecord` abstraction: binds key hash, actor, action, child,
assignment, canonical request hash, resulting proposal/activity-version/audit ids,
status, created_at. Same key + same request → replay; same key + any difference →
409 `idempotency_key_conflict`; missing/blank key → 400 `missing_idempotency_key`;
failed authz/validation → no success record. Raw key never stored (hash only).

## Audit event

Exactly one immutable `AuditEvent` `event_type=plan_change_proposal_created` with
proposal/type, actor, therapist, child, weekly_plan, assignment, original +
proposed version ids (via subject + fields), `idempotency_key_hash`,
`before_state`/`after_state` (both the unchanged approval status), `occurred_at`,
`request_id`. No raw tokens/keys/secrets. Append-only; no public audit endpoint.

## Read-after-write

- `GET /children` — `pending_proposal_count` increases; `plan_review_count`
  unchanged.
- `GET /children/{id}` — `pending_proposal_count` set; not Needs Plan Review.
- `GET /children/{id}/weekly-plan` — assignment shows the **original** version,
  unchanged approval/practice/assignment status, `pending_proposal_id`, and a
  `pending_proposal` summary; never the proposed version as the active activity.
- `GET /activity-templates/{id}` — original template/version intact; the derived
  version appears in version history.
- `GET /children/{id}/proposals` and `/proposals/{proposal_id}` — read-only,
  authorization-safe, existence-blind.

## Error contract (stable codes)

`missing_idempotency_key`(400), `invalid_request`(422), `forbidden`(403),
`not_found`(404), `idempotency_key_conflict`(409), `assignment_version_conflict`(409),
`invalid_modify_proposal_transition`(409), `pending_proposal_exists`(409),
`milestone_domain_mismatch`(422). Envelope `{ "error", "detail" }`; no stack
traces, internal ids, or authorization disclosure.

## Not implemented yet (later gated phases)

Parent accept/decline, atomic replacement, add/remove/generic-replace proposals,
note/private-note writes, Firebase Auth, Firestore, Cloud Run, frontend
integration, production config, real users/data.
