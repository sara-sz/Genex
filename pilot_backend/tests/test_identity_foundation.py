"""October pilot BACKEND 0.1 — identity foundation.

Covers the shared Parent/Therapist/RTM entity model: Practice, Provider,
Caregiver, Child, and the two relationship tables.

The properties worth the most here are the ones that are expensive to fix
later: IDs that cannot be derived from a name, a Child with no owner column,
and relationships that cannot be deleted. Each is a migration on live clinical
data if it is wrong, so each is tested directly rather than implied.

Fictional data only. No PHI.
"""

from __future__ import annotations

import ast
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest

from pilot_backend.domain import ids
from pilot_backend.domain.connections import (
    CaregiverChildConnection,
    ConnectionError_,
    ProviderChildConnection,
)
from pilot_backend.domain.entities import Caregiver, Child, Practice, Provider
from pilot_backend.domain.enums import (
    CaregiverRelationship,
    ConnectionStatus,
    EntityStatus,
    ProviderDiscipline,
    Visibility,
)
from pilot_backend.fixtures.pilot_topology import (
    CAREGIVER_AUTH_SUBJECT,
    CAREGIVER_NAME,
    PROVIDER_AUTH_SUBJECT,
    PROVIDER_NAME,
    build_pilot_topology,
)
from pilot_backend.repository import interface as repo_interface
from pilot_backend.repository.memory import InMemoryRepositories

PILOT_ROOT = pathlib.Path(__file__).resolve().parent.parent
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


# ===========================================================================
# A. stable IDs, not derived from name or email
# ===========================================================================

def test_ids_are_unique_across_repeated_creation():
    made = {Practice.create("Practice-Alpha").practice_id for _ in range(200)}
    assert len(made) == 200


@pytest.mark.parametrize("factory,prefix", [
    (ids.new_practice_id, "prac"), (ids.new_provider_id, "prov"),
    (ids.new_caregiver_id, "cgvr"), (ids.new_child_id, "chld"),
    (ids.new_caregiver_child_connection_id, "ccxn"),
    (ids.new_provider_child_connection_id, "pcxn"),
])
def test_id_shape_is_prefix_plus_uuid4_hex(factory, prefix):
    value = factory()
    assert re.fullmatch(rf"{prefix}_[0-9a-f]{{32}}", value), value


def test_id_is_not_derived_from_the_display_name():
    """Two identically-named records must not collide, and the name must not
    be recoverable from the id."""
    first = Caregiver.create(CAREGIVER_NAME)
    second = Caregiver.create(CAREGIVER_NAME)
    assert first.caregiver_id != second.caregiver_id
    for token in ("caregiver", "alpha", CAREGIVER_NAME.lower()):
        assert token not in first.caregiver_id.split("_", 1)[1].lower()


def test_id_is_not_derived_from_the_auth_subject_or_email():
    provider = Provider.create("prac_x", ProviderDiscipline.SLP, PROVIDER_NAME,
                               auth_subject="subject-with-email@example.test")
    tail = provider.provider_id.split("_", 1)[1]
    assert "example" not in tail and "subject" not in tail


def test_unknown_prefix_is_rejected():
    with pytest.raises(ValueError):
        ids.new_id("person")


# ===========================================================================
# B / C / D. the cardinalities October does not show but must not migrate for
# ===========================================================================

def test_case_b_one_child_supports_multiple_caregivers():
    repos, topo = build_pilot_topology(now=T0)
    second = repos.caregivers.create(Caregiver.create("Caregiver-Beta", now=T0))
    repos.caregiver_child.connect(
        CaregiverChildConnection.create(second.caregiver_id, topo.child.child_id,
                                        CaregiverRelationship.GUARDIAN, now=T0)
    )
    links = repos.caregiver_child.list_caregivers_for_child(topo.child.child_id)
    assert {l.caregiver_id for l in links} == {topo.caregiver.caregiver_id, second.caregiver_id}
    assert {l.relationship_role for l in links} == {
        CaregiverRelationship.PARENT, CaregiverRelationship.GUARDIAN}


def test_case_c_one_child_supports_multiple_providers():
    repos, topo = build_pilot_topology(now=T0)
    second = repos.providers.create(
        Provider.create(topo.practice.practice_id, ProviderDiscipline.OT,
                        "Provider-Beta", now=T0))
    link = repos.provider_child.connect(
        ProviderChildConnection.create(second.provider_id, topo.child.child_id,
                                       topo.practice.practice_id, now=T0))
    repos.provider_child.activate(link.connection_id, now=T0)
    links = repos.provider_child.list_providers_for_child(topo.child.child_id)
    assert {l.provider_id for l in links} == {topo.provider.provider_id, second.provider_id}


def test_case_d_one_provider_supports_multiple_children():
    repos, topo = build_pilot_topology(now=T0)
    for _ in range(3):
        child = repos.children.create(Child.create(now=T0))
        link = repos.provider_child.connect(
            ProviderChildConnection.create(topo.provider.provider_id, child.child_id,
                                           topo.practice.practice_id, now=T0))
        repos.provider_child.activate(link.connection_id, now=T0)
    caseload = repos.provider_child.list_children_for_provider(topo.provider.provider_id)
    assert len(caseload) == 4
    assert len({l.child_id for l in caseload}) == 4


def test_one_caregiver_supports_multiple_children():
    repos, topo = build_pilot_topology(now=T0)
    for _ in range(2):
        child = repos.children.create(Child.create(now=T0))
        repos.caregiver_child.connect(
            CaregiverChildConnection.create(topo.caregiver.caregiver_id, child.child_id,
                                            now=T0))
    assert len(repos.caregiver_child.list_children_for_caregiver(
        topo.caregiver.caregiver_id)) == 3


def test_child_has_no_owner_field():
    """The single-owner column is what makes multi-caregiver a migration."""
    fields = set(Child.create().__dataclass_fields__)
    for forbidden in ("caregiver_id", "parent_id", "parent_uid", "owner_id", "owner_uid"):
        assert forbidden not in fields, forbidden


def test_child_carries_no_phi():
    fields = set(Child.create().__dataclass_fields__)
    for phi in ("display_name", "name", "preferred_name", "date_of_birth", "dob",
                "diagnosis", "age_months", "notes"):
        assert phi not in fields, phi


# ===========================================================================
# E. ending a relationship preserves history
# ===========================================================================

def test_case_e_ending_a_caregiver_link_keeps_the_record():
    repos, topo = build_pilot_topology(now=T0)
    later = T0 + timedelta(days=30)
    ended = repos.caregiver_child.end_connection(topo.caregiver_link.connection_id, now=later)

    assert ended.status == ConnectionStatus.ENDED
    assert ended.ended_at == later
    assert not ended.is_active
    # Gone from the "who can see this child now" view...
    assert repos.caregiver_child.list_caregivers_for_child(topo.child.child_id) == []
    # ...but still provable in history.
    history = repos.caregiver_child.list_caregivers_for_child(topo.child.child_id,
                                                              include_ended=True)
    assert [c.connection_id for c in history] == [topo.caregiver_link.connection_id]
    assert repos.caregiver_child.get_by_id(topo.caregiver_link.connection_id) == ended


def test_case_e_ending_a_provider_link_keeps_the_record():
    repos, topo = build_pilot_topology(now=T0)
    later = T0 + timedelta(days=90)
    ended = repos.provider_child.end_connection(topo.provider_link.connection_id, now=later)
    assert ended.ended_at == later
    assert repos.provider_child.list_children_for_provider(topo.provider.provider_id) == []
    assert len(repos.provider_child.list_children_for_provider(
        topo.provider.provider_id, include_ended=True)) == 1
    # The activation that happened is still recorded.
    assert ended.activated_at is not None


def test_repositories_expose_no_delete():
    for protocol in (repo_interface.PracticeRepository, repo_interface.ProviderRepository,
                     repo_interface.CaregiverRepository, repo_interface.ChildRepository,
                     repo_interface.CaregiverChildConnectionRepository,
                     repo_interface.ProviderChildConnectionRepository):
        names = {n for n in dir(protocol) if not n.startswith("_")}
        assert not names & {"delete", "remove", "purge", "drop"}, protocol.__name__


def test_ended_connection_cannot_be_reactivated():
    repos, topo = build_pilot_topology(now=T0)
    repos.provider_child.end_connection(topo.provider_link.connection_id, now=T0)
    with pytest.raises(ConnectionError_):
        repos.provider_child.activate(topo.provider_link.connection_id, now=T0)


def test_end_rejects_a_non_terminal_status():
    link = CaregiverChildConnection.create("cgvr_x", "chld_x")
    with pytest.raises(ConnectionError_):
        link.end(status=ConnectionStatus.ACTIVE)


def test_entities_have_no_deleted_status():
    assert "DELETED" not in {s.name for s in EntityStatus}


# ===========================================================================
# F / H. application identity is independent of authentication
# ===========================================================================

def test_case_f_application_ids_are_independent_of_auth_subjects():
    repos, topo = build_pilot_topology(now=T0)
    assert topo.provider.provider_id != topo.provider.auth_subject
    assert topo.caregiver.caregiver_id != topo.caregiver.auth_subject
    assert PROVIDER_AUTH_SUBJECT not in topo.provider.provider_id
    assert CAREGIVER_AUTH_SUBJECT not in topo.caregiver.caregiver_id


def test_case_f_auth_subject_can_be_bound_later_without_changing_identity():
    """A record exists before anyone signs in; binding must not re-key it."""
    provider = Provider.create("prac_x", ProviderDiscipline.SLP, PROVIDER_NAME)
    assert provider.auth_subject is None
    bound = provider.with_auth_subject("subject-issued-later")
    assert bound.provider_id == provider.provider_id
    assert bound.auth_subject == "subject-issued-later"


def test_case_f_auth_subject_resolves_to_the_application_record():
    repos, topo = build_pilot_topology(now=T0)
    assert repos.providers.get_by_auth_subject(
        PROVIDER_AUTH_SUBJECT).provider_id == topo.provider.provider_id
    assert repos.caregivers.get_by_auth_subject(
        CAREGIVER_AUTH_SUBJECT).caregiver_id == topo.caregiver.caregiver_id
    assert repos.providers.get_by_auth_subject("nobody") is None
    assert repos.providers.get_by_auth_subject("") is None


def test_case_h_domain_layer_imports_no_auth_provider():
    """Structural: no Firebase/Identity Platform coupling in the domain."""
    banned = {"firebase_admin", "firebase", "google", "googleapiclient", "requests", "httpx"}
    for path in sorted((PILOT_ROOT / "domain").glob("*.py")) + \
                sorted((PILOT_ROOT / "repository").glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in banned, (path.name, alias.name)
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                assert node.module.split(".")[0] not in banned, (path.name, node.module)


def test_no_database_driver_imported():
    banned = {"firestore", "sqlalchemy", "psycopg2", "pymongo", "redis", "boto3"}
    for path in sorted(PILOT_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in banned, (path.name, alias.name)


# ===========================================================================
# G. privacy boundary is structural
# ===========================================================================

def test_case_g_every_entity_declares_a_visibility_tier():
    for cls in (Practice, Provider, Caregiver, Child,
                CaregiverChildConnection, ProviderChildConnection):
        assert isinstance(cls.VISIBILITY, Visibility), cls.__name__


def test_case_g_child_and_connections_are_not_parent_visible_payloads():
    """Identity and relationship rows are audit-tier, not content returned to a family."""
    assert Child.VISIBILITY is Visibility.SYSTEM_AUDIT
    assert CaregiverChildConnection.VISIBILITY is Visibility.SYSTEM_AUDIT
    assert ProviderChildConnection.VISIBILITY is Visibility.SYSTEM_AUDIT


def test_case_g_therapist_only_tier_exists_and_is_distinct():
    tiers = {v.value for v in Visibility}
    assert tiers == {"parent_visible", "therapist_only", "system_audit"}
    assert Visibility.THERAPIST_ONLY is not Visibility.PARENT_VISIBLE


def test_case_g_no_pilot_entity_mixes_parent_and_therapist_content():
    """0.1 carries identity only — no clinical content, so nothing to leak yet.

    The tier vocabulary is in place so that when notes and RTM time arrive they
    are declared, not inferred. The therapist service already models
    ParentNote and PrivateTherapistNote as separate records; this keeps that.
    """
    for cls in (Practice, Provider, Caregiver, Child,
                CaregiverChildConnection, ProviderChildConnection):
        fields = set(cls.__dataclass_fields__)
        for clinical in ("body", "note", "private_note", "clinical_interpretation",
                         "billing_minutes", "rtm_minutes"):
            assert clinical not in fields, (cls.__name__, clinical)


# ===========================================================================
# I. deterministic, isolated repository
# ===========================================================================

def test_case_i_repositories_are_isolated_between_instances():
    repos_a, topo_a = build_pilot_topology(now=T0)
    repos_b, _ = build_pilot_topology(now=T0)
    with pytest.raises(repo_interface.RecordNotFound):
        repos_b.children.get_by_id(topo_a.child.child_id)


def test_case_i_listings_are_deterministically_ordered():
    repos, topo = build_pilot_topology(now=T0)
    for index in range(5):
        child = repos.children.create(Child.create(now=T0 + timedelta(minutes=index)))
        repos.caregiver_child.connect(
            CaregiverChildConnection.create(topo.caregiver.caregiver_id, child.child_id,
                                            now=T0 + timedelta(minutes=index)))
    listings = [repos.caregiver_child.list_children_for_caregiver(topo.caregiver.caregiver_id)
                for _ in range(5)]
    ids = [[c.connection_id for c in listing] for listing in listings]
    assert all(run == ids[0] for run in ids), "repeated listings disagreed"

    # Stronger than repeat-stability: the order must be the DECLARED one,
    # (created_at, own id). Repeat-stability alone would pass on insertion
    # order, which is what the `_id_of` foreign-key bug was hiding behind —
    # the fixture connection and the first loop connection share a timestamp,
    # so this is the tie that must resolve on connection_id.
    expected = [c.connection_id for c in
                sorted(listings[0], key=lambda c: (c.created_at, c.connection_id))]
    assert ids[0] == expected, "listing order is not (created_at, connection_id)"

    timestamps = [c.created_at for c in listings[0]]
    assert len(set(timestamps)) < len(timestamps), "fixture no longer exercises a tie"


def test_case_i_duplicate_create_is_rejected():
    repos, topo = build_pilot_topology(now=T0)
    with pytest.raises(repo_interface.DuplicateRecord):
        repos.children.create(topo.child)


def test_case_i_missing_record_raises():
    repos = InMemoryRepositories()
    with pytest.raises(repo_interface.RecordNotFound):
        repos.providers.get_by_id("prov_missing")


def test_case_i_memory_repositories_satisfy_the_protocols():
    repos = InMemoryRepositories()
    assert isinstance(repos.practices, repo_interface.PracticeRepository)
    assert isinstance(repos.providers, repo_interface.ProviderRepository)
    assert isinstance(repos.caregivers, repo_interface.CaregiverRepository)
    assert isinstance(repos.children, repo_interface.ChildRepository)
    assert isinstance(repos.caregiver_child,
                      repo_interface.CaregiverChildConnectionRepository)
    assert isinstance(repos.provider_child,
                      repo_interface.ProviderChildConnectionRepository)


def test_stored_records_are_immutable():
    repos, topo = build_pilot_topology(now=T0)
    with pytest.raises(Exception):
        topo.child.status = EntityStatus.ARCHIVED


def test_status_update_does_not_change_identity():
    repos, topo = build_pilot_topology(now=T0)
    archived = repos.children.update_status(topo.child.child_id, EntityStatus.ARCHIVED,
                                            now=T0 + timedelta(days=1))
    assert archived.child_id == topo.child.child_id
    assert archived.created_at == topo.child.created_at
    assert archived.updated_at > topo.child.updated_at


# ===========================================================================
# J. the fictional October topology, end to end
# ===========================================================================

def test_case_j_pilot_topology_builds_and_queries():
    repos, topo = build_pilot_topology(now=T0)

    assert repos.practices.get_by_id(topo.practice.practice_id).legal_name == "Practice-Alpha"
    assert repos.providers.get_by_id(topo.provider.provider_id).discipline is \
        ProviderDiscipline.SLP
    assert repos.providers.list_by_practice(topo.practice.practice_id) == [topo.provider]

    # caregiver -> child
    links = repos.caregiver_child.list_children_for_caregiver(topo.caregiver.caregiver_id)
    assert [l.child_id for l in links] == [topo.child.child_id]

    # provider -> child, active after the invite was accepted
    caseload = repos.provider_child.list_children_for_provider(topo.provider.provider_id)
    assert [l.child_id for l in caseload] == [topo.child.child_id]
    assert caseload[0].is_active
    assert caseload[0].practice_id == topo.practice.practice_id


def test_case_j_provider_connection_starts_pending():
    repos, topo = build_pilot_topology(now=T0, activate_provider=False)
    assert topo.provider_link.status is ConnectionStatus.PENDING
    assert not topo.provider_link.is_active
    assert repos.provider_child.list_children_for_provider(topo.provider.provider_id) == []


def test_case_j_fixture_uses_neutral_aliases_only():
    """No real-person-associated name may appear in the pilot fixture."""
    source = (PILOT_ROOT / "fixtures" / "pilot_topology.py").read_text().lower()
    for name in ("sara", "hannah", "maya", "elena", "priya", "omar", "rosa",
                 "devika", "tamsin", "noah", "amara", "theo"):
        assert name not in source, name
    assert "practice-alpha" in source and "provider-alpha" in source
    assert "caregiver-alpha" in source


def test_case_j_fixture_contains_no_contact_details():
    source = (PILOT_ROOT / "fixtures" / "pilot_topology.py").read_text()
    assert "@" not in source.replace("@dataclass", ""), "possible email address in fixture"


# ===========================================================================
# RTM forward compatibility (shape only — no RTM code in this phase)
# ===========================================================================

def test_rtm_episode_could_reference_every_required_id():
    """A future RTMEpisode needs child, provider, practice and caregiver(s)."""
    repos, topo = build_pilot_topology(now=T0)
    episode_keys = {
        "child_id": topo.child.child_id,
        "provider_id": topo.provider.provider_id,
        "practice_id": topo.provider_link.practice_id,
        "caregiver_ids": [c.caregiver_id for c in
                          repos.caregiver_child.list_caregivers_for_child(topo.child.child_id)],
    }
    assert all(episode_keys.values())
    assert episode_keys["practice_id"] == topo.practice.practice_id
    assert episode_keys["caregiver_ids"] == [topo.caregiver.caregiver_id]


def test_no_rtm_implementation_exists_yet():
    for path in sorted(PILOT_ROOT.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text())
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        assert not {"RTMEpisode", "RTMTimeEntry", "MonitoringEvent"} & names, path.name


# ===========================================================================
# audit metadata
# ===========================================================================

@pytest.mark.parametrize("cls", [Practice, Provider, Caregiver, Child,
                                 CaregiverChildConnection, ProviderChildConnection])
def test_every_record_carries_audit_timestamps(cls):
    fields = set(cls.__dataclass_fields__)
    assert {"created_at", "updated_at", "created_by_actor_id", "schema_version"} <= fields


def test_timestamps_are_timezone_aware():
    practice = Practice.create("Practice-Alpha")
    assert practice.created_at.tzinfo is not None


def test_connections_record_who_created_them():
    _, topo = build_pilot_topology(now=T0)
    assert topo.caregiver_link.created_by_actor_id == topo.caregiver.caregiver_id
    assert topo.provider_link.created_by_actor_id == topo.provider.provider_id
