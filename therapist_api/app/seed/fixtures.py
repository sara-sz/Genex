"""Fictional fixtures for the alpha (dev-only).

Everything here is invented. No real child, parent, therapist, diagnosis,
feedback, message, or connection. Child identity is an alias only ("Child A").
"""

from __future__ import annotations

from typing import List

from ..domain.models import (
    ChildReference,
    Recommendation,
    TherapistChildConnection,
    TherapistProfile,
    WeeklyPlanItem,
)
from ..domain.provenance import Provenance
from ..domain.recommendation_state import RecommendationState

ENV = "dev"


def hannah_slp() -> TherapistProfile:
    return TherapistProfile(
        id="ther_hannah",
        uid="dev-slp-hannah",
        display_name="Hannah (SLP, fictional)",
        discipline="slp",
        environment=ENV,
    )


def child_a() -> ChildReference:
    return ChildReference(
        id="child_a",
        parent_uid="dev-parent-01",
        source="dev_fixture",
        display_alias="Child A",
        age_band="3-4y",
        primary_domains=["Talking & Communicating", "Social & Emotional"],
        environment=ENV,
    )


def hannah_child_a_connection() -> TherapistChildConnection:
    return TherapistChildConnection(
        id="conn_hannah_childa",
        therapist_uid="dev-slp-hannah",
        child_ref="child_a",
        status="active",
        scopes=["view_plan", "view_feedback", "view_progress", "recommend"],
        environment=ENV,
    )


def child_a_weekly_plan() -> List[WeeklyPlanItem]:
    return [
        WeeklyPlanItem(
            activity_ref="act_bubbles",
            title="Bubble requesting",
            domain="Talking & Communicating",
            day_of_week=0,
            provenance=Provenance.PARENT_REPORTED,
        ),
        WeeklyPlanItem(
            activity_ref="act_turntaking",
            title="Turn-taking with a ball",
            domain="Social & Emotional",
            day_of_week=2,
            provenance=Provenance.PARENT_REPORTED,
        ),
    ]


def sample_recommendation() -> Recommendation:
    return Recommendation(
        id="rec_sample",
        connection_id="conn_hannah_childa",
        therapist_uid="dev-slp-hannah",
        child_ref="child_a",
        target_activity_ref="act_bubbles",
        action="adapt",
        rationale_note="Parent reported this was too difficult; simplify the prompt.",
        status=RecommendationState.DRAFT,
        idempotency_key="seed-rec-sample",
        plan_version_at_creation=1,
        environment=ENV,
    )
