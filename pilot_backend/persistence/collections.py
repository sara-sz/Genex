"""pilot_backend/persistence/collections.py — where each record type lives.

## The pilot store is not the Parent 2.3 store

Every collection name carries the `pilot_` prefix. That is not cosmetic: it is
the structural half of the §6 separation requirement. The Parent 2.3 session
store is a GCS bucket holding `sessions/{uid}/{session_id}.json` objects — a
different service, a different product (object storage, not documents), and a
protected historical baseline. There is no name under this prefix that could
resolve to it, and `pilot_backend` contains no GCS client, no bucket name and
no object path.

The configuration layer enforces the other half: `PilotSettings` refuses to
construct if any configured value names a Parent 2.3 resource, in every
environment.

Structured clinical persistence for the pilot is Firestore. GCS remains
available for later object/file/export/backup work, which is a separate store
with a separate decision — not something this module can reach by accident.
"""

from __future__ import annotations

from typing import Mapping

PILOT_COLLECTION_PREFIX = "pilot_"

#: Logical record type -> collection name. The only names this package writes.
COLLECTIONS: Mapping[str, str] = {
    "practice": "pilot_practices",
    "provider": "pilot_providers",
    "caregiver": "pilot_caregivers",
    "child": "pilot_children",
    "caregiver_child_connection": "pilot_caregiver_child_connections",
    "provider_child_connection": "pilot_provider_child_connections",
    "audit_event": "pilot_audit_events",
    "revision": "pilot_revisions",
    "child_context": "pilot_child_contexts",
    "source_system_link": "pilot_source_system_links",
    "managing_clinician": "pilot_managing_clinicians",
    "identity_claim": "pilot_identity_claims",
    # 0.4B goals. Suggestions, versions and the two approved-goal types are
    # four collections, not one with a discriminator field. A query that means
    # "every clinician-approved goal for this child" must not be able to return
    # a caregiver-approved one because a filter was omitted.
    "goal_suggestion": "pilot_goal_suggestions",
    "goal_version": "pilot_goal_versions",
    "clinical_goal": "pilot_clinical_goals",
    "caregiver_goal": "pilot_caregiver_goals",
    # 0.4C monthly focus plan.
    "monthly_focus_plan": "pilot_monthly_focus_plans",
    "monthly_goal_allocation": "pilot_monthly_goal_allocations",
    "monthly_goal_snapshot": "pilot_monthly_goal_snapshots",
    # 0.4D weekly layer. These are the MONTHLY LAYER's records of a week, not
    # the Parent weekly plan — and Parent's plan store is a GCS bucket with no
    # Firestore collection at all, so no name here could resolve to it.
    "weekly_cycle": "pilot_weekly_cycles",
    "weekly_plan_link": "pilot_weekly_plan_links",
    "weekly_plan_snapshot": "pilot_weekly_plan_snapshots",
    "activity_goal_alignment": "pilot_activity_goal_alignments",
    "coverage_gap": "pilot_coverage_gaps",
    "capacity_ledger": "pilot_capacity_ledgers",
    # 0.4E evidence and adaptation. Observations and plan customizations are
    # separate collections because they are separate kinds of fact: one is
    # about an attempt, the other about a schedule. A query meaning "what did
    # the child do?" must not be able to return a plan edit.
    "observation_event": "pilot_observation_events",
    "customization_signal": "pilot_customization_signals",
    "defer_record": "pilot_defer_records",
    "therapist_intervention": "pilot_therapist_interventions",
    "adaptation_record": "pilot_adaptation_records",
}

#: Names this package must never address. Parent 2.3 / Beta infrastructure.
FORBIDDEN_COLLECTION_TARGETS = frozenset({
    "genex-api-dev-sessions-genex-mvp-2026",
    "genex-api-prod-sessions-genex-mvp-2026",
    "sessions",
})


class CollectionError(ValueError):
    """An unknown or unsafe collection was requested."""

    PHI_SAFE_MESSAGE = True


def collection_for(record_type: str) -> str:
    """Resolve a record type to its collection, refusing anything unregistered.

    Deliberately a lookup and not string interpolation: a caller cannot compose
    a collection name, so no request value can ever influence which collection
    is addressed.
    """
    try:
        name = COLLECTIONS[record_type]
    except KeyError:
        raise CollectionError(f"unregistered record type: {record_type}")
    if not name.startswith(PILOT_COLLECTION_PREFIX):
        raise CollectionError(f"collection is outside the pilot namespace: {name}")
    if name in FORBIDDEN_COLLECTION_TARGETS:
        raise CollectionError("refusing to address a protected Parent 2.3 resource")
    return name


def audit_collection() -> str:
    return collection_for("audit_event")
