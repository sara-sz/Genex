"""api/session_store_iam.py — the IAM the session store actually needs.

A documented, TESTED statement of the least-privilege permission set for the
Parent staging runtime service account, derived from the operations
`api/session_store.py` performs and nothing else.

It exists because an earlier proposal for this slice was WRONG in a way that
would have broken the Parent staging service on deploy — see below — and a
permission list that lives only in a report is a list nobody can check.

## THE CORRECTION

The first proposal was `roles/storage.objectViewer` + `roles/storage.objectCreator`,
on the reasoning that replacing an object needs only `storage.objects.create`.

That is wrong. In Google Cloud Storage, `storage.objects.create` adds a NEW
object; REPLACING an object that already exists at the same name additionally
requires `storage.objects.delete`. `roles/storage.objectCreator` therefore
cannot overwrite, and `_gcs_save_raw` overwrites
`sessions/{uid}/{session_id}.json` on every save after the first — which is
essentially every save. The service would have worked once per session and
then started failing.

## IS THERE A NARROWER MECHANISM?

Considered and rejected:

  * There is no `storage.objects.overwrite` permission. Replacement is
    modelled as create-over-delete and there is no third option.
  * Object versioning and soft delete do not change the requirement: they
    govern what happens to the superseded version, not who may supersede it.
  * `if_generation_match` makes the write CONDITIONAL, not differently
    permissioned — `save_if_generation_match` is still `objects.insert`.
  * Writing a NEW object name per save (append-only, e.g.
    `sessions/{uid}/{session_id}/{n}.json`) would avoid delete entirely. It is
    also a redesign of the session store's layout, and it would break the blob
    path the 0.5A pilot adapter reads and pins. Out of scope for A2, and not
    obviously better: it trades one permission for unbounded object growth and
    a read that has to find the latest.

So `storage.objects.delete` is required. The honest framing is that it is
required BY THE PLATFORM for same-name replacement and is never exercised as
an application operation: no module in `genex-parent` calls `blob.delete()`,
`delete_blob` or anything equivalent, and a test asserts that over the AST.

This statement is from Google's documented IAM model, not from an empirical
grant-and-try: proving it live needs a bucket and an IAM change, neither of
which this slice performs. The empirical confirmation belongs with the
deployment, alongside `probe_projection_auth.sh`.

## WHY A CUSTOM ROLE RATHER THAN A PREDEFINED ONE

The four permissions below are exactly `roles/storage.objectUser`. A custom
role is preferred anyway, for two reasons: a predefined role's contents can
change under you as Google revises it, and a custom role bound to ONE bucket
makes the intent reviewable — a reader sees four permissions and a bucket
rather than a role name they have to go look up.

`roles/storage.admin` is not on the table: it grants bucket administration,
including deleting the bucket itself.
"""

from __future__ import annotations

from typing import Mapping, Tuple

#: The one bucket the Parent STAGING runtime may touch. Production uses a
#: different bucket and will receive its own binding after PRE-PHI approval.
STAGING_SESSION_BUCKET = "genex-api-dev-sessions-genex-mvp-2026"

#: The proposed custom role, bound at BUCKET scope, not project scope.
CUSTOM_ROLE_ID = "genexParentStagingSessionStore"

#: Every permission, with the operation that needs it. The mapping is the
#: documentation: a reviewer can check each line against `session_store.py`.
REQUIRED_PERMISSIONS: Mapping[str, str] = {
    "storage.objects.get":
        "blob.download_as_text / bucket.get_blob / blob.exists — reading a "
        "session and probing for its existence",
    "storage.objects.list":
        "client.list_blobs — find_latest_for_uid enumerates a uid's prefix",
    "storage.objects.create":
        "blob.upload_from_string — writing a session, including the "
        "generation-guarded save",
    "storage.objects.delete":
        "REQUIRED BY GCS to replace an existing object at the same name. "
        "Never called by the application: no module deletes a blob.",
}

#: Permissions deliberately NOT requested, with the reason. Listed so the
#: absence is a decision rather than an oversight.
EXCLUDED_PERMISSIONS: Mapping[str, str] = {
    "storage.buckets.create": "the bucket already exists",
    "storage.buckets.delete": "nothing may delete the session bucket",
    "storage.buckets.update": "no lifecycle, CORS or IAM change is needed",
    "storage.buckets.setIamPolicy": "the runtime must not grant itself access",
    "storage.objects.setIamPolicy": "per-object ACLs are not used",
    "storage.objects.update": "metadata is never patched in place",
}

#: Operations the application performs. A delete is NOT among them, which is
#: the point of recording this list beside the permission that enables one.
APPLICATION_OPERATIONS: Tuple[str, ...] = (
    "upload_from_string",     # create, and overwrite of the same name
    "download_as_text",       # read
    "get_blob",               # read with generation
    "exists",                 # existence probe
    "list_blobs",             # enumerate a uid's prefix
)

#: Blob-level operations that must never appear in application code, even
#: though the platform requires the matching permission for overwrite.
FORBIDDEN_OPERATIONS: Tuple[str, ...] = (
    "delete",
    "delete_blob",
    "delete_blobs",
    "compose",
    "rewrite",
    "make_public",
)


def gcloud_commands() -> Tuple[str, ...]:
    """The exact commands an operator would run. Not executed from here.

    Returned as data so a test can assert the proposal stays bucket-scoped and
    never names a broad role — the kind of drift a prose runbook hides.
    """
    permissions = ",".join(sorted(REQUIRED_PERMISSIONS))
    return (
        f"gcloud iam roles create {CUSTOM_ROLE_ID} "
        f"--project genex-mvp-2026 "
        f"--title 'Genex Parent staging session store' "
        f"--description 'Read/write one session object per child-session; "
        f"delete is required by GCS for same-name replacement and is never "
        f"called by the application' "
        f"--permissions {permissions} "
        f"--stage GA",
        f"gcloud storage buckets add-iam-policy-binding "
        f"gs://{STAGING_SESSION_BUCKET} "
        f"--member serviceAccount:genex-parent-staging-run@"
        f"genex-mvp-2026.iam.gserviceaccount.com "
        f"--role projects/genex-mvp-2026/roles/{CUSTOM_ROLE_ID}",
    )
