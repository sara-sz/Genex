"""Parent 2.4 canonical developmental-domain taxonomy — THE single authority.

Fictional/dev work. No Firestore, no Firebase, no RTM, no real data.

## Why this module exists

The PARENT-0.1 audit found the same vocabulary expressed five times, in three
different spellings:

    genex_core/config.py        DOMAIN_CONFIG + ALIAS_TO_CATEGORY   (2 maps)
    genex_core/table_loader.py  _CATEGORY_DISPLAY_TO_KEY            (near-duplicate)
    api/adapters.py             label map
    api/pipeline.py             identical label map (copy)
    api/focus_selector.py       DIFFERENT label map

`focus_selector` labelled one key "Fine & Gross Motor & Daily Skills" — three
developmental domains named inside a single label. That is the collapse this
module ends.

This file is the ONE place the Parent 2.4 vocabulary is defined. Consumers must
import from here rather than restating strings.

## Deliberately NOT here

* **Therapy disciplines.** SLP / OT / PT are provider disciplines, not
  developmental domains. OT is not Fine Motor; PT is not a rename of Gross
  Motor; SLP is not an identity for Talking & Communicating. The discipline
  model is a later phase — see `docs/PARENT_2_4_TAXONOMY_FOUNDATION.md`.
* **Movement & Physical.** It is NOT a canonical Parent 2.4 domain. It survives
  only as a legacy compatibility alias for Beta 2.3 and historical state, and it
  deliberately resolves to `None` rather than to any single new domain, because
  it genuinely spanned three of them.

## Content status vs. domain existence

A domain EXISTING and Genex HAVING validated recommendation content for it are
two different facts, and the product must be able to tell them apart. `Sensory`
is a canonical domain with `ContentStatus.PENDING`: the Gold Standard holds zero
sensory milestone rows today. Sensory must never silently borrow content from
another domain — in particular it must not fall back to `emotional_regulation`,
which is what the pre-2.4 Brain did (`genex_core/config.py`: *"Sensory concerns
map to the closest supported domain (Social/Emotional regulation) until a
dedicated sensory domain exists"*).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple

TAXONOMY_VERSION = "parent-2.4-domains-v1"


class ContentStatus(str, Enum):
    """Whether Genex has validated Gold Standard content for a domain.

    `AVAILABLE` means milestone rows exist in the authoritative workbook.
    `PENDING` means the domain is real and selectable in the model, but no
    clinical content exists yet, so it must not generate recommendations.
    """

    AVAILABLE = "available"
    PENDING = "pending"


@dataclass(frozen=True)
class Domain:
    """One canonical Parent 2.4 developmental domain."""

    key: str
    display: str
    order: int
    content_status: ContentStatus = ContentStatus.AVAILABLE
    #: Free-text note explaining a non-AVAILABLE status. Never user-facing copy.
    content_note: str = ""

    @property
    def has_content(self) -> bool:
        return self.content_status is ContentStatus.AVAILABLE


# ── the seven canonical domains, in stable presentation order ───────────────
#
# Order is part of the contract: consumers must not re-sort. It runs
# communication → social → cognitive → motor (fine, then gross) → daily living
# → sensory, which keeps the two motor domains adjacent now that they are
# finally distinct.
DOMAINS: Tuple[Domain, ...] = (
    Domain("talking_and_communicating", "Talking & Communicating", 1),
    Domain("social_and_emotional", "Social & Emotional", 2),
    Domain("learning_and_thinking", "Learning & Thinking", 3),
    Domain("fine_motor", "Fine Motor", 4),
    Domain("gross_motor", "Gross Motor", 5),
    Domain("daily_living", "Daily Living", 6),
    Domain(
        "sensory",
        "Sensory",
        7,
        content_status=ContentStatus.PENDING,
        content_note=(
            "Canonical domain with no Gold Standard milestone rows. Must not "
            "borrow Social/Emotional content and must not route to "
            "emotional_regulation. Clinical content is a later phase."
        ),
    ),
)

DOMAIN_KEYS: Tuple[str, ...] = tuple(d.key for d in DOMAINS)
BY_KEY: Dict[str, Domain] = {d.key: d for d in DOMAINS}

#: Domains that can currently produce recommendations.
CONTENT_READY_KEYS: Tuple[str, ...] = tuple(d.key for d in DOMAINS if d.has_content)
#: Domains that exist but have no validated content yet.
CONTENT_PENDING_KEYS: Tuple[str, ...] = tuple(
    d.key for d in DOMAINS if not d.has_content
)


# ── legacy compatibility layer (READ-ONLY interpretation of Beta 2.3) ───────
#
# These are the four pre-2.4 Brain keys. They are NOT canonical 2.4 domains and
# never appear in DOMAIN_KEYS. They exist so historical sessions stay readable.
LEGACY_DOMAIN_KEYS: Tuple[str, ...] = (
    "language_and_communication",
    "social_and_emotional",
    "cognitive",
    "movement_and_physical",
)

#: Legacy key -> canonical key, ONLY where the mapping is unambiguous.
#:
#: `movement_and_physical` is deliberately absent: it spanned Fine Motor, Gross
#: Motor and Daily Living, so collapsing it onto any single 2.4 domain would
#: fabricate a precision the old data never had. Callers must use
#: `resolve_legacy_domain` and handle the ambiguous case explicitly.
LEGACY_TO_CANONICAL: Dict[str, str] = {
    "language_and_communication": "talking_and_communicating",
    "social_and_emotional": "social_and_emotional",
    "cognitive": "learning_and_thinking",
}

#: Legacy keys that genuinely span several canonical domains.
LEGACY_AMBIGUOUS: Dict[str, Tuple[str, ...]] = {
    "movement_and_physical": ("fine_motor", "gross_motor", "daily_living"),
}


class UnknownDomain(ValueError):
    """Raised for a key that is neither canonical nor a recognised legacy key."""


def get(key: str) -> Domain:
    """Return the canonical domain for `key`, or raise. Fail closed."""
    try:
        return BY_KEY[key]
    except KeyError:
        raise UnknownDomain(f"{key!r} is not a Parent 2.4 canonical domain") from None


def is_canonical(key: str) -> bool:
    return key in BY_KEY


def is_legacy(key: str) -> bool:
    return key in LEGACY_DOMAIN_KEYS


def resolve_legacy_domain(key: str) -> Optional[str]:
    """Interpret a historical Beta 2.3 domain key.

    Returns the canonical key when the legacy key maps unambiguously, or `None`
    when it spanned several canonical domains (today: only
    `movement_and_physical`). `None` means *"ambiguous — do not guess"*, not
    *"unknown"*; unknown keys raise instead.

    Callers that receive `None` should fall back to subdomain-level evidence
    where the historical session recorded it, and must NOT fabricate a
    Fine/Gross/Daily breakdown that the old data cannot support.
    """
    if key in LEGACY_TO_CANONICAL:
        return LEGACY_TO_CANONICAL[key]
    if key in LEGACY_AMBIGUOUS:
        return None
    if key in BY_KEY:
        return key  # already canonical — idempotent
    raise UnknownDomain(f"{key!r} is neither a canonical nor a legacy domain key")


def display_for(key: str) -> str:
    """Display label for a canonical key. The ONLY source of parent-facing labels."""
    return get(key).display


def ordered_domains() -> Tuple[Domain, ...]:
    """Canonical domains in stable contract order."""
    return DOMAINS
