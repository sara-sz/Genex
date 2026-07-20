"""Recommendation state-machine transitions."""

from __future__ import annotations

import pytest

from app.domain.recommendation_state import (
    Actor,
    InvalidTransitionError,
    RecommendationState as S,
    assert_transition,
    can_transition,
)


def test_valid_transitions():
    assert can_transition(S.DRAFT, S.PENDING_PARENT_ACCEPTANCE, Actor.THERAPIST)
    assert can_transition(S.PENDING_PARENT_ACCEPTANCE, S.ACCEPTED, Actor.PARENT)
    assert can_transition(S.PENDING_PARENT_ACCEPTANCE, S.DECLINED, Actor.PARENT)
    assert can_transition(S.ACCEPTED, S.APPLIED, Actor.SYSTEM)
    assert can_transition(S.FAILED_TO_APPLY, S.APPLIED, Actor.SYSTEM)
    assert can_transition(S.PENDING_PARENT_ACCEPTANCE, S.EXPIRED, Actor.SYSTEM)
    assert can_transition(S.PENDING_PARENT_ACCEPTANCE, S.SUPERSEDED, Actor.SYSTEM)


def test_invalid_transitions_rejected():
    assert not can_transition(S.DRAFT, S.APPLIED)
    assert not can_transition(S.DECLINED, S.ACCEPTED)
    assert not can_transition(S.APPLIED, S.PENDING_PARENT_ACCEPTANCE)
    with pytest.raises(InvalidTransitionError):
        assert_transition(S.DRAFT, S.APPLIED)


def test_therapist_cannot_apply():
    # Applying is a SYSTEM action; a therapist may not drive accepted->applied.
    assert not can_transition(S.ACCEPTED, S.APPLIED, Actor.THERAPIST)
    with pytest.raises(InvalidTransitionError):
        assert_transition(S.ACCEPTED, S.APPLIED, Actor.THERAPIST)


def test_parent_cannot_apply_or_expire():
    assert not can_transition(S.ACCEPTED, S.APPLIED, Actor.PARENT)
    assert not can_transition(S.PENDING_PARENT_ACCEPTANCE, S.EXPIRED, Actor.PARENT)


def test_only_parent_accepts():
    assert not can_transition(S.PENDING_PARENT_ACCEPTANCE, S.ACCEPTED, Actor.THERAPIST)
    assert can_transition(S.PENDING_PARENT_ACCEPTANCE, S.ACCEPTED, Actor.PARENT)
