"""pilot_backend/observability/safe_logging.py — logs that cannot carry PHI.

The therapist service already logs with an allowlist of structured extras, and
that approach is adopted here rather than reinvented. Two things are added,
both addressing gaps a PHI system cannot leave open.

## 1. The field allowlist is enforced, not just documented

`format_log` RAISES on a field outside `ALLOWED_LOG_FIELDS`. A formatter that
silently drops unknown fields trains developers to pass whatever they like and
assume it is handled; the day the allowlist is widened, everything they passed
starts being written. Raising means an unsafe log line fails in a test.

## 2. Exception text is opt-in, never blind

A third-party exception's message is attacker- and payload-influenced. A
Firestore client can put document contents in an error; an HTTP library can put
a URL with a query string in one; a JSON parser can quote the fragment it
choked on. So:

    * exceptions whose class declares `PHI_SAFE_MESSAGE = True` — every error
      type defined in this package — log their message, because we wrote it
      and it names a field, key or status, never a value;
    * every other exception logs its CLASS NAME ONLY.

That inverts the usual default. It costs some debugging convenience for
third-party failures, and the alternative is a clinical note in a log sink with
a different retention policy and a wider access list than the database it came
from.

## No PHI in paths or query strings

`route` must be a path TEMPLATE (`/children/{child_id}`), not a populated path.
`format_log` rejects a route containing `?` or a path segment that looks like a
populated application id, so a caller cannot log a URL with identifiers or
search terms in it.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

#: The complete set of loggable fields. Everything here is operational
#: metadata or an opaque internal identifier — never clinical content, never a
#: credential, never a name or an email.
ALLOWED_LOG_FIELDS = frozenset({
    "request_id",
    "event",
    "severity",
    "message",
    "route",          # template only
    "method",
    "status",
    "duration_ms",
    "environment",
    "actor_id",       # opaque application id
    "actor_role",
    "child_id",       # opaque application id
    "resource_type",
    "resource_id",    # opaque application id
    "denial_reason",
    "exception_type",
    "exception_message",  # only ever set from a PHI-safe exception
})

#: Fields that must never appear, even if the allowlist were widened by
#: mistake. Checked independently so the two lists have to fail together.
FORBIDDEN_LOG_FIELDS = frozenset({
    "authorization", "bearer", "token", "id_token", "refresh_token",
    "api_key", "apikey", "secret", "password", "credential",
    "body", "request_body", "response_body", "payload",
    "child_name", "name", "display_name", "email", "caregiver_email",
    "diagnosis", "concern", "note", "note_text", "comment", "feedback",
    "message_text", "prompt", "completion", "ai_response", "transcript",
})

_ID_IN_PATH = re.compile(r"/(prac|prov|cgvr|chld|ccxn|pcxn|audt|revn)_[0-9a-f]{8,}")


class LogFieldError(ValueError):
    """A log line attempted to carry a field that is not permitted.

    PHI-safe: names the offending FIELD, never its value — raising an error
    that quoted the value would defeat the check that produced it.
    """

    PHI_SAFE_MESSAGE = True


class SafeLogRecord(dict):
    """A validated log line. Constructing one is the validation."""

    def __init__(self, **fields: Any) -> None:
        super().__init__(format_log(**fields))


def _validate_route(route: str) -> str:
    if "?" in route:
        raise LogFieldError("route: query strings must not be logged")
    if _ID_IN_PATH.search(route):
        raise LogFieldError("route: log the path template, not a populated path")
    return route


def format_log(**fields: Any) -> Mapping[str, Any]:
    """Validate and normalise one structured log line.

    Raises `LogFieldError` for any field outside the allowlist, any explicitly
    forbidden field, and any route carrying identifiers or a query string.
    """
    validated: dict = {}
    for key, value in fields.items():
        lowered = key.lower()
        if lowered in FORBIDDEN_LOG_FIELDS:
            raise LogFieldError(f"field must never be logged: {key}")
        if key not in ALLOWED_LOG_FIELDS:
            raise LogFieldError(f"field is not in the log allowlist: {key}")
        if value is None:
            continue
        if key == "route":
            value = _validate_route(str(value))
        if isinstance(value, (str, int, float, bool)):
            validated[key] = value
        else:
            raise LogFieldError(f"field must be a scalar: {key}")
    return validated


def describe_exception(exc: BaseException) -> Mapping[str, str]:
    """Render an exception for logging, opt-in on the message.

    Only exception classes declaring `PHI_SAFE_MESSAGE = True` contribute their
    text. Everything else contributes its type alone.
    """
    described = {"exception_type": type(exc).__name__}
    if getattr(type(exc), "PHI_SAFE_MESSAGE", False):
        message = str(exc)
        if message and "\n" not in message:
            described["exception_message"] = message
    return described


def redact_bearer(header_value: str) -> str:
    """Render an Authorization header safe to mention at all.

    Never returns any part of the credential — not a prefix, not a suffix, not
    a length. A token fragment is still a token fragment, and a length is a
    fingerprint.
    """
    return "Bearer <redacted>" if (header_value or "").strip() else "<absent>"


def render(record: Mapping[str, Any]) -> str:
    """Serialize a validated record as one JSON line."""
    return json.dumps(dict(record), ensure_ascii=False, sort_keys=True)
