"""pilot_backend/transport/projection_v2_request.py — the A2 v2 request codec.

A DEDICATED decoder for the v2 private route. v1's body is three keys holding
seven scalars and its inline check in `projection_wsgi` is proportionate to that.
A v2 body carries two nested lists and free-text milestone prose, so its
validation is large enough to be its own testable unit rather than a few lines
inside a WSGI handler.

## WHY THIS IS STRICT IN BOTH DIRECTIONS

Every key set is EXACT: an unknown key is refused, and a missing key is refused.
Nothing is ignored.

"Ignore extra keys" is the specific behaviour that makes a trust boundary rot.
An ignored key means the sender believes it transmitted something and the
receiver believes it received a complete payload, and both are right about their
own half. If Parent one day sends `raw_answer` or `asked`, this boundary must
FAIL rather than quietly discard it — a silent drop teaches the sender that
sending it is fine.

The same strictness in the other direction is what makes a missing `state` or a
missing band total impossible to read as a default. There is no default here. A
skill with no state is not an unassessed skill; it is a malformed request.

## WHY THE SIZE CAP COMES BEFORE THE PARSE

`json.loads` on a hostile body is the expensive, attackable step. The declared
`Content-Length` is checked first, so an oversized body is refused without ever
being decoded, and the list caps then bound the work done after parsing. A
genuine v2 payload is a few kilobytes; the cap sits an order of magnitude above
that, which leaves room for a longer track without leaving room for an attack.

## WHAT THE ERRORS MAY SAY

Internal messages name the FIELD and never the VALUE. "a skill row requires
`state`" is a shape fact and safe to raise; echoing the state, the milestone or
the session id would put clinical content into an exception that something might
later log. The transport collapses all of these to one constant external string
anyway — this layer's discipline is about what reaches a log, not what reaches
the caller.

## WHAT THIS CODEC DELIBERATELY DOES NOT DO

It does not resolve a canonical rung, check a denominator against the Gold
Standard, or decide whether a band is complete. Those need the frozen artifact
and belong to the canonicalisation boundary. This module's single question is
whether the bytes are a well-formed v2 request.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Tuple

from ..domain.parent_baseline_projection_v2 import (
    FORBIDDEN_FIELDS,
    PROJECTED_STATES,
    SUMMARY_FIELDS,
)

#: Exactly the five top-level keys. `child_id`, `projection_id`,
#: `source_system`, `projected_at` and `projection_schema` are absent because
#: the Pilot DERIVES every one — sending one is refused here, not ignored.
BODY_KEYS: Tuple[str, ...] = (
    "source_session_id", "source_record_digest", "summary", "skills",
    "band_totals",
)

#: Exactly the five fields consumed per skill. `milestone` and `subdomain` are
#: TRANSIENT — needed to resolve the canonical rung and never persisted.
SKILL_KEYS: Tuple[str, ...] = (
    "domain", "subdomain", "months", "milestone", "state")

#: Exactly the two fields of a band denominator.
BAND_TOTAL_KEYS: Tuple[str, ...] = ("months", "total_skills")

#: A v2 payload is a few kilobytes. An order of magnitude of headroom leaves
#: room for a longer declared track and none for an attack.
MAX_BODY_BYTES = 32 * 1024

#: A baseline enters at most three bands, and the widest declared band in the
#: frozen source holds four skills. 64 is far above any real assessment while
#: still bounding the work after the parse.
MAX_SKILLS = 64
MAX_BAND_TOTALS = 16

#: Milestone prose is a sentence, not a document.
MAX_MILESTONE_CHARS = 512
MAX_IDENTIFIER_CHARS = 128

#: 0 to 20 years. A band outside this is not a developmental month.
MIN_MONTHS, MAX_MONTHS = 0, 240

#: A band holds at least one declared skill and never more skills than the
#: whole request may carry.
MIN_BAND_TOTAL, MAX_BAND_TOTAL = 1, MAX_SKILLS


class ProjectionV2RequestError(Exception):
    """The request is not a well-formed v2 projection. PHI-safe by construction.

    Messages name fields, never values. The transport maps every instance to one
    constant external string, so this type never becomes an oracle.
    """

    PHI_SAFE_MESSAGE = True


class ProjectionV2RequestTooLarge(ProjectionV2RequestError):
    """The declared body exceeded `MAX_BODY_BYTES`. Separate so it maps to 413."""


def _exact_keys(value: Any, expected: Tuple[str, ...], what: str) -> Mapping:
    """A mapping whose key set is EXACTLY `expected`. Both directions."""
    if not isinstance(value, dict):
        raise ProjectionV2RequestError(f"{what} must be an object")
    present = set(value)
    wanted = set(expected)
    if present - wanted:
        raise ProjectionV2RequestError(f"{what} carries an unexpected field")
    if wanted - present:
        raise ProjectionV2RequestError(f"{what} is missing a required field")
    return value


def _bounded_str(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ProjectionV2RequestError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise ProjectionV2RequestError(f"{field} must not be empty")
    if len(text) > limit:
        raise ProjectionV2RequestError(f"{field} is too long")
    return text


def _bounded_int(value: Any, field: str, low: int, high: int) -> int:
    # `bool` is an `int` in Python, and `True` would silently become month 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectionV2RequestError(f"{field} must be an integer")
    if not (low <= value <= high):
        raise ProjectionV2RequestError(f"{field} is out of range")
    return value


def _opt_int(value: Any, field: str) -> Any:
    """An integer month that may genuinely be absent.

    `None` is a real value for `routing_anchor_months` — an UNRESOLVED baseline
    has no anchor — and must not be coerced to zero, which would be a
    measurement nobody made.
    """
    if value is None:
        return None
    return _bounded_int(value, field, MIN_MONTHS, MAX_MONTHS)


def _digest(value: Any) -> str:
    """A lowercase sha256 hex digest, EXACTLY.

    Not normalised: `hexdigest()` is already lowercase, so anything else did not
    come from the canonical digest function and should be refused rather than
    coerced into matching.
    """
    if not isinstance(value, str):
        raise ProjectionV2RequestError("source_record_digest must be a string")
    text = value.strip()
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise ProjectionV2RequestError(
            "source_record_digest must be a sha256 hex digest")
    return text


def _summary(raw: Any) -> Dict[str, Any]:
    """The seven compatibility fields, each checked by name and by type."""
    value = _exact_keys(raw, SUMMARY_FIELDS, "summary")
    for forbidden in FORBIDDEN_FIELDS:
        if forbidden in value:
            # Unreachable while the key set is exact, and kept as a second line
            # on purpose: if SUMMARY_FIELDS ever grew, this would catch a
            # forbidden name entering through it rather than trusting review.
            raise ProjectionV2RequestError(
                "summary carries a field that must never cross the boundary")
    return {
        "domain": _bounded_str(value["domain"], "domain",
                               MAX_IDENTIFIER_CHARS),
        "area_id": _bounded_str(value["area_id"], "area_id",
                                MAX_IDENTIFIER_CHARS),
        "entry_choice_id": _bounded_str(value["entry_choice_id"],
                                        "entry_choice_id",
                                        MAX_IDENTIFIER_CHARS),
        "status": _bounded_str(value["status"], "status",
                               MAX_IDENTIFIER_CHARS),
        "baseline_version": _bounded_str(value["baseline_version"],
                                         "baseline_version",
                                         MAX_IDENTIFIER_CHARS),
        "routing_anchor_months": _opt_int(value["routing_anchor_months"],
                                          "routing_anchor_months"),
        "not_demonstrated_months": _opt_int(value["not_demonstrated_months"],
                                            "not_demonstrated_months"),
    }


def _skills(raw: Any) -> List[Dict[str, Any]]:
    """One row per assessed skill: exact keys, strict state, no duplicates.

    DUPLICATE DETECTION runs on `(domain, subdomain, months, milestone)` — the
    Parent-side source identity — rather than on the canonical ref, because the
    canonical ref does not exist yet at this layer. Canonicalisation refuses a
    duplicate ref as well, so two different source rows that resolve to one rung
    are also caught; this check catches the simpler case of the same row twice,
    where otherwise one state would silently overwrite the other and the band
    count would be short by one.
    """
    if not isinstance(raw, list):
        raise ProjectionV2RequestError("skills must be a list")
    if not raw:
        raise ProjectionV2RequestError("skills must not be empty")
    if len(raw) > MAX_SKILLS:
        raise ProjectionV2RequestError("skills carries too many rows")

    out: List[Dict[str, Any]] = []
    seen = set()
    for row in raw:
        value = _exact_keys(row, SKILL_KEYS, "a skill row")
        state = value["state"]
        if not isinstance(state, str) or state not in PROJECTED_STATES:
            # A STRICT enum, and `unassessed` is deliberately not in it: that is
            # the ABSENCE of a record, not a value one can transmit.
            raise ProjectionV2RequestError("a skill row has an invalid state")
        skill = {
            "domain": _bounded_str(value["domain"], "a skill row's domain",
                                   MAX_IDENTIFIER_CHARS),
            "subdomain": _bounded_str(value["subdomain"],
                                      "a skill row's subdomain",
                                      MAX_IDENTIFIER_CHARS),
            "months": _bounded_int(value["months"], "a skill row's months",
                                   MIN_MONTHS, MAX_MONTHS),
            "milestone": _bounded_str(value["milestone"],
                                      "a skill row's milestone",
                                      MAX_MILESTONE_CHARS),
            "state": state,
        }
        identity = (skill["domain"], skill["subdomain"], skill["months"],
                    " ".join(skill["milestone"].split()))
        if identity in seen:
            raise ProjectionV2RequestError("two skill rows share one identity")
        seen.add(identity)
        out.append(skill)
    return out


def _band_totals(raw: Any) -> List[Dict[str, Any]]:
    """Parent's declared denominator per band. One entry per band, exactly."""
    if not isinstance(raw, list):
        raise ProjectionV2RequestError("band_totals must be a list")
    if not raw:
        raise ProjectionV2RequestError("band_totals must not be empty")
    if len(raw) > MAX_BAND_TOTALS:
        raise ProjectionV2RequestError("band_totals carries too many rows")

    out: List[Dict[str, Any]] = []
    seen = set()
    for row in raw:
        value = _exact_keys(row, BAND_TOTAL_KEYS, "a band total")
        months = _bounded_int(value["months"], "a band total's months",
                              MIN_MONTHS, MAX_MONTHS)
        if months in seen:
            # Two denominators for one band would make completeness depend on
            # which entry a reader happened to find first.
            raise ProjectionV2RequestError(
                "two band totals name the same band")
        seen.add(months)
        out.append({
            "months": months,
            "total_skills": _bounded_int(
                value["total_skills"], "a band total's total_skills",
                MIN_BAND_TOTAL, MAX_BAND_TOTAL),
        })
    return out


def decode_body(parsed: Any) -> Dict[str, Any]:
    """Validate an already-parsed JSON object into a normalised v2 request.

    Separate from `read_request` so the validation is testable without a WSGI
    environ, and so the size cap stays where it has to be — before the parse.
    """
    value = _exact_keys(parsed, BODY_KEYS, "the request body")
    return {
        "source_session_id": _bounded_str(
            value["source_session_id"], "source_session_id",
            MAX_IDENTIFIER_CHARS),
        "source_record_digest": _digest(value["source_record_digest"]),
        "summary": _summary(value["summary"]),
        "skills": _skills(value["skills"]),
        "band_totals": _band_totals(value["band_totals"]),
    }


def read_request(environ: Mapping[str, Any]) -> Dict[str, Any]:
    """Read, size-check, parse and validate one v2 projection request.

    The order is the point: the declared length is checked BEFORE the body is
    read or decoded, so an oversized payload never reaches `json.loads`.
    """
    try:
        declared = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        raise ProjectionV2RequestError("a body is required")
    if declared <= 0:
        raise ProjectionV2RequestError("a body is required")
    if declared > MAX_BODY_BYTES:
        raise ProjectionV2RequestTooLarge("body too large")

    stream = environ.get("wsgi.input")
    raw = stream.read(declared) if stream is not None else b""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        # The decode error is NOT propagated. `json`'s message quotes the
        # offending document fragment, which here would be milestone prose.
        raise ProjectionV2RequestError("a JSON object body is required")
    return decode_body(parsed)


__all__ = [
    "BAND_TOTAL_KEYS",
    "BODY_KEYS",
    "MAX_BAND_TOTALS",
    "MAX_BODY_BYTES",
    "MAX_MILESTONE_CHARS",
    "MAX_SKILLS",
    "ProjectionV2RequestError",
    "ProjectionV2RequestTooLarge",
    "SKILL_KEYS",
    "decode_body",
    "read_request",
]
