"""Idempotent recommendation submission + parent response (DESIGN DEMONSTRATION).

Proves the correction #3 idempotency design end-to-end on the in-memory repo:

  * submit_recommendation: deterministic doc id from the idempotency key; a
    duplicate submission returns the ORIGINAL recommendation (no second record).
  * respond_to_recommendation: deterministic response doc id from the
    recommendation id; a duplicate accept/decline returns the ORIGINAL result and
    applies the (fictional) plan change EXACTLY ONCE.

Not an HTTP endpoint. Not connected to Firestore. Not a real plan mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from ..domain.ids import idempotency_doc_id, recommendation_response_doc_id
from ..domain.recommendation_state import (
    Actor,
    RecommendationState,
    assert_transition,
)
from ..repository.interface import CollaborationRepository

REC_COL = "recommendations"
RESP_COL = "recommendation_responses"
PLAN_COL = "fictional_plans"  # fictional; proves single-apply, not a real plan


@dataclass
class SubmitResult:
    recommendation_id: str
    created: bool
    recommendation: Dict


@dataclass
class RespondResult:
    response_id: str
    created: bool
    response: Dict
    plan_applied: bool


def submit_recommendation(
    repo: CollaborationRepository,
    *,
    idempotency_key: str,
    recommendation: Dict,
) -> SubmitResult:
    """Create a pending recommendation idempotently by idempotency key."""
    doc_id = idempotency_doc_id(idempotency_key)
    rec = dict(recommendation)
    rec["id"] = doc_id
    rec["idempotency_key"] = idempotency_key
    rec["status"] = RecommendationState.PENDING_PARENT_ACCEPTANCE.value
    created, stored = repo.create_if_absent(REC_COL, doc_id, rec)
    return SubmitResult(recommendation_id=doc_id, created=created, recommendation=stored)


def respond_to_recommendation(
    repo: CollaborationRepository,
    *,
    recommendation_id: str,
    parent_uid: str,
    response: str,  # "accepted" | "declined"
    environment: str,
) -> RespondResult:
    """Record a terminal parent response idempotently and apply once if accepted.

    Duplicate calls collide on the deterministic response doc id and return the
    original result WITHOUT a second plan mutation.
    """
    if response not in ("accepted", "declined"):
        raise ValueError("response must be 'accepted' or 'declined'.")

    rec = repo.get(REC_COL, recommendation_id)
    current = RecommendationState(rec["status"])

    resp_id = recommendation_response_doc_id(recommendation_id)
    resp_doc = {
        "id": resp_id,
        "recommendation_id": recommendation_id,
        "parent_uid": parent_uid,
        "response": response,
        "environment": environment,
    }

    created, stored = repo.create_if_absent(RESP_COL, resp_id, resp_doc)
    if not created:
        # Duplicate response — no state change, no second apply.
        already_applied = (
            repo.get(REC_COL, recommendation_id)["status"]
            == RecommendationState.APPLIED.value
        )
        return RespondResult(
            response_id=resp_id,
            created=False,
            response=stored,
            plan_applied=already_applied and stored["response"] == "accepted",
        )

    # First (and only) terminal response: drive the state machine.
    target = (
        RecommendationState.ACCEPTED
        if response == "accepted"
        else RecommendationState.DECLINED
    )
    assert_transition(current, target, Actor.PARENT)
    rec["status"] = target.value
    repo.set(REC_COL, recommendation_id, rec)

    plan_applied = False
    if target == RecommendationState.ACCEPTED:
        # SYSTEM apply transaction (fictional plan): accepted -> applied, once.
        assert_transition(
            RecommendationState.ACCEPTED, RecommendationState.APPLIED, Actor.SYSTEM
        )
        plan_id = rec["child_ref"]
        plan = (
            repo.get(PLAN_COL, plan_id)
            if repo.exists(PLAN_COL, plan_id)
            else {"id": plan_id, "version": rec.get("plan_version_at_creation", 0), "applied": []}
        )
        plan["version"] = int(plan["version"]) + 1
        plan["applied"].append(recommendation_id)
        repo.set(PLAN_COL, plan_id, plan)
        rec["status"] = RecommendationState.APPLIED.value
        repo.set(REC_COL, recommendation_id, rec)
        plan_applied = True

    return RespondResult(
        response_id=resp_id, created=True, response=stored, plan_applied=plan_applied
    )
