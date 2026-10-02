"""pilot_backend/transport/body.py — the ONE place a request body is read.

0.1 through 0.5B read no request body at all. That was not an oversight and it
is worth restating before widening it: a body is the one place a forged `role`,
`caregiver_id`, `provider_id` or `auth_subject` can arrive, and an application
with no code that looks cannot be fooled by one.

0.5C has to widen it. A clinician modifying a suggested goal's wording, writing
a clinical interpretation, or recording a number of minutes is supplying
CONTENT, and content cannot travel in a path segment. So the boundary moves
from "never read a body" to "read a body through exactly one function, with an
explicit per-route field allowlist".

## What this function refuses, and why each refusal exists

**Unknown fields are REJECTED, not ignored.** A silently-ignored field is how a
client comes to believe it has set something. If the caller sends
`emphasis_weight` to a route whose allowlist does not include it, the request
fails rather than quietly doing something else.

**Identity fields are rejected outright, by name.** `IDENTITY_FIELDS` can never
appear in any route's allowlist — a structural test asserts that over this
module — so the check below is a second, independent line of defence rather
than the only one. Identity comes from the verified token, through
`resolve_principal`, and there is no code path in this application that reads
an actor id from a body.

**Only a JSON object.** A list, a bare string or a number is refused, so no
route can be handed a shape it did not expect.

**A size cap, enforced before parsing.** `MAX_BODY_BYTES` is deliberately small
— these payloads are a few short fields — and the cap is applied to the
declared `CONTENT_LENGTH` *and* to what is actually read, because a client can
lie about the former.

**Nothing is logged.** A body may legitimately carry clinical text in 0.5C
(`clinical_interpretation`, a modified goal target). This module never logs,
never puts a field value in an exception message, and raises `BodyError` whose
message names only the FIELD, never its value.

## What this is not

Not a validator. Types, ranges, enums and domain invariants belong to the
services and the domain objects, which already enforce them and already have
tests. This function decides which keys may be present at all; what they mean
is settled further in.
"""

from __future__ import annotations

import json
from typing import Mapping, Optional, Sequence

#: Small on purpose. The largest legitimate 0.5C payload is a clinical
#: interpretation plus a few short fields.
MAX_BODY_BYTES = 16 * 1024

#: Never permitted in any body, on any route, ever.
#:
#: These are the fields a client would forge to act as somebody else. A
#: structural test asserts that no route allowlist intersects this set, so a
#: future route cannot accept one by accident.
IDENTITY_FIELDS = frozenset({
    "auth_subject", "subject", "uid", "email", "owner_uid",
    "role", "actor_role", "actor_id", "application_id",
    "caregiver_id", "provider_id", "practice_id", "principal",
    "managing_assignment_id", "assignment_id",
    # A child id is always a PATH segment, so a body carrying one would be a
    # second, unauthorized way to name the subject of an operation.
    "child_id",
})


class BodyError(ValueError):
    """A request body could not be accepted. PHI-safe: names fields, not values."""

    PHI_SAFE_MESSAGE = True


def read_json_body(environ: Mapping[str, object], *,
                   allowed: Sequence[str],
                   required: Sequence[str] = ()) -> dict:
    """Parse and validate a JSON object body against an explicit allowlist.

    Returns a dict containing only keys from `allowed`. Raises `BodyError` for
    anything else, including an unknown key.
    """
    permitted = frozenset(allowed)
    forbidden = permitted & IDENTITY_FIELDS
    if forbidden:
        # A programming error, not a request error: this route should never
        # have been written. Raised eagerly so it cannot ship quietly.
        raise BodyError(
            f"route allowlist contains identity fields: {sorted(forbidden)}")

    declared = str(environ.get("CONTENT_LENGTH") or "").strip()
    length = 0
    if declared:
        try:
            length = int(declared)
        except ValueError:
            raise BodyError("content length is not a number") from None
    if length < 0 or length > MAX_BODY_BYTES:
        raise BodyError("request body too large")

    stream = environ.get("wsgi.input")
    if stream is None or length == 0:
        payload = b""
    else:
        # Read one byte more than permitted so a client understating
        # CONTENT_LENGTH cannot slip a larger body past the cap above.
        payload = stream.read(min(length, MAX_BODY_BYTES) + 1)
        if len(payload) > MAX_BODY_BYTES:
            raise BodyError("request body too large")

    if not payload:
        body: dict = {}
    else:
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            # The SDK/stdlib message can quote the payload, so it is discarded.
            raise BodyError("request body is not valid JSON") from None
        if not isinstance(parsed, dict):
            raise BodyError("request body must be a JSON object")
        body = parsed

    present = set(body)
    unknown = present - permitted
    if unknown:
        # Named, because the caller needs to fix their request — and these are
        # KEYS, not values, so naming them discloses nothing.
        raise BodyError(f"unexpected fields: {sorted(unknown)}")
    identity = present & IDENTITY_FIELDS
    if identity:
        raise BodyError(f"identity may not be supplied: {sorted(identity)}")
    missing = [field for field in required if not str(body.get(field) or "").strip()]
    if missing:
        raise BodyError(f"missing required fields: {sorted(missing)}")
    return body


def opt_str(body: Mapping[str, object], field: str, default: str = "") -> str:
    value = body.get(field, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise BodyError(f"{field} must be a string")
    return value.strip()


def opt_int(body: Mapping[str, object], field: str) -> Optional[int]:
    """An integer, or None when absent.

    `bool` is rejected explicitly: it is a subclass of `int` in Python, so
    `True` would otherwise silently become 1 minute.
    """
    if field not in body or body[field] is None:
        return None
    value = body[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise BodyError(f"{field} must be an integer")
    return value


def opt_bool(body: Mapping[str, object], field: str) -> Optional[bool]:
    if field not in body or body[field] is None:
        return None
    value = body[field]
    if not isinstance(value, bool):
        raise BodyError(f"{field} must be a boolean")
    return value


def opt_str_list(body: Mapping[str, object], field: str) -> list:
    """A list of non-empty strings, or [] when absent.

    Used for `reviewed_event_ids`. The service re-validates that every id
    belongs to the right child — this only settles the shape.
    """
    if field not in body or body[field] is None:
        return []
    value = body[field]
    if not isinstance(value, list):
        raise BodyError(f"{field} must be a list")
    found = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise BodyError(f"{field} must contain non-empty strings")
        found.append(item.strip())
    return found


def enum_member(enum_cls, body: Mapping[str, object], field: str,
                *, default=None):
    """Resolve a short enum VALUE to its member, or raise.

    Matched on `.value`, never on the member NAME, so the wire format is the
    documented lowercase string rather than an internal identifier.
    """
    raw = opt_str(body, field)
    if not raw:
        if default is not None:
            return default
        raise BodyError(f"{field} is required")
    for member in enum_cls:
        if member.value == raw:
            return member
    # Lists the permitted values, which are a closed public vocabulary.
    raise BodyError(
        f"{field} must be one of {sorted(m.value for m in enum_cls)}")
