"""pilot_backend/fixtures/pilot_topology.py — the fictional October topology.

    Practice-Alpha
      └── Provider-Alpha  (SLP)
              └── ProviderChildConnection ──┐
                                            ├── Child-Alpha
    Caregiver-Alpha ── CaregiverChildConnection ──┘

FICTIONAL ONLY. Neutral aliases, chosen so no fixture name can be mistaken for
a real person: no clinician, family member or child associated with the project
appears here. There is no PHI — `Child` carries no name, date of birth or
clinical detail at all, only an identifier.

This is the exact shape October ships (one caregiver, one child, one SLP), but
it is built entirely out of relationship rows, so the multi-caregiver and
multi-provider cases are additional rows rather than a schema change. The tests
add those extra rows to the same topology to prove it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..domain.connections import CaregiverChildConnection, ProviderChildConnection
from ..domain.entities import Caregiver, Child, Practice, Provider
from ..domain.enums import CaregiverRelationship, ProviderDiscipline
from ..repository.memory import InMemoryRepositories

PRACTICE_NAME = "Practice-Alpha"
PROVIDER_NAME = "Provider-Alpha"
CAREGIVER_NAME = "Caregiver-Alpha"

#: Placeholder auth subjects. Deliberately NOT Firebase-shaped and obviously
#: fake, so nothing can mistake them for issued credentials.
PROVIDER_AUTH_SUBJECT = "fictional-subject-provider-alpha"
CAREGIVER_AUTH_SUBJECT = "fictional-subject-caregiver-alpha"


@dataclass(frozen=True)
class PilotTopology:
    """Everything the October pilot needs, by application id."""

    practice: Practice
    provider: Provider
    caregiver: Caregiver
    child: Child
    caregiver_link: CaregiverChildConnection
    provider_link: ProviderChildConnection


def build_pilot_topology(
    repos: Optional[InMemoryRepositories] = None,
    *,
    now: Optional[datetime] = None,
    activate_provider: bool = True,
) -> tuple[InMemoryRepositories, PilotTopology]:
    """Create and persist the fictional topology.

    The provider connection is created PENDING and then activated, mirroring
    the real invite flow rather than fabricating an already-connected
    clinician.
    """
    repos = repos or InMemoryRepositories()

    practice = repos.practices.create(Practice.create(PRACTICE_NAME, now=now))
    provider = repos.providers.create(
        Provider.create(
            practice.practice_id,
            ProviderDiscipline.SLP,
            PROVIDER_NAME,
            auth_subject=PROVIDER_AUTH_SUBJECT,
            now=now,
        )
    )
    caregiver = repos.caregivers.create(
        Caregiver.create(CAREGIVER_NAME, auth_subject=CAREGIVER_AUTH_SUBJECT, now=now)
    )
    child = repos.children.create(Child.create(actor_id=caregiver.caregiver_id, now=now))

    caregiver_link = repos.caregiver_child.connect(
        CaregiverChildConnection.create(
            caregiver.caregiver_id,
            child.child_id,
            CaregiverRelationship.PARENT,
            actor_id=caregiver.caregiver_id,
            now=now,
        )
    )

    provider_link = repos.provider_child.connect(
        ProviderChildConnection.create(
            provider.provider_id,
            child.child_id,
            practice.practice_id,
            actor_id=provider.provider_id,
            now=now,
        )
    )
    if activate_provider:
        provider_link = repos.provider_child.activate(provider_link.connection_id, now=now)

    return repos, PilotTopology(
        practice=practice,
        provider=provider,
        caregiver=caregiver,
        child=child,
        caregiver_link=caregiver_link,
        provider_link=provider_link,
    )
