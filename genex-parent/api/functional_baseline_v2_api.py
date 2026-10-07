"""api/functional_baseline_v2_api.py — the Parent 2.4 baseline v2 product path.

0.6A-1F. The v2 engine (`genex_core/functional_baseline_v2`) was built and
proven in 0.6A-1D/E but nothing called it: before this slice it was imported by
its own test and by the A2 projection test, so no real session ever carried a v2
baseline. This module is the authenticated product path that changes that.

Routes live in `api/main.py`; the logic lives here — the same split v1 uses.

## v1 IS NOT TOUCHED

A separate module, separate session keys, separate routes, separate request
schemas. `functional_baseline_api` is not imported here and not modified, so
every v1 behaviour, payload and stored record stays exactly as it was. A session
may legitimately carry both generations side by side.

## THE ENGINE IS THE SOURCE OF TRUTH AND IS NOT RE-IMPLEMENTED

Which band to enter, which skill to ask next, when a band is finished, whether
to step up or down, what the status is — every one is a call into the frozen v2
engine, not a copy of its rules. This module only resolves the area, reads
chronological age from the session (never from the request), persists
`record_to_state_v2` through the engine's own `attach_to_state_v2`, rebuilds the
record on the next request, and shapes the HTTP lifecycle answers.

There is no second baseline algorithm here and no clinical rule.

## WHY `attach_to_state_v2`, AND WHY `dev_age` STAYS UNTOUCHED

The engine's writer is additive: it writes `functional_baseline_v2[domain]` and
nothing else. `dev_age` is what `bridge_selector.select_next_milestones` reads to
choose plan targets, so writing it would change which activities a live Beta
session generates — a behaviour change in the shipped parent product, far beyond
"persist a baseline". Letting the baseline influence planning is a real and
probably desirable step, and it is a separate founder decision with its own
review rather than something switched on quietly here.

## ONE SUBTLETY THE HTTP LAYER HAS TO RESPECT

`next_question_v2` ADVANCES the record: entering a band appends to
`bands_entered`. That is correct for the engine — entering a band is a real
decision — but it means a read must not be allowed to persist one.

So every operation here works on a record REBUILT from the stored dict, which
`record_from_state_v2` returns fully detached (new list, new dict). `start` and
`answer` persist the advance they caused; `view` computes a question on its own
rebuilt copy and writes nothing. Band entry is deterministic given the stored
evidence, so the band a GET computed is the same band the next `answer`
recomputes and stores — a read can never fork the record.

## WHAT THE CLIENT IS TOLD

Progress COUNTS and derived booleans per band, never the per-skill clinical
states and never the derived month anchors. `routing_anchor_months` rendered in
a parent UI would read as a developmental age, which is exactly what that
field's own naming comment warns against; the band booleans answer "have we
finished asking" and "was it all demonstrated" without exposing a number that
invites that misreading.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from genex_core.functional_baseline import (
    BaselineError,
    area_for_domain,
    entry_screen,
)
from genex_core.functional_baseline_v2 import (
    BASELINE_VERSION_V2,
    STATE_KEY_V2,
    BaselineRecordV2,
    BaselineV2Error,
    attach_to_state_v2,
    band_assessment,
    finalize_v2,
    next_question_v2,
    record_answer_v2,
    record_from_state_v2,
    skill_key,
    start_baseline_v2,
)

#: Version of THIS module's lifecycle envelope — not the baseline's own version,
#: which the engine stamps into the record as `baseline_version`.
API_ENVELOPE_VERSION_V2 = "parent-2.4-baseline-api-v2"

#: The same single domain v1 exposes. The other six areas exist in the engine
#: and are deliberately not wired: each needs its own content review before a
#: parent is calibrated against it, and the pilot consuming this is SLP-only.
SUPPORTED_DOMAINS: Tuple[str, ...] = ("talking_and_communicating",)

#: Session-document keys. SIBLINGS of v1's `functional_baseline` /
#: `functional_baseline_api`, never the same key with a different meaning.
CANONICAL_STATE_KEY_V2 = STATE_KEY_V2
ENVELOPE_STATE_KEY_V2 = "functional_baseline_v2_api"


class BaselineV2DomainUnsupported(Exception):
    """The domain is real but not exposed by this slice."""


class BaselineV2NotStarted(Exception):
    """No v2 baseline exists for this session and domain."""


class BaselineV2AlreadyFinalized(Exception):
    """A finalized baseline is immutable; answers and restarts are refused."""


class BaselineV2NotInProgress(Exception):
    """The engine has no further question, so an answer cannot be recorded."""


class BaselineV2SkillMismatch(Exception):
    """The answer does not belong to the skill the engine asked next."""

    def __init__(self, expected: str, received: str) -> None:
        super().__init__("baseline_skill_mismatch")
        self.expected = expected
        self.received = received


class BaselineV2EntryChoiceUnknown(Exception):
    """The entry descriptor is not one this area declares."""


class BaselineV2NotFinalized(Exception):
    """The baseline exists but has not been finalized.

    Projection copies a FINALIZED record. An in-progress v2 baseline has bands
    still being asked, so projecting it would send a half-finished assessment as
    though it were a result — and A2 would compute band completeness against
    denominators whose evidence had not all arrived.
    """


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_supported_domain(domain: str) -> str:
    """The canonical domain key, or a refusal. No normalising guesswork."""
    key = (domain or "").strip()
    if key not in SUPPORTED_DOMAINS:
        raise BaselineV2DomainUnsupported(key)
    return key


def _area_for(domain: str):
    area = area_for_domain(domain)
    if area is None:  # pragma: no cover - SUPPORTED_DOMAINS guarantees an area
        raise BaselineV2DomainUnsupported(domain)
    return area


def _chronological_months(doc: Dict[str, Any]) -> int:
    """Age in months, read from the session the parent already completed.

    CONTEXT ONLY, exactly as in v1: the engine uses it as the neutral starting
    band when the caregiver picks "not sure", and never as evidence. Read from
    stored state rather than from the request body, so a client cannot move the
    starting band by restating the age. An absent or unparseable age becomes 0,
    which the engine clamps — it does not become a guess.
    """
    child = ((doc.get("brain_state") or {}).get("child") or {})
    try:
        return int(child.get("chronological_months") or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _load_record(doc: Dict[str, Any], domain: str) -> Optional[BaselineRecordV2]:
    """Rebuild a v2 record from stored state, or None if there is none.

    Delegates to the ENGINE's `record_from_state_v2`, which checks both
    discriminators — so a v1 dict handed to this loader is refused rather than
    partially decoded, and the returned record is fully detached from the
    session document.
    """
    stored = (doc.get(CANONICAL_STATE_KEY_V2) or {}).get(domain)
    if not stored:
        return None
    return record_from_state_v2(stored)


def _envelope(doc: Dict[str, Any], domain: str) -> Dict[str, Any]:
    return (doc.get(ENVELOPE_STATE_KEY_V2) or {}).get(domain) or {}


def is_finalized(doc: Dict[str, Any], domain: str) -> bool:
    return bool(_envelope(doc, domain).get("finalized"))


def _store(doc: Dict[str, Any], record: BaselineRecordV2, *,
           finalized: bool) -> None:
    """Persist the canonical v2 record and update the lifecycle envelope.

    The canonical write goes through the ENGINE's `attach_to_state_v2`, so the
    stored dict is exactly `record_to_state_v2(record)` and nothing this module
    shaped. That is what makes the digest A2 keys on attest the real stored
    bytes rather than a re-serialisation.

    The envelope is a SIBLING key rather than a flag inside the record, for the
    same reason v1 keeps it apart: the engine's serialisation has no notion of
    "finalized", and adding one would mean the stored dict was no longer exactly
    what the engine produces. It genuinely cannot be derived either — a baseline
    that has only just started and one finalized with no usable evidence both
    read as `status=UNRESOLVED` with no anchor.
    """
    attach_to_state_v2(doc, record)

    envelope = doc.setdefault(ENVELOPE_STATE_KEY_V2, {})
    existing = envelope.get(record.domain) or {}
    now = _now_iso()
    envelope[record.domain] = {
        "schema_version": API_ENVELOPE_VERSION_V2,
        "finalized": bool(finalized),
        "started_at": existing.get("started_at") or now,
        "updated_at": now,
        "finalized_at": (
            existing.get("finalized_at") or now if finalized else None),
    }


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def start(doc: Dict[str, Any], domain: str, entry_choice_id: str
          ) -> Tuple[Dict[str, Any], bool]:
    """Begin a v2 baseline, or return an in-progress one unchanged.

    Returns `(view, mutated)` so the route knows whether to save.

    IDEMPOTENT RESTART, matching v1 and `/focus/{key}/start`: calling this again
    while a baseline is in progress returns the current state and does NOT
    restart. Restarting would discard evidence a parent already gave, and the
    engine offers no partial undo. A finalized baseline is refused outright.

    `next_question_v2` is called before storing so the ENTRY BAND is persisted
    by this request rather than first appearing on some later write. The band is
    a real decision and belongs in the record that the start produced.
    """
    key = require_supported_domain(domain)
    if is_finalized(doc, key):
        raise BaselineV2AlreadyFinalized(key)

    existing = _load_record(doc, key)
    if existing is not None:
        return view(doc, key), False

    area = _area_for(key)
    try:
        area.choice(entry_choice_id)
    except Exception as exc:  # the engine raises on an unknown descriptor
        raise BaselineV2EntryChoiceUnknown(entry_choice_id) from exc

    record = start_baseline_v2(area.area_id, entry_choice_id,
                               _chronological_months(doc))
    next_question_v2(record)
    _store(doc, record, finalized=False)
    return view(doc, key), True


def answer(doc: Dict[str, Any], domain: str, requested_skill_key: str,
           value: str) -> Dict[str, Any]:
    """Record one skill's evidence against the skill the engine asked next.

    THE SKILL-KEY CHECK IS WHAT MAKES RETRIES SAFE, and it is strictly stronger
    than v1's. v1's `question_id` embedded `milestone[:48]`, so two milestones
    sharing a 48-character prefix shared an id and an answer could land on the
    wrong rung. v2's key is the full canonical identity joined on a separator
    that cannot occur in the data, so it identifies exactly one skill.

    A resubmitted answer is REFUSED rather than re-recorded: once evidence
    lands, the engine's next question is a different skill, so the stale key no
    longer matches. Note that the engine's `record_answer_v2` would happily
    REPLACE the evidence — it is keyed, not appended — so without this check a
    double submit would silently overwrite an answer with itself and, worse, a
    late retry could overwrite a LATER corrected answer. The guard is what makes
    the record append-only in practice.

    Changing an earlier answer is not offered here. That is a product decision:
    the engine supports replacement, but exposing it would let a client rewrite
    a band the engine had already stepped past, leaving the stored
    `bands_entered` history describing a traversal that no longer follows from
    the evidence.
    """
    key = require_supported_domain(domain)
    if is_finalized(doc, key):
        raise BaselineV2AlreadyFinalized(key)

    record = _load_record(doc, key)
    if record is None:
        raise BaselineV2NotStarted(key)

    question = next_question_v2(record)
    if question is None:
        raise BaselineV2NotInProgress(key)
    if question["skill_key"] != requested_skill_key:
        raise BaselineV2SkillMismatch(question["skill_key"],
                                      requested_skill_key)

    # `_classify` raises BaselineError on an unsupported answer. Pydantic has
    # already constrained the vocabulary, so this is the engine's own second
    # line rather than a validation this module performs.
    record_answer_v2(record, question, value)
    _store(doc, record, finalized=False)
    return view(doc, key)


def finalize_baseline(doc: Dict[str, Any], domain: str
                      ) -> Tuple[Dict[str, Any], bool]:
    """Derive the compatibility months from evidence and make the record immutable.

    Returns `(view, mutated)`.

    IDEMPOTENT: finalizing an already-finalized baseline returns the stored
    state and rewrites nothing. `finalize_v2` is a pure recomputation from the
    stored evidence, so re-running it would produce identical values — but
    re-storing would move `updated_at` and make an immutable record look edited.

    Finalizing EARLY is allowed and the ENGINE decides what the evidence
    supports: with nothing usable it returns UNRESOLVED and no anchor rather
    than a fabricated level. This module imposes no minimum answer count,
    because that would be a clinical rule living in the transport.

    An early finalize can leave a band INCOMPLETE, and that is honest rather
    than broken — A2 will compute `assessment_complete(band)` as False for it,
    which is precisely the distinction v2 exists to preserve.
    """
    key = require_supported_domain(domain)
    record = _load_record(doc, key)
    if record is None:
        raise BaselineV2NotStarted(key)
    if is_finalized(doc, key):
        return view(doc, key), False

    finalize_v2(record)
    _store(doc, record, finalized=True)
    return view(doc, key), True


# ---------------------------------------------------------------------------
# the client view
# ---------------------------------------------------------------------------

def _client_question(question: Optional[Dict[str, Any]]
                     ) -> Optional[Dict[str, Any]]:
    """The parent-facing part of a question, and nothing else.

    `subdomain` and `activity_family` are withheld exactly as in v1: the
    activity family is a binding the canonical-rung path owns, and the subdomain
    is track membership. Neither is anything a parent answering a question
    needs.

    `skill_key` IS returned, because the client must echo it back on `/answer` —
    that is the whole retry guard. It is an opaque identity string built from
    the question's own fields and tells the client nothing it was not just
    shown.
    """
    if question is None:
        return None
    return {
        "skill_key": question["skill_key"],
        "months": question["months"],
        "milestone": question["milestone"],
        "parent_explanation": question.get("parent_explanation", ""),
    }


def _band_progress(record: BaselineRecordV2) -> List[Dict[str, Any]]:
    """Per-band COUNTS and derived booleans. No clinical states, no prose.

    `complete` and `mastered` are the two questions v2 exists to keep apart —
    "have we finished asking this band" and "was every skill in it
    demonstrated" — and one boolean cannot carry both. A band holding an
    `unknown` is complete and NOT mastered; a band with an unasked sibling is
    not complete at all.

    Counts rather than per-skill states keeps this view the same shape as v1's
    `answers_recorded`: enough for a progress indicator without re-serving
    clinical content back to the client.
    """
    out = []
    for months in record.bands_entered:
        assessment = band_assessment(record, months)
        out.append({
            "months": int(months),
            "assessed": len(assessment.assessed),
            "total": assessment.total_skills,
            "complete": assessment.assessment_complete,
            "mastered": assessment.band_mastered,
        })
    return out


def view(doc: Dict[str, Any], domain: str) -> Dict[str, Any]:
    """What a client may see about a v2 baseline.

    Deliberately WITHOUT `routing_anchor_months`, `demonstrated_months`,
    `not_demonstrated_months`, the track, and the per-skill states. Those are
    server-side provenance for the projection path; a raw anchor number in a
    parent UI would be read as a developmental age, which is what that field's
    own naming comment exists to prevent.

    Writes NOTHING. `next_question_v2` may advance the rebuilt copy's band, and
    that copy is discarded — a read must never persist a traversal decision.
    """
    key = require_supported_domain(domain)
    record = _load_record(doc, key)
    if record is None:
        raise BaselineV2NotStarted(key)

    envelope = _envelope(doc, key)
    finalized = bool(envelope.get("finalized"))
    question = None if finalized else next_question_v2(record)
    return {
        "domain": key,
        "area_id": record.area_id,
        "baseline_version": record.baseline_version,
        "entry_choice_id": record.entry_choice_id,
        "finalized": finalized,
        "status": record.status,
        "skills_assessed": len(record.skills),
        "bands": _band_progress(record),
        "current_question": _client_question(question),
        "ready_to_finalize": (not finalized) and question is None,
        "updated_at": envelope.get("updated_at", ""),
    }


def entry_screen_for(domain: str) -> Dict[str, Any]:
    """The area's reviewed entry descriptors, for the start screen.

    The SAME descriptors v1 offers, because they describe the area rather than
    the assessment algorithm: v2 changed how a band is assessed, not where a
    caregiver says to begin. A parallel v2-only descriptor set would be two
    reviewed content sets to keep in step for no clinical difference.
    """
    key = require_supported_domain(domain)
    return entry_screen(_area_for(key).area_id)


# ---------------------------------------------------------------------------
# the projection source — read by the A2 v2 client only
# ---------------------------------------------------------------------------

def finalized_record(doc: Dict[str, Any], domain: str) -> Dict[str, Any]:
    """The canonical finalized v2 record, verbatim, for digesting.

    Returns the stored dict as-is — exactly `record_to_state_v2(record)` — so
    the digest computed over it attests the real stored bytes rather than a
    re-serialisation of a rebuilt object. A copy is returned so a caller cannot
    mutate session state through the reference.
    """
    key = require_supported_domain(domain)
    stored = (doc.get(CANONICAL_STATE_KEY_V2) or {}).get(key)
    if not stored:
        raise BaselineV2NotStarted(key)
    if not is_finalized(doc, key):
        raise BaselineV2NotFinalized(key)
    return dict(stored)


#: Exactly the seven compatibility summary fields A2 accepts. Built by NAME, so
#: a field added to the v2 record cannot start travelling by accident.
PROJECTED_SUMMARY_FIELDS: Tuple[str, ...] = (
    "domain", "area_id", "entry_choice_id", "routing_anchor_months",
    "not_demonstrated_months", "status", "baseline_version",
)

#: Exactly the five fields A2 consumes per skill. `milestone` and `subdomain`
#: are TRANSIENT: the Pilot needs them to resolve the canonical rung and
#: discards them, and they are never persisted on the Pilot side.
PROJECTED_SKILL_FIELDS: Tuple[str, ...] = (
    "domain", "subdomain", "months", "milestone", "state",
)


def projection_payload(doc: Dict[str, Any], domain: str) -> Dict[str, Any]:
    """The transient A2 v2 request body, built from the FINALIZED record.

    Three parts, each additive and built by name:

        summary       the seven v1-compatible month-level fields
        skills        one row per assessed skill, with the transient identity
        band_totals   Parent's own declared-track count per band entered

    ## Why `band_totals` comes from here and not from the Pilot

    It is the DENOMINATOR for band completeness, and Parent is the source of
    truth for how many declared-track skills a band holds. A Pilot-side count
    would be unsafe in one direction: a stale or narrowed artifact with a
    SMALLER roster would make "assessed 3 of the 3 I know about" read as
    complete when Parent assessed four, turning missing evidence into mastery.

    A2 does not simply trust the number either — 0.6A-1F verifies it against
    the canonical roster and refuses a mismatch in BOTH directions. The two
    checks are complementary: Parent supplies the claim, the Pilot refuses to
    accept a claim that disagrees with the frozen source.

    ## What is deliberately absent

    No `asked`, no raw caregiver answer, no `skill_key`, no `question_id`, no
    chronological age, no `entry_anchor_months`, no `entry_choice_label`, no
    session identifier, and nothing about the child or the caregiver. The
    session id and digest are added by the CLIENT, which is the module that
    knows it is sending a request.
    """
    key = require_supported_domain(domain)
    stored = finalized_record(doc, key)
    record = record_from_state_v2(stored)

    missing = [f for f in PROJECTED_SUMMARY_FIELDS if f not in stored]
    if missing:
        raise BaselineV2Error("the finalized record is missing summary fields")
    summary = {name: stored[name] for name in PROJECTED_SUMMARY_FIELDS}

    skills = []
    for evidence in record.skills.values():
        skills.append({
            "domain": evidence.domain,
            "subdomain": evidence.subdomain,
            "months": int(evidence.months),
            "milestone": evidence.milestone,
            "state": evidence.state,
        })
    # Deterministic order so two payloads for one record compare equal. A2
    # re-sorts canonically anyway; this makes the REQUEST reproducible too.
    skills.sort(key=lambda s: (s["months"], s["subdomain"], s["milestone"]))

    band_totals = [
        {"months": int(months),
         "total_skills": band_assessment(record, months).total_skills}
        for months in record.bands_entered
    ]
    band_totals.sort(key=lambda b: b["months"])

    return {"summary": summary, "skills": skills, "band_totals": band_totals}


__all__ = [
    "API_ENVELOPE_VERSION_V2",
    "BASELINE_VERSION_V2",
    "CANONICAL_STATE_KEY_V2",
    "ENVELOPE_STATE_KEY_V2",
    "PROJECTED_SKILL_FIELDS",
    "PROJECTED_SUMMARY_FIELDS",
    "SUPPORTED_DOMAINS",
    "BaselineError",
    "BaselineV2AlreadyFinalized",
    "BaselineV2DomainUnsupported",
    "BaselineV2EntryChoiceUnknown",
    "BaselineV2Error",
    "BaselineV2NotFinalized",
    "BaselineV2NotInProgress",
    "BaselineV2NotStarted",
    "BaselineV2SkillMismatch",
    "answer",
    "entry_screen_for",
    "finalize_baseline",
    "finalized_record",
    "is_finalized",
    "projection_payload",
    "require_supported_domain",
    "skill_key",
    "start",
    "view",
]
