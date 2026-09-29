"""pilot_backend/fixtures/secure_topology.py — a topology with negative cases in it.

The BACKEND 0.1 fixture builds the happy path: one caregiver, one child, one
SLP, all connected. Security behaviour cannot be proven against that alone —
every check would pass, and a function that returned True unconditionally would
look correct.

This adds the shapes access control has to refuse:

    Family Alpha                        Family Beta
      Caregiver-Alpha ─ ACTIVE ─┐         Caregiver-Beta ─ ACTIVE ─┐
      Provider-Alpha  ─ ACTIVE ─┼ Child-Alpha   Provider-Beta ─ ACTIVE ─┼ Child-Beta
      Caregiver-Gamma ─ ENDED  ─┤
      Provider-Gamma  ─ PENDING ┘

  * two unrelated families, so "parent A cannot reach child B" is a real
    query against real rows rather than an empty-store accident;
  * Caregiver-Gamma, who genuinely HAD access and no longer does — the ended
    relationship is the case a naive `if connection exists` check passes;
  * Provider-Gamma, invited but never activated — the pending case, which is
    the same bug in the other direction.

FICTIONAL ONLY. Neutral aliases; no real-person-associated name appears. `Child`
still carries no name, date of birth or clinical detail — there is no PHI here
to leak, and the tests additionally use explicit sentinel strings to prove that
nothing which WOULD be PHI reaches logs or audit records.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..domain.connections import CaregiverChildConnection, ProviderChildConnection
from ..domain.entities import Caregiver, Child, Practice, Provider
from ..domain.enums import CaregiverRelationship, ConnectionStatus, ProviderDiscipline

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

CAREGIVER_ALPHA_SUBJECT = "fictional-subject-caregiver-alpha"
CAREGIVER_BETA_SUBJECT = "fictional-subject-caregiver-beta"
CAREGIVER_GAMMA_SUBJECT = "fictional-subject-caregiver-gamma"
PROVIDER_ALPHA_SUBJECT = "fictional-subject-provider-alpha"
PROVIDER_BETA_SUBJECT = "fictional-subject-provider-beta"
PROVIDER_GAMMA_SUBJECT = "fictional-subject-provider-gamma"

#: A subject that verifies successfully but maps to no application record.
UNPROVISIONED_SUBJECT = "fictional-subject-unprovisioned"


@dataclass(frozen=True)
class SecureTopology:
    practice: Practice
    provider_alpha: Provider
    provider_beta: Provider
    provider_gamma: Provider
    caregiver_alpha: Caregiver
    caregiver_beta: Caregiver
    caregiver_gamma: Caregiver
    child_alpha: Child
    child_beta: Child
    link_alpha_caregiver: CaregiverChildConnection
    link_beta_caregiver: CaregiverChildConnection
    link_gamma_caregiver_ended: CaregiverChildConnection
    link_alpha_provider: ProviderChildConnection
    link_beta_provider: ProviderChildConnection
    link_gamma_provider_pending: ProviderChildConnection


def build_secure_topology(repos, *, now: Optional[datetime] = None,
                          subject_suffix: str = "") -> SecureTopology:
    """Build the two-family topology into ANY repository set.

    `repos` is duck-typed: `InMemoryRepositories` and `FirestoreRepositories`
    both satisfy the BACKEND 0.1 protocols, so the identical fixture — and
    therefore the identical security test — runs against both backends. That
    is the practical proof that the domain layer is storage-agnostic.

    `subject_suffix` makes the auth subjects unique per call. It exists for
    the Firestore emulator suite, where the database is deliberately NOT reset
    between tests: two topologies in one store would otherwise bind two
    caregiver records to one auth subject, which is a data-integrity fault the
    resolver now refuses outright. Defaults to empty, so single-topology
    callers see the documented subject strings unchanged.
    """
    stamp = now or T0
    suffix = subject_suffix

    practice = repos.practices.create(Practice.create("Practice-Alpha", now=stamp))

    def _provider(name: str, subject: str) -> Provider:
        return repos.providers.create(Provider.create(
            practice.practice_id, ProviderDiscipline.SLP, name,
            auth_subject=subject, now=stamp))

    def _caregiver(name: str, subject: str) -> Caregiver:
        return repos.caregivers.create(Caregiver.create(
            name, auth_subject=subject, now=stamp))

    provider_alpha = _provider("Provider-Alpha", PROVIDER_ALPHA_SUBJECT + suffix)
    provider_beta = _provider("Provider-Beta", PROVIDER_BETA_SUBJECT + suffix)
    provider_gamma = _provider("Provider-Gamma", PROVIDER_GAMMA_SUBJECT + suffix)

    caregiver_alpha = _caregiver("Caregiver-Alpha", CAREGIVER_ALPHA_SUBJECT + suffix)
    caregiver_beta = _caregiver("Caregiver-Beta", CAREGIVER_BETA_SUBJECT + suffix)
    caregiver_gamma = _caregiver("Caregiver-Gamma", CAREGIVER_GAMMA_SUBJECT + suffix)

    child_alpha = repos.children.create(
        Child.create(actor_id=caregiver_alpha.caregiver_id, now=stamp))
    child_beta = repos.children.create(
        Child.create(actor_id=caregiver_beta.caregiver_id, now=stamp))

    def _caregiver_link(caregiver: Caregiver, child: Child) -> CaregiverChildConnection:
        return repos.caregiver_child.connect(CaregiverChildConnection.create(
            caregiver.caregiver_id, child.child_id, CaregiverRelationship.PARENT,
            actor_id=caregiver.caregiver_id, now=stamp))

    link_alpha_caregiver = _caregiver_link(caregiver_alpha, child_alpha)
    link_beta_caregiver = _caregiver_link(caregiver_beta, child_beta)

    # Gamma had access to Child-Alpha and lost it. The row remains.
    gamma_link = _caregiver_link(caregiver_gamma, child_alpha)
    link_gamma_caregiver_ended = repos.caregiver_child.end_connection(
        gamma_link.connection_id, status=ConnectionStatus.REVOKED, now=stamp)

    def _provider_link(provider: Provider, child: Child,
                       activate: bool) -> ProviderChildConnection:
        link = repos.provider_child.connect(ProviderChildConnection.create(
            provider.provider_id, child.child_id, practice.practice_id,
            actor_id=provider.provider_id, now=stamp))
        if activate:
            link = repos.provider_child.activate(link.connection_id, now=stamp)
        return link

    link_alpha_provider = _provider_link(provider_alpha, child_alpha, True)
    link_beta_provider = _provider_link(provider_beta, child_beta, True)
    # Invited to Child-Alpha, never accepted. Still PENDING.
    link_gamma_provider_pending = _provider_link(provider_gamma, child_alpha, False)

    return SecureTopology(
        practice=practice,
        provider_alpha=provider_alpha,
        provider_beta=provider_beta,
        provider_gamma=provider_gamma,
        caregiver_alpha=caregiver_alpha,
        caregiver_beta=caregiver_beta,
        caregiver_gamma=caregiver_gamma,
        child_alpha=child_alpha,
        child_beta=child_beta,
        link_alpha_caregiver=link_alpha_caregiver,
        link_beta_caregiver=link_beta_caregiver,
        link_gamma_caregiver_ended=link_gamma_caregiver_ended,
        link_alpha_provider=link_alpha_provider,
        link_beta_provider=link_beta_provider,
        link_gamma_provider_pending=link_gamma_provider_pending,
    )
