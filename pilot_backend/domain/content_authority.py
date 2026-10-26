"""A2 v2.1 — the Pilot is the SOLE authority on content availability.

Parent derives a served-family set of its own and uses it for caregiver UX and
for withholding a projection upstream. That set is a MIRROR. It is useful — it
stops an unservable target reaching the Pilot at all — but it is not authority,
because the approved activity bank lives here and only here.

So generation asks THIS, immediately before it writes, rather than trusting
what Parent said when it projected.

## NO SECOND REGISTRY

`support_for_target` reads the existing approved activity bank — the same
object the Week 1 release path already uses, with its own
`served_families()` and `artifact_digest()`. There is deliberately no new
family table and no mirrored support map here: a second copy of "which
families have content" is exactly how the Parent mirror and the real bank
would drift apart without anyone noticing.

## THE AUTHORITY MUST BE PRESENT, NOT OPTIONAL

`support_for_target` raises `ContentAuthorityUnavailable` rather than
returning "unknown". A generation path that treated an absent bank as
permission would be the activity-bank composition defect again: that bug
shipped a served application with `activity_bank=None` and every correctly
authorised release fail-closed, which was visible. An absent bank that
silently authorised a WRITE would not be.

## WHY THE DIGEST TRAVELS WITH THE ANSWER

Serviceability is a statement about the content state at a moment. Returning
the digest alongside the verdict means a refusal can say which content state
produced it, and two disagreeing answers are always attributable to content
rather than to anything about the child. It is the same reason the Parent-side
decision carries its own content digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple

#: The three serviceability states, shared with the Parent-side vocabulary so a
#: reconciliation compares like with like.
SUPPORT_SUPPORTED = "supported"
SUPPORT_UNSUPPORTED = "unsupported"
SUPPORT_NOT_APPLICABLE = "not_applicable"

SUPPORT_STATES: Tuple[str, ...] = (SUPPORT_SUPPORTED, SUPPORT_UNSUPPORTED,
                                   SUPPORT_NOT_APPLICABLE)

#: Why a target is unsupported. Diagnostic, PHI-safe, never clinical prose.
REASON_NOT_MAPPABLE = "target_not_activity_mappable"
REASON_FAMILY_NOT_SERVED = "bound_family_not_served"


class ContentAuthorityError(Exception):
    """A content verdict could not be produced. PHI-safe: no child data."""

    PHI_SAFE_MESSAGE = True


class ContentAuthorityUnavailable(ContentAuthorityError):
    """No approved activity bank is wired. Generation must refuse."""


@dataclass(frozen=True)
class TargetSupport:
    """The Pilot's verdict on one canonical target, and the state it used."""

    state: str
    content_digest: str
    unsupported_families: Tuple[str, ...] = ()
    reason: Optional[str] = None

    @property
    def supported(self) -> bool:
        return self.state == SUPPORT_SUPPORTED

    def as_dict(self) -> dict:
        """PHI-safe. States, family identifiers and a digest."""
        return {
            "state": self.state,
            "content_digest": self.content_digest,
            "unsupported_families": list(self.unsupported_families),
            "reason": self.reason,
        }


class ActivityBankContentAuthority:
    """The content authority, backed by the approved activity bank.

    A thin read-only adapter, deliberately. It owns no data: every answer is
    derived from `served_families()` and stamped with `artifact_digest()`, so
    the authority and the bank cannot disagree.
    """

    def __init__(self, activity_bank: Any) -> None:
        if activity_bank is None:
            raise ContentAuthorityUnavailable(
                "a content authority requires an approved activity bank")
        self._bank = activity_bank

    @property
    def content_digest(self) -> str:
        # `artifact_digest` is a PROPERTY on the bank and `served_families` is
        # a METHOD. Read exactly as the bank declares them rather than
        # normalising here, so a change on the bank's side is a loud
        # AttributeError instead of a silently wrong verdict.
        return str(self._bank.artifact_digest)

    def support_for_target(self, canonical_rung: Any) -> TargetSupport:
        """Whether the approved bank can currently serve this exact target.

        EVERY bound family must be served, not merely one. A rung binding two
        families, one of which has content, would otherwise authorise a week
        that practises the half we happen to have while reporting the target as
        covered — the same all-families-served rule the release path enforces,
        applied early enough that no suggestion is written.
        """
        digest = self.content_digest
        if not getattr(canonical_rung, "is_activity_mappable", False):
            return TargetSupport(state=SUPPORT_UNSUPPORTED,
                                 content_digest=digest,
                                 reason=REASON_NOT_MAPPABLE)
        served = set(self._bank.served_families())
        # `family_ref` is the binding's family identifier; `ActivityFamilyBinding`
        # names it that, and the bank's `served_families()` uses the same
        # identifiers — which is what lets this compare directly rather than
        # through a translation nobody owns.
        bound = tuple(binding.family_ref
                      for binding in (canonical_rung.family_bindings or ()))
        missing = tuple(sorted(f for f in bound if f not in served))
        if missing:
            return TargetSupport(state=SUPPORT_UNSUPPORTED,
                                 content_digest=digest,
                                 unsupported_families=missing,
                                 reason=REASON_FAMILY_NOT_SERVED)
        return TargetSupport(state=SUPPORT_SUPPORTED, content_digest=digest)


__all__ = [
    "SUPPORT_SUPPORTED", "SUPPORT_UNSUPPORTED", "SUPPORT_NOT_APPLICABLE",
    "SUPPORT_STATES", "REASON_NOT_MAPPABLE", "REASON_FAMILY_NOT_SERVED",
    "ContentAuthorityError", "ContentAuthorityUnavailable",
    "TargetSupport", "ActivityBankContentAuthority",
]
