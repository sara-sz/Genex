"""0.5C — the request-body boundary.

0.1 through 0.5B read no request body at all, and a test in
`test_integration_identity.py` asserts the identity handlers still never touch
`wsgi.input`. 0.5C has to accept content — a modified goal target, a clinical
interpretation, a number of minutes — so the rule changes from "never read a
body" to "read one through exactly one function with an explicit allowlist".

These tests pin that function, because it is now the only place a forged field
could enter.
"""

from __future__ import annotations

import io
import json

import pytest

from pilot_backend.transport.body import (
    IDENTITY_FIELDS,
    MAX_BODY_BYTES,
    BodyError,
    enum_member,
    opt_bool,
    opt_int,
    opt_str,
    opt_str_list,
    read_json_body,
)


def env(payload, *, declared=None):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return {
        "wsgi.input": io.BytesIO(raw),
        "CONTENT_LENGTH": str(len(raw) if declared is None else declared),
    }


# ===========================================================================
# the allowlist
# ===========================================================================

def test_an_allowed_field_is_returned():
    body = read_json_body(env({"text": "Fictional target"}), allowed=["text"])
    assert body == {"text": "Fictional target"}


def test_an_absent_body_is_an_empty_mapping():
    assert read_json_body(env(b""), allowed=["text"]) == {}


def test_an_unknown_field_is_REJECTED_not_ignored():
    """Silently ignoring a field is how a client comes to believe it set one."""
    with pytest.raises(BodyError) as caught:
        read_json_body(env({"text": "x", "emphasis_weight": 3}),
                       allowed=["text"])
    assert "emphasis_weight" in str(caught.value)


def test_a_missing_required_field_is_refused():
    with pytest.raises(BodyError):
        read_json_body(env({"reason": "x"}), allowed=["text", "reason"],
                       required=["text"])
    # Whitespace does not satisfy a requirement.
    with pytest.raises(BodyError):
        read_json_body(env({"text": "   "}), allowed=["text"], required=["text"])


# ===========================================================================
# identity can never arrive in a body
# ===========================================================================

@pytest.mark.parametrize("field", sorted(IDENTITY_FIELDS))
def test_every_identity_field_is_refused_even_if_allowlisted(field):
    """Two independent lines of defence, and this proves the second.

    Passing the field in `allowed` is itself refused, so a route that tried to
    accept one fails loudly rather than admitting it.
    """
    with pytest.raises(BodyError):
        read_json_body(env({field: "forged"}), allowed=[field])
    # And it is refused when merely PRESENT on a route that does not list it.
    with pytest.raises(BodyError) as caught:
        read_json_body(env({"text": "x", field: "forged"}), allowed=["text"])
    assert field in str(caught.value)


def test_no_identity_field_may_be_allowlisted_by_any_route():
    """Structural: every route's allowlist in the transport layer is clean.

    Scans the real call sites rather than trusting review. A route that
    allowlisted `provider_id` would make the body a second way to name the
    actor, which is exactly the class of defect the no-body rule prevented.
    """
    import ast
    import pathlib

    wsgi = (pathlib.Path(__file__).resolve().parent.parent
            / "transport" / "wsgi_app.py")
    tree = ast.parse(wsgi.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "read_json_body"):
            continue
        for keyword in node.keywords:
            if keyword.arg not in {"allowed", "required"}:
                continue
            if not isinstance(keyword.value, (ast.List, ast.Tuple)):
                continue
            for element in keyword.value.elts:
                if (isinstance(element, ast.Constant)
                        and element.value in IDENTITY_FIELDS):
                    offenders.append((keyword.arg, element.value))
    assert not offenders, offenders


# ===========================================================================
# shape and size
# ===========================================================================

@pytest.mark.parametrize("payload", [b'["a"]', b'"text"', b"42", b"null"])
def test_only_a_json_object_is_accepted(payload):
    with pytest.raises(BodyError):
        read_json_body(env(payload), allowed=["text"])


def test_malformed_json_is_refused_without_quoting_the_payload():
    """The refusal must not echo the body — it may carry clinical text."""
    secret = b'{"text": "Fictional-sentinel-ABC123", '
    with pytest.raises(BodyError) as caught:
        read_json_body(env(secret), allowed=["text"])
    assert "Fictional-sentinel-ABC123" not in str(caught.value)


def test_an_oversized_body_is_refused():
    with pytest.raises(BodyError):
        read_json_body(env(b"x" * (MAX_BODY_BYTES + 10)), allowed=["text"])


def test_a_client_understating_content_length_cannot_beat_the_cap():
    """The cap applies to what is READ, not only to what is declared."""
    oversized = json.dumps({"text": "x" * (MAX_BODY_BYTES + 100)}).encode()
    with pytest.raises(BodyError):
        read_json_body(env(oversized, declared=10), allowed=["text"])


def test_a_non_numeric_content_length_is_refused():
    with pytest.raises(BodyError):
        read_json_body({"wsgi.input": io.BytesIO(b"{}"),
                        "CONTENT_LENGTH": "banana"}, allowed=["text"])


# ===========================================================================
# the typed readers
# ===========================================================================

def test_opt_str_trims_and_defaults():
    assert opt_str({"text": "  x  "}, "text") == "x"
    assert opt_str({}, "text") == ""
    assert opt_str({"text": None}, "text", "fallback") == "fallback"
    with pytest.raises(BodyError):
        opt_str({"text": 5}, "text")


def test_opt_int_rejects_bool_because_bool_is_an_int():
    """`True` must not silently become one minute."""
    assert opt_int({"minutes": 12}, "minutes") == 12
    assert opt_int({}, "minutes") is None
    with pytest.raises(BodyError):
        opt_int({"minutes": True}, "minutes")
    with pytest.raises(BodyError):
        opt_int({"minutes": "12"}, "minutes")


def test_opt_bool_requires_a_real_boolean():
    assert opt_bool({"real_time_affirmed": True}, "real_time_affirmed") is True
    assert opt_bool({}, "real_time_affirmed") is None
    for bad in ("true", 1, 0):
        with pytest.raises(BodyError):
            opt_bool({"real_time_affirmed": bad}, "real_time_affirmed")


def test_opt_str_list_requires_non_empty_strings():
    assert opt_str_list({"ids": [" a ", "b"]}, "ids") == ["a", "b"]
    assert opt_str_list({}, "ids") == []
    for bad in ("a", [1], [""], ["  "], {"a": 1}):
        with pytest.raises(BodyError):
            opt_str_list({"ids": bad}, "ids")


def test_enum_member_matches_on_value_not_name():
    """The wire format is the documented lowercase value."""
    from pilot_backend.domain.goals import EditType

    assert enum_member(EditType, {"edit_type": "modified"},
                       "edit_type") is EditType.MODIFIED
    # The member NAME must not be accepted — it is an internal identifier.
    with pytest.raises(BodyError):
        enum_member(EditType, {"edit_type": "MODIFIED"}, "edit_type")
    with pytest.raises(BodyError) as caught:
        enum_member(EditType, {"edit_type": "invented"}, "edit_type")
    assert "modified" in str(caught.value), "the refusal should list valid values"


def test_enum_member_requires_a_value_unless_defaulted():
    from pilot_backend.domain.goals import EditType

    with pytest.raises(BodyError):
        enum_member(EditType, {}, "edit_type")
    assert enum_member(EditType, {}, "edit_type",
                       default=EditType.MODIFIED) is EditType.MODIFIED


# ===========================================================================
# the module keeps no logging and no PHI surface
# ===========================================================================

def test_the_body_module_never_logs():
    """A body may legitimately carry clinical text in 0.5C."""
    import ast
    import pathlib

    module = (pathlib.Path(__file__).resolve().parent.parent
              / "transport" / "body.py")
    tree = ast.parse(module.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert "logging" not in imported
    printed = {n.func.id for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "print" not in printed


# ===========================================================================
# distinguishing the two identity guards
#
# `read_json_body` has two identity checks and a mutation sweep showed them
# covering each other. They are NOT equivalent, and the difference is which
# mistake each catches:
#
#   `forbidden` = the ROUTE is wrong — it allowlisted an identity field. A
#                 programming error, raised before any payload is looked at.
#   `identity`  = the PAYLOAD carried one. Reachable only if the field was
#                 allowlisted, which `forbidden` already refuses.
#
# So `forbidden` is load-bearing and `identity` is unreachable defence in
# depth. The tests below pin that by asserting WHICH guard fired, rather than
# merely that something did.
# ===========================================================================

def test_the_route_allowlist_guard_names_itself():
    """An allowlisted identity field fails as a ROUTE error, before parsing."""
    with pytest.raises(BodyError) as caught:
        read_json_body(env({}), allowed=["provider_id"])
    assert "route allowlist" in str(caught.value), (
        "the refusal must identify this as a route-definition error, not a "
        "caller error")
    # And it fires with NO payload at all, which proves it precedes parsing.
    assert "identity may not be supplied" not in str(caught.value)


def test_child_id_is_an_identity_field_for_the_allowlist_guard():
    """`child_id` must be refusable as a route allowlist entry.

    A child is always named by a PATH segment. If `child_id` were not an
    identity field, a future route could allowlist it and the body would
    become a second, unauthorized way to name the subject of an operation.
    """
    assert "child_id" in IDENTITY_FIELDS
    with pytest.raises(BodyError) as caught:
        read_json_body(env({}), allowed=["child_id"])
    assert "route allowlist" in str(caught.value)


def test_a_body_at_the_cap_boundary_is_refused_on_the_READ_side():
    """The read-side cap, reached by declaring exactly the maximum.

    An earlier test declared a SMALL length and sent a large body, which the
    read truncates to `length + 1` — so it never reached this branch and the
    mutation that removed it survived. Declaring exactly `MAX_BODY_BYTES`
    while sending one byte more is the case that does.
    """
    oversized = b"{" + b"x" * MAX_BODY_BYTES
    assert len(oversized) == MAX_BODY_BYTES + 1
    with pytest.raises(BodyError) as caught:
        read_json_body(env(oversized, declared=MAX_BODY_BYTES),
                       allowed=["text"])
    assert "too large" in str(caught.value)
