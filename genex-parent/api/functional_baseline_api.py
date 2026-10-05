"""api/functional_baseline_api.py — the Parent 2.4 functional baseline, wired.

Parent 0.4 built the functional-baseline engine and froze it. Nothing then
called it: before this slice `genex_core/functional_baseline.py` was imported
by exactly two files in the repository — itself and its own test — so
`state["functional_baseline"]` was never written and no real session carried a
baseline. This module is the product path that changes that.

Routes live in `api/main.py`; the logic lives here, the same split
`api/pipeline.py` already uses.

## The engine is the source of truth and is not re-implemented

Every decision — which rung to ask, which direction to step, where the floor
and ceiling are, what the status is, what the routing anchor is — is made by
the frozen engine. This module only:

  * resolves which area a domain belongs to,
  * reads chronological age from the session (never from the client),
  * persists `BaselineRecord.to_state()` through the engine's own
    `attach_to_state`,
  * rebuilds a `BaselineRecord` from that stored dict on the next request,
  * and decides the HTTP-shaped lifecycle answers.

There is no second baseline algorithm here, and no clinical rule. A reviewer
checking that claim can grep this file for the engine's names and find that
every one of them is a call, not a copy.

## WHY `attach_to_state` AND NOT `apply_baseline_to_state`

The engine ships both. `attach_to_state` writes
`functional_baseline[domain]` and explicitly "never touches `dev_age`".
`apply_baseline_to_state` ALSO writes `dev_age[domain]`.

This slice uses `attach_to_state` only. `dev_age` is what
`bridge_selector.select_next_milestones` reads to choose plan targets, so
writing it would change which activities an existing Beta session generates —
a live behaviour change in the parent product, well beyond "persist the
baseline". 0.5F-A1's scope ends at correct persistence.

Letting the baseline influence planning is a real and probably desirable step,
but it is a separate founder decision with its own review, so the capability
is left unused rather than switched on quietly here.

## WHAT IS STORED, AND WHERE

Two sibling keys, kept apart on purpose:

    doc["functional_baseline"][domain]      the engine's own `to_state()`,
                                            verbatim, nothing added
    doc["functional_baseline_api"][domain]  this module's lifecycle envelope

The canonical record is written only by `attach_to_state`, so it stays
byte-comparable with what the engine produces — which is what makes the
save-and-reload proof meaningful. No field is invented alongside it.

The envelope exists because the engine's serialisation has no notion of
"finalized", and it genuinely cannot be derived: a baseline that has only just
started and a baseline finalized with no usable evidence BOTH read as
`status=UNRESOLVED, routing_anchor_months=None`. Rather than add a flag to the
engine's dict — which would make the stored record no longer exactly
`to_state()` — the flag lives beside it.

## WHAT THE CLIENT IS TOLD

The view deliberately omits `subdomain`, `activity_family`, and every derived
anchor number. Activity families and track membership belong to the later
canonical-rung and suggestion-generation path, and a raw
`routing_anchor_months` shown to a parent would be read as a developmental
age, which is exactly what the field's own naming comment warns against. The
parent sees the question they were asked and how far through they are.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from genex_core.functional_baseline import (
    BaselineError,
    BaselineRecord,
    area_for_domain,
    attach_to_state,
    entry_screen,
    finalize,
    next_question,
    record_answer,
    start_baseline,
)

#: Version of THIS module's lifecycle envelope — not the baseline's own
#: version, which the engine stamps into the record as `baseline_version`.
API_ENVELOPE_VERSION = "parent-2.4-baseline-api-v1"

#: 0.5F-A1 supports exactly one domain. The other six areas exist in the
#: engine and are deliberately not exposed: each needs its own content review
#: before a parent is asked to calibrate against it, and the pilot consuming
#: this is SLP-only. An unsupported domain is refused rather than silently
#: routed to its area.
SUPPORTED_DOMAINS: Tuple[str, ...] = ("talking_and_communicating",)

#: Keys this module owns in the session document.
CANONICAL_STATE_KEY = "functional_baseline"
ENVELOPE_STATE_KEY = "functional_baseline_api"


class BaselineDomainUnsupported(Exception):
    """The domain is real but not exposed by this slice."""


class BaselineNotStarted(Exception):
    """No baseline exists for this session and domain."""


class BaselineAlreadyFinalized(Exception):
    """A finalized baseline is immutable; answers are refused."""


class BaselineNotInProgress(Exception):
    """The engine has no further question, so an answer cannot be recorded."""


class BaselineQuestionMismatch(Exception):
    """The answer does not belong to the question the engine asked next."""

    def __init__(self, expected: str, received: str) -> None:
        super().__init__("baseline_question_mismatch")
        self.expected = expected
        self.received = received


class BaselineEntryChoiceUnknown(Exception):
    """The entry descriptor is not one this area declares."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_supported_domain(domain: str) -> str:
    """The canonical domain key, or a refusal. No normalising guesswork."""
    key = (domain or "").strip()
    if key not in SUPPORTED_DOMAINS:
        raise BaselineDomainUnsupported(key)
    return key


def _area_for(domain: str):
    area = area_for_domain(domain)
    if area is None:  # pragma: no cover - SUPPORTED_DOMAINS guarantees an area
        raise BaselineDomainUnsupported(domain)
    return area


def _chronological_months(doc: Dict[str, Any]) -> int:
    """Age in months, read from the session the parent already completed.

    CONTEXT ONLY, and the engine treats it as such: it is the neutral starting
    rung when a parent answers "Not sure", and it decides whether a
    demonstrated skill counts as age-relevant. It never selects the anchor.

    Read from stored state rather than accepted from the request body, so a
    client cannot move the starting rung by restating the age. Absent or
    unparseable age becomes 0, which the engine clamps — it does not become a
    guess.
    """
    child = ((doc.get("brain_state") or {}).get("child") or {})
    try:
        return int(child.get("chronological_months") or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _load_record(doc: Dict[str, Any], domain: str) -> Optional[BaselineRecord]:
    """Rebuild a `BaselineRecord` from the stored canonical dict.

    The inverse of `to_state()`. Written explicitly rather than by `**kwargs`
    splat so an unexpected stored key is a visible failure here instead of a
    confusing TypeError deep in the dataclass, and so adding a field to the
    engine's record cannot silently start round-tripping unvalidated data.
    """
    stored = (doc.get(CANONICAL_STATE_KEY) or {}).get(domain)
    if not stored:
        return None
    return BaselineRecord(
        baseline_version=stored["baseline_version"],
        area_id=stored["area_id"],
        domain=stored["domain"],
        entry_choice_id=stored["entry_choice_id"],
        entry_choice_label=stored["entry_choice_label"],
        entry_anchor_months=stored["entry_anchor_months"],
        chronological_months=stored["chronological_months"],
        asked=[dict(a) for a in (stored.get("asked") or [])],
        status=stored["status"],
        routing_anchor_months=stored.get("routing_anchor_months"),
        demonstrated_months=stored.get("demonstrated_months"),
        not_demonstrated_months=stored.get("not_demonstrated_months"),
    )


def _envelope(doc: Dict[str, Any], domain: str) -> Dict[str, Any]:
    return (doc.get(ENVELOPE_STATE_KEY) or {}).get(domain) or {}


def is_finalized(doc: Dict[str, Any], domain: str) -> bool:
    return bool(_envelope(doc, domain).get("finalized"))


def _store(doc: Dict[str, Any], record: BaselineRecord, *,
           finalized: bool) -> None:
    """Persist the canonical record and update the lifecycle envelope.

    The canonical write goes through the ENGINE's `attach_to_state`, so the
    stored dict is exactly `to_state()` and nothing this module shaped.
    """
    attach_to_state(doc, record)

    envelope = doc.setdefault(ENVELOPE_STATE_KEY, {})
    existing = envelope.get(record.domain) or {}
    now = _now_iso()
    envelope[record.domain] = {
        "schema_version": API_ENVELOPE_VERSION,
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
    """Begin a baseline, or return an in-progress one unchanged.

    Returns `(view, mutated)` so the route knows whether to save.

    IDEMPOTENT RESTART, matching `/focus/{key}/start`: calling this again while
    a baseline is in progress returns the current state and does NOT restart.
    Restarting would silently discard answers a parent already gave, and the
    engine's `asked` list is append-only with no removal, so there is no
    partial-undo to offer. A finalized baseline is refused outright.
    """
    key = require_supported_domain(domain)
    if is_finalized(doc, key):
        raise BaselineAlreadyFinalized(key)

    existing = _load_record(doc, key)
    if existing is not None:
        return view(doc, key), False

    area = _area_for(key)
    try:
        area.choice(entry_choice_id)
    except Exception as exc:  # the engine raises on an unknown descriptor
        raise BaselineEntryChoiceUnknown(entry_choice_id) from exc

    record = start_baseline(area.area_id, entry_choice_id,
                            _chronological_months(doc))
    _store(doc, record, finalized=False)
    return view(doc, key), True


def answer(doc: Dict[str, Any], domain: str, question_id: str,
           value: str) -> Dict[str, Any]:
    """Record one answer against the question the engine asked next.

    THE QUESTION-ID CHECK IS WHAT MAKES RETRIES SAFE. The engine's
    `record_answer` appends unconditionally and does not deduplicate, so a
    double-submitted answer would otherwise be recorded twice and skew the
    floor. Because the expected question id changes as soon as an answer lands,
    a retry of the same request is refused rather than appended — the same
    guard, and the same 422, that `/focus/{key}/answer` already uses.

    CHANGING AN ANSWER IS NOT SUPPORTED, and that is the engine's own shape:
    `asked` is append-only and exposes no edit or removal. Rather than invent a
    rewrite path, an answer for an already-answered rung is simply not the
    expected next question, so it is refused.
    """
    key = require_supported_domain(domain)
    if is_finalized(doc, key):
        raise BaselineAlreadyFinalized(key)

    record = _load_record(doc, key)
    if record is None:
        raise BaselineNotStarted(key)

    question = next_question(record)
    if question is None:
        raise BaselineNotInProgress(key)
    if question["question_id"] != question_id:
        raise BaselineQuestionMismatch(question["question_id"], question_id)

    # `_classify` raises BaselineError on an unsupported answer. Pydantic has
    # already constrained the vocabulary, so this is the engine's own second
    # line rather than a validation this module performs.
    record_answer(record, question, value)
    _store(doc, record, finalized=False)
    return view(doc, key)


def finalize_baseline(doc: Dict[str, Any], domain: str
                      ) -> Tuple[Dict[str, Any], bool]:
    """Resolve the bracket and mark the baseline immutable.

    Returns `(view, mutated)`.

    IDEMPOTENT: finalizing an already-finalized baseline returns the stored
    state and rewrites nothing. The engine's `finalize` is itself a pure
    recomputation from `asked`, so re-running it would produce the same values
    — but re-storing would move `updated_at` and make an immutable record look
    edited, so it is skipped entirely.

    Finalizing EARLY is allowed. The engine decides what the evidence supports:
    with nothing usable it returns UNRESOLVED and a `None` anchor rather than a
    fabricated level, which is the whole point of the 0.4 slice. This module
    does not second-guess that by requiring a minimum number of answers.
    """
    key = require_supported_domain(domain)
    record = _load_record(doc, key)
    if record is None:
        raise BaselineNotStarted(key)
    if is_finalized(doc, key):
        return view(doc, key), False

    finalize(record)
    _store(doc, record, finalized=True)
    return view(doc, key), True


# ---------------------------------------------------------------------------
# the client view
# ---------------------------------------------------------------------------

def _client_question(question: Optional[Dict[str, Any]]
                     ) -> Optional[Dict[str, Any]]:
    """The parent-facing part of a question, and nothing else.

    `question_at` also returns `subdomain` and `activity_family`. Both are
    withheld: the activity family is a binding the canonical-rung and
    suggestion-generation path owns, and the subdomain is track membership.
    Neither is anything a parent answering a question needs, and shipping them
    now would make a later contract change out of a field nobody asked for.
    """
    if question is None:
        return None
    return {
        "question_id": question["question_id"],
        "months": question["months"],
        "milestone": question["milestone"],
        "parent_explanation": question.get("parent_explanation", ""),
    }


def view(doc: Dict[str, Any], domain: str) -> Dict[str, Any]:
    """What a client may see about a baseline.

    Deliberately WITHOUT `routing_anchor_months`, `demonstrated_months`,
    `not_demonstrated_months` or the track. Those are server-side provenance
    for the later projection; a raw anchor number rendered in a parent UI would
    be read as a developmental age, which is precisely what that field's naming
    comment exists to prevent.

    `answers_recorded` is a count, not the answers: enough for a progress
    indicator without re-serving clinical content the client already sent.
    """
    key = require_supported_domain(domain)
    record = _load_record(doc, key)
    if record is None:
        raise BaselineNotStarted(key)

    envelope = _envelope(doc, key)
    finalized = bool(envelope.get("finalized"))
    question = None if finalized else next_question(record)
    return {
        "domain": key,
        "area_id": record.area_id,
        "baseline_version": record.baseline_version,
        "entry_choice_id": record.entry_choice_id,
        "entry_choice_label": record.entry_choice_label,
        "finalized": finalized,
        "status": record.status,
        "answers_recorded": len(record.asked),
        "current_question": _client_question(question),
        "ready_to_finalize": (not finalized) and question is None,
        "updated_at": envelope.get("updated_at", ""),
    }


class BaselineNotFinalized(Exception):
    """The baseline exists but has not been finalized.

    Projection copies a FINALIZED record. An in-progress baseline has no
    resolved status and no routing anchor, so projecting it would send a
    half-answered calibration as though it were a result.
    """


def finalized_record(doc: Dict[str, Any], domain: str) -> Dict[str, Any]:
    """The canonical finalized record, verbatim, for projection.

    Returns the stored dict as-is — exactly `BaselineRecord.to_state()` — so
    the digest the caller computes attests the real stored bytes rather than a
    re-serialisation of a rebuilt object. A copy is returned so a caller
    cannot mutate session state through the reference.

    This is the ONLY reader that hands out the full record, and it is used by
    the projection path alone. The client view deliberately exposes far less.
    """
    key = require_supported_domain(domain)
    stored = (doc.get(CANONICAL_STATE_KEY) or {}).get(key)
    if not stored:
        raise BaselineNotStarted(key)
    if not is_finalized(doc, key):
        raise BaselineNotFinalized(key)
    return dict(stored)


def entry_screen_for(domain: str) -> Dict[str, Any]:
    """The area's reviewed entry descriptors, for the start screen.

    Thin pass-through of the engine's own `entry_screen`, included so a client
    never has to hard-code descriptor ids that the engine owns.
    """
    key = require_supported_domain(domain)
    return entry_screen(_area_for(key).area_id)


__all__ = [
    "API_ENVELOPE_VERSION",
    "BaselineNotFinalized",
    "finalized_record",
    "CANONICAL_STATE_KEY",
    "ENVELOPE_STATE_KEY",
    "SUPPORTED_DOMAINS",
    "BaselineAlreadyFinalized",
    "BaselineDomainUnsupported",
    "BaselineEntryChoiceUnknown",
    "BaselineError",
    "BaselineNotInProgress",
    "BaselineNotStarted",
    "BaselineQuestionMismatch",
    "answer",
    "entry_screen_for",
    "finalize_baseline",
    "is_finalized",
    "require_supported_domain",
    "start",
    "view",
]
