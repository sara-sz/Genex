# Therapist API — Read-Only Vertical Slice (Phase 1A)

**Local, read-only, fictional data.** This service does **not** connect to the
Parent API or any Parent data store, does **not** use Firebase/Firestore, and
implements **no** write operations. All data is invented (emails use `.example`).

## Layering

```
HTTP route (app/api/routes.py)
  → ReadService (app/services/read_service.py)     [all reads go through here]
    → access policy (app/services/access.py)       [authorization, existence-blind]
    → CollaborationRepository (in-memory)           [app/repository/memory.py]
      ← fictional fixtures (app/fixtures/*)         [seeded once at app build]
```

Route handlers never touch fixture dicts directly. Domain / service / repository
/ API layers are separated. The in-memory repository implements the same
`CollaborationRepository` interface a future Firestore repository will satisfy.

## Domain model overview

`app/domain/read_models.py`: `UserPrincipal`, `TherapistProfile`,
`TherapistPreferences`, `ParentProfile`, `Child`, `TherapistChildConnection`,
`DevelopmentalMilestone`, `ActivityTemplate`, `ActivityVersion`, `WeeklyPlan`,
`PlanAssignment`, `PlanChangeProposal`, `ParentNote`, `PrivateTherapistNote`,
`AuditEvent`. Every entity has a stable string `id`.

**Activity matching is by `milestone_id` + developmental `domain`, never by
chronological age.** `ActivityTemplate`/`ActivityVersion` carry **no** age range.
A `DevelopmentalMilestone` may retain `source_age_band_months` for provenance
only. Templates are immutable; a "modify" yields an immutable derived
`ActivityVersion` that references the original template + version.

## Enums (`app/domain/enums.py`)

| Enum | Values |
|---|---|
| ConnectionStatus | active, pending_parent_acceptance, invitation_not_activated, paused_by_parent, ended |
| PlanApprovalStatus | needs_plan_review, approved, change_pending_parent, replaced, archived |
| PracticeStatus | not_tried, tried, tried_with_help, did_it, loved_it |
| AssignmentStatus | current, retired, proposed |
| ProposalType | add, modify, replace, remove |
| ProposalStatus | pending_parent_acceptance, accepted, declined, cancelled |
| ParentNoteType | question, note, update |
| ParentNoteReviewStatus | new, reviewed |
| SessionPreparationStatus | none, discuss_at_next_session, discussed |
| ActivitySaveScope | child_only, therapist_library, submitted_for_genex_review |
| CreatedByType | genex, therapist |

Review status and session-preparation status are **independent** fields;
plan-approval / practice / assignment states are **separate** — never collapsed.

## Read-only endpoints

| Method | Path | Access |
|---|---|---|
| GET | /health | public |
| GET | /api/v1/app/config | public (non-secret) |
| GET | /api/v1/me | therapist |
| GET | /api/v1/children | therapist (own connections) |
| GET | /api/v1/children/{child_id} | therapist, **active** connection |
| GET | /api/v1/children/{child_id}/weekly-plan | therapist, active |
| GET | /api/v1/children/{child_id}/progress | therapist, active |
| GET | /api/v1/notes | therapist (inbox: active children) |
| GET | /api/v1/children/{child_id}/notes | therapist, active |
| GET | /api/v1/children/{child_id}/private-notes | therapist, active, **own only** |
| GET | /api/v1/children/{child_id}/next-session | therapist, active |
| GET | /api/v1/children/{child_id}/connection | therapist (any live connection, incl. restricted) |
| GET | /api/v1/activity-templates | therapist |
| GET | /api/v1/activity-templates/{activity_template_id} | therapist |
| GET | /api/v1/milestones | therapist |

Lists use a pagination-ready envelope: `{ "items": [...], "total": N, "next_cursor": null }`.

## Local fictional authentication

Enabled only when `ENVIRONMENT=dev` (or `test`) **and** `DEV_AUTH_ENABLED=true`
(disabled by default; `env_validation` forbids it in prod). Selection is by a
fictional bearer token:

| `Authorization: Bearer …` | Principal |
|---|---|
| `dev-hannah` | Hannah Lieberknecht (therapist, connected to Maya/Eli/Noah + restricted Amara/Sana) |
| `dev-elena` | Elena Ruiz (parent) |
| `dev-unconnected-therapist` | therapist with no connections |

There is **no** unauthenticated pathway and **no** Firebase yet.

### Example

```
GET /api/v1/children     Authorization: Bearer dev-hannah
200 {"items":[{"child_id":"child_maya","display_name":"Maya",
     "parent_display_name":"Elena Ruiz","connection_status":"active",
     "active_practice_domains":["Talking & Communicating","Social & Emotional"],
     "plan_review_count":0,"new_parent_note_count":1,"pending_proposal_count":1}, ...],
     "total":5,"next_cursor":null}

GET /api/v1/children/child_amara    Authorization: Bearer dev-hannah
404 {"detail":"Not found."}          # restricted -> existence-blind

GET /api/v1/children/child_amara/connection    Authorization: Bearer dev-hannah
200 {"child_id":"child_amara","connection_status":"pending_parent_acceptance",
     "restricted":true,"activation_reminder_simulated":true, ...}
```

## Authorization matrix

| Principal | Maya/Eli/Noah (active) | Amara (pending) / Sana (paused) | Unknown id | Private notes |
|---|---|---|---|---|
| Hannah (therapist) | full workspace | connection details only; full-content routes → 404 | 404 | own notes only |
| Unconnected therapist | 404 | 404 | 404 | 404 |
| Elena (parent) | 403 on therapist routes | 403 | 403 | 403 |

Existence-blind: unknown **and** unauthorized child ids both return **404** with
an identical body — the API never reveals whether a child id exists. Parent
principals are forbidden (403) from therapist-only routes, including private notes.

## Future Firestore mapping notes

Each domain entity maps to a Firestore document keyed by its stable `id`, in a
collection named per `app/repository/collections.py`. Queries used here
(`therapist_id`, `child_id`, `status`, `activity_template_id`) become composite
indexes. The `CollaborationRepository` interface is preserved so a Firestore
implementation drops in without changing services/routes. Writes, audit events,
and idempotency keys are **later, gated phases** (schemas exist; no write paths).

## Explicit scope statement

This phase is **local and read-only** and uses **fictional data only**. **No
Parent API or Parent data store is connected.** No GCP/Firebase/Firestore, no
Cloud Run deploy, no frontend integration, no writes.
