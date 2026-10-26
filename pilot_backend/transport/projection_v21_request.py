"""A2 v2.1 — an explicitly versioned SIBLING codec. v2 is untouched.

## WHY A SIBLING AND NOT A WIDER v2

`projection_v2_request.decode_body` calls `_exact_keys(parsed, BODY_KEYS, ...)`,
so the v2 body is exactly five keys in both directions. Widening it would mean
every historical v2 projection had been accepted under a schema that no longer
exists, and a v2 client would start being interpreted under rules it was never
written against.

So v2.1 is a separate module with its own key set and its own route. v2 keeps
its five keys, its validation and its historical behaviour verbatim; this file
REUSES v2's parsers for the three clinical parts rather than copying them,
because two implementations of "what a skill row may contain" is how the two
versions would drift into disagreeing about the same bytes.

## DISPATCH IS BY ROUTE, NEVER BY SHAPE

A v2.1 body is recognised because it arrived at the v2.1 path. Sniffing for
extra keys would make the two versions mutually ambiguous in exactly the case
that matters — a malformed v2 body with a stray field would silently become a
v2.1 request — and `_exact_keys` means neither decoder will ever accept the
other's body anyway. That mutual refusal is asserted, in both directions.

## THE TWO ADDITIONS ARE SEPARATE KEYS

    clinician_profile          derived clinical context. Parent is the
                               authority; the Pilot cannot reconstruct it.
    parent_content_evaluation  provenance about Parent's content mirror.

Not one nested object. They are different kinds of fact, and a clinician
reading a profile must not be reading a claim about our content library as if
it were a claim about the child.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Tuple

from ..domain.parent_clinician_profile_v21 import (
    AREA_NOT_ASSESSED,
    AREA_STATES,
    INDETERMINATE_REASONS,
    MAX_AREA_KEY_CHARS,
    MAX_AREAS,
    MULTI_AREA_INDETERMINATE,
    MULTI_AREA_STATES,
    NOT_ASSESSED_CAUSES,
    OUTCOME_STATES,
    SERVICEABILITY_STATES,
)
from .projection_v2_request import (
    MAX_IDENTIFIER_CHARS,
    ProjectionV2RequestError,
    ProjectionV2RequestTooLarge,
    _band_totals,
    _bounded_str,
    _digest,
    _exact_keys,
    _skills,
    _summary,
    read_json_body,
)

#: The v2.1 body, exactly. SEVEN keys: v2's five plus the two additions, each
#: named at the top level so neither hides inside the other.
BODY_KEYS_V21: Tuple[str, ...] = (
    "source_session_id", "source_record_digest", "summary", "skills",
    "band_totals", "clinician_profile", "parent_content_evaluation",
)

PROFILE_KEYS: Tuple[str, ...] = (
    "outcome", "areas", "not_assessed_causes", "multi_area",
    "multi_area_indeterminate_reasons",
)

CONTENT_EVALUATION_KEYS: Tuple[str, ...] = (
    "content_set_id", "content_digest", "target_serviceability",
)

#: The v2.1 path. An explicit version boundary, not a content negotiation.
PROJECTION_V21_PATH = "/internal/parent-baseline-projections-v21"

MAX_REASONS = len(INDETERMINATE_REASONS)


class ProjectionV21RequestError(ProjectionV2RequestError):
    """A v2.1 request was refused. PHI-safe: no child data in messages."""


def _enum(value: Any, allowed: Tuple[str, ...], field: str) -> str:
    text = _bounded_str(value, field, MAX_IDENTIFIER_CHARS)
    if text not in allowed:
        raise ProjectionV21RequestError(
            f"{field} is outside the approved vocabulary")
    return text


def _areas(raw: Any) -> Dict[str, str]:
    if not isinstance(raw, Mapping):
        raise ProjectionV21RequestError("areas must be an object")
    if not raw:
        raise ProjectionV21RequestError(
            "a clinician profile with no area state says nothing")
    if len(raw) > MAX_AREAS:
        raise ProjectionV21RequestError("too many profile areas")
    out: Dict[str, str] = {}
    for key, value in raw.items():
        area = _bounded_str(key, "an area key", MAX_AREA_KEY_CHARS)
        out[area] = _enum(value, AREA_STATES, "an area state")
    return out


def _causes(raw: Any, areas: Mapping[str, str]) -> Dict[str, str]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, Mapping):
        raise ProjectionV21RequestError(
            "not_assessed_causes must be an object")
    if len(raw) > MAX_AREAS:
        raise ProjectionV21RequestError("too many not-assessed causes")
    out: Dict[str, str] = {}
    for key, value in raw.items():
        area = _bounded_str(key, "a cause key", MAX_AREA_KEY_CHARS)
        if area not in areas:
            raise ProjectionV21RequestError(
                "a not-assessed cause names an unknown area")
        if areas[area] != AREA_NOT_ASSESSED:
            # A cause on an ASSESSED area is nonsense a clinician could
            # reasonably read as a hedge about real evidence. The record
            # refuses it too; this is the first line.
            raise ProjectionV21RequestError(
                "a not-assessed cause was given for an assessed area")
        out[area] = _enum(value, NOT_ASSESSED_CAUSES, "a not-assessed cause")
    return out


def _reasons(raw: Any) -> List[str]:
    if raw in (None, []):
        return []
    if not isinstance(raw, (list, tuple)):
        raise ProjectionV21RequestError(
            "multi_area_indeterminate_reasons must be a list")
    if len(raw) > MAX_REASONS:
        raise ProjectionV21RequestError("too many indeterminate reasons")
    out = [_enum(value, INDETERMINATE_REASONS, "an indeterminate reason")
           for value in raw]
    if len(set(out)) != len(out):
        raise ProjectionV21RequestError(
            "an indeterminate reason was repeated")
    return out


def _clinician_profile(raw: Any) -> Dict[str, Any]:
    value = _exact_keys(raw, PROFILE_KEYS, "the clinician profile")
    areas = _areas(value["areas"])
    multi_area = _enum(value["multi_area"], MULTI_AREA_STATES, "multi_area")
    reasons = _reasons(value["multi_area_indeterminate_reasons"])
    if multi_area == MULTI_AREA_INDETERMINATE and not reasons:
        raise ProjectionV21RequestError(
            "an indeterminate verdict must say why")
    if multi_area != MULTI_AREA_INDETERMINATE and reasons:
        raise ProjectionV21RequestError(
            "indeterminate reasons were given for a determinate verdict")
    return {
        "outcome": _enum(value["outcome"], OUTCOME_STATES, "outcome"),
        "areas": areas,
        "not_assessed_causes": _causes(value["not_assessed_causes"], areas),
        "multi_area": multi_area,
        "multi_area_indeterminate_reasons": reasons,
    }


def _parent_content_evaluation(raw: Any) -> Dict[str, Any]:
    value = _exact_keys(raw, CONTENT_EVALUATION_KEYS,
                        "the parent content evaluation")
    return {
        "content_set_id": _bounded_str(value["content_set_id"],
                                       "content_set_id",
                                       MAX_IDENTIFIER_CHARS),
        "content_digest": _digest(value["content_digest"]),
        "target_serviceability": _enum(value["target_serviceability"],
                                       SERVICEABILITY_STATES,
                                       "target_serviceability"),
    }


def decode_body_v21(parsed: Any) -> Dict[str, Any]:
    """Validate an already-parsed JSON object into a normalised v2.1 request.

    The three clinical parts go through v2's own parsers, so a skill row means
    exactly what it means in v2 and cannot drift. The two additions are
    validated here.
    """
    value = _exact_keys(parsed, BODY_KEYS_V21, "the request body")
    return {
        "source_session_id": _bounded_str(
            value["source_session_id"], "source_session_id",
            MAX_IDENTIFIER_CHARS),
        "source_record_digest": _digest(value["source_record_digest"]),
        "summary": _summary(value["summary"]),
        "skills": _skills(value["skills"]),
        "band_totals": _band_totals(value["band_totals"]),
        "clinician_profile": _clinician_profile(value["clinician_profile"]),
        "parent_content_evaluation": _parent_content_evaluation(
            value["parent_content_evaluation"]),
    }


def read_request_v21(environ: Mapping[str, Any]) -> Dict[str, Any]:
    """Read, size-cap and decode a v2.1 request from a WSGI environ.

    The size cap and JSON parse are v2's, reused: the limits are a property of
    this transport rather than of a schema version, and a second copy would be
    a second number to keep in step.
    """
    return decode_body_v21(read_json_body(environ))


__all__ = [
    "BODY_KEYS_V21", "PROFILE_KEYS", "CONTENT_EVALUATION_KEYS",
    "PROJECTION_V21_PATH",
    "ProjectionV21RequestError", "ProjectionV2RequestTooLarge",
    "decode_body_v21", "read_request_v21",
]
