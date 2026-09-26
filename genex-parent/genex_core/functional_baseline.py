"""genex_core/functional_baseline.py — PARENT-0.4 deterministic functional baseline.

A short structured step BEFORE the milestone estimator. The parent picks one
fixed option describing what their child does now; that option selects a
starting neighbourhood in the EXISTING validated Gold Standard ladder; the
estimator then asks 2–4 nearby real milestone questions to bracket the child.

## LLM-free by construction

Nothing here calls a model, the network, or a random source. Questions come from
the Gold Standard workbook, ordering comes from the `months` column, and the
next question is a pure function of the answers so far. Same age + domain +
entry choice + answer sequence always yields the same sequence and result.

## The entry choice is an ANCHOR, not a result

A 48-month-old whose parent selects "No words yet" does NOT get
`dev_age = 12`. The choice only says *where to start asking*. The functional
baseline is whatever the validated questions then demonstrate.

## Why anchors are months, not milestone IDs

The Gold Standard has no stable per-row identifier, and several rows repeat the
same milestone text at the same age. The `months` column IS the ladder — it is
ordered, it is what `get_category_questions` already bands on, and it is stable
across workbook edits in a way row position is not. Each anchor below is
therefore a month that has real rows, and the provenance string records the
milestone those rows carry so the mapping can be audited against the source.

## No clinical developmental-age claim

`routing_anchor_months` is a PLANNING anchor derived from observed milestone
evidence — which validated skills the parent reported, and which they did not.
It is not a developmental-age measurement and must never be shown to a parent as
one. It is deliberately named differently from `dev_age` for that reason.

Crucially it is `None` until evidence exists. There is no "no answers means six
months" default here: unknown stays unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from genex_core.activity_engine import DAILY_LIVING_CLINICAL_HOLD

BASELINE_VERSION = "parent-2.4-functional-baseline-v1"

# ---------------------------------------------------------------------------
# Answer semantics
# ---------------------------------------------------------------------------
#
# Reuses the existing structured vocabulary (config.VALID_ANSWERS) unchanged.
# The one baseline-specific rule: `not_sure` and a missing answer are UNKNOWN.
# They are never treated as a failed milestone, and they never contribute a
# numeric score — config.ANSWER_SCORES gives not_sure 0.1, which is right for
# aggregate scoring but wrong here, where a single unknown must not look like
# weak evidence of absence.

DEMONSTRATED: FrozenSet[str] = frozenset({"yes"})
EMERGING: FrozenSet[str] = frozenset({"sometimes", "with_help"})
NOT_DEMONSTRATED: FrozenSet[str] = frozenset({"no"})
UNKNOWN: FrozenSet[str] = frozenset({"not_sure", ""})

#: Normally 2–3 questions; a 4th only when answers conflict or are mostly
#: unknown. This is a starting point for planning, not an assessment.
TARGET_QUESTIONS = 3
MAX_QUESTIONS = 4


class BaselineStatus:
    BOUNDED = "BOUNDED"                # demonstrated floor + not-demonstrated ceiling
    EMERGING = "EMERGING"              # best evidence is partial
    AGE_RELEVANT = "AGE_RELEVANT"      # demonstrated at or above the child's age band
    UNRESOLVED = "UNRESOLVED"          # no usable evidence (all unknown / none)
    CONTRADICTORY = "CONTRADICTORY"    # higher skill yes, lower skill no


class BaselineError(ValueError):
    """Invalid baseline area, choice, or answer."""


# ---------------------------------------------------------------------------
# Entry choices — grounded in real Gold Standard rows
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EntryChoice:
    """One fixed parent-facing option.

    `anchor_months` is the Gold Standard month this choice starts at. `None`
    means "not sure" — no anchor is assumed and the child's age band is used as
    a neutral starting point, with the result marked UNRESOLVED unless the
    questions themselves produce evidence.
    """

    choice_id: str
    label: str
    anchor_months: Optional[int]
    provenance: str = ""
    #: Optional substring selecting WHICH existing row at `anchor_months` to
    #: lead with, when that rung carries several milestones in the same
    #: subdomain. It only reorders real Gold Standard rows — it never adds,
    #: edits or filters one, and it affects the first question only. Without it
    #: a parent choosing "Has trouble holding toys" is asked about bringing
    #: hands to mouth, purely because that sorts first alphabetically.
    prefer_milestone: str = ""


@dataclass(frozen=True)
class BaselineArea:
    """A parent-facing functional area. The label is UX; the domain is canonical."""

    area_id: str
    domain: str
    title: str
    subtitle: str
    question: str
    choices: Tuple[EntryChoice, ...]

    def choice(self, choice_id: str) -> EntryChoice:
        for c in self.choices:
            if c.choice_id == choice_id:
                return c
        raise BaselineError(f"unknown choice {choice_id!r} for area {self.area_id!r}")


_NOT_SURE = EntryChoice("not_sure", "Not sure", None, "no anchor assumed")

AREAS: Tuple[BaselineArea, ...] = (
    BaselineArea(
        area_id="talking",
        domain="talking_and_communicating",
        title="Talking & Communicating",
        subtitle="Words, understanding, conversation",
        question="Which best describes your child now?",
        choices=(
            EntryChoice("no_words_yet", "No words yet", 9,
                        "9m 'makes a lot of different sounds like mamamamama'; first words appear at 12m"),
            EntryChoice("few_sounds_or_words", "A few sounds or words", 12,
                        "12m 'calls parents mama or dada'; 15m 'tries to say two or more words'"),
            EntryChoice("many_single_words", "Many single words", 18,
                        "18m 'tries to say three or more words'; 24m 'says at least two words together'"),
            EntryChoice("two_three_words", "Puts 2–3 words together", 24,
                        "24m 'says at least two words together'; 30m 'two or more words with one action word'"),
            EntryChoice("short_sentences", "Uses short sentences", 36,
                        "36m 'talks with you in conversation using at least two back and forth exchanges'; "
                        "48m 'says sentences with four or more words'"),
            _NOT_SURE,
        ),
    ),
    BaselineArea(
        area_id="hand_finger",
        domain="fine_motor",
        title="Hand & Finger Skills",
        subtitle="Grasping, picking up, drawing",
        question="Which best describes your child now?",
        choices=(
            EntryChoice("trouble_holding", "Has trouble holding toys", 4,
                        "4m 'holds a toy when you put it in his hand'",
                        prefer_milestone="holds a toy"),
            EntryChoice("whole_hand", "Holds objects with whole hand", 9,
                        "9m 'moves things from one hand to her other hand'; 'uses fingers to rake food'"),
            EntryChoice("finger_pickup", "Picks up small objects with fingers", 12,
                        "12m 'picks things up between thumb and pointer finger'"),
            EntryChoice("stacks_scribbles", "Stacks, places, or scribbles", 18,
                        "15m 'releases an object on purpose'; 18m 'scribbles'"),
            EntryChoice("crayons_tools", "Uses crayons and tools with control", 36,
                        "30m 'uses hands to twist things'; 36m 'strings items together'; "
                        "48m 'holds crayon or pencil between fingers and thumb'"),
            _NOT_SURE,
        ),
    ),
    BaselineArea(
        area_id="movement",
        domain="gross_motor",
        title="Movement Skills",
        subtitle="Sitting, walking, running, jumping",
        question="Which best describes your child now?",
        choices=(
            EntryChoice("needs_help_sitting", "Needs help sitting or standing", 6,
                        "6m 'leans on hands to support herself when sitting'; 9m 'sits without support'"),
            EntryChoice("moves_not_walking", "Moves around, not walking yet", 12,
                        "12m 'pulls up to stand'; 'walks holding on to furniture'"),
            EntryChoice("walks_independently", "Walks independently", 15,
                        "15m 'takes a few steps on her own'; 18m 'walks without holding on'"),
            EntryChoice("runs_climbs", "Runs and climbs", 24,
                        "18m 'climbs on and off a couch or chair'; 24m 'runs'",
                        prefer_milestone="runs"),
            EntryChoice("jumps_balances", "Jumps and balances", 30,
                        "30m 'jumps off the ground with both feet'; 60m 'hops on one foot'"),
            _NOT_SURE,
        ),
    ),
    BaselineArea(
        area_id="daily_skills",
        domain="daily_living",
        title="Daily Skills",
        subtitle="Dressing, eating, simple routines",
        question="Which best describes your child now?",
        choices=(
            EntryChoice("needs_help_most", "Needs help with most routines", 15,
                        "15m 'uses fingers to feed herself some food'"),
            EntryChoice("helps_with_parts", "Helps with parts of routines", 18,
                        "18m 'tries to use a spoon'; 'feeds himself with his fingers'",
                        prefer_milestone="tries to use a spoon"),
            EntryChoice("some_steps_independent", "Does some steps independently", 24,
                        "24m 'eats with a spoon'"),
            EntryChoice("many_routines_with_help", "Does many routines with help", 30,
                        "30m 'takes some clothes off by himself'; 36m 'puts on some clothes by himself'"),
            EntryChoice("mostly_independent", "Mostly independent", 48,
                        "48m 'serves herself food or pours water'; 'unbuttons some buttons'"),
            _NOT_SURE,
        ),
    ),
)

_AREA_BY_ID = {a.area_id: a for a in AREAS}
_AREA_BY_DOMAIN = {a.domain: a for a in AREAS}


def get_area(area_id: str) -> BaselineArea:
    if area_id not in _AREA_BY_ID:
        raise BaselineError(f"unknown baseline area {area_id!r}")
    return _AREA_BY_ID[area_id]


def area_for_domain(domain: str) -> Optional[BaselineArea]:
    """The baseline area covering a canonical domain, or None.

    Social & Emotional, Learning & Thinking and Sensory intentionally have no
    baseline area in v1 — they are not part of this phase.
    """
    return _AREA_BY_DOMAIN.get(domain)


def entry_screen(area_id: str) -> Dict[str, Any]:
    """The mobile-first entry screen payload. Pure data, no model involved."""
    area = get_area(area_id)
    return {
        "baseline_version": BASELINE_VERSION,
        "area_id": area.area_id,
        "domain": area.domain,
        "title": area.title,
        "subtitle": area.subtitle,
        "question": area.question,
        "choices": [
            {"choice_id": c.choice_id, "label": c.label, "order": i + 1}
            for i, c in enumerate(area.choices)
        ],
    }


# ---------------------------------------------------------------------------
# The validated ladder
# ---------------------------------------------------------------------------

def _rows_for_domain(domain: str) -> List[Dict[str, Any]]:
    """Gold Standard rows for a domain, held activity families removed.

    Daily Living's `cup_drinking` and `feeding_self_regulation` families are on
    a clinical hold, so their milestones must not become baseline questions —
    the baseline would otherwise ask about oral-motor and feeding-response
    observations that the activity layer deliberately refuses to act on.
    """
    from genex_core.milestones import get_cdc_df

    frame = get_cdc_df()
    rows: List[Dict[str, Any]] = []
    seen = set()
    for record in frame[frame["category_key"] == domain].to_dict(orient="records"):
        family = str(record.get("activity_family", "") or "")
        if family in DAILY_LIVING_CLINICAL_HOLD:
            continue
        months = record.get("months")
        milestone = str(record.get("milestone", "") or "").strip()
        if months is None or not milestone:
            continue
        key = (int(months), milestone)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "months": int(months),
            "milestone": milestone,
            "subdomain": str(record.get("subdomain", "") or ""),
            "activity_family": family,
            "parent_explanation": str(record.get("parent_explanation", "") or ""),
        })
    # Deterministic order: by month, then milestone text.
    rows.sort(key=lambda r: (r["months"], r["milestone"]))
    return rows


def ladder_months(domain: str) -> List[int]:
    """Ordered distinct months that carry usable questions for `domain`."""
    return sorted({r["months"] for r in _rows_for_domain(domain)})


#: Which subdomain to ask about when several share a rung.
#:
#: Purely a tie-break, and deterministic. Without it the choice falls out of
#: alphabetical milestone text, which can ask a parent who just said "no words
#: yet" about waving goodbye — a real milestone, but not the skill they
#: described. Listed subdomains are preferred in order; anything unlisted sorts
#: after them, so no row is ever excluded and the ladder is unchanged.
_SUBDOMAIN_PREFERENCE: Dict[str, Tuple[str, ...]] = {
    "talking_and_communicating": (
        "expressive_language",
        "early_vocalization_and_babbling",
        "conversation_narrative",
        "receptive_language",
        "speech_intelligibility",
        "gestural_communication",
    ),
    "daily_living": (
        "self_help_motor_skills",
        "safety_awareness",
        "adaptive_feeding_cues",
    ),
    "gross_motor": (
        "gross_motor_mobility_and_coordination",
        "postural_control_and_transitions",
    ),
    # fine_motor has a single subdomain — no tie-break needed.
}


def _preference_rank(domain: str, subdomain: str) -> int:
    order = _SUBDOMAIN_PREFERENCE.get(domain, ())
    return order.index(subdomain) if subdomain in order else len(order)


def question_at(
    domain: str, months: int, prefer_milestone: str = ""
) -> Optional[Dict[str, Any]]:
    """The single deterministic question for a domain at a rung.

    Several rows can share a month. The preferred subdomain wins, then
    milestone text, so the same rung always yields the same question.
    """
    candidates = [r for r in _rows_for_domain(domain) if r["months"] == months]
    if not candidates:
        return None
    hint = (prefer_milestone or "").strip().lower()
    candidates.sort(key=lambda r: (
        0 if hint and hint in r["milestone"].lower() else 1,
        _preference_rank(domain, r["subdomain"]),
        r["milestone"],
    ))
    row = candidates[0]
    return {
        "question_id": f"{BASELINE_VERSION}:{domain}:{months}:{row['milestone'][:48]}",
        "domain": domain,
        "months": months,
        "milestone": row["milestone"],
        "subdomain": row["subdomain"],
        "activity_family": row["activity_family"],
        "parent_explanation": row["parent_explanation"],
    }


def _nearest_rung(domain: str, months: int) -> Optional[int]:
    """The ladder rung at or nearest below `months`, else the lowest rung."""
    rungs = ladder_months(domain)
    if not rungs:
        return None
    at_or_below = [m for m in rungs if m <= months]
    return at_or_below[-1] if at_or_below else rungs[0]


def _step(domain: str, months: int, direction: int) -> Optional[int]:
    """One rung harder (+1) or easier (-1). None at the end of the ladder."""
    rungs = ladder_months(domain)
    if months not in rungs:
        months = _nearest_rung(domain, months)
        if months is None:
            return None
    index = rungs.index(months) + direction
    if 0 <= index < len(rungs):
        return rungs[index]
    return None


# ---------------------------------------------------------------------------
# The baseline record
# ---------------------------------------------------------------------------

@dataclass
class BaselineRecord:
    """Structured provenance — enough to reproduce the baseline exactly."""

    baseline_version: str
    area_id: str
    domain: str
    entry_choice_id: str
    entry_choice_label: str
    entry_anchor_months: Optional[int]
    chronological_months: int
    asked: List[Dict[str, Any]] = field(default_factory=list)
    status: str = BaselineStatus.UNRESOLVED
    routing_anchor_months: Optional[int] = None
    demonstrated_months: Optional[int] = None
    not_demonstrated_months: Optional[int] = None

    def to_state(self) -> Dict[str, Any]:
        return {
            "baseline_version": self.baseline_version,
            "area_id": self.area_id,
            "domain": self.domain,
            "entry_choice_id": self.entry_choice_id,
            "entry_choice_label": self.entry_choice_label,
            "entry_anchor_months": self.entry_anchor_months,
            "chronological_months": self.chronological_months,
            "asked": [dict(a) for a in self.asked],
            "status": self.status,
            # Named to make the semantic boundary explicit: a planning anchor
            # derived from observed evidence, NOT a developmental-age claim.
            "routing_anchor_months": self.routing_anchor_months,
            "demonstrated_months": self.demonstrated_months,
            "not_demonstrated_months": self.not_demonstrated_months,
        }


def start_baseline(area_id: str, choice_id: str, chronological_months: int) -> BaselineRecord:
    """Begin a baseline from a structured entry choice.

    `chronological_months` is CONTEXT only — it never selects the anchor. It is
    used solely as the neutral starting rung when the parent answers "Not sure",
    and to decide whether a demonstrated skill is age-relevant.
    """
    area = get_area(area_id)
    choice = area.choice(choice_id)
    return BaselineRecord(
        baseline_version=BASELINE_VERSION,
        area_id=area.area_id,
        domain=area.domain,
        entry_choice_id=choice.choice_id,
        entry_choice_label=choice.label,
        entry_anchor_months=choice.anchor_months,
        chronological_months=int(chronological_months),
    )


def first_question(record: BaselineRecord) -> Optional[Dict[str, Any]]:
    """The first validated question, at the anchor the entry choice selected."""
    start = record.entry_anchor_months
    if start is None:
        # "Not sure" — no anchor is assumed. Start at the child's own age band,
        # which is neutral rather than a guess about ability.
        start = record.chronological_months
    rung = _nearest_rung(record.domain, start)
    if rung is None:
        return None
    area = get_area(record.area_id)
    prefer = area.choice(record.entry_choice_id).prefer_milestone
    return question_at(record.domain, rung, prefer_milestone=prefer)


def _classify(answer: str) -> str:
    value = str(answer or "").strip().lower()
    if value in DEMONSTRATED:
        return "demonstrated"
    if value in EMERGING:
        return "emerging"
    if value in NOT_DEMONSTRATED:
        return "not_demonstrated"
    if value in UNKNOWN:
        return "unknown"
    raise BaselineError(
        f"unsupported baseline answer {answer!r}; expected one of "
        f"yes / sometimes / with_help / no / not_sure"
    )


def record_answer(record: BaselineRecord, question: Dict[str, Any], answer: str) -> BaselineRecord:
    """Record one structured answer. Mutates and returns the record."""
    record.asked.append({
        "question_id": question["question_id"],
        "months": question["months"],
        "milestone": question["milestone"],
        "subdomain": question.get("subdomain", ""),
        "answer": str(answer or "").strip().lower(),
        "classification": _classify(answer),
    })
    return record


def next_question(record: BaselineRecord) -> Optional[Dict[str, Any]]:
    """The next validated question, or None when the baseline is complete.

    Deterministic routing, one rung at a time:

      demonstrated      -> step HARDER, looking for the ceiling
      not_demonstrated  -> step EASIER, looking for solid ground
      emerging          -> step HARDER once, to see whether the next skill is
                           absent (which brackets) or present (which means the
                           emerging rung was not the ceiling)
      unknown           -> step EASIER, toward a skill the parent can answer

    Stops as soon as there is a usable bracket, or at MAX_QUESTIONS.
    """
    if not record.asked:
        return first_question(record)
    if len(record.asked) >= MAX_QUESTIONS:
        return None

    # A bracket exists once something is demonstrated/emerging below something
    # not demonstrated. Further questions would add precision we do not need.
    floor = _floor_months(record)
    ceiling = _ceiling_months(record)
    if floor is not None and ceiling is not None and ceiling > floor:
        if len(record.asked) >= 2:
            return None

    last = record.asked[-1]
    direction = {
        "demonstrated": +1,
        "emerging": +1,
        "not_demonstrated": -1,
        "unknown": -1,
    }[last["classification"]]

    asked_months = {a["months"] for a in record.asked}
    rung = _step(record.domain, last["months"], direction)
    # Never re-ask a rung; keep stepping the same way until a new one appears.
    while rung is not None and rung in asked_months:
        rung = _step(record.domain, rung, direction)
    if rung is None:
        return None
    return question_at(record.domain, rung)


def _floor_months(record: BaselineRecord) -> Optional[int]:
    """Highest rung with demonstrated or emerging evidence."""
    months = [a["months"] for a in record.asked
              if a["classification"] in ("demonstrated", "emerging")]
    return max(months) if months else None


def _ceiling_months(record: BaselineRecord) -> Optional[int]:
    """Lowest rung explicitly NOT demonstrated."""
    months = [a["months"] for a in record.asked if a["classification"] == "not_demonstrated"]
    return min(months) if months else None


def finalize(record: BaselineRecord) -> BaselineRecord:
    """Resolve the bracket, status and routing anchor. No averaging, no guessing."""
    demonstrated = [a["months"] for a in record.asked if a["classification"] == "demonstrated"]
    emerging = [a["months"] for a in record.asked if a["classification"] == "emerging"]
    not_demo = [a["months"] for a in record.asked if a["classification"] == "not_demonstrated"]

    floor = _floor_months(record)
    ceiling = _ceiling_months(record)

    record.demonstrated_months = max(demonstrated) if demonstrated else None
    record.not_demonstrated_months = ceiling

    # Contradiction: a harder skill demonstrated while an easier one is not.
    # Reported, never averaged away.
    if demonstrated and not_demo and max(demonstrated) > min(not_demo):
        record.status = BaselineStatus.CONTRADICTORY
        record.routing_anchor_months = None
        return record

    if floor is None:
        # No usable evidence at all — every answer unknown, or none given.
        # This is the case that used to silently become six months.
        record.status = BaselineStatus.UNRESOLVED
        record.routing_anchor_months = None
        return record

    record.routing_anchor_months = floor

    if ceiling is not None and ceiling > floor:
        record.status = BaselineStatus.BOUNDED
    elif demonstrated and max(demonstrated) >= record.chronological_months:
        record.status = BaselineStatus.AGE_RELEVANT
    elif emerging and (not demonstrated or max(emerging) >= max(demonstrated)):
        record.status = BaselineStatus.EMERGING
    else:
        record.status = BaselineStatus.BOUNDED if ceiling is not None else BaselineStatus.EMERGING
    return record


def run_baseline(
    area_id: str,
    choice_id: str,
    chronological_months: int,
    answers: List[str],
) -> BaselineRecord:
    """Drive a whole baseline from a fixed answer sequence.

    Pure and deterministic: the same arguments always produce the same asked
    sequence and the same result. This is the function the tests pin.
    """
    record = start_baseline(area_id, choice_id, chronological_months)
    for answer in answers:
        question = next_question(record)
        if question is None:
            break
        record_answer(record, question, answer)
    return finalize(record)


#: The historical fallback this phase exists to keep out of the baseline flow.
#: `scoring.compute_dev_age_from_answers` returns 6 when handed no answers, and
#: `finalize_domain_dev_age` writes that straight into `state["dev_age"]`. A
#: child with no evidence then looks like a six-month-old to every downstream
#: consumer. That function is 0.3B scoring math and is NOT modified here; the
#: baseline simply never routes through it without evidence.
LEGACY_NO_ANSWER_DEV_AGE = 6


def apply_baseline_to_state(state: Dict[str, Any], record: BaselineRecord) -> Dict[str, Any]:
    """Apply a finalized baseline to the Brain state.

    Writes `dev_age[domain]` ONLY when the baseline actually observed something.
    With no evidence the key is left ABSENT — which downstream already handles
    by falling back to chronological age — rather than being set to a number
    nobody measured.

    This is the whole point of the phase: UNRESOLVED and CONTRADICTORY must stay
    visible as "keep calibrating", not collapse into a confident wrong value.
    """
    attach_to_state(state, record)

    anchor = record.routing_anchor_months
    if anchor is None:
        # No evidence. Do not invent one, and do not leave a stale value behind.
        state.get("dev_age", {}).pop(record.domain, None)
        return state

    if anchor == LEGACY_NO_ANSWER_DEV_AGE and not record.asked:
        raise BaselineError(
            "refusing to write the legacy no-answer dev_age of 6 without evidence"
        )

    state.setdefault("dev_age", {})[record.domain] = int(anchor)
    return state


def baseline_is_unresolved(record: BaselineRecord) -> bool:
    """True when calibration should continue rather than plan."""
    return record.status in (BaselineStatus.UNRESOLVED, BaselineStatus.CONTRADICTORY)


def attach_to_state(state: Dict[str, Any], record: BaselineRecord) -> Dict[str, Any]:
    """Store the baseline on the existing Brain state. Additive only.

    Writes under `functional_baseline[domain]` and never touches `dev_age`.
    The estimator keeps ownership of `dev_age`; the baseline only says where it
    should start looking, so a routing anchor can never be mistaken downstream
    for a scored developmental age.
    """
    baselines = state.setdefault("functional_baseline", {})
    baselines[record.domain] = record.to_state()
    return state
