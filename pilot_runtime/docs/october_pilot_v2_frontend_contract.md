# October pilot v2 — frontend contract (Parent + Therapist)

Status: **deployed and verified in fictional staging.** Every shape below was read
back from the running services, not from source alone.

| | |
|---|---|
| Pilot API image | `pilot-api-staging-00011-mjf` · `sha256:4a2b3e41e5138978592b49873acaecb511c5adc5f0333ca54286421f13a79ffc` |
| Parent API image | `genex-api-staging-00066-v9j` · `sha256:0d9b3f6957f2d3d01082803968f58173d107c35c0322b96552614d6a0afc6c2a` |
| Projection service | `pilot-projection-staging-00004-prd` |
| Source | `cd4ad2072cd7fa802d6131d7faf2ae61b6508c4f` (composition fix over tag `october-pilot-0.6a-2-week-one-release`) |

> **This file contains no credentials, no API keys and no beta codes.** Staging
> logins are handed over out of band and must never be committed.

> **Scope.** This document owns the **observable HTTP contract**: routes,
> payload shapes, status codes, retry semantics and which fields may reach which
> audience. It does **not** own Baseline v2's product behaviour — why the
> baseline asks what it asks, how many questions it may spend, when it is
> allowed to stop, how a multi-area profile is recorded, or what the completion
> screen may claim. Those are founder decisions recorded in
> **[`parent_baseline_v2_design.md`](./parent_baseline_v2_design.md)**, which is
> canonical for them and also records which behaviour is implemented today
> versus approved-but-not-yet-built. Read it before building the baseline UI:
> some of what this contract describes is **current state under active review**,
> not the target.

> **Why this file lives under `pilot_runtime/docs/` rather than `/docs/`.**
> `genex-parent/tests/test_parent_24_integrity.py::test_parent_commits_touch_only_parent_paths`
> diffs `PARENT_BASE_TAG..HEAD` and rejects any path outside
> `("genex-parent/", ".github/workflows/parent-2.4-ci.yml", "pilot_backend/",
> "pilot_runtime/")`. A root-level `docs/` file fails that guard. This is the
> same reason `pilot_runtime/deploy/Dockerfile` and
> `pilot_runtime/requirements-integration-ci.txt` sit where they do. Moving this
> file to `/docs/` requires widening `PARENT_ALLOWED_PREFIXES`, which is a
> founder decision.

---

## 1. Two systems, two identity directories

| | Base URL | Firebase project |
|---|---|---|
| **Parent API** | `https://genex-api-staging-67icluiswq-uc.a.run.app` | `genex-mvp-2026` |
| **Pilot API** (therapist *and* caregiver) | `https://pilot-api-staging-o2wbm2vguq-uc.a.run.app` | `genex-pilot-staging` |

These are **separate Firebase user directories**. There is no shared uid and the
systems deliberately do not federate. A token minted for one is rejected by the
other. The same human therefore signs in twice — once per side.

The bridge between them is a **one-time capability token over a Parent session**
(`POST /api/v1/session/{session_id}/pilot-claim`), redeemed once at
`POST /pilot/parent-session-claims`. The capability names a *session*, never a
person, so no uid crosses in either direction. The browser never holds a
service credential.

### Auth expectations

- Every route except `GET /health` on either service requires
  `Authorization: Bearer <Firebase ID token>`.
- Missing, malformed, expired or revoked token → **401**. This is the only
  non-403 refusal on the Pilot workflow surface.
- The Pilot resolves the actor (caregiver vs provider) from the verified token.
  **No request body may carry `role`, `caregiver_id`, `provider_id`, `uid`,
  `auth_subject` or `child_id`** — those names are rejected by the body reader
  before any handler sees them.
- The Pilot API is `allow-unauthenticated` at the Cloud Run layer on purpose:
  authorization is enforced in the application, not by platform IAM.

### CORS origins (exact; no wildcards)

**Parent API** — `http://localhost:3000`, `http://localhost:5173`,
`https://lovable.dev`,
`https://8fc006ff-8275-4dce-b0ac-2f3c0dd82694.lovableproject.com`,
`https://id-preview--8fc006ff-8275-4dce-b0ac-2f3c0dd82694.lovable.app`,
`https://genex-kiddo-compass.lovable.app`

**Pilot API** — `https://genex-kiddo-compass.lovable.app`,
`https://your-childs-plan.lovable.app`,
`https://65983817-88f8-452c-8c2d-03aa353e058e.lovableproject.com`,
`https://id-preview--65983817-88f8-452c-8c2d-03aa353e058e.lovable.app`,
`https://id-preview--2aea7f68-0bf4-4cf4-935d-758ab17e96ba.lovable.app`,
`https://2aea7f68-0bf4-4cf4-935d-758ab17e96ba.lovableproject.com`

Any other origin fails CORS. New preview URLs must be added to the service
before they will work.

---

## 2. Parent session prerequisite and the beta gate

Baseline v2 operates **on an existing Parent session**. Create one first:

```
POST /api/v1/session/start
{ "child_name": "...", "age_years": 3, "age_months": 0, "age_in_months": 36,
  "diagnosis_or_condition": "No known diagnosis / not sure",
  "parent_concern": "...", "daily_time_minutes": 20,
  "timezone": "America/Los_Angeles", "beta_access_code": "<code>" }
→ 200 { session_id, status, domains, total_questions_estimate, current_question }
```

- `age_in_months` **must** equal `age_years * 12 + age_months`; range 0–131.
- `diagnosis_or_condition` ∈ `No known diagnosis / not sure` · `Down syndrome` ·
  `ADHD` · `Autism spectrum` · `Other` · `Prefer not to say`.
- `daily_time_minutes` ≥ 5. `timezone` is an IANA string and anchors the
  planning week.

**Beta gate.** When `REQUIRE_BETA_CODE` is enabled (it is, on staging),
`beta_access_code` must match or the call returns **403**. The code is matched
case-insensitively and space-trimmed, is **never stored**, and the session
records only a `beta_authorized` flag. Later endpoints do **not** re-check it —
they rely on the Firebase token plus session ownership. The code value is
supplied out of band.

`child_name` is used by the client for display only. It is sanitised out of
`parent_concern` before storage and is never persisted or sent to any model.

This route is deterministic — no LLM is on this path.

---

## 3. Baseline v2 (Parent API)

Only `talking_and_communicating` is wired. Any other domain → **404
`unsupported_baseline_domain`**.

```
GET  /api/v2/session/{session_id}/baseline/{domain}/entry-screen
POST /api/v2/session/{session_id}/baseline/{domain}/start       { entry_choice_id }
POST /api/v2/session/{session_id}/baseline/{domain}/answer      { skill_key, answer }
POST /api/v2/session/{session_id}/baseline/{domain}/finalize    (no body)
GET  /api/v2/session/{session_id}/baseline/{domain}
POST /api/v2/session/{session_id}/baseline/{domain}/projection  (no body)
```

Request bodies are `extra: forbid` — an unknown field is a 422.

### entry-screen → 200

```json
{"baseline_version": "parent-2.4-functional-baseline-v1",
 "area_id": "talking", "domain": "talking_and_communicating",
 "title": "Talking & Communicating",
 "subtitle": "Words, understanding, conversation",
 "question": "Which best describes your child now?",
 "choices": [{"choice_id": "no_words_yet", "label": "No words yet", "order": 1},
             {"choice_id": "few_sounds_or_words", "label": "A few sounds or words", "order": 2},
             {"choice_id": "many_single_words", "label": "Many single words", "order": 3},
             {"choice_id": "two_three_words", "label": "Puts 2–3 words together", "order": 4},
             {"choice_id": "short_sentences", "label": "Uses short sentences", "order": 5},
             {"choice_id": "not_sure", "label": "Not sure", "order": 6}]}
```

`baseline_version` reads `…-v1` here **on purpose**: the entry descriptors
describe the *area*, not the assessment algorithm. **Do not branch on it.**

### start / answer / finalize / GET — one shared view → 200

```json
{"domain": "talking_and_communicating", "area_id": "talking",
 "baseline_version": "parent-2.4-functional-baseline-v2",
 "entry_choice_id": "many_single_words",
 "finalized": true, "status": "BOUNDED", "skills_assessed": 2,
 "bands": [{"position": 1, "assessed": 1, "total": 1, "complete": true,  "mastered": true},
           {"position": 2, "assessed": 1, "total": 1, "complete": true,  "mastered": false}],
 "current_question": null,
 "ready_to_finalize": false,
 "updated_at": "2026-10-08T17:26:54.781802+00:00",
 "outcome": {"state": "plan_ready", "plan_available": true,
             "answers_recorded": 6}}
```

`current_question` when present:
`{ question_ref, milestone, parent_explanation }`.

### BREAKING for the Parent dev UI — developmental months removed

Two fields previously carried a canonical **developmental band month** into the
browser and have been **removed**:

| Was | Now |
|---|---|
| `bands[].months` | `bands[].position` — 1-based ordinal in the order bands were entered |
| `current_question.months` | *gone*; nothing replaces it |

Chronological age is legitimate routing context. A developmental band month is an
*interpretation*, and `18` beside a progress bar reads as a verdict about the child
however it is labelled — the `dev_age` mistake arriving through a progress
indicator.

**Migration:** anything that used `months` for ordering or progress should use
`position`. Do **not** substitute a field that indirectly reveals the band.
`bands[]` remains in entry order, so `position` is redundant with the array index
and either may be used.

The canonical months are **unchanged server-side** — the record stores them, the
traversal routes on them, and the A2 v2 projection still sends them (§4). Only the
browser stops receiving them.

### `skill_key` → `question_ref` — also BREAKING

`skill_key` carried `domain · subdomain · months · milestone` joined by `U+001F`,
so splitting it recovered the month and the subdomain. Instructing clients to treat
it as opaque did not make it opaque, so it is **removed**.

The question payload and the `/answer` body now use **`question_ref`**: a
32-character lowercase-hex digest, salted per baseline session.

```
POST .../baseline/{domain}/answer
{"question_ref": "<32 hex>", "answer": "yes"}
```

| | Was | Now |
|---|---|---|
| question field | `skill_key` | `question_ref` |
| `/answer` body field | `skill_key` | `question_ref` |
| 422 mismatch body | echoed the canonical key | echoes **references** only |

**Client rules:**

- Echo `question_ref` back **verbatim**. It is opaque by construction — there is
  nothing in it to parse, and nothing to render.
- It is **stable** while the same question is outstanding, so a retry with the same
  reference behaves exactly as before, and a stale one still `422`s with the
  expected reference in the message so you can recover.
- It is **session-scoped**: the same question in a different session has a
  different reference. Never cache one across sessions.
- The body schema pins it to exactly 32 hex characters, so a malformed value is
  refused by validation (`422`) before any session or lifecycle check. If you are
  testing an auth or lifecycle refusal, send a well-formed reference.

The canonical identity is unchanged server-side and still travels in the A2 v2
projection (§4). The per-session salt lives in the lifecycle envelope, never in the
baseline record, so no projection id and no finalized evidence moves.

### `outcome` — ADDITIVE, present only once `finalized` is true

Every field above `outcome` is unchanged from the previous contract. `outcome` is
**added** on finalize and absent before it: an outcome derived mid-interview would
be a prediction, and a caregiver surface showing "no plan yet" at question four
would be reporting our progress as a finding about their child.

Exactly three fields, and this is the **whole** parent-facing surface for the
result:

| Field | Values | Notes |
|---|---|---|
| `state` | `plan_ready` · `plan_not_available_yet` · `no_focus_identified` · `needs_more_information` | presentation key, **not** a clinical state |
| `plan_available` | boolean | a plan can be produced right now; says nothing about why not |
| `answers_recorded` | integer | questions answered in total |

**`state` is deliberately not a reversible encoding of the clinical outcome.** A
caregiver surface cannot recover the clinical state from it, and that is the point.
The four internal outcome states, the content-support states, canonical family
identifiers, developmental-month labels and the additional-question reason codes are
system and clinician concepts and appear in **no** parent payload.

**Copy is the frontend's.** This contract ships no sentences. Keep parent-facing
language simple and non-diagnostic — qualitative ("a few quick questions"), never a
promised question count: the interview normally targets six questions but
legitimately runs to nine when establishing safe target evidence requires it.
Suggested mapping, to be reworded freely:

| `state` | intent |
|---|---|
| `plan_ready` | a plan is ready to view |
| `plan_not_available_yet` | we do not have activities ready for this yet — a statement about Genex, never about the child |
| `no_focus_identified` | nothing to focus on right now |
| `needs_more_information` | we need a little more information |

### Clinician and engineering views — NOT on this route

Two further derived views exist server-side and are **not** returned here:

- a **minimal clinician summary** (for the later A2 v2.1 surface): derived outcome,
  current serviceability with its content digest, per-area
  concern/uncertain/not-assessed status, and the multi-area result with reasons. It
  carries **no** month labels, milestone prose, raw answers, skill keys, scores or
  internal reason codes.
- an **engineering provenance** view including the additional-question reason
  ledger, for reproducibility and debugging. Not a product surface; the raw reason
  ledger must never be rendered in the Parent UI.

Field audience is frozen in
[`parent_baseline_v2_design.md` §14](./parent_baseline_v2_design.md).

- `status` ∈ `IN_PROGRESS` · `AGE_RELEVANT` · `BOUNDED` · `EMERGING` ·
  `UNRESOLVED`.
- `answer` ∈ `yes` · `sometimes` · `with_help` · `no` · `not_sure`.

### Answer semantics — the rules that make retries safe

1. **`skill_key` must echo `current_question.skill_key` byte for byte.** The v2
   engine *replaces* evidence for a skill rather than appending, so this guard is
   the only thing preventing a late retry from overwriting a correction. Once
   evidence lands the expected key changes and the stale retry is refused with
   **422** — and the error text names the key the server expected, so a client
   can recover without guessing.
2. **A band is completed before the ladder moves.** Every sibling skill in a band
   is asked. A demonstrated skill never carries its siblings, and an `unknown`
   (`not_sure`) never advances the ladder.
3. **The interview can be short.** The band budget is 3 and a band may hold a
   single skill, so a complete assessment may be **two questions**. Do not render
   a fixed-length progress bar; drive progress from `bands[]`.
4. **`GET` writes nothing**, even though it computes the next question
   server-side on a discarded copy. Safe to poll.
5. **`finalize` is idempotent** and takes no body. Finalizing early is allowed;
   the engine reports honestly what the evidence supports (possibly
   `UNRESOLVED`) rather than rounding up to mastery.
6. **The entry choice is routing only.** It says where to *start asking*. It is
   not an ability claim and not an age. Verified live: a `short_sentences`
   (36-month) entry answered all-"no" descended 36 → 30 → 24 and agreed exactly
   with an 18-month entry on band 24. Do not present it as self-assessment.

### Errors — body is always `{"detail": "<code>"}`

| Status | `detail` | Meaning |
|---|---|---|
| 404 | `session_not_found` | no such session |
| 403 | — | session belongs to another user |
| 404 | `unsupported_baseline_domain` | only `talking_and_communicating` is wired |
| 404 | `baseline_not_started` | call `/start` first |
| 409 | `baseline_already_finalized` | `/start` or `/answer` after finalize |
| 409 | `baseline_not_in_progress` | lifecycle violation |
| 409 | `baseline_not_finalized` | projection attempted before finalize |
| 422 | `unknown_entry_choice` | bad `entry_choice_id` |
| 422 | `Unexpected skill_key 'x'. Expected 'y'. Answers must be submitted in order.` | out-of-order answer |

---

## 4. Projection route (Parent → Pilot)

```
POST /api/v2/session/{session_id}/baseline/{domain}/projection   (no body)
→ 200 { "domain", "projected": true, "created": bool, "projection_id": "pbp2_…" }
```

A separate, explicit caregiver action — deliberately **not** automatic on
finalize. Finalizing writes durably to the Parent session and must never depend
on the Pilot being reachable; a failed projection leaves the finalized baseline
exactly as it was and writes nothing back into the Parent session.

The browser sends only this request. It never sees the payload, the Pilot URL or
the service identity token.

### Status codes

| Status | `detail` | Client action |
|---|---|---|
| 200 | — | `created: true` first time, `created: false` on replay |
| 501 | `projection_not_configured` | this deployment may not project. A capability gap, not a fault. Do not retry. |
| 403 | `projection_pairing_forbidden` | declared environment pairing not permitted. Do not retry. |
| 422 | `projection_rejected` | the Pilot refused on the merits — most commonly **the session has no linked canonical child yet** (do the pilot-claim bridge first). Do not retry unchanged. |
| 409 | `projection_withheld` *(object body — see below)* | the baseline outcome is real and is **not** projectable. Do not retry; do not re-run the baseline. |
| 503 | `projection_unavailable` | **retryable** — see below |

### 409 `projection_withheld` — a finding, not an error

The only `detail` on this route that is an **object** rather than a string. It is
returned when the baseline identified a clinically appropriate target and no
approved pilot content currently serves it.

```json
{"detail": {"error": "projection_withheld",
            "status": "WITHHELD",
            "outcome": "TARGET_UNSUPPORTED_BY_CONTENT",
            "withheld_because": "unsupported_by_content",
            "target_identified": true,
            "target_band": 6,
            "target_support": "unserved",
            "unsupported_families": ["..."],
            "unmappable_skills": 0,
            "served_family_set_id": "...",
            "served_family_set_digest": "<64 hex>"}}
```

Nothing is wrong with the request and nothing is wrong with the service, which is
why it is `409` and specifically **not** `422 projection_rejected` — the Pilot never
saw it and never should.

**Client rules:**

- Do **not** retry, and do **not** prompt the caregiver to redo the baseline. The
  evidence is sound; our content is not yet there.
- Do **not** show this body to a caregiver. It is a clinician/engineering payload —
  the parent surface for the same situation is `outcome.state ==
  "plan_not_available_yet"` on the baseline view (§3).
- A clinician surface may render: *a clinically appropriate target was identified,
  but Genex does not currently have approved pilot content for it.*

**Projectability is current system capability, not a property of the child.** It is
evaluated fresh on every call against the reviewed-content state and is never
stored. Adding approved content can therefore make an existing, unchanged baseline
projectable with no migration and no re-interview — and when it does, the child's
answers, canonical target and floor are all unchanged, and the baseline was not
reassessed. `served_family_set_digest` identifies the content state a given
determination was made under, so two differing results are always attributable.
This boundary is frozen in
[`parent_baseline_v2_design.md` §13](./parent_baseline_v2_design.md).

### 503 retry and idempotency — required client behaviour

The Pilot endpoint is **idempotent on `(session, domain, record digest)`**, so an
identical retry is always safe.

A 503 does **not** mean nothing happened. Observed in staging: the Parent-side
HTTP client timed out at 10 seconds during a projection-service cold start and
returned 503, while the projection service logged **200** for the same request —
the write had landed. The retry then returned **200 with `created: false`**.

So the client must:

- treat 503 as retryable with backoff;
- treat `created: false` on a retry as **success**, not a duplicate or an error;
- never create a second projection to "fix" a 503.

Changed evidence yields a different digest, hence a different `projection_id`,
hence a new immutable document — never a silent overwrite. Re-finalizing with the
same evidence therefore re-projects to the *same* id.

`/api/v1/.../projection` behaves identically and remains live; v1 and v2 produce
**separate** records (`pbpj_…` vs `pbp2_…`).

---

## 5. Goal suggestions (Pilot API)

```
POST /pilot/children/{child_id}/goal-suggestions/generate-v2   (no body)
POST /pilot/children/{child_id}/goal-suggestions/generate      (no body)   v1 month-level trigger
GET  /pilot/children/{child_id}/goal-suggestions
```

`generate-v2` requires **provider role AND being this child's managing
clinician**. The `GET` is readable by caregiver *and* provider.

### generate-v2 → 200

```json
{"child_id": "chld_…",
 "outcome": "suggestions_generated",
 "generation_policy": "goal-suggestion-generation-policy-v2-evidence",
 "projection_id": "pbp2_…",
 "target_band_months": 24,
 "generated": [{"rung_ref": "rung1:feb590cf…", "months": 24,
                "created": true, "suggestion_ids": ["gsug_…"]}],
 "suggestion_ids": ["gsug_…"],
 "unsupported_target_refs": [],
 "unknown_refs": [],
 "request_id": "…"}
```

**Zero suggestions with HTTP 200 is an answer, not a failure.** Three outcomes
legitimately produce none: every band mastered; a band left unresolved only by
`unknown`; a band whose known deficits have no reconciled activity family.
Rendering those as errors would tell a clinician her request failed when it
answered, and would push a pointless retry.

- `unknown_refs` — assessed-but-unanswerable skills keeping a band off
  "mastered". **Never a target**: an unanswerable question is not evidence a
  skill is absent.
- `unsupported_target_refs` — canonical deficits with no reconciled activity
  family. Reported rather than dropped, and deliberately **not** rendered as
  suggestions nor near-mapped to a neighbouring family.
- Replay is idempotent: `created: false` per target, same `suggestion_ids`.

No LLM chooses a target. `generation_mode` is `deterministic` and the provenance
fields are the evidence for that claim.

### GET goal-suggestions → 200

`{ "suggestions": [ … ], "request_id": "…" }`, each entry exactly:

`suggestion_id` · `family_facing_text_template` · `status` · `cycle_month` ·
`suggested_priority_rank` · `suggested_emphasis_weight` · `generator_version` ·
`generation_mode` · `domain_key` · `evidence_source` · `milestone_refs` ·
`functional_baseline_area` · `observed_level` · `explicitly_selected` ·
`rule_version`

`family_facing_text_template` contains the literal `{child}` — the Pilot stores
no child name, so **substitution is the client's job at presentation time**. Do
not show the raw template to a family.

`status` moves `offered` → `modified` (or the matching edit type) once a goal is
approved from the suggestion.

---

## 6. Clinician modify / approve (Pilot API)

```
POST /pilot/children/{child_id}/goals            { edit_type, suggestion_id, text, reason }
GET  /pilot/children/{child_id}/goals
POST /pilot/goals/{goal_kind}/{goal_id}/revisions { text, edit_type, reason }
```

`goal_kind` path segment: `clinical` (or `caregiver`). An unrecognised kind is
the standard 403, not a 404, so the vocabulary is not enumerable.

- `edit_type` ∈ `accepted_verbatim` · `modified` · `replaced` · `authored_fresh`.
- **`reason` is required** for `modified`, `replaced` and `authored_fresh`.
  Accepting verbatim needs none — the suggestion *is* the reason. Omitting a
  required reason is **403** (verified).
- `text` is required on `revisions`.

### Responses

```json
POST /goals     → 200 {"goal": { …goal… }, "request_id": "…"}
GET  /goals     → 200 {"goals": [ … ], "request_id": "…"}
POST /revisions → 200 {"version_id": "gver_…", "version_number": 3, "request_id": "…"}
```

Goal object — exactly these fields:

```json
{"goal_kind": "clinical", "goal_id": "clgl_…",
 "current_version_id": "gver_…", "status": "active",
 "is_rtm_eligible": true, "text": "…"}
```

### Client rules

- **Render the `text` from the response, not the string you submitted.** It is
  read back through the authorized path and may not have kept the submitted
  string verbatim.
- `text` is a template; the same `{child}` substitution rule applies.
- `edit_type`, `reason`, actor identity, `derived_from_suggestion_id` and
  `supersedes_version_id` are **not exposed on reads**. Provenance is
  deliberately unreadable — `reason` in particular can carry clinical rationale.
  `current_version_id` is present so a client can correlate versions.
- A clinician reword **does not retarget the goal.** Verified live: after two
  rewords, `generate-v2` still returned the same `target_band_months` and the
  same `rung_ref`. Wording and canonical binding are independent.
- **A revision does not mutate a released week** (see §8).

### Authorization (verified)

| Caller | `POST /goals` | `POST /revisions` | `GET /goals` |
|---|---|---|---|
| caregiver | 403 | 403 | **200** |
| provider, connected but **not** managing | 403 | — | 200 |
| managing clinician | 200 | 200 | 200 |

---

## 7. Week release (Pilot API)

```
POST /pilot/children/{child_id}/weekly-cycles/current/release
{ "family_declared_capacity": 3 }
```

The body allowlist is **exactly one field, and it is required.** One provider
action composes every transition: goal validation → monthly plan (create or
reuse) → goal allocation → activation → Week 1 cycle → candidate lookup →
allocation → immutable snapshot → release. The low-level transitions are
deliberately **not** exposed individually: a browser able to allocate but not
snapshot could leave a week half-planned and visible to nobody.

### `family_declared_capacity`

What the **family reports it can manage this week**, recorded by the provider for
planning. It is **not** a recommended frequency, a prescribed frequency, a
clinical dosage, or a required number of sessions.

**There is no default.** The activity count is a direct function of this number,
so defaulting it would author a clinical frequency nobody approved. The UI must
collect it explicitly. Must be an integer ≥ 1.

### → 200

```json
{"child_id": "chld_…", "created": true, "cycle_id": "wcyc_…",
 "focus_plan_id": "mfpl_…", "goal_ids": ["clgl_…"],
 "activity_count": 3,
 "released_at": "2026-10-13T16:55:29.912417+00:00",
 "week": { …parent week view, §8… },
 "request_id": "…"}
```

The embedded `week` is the **same structured projection the family reads**, so a
clinician sees exactly what was released — no capacity ledger, no weights, no
alignment rows, no rung refs.

### Refusals — every one is `403 {"error": "not permitted"}`

A deliberate no-oracle rule: a finer status would tell a prober which gate
stopped them. Verified causes:

| Cause | Result |
|---|---|
| no / invalid token | **401** (the only non-403) |
| caregiver | 403 |
| provider connected but **not** managing clinician | 403 |
| `family_declared_capacity` absent | 403 |
| `family_declared_capacity` = `0` | 403 (zero would release an empty week) |
| `family_declared_capacity` = `"3"` (string) | 403 |
| `family_declared_capacity` = `true` (bool) | 403 |
| any extra body field | 403 |
| no approved, anchored, activity-mappable goal | 403 |

Because every refusal looks identical, the UI must validate capacity
client-side and surface a generic "could not release" with a retry path rather
than guessing the cause.

### Replay and immutability

- Re-releasing returns the **existing** week: `created: false`, same `cycle_id`,
  identical `week`.
- **A different capacity cannot rewrite a released week.** Verified: sending `7`
  after a release of `3` returned the unchanged 3-activity week. A released
  cycle's content is frozen; a later capacity applies to a future cycle.

---

## 8. This week (Pilot API)

```
GET /pilot/children/{child_id}/this-week
→ 200 {"child_id": "chld_…", "released": bool, "week": null | <view>, "request_id": "…"}
```

Readable by **caregiver and provider** (verified). The caregiver's payload is
byte-identical to the `week` returned by the release call.

### Parent week view

```json
{"view_schema": "pilot-parent-week-v1",
 "week": {"cycle_id": "wcyc_…",
          "starts_on": "2026-10-12", "ends_on": "2026-10-18",
          "released_at": "2026-10-13T16:55:29.912417+00:00"},
 "goals": [{"goal_id": "clgl_…", "text": "…"}],
 "days": [{"local_date": "2026-10-12",
           "activities": [{"activity_ref": "pact1:ef904328…",
                           "goal_id": "clgl_…",
                           "title": "My Turn",
                           "materials": "a favorite shared toy, simple game, …",
                           "instructions": "Share a favorite toy and model …",
                           "success_criteria": "The child attempts 'my turn,' …",
                           "make_easier": "Model the whole phrase and …",
                           "make_harder": "Model one extra word naturally, …",
                           "what_to_avoid": "Do not take away a toy simply …"}]}]}
```

Exactly **nine keys per activity**: `activity_ref`, `goal_id`, and the seven
content fields. The view is built by naming every field rather than by copying
and deleting, so a field added to the stored document never starts shipping.
A recursive guard additionally refuses any internal key.

### Client rules

- **Draft invisibility.** `released: false` with `week: null` covers *both*
  "nothing yet" *and* "a draft cycle exists". A draft is deliberately
  indistinguishable from nothing, because a partially allocated week must never
  reach a family. Verified: a DRAFT monthly plan and an ACTIVE monthly plan with
  no released week both read `released: false`.
- **Days cover the whole cycle, including empty days.** A family seeing Thursday
  absent entirely cannot tell "nothing scheduled" from "the app lost it". Render
  empty days explicitly.
- `activity_ref` is a `pact1:` **placement** reference, not the internal
  `atpl1:` template id. Use it as the stable key for future observations.
- Goal rows carry `goal_id` and `text` only — no rung, no anchor, no version
  lineage.

### Starter-week rule (frozen)

```
plan_start_date = the child's FIRST PLAN DATE itself
plan_end_date   = the SUNDAY of that local week
plan_type       = starter_partial_week, unless it starts on a Monday
week 2 onward   = Monday through Sunday
```

Verified live: a first plan on Thursday 2026-10-08 produced `2026-10-08 →
2026-10-11` — a 4-day partial week, **not** back-dated to Monday Oct 5 — and the
next cycle began Monday 2026-10-12 and ran a full week to Sunday 2026-10-18.
No activity ever predates the first-plan date.

**`/weekly-cycles/current/release` always targets the cycle containing *today*.**
For a brand-new child that is the partial starter week. Once the week turns it is
the current Monday–Sunday cycle instead. "Week 1" is not a state a client can
re-enter — any rehearsal that assumes "release gives me Week 1" will silently
exercise a different cycle after the week rolls over.

### Released-week immutability

A released week is frozen. Verified live:

- re-release → `created: false`, identical week;
- a different `family_declared_capacity` → cannot rewrite it;
- a **later goal revision** → the goal advances to a new version, and the
  released week is **byte-identical** and still carries the wording approved at
  release time.

So a client must not expect a released week to reflect subsequent goal edits, and
must re-read `this-week` rather than patching it locally.

---

## 9. Supporting reads

| Route | Response | Audience |
|---|---|---|
| `GET /pilot/me` | `{actor_id, role, provider_id, practice_id, request_id}` — caregivers get `caregiver_id` instead | both |
| `GET /pilot/me/children` | `{child_ids: [...], request_id}` | both |
| `GET /pilot/children/{id}/managing-clinician` | `{assignment_id, provider_id, request_id}` | caregiver-readable |
| `GET /pilot/children/{id}/provider-connections` | connection rows | caregiver |
| `GET /pilot/children/{id}/monthly-plan` | `{plan: {focus_plan_id, cycle_month, state, timezone_of_record, policy_version}, allocations: [{allocation_id, goal_kind, goal_id, priority_rank, emphasis_weight, min_coverage_per_cycle, status}], request_id}` | both — see §10 |
| `GET /pilot/children/{id}/current-cycle` | adds `cycle: {cycle_id, sequence_in_month, starts_on, ends_on, is_partial, state}` plus `alignments`, `coverage_gaps`, `plan_snapshot` | **clinician only** |

`emphasis_weight` is a relative weight and `priority_rank` an ordering. **Neither
is a percentage or a share of anything.** The field names are the domain names so
a UI cannot mistake one for a proportion.

An absent monthly plan reads `{"plan": null, "allocations": []}`; an absent cycle
reads `{"plan": null, "cycle": null, "alignments": [], "coverage_gaps": [],
"plan_snapshot": null}`.

---

## 10. Field audience rules

### Clinician-only — never render in the Parent/caregiver UI

- `rung_ref`, `track_ref`, `target_band_months`, `months` on a generated target
- `projection_id`, `source_record_digest`, any artifact or record digest
- `generation_policy`, `generator_version`, `generation_mode`, `rule_version`,
  `policy_version`, `taxonomy_version`, `baseline_version`
- `unsupported_target_refs`, `unknown_refs`, `milestone_refs`
- `suggested_priority_rank`, `suggested_emphasis_weight`, `priority_rank`,
  `emphasis_weight`, `min_coverage_per_cycle`
- `family_declared_capacity` and anything capacity- or ledger-shaped
- `alignments`, `coverage_gaps`, `plan_snapshot`, `cycle.sequence_in_month`
- `activity_family_ref`, activity bucket names, `atpl1:` template ids,
  `source_pool_ref`, `source_tier`, `group_play_line`
- `GoalVersion` provenance: `edit_type`, `reason`, actor,
  `derived_from_suggestion_id`, `supersedes_version_id`

**The API permits a caregiver token to read `goals`, `goal-suggestions` and
`monthly-plan` (all 200, verified).** That is a deliberate access decision, not
a licence to display them. Deciding what the family app surfaces is a product
choice the frontend must make explicitly.

### Must never appear anywhere in a Parent-facing payload

The released-week view is a strict allowlist and was verified live to contain
none of the following. If any appears, treat it as a defect and stop:

`family_declared_capacity` · `declared_capacity` · `capacity` ·
`allocated_by_planner` · `emphasis_weight` · `priority_rank` · `rung_ref` ·
`track_ref` · `taxonomy_version` · `baseline_version` · `generation_key` ·
`claim_id` · `alignment_id` · `coverage_gap(s)` · `gap_id` · `ledger_id` ·
`rule_version` · `milestone_refs` · any `atpl1:` template id · any 64-character
hex digest · `dev_age` · `skill_key` · `subdomain` · `auth_subject` ·
`source_record_digest` · `entry_choice_id` · `routing_anchor_months` ·
`demonstrated_months` · `not_demonstrated_months`

### No developmental-age representation — anywhere

There is **no `dev_age` field in this system** and none may be synthesised.
Verified absent from the Parent session document, the finalized v2 record, and
every Pilot read.

Specifically:

- `bands[].months` is a **band label** on the assessment ladder. It is **not** the
  child's developmental age and must never be rendered as one, nor as "your
  child is at N months".
- The baseline view deliberately omits `routing_anchor_months`,
  `demonstrated_months` and `not_demonstrated_months`. Those are server-side
  provenance for the projection path. A raw anchor number in a parent UI would
  read as a developmental age, which is exactly what their naming prevents.
- `entry_choice_id` is a routing descriptor, not a level. Do not display it back
  as a finding or an achieved stage.
- Chronological age is never echoed by the baseline surface; it is read from the
  session the parent already completed, so a client cannot shift the starting
  band by restating it.

Render progress as *what was asked and what the caregiver reported* — band
completion and mastery booleans — never as an age equivalence.

---

## 11. Quick reference — status codes

| Code | Where | Meaning |
|---|---|---|
| 401 | both | missing / invalid / expired token |
| 403 | Pilot workflow surface | **every** authorization, body and domain refusal, as one constant body `{"error": "not permitted"}` |
| 403 | Parent | session not owned; beta code rejected; `projection_pairing_forbidden` |
| 404 | Pilot | the path is not registered at all |
| 404 | Parent | `session_not_found`, `unsupported_baseline_domain`, `baseline_not_started` |
| 405 | Pilot | path registered under a different method |
| 409 | Parent | baseline lifecycle conflicts, and `projection_withheld` (§4 — object body) |
| 422 | Parent | Pydantic validation, skill-key mismatch, `projection_rejected` |
| 500 | Pilot | unexpected failure; nothing about it reaches the client |
| 501 | Parent | `projection_not_configured` — capability gap, not a fault |
| 503 | Parent | `projection_unavailable` — retryable and idempotent (§4) |

On the Pilot, `404` vs `405` vs `401` is the only way to tell a missing route
from a wrong method from an unauthenticated call. Past the auth gate every
refusal collapses to `403` by design.
