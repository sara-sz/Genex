# Genex Therapist API (SLP-first) — local foundation

Isolated provider service for the therapist product. **Alpha / fictional data
only.** This directory lives in a dedicated git worktree and does not modify the
parent backend, `genex_core`, or any parent GCS data.

**Phase 1A — read-only vertical slice:** see [docs/READ_SLICE.md](docs/READ_SLICE.md)
for the domain models, enums, endpoint table, local fictional dev-auth, and the
authorization matrix. It is local + read-only + fictional; no Parent API, no
Firebase/Firestore, no writes.

## Hard isolation guarantees

- ❌ No `genex_core` import
- ❌ No parent `api/` import
- ❌ No parent GCS access
- ❌ No live Firebase / Firestore / GCP calls in this phase
- ✅ Frontend → Cloud Run API → Firestore (clients never touch Firestore directly)

## Layout

```
therapist_api/
  app/
    constants.py            non-secret identifiers (projects, origins, taxonomy)
    settings.py             typed env settings (dataclass)
    env_validation.py       fail-closed environment validation
    logging_config.py       structured JSON logs + request id
    middleware.py           request-id + logging middleware
    main.py                 FastAPI factory: /health, /api/v1/app/config
    auth/                   auth interface (fail-closed default) + test stub
    authz/                  authz interface (fail-closed default) + test stub
    domain/                 roles, domains, provenance, recommendation state machine,
                            deterministic ids, fixture/schema models
    repository/             repo interface + in-memory impl
    services/              idempotent recommendation design (test-exercised, no HTTP)
    seed/                   dev-only guard, fixtures, seed + destructive QA scripts
  firestore/                deny-all client rules, indexes, schema docs (design)
  infra/terraform/          therapist environment IaC (design; never applied here)
  tests/                    pytest suite
  Dockerfile                independent container (uvicorn --factory)
  cloudbuild.therapist.yaml build/promote design (correction #4)
  pyproject.toml            independent project + pytest config
```

## Run tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

## Run locally (dev)

```bash
ENVIRONMENT=dev \
GCP_PROJECT_ID=genex-provider-dev-2026 \
FIREBASE_PROJECT_ID=genex-provider-dev-2026 \
FIRESTORE_PROJECT_ID=genex-provider-dev-2026 \
REGION=us-central1 \
ALLOWED_ORIGINS=https://genex-therapist-dev.lovable.app,http://localhost:5173 \
REGISTRATION_POLICY=open-dev \
uvicorn app.main:create_asgi_app --factory --port 8080
```

The app **refuses to start** if the environment is missing or incoherent
(fail-closed). Prod requires `SEED_ENABLED=false`, `DEBUG_PANELS=false`,
`REGISTRATION_POLICY=invite-only`, and prod-only origins.

## What is intentionally NOT here (later, explicitly-approved phases)

Real Firebase verification, real Firestore, GCP/Firebase resources, Terraform
apply, deployment, parent-service access, real recommendation endpoints, real
plan mutation, frontend integration.
