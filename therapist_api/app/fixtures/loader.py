"""Seed the fictional fixtures into a repository (dev/test only).

Route handlers never touch fixture dicts directly — they read through the
service/repository layers. This loader is the ONLY place fixtures enter the
repository.
"""

from __future__ import annotations

from ..repository import collections as C
from ..repository.interface import CollaborationRepository
from . import data


def _seed(repo: CollaborationRepository, collection: str, items) -> int:
    n = 0
    for obj in items:
        repo.set(collection, obj.id, obj.model_dump())
        n += 1
    return n


def load_fixtures(repo: CollaborationRepository) -> dict:
    """Load all fictional fixtures into `repo`. Returns a per-collection count."""
    summary = {
        C.USER_PRINCIPALS: _seed(repo, C.USER_PRINCIPALS, data.principals()),
        C.THERAPIST_PROFILES: _seed(repo, C.THERAPIST_PROFILES, data.therapists()),
        C.PARENT_PROFILES: _seed(repo, C.PARENT_PROFILES, data.parents()),
        C.CHILDREN: _seed(repo, C.CHILDREN, data.children()),
        C.CONNECTIONS: _seed(repo, C.CONNECTIONS, data.connections()),
        C.MILESTONES: _seed(repo, C.MILESTONES, data.milestones()),
        C.ACTIVITY_TEMPLATES: _seed(repo, C.ACTIVITY_TEMPLATES, data.activity_templates()),
        C.ACTIVITY_VERSIONS: _seed(repo, C.ACTIVITY_VERSIONS, data.activity_versions()),
        C.WEEKLY_PLANS: _seed(repo, C.WEEKLY_PLANS, data.weekly_plans()),
        C.PLAN_ASSIGNMENTS: _seed(repo, C.PLAN_ASSIGNMENTS, data.plan_assignments()),
        C.PLAN_CHANGE_PROPOSALS: _seed(repo, C.PLAN_CHANGE_PROPOSALS, data.plan_change_proposals()),
        C.PARENT_NOTES: _seed(repo, C.PARENT_NOTES, data.parent_notes()),
        C.PRIVATE_THERAPIST_NOTES: _seed(repo, C.PRIVATE_THERAPIST_NOTES, data.private_therapist_notes()),
    }
    return summary
