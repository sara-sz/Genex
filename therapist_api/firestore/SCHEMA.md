# Therapist collaboration store — Firestore schema & index notes (DESIGN)

**Status:** design documentation for the alpha. **No Firestore database is
created or connected in this phase.** All data is fictional.

## Access model (correction #2)

```
Frontend (browser/mobile)  ──HTTPS──▶  Cloud Run API  ──SA creds──▶  Firestore
        (Firebase ID token)                (verifies + authorizes)
```

- Clients **never** touch Firestore directly. `firestore.rules` is **deny-all**.
- The Cloud Run service account accesses Firestore; it bypasses client rules,
  so authorization is enforced **in the API**, not by client rules.
- Deny-all rules are a backstop, **not** the therapist-child authorization layer.

## Uniqueness & idempotency (correction #3)

Firestore has no SQL-style UNIQUE constraint. Uniqueness is enforced by
**deterministic document ids + create-if-absent transactions**:

| Concern | Document id | Effect |
|---|---|---|
| Idempotent operation | `idem_<sha256(idempotency_key)[:32]>` | one record per key |
| Terminal parent response | `resp_<sha256(recommendation_id)[:32]>` | exactly one response per recommendation |

A duplicate submission/response collides on the same id, the transaction sees an
existing document, and the **original** result is returned — no duplicate record,
no duplicate plan mutation. Reference: `app/domain/ids.py`,
`app/services/recommendations.py`.

## Collections (alpha subset)

| Collection | Key fields | Notes |
|---|---|---|
| `environment_metadata` | singleton | read at startup for fail-closed checks |
| `user_roles` | `uid`, `role`, `environment` | role resolved server-side; UI role ≠ authz |
| `therapist_profiles` | `uid`, `discipline="slp"` | SLP-first |
| `child_references` | `id (child_ref)`, `display_alias` | **name-minimal**; `preferred_name` reserved, consent+BAA-gated (correction #7) |
| `connections` | `therapist_uid`, `child_ref`, `status`, `scopes` | invited/active/paused/ended/declined |
| `recommendations` | `child_ref`, `status`, `idempotency_key`, `plan_version_at_creation` | state machine in `app/domain/recommendation_state.py` |
| `recommendation_responses` | deterministic id from `recommendation_id` | one terminal response per recommendation |
| `audit_events` | append-only | ids/metadata only; never content |

## Composite indexes

See `firestore.indexes.json`. Indexes support queries by
`(therapist_uid,status)`, `(child_ref,status)`, `(connection_id,status)`, and
audit trails by `(recommendation_id,created_at)` / `(child_ref,created_at)`.
Indexes are **not** the uniqueness mechanism.

## Environments

Separate Firestore databases in separate GCP/Firebase projects
(`genex-provider-dev-2026`, `genex-provider-prod-2026`). Prod Firestore stays
**empty** while dark. Every document carries an `environment` field as a
defense-in-depth cross-check alongside project isolation.
