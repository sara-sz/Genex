"""Parent functional baseline v2 — band-complete, skill-level assessment.

## WHY v2 EXISTS

v1 is a binary search over MONTHS. `next_question` keeps
`asked_months = {a["months"] for a in record.asked}` and refuses to revisit a
month, so ONE answer retires a whole band; with `MAX_QUESTIONS = 4` for an
entire domain it cannot do otherwise. Measured on the real ladder: a child who
answers yes four times is anchored at 48 months with SIX declared-track skills
never asked, including pronouns, 50-word vocabulary and four-word sentences.

That contradicts the governing product rule. A month is a routing BAND, not a
developmental age, and skills inside a band are independent: demonstrating
two-word combination says nothing about pronouns at the same band.

v2 therefore assesses every declared-track skill in a band before deciding
whether to move, and keeps each skill's evidence separately.

## v1 IS NOT MODIFIED

This is a separate module and a separate version string. `functional_baseline`
is untouched, every v1 record stays readable, and `legacy_record_is_band_complete`
answers the only question that matters about one: NO. A v1 record proves what it
asked and nothing about the siblings it skipped, so it must never be read as
evidence that an unasked skill was mastered.

## UNASSESSED IS NOT A STATE

The four assessed states are `demonstrated`, `emerging`, `not_demonstrated`,
`unknown`. `unknown` means the question WAS put to the caregiver and they could
not answer — that is evidence about the asking, and it counts toward assessment
completeness. `unassessed` is the ABSENCE of a `BaselineSkillEvidence` record.

Collapsing the two would destroy provenance: "we asked and nobody knows" and
"we never asked" lead to different clinical next steps, and only the first can
honestly be shown to a clinician as a gap in the child's profile rather than a
gap in ours.

## ASSESSMENT COMPLETENESS AND MASTERY ARE DIFFERENT QUESTIONS

    assessment_complete   every skill in the band has an evidence record
    band_mastered         every skill in the band is `demonstrated`

    all demonstrated          -> complete,     mastered
    one not_demonstrated      -> complete, NOT mastered
    one emerging              -> complete, NOT mastered
    one unknown               -> complete, NOT mastered   (and not refuted)
    one unassessed            -> INCOMPLETE, not mastered

One boolean cannot carry both. A band with an unknown has been fully assessed
and is still not mastered; a band with an unasked skill has not been assessed at
all, and advancing past it would be the v1 defect again.

## IDENTITY: THE CANONICAL TRIPLE, NOT A TRUNCATED STRING

v1's `question_id` embeds `milestone[:48]`, so two milestones sharing a 48-char
prefix share an id. v2 stores the exact three fields the pilot's frozen
`compute_rung_ref` hashes — `domain`, `months`, `milestone` (FULL text) — and
`skill_key()` joins them with a separator that cannot occur in the data.

The canonical `rung_ref` itself is deliberately NOT computed here.
`compute_rung_ref` lives in `pilot_backend`, which is not importable from
`genex-parent` and must not be: the Pilot depends on Parent, never the reverse.
Re-implementing the hash here would create exactly the mirror the architecture
exists to prevent. A later A2 projection derives the ref from this triple using
the one frozen implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import functional_baseline as v1

#: A new version string. v1 semantics are never mutated in place, and this value
#: travels with the record so a reader always knows which rules produced it.
BASELINE_VERSION_V2 = "parent-2.4-functional-baseline-v2"

#: The four ASSESSED states. `unassessed` is deliberately absent — it is the
#: absence of a record, not a value one can store.
STATE_DEMONSTRATED = "demonstrated"
STATE_EMERGING = "emerging"
STATE_NOT_DEMONSTRATED = "not_demonstrated"
STATE_UNKNOWN = "unknown"

ASSESSED_STATES: Tuple[str, ...] = (
    STATE_DEMONSTRATED, STATE_EMERGING, STATE_NOT_DEMONSTRATED, STATE_UNKNOWN)

#: States that make a skill a legitimate clinical target. `unknown` is NOT one:
#: an unanswerable question is not evidence that the skill is absent.
UNRESOLVED_STATES: Tuple[str, ...] = (STATE_EMERGING, STATE_NOT_DEMONSTRATED)

#: Band budget. v1 capped QUESTIONS for a whole domain at 4, which made
#: band-complete assessment arithmetically impossible — 30 months alone has four
#: declared-track skills. v2 caps BANDS instead and always finishes a band it
#: has entered, so cost scales with how many bands are needed rather than
#: truncating the one band that matters.
MAX_BANDS = 3

#: Separator for `skill_key`. ASCII unit separator cannot occur in milestone
#: text, which makes the joined key injective.
_UNIT_SEP = "\x1f"


class BaselineV2Error(Exception):
    """A v2 baseline could not proceed. PHI-safe: no child data in messages."""

    PHI_SAFE_MESSAGE = True


def skill_key(domain: str, subdomain: str, months: int,
              milestone: str) -> str:
    """The complete SOURCE identity of one Parent baseline skill.

    Four fields, joined with a separator that cannot occur in the data.

    ## Why subdomain is present even though it adds no uniqueness

    Measured over the frozen workbook: 369 rows collapse to 163 distinct
    `(domain, months, milestone)` rungs, and ZERO of those rungs carries more
    than one subdomain — subdomain is functionally determined. So the three-field
    key is already unique and the four-field key yields the same 163 keys.

    It is included because it makes the key SELF-VALIDATING at the A2 boundary.
    A2 must fail closed on a mismatched subdomain, and a key that carries the
    subdomain lets that check compare like with like instead of trusting a
    separate field to have travelled with the right skill.

    ## It is deliberately NOT a hash, and NOT the pilot's rung_ref

    `compute_rung_ref` lives in `pilot_backend`, which is not importable from
    `genex-parent` and must not be: the Pilot depends on Parent, never the
    reverse. Copying the hash here would create two implementations of one
    identity and an eventual drift. Parent therefore stores its own source
    identity and A2 converts.

    Note that `compute_rung_ref` deliberately EXCLUDES subdomain, for the same
    functional-determination reason plus robustness to a relabel. That asymmetry
    is intentional and safe: A2 resolves on `(domain, months, milestone)` and
    then VERIFIES subdomain, so a relabel fails loudly rather than silently
    mapping to the wrong rung.
    """
    return _UNIT_SEP.join((
        (domain or "").strip(),
        (subdomain or "").strip(),
        str(int(months)),
        " ".join((milestone or "").split()),
    ))


@dataclass(frozen=True)
class BaselineSkillEvidence:
    """One assessed skill. Immutable; a re-answer replaces the record.

    Carries the canonical identity triple plus the classified state, and
    nothing else. In particular NO raw caregiver answer: `state` is the
    classification, the raw string adds no clinical information, and keeping it
    out means a later minimum-necessary projection has nothing to strip.
    """

    domain: str
    months: int
    milestone: str
    subdomain: str
    state: str

    def __post_init__(self) -> None:
        if not (self.subdomain or "").strip():
            # Required because the key includes it: a blank subdomain would
            # make two different skills collapse onto one key.
            raise BaselineV2Error("skill evidence requires a subdomain")
        if self.state not in ASSESSED_STATES:
            raise BaselineV2Error(
                f"{self.state!r} is not an assessed state; `unassessed` is the "
                "absence of a record, never a value")
        if not (self.domain or "").strip():
            raise BaselineV2Error("skill evidence requires a domain")
        if not (self.milestone or "").strip():
            raise BaselineV2Error("skill evidence requires a milestone")
        if not isinstance(self.months, int) or isinstance(self.months, bool):
            raise BaselineV2Error("skill evidence requires integer months")

    @property
    def key(self) -> str:
        return skill_key(self.domain, self.subdomain, self.months,
                         self.milestone)

    @property
    def is_unresolved(self) -> bool:
        """A legitimate target: assessed, and not demonstrated."""
        return self.state in UNRESOLVED_STATES


@dataclass
class BandAssessment:
    """What is known about ONE band. Derived, never stored as truth."""

    months: int
    total_skills: int
    assessed: Tuple[BaselineSkillEvidence, ...]

    @property
    def assessment_complete(self) -> bool:
        """Every skill in the band has an evidence record.

        An assessed `unknown` COUNTS as assessed — the question was asked.
        """
        return len(self.assessed) >= self.total_skills > 0

    @property
    def band_mastered(self) -> bool:
        """Every skill is demonstrated. Requires completeness first."""
        return (self.assessment_complete
                and all(e.state == STATE_DEMONSTRATED for e in self.assessed))

    @property
    def unresolved(self) -> Tuple[BaselineSkillEvidence, ...]:
        """Assessed-and-not-demonstrated skills: the target candidates."""
        return tuple(e for e in self.assessed if e.is_unresolved)

    @property
    def unknown(self) -> Tuple[BaselineSkillEvidence, ...]:
        return tuple(e for e in self.assessed if e.state == STATE_UNKNOWN)

    @property
    def unassessed_count(self) -> int:
        return max(0, self.total_skills - len(self.assessed))


@dataclass
class BaselineRecordV2:
    """A skill-level baseline. The month fields are DERIVED, not primary."""

    baseline_version: str
    area_id: str
    domain: str
    entry_choice_id: str
    entry_anchor_months: Optional[int]
    chronological_months: int
    #: Per-skill evidence, keyed by the canonical triple. The ONLY place
    #: assessment state lives.
    skills: Dict[str, BaselineSkillEvidence] = field(default_factory=dict)
    #: Bands entered, in order, so a reader can see what was attempted.
    bands_entered: List[int] = field(default_factory=list)
    status: str = "IN_PROGRESS"
    #: Derived in `finalize_v2` for backward compatibility ONLY.
    routing_anchor_months: Optional[int] = None
    demonstrated_months: Optional[int] = None
    not_demonstrated_months: Optional[int] = None

    # -- track ------------------------------------------------------------

    def track(self) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
        area = v1.get_area(self.area_id)
        choice = area.choice(self.entry_choice_id)
        return area.track_subdomains, choice.track_families


def band_skill_rows(record: BaselineRecordV2, months: int
                    ) -> Tuple[Dict[str, Any], ...]:
    """EVERY declared-track skill at this band, deterministically ordered.

    This is the function v1 lacks. `question_at` returns ONE row for a month;
    this returns all of them, which is what makes band-complete assessment
    possible.

    Ordered by `(subdomain preference, milestone)` purely so the UX presents
    questions in a stable sequence. That ordering decides nothing clinical:
    every row is asked, so no row can stand for the band, and no row can be
    skipped because of where it sorts. The v1 defect was not the sort — it was
    taking `candidates[0]` and discarding the rest.
    """
    subdomains, families = record.track()
    rows = [r for r in v1._rows_for_domain(record.domain, subdomains, families)
            if r["months"] == months]
    return tuple(sorted(rows, key=lambda r: (
        v1._preference_rank(record.domain, r["subdomain"]), r["milestone"])))


def band_assessment(record: BaselineRecordV2, months: int) -> BandAssessment:
    """What is known about one band, derived from skill evidence."""
    rows = band_skill_rows(record, months)
    assessed = []
    for row in rows:
        key = skill_key(record.domain, row["subdomain"], months,
                        row["milestone"])
        evidence = record.skills.get(key)
        if evidence is not None:
            assessed.append(evidence)
    return BandAssessment(months=months, total_skills=len(rows),
                          assessed=tuple(assessed))


def ladder(record: BaselineRecordV2) -> Tuple[int, ...]:
    """The declared-track band months, ascending."""
    subdomains, families = record.track()
    return tuple(sorted({r["months"] for r in v1._rows_for_domain(
        record.domain, subdomains, families)}))


def start_baseline_v2(area_id: str, choice_id: str,
                      chronological_months: int) -> BaselineRecordV2:
    """Begin a v2 baseline at the band the entry choice selects.

    The anchor chooses WHERE TO START ASKING. It is not evidence, and nothing
    below it is assumed demonstrated. For `not_sure` the chronological age is
    used purely as a routing fallback — the most neutral place to begin when the
    caregiver cannot describe the child's speech — and still asserts nothing
    about ability.
    """
    area = v1.get_area(area_id)
    choice = area.choice(choice_id)
    record = BaselineRecordV2(
        baseline_version=BASELINE_VERSION_V2,
        area_id=area_id,
        domain=area.domain,
        entry_choice_id=choice_id,
        entry_anchor_months=choice.anchor_months,
        chronological_months=int(chronological_months),
    )
    return record


def _entry_band(record: BaselineRecordV2) -> Optional[int]:
    start = record.entry_anchor_months
    if start is None:
        start = record.chronological_months
    subdomains, families = record.track()
    return v1._nearest_rung(record.domain, start, subdomains, families)


def next_question_v2(record: BaselineRecordV2) -> Optional[Dict[str, Any]]:
    """The next question, or None when the baseline is bounded.

    The algorithm, in the order the checks run:

      1. no band entered yet        -> enter the entry band
      2. current band incomplete    -> ask its next UNASSESSED skill
      3. current band mastered      -> enter exactly ONE band up
      4. current band complete but not mastered
             entry band and no fully demonstrated band below
                                    -> descend exactly ONE band
             otherwise              -> STOP, the bracket is established
      5. band budget exhausted      -> STOP

    Step 2 is the whole correction: a band is finished before any vertical
    movement, so a demonstrated skill can never carry its siblings with it and
    an unknown can never advance the ladder.
    """
    if not record.bands_entered:
        band = _entry_band(record)
        if band is None:
            return None
        record.bands_entered.append(band)
    current = record.bands_entered[-1]

    # 2. finish the band first.
    assessment = band_assessment(record, current)
    if not assessment.assessment_complete:
        for row in band_skill_rows(record, current):
            key = skill_key(record.domain, row["subdomain"], current,
                            row["milestone"])
            if key not in record.skills:
                return _question(record, current, row)
        return None  # pragma: no cover - completeness implies a row was found

    if len(record.bands_entered) >= MAX_BANDS:
        return None

    rungs = ladder(record)
    higher = [m for m in rungs if m > current]
    lower = [m for m in rungs if m < current]

    # 3. mastered -> exactly one band up.
    if assessment.band_mastered:
        if not higher:
            return None
        nxt = higher[0]
        if nxt in record.bands_entered:
            return None
        record.bands_entered.append(nxt)
        return next_question_v2(record)

    # 4. complete but not mastered.
    #
    # Descend only to establish solid ground below the first deficit, and only
    # while no band below has been shown fully demonstrated. Ascending is over:
    # an unresolved skill here is a legitimate target and must not be stepped
    # past.
    confirmed_below = any(
        band_assessment(record, m).band_mastered
        for m in record.bands_entered if m < current)
    if confirmed_below or not lower:
        return None
    nxt = max(lower)
    if nxt in record.bands_entered:
        return None
    record.bands_entered.append(nxt)
    return next_question_v2(record)


def _question(record: BaselineRecordV2, months: int,
              row: Dict[str, Any]) -> Dict[str, Any]:
    """One question, identified by the canonical triple rather than a string.

    `skill_key` replaces v1's `question_id`, which embedded `milestone[:48]` and
    so could collide between two milestones sharing a prefix.
    """
    return {
        "skill_key": skill_key(record.domain, row["subdomain"], months,
                               row["milestone"]),
        "baseline_version": BASELINE_VERSION_V2,
        "domain": record.domain,
        "months": months,
        "milestone": row["milestone"],
        "subdomain": row["subdomain"],
        "activity_family": row["activity_family"],
        "parent_explanation": row["parent_explanation"],
    }


def record_answer_v2(record: BaselineRecordV2, question: Dict[str, Any],
                     answer: str) -> BaselineRecordV2:
    """Record one skill's evidence. Replaces any earlier answer for that skill."""
    state = v1._classify(answer)
    evidence = BaselineSkillEvidence(
        domain=question["domain"],
        months=int(question["months"]),
        milestone=question["milestone"],
        subdomain=question.get("subdomain", ""),
        state=state,
    )
    record.skills[evidence.key] = evidence
    return record


def finalize_v2(record: BaselineRecordV2) -> BaselineRecordV2:
    """Derive the month-level fields FROM skill evidence, for compatibility.

    These three fields exist because A2 and the frozen pilot projection consume
    them. They are explicitly NOT the primary truth: `record.skills` is. Each is
    derived so it cannot disagree with the evidence.

      demonstrated_months      highest band that is complete AND mastered
      not_demonstrated_months  lowest band holding an assessed unresolved skill
      routing_anchor_months    == demonstrated_months

    A band that is merely complete does not count as demonstrated, and an
    incompletely assessed band counts as neither — which is what stops an
    unasked sibling from being read as mastery.
    """
    mastered = [m for m in record.bands_entered
                if band_assessment(record, m).band_mastered]
    deficits = [m for m in record.bands_entered
                if band_assessment(record, m).unresolved]
    record.demonstrated_months = max(mastered) if mastered else None
    record.not_demonstrated_months = min(deficits) if deficits else None
    record.routing_anchor_months = record.demonstrated_months

    if record.demonstrated_months is None and \
            record.not_demonstrated_months is None:
        record.status = "UNRESOLVED"
    elif record.demonstrated_months is not None and \
            record.not_demonstrated_months is not None:
        record.status = ("CONTRADICTORY"
                         if record.not_demonstrated_months <= record.demonstrated_months
                         else "BOUNDED")
    elif record.not_demonstrated_months is not None:
        record.status = "EMERGING"
    else:
        record.status = "AGE_RELEVANT"
    return record


def unresolved_skills(record: BaselineRecordV2
                      ) -> Tuple[BaselineSkillEvidence, ...]:
    """Every assessed-and-not-demonstrated skill, lowest band first.

    This is what a later F-B v2 should select targets from. Returning ALL of
    them rather than one is deliberate: a band with two deficits is two clinical
    facts, and choosing between them is a clinician's decision.

    An `unknown` sibling does not appear here and does not suppress anything
    here either — it prevents the band being called mastered, and nothing more.
    """
    out = [e for e in record.skills.values() if e.is_unresolved]
    return tuple(sorted(out, key=lambda e: (e.months, e.milestone)))


def unassessed_skills_in_band(record: BaselineRecordV2, months: int
                              ) -> Tuple[Dict[str, Any], ...]:
    """Band skills with NO evidence record — distinct from `unknown`."""
    return tuple(row for row in band_skill_rows(record, months)
                 if skill_key(record.domain, row["subdomain"], months,
                              row["milestone"]) not in record.skills)


def legacy_record_is_band_complete(record: Any) -> bool:
    """Whether a v1 record proves band-complete assessment. Always False.

    v1 asked at most one skill per band, so it cannot evidence that a band's
    siblings were assessed — let alone mastered. Stated as a function rather
    than left implicit so a later reader cannot treat a v1 record as equivalent
    to a v2 one, and so the rule is testable.
    """
    version = getattr(record, "baseline_version", "")
    if version == BASELINE_VERSION_V2:
        raise BaselineV2Error(
            "this helper answers for LEGACY records; a v2 record should be "
            "asked about a specific band via `band_assessment`")
    return False


# ---------------------------------------------------------------------------
# 0.6A-1F — VERSIONED serialisation
#
# v1 stores `BaselineRecord.to_state()` under `functional_baseline[domain]`.
# v2 stores `BaselineRecordV2.to_state()` under a DIFFERENT key, and the two
# shapes are not interchangeable by construction:
#
#     v1 has `asked` (append-only month history) and `entry_choice_label`
#     v2 has `skills` (per-skill evidence) and `bands_entered`, no `asked`
#
# plus an explicit `record_schema` discriminator that `record_from_state_v2`
# REQUIRES. So no field ever means one thing in v1 and another in v2, and a
# decoder handed the wrong generation refuses instead of guessing.
# ---------------------------------------------------------------------------

#: The stored-record discriminator. Distinct from `baseline_version` because
#: they answer different questions: `baseline_version` names the CLINICAL rules
#: that produced the assessment, this names the SERIALISATION. A later v2.1
#: rule change could keep this shape, and a later shape change must not be
#: mistaken for a rule change.
RECORD_SCHEMA_V2 = "parent-2.4-functional-baseline-v2-record"

#: Session-document keys this generation owns. Siblings of v1's, never shared.
STATE_KEY_V2 = "functional_baseline_v2"


def _skill_to_state(evidence: BaselineSkillEvidence) -> Dict[str, Any]:
    """One evidence record as a plain dict. Five fields, no raw answer."""
    return {
        "domain": evidence.domain,
        "subdomain": evidence.subdomain,
        "months": int(evidence.months),
        "milestone": evidence.milestone,
        "state": evidence.state,
    }


def record_to_state_v2(record: BaselineRecordV2) -> Dict[str, Any]:
    """The canonical stored form of a v2 record.

    ## Skills are a SORTED LIST, not a keyed mapping

    `skill_key` joins on `\\x1f`, which is legal in neither a Firestore field
    name nor a readable JSON key, and a mapping's iteration order is not part of
    its value — so a dict would give two byte-different serialisations of one
    identical assessment. The digest A2 keys its idempotency on is computed over
    these bytes, so an unstable order would make an exact replay look like a
    changed source record and fail closed against itself.

    Sorted by `(months, subdomain, milestone)`: total, deterministic, and
    derived only from the identity fields.
    """
    skills = sorted((_skill_to_state(e) for e in record.skills.values()),
                    key=lambda s: (s["months"], s["subdomain"], s["milestone"]))
    return {
        "record_schema": RECORD_SCHEMA_V2,
        "baseline_version": record.baseline_version,
        "area_id": record.area_id,
        "domain": record.domain,
        "entry_choice_id": record.entry_choice_id,
        "entry_anchor_months": record.entry_anchor_months,
        "chronological_months": int(record.chronological_months),
        "skills": skills,
        "bands_entered": [int(m) for m in record.bands_entered],
        "status": record.status,
        "routing_anchor_months": record.routing_anchor_months,
        "demonstrated_months": record.demonstrated_months,
        "not_demonstrated_months": record.not_demonstrated_months,
    }


def record_from_state_v2(stored: Dict[str, Any]) -> BaselineRecordV2:
    """Rebuild a v2 record, or refuse. The inverse of `record_to_state_v2`.

    Field names are read EXPLICITLY rather than splatted, for the same reason
    v1's loader does: an unexpected stored key becomes a visible failure here
    instead of a confusing TypeError inside the dataclass.

    Both discriminators are checked. A v1 dict has neither, so it is refused
    before a single field is read — which is the guarantee that one stored shape
    can never be decoded as the other generation.
    """
    if not isinstance(stored, dict):
        raise BaselineV2Error("a stored v2 baseline must be a mapping")
    if stored.get("record_schema") != RECORD_SCHEMA_V2:
        raise BaselineV2Error(
            "this stored record is not a v2 baseline record; v1 and v2 are "
            "separate shapes and are never decoded interchangeably")
    if stored.get("baseline_version") != BASELINE_VERSION_V2:
        raise BaselineV2Error("this stored record is not a v2 baseline")

    record = BaselineRecordV2(
        baseline_version=stored["baseline_version"],
        area_id=stored["area_id"],
        domain=stored["domain"],
        entry_choice_id=stored["entry_choice_id"],
        entry_anchor_months=stored.get("entry_anchor_months"),
        chronological_months=int(stored["chronological_months"]),
        bands_entered=[int(m) for m in (stored.get("bands_entered") or [])],
        status=stored["status"],
        routing_anchor_months=stored.get("routing_anchor_months"),
        demonstrated_months=stored.get("demonstrated_months"),
        not_demonstrated_months=stored.get("not_demonstrated_months"),
    )
    for row in (stored.get("skills") or []):
        evidence = BaselineSkillEvidence(
            domain=row["domain"],
            months=int(row["months"]),
            milestone=row["milestone"],
            subdomain=row["subdomain"],
            state=row["state"],
        )
        if evidence.key in record.skills:
            # Two stored rows collapsing to one key would mean one skill's
            # evidence silently overwrote another's. Refused rather than
            # last-write-wins.
            raise BaselineV2Error(
                "two stored skills collapse to one skill key")
        record.skills[evidence.key] = evidence
    return record


def attach_to_state_v2(state: Dict[str, Any],
                       record: BaselineRecordV2) -> Dict[str, Any]:
    """Store a v2 baseline on the session state. Additive only.

    Writes under `functional_baseline_v2[domain]` and NEVER touches `dev_age`,
    for exactly the reason v1's `attach_to_state` does not: `dev_age` is what
    the parent planner reads to choose activity targets, so writing it here
    would change which activities a live Beta session generates. A routing
    anchor must never be mistaken downstream for a scored developmental age.

    It also never touches `functional_baseline`, so a session may legitimately
    carry a v1 and a v2 baseline side by side without either overwriting the
    other.
    """
    baselines = state.setdefault(STATE_KEY_V2, {})
    baselines[record.domain] = record_to_state_v2(record)
    return state


def assert_skill_keys_are_unique(domain: str, subdomains: Tuple[str, ...] = (),
                                 families: Tuple[str, ...] = ()) -> int:
    """Two different declared-track source skills must never share a key.

    A construction-time invariant rather than a comment. If the frozen source
    ever gained two rungs whose four identity fields agree, every band
    assessment built on `skill_key` would silently merge them and one skill's
    evidence would overwrite the other's.

    Returns the number of distinct skills checked, so a caller can assert the
    check was not vacuous.
    """
    seen: Dict[str, Tuple[int, str]] = {}
    rungs = {}
    for row in v1._rows_for_domain(domain, subdomains, families):
        # Rows are bridge steps; a RUNG is one (months, milestone). Fold first,
        # or the duplicate-row structure would look like a key collision.
        rungs[(row["months"], row["milestone"])] = row["subdomain"]
    for (months, milestone), subdomain in rungs.items():
        key = skill_key(domain, subdomain, months, milestone)
        if key in seen:
            raise BaselineV2Error(
                "two declared-track source skills collapse to one skill key "
                f"at {months} months")
        seen[key] = (months, milestone)
    return len(seen)
