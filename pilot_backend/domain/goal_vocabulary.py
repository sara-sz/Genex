"""pilot_backend/domain/goal_vocabulary.py — canonical domains, MIRRORED.

`parent_taxonomy.domains` is the SOURCE OF TRUTH for the seven canonical
developmental domains. This module is a mirror, not a second definition.

## Why a mirror rather than an import

`pilot_backend` and `genex-parent` are separate top-level namespaces, and the
CI job that runs every pilot test executes from the repository root, where
`genex-parent/parent_taxonomy` is not importable. Reaching across would mean
either a sys.path manipulation or making the pilot depend on the Parent
package — and the Parent package is frozen.

So the keys are restated here and `test_canonical_domains_mirror_parent_taxonomy`
pins the exact set. If Parent's taxonomy ever changes, that test is where the
divergence surfaces, deliberately and loudly.

## Sensory is real but under-evidenced

`sensory` is a canonical domain and is listed. Parent 2.4 shipped it with no
curated milestone content, so the suggestion engine will never propose a
sensory goal from milestone evidence that does not exist — see
`goals/suggestion_engine.py`. It is included here because omitting a canonical
domain from the vocabulary would be a different and worse kind of wrong.
"""

from __future__ import annotations

from typing import Tuple

#: The seven canonical Parent 2.4 domains, in canonical order.
CANONICAL_DOMAIN_KEYS: Tuple[str, ...] = (
    "talking_and_communicating",
    "social_and_emotional",
    "learning_and_thinking",
    "fine_motor",
    "gross_motor",
    "daily_living",
    "sensory",
)

#: Deterministic ranking order used only as a LATE tie-break, after explicit
#: parent/observed selection and after evidence strength. It is a stable
#: ordering, not a clinical priority claim — a clinician reprioritises through
#: `MonthlyGoalAllocation`, which is the only place priority is expressed.
DOMAIN_TIE_BREAK_ORDER: Tuple[str, ...] = CANONICAL_DOMAIN_KEYS


class UnknownDomainError(ValueError):
    """A domain key outside the canonical seven. PHI-safe: names the key only."""

    PHI_SAFE_MESSAGE = True


def require_canonical_domain(domain_key: str) -> str:
    """Normalise and validate a domain key, or refuse.

    Fails closed rather than passing an unrecognised key through: a domain the
    vocabulary does not know cannot be ranked, cannot be worded, and would
    produce a goal nobody can trace back to evidence.
    """
    key = (domain_key or "").strip().lower()
    if key not in CANONICAL_DOMAIN_KEYS:
        raise UnknownDomainError(f"not a canonical domain key: {key or '<empty>'}")
    return key


def domain_rank(domain_key: str) -> int:
    """Index in the tie-break order. Lower sorts first."""
    return DOMAIN_TIE_BREAK_ORDER.index(require_canonical_domain(domain_key))
