"""Recommendation lifecycle state machine.

A therapist can PROPOSE; only a parent's acceptance (then a SYSTEM apply
transaction) changes a plan. The therapist never mutates a plan directly.

States:
  draft                       therapist is preparing
  pending_parent_acceptance   submitted; awaiting the parent
  accepted                    parent accepted (not yet applied)
  declined                    parent declined (terminal)
  withdrawn                   therapist withdrew (terminal)
  expired                     TTL elapsed (terminal)
  superseded                  a newer recommendation replaced it (terminal)
  applied                     system applied the accepted change (terminal)
  failed_to_apply             apply failed; retryable

Each transition records the actor role that is permitted to drive it. Applying
is a SYSTEM action (executed inside a transaction), never a therapist action.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, Set


class RecommendationState(str, Enum):
    DRAFT = "draft"
    PENDING_PARENT_ACCEPTANCE = "pending_parent_acceptance"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    APPLIED = "applied"
    FAILED_TO_APPLY = "failed_to_apply"


class Actor(str, Enum):
    THERAPIST = "therapist"
    PARENT = "parent"
    SYSTEM = "system"


S = RecommendationState

TERMINAL_STATES: Set[RecommendationState] = {
    S.DECLINED,
    S.WITHDRAWN,
    S.EXPIRED,
    S.SUPERSEDED,
    S.APPLIED,
}

# transition -> the single actor permitted to drive it.
_TRANSITIONS: Dict[RecommendationState, Dict[RecommendationState, Actor]] = {
    S.DRAFT: {
        S.PENDING_PARENT_ACCEPTANCE: Actor.THERAPIST,
        S.WITHDRAWN: Actor.THERAPIST,
    },
    S.PENDING_PARENT_ACCEPTANCE: {
        S.ACCEPTED: Actor.PARENT,
        S.DECLINED: Actor.PARENT,
        S.WITHDRAWN: Actor.THERAPIST,
        S.EXPIRED: Actor.SYSTEM,
        S.SUPERSEDED: Actor.SYSTEM,
    },
    S.ACCEPTED: {
        S.APPLIED: Actor.SYSTEM,
        S.FAILED_TO_APPLY: Actor.SYSTEM,
    },
    S.FAILED_TO_APPLY: {
        S.APPLIED: Actor.SYSTEM,  # retry
    },
}


class InvalidTransitionError(ValueError):
    """Raised when a state transition is not permitted (optionally by that actor)."""


def allowed_next(state: RecommendationState) -> Set[RecommendationState]:
    return set(_TRANSITIONS.get(state, {}).keys())


def can_transition(
    current: RecommendationState,
    target: RecommendationState,
    actor: Actor | None = None,
) -> bool:
    allowed = _TRANSITIONS.get(current, {})
    if target not in allowed:
        return False
    if actor is not None and allowed[target] != actor:
        return False
    return True


def assert_transition(
    current: RecommendationState,
    target: RecommendationState,
    actor: Actor | None = None,
) -> None:
    if not can_transition(current, target, actor):
        by = f" by {actor.value}" if actor else ""
        raise InvalidTransitionError(
            f"Illegal recommendation transition{by}: {current.value} -> {target.value}."
        )
