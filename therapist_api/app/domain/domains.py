"""Developmental-domain display taxonomy (correction #6).

The eight labels are the EXACT current Genex parent-facing display values,
versioned via DOMAIN_TAXONOMY_VERSION. Unknown domains are handled EXPLICITLY:
`require_known_domain` raises rather than silently coercing.

We deliberately do NOT map these onto the parent backend's four canonical
domains here — that mapping (and any parent snapshot contract) is a Parent
Beta 2.4 design decision.
"""

from __future__ import annotations

from ..constants import DISPLAY_DOMAINS, DOMAIN_TAXONOMY_VERSION

__all__ = [
    "DISPLAY_DOMAINS",
    "DOMAIN_TAXONOMY_VERSION",
    "UnknownDomainError",
    "is_valid_domain",
    "require_known_domain",
]

_VALID = frozenset(DISPLAY_DOMAINS)

# Snake-case domain keys (frontend/request form) → versioned display labels.
DOMAIN_KEY_TO_DISPLAY = {
    "talking_and_communicating": "Talking & Communicating",
    "social_and_emotional": "Social & Emotional",
    "learning_and_thinking": "Learning & Thinking",
    "movement_and_physical": "Movement & Physical",
    "daily_living": "Daily Living",
    "sensory": "Sensory",
    "fine_motor": "Fine Motor",
    "gross_motor": "Gross Motor",
}


def display_for_domain_key(key: str) -> str:
    """Return the display label for a snake-case domain key, or '' if unknown."""
    return DOMAIN_KEY_TO_DISPLAY.get((key or "").strip().lower(), "")


class UnknownDomainError(ValueError):
    """Raised when a domain label is not part of the versioned taxonomy."""


def is_valid_domain(label: str) -> bool:
    return label in _VALID


def require_known_domain(label: str) -> str:
    """Return the label if known; otherwise fail explicitly (never coerce)."""
    if label not in _VALID:
        raise UnknownDomainError(
            f"Unknown developmental domain '{label}'. "
            f"Expected one of {list(DISPLAY_DOMAINS)} "
            f"(taxonomy {DOMAIN_TAXONOMY_VERSION})."
        )
    return label
