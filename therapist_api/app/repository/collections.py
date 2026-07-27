"""Canonical collection names (single source of truth for repository keys)."""

from __future__ import annotations

USER_PRINCIPALS = "user_principals"
THERAPIST_PROFILES = "therapist_profiles"
PARENT_PROFILES = "parent_profiles"
CHILDREN = "children"
CONNECTIONS = "connections"
MILESTONES = "milestones"
ACTIVITY_TEMPLATES = "activity_templates"
ACTIVITY_VERSIONS = "activity_versions"
WEEKLY_PLANS = "weekly_plans"
PLAN_ASSIGNMENTS = "plan_assignments"
PLAN_CHANGE_PROPOSALS = "plan_change_proposals"
PARENT_NOTES = "parent_notes"
PRIVATE_THERAPIST_NOTES = "private_therapist_notes"
AUDIT_EVENTS = "audit_events"
IDEMPOTENCY_RECORDS = "idempotency_records"

ALL = (
    USER_PRINCIPALS,
    THERAPIST_PROFILES,
    PARENT_PROFILES,
    CHILDREN,
    CONNECTIONS,
    MILESTONES,
    ACTIVITY_TEMPLATES,
    ACTIVITY_VERSIONS,
    WEEKLY_PLANS,
    PLAN_ASSIGNMENTS,
    PLAN_CHANGE_PROPOSALS,
    PARENT_NOTES,
    PRIVATE_THERAPIST_NOTES,
    AUDIT_EVENTS,
    IDEMPOTENCY_RECORDS,
)
