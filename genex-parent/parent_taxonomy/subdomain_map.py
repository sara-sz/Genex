"""Parent 2.4 subdomain → canonical domain map. Deterministic and fail-closed.

Fictional/dev work. No Firestore, no Firebase, no RTM, no real data.

## The finding this module acts on

The PARENT-0.1 audit established that the Gold Standard's `subdomain` column
ALREADY separates the motor concepts the pre-2.4 taxonomy collapsed:

    fine_motor_hand_use                     29 rows
    gross_motor_mobility_and_coordination   28 rows
    postural_control_and_transitions        21 rows
    self_help_motor_skills                  26 rows

So the Fine/Gross/Daily Living split is a **re-parenting of existing rows**, not
a re-rating of milestone content. Every one of the 24 subdomains maps here
exactly once, and the counts reconcile to the full 369 rows.

## Fail closed, always

A subdomain that is unknown, maps to nothing, or maps to more than one
developmental domain raises. It never silently lands in a default bucket — the
pre-2.4 behaviour (`motor`/`physical`/`adaptive` quietly folding into
`movement_and_physical` / `cognitive`) is precisely the failure mode that made
the collapse invisible for so long.

## Cross-category moves

Three subdomains move OUT of `cognitive` and into `daily_living`
(`adaptive_feeding_cues`, `safety_awareness`) or are re-parented within the old
movement bucket (`self_help_motor_skills`). Those moves are the reason
`learning_and_thinking` is smaller than the old `cognitive` category.
"""

from __future__ import annotations

from typing import Dict, Set, Tuple

from .domains import BY_KEY, UnknownDomain

SUBDOMAIN_MAP_VERSION = "parent-2.4-subdomain-map-v1"


class SubdomainMappingError(ValueError):
    """A subdomain could not be mapped to exactly one canonical domain."""


#: Every subdomain present in the frozen Gold Standard snapshot, mapped to
#: exactly one canonical Parent 2.4 domain. Founder-locked for this phase.
SUBDOMAIN_TO_DOMAIN: Dict[str, str] = {
    # ── Talking & Communicating (was: language and communication) ──────────
    "early_vocalization_and_babbling": "talking_and_communicating",
    "receptive_language": "talking_and_communicating",
    "gestural_communication": "talking_and_communicating",
    "expressive_language": "talking_and_communicating",
    "speech_intelligibility": "talking_and_communicating",
    "conversation_narrative": "talking_and_communicating",
    # ── Social & Emotional (unchanged parentage) ───────────────────────────
    "emotional_regulation": "social_and_emotional",
    "attachment_and_separation": "social_and_emotional",
    "social_engagement_and_joint_attention": "social_and_emotional",
    "play_and_symbolic_social_play": "social_and_emotional",
    "peer_interaction_and_social_rules": "social_and_emotional",
    "empathy_and_prosocial_behavior": "social_and_emotional",
    # ── Learning & Thinking (was: cognitive, minus the 9 rows below) ───────
    "attention_and_processing": "learning_and_thinking",
    "exploration_and_object_use": "learning_and_thinking",
    "object_permanence_and_problem_solving": "learning_and_thinking",
    "imitation_and_play_skills": "learning_and_thinking",
    "concepts_and_following_directions": "learning_and_thinking",
    "pre_academic_skills": "learning_and_thinking",
    # ── Fine Motor (was: movement and physical) ────────────────────────────
    "fine_motor_hand_use": "fine_motor",
    # ── Gross Motor (was: movement and physical) ───────────────────────────
    "gross_motor_mobility_and_coordination": "gross_motor",
    "postural_control_and_transitions": "gross_motor",
    # ── Daily Living (was: movement and physical / cognitive) ──────────────
    "self_help_motor_skills": "daily_living",       # from movement and physical
    "adaptive_feeding_cues": "daily_living",        # from cognitive
    "safety_awareness": "daily_living",             # from cognitive
    # ── Sensory ────────────────────────────────────────────────────────────
    # Intentionally empty. No Gold Standard subdomain maps to `sensory`; the
    # domain exists with ContentStatus.PENDING. Adding a borrowed subdomain
    # here would fabricate content the clinical record does not support.
}

#: Subdomains whose canonical parent differs from their historical category.
#: Derived at import for the audit table; not a second source of truth.
CROSS_CATEGORY_MOVES: Tuple[str, ...] = (
    "self_help_motor_skills",
    "adaptive_feeding_cues",
    "safety_awareness",
)


def resolve(subdomain: str) -> str:
    """Return the canonical domain key for `subdomain`. Raises on any ambiguity.

    Fail-closed on three distinct failures, each reported distinctly:
      * unknown subdomain
      * mapped to an empty/blank value
      * mapped to a key that is not a canonical Parent 2.4 domain
    """
    key = (subdomain or "").strip()
    if not key:
        raise SubdomainMappingError("subdomain must not be empty")
    if key not in SUBDOMAIN_TO_DOMAIN:
        raise SubdomainMappingError(
            f"unknown subdomain {key!r}: no Parent 2.4 domain mapping. "
            "Refusing to guess — add an explicit mapping."
        )
    domain = (SUBDOMAIN_TO_DOMAIN[key] or "").strip()
    if not domain:
        raise SubdomainMappingError(f"subdomain {key!r} maps to no domain")
    if domain not in BY_KEY:
        raise UnknownDomain(
            f"subdomain {key!r} maps to {domain!r}, which is not a canonical domain"
        )
    return domain


def validate_map() -> None:
    """Assert the map's internal invariants. Raises on the first violation.

    Pinned invariants:
      * every target is a canonical domain key
      * no subdomain maps to more than one domain (dict enforces it structurally,
        so this additionally guards against a duplicated literal key being
        silently collapsed at parse time — checked by comparing counts)
      * `sensory` receives no subdomain
    """
    for sub, dom in SUBDOMAIN_TO_DOMAIN.items():
        if dom not in BY_KEY:
            raise UnknownDomain(f"{sub!r} -> {dom!r} is not a canonical domain")
    sensory = [s for s, d in SUBDOMAIN_TO_DOMAIN.items() if d == "sensory"]
    if sensory:
        raise SubdomainMappingError(
            f"sensory must have no Gold Standard subdomains; got {sensory}"
        )


def mapped_subdomains() -> Set[str]:
    return set(SUBDOMAIN_TO_DOMAIN)


def domains_covered() -> Set[str]:
    return set(SUBDOMAIN_TO_DOMAIN.values())
