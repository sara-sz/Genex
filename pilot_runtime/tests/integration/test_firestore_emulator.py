"""Real Firestore adapter against a real emulator.

Every test here drives `FirestoreDocumentStore` over the official client. No
fake store appears in this file. What is being proven is not the domain logic
— BACKEND 0.2 proved that against the port — but that the real adapter honours
the same contract the port promised, which is precisely the assumption a fake
cannot test.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pilot_backend.domain.child_context import ChildContextRecord
from pilot_backend.domain.entities import Caregiver, Child, Practice, Provider
from pilot_backend.domain.enums import (
    CaregiverRelationship,
    ConnectionStatus,
    EntityStatus,
    ProviderDiscipline,
)
from pilot_backend.domain.roles import ActorRole
from pilot_backend.audit.events import AuditAction, AuditEvent, AuditResult
from pilot_backend.persistence import decode, encode
from pilot_backend.persistence.codecs import CodecError
from pilot_backend.persistence.document_store import DocumentStoreError
from pilot_backend.repository.interface import DuplicateRecord, RecordNotFound
from pilot_backend.revision.records import (
    ImmutableRecordError,
    RecordState,
    amend,
    finalize,
    start_draft,
)
from pilot_backend.fixtures.secure_topology import build_secure_topology

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


# ===========================================================================
# the fixture really is an emulator
# ===========================================================================

def test_the_suite_is_running_against_a_real_emulator(emulator_host, firestore_client):
    """Guard against this suite silently degrading into a fake-backed run.

    Asserted on the CLIENT, not on the process environment. The SDK records
    the emulator endpoint it was built with, so this proves the target without
    anything needing to set a global variable — which is the variable the
    composition root refuses to start with.
    """
    import os

    from google.cloud import firestore

    assert isinstance(firestore_client, firestore.Client)
    assert firestore_client._emulator_host == emulator_host
    assert firestore_client.project.startswith("demo-"), "must be an offline demo project"
    assert "FIRESTORE_EMULATOR_HOST" not in os.environ, (
        "the harness must not leak a process-global emulator endpoint")


# ===========================================================================
# entity round-trips through the real adapter
# ===========================================================================

def test_practice_round_trip(repos):
    practice = repos.practices.create(Practice.create("Practice-Alpha", now=T0))
    assert repos.practices.get_by_id(practice.practice_id) == practice


def test_provider_round_trip(repos):
    practice = repos.practices.create(Practice.create("Practice-Alpha", now=T0))
    provider = repos.providers.create(Provider.create(
        practice.practice_id, ProviderDiscipline.SLP, "Provider-Alpha",
        auth_subject="fictional-subject-provider-alpha", now=T0))
    assert repos.providers.get_by_id(provider.provider_id) == provider
    assert repos.providers.list_by_practice(practice.practice_id) == [provider]


def test_caregiver_round_trip(repos):
    caregiver = repos.caregivers.create(Caregiver.create(
        "Caregiver-Alpha", auth_subject="fictional-subject-caregiver-alpha", now=T0))
    assert repos.caregivers.get_by_id(caregiver.caregiver_id) == caregiver


def test_child_round_trip(repos):
    child = repos.children.create(Child.create(actor_id="cgvr_fictional", now=T0))
    assert repos.children.get_by_id(child.child_id) == child


def test_caregiver_child_connection_round_trip(repos, topology):
    topo = topology
    stored = repos.caregiver_child.get_by_id(topo.link_alpha_caregiver.connection_id)
    assert stored == topo.link_alpha_caregiver
    assert stored.relationship_role is CaregiverRelationship.PARENT


def test_provider_child_connection_round_trip(repos, topology):
    topo = topology
    stored = repos.provider_child.get_by_id(topo.link_alpha_provider.connection_id)
    assert stored == topo.link_alpha_provider
    assert stored.practice_id == topo.practice.practice_id


# ===========================================================================
# auth-subject lookup and stable ids
# ===========================================================================

def test_auth_subject_lookup_works_as_a_real_firestore_query(repos, topology,
                                                             unique_suffix):
    found = repos.caregivers.get_by_auth_subject(
        "fictional-subject-caregiver-alpha" + unique_suffix)
    assert found is not None
    assert found.caregiver_id == topology.caregiver_alpha.caregiver_id
    assert repos.caregivers.get_by_auth_subject("nobody-fictional") is None
    assert repos.providers.get_by_auth_subject("") is None


def test_two_records_sharing_an_auth_subject_fail_closed(repos, unique_suffix):
    """Regression: the emulator surfaced this because it does not reset.

    Two caregiver records bound to one subject used to resolve silently to
    whichever sorted first, making identity a function of document ordering.
    """
    from pilot_backend.repository.interface import AmbiguousAuthSubject

    subject = "fictional-subject-duplicated" + unique_suffix
    repos.caregivers.create(Caregiver.create("Caregiver-Alpha",
                                             auth_subject=subject, now=T0))
    repos.caregivers.create(Caregiver.create("Caregiver-Beta",
                                             auth_subject=subject, now=T0))
    with pytest.raises(AmbiguousAuthSubject):
        repos.caregivers.get_by_auth_subject(subject)


def test_application_ids_are_stable_across_writes_and_reads(repos, topology):
    topo = topology
    original = topo.child_alpha.child_id
    repos.children.update_status(original, EntityStatus.INACTIVE)
    assert repos.children.get_by_id(original).child_id == original
    assert original.startswith("chld_")


# ===========================================================================
# relationship lifecycle
# ===========================================================================

def test_active_inactive_and_ended_relationships_persist_correctly(repos, topology):
    topo = topology
    child = topo.child_alpha.child_id

    active = repos.caregiver_child.list_caregivers_for_child(child)
    assert [c.caregiver_id for c in active] == [topo.caregiver_alpha.caregiver_id]

    everything = repos.caregiver_child.list_caregivers_for_child(child, include_ended=True)
    assert len(everything) == 2, "the revoked Gamma row must survive in the store"
    revoked = [c for c in everything if not c.is_active]
    assert len(revoked) == 1
    assert revoked[0].status is ConnectionStatus.REVOKED
    assert revoked[0].ended_at is not None

    pending = repos.provider_child.list_providers_for_child(child, include_ended=True)
    assert any(p.status is ConnectionStatus.PENDING for p in pending)
    assert [p.provider_id for p in
            repos.provider_child.list_providers_for_child(child)] == [
        topo.provider_alpha.provider_id]


def test_ending_a_connection_persists_and_never_deletes(repos, topology):
    topo = topology
    connection_id = topo.link_alpha_provider.connection_id
    repos.provider_child.end_connection(connection_id, status=ConnectionStatus.ENDED)

    stored = repos.provider_child.get_by_id(connection_id)
    assert stored.status is ConnectionStatus.ENDED
    assert stored.ended_at is not None
    assert not stored.is_active
    # The row is still there — the document was updated, not removed.
    assert connection_id in [
        c.connection_id for c in
        repos.provider_child.list_providers_for_child(topo.child_alpha.child_id,
                                                      include_ended=True)]


# ===========================================================================
# ordering assumptions the authorization path depends on
# ===========================================================================

def test_listings_are_deterministically_ordered_in_real_firestore(repos, topology):
    """Firestore does not promise an order; the adapter must impose one."""
    topo = topology
    child = topo.child_alpha.child_id
    seen = [
        [c.connection_id for c in
         repos.caregiver_child.list_caregivers_for_child(child, include_ended=True)]
        for _ in range(6)
    ]
    assert all(order == seen[0] for order in seen), "order varied between reads"
    assert seen[0] == sorted(seen[0]), "adapter must order by document id"


def test_equality_queries_do_not_leak_across_children(repos, topology):
    topo = topology
    alpha = repos.caregiver_child.list_caregivers_for_child(
        topo.child_alpha.child_id, include_ended=True)
    beta = repos.caregiver_child.list_caregivers_for_child(
        topo.child_beta.child_id, include_ended=True)
    assert {c.child_id for c in alpha} == {topo.child_alpha.child_id}
    assert {c.child_id for c in beta} == {topo.child_beta.child_id}


# ===========================================================================
# timestamps
# ===========================================================================

def test_timestamps_round_trip_as_aware_utc_through_firestore(repos):
    """The documented strategy: application-generated, stored as ISO strings."""
    offset_zone = timezone(timedelta(hours=5, minutes=30))
    stamped = datetime(2026, 10, 1, 9, 15, 0, tzinfo=offset_zone)
    practice = repos.practices.create(Practice.create("Practice-Alpha", now=stamped))

    stored = repos.practices.get_by_id(practice.practice_id)
    assert stored.created_at.tzinfo is not None
    assert stored.created_at == stamped
    assert stored.created_at.utcoffset() == timedelta(0), "normalised to UTC"


def test_no_firestore_server_timestamp_sentinel_is_written(repos, firestore_client):
    """Raw document inspection: timestamps are ISO strings, not Firestore types."""
    practice = repos.practices.create(Practice.create("Practice-Alpha", now=T0))
    raw = (firestore_client.collection("pilot_practices")
           .document(practice.practice_id).get().to_dict())
    assert isinstance(raw["created_at"], str)
    assert raw["created_at"].startswith("2026-10-01T12:00:00")


# ===========================================================================
# store semantics
# ===========================================================================

def test_duplicate_create_is_refused_by_real_firestore(repos):
    practice = Practice.create("Practice-Alpha", now=T0)
    repos.practices.create(practice)
    with pytest.raises(DuplicateRecord):
        repos.practices.create(practice)


def test_missing_document_raises_record_not_found(repos):
    with pytest.raises(RecordNotFound):
        repos.children.get_by_id("chld_" + "0" * 32)


def test_set_refuses_to_create_a_missing_document(store):
    """Firestore's set() is an upsert; the port's set() must not be."""
    with pytest.raises(DocumentStoreError):
        store.set("pilot_practices", "prac_" + "0" * 32, {"legal_name": "ghost"})


def test_malformed_stored_record_fails_closed(repos, firestore_client):
    """A hand-edited or partially-migrated document must not decode."""
    practice = repos.practices.create(Practice.create("Practice-Alpha", now=T0))
    reference = firestore_client.collection("pilot_practices").document(
        practice.practice_id)

    corrupted = reference.get().to_dict()
    corrupted.pop("legal_name")
    reference.set(corrupted)
    with pytest.raises(CodecError):
        repos.practices.get_by_id(practice.practice_id)

    corrupted["legal_name"] = "Practice-Alpha"
    corrupted["unexpected_column"] = "x"
    reference.set(corrupted)
    with pytest.raises(CodecError):
        repos.practices.get_by_id(practice.practice_id)


def test_unrecognised_enum_in_storage_fails_closed(repos, firestore_client, topology):
    """A status this code does not understand must not read as one it does."""
    topo = topology
    reference = firestore_client.collection("pilot_caregiver_child_connections").document(
        topo.link_alpha_caregiver.connection_id)
    document = reference.get().to_dict()
    document["status"] = "super_active"
    reference.set(document)
    with pytest.raises(CodecError):
        repos.caregiver_child.get_by_id(topo.link_alpha_caregiver.connection_id)


# ===========================================================================
# audit and revisions through the real adapter
# ===========================================================================

def test_audit_events_persist_append_only_in_firestore(repos):
    event = AuditEvent.build(
        AuditAction.CHILD_ACCESS_GRANTED, AuditResult.SUCCESS, "child",
        resource_id="chld_fictional", child_id="chld_fictional",
        actor_application_id="cgvr_fictional", actor_role=ActorRole.CAREGIVER,
        request_id="req-fictional-1", metadata={"http_status": 200}, now=T0)
    repos.audit_events.append(event)

    stored = repos.audit_events.get_by_id(event.event_id)
    assert stored == event
    assert stored.occurred_at.tzinfo is not None
    with pytest.raises(DuplicateRecord):
        repos.audit_events.append(event)
    assert not hasattr(repos.audit_events, "update")
    assert not hasattr(repos.audit_events, "delete")


def test_revision_chain_persists_with_history_retained(repos):
    draft = start_draft("rec-fictional-1", actor_application_id="prov_fictional",
                        actor_role=ActorRole.PROVIDER, content_ref="ctx-blob-1", now=T0)
    repos.revisions.append(draft)
    sealed = finalize(draft, now=T0)
    repos.revisions.seal(sealed)
    second = amend(sealed, actor_application_id="prov_fictional",
                   actor_role=ActorRole.PROVIDER, reason="corrected session date",
                   content_ref="ctx-blob-2", now=T0)
    repos.revisions.append(second)

    chain = repos.revisions.list_chain("rec-fictional-1")
    assert [r.version for r in chain] == [1, 2]
    assert chain[0].state is RecordState.FINALIZED
    assert chain[0].finalized_at is not None
    assert chain[1].supersedes_revision_id == chain[0].revision_id
    assert chain[1].amendment_reason == "corrected session date"
    assert chain[1].actor_application_id == "prov_fictional"
    assert chain[1].created_at.tzinfo is not None
    # v1 survives amendment, with its own content pointer intact.
    assert chain[0].content_ref == "ctx-blob-1"


def test_a_finalized_revision_cannot_be_resealed_in_storage(repos):
    draft = start_draft("rec-fictional-2", actor_application_id="cgvr_fictional",
                        actor_role=ActorRole.CAREGIVER, now=T0)
    repos.revisions.append(draft)
    repos.revisions.seal(finalize(draft, now=T0))
    with pytest.raises(ImmutableRecordError):
        repos.revisions.seal(finalize(draft, now=T0))


def test_child_context_record_round_trips(repos):
    record = ChildContextRecord.create("chld_fictional", actor_id="cgvr_fictional",
                                       actor_role=ActorRole.CAREGIVER, now=T0)
    repos.child_contexts.create(record)
    assert repos.child_contexts.get_by_id(record.record_id) == record

    moved = record.with_current_revision("revn_fictional", 1, now=T0)
    repos.child_contexts.update(moved)
    assert repos.child_contexts.get_by_id(record.record_id).current_version == 1
    assert repos.child_contexts.list_for_child("chld_fictional") == [moved]


# ===========================================================================
# isolation from Parent 2.3
# ===========================================================================

def test_only_pilot_prefixed_collections_are_ever_created(repos, firestore_client, topology):
    names = sorted(c.id for c in firestore_client.collections())
    assert names, "the topology wrote nothing"
    for name in names:
        assert name.startswith("pilot_"), name
    for forbidden in ("sessions", "genex-api-dev-sessions-genex-mvp-2026"):
        assert forbidden not in names


@pytest.mark.parametrize("collection", [
    "sessions", "genex-api-dev-sessions-genex-mvp-2026", "users", "",
])
def test_the_adapter_refuses_non_pilot_collections(store, collection):
    with pytest.raises(DocumentStoreError):
        store.create(collection, "doc-1", {"a": "1"})
    with pytest.raises(DocumentStoreError):
        store.get(collection, "doc-1")


def test_the_adapter_exposes_no_destructive_operation(store):
    banned = ("delete", "remove", "purge", "drop", "destroy", "erase", "truncate")
    for attribute in dir(store):
        if attribute.startswith("_"):
            continue
        assert not any(word in attribute.lower() for word in banned), attribute
