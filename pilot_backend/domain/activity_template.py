"""0.6A-1 — one curated, parent-facing activity card as pilot domain data.

An `ActivityTemplate` is a REUSABLE piece of authored content, not a scheduled
thing. One template can be placed in many cycles for many children; the
scheduled placement is `ActivityGoalAlignment`, which already exists and is
untouched by this slice.

## IDENTITY IS THE CONTENT

`activity_template_id` is a digest over the family plus all nine authored
fields. So an edited card is a DIFFERENT template, not the same template with
new words. That is the honest reading for clinical content: a parent who was
given "wait 5 seconds" and a parent given "wait 10 seconds" did not receive the
same activity, and a later evidence record that names a template must not
silently come to mean something the clinician never approved.

It also makes the generated artifact self-checking in the same way
`CanonicalRung` is: the runtime recomputes the id from the fields and refuses a
mismatch, so a hand-edited card cannot keep its id.

## THE NINE FIELDS ARE THE SOURCE'S, NOT OURS

They are exactly the schema the frozen Parent curated cards already use. No
field is added, renamed or defaulted here — in particular there is no
`duration_minutes` (the Parent engine supplies a global constant, not per-card
data), no frequency field (repetitions live inside `instructions` as prose) and
no difficulty tier (the source has none). Inventing any of them would make this
record claim authorship it does not have.

## WHAT A TEMPLATE IS NOT

Not a plan, not a schedule, not a goal, not evidence. It carries no child, no
date, no cycle and no outcome. A template says "this activity exists and serves
this family"; everything about a particular child is in the weekly layer.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping, Tuple

#: Prefix on every template id. A future scheme change takes a new prefix
#: rather than silently producing different digests under the same name — the
#: same discipline `RUNG_SCHEME` uses.
TEMPLATE_SCHEME = "atpl1:"

#: Mixed into the digest so a hash computed elsewhere over the same strings
#: cannot be mistaken for a template id.
TEMPLATE_HASH_TAG = "genex-activity-template-v1"

#: ASCII unit separator — cannot occur in authored card text, which makes the
#: joined payload injective.
_UNIT_SEP = "\x1f"

#: The EXACT nine authored fields of a frozen Parent curated card, in a fixed
#: order so the digest cannot depend on dict iteration.
CARD_FIELDS: Tuple[str, ...] = (
    "title",
    "theme",
    "materials",
    "instructions",
    "success_criteria",
    "make_easier",
    "make_harder",
    "group_play_line",
    "what_to_avoid",
)

#: Curated tiers the pilot will admit. Tier 3 — the Parent engine's generic
#: `base`/`overrides` template — is DELIBERATELY ABSENT and must never be added:
#: it is placeholder prose that the Parent validator itself rejects.
ADMITTED_SOURCE_TIERS: Tuple[str, ...] = ("family_curated", "bucket_curated")

#: The tier that must never appear, named explicitly so a reader sees the
#: exclusion rather than inferring it from the absence of an entry above.
FORBIDDEN_SOURCE_TIER = "generic_fallback"


class ActivityTemplateError(Exception):
    """A template could not be formed. PHI-safe: carries no child data."""

    PHI_SAFE_MESSAGE = True


def compute_template_id(activity_family_ref: str,
                        card: Mapping[str, str]) -> str:
    """The deterministic identity of one curated card.

    Content-addressed over the family and all nine fields. Whitespace is
    stripped but nothing else is normalised: unlike a milestone, an activity
    card's wording IS the deliverable, so folding case or punctuation would
    make two genuinely different instructions share an id.
    """
    family = (activity_family_ref or "").strip()
    if not family:
        raise ActivityTemplateError("a template requires an activity family")
    parts = [TEMPLATE_HASH_TAG, family]
    for field_name in CARD_FIELDS:
        value = card.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ActivityTemplateError(
                f"a curated card requires a non-empty {field_name}")
        parts.append(value.strip())
    digest = hashlib.sha256(_UNIT_SEP.join(parts).encode("utf-8")).hexdigest()
    return f"{TEMPLATE_SCHEME}{digest[:32]}"


@dataclass(frozen=True)
class ActivityTemplate:
    """One curated parent-facing activity, bound to one canonical family."""

    activity_template_id: str
    activity_family_ref: str
    source_tier: str
    source_pool_ref: str
    title: str
    theme: str
    materials: str
    instructions: str
    success_criteria: str
    make_easier: str
    make_harder: str
    group_play_line: str
    what_to_avoid: str

    def __post_init__(self) -> None:
        if self.source_tier not in ADMITTED_SOURCE_TIERS:
            # Names the forbidden tier in the message so the generic-fallback
            # case is unmistakable in a build log.
            raise ActivityTemplateError(
                f"source tier {self.source_tier!r} is not admitted; only "
                f"{ADMITTED_SOURCE_TIERS} are, and "
                f"{FORBIDDEN_SOURCE_TIER!r} never is")
        expected = compute_template_id(self.activity_family_ref, self.as_card())
        if self.activity_template_id != expected:
            # The integrity check. A hand-edited card cannot keep its id.
            raise ActivityTemplateError(
                "activity_template_id does not match the card content")

    def as_card(self) -> Mapping[str, str]:
        """The nine authored fields, exactly as the source holds them."""
        return {name: getattr(self, name) for name in CARD_FIELDS}

    @staticmethod
    def build(*, activity_family_ref: str, source_tier: str,
              source_pool_ref: str,
              card: Mapping[str, str]) -> "ActivityTemplate":
        """The only constructor callers should use; it computes the id."""
        family = (activity_family_ref or "").strip()
        values = {}
        for field_name in CARD_FIELDS:
            value = card.get(field_name)
            if not isinstance(value, str) or not value.strip():
                raise ActivityTemplateError(
                    f"a curated card requires a non-empty {field_name}")
            values[field_name] = value.strip()
        return ActivityTemplate(
            activity_template_id=compute_template_id(family, values),
            activity_family_ref=family,
            source_tier=source_tier,
            source_pool_ref=source_pool_ref,
            **values)
