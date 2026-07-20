"""Fictional fixture / schema models (alpha).

These are pydantic models for the therapist collaboration store. In the alpha,
every instance is FICTIONAL — no real child, parent, therapist, diagnosis,
feedback, message, or connection data (real-data decision).

Child privacy (correction #7):
  * The alpha uses a `display_alias` only (e.g. "Child A"); it does NOT store a
    real first/preferred name.
  * The permanent production schema is NOT designed around aliases alone —
    `preferred_name` is reserved as an OPTIONAL field that may only ever be
    populated AFTER explicit consent + privacy + retention + BAA approval.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from .provenance import Provenance
from .recommendation_state import RecommendationState
from .roles import UserRole


class EnvironmentMetadata(BaseModel):
    environment: str
    gcp_project_id: str
    firebase_project_id: str
    firestore_project_id: str
    region: str
    schema_version: str = "therapist-alpha-0.1"


class TherapistProfile(BaseModel):
    id: str
    uid: str
    display_name: str
    discipline: str = "slp"
    status: str = "active"
    environment: str
    schema_version: str = "therapist-alpha-0.1"


class ChildReference(BaseModel):
    """Opaque handle to a (fictional, in alpha) child. Name-minimal by design."""

    id: str  # child_ref
    parent_uid: str
    source: str = "dev_fixture"  # dev_fixture | parent_session (future)
    display_alias: str  # e.g. "Child A" — NOT a real name
    preferred_name: Optional[str] = None  # reserved; consent+BAA-gated (never in alpha)
    age_band: Optional[str] = None
    primary_domains: List[str] = Field(default_factory=list)
    environment: str
    schema_version: str = "therapist-alpha-0.1"


class TherapistChildConnection(BaseModel):
    id: str
    therapist_uid: str
    child_ref: str
    status: str = "invited"  # invited|active|paused|ended|declined
    scopes: List[str] = Field(default_factory=list)
    environment: str
    version: int = 1
    schema_version: str = "therapist-alpha-0.1"


class WeeklyPlanItem(BaseModel):
    activity_ref: str
    title: str
    domain: str
    day_of_week: int  # 0=Mon .. 6=Sun
    provenance: Provenance = Provenance.PARENT_REPORTED


class Recommendation(BaseModel):
    id: str
    connection_id: str
    therapist_uid: str
    child_ref: str
    target_activity_ref: str
    action: str  # adapt|replace|add
    rationale_note: Optional[str] = None
    status: RecommendationState = RecommendationState.DRAFT
    idempotency_key: str
    plan_version_at_creation: int
    environment: str
    version: int = 1
    schema_version: str = "therapist-alpha-0.1"


class RecommendationResponse(BaseModel):
    id: str
    recommendation_id: str
    parent_uid: str
    response: str  # accepted|declined
    idempotency_key: str
    environment: str
    schema_version: str = "therapist-alpha-0.1"


class AuditEvent(BaseModel):
    id: str
    event_type: str
    actor_uid: str
    actor_role: UserRole
    subject_type: str
    subject_id: str
    recommendation_id: Optional[str] = None
    child_ref: Optional[str] = None
    request_id: Optional[str] = None
    environment: str
    schema_version: str = "therapist-alpha-0.1"
