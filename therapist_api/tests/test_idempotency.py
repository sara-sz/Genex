"""Deterministic idempotency ids + no duplicate records / no duplicate mutation."""

from __future__ import annotations

from app.domain.ids import idempotency_doc_id, recommendation_response_doc_id
from app.repository.memory import InMemoryRepository
from app.services.recommendations import (
    PLAN_COL,
    respond_to_recommendation,
    submit_recommendation,
)


def test_idempotency_doc_id_is_deterministic():
    a = idempotency_doc_id("key-123")
    b = idempotency_doc_id("key-123")
    assert a == b and a.startswith("idem_")
    assert idempotency_doc_id("other") != a


def test_response_doc_id_deterministic_per_recommendation():
    a = recommendation_response_doc_id("rec-1")
    assert a == recommendation_response_doc_id("rec-1")
    assert a.startswith("resp_")
    assert recommendation_response_doc_id("rec-2") != a


def _rec(env="dev"):
    return {
        "connection_id": "conn1",
        "therapist_uid": "slp1",
        "child_ref": "child_a",
        "target_activity_ref": "act_bubbles",
        "action": "adapt",
        "plan_version_at_creation": 1,
        "environment": env,
    }


def test_duplicate_submission_returns_original():
    repo = InMemoryRepository()
    first = submit_recommendation(repo, idempotency_key="k1", recommendation=_rec())
    second = submit_recommendation(repo, idempotency_key="k1", recommendation=_rec())
    assert first.created is True
    assert second.created is False
    assert first.recommendation_id == second.recommendation_id
    # Exactly one record exists.
    assert len(repo.query("recommendations")) == 1


def test_duplicate_accept_produces_no_duplicate_mutation():
    repo = InMemoryRepository()
    sub = submit_recommendation(repo, idempotency_key="k2", recommendation=_rec())
    rec_id = sub.recommendation_id

    r1 = respond_to_recommendation(
        repo, recommendation_id=rec_id, parent_uid="par1", response="accepted", environment="dev"
    )
    r2 = respond_to_recommendation(
        repo, recommendation_id=rec_id, parent_uid="par1", response="accepted", environment="dev"
    )

    assert r1.created is True and r1.plan_applied is True
    assert r2.created is False  # duplicate collides on deterministic response id
    # Plan applied EXACTLY once: version incremented by 1, one applied entry.
    plan = repo.get(PLAN_COL, "child_a")
    assert plan["version"] == 2  # started at 1, applied once
    assert plan["applied"] == [rec_id]
    # Only one response record.
    assert len(repo.query("recommendation_responses")) == 1


def test_decline_does_not_apply_plan():
    repo = InMemoryRepository()
    sub = submit_recommendation(repo, idempotency_key="k3", recommendation=_rec())
    r = respond_to_recommendation(
        repo,
        recommendation_id=sub.recommendation_id,
        parent_uid="par1",
        response="declined",
        environment="dev",
    )
    assert r.created is True and r.plan_applied is False
    assert not repo.exists(PLAN_COL, "child_a")
