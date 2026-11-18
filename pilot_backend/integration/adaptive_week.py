"""pilot_backend/integration/adaptive_week.py — P0-B1 reviewed-bank selection.

Pure. Given the P0-B1 decisions for last week's activities, the reviewed
templates the goal's anchor permits, and how many activities the week can hold,
choose WHICH reviewed templates Week N+1 schedules. Nothing here writes, and
nothing here can reach a template outside the pool it is handed.

## The rules, in order

1. NO_CHANGE activities are CARRIED: the same reviewed template again. A
   not-yet-supported trigger is recorded for a clinician, never "adapted".
2. FRESH templates fill the remaining slots: reviewed, inside the anchor's
   families, NOT scheduled last week. When any CHANGE_THEME decision fired,
   every fresh template must also carry a theme NOT used last week.
3. If slots remain, MAINTAIN_VARY templates from last week are REUSED. Only
   then: variation prefers an unused alternative and repeats only when none
   exists. A CHANGE_THEME template is never reused.
4. A CHANGE_THEME decision that found NO fresh template with a new theme is a
   REFUSAL, not a quiet repeat of the activity the child disengaged from.

Every ordering is by `activity_template_id`, a content digest. There is no
difficulty ordering — the reviewed source has none, and inventing one would be
an unreviewed clinical judgement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

from ..domain.activity_template import ADMITTED_SOURCE_TIERS, ActivityTemplate
from ..domain.adaptation_decision import AdaptationBranch


class AdaptiveSelectionError(ValueError):
    """Week N+1 content could not be chosen inside the boundary. PHI-safe.

    A plain ValueError family on purpose: this module is pure and must not
    import the release service. The release composition translates it into a
    `WeekOneReleaseError`, which the route renders as its constant refusal.
    """

    PHI_SAFE_MESSAGE = True


class AdaptiveSelectionRefused(AdaptiveSelectionError):
    """No compliant selection exists (e.g. no new theme is available)."""


class ClinicalBoundaryViolation(AdaptiveSelectionError):
    """A selection would leave reviewed content or the anchor's families."""


def _theme_key(theme: str) -> str:
    return " ".join((theme or "").split()).casefold()


@dataclass(frozen=True)
class WeekSelection:
    """Which reviewed templates Week N+1 will offer the allocator."""

    selected_template_ids: Tuple[str, ...]
    carried_template_ids: Tuple[str, ...]
    fresh_template_ids: Tuple[str, ...]
    reused_template_ids: Tuple[str, ...]
    excluded_themes: Tuple[str, ...]

    @property
    def repeated_template_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(set(self.carried_template_ids)
                            | set(self.reused_template_ids)))


def select_week_templates(*, decisions: Sequence, previous_template_ids:
                          Iterable[str], pool: Sequence[ActivityTemplate],
                          slots: int) -> WeekSelection:
    """Choose Week N+1's reviewed templates. Deterministic, pure, fail closed."""
    if slots <= 0:
        raise AdaptiveSelectionRefused("the week has no capacity to fill")
    by_id: Mapping[str, ActivityTemplate] = {
        t.activity_template_id: t for t in pool}
    previous = sorted(set(previous_template_ids))
    missing = [ref for ref in previous if ref not in by_id]
    if missing:
        # Last week's content is no longer in the reviewed pool for these
        # families. Refused: the theme rule cannot be evaluated against content
        # we cannot see, and carrying it would schedule withdrawn content.
        raise AdaptiveSelectionRefused(
            "last week's reviewed content is not in the current pool")

    branch_by_ref: Dict[str, AdaptationBranch] = {
        d.activity_identity_ref: d.branch for d in decisions}
    unknown = sorted(set(branch_by_ref) - set(previous))
    if unknown:
        raise AdaptiveSelectionRefused(
            "a decision names an activity last week did not schedule")
    for ref in previous:
        branch_by_ref.setdefault(ref, AdaptationBranch.MAINTAIN_VARY)

    change_theme = sorted(r for r, b in branch_by_ref.items()
                          if b is AdaptationBranch.CHANGE_THEME)
    excluded = sorted({_theme_key(by_id[r].theme) for r in previous}) \
        if change_theme else []

    carried = sorted(r for r, b in branch_by_ref.items()
                     if b is AdaptationBranch.NO_CHANGE)[:slots]
    chosen: List[str] = list(carried)

    fresh_pool = [t.activity_template_id for t in
                  sorted(pool, key=lambda t: t.activity_template_id)
                  if t.activity_template_id not in previous
                  and (not change_theme
                       or _theme_key(t.theme) not in excluded)]
    fresh = fresh_pool[:max(0, slots - len(chosen))]
    chosen.extend(fresh)

    if change_theme and not fresh:
        raise AdaptiveSelectionRefused(
            "no reviewed activity with a theme not used last week exists")

    reused: List[str] = []
    if len(chosen) < slots:
        reusable = sorted(r for r, b in branch_by_ref.items()
                          if b is AdaptationBranch.MAINTAIN_VARY)
        reused = reusable[:slots - len(chosen)]
        chosen.extend(reused)

    return WeekSelection(
        selected_template_ids=tuple(sorted(chosen)),
        carried_template_ids=tuple(carried),
        fresh_template_ids=tuple(sorted(fresh)),
        reused_template_ids=tuple(sorted(reused)),
        excluded_themes=tuple(excluded))


def verify_reviewed_and_bounded(selected_ids: Iterable[str],
                                pool: Sequence[ActivityTemplate],
                                allowed_families: Iterable[str]) -> None:
    """The pre-write content gate. Raises on the first violation.

    Every selected id must be a template the reviewed bank served for the
    anchor's families, with an admitted curated tier, in an allowed family.
    `ActivityTemplate` already refuses a forbidden tier or a tampered id at
    construction; this re-checks at the boundary so a future pool source that
    skipped construction could not slip one through.
    """
    by_id = {t.activity_template_id: t for t in pool}
    allowed = set(allowed_families)
    for ref in selected_ids:
        template = by_id.get(ref)
        if template is None:
            raise ClinicalBoundaryViolation(
                "a selected activity is not reviewed bank content")
        if template.source_tier not in ADMITTED_SOURCE_TIERS:
            raise ClinicalBoundaryViolation(
                "a selected activity is not an admitted reviewed tier")
        if template.activity_family_ref not in allowed:
            raise ClinicalBoundaryViolation(
                "a selected activity is outside the goal's approved families")


__all__ = [
    "AdaptiveSelectionError",
    "AdaptiveSelectionRefused",
    "ClinicalBoundaryViolation",
    "WeekSelection",
    "select_week_templates",
    "verify_reviewed_and_bounded",
]
