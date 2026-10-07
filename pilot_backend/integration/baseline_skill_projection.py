"""0.6A-1E — the A2 v2 canonicalisation boundary.

Converts Parent v2 skill evidence into `ParentBaselineProjectionV2`. This is the
ONE place a Parent source identity becomes a pilot canonical identity.

## WHY THE CONVERSION LIVES HERE AND NOT IN PARENT

`compute_rung_ref` is the pilot's identity scheme. Parent cannot import it —
`pilot_backend` is not importable from `genex-parent`, and the dependency must
stay one-way or there would be two implementations of one identity and an
eventual drift. So Parent stores its own source identity
`(domain, subdomain, months, milestone)` and this boundary resolves it.

## ONE PARENT SKILL -> EXACTLY ONE CANONICAL RUNG

Enforced, not hoped for. The resolver refuses zero matches and refuses multiple
matches; this module additionally refuses a subdomain mismatch and a duplicate
canonical ref across the batch. Nothing is selected alphabetically, nothing
takes the first match, and nothing is silently dropped — a skill that cannot be
resolved fails the whole projection.

Dropping would be the worst option available: the projection would look
complete while being quietly smaller than the assessment, and the pilot would
then compute band completeness against a denominator it never received.

## CANONICALISABLE IS NOT ACTIVITY-MAPPABLE

Resolution goes through `canonical_identity`, which answers for every rung the
Gold Standard names — including the four declared-track rungs whose activity
families the taxonomy has not reconciled (`pronouns`, `wh_question_asking`,
`expressive_name_response`, and the 4m sound-response rung).

That is deliberate. A baseline skill is real evidence about a child whether or
not anyone has written activities for it. Requiring mappability here would make
the projection silently forget that a clinician's question was ever asked, and
`not_demonstrated` pronouns would vanish rather than being available to a later
clinical decision. Whether an unresolved skill can become an ANCHORED suggestion
is F-B's question, asked later, with mappability as its own gate.

## MILESTONE PROSE IS CONSUMED, NOT FORWARDED

The text is an input to resolution and nothing else. Once a `rung_ref` exists it
identifies the skill exactly, so forwarding the prose would duplicate clinical
content and put free text into a projection whose guards assume none.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..domain.parent_baseline_projection_v2 import (
    ACCEPTED_BASELINE_VERSION,
    SUMMARY_FIELDS,
    ParentBaselineProjectionV2,
    ProjectedBandTotal,
    ProjectedSkillEvidence,
    ProjectionV2Error,
)


class SkillCanonicalisationError(ProjectionV2Error):
    """A Parent skill could not be canonicalised. The projection is refused."""


#: The exact fields this boundary CONSUMES from each Parent skill record. Named
#: so a caller cannot pass a whole Parent record and have extra content ride
#: along into the resolver.
CONSUMED_SKILL_FIELDS: Tuple[str, ...] = (
    "domain", "subdomain", "months", "milestone", "state")


def _consume(skill: Any) -> Dict[str, Any]:
    """Read exactly the five fields needed, from an object or a mapping."""
    out: Dict[str, Any] = {}
    for name in CONSUMED_SKILL_FIELDS:
        if isinstance(skill, dict):
            if name not in skill:
                raise SkillCanonicalisationError(
                    f"a Parent skill record requires {name}")
            out[name] = skill[name]
        else:
            if not hasattr(skill, name):
                raise SkillCanonicalisationError(
                    f"a Parent skill record requires {name}")
            out[name] = getattr(skill, name)
    return out


def canonicalise_skills(rung_source: Any, skills: Iterable[Any]
                        ) -> Tuple[ProjectedSkillEvidence, ...]:
    """Resolve every Parent skill to its canonical pilot identity, or refuse.

    Ordered by `(months, rung_ref)` so the projection is deterministic and two
    projections of one baseline compare equal.
    """
    resolved: List[ProjectedSkillEvidence] = []
    seen: Dict[str, int] = {}
    for skill in skills:
        fields = _consume(skill)
        months = int(fields["months"])
        try:
            rung_ref, canonical_subdomain = rung_source.canonical_identity(
                fields["domain"], months, fields["milestone"])
        except Exception as exc:
            # Zero matches, several matches, wrong domain, bad months. Every
            # case refuses the WHOLE projection rather than this one skill.
            raise SkillCanonicalisationError(
                "a Parent skill could not be resolved to exactly one "
                "canonical rung") from exc

        parent_subdomain = str(fields["subdomain"] or "").strip()
        if parent_subdomain != canonical_subdomain:
            # The canonical ref deliberately excludes subdomain, so a relabel
            # would otherwise map silently to the right rung with the wrong
            # clinical grouping. Checked here so it fails loudly instead.
            raise SkillCanonicalisationError(
                "a Parent skill's subdomain disagrees with the canonical rung")

        if rung_ref in seen:
            raise SkillCanonicalisationError(
                "two Parent skills resolved to one canonical rung")
        seen[rung_ref] = months

        resolved.append(ProjectedSkillEvidence(
            rung_ref=rung_ref, months=months, state=str(fields["state"])))
    if not resolved:
        raise SkillCanonicalisationError(
            "a v2 projection requires at least one assessed skill")
    return tuple(sorted(resolved, key=lambda e: (e.months, e.rung_ref)))


def band_totals_from_parent(totals: Sequence[Tuple[int, int]]
                            ) -> Tuple[ProjectedBandTotal, ...]:
    """Parent's own per-band declared-track counts, as the denominator.

    Taken from Parent rather than counted on the pilot side — see the
    `parent_baseline_projection_v2` docstring on why a pilot-side roster is
    unsafe in the direction that matters.
    """
    return tuple(sorted((ProjectedBandTotal(months=int(m), total_skills=int(n))
                         for m, n in totals),
                        key=lambda b: b.months))


def verify_band_denominators(rung_source: Any,
                             evidence: Sequence[ProjectedSkillEvidence],
                             totals: Sequence[ProjectedBandTotal],
                             *, domain: str) -> None:
    """Check Parent's denominator against the CANONICAL declared-track roster.

    0.6A-1F. Parent's `total_skills` is no longer trusted on its own.

    ## The attack this closes

    The canonical 30-month band holds four declared-track skills. A baseline
    could send `total_skills=3` with three genuinely valid 30-month rows, and
    the derived `assessment_complete(30)` would be TRUE while a fourth declared
    skill had never been asked. Missing evidence would become mastery — the one
    failure this whole repair exists to prevent, reintroduced through the
    denominator instead of the questioning.

    A wrong-in-the-other-direction claim is refused too: `total_skills=5` would
    make a complete band look incomplete and silently suppress a real target.

    ## Count is not enough; MEMBERSHIP is checked

    A band of the right SIZE made of the wrong skills is still wrong. Evidence
    refs must be a subset of that band's exact canonical roster, so a 30-month
    row carrying a 36-month rung's ref cannot pad the count.

    The roster comes from `declared_band_roster`, i.e. the same frozen artifact
    that canonicalised the evidence — so the identities and the count cannot
    come from two sources that disagree.
    """
    by_month: Dict[int, ProjectedBandTotal] = {}
    for band in totals:
        if band.months in by_month:
            raise SkillCanonicalisationError(
                "two band-total entries name the same band")
        by_month[band.months] = band

    evidence_months = {item.months for item in evidence}
    missing = sorted(evidence_months - set(by_month))
    if missing:
        raise SkillCanonicalisationError(
            "an evidence-bearing band has no band-total declaration")

    for months, band in sorted(by_month.items()):
        try:
            roster = set(rung_source.declared_band_roster(domain, months))
        except Exception as exc:
            raise SkillCanonicalisationError(
                "the canonical declared-track roster for a band could not be "
                "resolved") from exc
        if not roster:
            raise SkillCanonicalisationError(
                "a declared band total names a band with no canonical skills")
        if band.total_skills != len(roster):
            # Too low AND too high are both refused: one manufactures mastery,
            # the other hides a target.
            raise SkillCanonicalisationError(
                "Parent's declared band total disagrees with the canonical "
                "declared-track roster")
        refs = [item.rung_ref for item in evidence if item.months == months]
        if len(set(refs)) != len(refs):
            raise SkillCanonicalisationError(
                "the same canonical skill appears twice in one band")
        stray = sorted(set(refs) - roster)
        if stray:
            raise SkillCanonicalisationError(
                "a projected skill is not a member of its canonical band")


def canonicalise_and_verify(*, rung_source: Any, summary: Dict[str, Any],
                            skills: Iterable[Any],
                            band_totals: Sequence[Tuple[int, int]]
                            ) -> Tuple[Tuple[ProjectedSkillEvidence, ...],
                                       Tuple[ProjectedBandTotal, ...]]:
    """Everything the boundary can decide WITHOUT touching persistence.

    0.6A-1F. Split out of `project_baseline_v2` so the accepting service can run
    the whole shape check BEFORE its first repository read.

    That ordering is a security property, not a performance one, and it is the
    rule v1's service already follows: if a malformed payload could cause a
    repository read, a caller could use validation failures to probe which
    sessions exist. Building the projection needs a `child_id`, which only a
    repository read can supply — so the pure part has to be separable, or the
    check order would have to be given up.

    Returns `(evidence, totals)`. Raises on any refusal; there is no partial
    success, because a projection smaller than the assessment is the one outcome
    worse than no projection at all.
    """
    if summary.get("baseline_version") != ACCEPTED_BASELINE_VERSION:
        raise SkillCanonicalisationError(
            "only a v2 Parent baseline can produce a v2 projection")
    missing = set(SUMMARY_FIELDS) - set(summary)
    if missing:
        raise SkillCanonicalisationError(
            f"the summary is missing: {sorted(missing)}")

    evidence = canonicalise_skills(rung_source, skills)
    totals = band_totals_from_parent(band_totals)
    verify_band_denominators(rung_source, evidence, totals,
                             domain=summary["domain"])
    return evidence, totals


def project_baseline_v2(*, rung_source: Any, child_id: str,
                        source_session_id: str, source_record_digest: str,
                        summary: Dict[str, Any], skills: Iterable[Any],
                        band_totals: Sequence[Tuple[int, int]],
                        now: Optional[Any] = None
                        ) -> ParentBaselineProjectionV2:
    """The whole boundary: canonicalise, then build an immutable projection.

    Refuses a non-v2 baseline explicitly rather than inferring it from the
    presence of skill evidence, so a v1 summary cannot be dressed up as v2.

    Delegates the pure half to `canonicalise_and_verify`, so this function and
    the accepting service share ONE implementation of every check rather than
    two that could drift.
    """
    evidence, totals = canonicalise_and_verify(
        rung_source=rung_source, summary=summary, skills=skills,
        band_totals=band_totals)
    return ParentBaselineProjectionV2.build(
        child_id=child_id, source_session_id=source_session_id,
        source_record_digest=source_record_digest, summary=summary,
        skill_evidence=evidence, band_totals=totals, now=now)
