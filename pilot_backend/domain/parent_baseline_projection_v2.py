"""0.6A-1E — A2 v2: the minimum-necessary SKILL-LEVEL baseline projection.

v1 projects a month-level summary: seven fields, no per-skill evidence, with
`asked` explicitly forbidden. That was correct for what Parent v1 produced,
because Parent v1 only ever asked one skill per band — there was no sibling
evidence to carry.

Parent baseline v2 assesses every declared-track skill in a band, so there now
IS evidence worth projecting, and projecting only the month summary would throw
away the very thing the baseline was repaired to collect.

## v1 IS NOT MODIFIED

A separate record, a separate schema version, a separate collection. Every v1
projection stays readable on its original seven-field wire contract. The pilot's
codec refuses key-set mismatches in both directions, so adding fields to v1 in
place was never an option and is not attempted.

## v1 IS NEVER UPGRADED IN MEANING

`legacy_projection_has_skill_evidence` always answers False. A v1 projection
evidences what Parent v1 asked — at most one skill per band — and says nothing
about the siblings it skipped. Reading it as band-complete would reinstate
exactly the defect v2 exists to fix, so a future F-B must treat it as
"skill evidence unavailable / band incomplete" and must never fabricate v2
evidence from it.

## WHAT CROSSES, AND WHAT IS CONSUMED AT THE BOUNDARY

    crosses     canonical rung_ref · months · state
                per-band total_skills (Parent's own denominator)
    consumed    milestone prose, subdomain — used to RESOLVE the canonical
                identity at the boundary, then discarded
    never       raw caregiver answer · asked[] · Parent UID · child name ·
                chronological age · diagnosis · concerns · qna

Milestone text is the loudest omission and the most deliberate. It is needed to
find the canonical rung, and once found the `rung_ref` identifies it exactly —
so carrying the prose into the Pilot would duplicate clinical content for no
added precision, and would put free text where the pilot's logging and
projection guards are designed to assume there is none.

## WHY `total_skills` IS PERSISTED BUT COMPLETENESS IS NOT

Band completeness could be derived in the Pilot: the generated rung table knows
which declared-track rungs exist at each band. That derivation is REJECTED as
unsafe in one direction. If the Pilot's roster were ever SMALLER than Parent's —
a stale artifact, a narrowed track — then "assessed 3 of the 3 I know about"
would read as COMPLETE when Parent assessed four. Missing evidence would become
mastery, which is the one failure mode this whole slice exists to prevent.

So the denominator comes from the source of truth, as one integer per assessed
band. `assessment_complete` and `band_mastered` are then DERIVED from the
projected evidence against that denominator — no duplicated band object, no
booleans that can disagree with the evidence beside them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Tuple

from .entities import SCHEMA_VERSION, utc_now
from .identity_claims import key_digest
from .source_link import SourceSystem

#: The projection's wire schema. v1 is `parent-baseline-projection-v1` and is
#: untouched; this is a new, additive contract.
PROJECTION_SCHEMA_V2 = "parent-baseline-projection-v2"

#: A DISTINCT id prefix, so the two projection generations are never confusable
#: by id alone. v1 uses `pbpj_`.
PROJECTION_V2_ID_PREFIX = "pbp2_"

#: The Parent baseline version this projection accepts. A v1 baseline cannot
#: produce a v2 projection, and the check is explicit rather than implied by
#: the presence of skill evidence.
ACCEPTED_BASELINE_VERSION = "parent-2.4-functional-baseline-v2"

#: The month-level compatibility summary, unchanged from v1 so an existing
#: reader of those seven fields keeps working.
SUMMARY_FIELDS: Tuple[str, ...] = (
    "domain",
    "area_id",
    "entry_choice_id",
    "routing_anchor_months",
    "not_demonstrated_months",
    "status",
    "baseline_version",
)

#: The four assessed states, mirrored from Parent v2. `unassessed` is absent
#: here for the same reason it is absent there: it is the lack of a record.
PROJECTED_STATES: Tuple[str, ...] = (
    "demonstrated", "emerging", "not_demonstrated", "unknown")

#: States that make a skill a target candidate. `unknown` is NOT one — an
#: unanswerable question is not evidence a skill is absent.
UNRESOLVED_STATES: Tuple[str, ...] = ("emerging", "not_demonstrated")

#: Fields that must never appear in a v2 projection payload. Carried over from
#: v1 and EXTENDED with the v2-specific temptations: the milestone prose and
#: subdomain that the boundary consumes, and the raw answer.
FORBIDDEN_FIELDS: Tuple[str, ...] = (
    "child_id", "owner_uid", "uid", "external_owner_ref", "parent_uid",
    "child_name", "name", "chronological_months", "asked", "diagnosis",
    "diagnosis_or_condition", "concern", "qna", "schedules",
    "weekly_schedule", "activities", "activity_banks", "demonstrated_months",
    "entry_anchor_months", "dev_age", "brain_state",
    # v2 additions — consumed at the boundary, never persisted.
    "milestone", "milestone_text", "subdomain", "answer", "raw_answer",
    "skill_key", "question_id", "entry_choice_label",
)


def projection_v2_id_for(source_session_id: str, domain: str,
                         source_record_digest: str) -> str:
    """The DETERMINISTIC id, so an exact retry collides instead of duplicating.

    Mirrors v1's `projection_id_for` exactly — same `key_digest` NUL-joining,
    same three inputs — because the idempotency PROPERTY must be identical: the
    second create addresses the same document and a create-only repository
    refuses it. Only the prefix differs, so a v1 and a v2 projection of the same
    baseline are distinct documents rather than one overwriting the other.

    Note what is NOT in the key: the skill evidence. The digest of the Parent
    record already covers it, so CHANGED evidence yields a different digest,
    hence a different id, hence a new document — never a silent overwrite of the
    existing one.
    """
    return PROJECTION_V2_ID_PREFIX + key_digest(
        source_session_id, domain, source_record_digest)[:32]


class ProjectionV2Error(Exception):
    """A v2 projection could not be formed. PHI-safe: no child data."""

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class ProjectedSkillEvidence:
    """One assessed skill, in CANONICAL pilot identity. Immutable.

    Three fields and nothing else. `months` is retained despite being derivable
    from the rung, because band grouping is the pilot's most common read and
    re-resolving every ref to learn its band would make a simple grouping
    depend on the Gold Standard being loaded.
    """

    rung_ref: str
    months: int
    state: str

    def __post_init__(self) -> None:
        if not (self.rung_ref or "").strip():
            raise ProjectionV2Error("projected evidence requires a rung_ref")
        if not self.rung_ref.startswith("rung1:"):
            # The canonical scheme. Rejected rather than accepted loosely, so a
            # Parent-side identity cannot be smuggled through as a pilot ref.
            raise ProjectionV2Error(
                "projected evidence requires a canonical rung ref")
        if self.state not in PROJECTED_STATES:
            raise ProjectionV2Error(
                f"{self.state!r} is not a projected state; `unassessed` is the "
                "absence of a record")
        if not isinstance(self.months, int) or isinstance(self.months, bool):
            raise ProjectionV2Error("projected evidence requires integer months")

    @property
    def is_unresolved(self) -> bool:
        return self.state in UNRESOLVED_STATES


@dataclass(frozen=True)
class ProjectedBandTotal:
    """Parent's OWN count of declared-track skills in one assessed band.

    The denominator for completeness. One integer, from the source of truth,
    so the pilot never has to guess how many siblings it should have received.
    """

    months: int
    total_skills: int

    def __post_init__(self) -> None:
        if not isinstance(self.months, int) or isinstance(self.months, bool):
            raise ProjectionV2Error("a band total requires integer months")
        if not isinstance(self.total_skills, int) or self.total_skills <= 0:
            raise ProjectionV2Error("a band total requires a positive count")


@dataclass(frozen=True)
class ParentBaselineProjectionV2:
    """An immutable, create-only skill-level baseline projection."""

    projection_id: str
    child_id: str
    source_system: SourceSystem
    source_session_id: str
    source_record_digest: str
    # -- the v1 compatibility summary -----------------------------------
    domain: str
    area_id: str
    entry_choice_id: str
    status: str
    baseline_version: str
    routing_anchor_months: Optional[int]
    not_demonstrated_months: Optional[int]
    # -- the v2 addition -------------------------------------------------
    skill_evidence: Tuple[ProjectedSkillEvidence, ...] = ()
    band_totals: Tuple[ProjectedBandTotal, ...] = ()
    projected_at: datetime = field(default_factory=utc_now)
    schema_version: str = SCHEMA_VERSION
    projection_schema: str = PROJECTION_SCHEMA_V2

    def __post_init__(self) -> None:
        if not (self.projection_id or "").startswith(PROJECTION_V2_ID_PREFIX):
            raise ProjectionV2Error(
                "a v2 projection id must carry the v2 prefix")
        if len(self.source_record_digest or "") != 64:
            raise ProjectionV2Error(
                "source_record_digest must be a sha256 hex digest")
        expected = projection_v2_id_for(self.source_session_id, self.domain,
                                        self.source_record_digest)
        if self.projection_id != expected:
            # Recomputed and compared, so a hand-built projection cannot be
            # stored under an id that does not describe its own source.
            raise ProjectionV2Error(
                "the projection id is not derived from its source identity")
        if self.baseline_version != ACCEPTED_BASELINE_VERSION:
            raise ProjectionV2Error(
                "a v2 projection requires a v2 Parent baseline")
        if not self.skill_evidence:
            # A v2 baseline always assessed at least one skill. An empty list
            # would be indistinguishable from a v1 summary wearing a v2 label.
            raise ProjectionV2Error(
                "a v2 projection requires at least one assessed skill")
        refs = [e.rung_ref for e in self.skill_evidence]
        if len(set(refs)) != len(refs):
            # One Parent skill -> exactly one canonical rung. A duplicate means
            # two Parent skills collapsed, and one child's evidence would
            # overwrite the other's.
            raise ProjectionV2Error(
                "two projected skills share one canonical rung ref")
        bands = [b.months for b in self.band_totals]
        if len(set(bands)) != len(bands):
            raise ProjectionV2Error("duplicate band totals")
        covered = set(bands)
        for evidence in self.skill_evidence:
            if evidence.months not in covered:
                # Evidence for a band with no declared denominator would make
                # completeness unanswerable for that band.
                raise ProjectionV2Error(
                    "projected evidence names a band with no declared total")
        for band in self.band_totals:
            assessed = sum(1 for e in self.skill_evidence
                           if e.months == band.months)
            if assessed > band.total_skills:
                raise ProjectionV2Error(
                    "more skills were projected for a band than Parent "
                    "declared it contains")

    # -- derived band state ------------------------------------------------

    def _band(self, months: int) -> Optional[ProjectedBandTotal]:
        for band in self.band_totals:
            if band.months == months:
                return band
        return None

    def assessed_in_band(self, months: int
                         ) -> Tuple[ProjectedSkillEvidence, ...]:
        return tuple(e for e in self.skill_evidence if e.months == months)

    def assessment_complete(self, months: int) -> bool:
        """Every declared skill in the band has evidence.

        DERIVED against Parent's own `total_skills`, never against a pilot-side
        roster — see the module docstring on why that direction is unsafe. An
        assessed `unknown` counts: the question was put.
        """
        band = self._band(months)
        if band is None:
            return False
        return len(self.assessed_in_band(months)) == band.total_skills

    def band_mastered(self, months: int) -> bool:
        """Complete AND every skill demonstrated. Never one without the other."""
        if not self.assessment_complete(months):
            return False
        return all(e.state == "demonstrated"
                   for e in self.assessed_in_band(months))

    def unresolved_skills(self) -> Tuple[ProjectedSkillEvidence, ...]:
        """Every assessed-and-not-demonstrated skill, lowest band first.

        An `unknown` sibling is absent from this list and does not suppress
        anything in it: it prevents its band being called mastered, and nothing
        more. F-B v2 will consume this; it is not consumed here.
        """
        return tuple(sorted((e for e in self.skill_evidence if e.is_unresolved),
                            key=lambda e: (e.months, e.rung_ref)))

    @property
    def has_routing_anchor(self) -> bool:
        return self.routing_anchor_months is not None

    def summary(self) -> Dict[str, Any]:
        """The seven v1 compatibility fields, for a reader that wants only them."""
        return {name: getattr(self, name) for name in SUMMARY_FIELDS}

    @staticmethod
    def build(*, child_id: str, source_session_id: str,
              source_record_digest: str, summary: Mapping[str, Any],
              skill_evidence: Tuple[ProjectedSkillEvidence, ...],
              band_totals: Tuple[ProjectedBandTotal, ...],
              now: Optional[datetime] = None
              ) -> "ParentBaselineProjectionV2":
        """Construct a v2 projection from an already-canonicalized payload.

        The summary must carry EXACTLY the seven v1 fields — no more, so a
        forbidden field cannot ride along, and no fewer, so a partial summary
        cannot masquerade as complete.
        """
        present = set(summary)
        for forbidden in FORBIDDEN_FIELDS:
            if forbidden in present:
                raise ProjectionV2Error(
                    f"{forbidden!r} must never cross the projection boundary")
        unexpected = present - set(SUMMARY_FIELDS)
        if unexpected:
            raise ProjectionV2Error(
                f"unexpected summary fields: {sorted(unexpected)}")
        missing = set(SUMMARY_FIELDS) - present
        if missing:
            raise ProjectionV2Error(
                f"missing summary fields: {sorted(missing)}")
        return ParentBaselineProjectionV2(
            projection_id=projection_v2_id_for(
                source_session_id, summary["domain"], source_record_digest),
            child_id=child_id,
            source_system=SourceSystem.PARENT,
            source_session_id=source_session_id,
            source_record_digest=source_record_digest,
            skill_evidence=tuple(skill_evidence),
            band_totals=tuple(band_totals),
            projected_at=now or utc_now(),
            **{name: summary[name] for name in SUMMARY_FIELDS})


def legacy_projection_has_skill_evidence(projection: Any) -> bool:
    """Whether a v1 projection carries skill evidence. Always False.

    Stated as a function so the rule is testable and so a later reader cannot
    quietly treat a v1 projection as band-complete. A v1 projection is a
    month-level summary produced by a baseline that asked at most one skill per
    band; it is evidence about those questions and about nothing else.
    """
    schema = getattr(projection, "projection_schema", None)
    if schema == PROJECTION_SCHEMA_V2:
        raise ProjectionV2Error(
            "this helper answers for LEGACY v1 projections; a v2 projection "
            "should be asked about a specific band")
    return False
