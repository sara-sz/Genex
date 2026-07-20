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
