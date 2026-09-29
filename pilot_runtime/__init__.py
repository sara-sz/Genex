"""pilot_runtime — the SDK boundary for the October pilot backend.

`pilot_backend` is deliberately provider-agnostic: it defines the `TokenDecoder`
and `DocumentStore` ports and imports no auth SDK and no database driver, a
property asserted structurally by the BACKEND 0.1 and 0.2 guards. This package
is where the real Google bindings live, and it is the ONLY place in the repo
that may import `firebase_admin` or `google.cloud.firestore`.

The split is not ceremony. It is what lets the entire security suite — 292
tests covering authentication, authorization, audit, revisions, logging and
HTTP composition — run in a CI job that installs pytest and the standard
library, with no credentials and no network. Those tests would be unable to
prove "no SDK is imported" in a job where the SDKs were installed.

Layout:

    auth/          Firebase Admin ID-token verification
    persistence/   Firestore document store
    workflows/     the minimal fictional pilot workflows
    composition.py the application composition root
"""
