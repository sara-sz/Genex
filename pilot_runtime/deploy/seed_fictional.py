"""Seed the FICTIONAL staging identities. Not a product feature.

Two of the three identity shapes the pilot needs can be created over HTTP by
the subject themselves: a caregiver bootstraps via
`POST /pilot/bootstrap/caregiver`, and a child is linked from a Parent
session. A PROVIDER cannot. `ProviderProvisioningService` is deliberately
unexposed — admitting a clinician is an administrative act, not a
self-service one, and 0.5B scoped out public provider self-registration on
purpose.

So provisioning a fictional provider needs a script, and this is it. It calls
the SAME frozen `provision_provider_record` the proven suites call, so the
atomic claim-plus-Provider commit that closed PRE-PHI blocker 4 is exercised
rather than bypassed. No record is written by hand.

## Refuses to run against anything real

    PILOT_ENVIRONMENT must be dev or test        - never prod
    every name written here is visibly fictional - see FICTIONAL_* below
    idempotent                                   - re-running converges

Run with the staging configuration in the environment:

    PILOT_ENVIRONMENT=dev \
    PILOT_GCP_PROJECT_ID=genex-pilot-staging \
    PILOT_FIREBASE_PROJECT_ID=genex-pilot-staging \
    PILOT_FIRESTORE_DATABASE=pilot-staging \
    PILOT_ALLOWED_ORIGINS=https://example.invalid \
    python -m pilot_runtime.deploy.seed_fictional <provider_auth_subject>
"""

from __future__ import annotations

import os
import sys

from pilot_backend.config import PilotSettings
from pilot_backend.domain.entities import Practice
from pilot_backend.domain.enums import ProviderDiscipline
from pilot_backend.persistence import FirestoreRepositories
from pilot_backend.provisioning.service import provision_provider_record
from pilot_backend.repository.interface import RecordNotFound
from pilot_runtime.composition import build_store

#: Deliberately unmistakable. A reader seeing these in a console or a
#: Firestore row should never have to wonder whether they are looking at a
#: real practice or a real clinician.
FICTIONAL_PRACTICE_NAME = "FICTIONAL Pilot Staging Practice (not a real clinic)"
FICTIONAL_PROVIDER_NAME = "FICTIONAL Staging Clinician (not a real person)"

#: A FIXED id rather than a minted one, so re-running converges instead of
#: accumulating practices. `new_practice_id()` is random by design, which is
#: right for the product and wrong for an idempotent seed. The id spells out
#: what it is; nothing validates the suffix's shape, only its prefix is
#: conventional.
FICTIONAL_PRACTICE_ID = "prac_fictionalstagingpracticedonotuse"


class SeedRefused(Exception):
    """The seed was pointed at something it must not write to."""

    PHI_SAFE_MESSAGE = True


def _settings() -> PilotSettings:
    settings = PilotSettings.from_env(os.environ)
    if settings.environment.is_prod:
        raise SeedRefused(
            "the fictional seed must never run against prod")
    if not settings.gcp_project_id.strip():
        raise SeedRefused("PILOT_GCP_PROJECT_ID is required")
    return settings


def _practice(repos) -> Practice:
    """Fetch the fictional practice or create it. Idempotent.

    `FirestorePracticeRepository` exposes no public listing — only `create`,
    `get_by_id` and `update_status` — so this resolves by the fixed id rather
    than reaching for the protected `_all()`. Narrow repositories are a
    feature, not an obstacle to route around.
    """
    try:
        return repos.practices.get_by_id(FICTIONAL_PRACTICE_ID)
    except RecordNotFound:
        pass
    practice = Practice(practice_id=FICTIONAL_PRACTICE_ID,
                        legal_name=FICTIONAL_PRACTICE_NAME)
    repos.practices.create(practice)
    return practice


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 64
    auth_subject = argv[1].strip()
    if not auth_subject:
        print("a provider auth subject is required")
        return 64

    settings = _settings()
    store = build_store(settings, process_env=os.environ)
    repos = FirestoreRepositories(store)

    practice = _practice(repos)
    print(f"practice : {practice.practice_id}")

    # The frozen path. Idempotent and concurrency-safe by its own contract,
    # so re-running this converges on the same provider rather than minting a
    # second one for the same subject.
    result = provision_provider_record(
        repos,
        auth_subject=auth_subject,
        practice_id=practice.practice_id,
        discipline=ProviderDiscipline.SLP,
        display_name=FICTIONAL_PROVIDER_NAME,
    )
    provider = getattr(result, "provider", result)
    print(f"provider : {provider.provider_id}")
    print(f"outcome  : {getattr(result, 'outcome', 'n/a')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
