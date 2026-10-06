"""pilot_runtime/bootstrap_iam.py — the IAM the bootstrap service needs.

A documented, TESTED statement of the least-privilege permission set for the
`pilot-bootstrap-staging` runtime service account, derived from the operations
the registration service actually performs and nothing else.

It exists because a permission list that lives only in a report is a list nobody
can check — and because an earlier description of this boundary was WRONG in a
way worth correcting in code rather than prose.

## THE CORRECTION: FIRESTORE IAM IS NOT PER-COLLECTION

An earlier note said this service account needs permissions "only on the claims
collection". That is not a thing Firestore can express.

Server-side Firestore authorization is `datastore.entities.*` at the DATABASE
level. There is no per-collection IAM resource, no collection-scoped condition,
and no way to grant `datastore.entities.get` for `pilot_parent_session_claims`
while withholding it for `pilot_children`. A principal holding
`datastore.entities.get` on this database can read ANY document in it.

(Firestore Security Rules ARE path-scoped, but they govern the client SDKs —
web, iOS, Android — and are bypassed entirely by the server SDKs that run here.
They are not an authorization boundary for this service.)

So the narrowing is real but it lives in TWO different places, and conflating
them overstates what IAM is doing:

  ## THE IAM BOUNDARY — as narrow as GCP permits

      * a CUSTOM role, not `roles/datastore.user`, containing exactly the two
        entity permissions the service performs
      * granted to a DEDICATED service account used by no other service
      * `create` and `get` only — no `update`, no `delete`, no `list`, no
        database administration, no import/export
      * scoped to the one project that holds the one staging database

    What this genuinely prevents: this identity cannot modify or delete ANY
    existing record anywhere in the database, and cannot enumerate collections
    by query. The worst a compromise of it can do is read documents it knows
    the id of, and create new ones.

    What it does NOT prevent: reading a `pilot_children` document whose id is
    known. IAM cannot express that restriction, and claiming otherwise would be
    a security statement that is not true.

  ## THE APPLICATION / REPOSITORY BOUNDARY — where collection scoping lives

      * the service reaches exactly ONE repository,
        `FirestoreParentSessionClaimRepository`, whose only methods are `create`
        and `find`
      * it never imports, constructs or calls the children, caregiver-child
        connection, source-link, goal, plan, cycle, observation or RTM
        repositories
      * `BootstrapApp` exposes one route that calls one service method

    This is asserted structurally by `tests/test_bootstrap_service.py` and
    `pilot_backend/tests/test_parent_session_claim.py`, over the import graph
    and over the recorded store operations — not merely intended.

The honest summary: IAM bounds the OPERATIONS, the application bounds the
COLLECTIONS, and only both together give the property we want.

## WHY NOT roles/datastore.user

`roles/datastore.user` grants `datastore.entities.update`, `.delete` and
`.list`, plus namespace and statistics access. The registration repository is
create-and-read-only and performs no query, so every one of those is a
capability the code cannot use and an attacker could. The two-permission custom
role is strictly smaller than the A2 projection role, which needs `.list` for
its integrity query.

## WHAT IS NOT NEEDED, AND WHY

Redemption — which DOES write children, connections, source links and identity
claims transactionally — happens on the BROWSER Pilot API under a caregiver
principal, not here. So this service account needs none of those writes, and
deliberately has none.
"""

from __future__ import annotations

from typing import Mapping, Tuple

#: The project and database the staging bootstrap service addresses.
STAGING_PROJECT = "genex-pilot-staging"
STAGING_DATABASE = "pilot-staging"

#: The dedicated runtime identity. Deliberately NOT the A2 projection service
#: account: that one holds a different (and differently-shaped) role, and
#: sharing an identity between two internal services would make either one's
#: compromise the other's.
BOOTSTRAP_SERVICE_ACCOUNT = (
    "pilot-bootstrap-staging-run@genex-pilot-staging.iam.gserviceaccount.com")

#: The service account that may INVOKE the bootstrap service. Exactly one.
PERMITTED_INVOKER = (
    "genex-parent-staging-run@genex-mvp-2026.iam.gserviceaccount.com")

#: The proposed custom role id.
CUSTOM_ROLE_ID = "pilotBootstrapClaimRegistrar"

#: Every permission, with the operation that needs it. Empirically derived: a
#: recording wrapper around the document store observed exactly these two.
REQUIRED_PERMISSIONS: Mapping[str, str] = {
    "datastore.entities.create":
        "reference.create — registering a new pending claim. A true create "
        "with an exists=false precondition, not an upsert.",
    "datastore.entities.get":
        "reference.get — the idempotency read that turns a replayed "
        "registration into one read instead of a guaranteed collision.",
}

#: Permissions deliberately NOT requested, with the reason. Listed so each
#: absence is a decision rather than an oversight.
EXCLUDED_PERMISSIONS: Mapping[str, str] = {
    "datastore.entities.update":
        "a pending claim is immutable; there is no update method on the "
        "repository and redemption is recorded as a separate claim",
    "datastore.entities.delete":
        "nothing deletes a claim; an expired one simply stops being "
        "redeemable, and deleting it would erase the audit of its issuance",
    "datastore.entities.list":
        "the registration service performs NO query — it addresses the claim "
        "by its digest, which is the document id. This is the one permission "
        "the A2 projection role needs and this one does not.",
    "datastore.databases.create": "the database already exists",
    "datastore.databases.delete": "nothing may delete the pilot database",
    "datastore.indexes.create": "no composite index is required for a get",
    "datastore.databases.export": "no bulk read of clinical data",
    "datastore.databases.import": "no bulk write",
}

#: Predefined roles explicitly rejected.
REJECTED_ROLES: Mapping[str, str] = {
    "roles/datastore.user":
        "grants entities.update, entities.delete and entities.list, none of "
        "which this service can use",
    "roles/datastore.owner": "database administration",
    "roles/datastore.importExportAdmin": "bulk movement of clinical records",
    "roles/editor": "project-wide; the mistake the Parent staging SA replaced",
    "roles/owner": "not on the table",
}

#: Store operations the application performs. Empirically recorded, and the
#: companion to the permission list: this is what makes the two-permission role
#: sufficient.
APPLICATION_OPERATIONS: Tuple[str, ...] = (
    "get",      # find(digest) — the idempotency read
    "create",   # create(claim) — the registration write
)

#: Store operations that must NEVER appear in this service's reachable code.
FORBIDDEN_OPERATIONS: Tuple[str, ...] = (
    "set",
    "overwrite",
    "delete",
    "query_equals",
    "list_all",
    "run_in_transaction",
)

#: Repositories the bootstrap service may reach. Exactly one.
#:
#: This — not IAM — is what confines the service to one collection, and the
#: distinction is the whole point of this module.
PERMITTED_REPOSITORIES: Tuple[str, ...] = (
    "parent_session_claims",
)

#: Repositories the bootstrap service must never reach. Named individually so a
#: future edit that wires one in fails a test rather than passing review.
FORBIDDEN_REPOSITORIES: Tuple[str, ...] = (
    "children",
    "caregivers",
    "caregiver_child",
    "provider_child",
    "source_links",
    "identity_claims",
    "auth_subject_claims",
    "goal_suggestions",
    "clinical_goals",
    "caregiver_goals",
    "goal_versions",
    "suggestion_anchors",
    "clinical_goal_anchors",
    "focus_plans",
    "goal_allocations",
    "goal_snapshots",
    "weekly_cycles",
    "observation_events",
    "rtm_episodes",
    "rtm_periods",
    "managing_clinicians",
    "parent_baseline_projections",
)


def gcloud_commands() -> Tuple[str, ...]:
    """The exact commands an operator would run. Not executed from here.

    Returned as data so a test can assert the proposal never names a broad role
    — the kind of drift a prose runbook hides.
    """
    permissions = ",".join(sorted(REQUIRED_PERMISSIONS))
    return (
        f"gcloud iam service-accounts create pilot-bootstrap-staging-run "
        f"--project {STAGING_PROJECT} "
        f"--display-name 'Pilot 0.5F-A3 bootstrap staging Cloud Run runtime'",
        f"gcloud iam roles create {CUSTOM_ROLE_ID} "
        f"--project {STAGING_PROJECT} "
        f"--title 'Pilot bootstrap claim registrar' "
        f"--description 'Create and read pending Parent-session claims. No "
        f"entity update, delete or query. Firestore IAM is database-scoped, so "
        f"the single-collection restriction is enforced by the application, "
        f"not by this role.' "
        f"--permissions {permissions} "
        f"--stage GA",
        f"gcloud projects add-iam-policy-binding {STAGING_PROJECT} "
        f"--member serviceAccount:{BOOTSTRAP_SERVICE_ACCOUNT} "
        f"--role projects/{STAGING_PROJECT}/roles/{CUSTOM_ROLE_ID}",
        f"gcloud run services add-iam-policy-binding pilot-bootstrap-staging "
        f"--project {STAGING_PROJECT} --region us-central1 "
        f"--member serviceAccount:{PERMITTED_INVOKER} "
        f"--role roles/run.invoker",
    )
