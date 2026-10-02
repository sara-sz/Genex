"""Seed ONE fictional child and its developmental inputs. Staging only.

## Why a script rather than a route

Three of the things a browser-driven pilot needs cannot be created over HTTP,
by design rather than omission:

    a Child                  - `link_parent_session` mints one, but it needs a
                               Parent session from the GCS bucket that staging
                               deliberately does not have
    goal suggestions         - `generate_suggestions` takes an
                               `ObservationSnapshot` as a PARAMETER and no
                               route exposes it; the developmental inputs come
                               from Parent, which is not wired to staging
    a weekly cycle           - `create_cycle`, `capture_snapshot`,
                               `allocate_cycle` and `release_cycle` have zero
                               references in the transport layer

The first two are what this script creates. The third is deliberately NOT
created here — see `seed_fictional_cycle.py`, which has to run after the two
UIs have done their part, because a cycle needs an active monthly plan and a
plan needs a managing clinician.

## The connection is deliberately absent

No `ProviderChildConnection` and no `ManagingClinicianAssignment` are created.
That is the point: the Parent UI invites the provider and the Therapist UI
accepts, which is the flow being tested. Seeding it would skip the thing we
want to watch a human do.

## Everything is fictional and deterministic

Ids are derived from a fixed literal, so re-running converges rather than
accumulating children. The `Child` domain object has nowhere to put a name, an
age or a diagnosis, so none is invented. Domain keys are canonical vocabulary
terms, not clinical statements about a person. No real name, date of birth,
diagnosis or clinical text appears anywhere in this file.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from pilot_backend.auth.resolver import resolve_principal
from pilot_backend.auth.verifiers import VerifiedToken
from pilot_backend.config import PilotSettings
from pilot_backend.domain.connections import CaregiverChildConnection
from pilot_backend.domain.entities import Child
from pilot_backend.domain.enums import CaregiverRelationship
from pilot_backend.goals.service import GoalService
from pilot_backend.goals.suggestion_engine import (
    EvidenceSource,
    ObservationSnapshot,
    ObservedDomain,
)
from pilot_backend.persistence import FirestoreRepositories
from pilot_backend.repository.interface import RecordNotFound
from pilot_runtime.composition import build_store

#: Fixed so the seed is idempotent. `Child.create` mints a random id, which is
#: right for the product and wrong for a fixture that may be re-run.
FICTIONAL_CHILD_ID = "child_fictionalstagingchilddonotuse"

#: Three canonical domains, two answered and one explicitly chosen. Enough for
#: the ranker to produce an ordered, reproducible set of suggestions without
#: asserting anything about a real person's development.
#:
#: `answered=False` on the third is meaningful, not filler: the engine skips
#: an unanswered domain rather than defaulting it to a level, and having one
#: present exercises that branch in staging.
FICTIONAL_DOMAINS = (
    ("talking_and_communicating", True, EvidenceSource.EXPLICIT_SELECTION,
     "fictional-baseline-area-1", "fictional-level-1", True),
    ("social_and_emotional", True, EvidenceSource.CAREGIVER_REPORTED_MILESTONE,
     "fictional-baseline-area-2", "fictional-level-2", False),
    ("fine_motor", False, EvidenceSource.CAREGIVER_REPORTED_MILESTONE,
     "", "", False),
)


class SeedRefused(Exception):
    PHI_SAFE_MESSAGE = True


def _settings() -> PilotSettings:
    settings = PilotSettings.from_env(os.environ)
    if settings.environment.is_prod:
        raise SeedRefused("the fictional seed must never run against prod")
    return settings


def _child(repos, caregiver_id: str) -> Child:
    try:
        return repos.children.get_by_id(FICTIONAL_CHILD_ID)
    except RecordNotFound:
        pass
    child = Child(child_id=FICTIONAL_CHILD_ID,
                  created_by_actor_id=caregiver_id)
    return repos.children.create(child)


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 64
    caregiver_auth_subject = argv[1].strip()
    if not caregiver_auth_subject:
        print("the fictional caregiver's auth subject is required")
        return 64

    settings = _settings()
    repos = FirestoreRepositories(build_store(settings,
                                              process_env=os.environ))

    # Resolve the caregiver the SAME way a request does, so the seed cannot
    # create a child owned by an identity the server would not recognise.
    principal = resolve_principal(
        VerifiedToken(subject=caregiver_auth_subject), repos)
    if principal is None:
        raise SeedRefused(
            "that auth subject resolves to no caregiver; bootstrap it first")
    caregiver_id = principal.application_id
    print(f"caregiver : {caregiver_id}")

    child = _child(repos, caregiver_id)
    print(f"child     : {child.child_id}")

    # The ownership edge. Without it `authorize_child_access` denies, which is
    # correct — a Child row alone confers nothing.
    existing = repos.caregiver_child.list_children_for_caregiver(caregiver_id)
    if child.child_id not in set(existing):
        repos.caregiver_child.connect(CaregiverChildConnection.create(
            caregiver_id, child.child_id, CaregiverRelationship.PARENT,
            actor_id=caregiver_id))
        print("link      : created")
    else:
        print("link      : already present")

    # Developmental inputs -> suggestions, through the frozen engine.
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    goals = GoalService(repos=repos)
    already = goals.list_suggestions(principal, child.child_id)
    if already:
        print(f"suggestions: {len(already)} already present (no new ones)")
    else:
        snapshot = ObservationSnapshot(
            child_id=child.child_id,
            cycle_month=month,
            domains=tuple(
                ObservedDomain(
                    domain_key=key, answered=answered,
                    evidence_source=source,
                    functional_baseline_area=area,
                    observed_level=level,
                    explicitly_selected=selected)
                for key, answered, source, area, level, selected
                in FICTIONAL_DOMAINS),
        )
        created = goals.generate_suggestions(principal, child.child_id,
                                             snapshot)
        print(f"suggestions: {len(created)} generated for {month}")

    print("\nconnection : DELIBERATELY ABSENT — the Parent UI invites and the "
          "Therapist UI accepts")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
