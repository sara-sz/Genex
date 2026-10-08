"""pilot_backend/transport/wsgi_app.py — the composition proof, over real HTTP.

## What this is, and what it is not

This is COMPOSITION ASSURANCE. It exists to prove that a real HTTP request
cannot reach a repository without passing through token extraction,
verification, revocation semantics, identity resolution, server-derived role
and relationship authorization — in that order, with no bypass.

It is NOT the product API. It has two routes. It returns no clinical content.
Adding a product endpoint here would be adding product scope, so nothing is
added here that a workflow would need.

## Why WSGI from the standard library, and not FastAPI

The repo's convention is FastAPI — Parent `api/` and `therapist_api` both use
it, and the app-consumer CI job pins `fastapi==0.135.3`. It was still the wrong
choice here, for one decisive reason: the CI job that runs every pilot security
test installs `pytest`, `pandas` and `openpyxl` and nothing else. A FastAPI
proof would have to live in the other job, separated from the suite it is
meant to complete, and the composition gate could then pass while the security
gate was skipped — exactly the "skipped downstream gate" failure this project
has already been bitten by once.

`wsgiref` is in the standard library. That buys:

  * a genuine HTTP boundary — real sockets, real request lines, real headers,
    real status codes — which the tests exercise through `urllib`, not through
    an in-process test client that could mask a transport-layer mistake;
  * zero new dependency, so the proof runs beside the rest of the security
    suite in the dependency-pure job;
  * no throwaway work: WSGI is a standard interface, so this application
    mounts under gunicorn directly, or under Starlette/FastAPI via
    `WSGIMiddleware`, when a production transport is chosen.

"Smallest reasonable option" was taken literally.

## Deny by default at the routing layer too

`ROUTE_TABLE` is an explicit list. An unregistered path is 404 and is never
public — there is no prefix rule, no catch-all, and no fallthrough that could
let a future route be served before anyone remembers to protect it. The
protected route's handler is never even constructed unless authorization
returned an allow.

## Responses carry no internal detail

Every failure renders a constant body for its status class. Exceptions are
caught at the boundary and rendered as a bare 500 with no type, message or
traceback — an unexpected error is exactly when a vendor exception is most
likely to be holding a document fragment.
"""

from __future__ import annotations

import json
import uuid
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from ..apisurface.surface import health_payload, is_public_route
from ..audit.events import AuditAction, AuditResult
from ..authz.decisions import HTTP_FORBIDDEN, HTTP_OK, HTTP_UNAUTHORIZED
from ..authz.policy import authenticate_and_authorize_child
from ..domain.roles import ActorRole
from ..integration.errors import IntegrationError, SecondSessionUnresolved
from ..observability.safe_logging import format_log, render

#: The paths this application serves. Every one is listed.
PUBLIC_HEALTH_ROUTE = "/health"
PROTECTED_CHILD_ROUTE = "/pilot/children/{child_id}/access-check"

#: 0.5A identity surface. Protected, like everything that is not `/health`.
ME_ROUTE = "/pilot/me"
MY_CHILDREN_ROUTE = "/pilot/me/children"
BOOTSTRAP_CAREGIVER_ROUTE = "/pilot/bootstrap/caregiver"
LINK_CHILD_ROUTE = "/pilot/parent-sessions/{session_id}/link-child"

#: 0.5F-A3. Redeem a Parent handoff capability as the authenticated caregiver.
#:
#: The token is the ONE identifier in this application that travels in a BODY
#: rather than a path segment, and that is deliberate: Cloud Run records the
#: request path in its own logs, so a capability in the URL would be written to
#: a log this application cannot redact. A path segment is right for a child id
#: and wrong for a credential.
#:
#: There is no `{session_id}` in the template either. The session comes from the
#: stored claim, so a browser cannot name a session it holds no capability for.
CONSUME_CLAIM_ROUTE = "/pilot/parent-session-claims"

#: The body allowlist for that route. One field. `read_json_body` refuses every
#: `IDENTITY_FIELDS` name — including `child_id` — so this body cannot carry
#: identity or select a canonical child.
CONSUME_CLAIM_FIELDS = ("claim_token",)

#: 0.5B provider connection surface. Protected, like everything but `/health`.
#:
#: Every identifier a caller supplies travels in the PATH, never in a body. No
#: handler in this application reads `wsgi.input`, and these keep it that way:
#: a body is the one place a forged `role`, `caregiver_id` or `auth_subject`
#: could arrive, so the application simply has no code that looks.
#:
#: There is deliberately NO provisioning route. Creating a Provider is an
#: administrative act performed on someone else's behalf, so an HTTP endpoint
#: for it would need an admin principal that `ActorRole` does not have — and an
#: unauthenticated or self-authorizing version of it IS the public
#: provider self-registration surface 0.5B is required not to build. Hannah is
#: provisioned operationally through `ProviderProvisioningService`; see
#: SECURITY.md.
INVITE_PROVIDER_ROUTE = "/pilot/children/{child_id}/provider-connections/{provider_id}"
CHILD_CONNECTIONS_ROUTE = "/pilot/children/{child_id}/provider-connections"
CONNECTION_ACTION_ROUTE = "/pilot/provider-connections/{connection_id}/{action}"
MANAGING_CLINICIAN_ROUTE = "/pilot/children/{child_id}/managing-clinician"
ASSIGN_MANAGING_ROUTE = "/pilot/children/{child_id}/managing-clinician/{provider_id}"
END_MANAGING_ROUTE = "/pilot/children/{child_id}/managing-clinician/end"

#: The closed set of lifecycle transitions reachable over HTTP, and who may
#: ask for each. The pairing is data rather than a chain of `if`s so that a new
#: action cannot be added without stating its role — which is the mistake that
#: would let a provider revoke a family's connection or a family accept on a
#: clinician's behalf.
CONNECTION_ACTIONS = {
    "accept": ActorRole.PROVIDER,
    "decline": ActorRole.PROVIDER,
    "pause": ActorRole.CAREGIVER,
    "resume": ActorRole.CAREGIVER,
    "revoke": ActorRole.CAREGIVER,
    "end": ActorRole.CAREGIVER,
}

#: 0.5C Tuesday-minimum workflow surface. Protected, every one.
#:
#: Scoped deliberately to the routes the Tuesday browser workflow calls. The
#: frozen services expose 91 public methods; exposing all of them would be a
#: larger attack surface and a larger review burden for no pilot benefit, so
#: this is 17 routes and no general CRUD.
#:
#: Every identifier is a PATH segment. Bodies carry CONTENT only — a modified
#: goal target, a clinical interpretation, minutes — and go through
#: `read_json_body`, which refuses identity fields outright.
CHILD_GOALS_ROUTE = "/pilot/children/{child_id}/goals"
CHILD_SUGGESTIONS_ROUTE = "/pilot/children/{child_id}/goal-suggestions"

#: 0.5F-B. The provider TRIGGER for deterministic anchored generation.
#:
#: POST, never GET: it writes a GoalSuggestion, an anchor and a generation
#: claim. A GET that mutated would be cacheable, prefetchable and retried by
#: intermediaries that assume it is safe.
#:
#: A SUBROUTE of the existing read rather than a new top-level resource, so the
#: thing Hannah reads and the thing that fills it stay visibly the same
#: resource. The read endpoint is unchanged.
GENERATE_SUGGESTIONS_ROUTE = (
    "/pilot/children/{child_id}/goal-suggestions/generate")
#: 0.6A-1G. The EVIDENCE-DRIVEN trigger, as its own route rather than a mode on
#: the one above.
#:
#: The two are different clinical contracts, not two settings of one. v1 answers
#: "what is one rung up from where this child is", from a month-level summary, and
#: produces at most one suggestion. v2 answers "which skills was this child
#: actually shown not to have", from per-skill evidence, and produces zero, one or
#: several — plus a list of canonical deficits it cannot yet support.
#:
#: A `?policy=v2` flag on one route would make the first thing the handler does a
#: branch on client-supplied data deciding which clinical algorithm runs, and a
#: client that omitted the flag would silently get the alphabetical-winner
#: behaviour this slice exists to retire.
GENERATE_SUGGESTIONS_V2_ROUTE = (
    "/pilot/children/{child_id}/goal-suggestions/generate-v2")
#: 0.6A-2. ONE managing-provider action that composes every existing transition
#: from approved-goal validation through to release. The low-level transitions
#: are deliberately NOT exposed individually: a browser able to allocate but not
#: snapshot could leave a cycle half-planned and visible to nobody.
WEEK_ONE_RELEASE_ROUTE = (
    "/pilot/children/{child_id}/weekly-cycles/current/release")
#: The body allowlist for that route. ONE field, and it is REQUIRED.
#:
#: `family_declared_capacity` is what the family reports it can manage this
#: week, recorded by the provider for planning — never a recommended or
#: prescribed frequency and never a clinical dosage. There is no default: the
#: number of activities is a direct function of it, so defaulting would author a
#: clinical frequency nobody approved. `read_json_body` refuses every
#: `IDENTITY_FIELDS` name, so this body cannot carry identity either.
WEEK_ONE_RELEASE_FIELDS = ("family_declared_capacity",)

#: 0.6A-2. The family's read. Returns ONLY a released week — never a draft.
THIS_WEEK_ROUTE = "/pilot/children/{child_id}/this-week"
CHILD_MONTHLY_PLAN_ROUTE = "/pilot/children/{child_id}/monthly-plan"
CHILD_CURRENT_CYCLE_ROUTE = "/pilot/children/{child_id}/current-cycle"
CHILD_RTM_ROUTE = "/pilot/children/{child_id}/rtm"
GOAL_REVISIONS_ROUTE = "/pilot/goals/{goal_kind}/{goal_id}/revisions"
PLAN_ALLOCATIONS_ROUTE = "/pilot/monthly-plans/{focus_plan_id}/allocations"
#: Activation is a SEPARATE call because the domain refuses to activate a
#: plan with no allocated goal — "a plan cannot be activated with no
#: allocated goal". So the real sequence is create (DRAFT) -> allocate ->
#: activate, and a handler that tried to create-and-activate in one call
#: simply failed. Respecting the invariant costs one route; routing around
#: it would have meant activating an empty month.
PLAN_ACTIVATE_ROUTE = "/pilot/monthly-plans/{focus_plan_id}/activate"
CYCLE_OBSERVATIONS_ROUTE = "/pilot/cycles/{cycle_id}/observations"
CYCLE_DEFERS_ROUTE = "/pilot/cycles/{cycle_id}/defers"
PERIOD_REVIEWS_ROUTE = "/pilot/rtm-periods/{period_id}/reviews"
PERIOD_TIME_ROUTE = "/pilot/rtm-periods/{period_id}/time-entries"
PERIOD_INTERACTIONS_ROUTE = "/pilot/rtm-periods/{period_id}/interactions"
PERIOD_REPORT_ROUTE = "/pilot/rtm-periods/{period_id}/report"
REVIEW_ACTIONS_ROUTE = "/pilot/rtm-reviews/{review_id}/actions"

#: (method, template, is_public). Explicit; no prefix matching anywhere.
ROUTE_TABLE: Tuple[Tuple[str, str, bool], ...] = (
    ("GET", PUBLIC_HEALTH_ROUTE, True),
    ("GET", PROTECTED_CHILD_ROUTE, False),
    ("GET", ME_ROUTE, False),
    ("GET", MY_CHILDREN_ROUTE, False),
    ("POST", BOOTSTRAP_CAREGIVER_ROUTE, False),
    ("POST", LINK_CHILD_ROUTE, False),
    ("POST", INVITE_PROVIDER_ROUTE, False),
    ("GET", CHILD_CONNECTIONS_ROUTE, False),
    ("POST", CONNECTION_ACTION_ROUTE, False),
    ("GET", MANAGING_CLINICIAN_ROUTE, False),
    ("POST", ASSIGN_MANAGING_ROUTE, False),
    ("POST", END_MANAGING_ROUTE, False),
    # 0.5C workflow surface.
    ("GET", CHILD_GOALS_ROUTE, False),
    ("POST", CHILD_GOALS_ROUTE, False),
    ("GET", CHILD_SUGGESTIONS_ROUTE, False),
    ("POST", GENERATE_SUGGESTIONS_ROUTE, False),
    ("POST", GENERATE_SUGGESTIONS_V2_ROUTE, False),
    ("POST", WEEK_ONE_RELEASE_ROUTE, False),
    ("GET", THIS_WEEK_ROUTE, False),
    ("GET", CHILD_MONTHLY_PLAN_ROUTE, False),
    ("POST", CHILD_MONTHLY_PLAN_ROUTE, False),
    ("GET", CHILD_CURRENT_CYCLE_ROUTE, False),
    ("GET", CHILD_RTM_ROUTE, False),
    ("POST", GOAL_REVISIONS_ROUTE, False),
    ("POST", PLAN_ALLOCATIONS_ROUTE, False),
    ("POST", PLAN_ACTIVATE_ROUTE, False),
    ("GET", CYCLE_OBSERVATIONS_ROUTE, False),
    ("POST", CYCLE_OBSERVATIONS_ROUTE, False),
    ("POST", CYCLE_DEFERS_ROUTE, False),
    ("POST", PERIOD_REVIEWS_ROUTE, False),
    ("POST", PERIOD_TIME_ROUTE, False),
    ("POST", PERIOD_INTERACTIONS_ROUTE, False),
    ("POST", PERIOD_REPORT_ROUTE, False),
    ("POST", REVIEW_ACTIONS_ROUTE, False),
)

_STATUS_TEXT = {
    HTTP_OK: "200 OK",
    HTTP_UNAUTHORIZED: "401 Unauthorized",
    HTTP_FORBIDDEN: "403 Forbidden",
    404: "404 Not Found",
    405: "405 Method Not Allowed",
    409: "409 Conflict",
    500: "500 Internal Server Error",
}

#: Constant bodies. A response body never varies with the reason for refusal,
#: so it cannot become an oracle for which child ids exist.
_ERROR_BODIES = {
    HTTP_UNAUTHORIZED: {"error": "authentication required"},
    HTTP_FORBIDDEN: {"error": "not permitted"},
    404: {"error": "not found"},
    405: {"error": "method not allowed"},
    500: {"error": "internal error"},
}


def _match_child_route(path: str) -> Optional[str]:
    """Return the child id if `path` is the protected route, else None.

    Hand-matched rather than regex-routed so the shape is obvious: exactly four
    segments, the fixed ones exact, and the id segment non-empty. A trailing
    segment, an extra segment or a different prefix does not match and
    therefore 404s rather than falling through to a handler.
    """
    parts = [p for p in path.split("/") if p != ""]
    if len(parts) != 4:
        return None
    if parts[0] != "pilot" or parts[1] != "children" or parts[3] != "access-check":
        return None
    return parts[2]


def _match_generate_suggestions_route(path: str) -> Optional[str]:
    """Return the child id for `/pilot/children/{id}/goal-suggestions/generate`.

    Five exact segments, hand-matched like every other route here. Deliberately
    NOT folded into the `_match_resource_subroute` table: that table pairs a
    collection with ONE tail segment, and bending it to accept a two-segment
    tail would make every route in it harder to read for the sake of one.
    """
    parts = [p for p in path.split("/") if p != ""]
    if len(parts) != 5:
        return None
    if (parts[0] != "pilot" or parts[1] != "children"
            or parts[3] != "goal-suggestions" or parts[4] != "generate"):
        return None
    return parts[2]


def _match_generate_suggestions_v2_route(path: str) -> Optional[str]:
    """Return the child id for `.../goal-suggestions/generate-v2`. 0.6A-1G.

    A SEPARATE matcher with its own exact final segment, not a prefix test. The
    two tails differ by a suffix, so `startswith("generate")` would make the v1
    matcher swallow the v2 path and silently run the v1 algorithm — the one
    failure mode a versioned route exists to prevent.
    """
    parts = [p for p in path.split("/") if p != ""]
    if len(parts) != 5:
        return None
    if (parts[0] != "pilot" or parts[1] != "children"
            or parts[3] != "goal-suggestions" or parts[4] != "generate-v2"):
        return None
    return parts[2]


def _match_week_one_release_route(path: str) -> Optional[str]:
    """Return the child id for `.../weekly-cycles/current/release`. 0.6A-2.

    Six exact segments, hand-matched like every other route here. `current` is a
    literal rather than a cycle id: the action is "release THIS child's current
    Week 1", and accepting an id would let a client name a cycle belonging to
    another child and rely on a later check to catch it.
    """
    parts = [p for p in path.split("/") if p != ""]
    if len(parts) != 6:
        return None
    if (parts[0] != "pilot" or parts[1] != "children"
            or parts[3] != "weekly-cycles" or parts[4] != "current"
            or parts[5] != "release"):
        return None
    return parts[2]


def _match_parent_session_route(path: str) -> Optional[str]:
    """Return the session id for `/pilot/parent-sessions/{id}/link-child`.

    Same hand-matching discipline as `_match_child_route`: exactly four
    segments, the fixed ones exact, the id segment non-empty. The session id is
    the ONLY value a client supplies to that endpoint.
    """
    parts = [p for p in path.split("/") if p != ""]
    if len(parts) != 4:
        return None
    if (parts[0] != "pilot" or parts[1] != "parent-sessions"
            or parts[3] != "link-child"):
        return None
    return parts[2]


def _match_child_subroute(path: str, tail: str) -> Optional[str]:
    """Child id for `/pilot/children/{id}/{tail}`, else None.

    Same hand-matching discipline as every other matcher here: an exact
    segment count, the fixed segments compared exactly, and the id segment
    merely required to be non-empty. No regex and no prefix rule, so a path
    cannot fall through to a handler it was not written for.
    """
    parts = [p for p in path.split("/") if p != ""]
    if len(parts) != 4:
        return None
    if parts[0] != "pilot" or parts[1] != "children" or parts[3] != tail:
        return None
    return parts[2]


def _match_child_pair_route(path: str, tail: str) -> Optional[Tuple[str, str]]:
    """`(child_id, trailing_id)` for `/pilot/children/{id}/{tail}/{other}`."""
    parts = [p for p in path.split("/") if p != ""]
    if len(parts) != 5:
        return None
    if parts[0] != "pilot" or parts[1] != "children" or parts[3] != tail:
        return None
    if not parts[2] or not parts[4]:
        return None
    return parts[2], parts[4]


def _match_resource_subroute(path: str, collection: str, tail: str):
    """Resource id for `/pilot/{collection}/{id}/{tail}`, else None.

    The same hand-matching discipline as every other matcher here: an exact
    segment count, fixed segments compared exactly, and the id segment merely
    required to be non-empty. Generalised over `collection` so 0.5C adds
    fourteen routes without fourteen near-identical parsers, each of which
    would be a place to get the segment count wrong.
    """
    parts = [p for p in path.split("/") if p != ""]
    if len(parts) != 4:
        return None
    if parts[0] != "pilot" or parts[1] != collection or parts[3] != tail:
        return None
    return parts[2] or None


def _match_goal_revisions_route(path: str):
    """`(goal_kind, goal_id)` for `/pilot/goals/{kind}/{id}/revisions`.

    The goal KIND travels in the path because a `GoalRef` is (kind, id) and
    the kind decides which repository and which authorization rule apply. It
    is validated against the enum in the handler, not here.
    """
    parts = [p for p in path.split("/") if p != ""]
    if len(parts) != 5:
        return None
    if parts[0] != "pilot" or parts[1] != "goals" or parts[4] != "revisions":
        return None
    if not parts[2] or not parts[3]:
        return None
    return parts[2], parts[3]


def _match_connection_action_route(path: str) -> Optional[Tuple[str, str]]:
    """`(connection_id, action)` for `/pilot/provider-connections/{id}/{action}`.

    The action is NOT validated here. Routing decides which handler runs;
    whether the action exists, and which role may ask for it, is settled
    against `CONNECTION_ACTIONS` inside the handler where the principal is
    known. Validating here would mean an unknown action 404s while a known one
    the caller may not use 403s — a difference a prober could read.
    """
    parts = [p for p in path.split("/") if p != ""]
    if len(parts) != 4:
        return None
    if parts[0] != "pilot" or parts[1] != "provider-connections":
        return None
    if not parts[2] or not parts[3]:
        return None
    return parts[2], parts[3]


def _requested_month(environ) -> str:
    """The month to read, defaulting to the CURRENT one on the server clock.

    `active_plan` and the cycle read both require a `YYYY-MM`. The UI asks for
    "the current monthly plan" and should not have to compute which month that
    is — a browser clock disagreeing with the server about the month boundary
    would silently show an empty plan.

    A supplied `?cycle_month=` still wins, so a clinician can look at a
    specific month. This is a default for a READ FILTER, not a business rule:
    which month is current decides nothing about authorization, and every read
    it reaches is already scoped to a child the caller is proven to hold.
    """
    supplied = _query_value(environ, "cycle_month")
    if supplied:
        return supplied
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m")


def _query_value(environ, field: str) -> str:
    """One query-string value, or "".

    The ONLY query parameter 0.5C reads is `cycle_month`, which selects which
    month to look at. It is not identity and not authorization: every read it
    reaches is already scoped to a child the caller is proven to hold.

    Hand-parsed rather than via `urllib.parse.parse_qs`, because the no-network
    gate bans the whole `urllib` package — `urllib.request` opens sockets, and
    the gate is an import-level assertion that cannot tell `parse` from
    `request`. Widening the allowlist to admit one submodule would weaken a
    guard that exists to keep this package incapable of egress, for a function
    that is four lines of string splitting.

    Percent-decoding is deliberately NOT implemented: the only accepted value
    is a `YYYY-MM` month, which needs none, and a decoder here would be
    untested surface. A value containing `%` simply fails the service's own
    month validation.
    """
    raw = str(environ.get("QUERY_STRING") or "")
    for pair in raw.split("&"):
        if not pair or "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        if name == field:
            return value.replace("+", " ").strip()
    return ""


# -- serialisers -----------------------------------------------------------
#
# Flat, explicit dicts rather than a generic dataclass dumper. A dumper would
# ship whatever field someone adds to a domain object next, which is how a
# clinical field reaches a browser without anyone deciding it should. Each
# function below is a decision about what leaves the server.


def _goal_payload(goal, kind, *, text=None) -> dict:
    """The goal, plus its CURRENT wording when the caller resolved it.

    0.5D adds `text`. It is passed in rather than read here because resolving
    it requires an authorized service call, and a serialiser that reached for
    a repository would be deciding who may read what.

    `text` is a TEMPLATE, not display-ready prose. `current_text`'s own
    docstring is explicit that `pilot_backend` holds no child name to render
    with, so substitution is the client's job at presentation time. The field
    is omitted entirely when unresolved, so a caller can never mistake "not
    looked up" for "no wording".

    Deliberately NOT exposed from `GoalVersion`: `edit_type`, `reason`,
    `actor_id`, `actor_role`, `derived_from_suggestion_id` and
    `supersedes_version_id`. Those are provenance and history — `reason` in
    particular can carry clinical rationale, and none of it is needed to
    render a goal. `current_version_id` is already here, so a client that
    needs to correlate versions can.
    """
    payload = {
        "goal_kind": kind.value,
        "goal_id": getattr(goal, "clinical_goal_id", None)
                   or getattr(goal, "caregiver_goal_id", ""),
        "current_version_id": goal.current_version_id,
        "status": goal.status.value,
        "is_rtm_eligible": kind.value == "clinical",
    }
    if text is not None:
        payload["text"] = text
    return payload


def _suggestion_payload(suggestion) -> dict:
    """Suggestion plus its PROVENANCE.

    `generator_version`, the rule version and the domain/milestone references
    are what let a clinician see WHY a target was proposed. No LLM chooses a
    target: these are deterministic engine outputs, and the provenance is the
    evidence for that claim.

    ## 0.5D correction

    Every field here was wrong, and the route returned 500 for any non-empty
    list. `suggestion.text` does not exist — the attribute is
    `family_facing_text_template` — so this raised `AttributeError` on the
    first real suggestion, which the transport correctly turned into a
    constant 500. And `policy_version`, `domain_key`, `milestone_ref` and
    `functional_baseline_ref` were read with `getattr(..., "")` against a
    `GoalSuggestion` that has none of them, so they would have served ""
    even once the crash was fixed: the provenance this docstring promised was
    never actually supplied.

    The provenance is real — it lives one level down, on
    `GoalSuggestionEvidence` — and is read from there now.

    This survived 0.5C because no test ever had a suggestion PRESENT. Same
    root cause as the `_report_payload` defect that slice caught, and the same
    lesson: a route whose happy path is never exercised is not a route anyone
    should wire a UI to.

    `family_facing_text_template` keeps its real name rather than being
    flattened to `text`, because it is a TEMPLATE: `pilot_backend` holds no
    child name to render with, so a client must not mistake it for
    display-ready prose.

    `policy_version` is dropped — it is not a field of `GoalSuggestion` at
    all, and the planning policy version lives on the monthly plan the UI
    already reads. `prior_month_summary_id` stays unexposed: it points at a
    prior clinical summary and no screen needs it.
    """
    evidence = suggestion.evidence
    return {
        "suggestion_id": suggestion.suggestion_id,
        "family_facing_text_template": suggestion.family_facing_text_template,
        "status": suggestion.status.value,
        "cycle_month": suggestion.cycle_month,
        "suggested_priority_rank": suggestion.suggested_priority_rank,
        "suggested_emphasis_weight": suggestion.suggested_emphasis_weight,
        "generator_version": suggestion.generator_version,
        "generation_mode": suggestion.generation_mode,
        "domain_key": evidence.domain_key,
        "evidence_source": evidence.evidence_source.value,
        "milestone_refs": list(evidence.milestone_refs),
        "functional_baseline_area": evidence.functional_baseline_area,
        "observed_level": evidence.observed_level,
        "explicitly_selected": evidence.explicitly_selected,
        "rule_version": evidence.rule_version,
    }


def _plan_payload(plan) -> dict:
    return {
        "focus_plan_id": plan.focus_plan_id,
        "cycle_month": plan.cycle_month,
        "state": plan.state.value,
        "timezone_of_record": plan.timezone_of_record,
        "policy_version": getattr(plan, "policy_version", ""),
    }


def _allocation_payload(allocation) -> dict:
    """Relative EMPHASIS, never a percentage.

    `emphasis_weight` is a relative weight and `priority_rank` an ordering.
    Neither is a share of anything, and the field names are kept as the domain
    names so a UI cannot mistake one for a percentage.
    """
    return {
        "allocation_id": allocation.allocation_id,
        "goal_kind": allocation.goal_kind.value,
        "goal_id": allocation.goal_id,
        "priority_rank": allocation.priority_rank,
        "emphasis_weight": allocation.emphasis_weight,
        "min_coverage_per_cycle": allocation.min_coverage_per_cycle,
        "status": allocation.status.value,
    }


def _cycle_payload(cycle) -> dict:
    return {
        "cycle_id": cycle.cycle_id,
        "sequence_in_month": cycle.sequence_in_month,
        "starts_on": cycle.starts_on,
        "ends_on": cycle.ends_on,
        "is_partial": cycle.is_partial,
        "state": cycle.state.value if hasattr(cycle, "state") else None,
    }


def _snapshot_payload(snapshot) -> dict:
    """The captured parent-facing plan, VERBATIM and explicitly non-canonical.

    0.5D. This is the one read model in the pilot that deliberately has no
    schema, and the naming says so at every level so a client cannot acquire
    the shape by accident and then depend on it.

    ## Why it is not normalised

    `WeeklyPlanSnapshot.resolved_plan_document` is a JSON string because it is
    an opaque capture of ANOTHER system's document. The frozen docstring states
    the reason: "Parent's plan shape is not ours to version, and a codec that
    validated its fields would start failing the moment Parent changed one."
    Giving it a pilot-owned schema here would make exactly the claim that
    sentence refuses, and would create a second source of truth for plan
    content that Parent would then have to conform to.

    So the document is handed back parsed and untouched, under
    `source_document`, wrapped in the provenance that makes it interpretable:
    WHICH system produced it, WHICH plan it came from, WHEN that system
    generated it and WHEN the pilot froze it. A client that renders this is
    rendering Parent's document, at its own risk, and `is_canonical: false`
    plus `schema: "opaque_source_document"` are in the payload to keep that
    unambiguous in the response itself — not merely in documentation nobody
    re-reads.

    ## What this is NOT

    Not a promise of forward compatibility. Not a contract Parent currently
    keeps. Not a field set the pilot will maintain, validate, migrate or
    version. No key inside `source_document` is guaranteed to exist, keep its
    type, or mean the same thing next week.

    Nothing clinical is added by this projection: the snapshot is
    `PARENT_VISIBLE` precisely because, as its docstring puts it, "the family
    already has this content; the snapshot is a copy of it."
    """
    return {
        "snapshot_id": snapshot.snapshot_id,
        "cycle_id": snapshot.cycle_id,
        "schema": "opaque_source_document",
        "is_canonical": False,
        "source_system": snapshot.source_system.value,
        "source_plan_id": snapshot.source_plan_id,
        "source_generated_at": (snapshot.source_generated_at.isoformat()
                                if snapshot.source_generated_at else None),
        "captured_at": snapshot.captured_at.isoformat(),
        # Parsed, not re-serialised: `document()` is the frozen accessor and
        # it never mutates the record.
        "source_document": snapshot.document(),
    }


def _alignment_payload(alignment) -> dict:
    """SCHEDULED coverage. Not attempted, not completed, not improvement."""
    return {
        "alignment_id": alignment.alignment_id,
        "activity_instance_ref": alignment.activity_instance_ref,
        "goal_kind": alignment.goal_kind.value,
        "goal_id": alignment.goal_id,
        "scheduled_local_date": getattr(alignment, "scheduled_local_date", ""),
    }


def _gap_payload(gap) -> dict:
    return {
        "gap_id": gap.gap_id,
        "goal_kind": gap.goal_kind.value,
        "goal_id": gap.goal_id,
        "reason": getattr(gap, "reason", ""),
    }


def _observation_payload(event) -> dict:
    """Structured Parent evidence. NO free text of any kind.

    `assistance`, `child_response` and `observation_text_ref` exist on the
    domain object and are deliberately NOT serialised: the pilot carries
    structured evidence only, and none of those three is structured.
    """
    return {
        "event_id": event.event_id,
        "activity_instance_ref": event.activity_instance_ref,
        "local_date": event.local_date,
        "attribution_month": event.attribution_month,
        "timezone_of_record": event.timezone_of_record,
        "attempt_outcome": event.attempt_outcome.value,
        "difficulty": event.difficulty.value if event.difficulty else None,
        "enjoyment": event.enjoyment.value if event.enjoyment else None,
    }


def _episode_payload(episode) -> dict:
    return {
        "episode_id": episode.episode_id,
        "status": episode.status.value,
        "managing_provider_id": episode.managing_provider_id,
        "opened_at": episode.opened_at.isoformat() if episode.opened_at else None,
    }


def _report_payload(report) -> dict:
    """A DRAFT month-end report — a preview, not a submission.

    Field names are the DOMAIN's, verified against `MonthEndReport` rather
    than assumed: it carries `version`, not `report_version`, and has no
    `documented_minutes` at all. An earlier revision of this function guessed
    both and would have raised `AttributeError` on the first real call — the
    mutation sweep caught it by flagging that nothing asserted this shape.

    Any coding assistance reachable from a report is POTENTIAL only.
    98979/98980/98981 output requires explicit clinician confirmation, carries
    no reimbursement guarantee, and Genex makes no medical-necessity
    determination. There is no payer, member, insurance or claim field here,
    and a test asserts that over the whole payload.
    """
    return {
        "report_id": report.report_id,
        "period_id": report.period_id,
        "cycle_month": report.cycle_month,
        "state": report.state.value,
        "version": report.version,
        "section_count": len(report.sections),
        # A preview until a clinician finalizes it, which no 0.5C route does.
        "is_preview": report.state.value != "finalized",
    }


def _present(**kwargs) -> dict:
    """Drop keys whose value is None.

    Passing `None` for an omitted optional is NOT the same as omitting it: the
    frozen services declare their own defaults — `suppress_for_cycles`,
    `emphasis_weight`, `min_coverage_per_cycle` — and handing them None
    replaces a working default with a value they never expected. That produced
    two 500s during 0.5C, both of which read as authorization failures at
    first glance.
    """
    return {name: value for name, value in kwargs.items() if value is not None}


def _coding_payload(coding) -> dict:
    """POTENTIAL coding assistance. Field names verified against the domain.

    `potential_code_candidates` is the domain's own name and is carried
    through unchanged, because "potential" is the whole claim: 98979/98980/
    98981 output is a CANDIDATE requiring explicit clinician confirmation. It
    carries no reimbursement guarantee, and Genex makes no medical-necessity
    determination.

    `missing_requirement_flags` and `rule_explanations` are included
    deliberately — a clinician deciding whether to confirm a code needs to see
    what the rule set thought was missing, not just the number it produced.

    `documented_management_minutes` is a sum of MANUALLY ENTERED minutes.
    Nothing in this system infers time.
    """
    return {
        "coding_summary_id": coding.coding_summary_id,
        "coding_rule_set_id": coding.coding_rule_set_id,
        "coding_rule_version": coding.coding_rule_version,
        "documented_management_minutes": coding.documented_management_minutes,
        "real_time_interactive_communication_present":
            coding.real_time_interactive_communication_present,
        "potential_code_candidates": [
            {"code": code, "count": count}
            for code, count in coding.potential_code_candidates],
        "missing_requirement_flags": [f.value for f
                                      in coding.missing_requirement_flags],
        "rule_explanations": list(coding.rule_explanations),
        # Never a determination. Always a candidate awaiting confirmation.
        "is_candidate_only": True,
        "requires_clinician_confirmation": True,
    }


def _period_payload(period) -> dict:
    """The monitoring period the Tuesday UI writes against."""
    return {
        "period_id": period.period_id,
        "episode_id": period.episode_id,
        "focus_plan_id": period.focus_plan_id,
        "cycle_month": period.cycle_month,
        "timezone_of_record": period.timezone_of_record,
        "status": period.status.value,
    }


class PilotWSGIApplication:
    """A two-route WSGI application wired to the BACKEND 0.2 components.

    Holds the verifier, repositories, settings and recorder. A handler cannot
    assemble a partial security chain because it never receives the pieces —
    it receives only the finished `AccessDecision`.
    """

    def __init__(self, *, settings, verifier, repos, recorder=None,
                 parent_source=None,
                 rung_source=None,
                 activity_bank=None,
                 log_sink: Optional[List[str]] = None) -> None:
        self._settings = settings
        self._verifier = verifier
        self._repos = repos
        self._recorder = recorder
        #: The READ-ONLY Parent boundary. Absent by default: without one, the
        #: link route refuses rather than inventing a session, and the rest of
        #: the application is unaffected.
        self._parent_source = parent_source
        #: 0.5F-B. The Gold Standard canonical-rung source, or None.
        #:
        #: Optional and defaulted so every pre-0.5F-B caller is unaffected, and
        #: None FAILS the generation route closed: without the real workbook no
        #: canonical target can be resolved, and an unanchored suggestion would
        #: become a goal `allocate_goal` refuses. Same posture
        #: `build_parent_source` takes — unconfigured means the capability is
        #: simply absent, which is a safe state.
        self._rung_source = rung_source
        #: 0.6A-2. The reviewed static activity bank, or None.
        #:
        #: Optional and defaulted for the same reason `rung_source` is, and None
        #: FAILS the Week 1 release closed: a week with no reviewed activities
        #: is not a plan, and there is no fallback content to reach for.
        self._activity_bank = activity_bank
        #: Tests capture emitted lines here. Production would hand these to a
        #: logging handler; either way they pass through `format_log` first,
        #: so an unsafe field raises rather than being written.
        self.log_sink: List[str] = log_sink if log_sink is not None else []

    # -- WSGI entry point ---------------------------------------------------

    def __call__(self, environ: Mapping[str, object],
                 start_response: Callable) -> Iterable[bytes]:
        request_id = str(environ.get("HTTP_X_REQUEST_ID") or uuid.uuid4().hex)
        try:
            status_code, payload, route_template = self._route(environ, request_id)
        except Exception:
            # Boundary catch-all. Nothing about the exception is rendered or
            # logged — not the type, not the message, not a traceback. An
            # unexpected failure is precisely when third-party exception text
            # is most likely to be carrying request or document content.
            status_code, payload, route_template = 500, _ERROR_BODIES[500], "unrouted"
            self._log(request_id, route_template, str(environ.get("REQUEST_METHOD", "")),
                      status_code, None)

        body = json.dumps(payload).encode("utf-8")
        start_response(_STATUS_TEXT[status_code], [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("X-Request-Id", request_id),
            # Defensive headers for a JSON API that must never be framed or
            # sniffed into something executable.
            ("X-Content-Type-Options", "nosniff"),
            ("Cache-Control", "no-store"),
        ])
        return [body]

    # -- routing ------------------------------------------------------------

    def _route(self, environ: Mapping[str, object],
               request_id: str) -> Tuple[int, Mapping, str]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", "") or "/")

        if path == PUBLIC_HEALTH_ROUTE:
            if method != "GET":
                return 405, _ERROR_BODIES[405], PUBLIC_HEALTH_ROUTE
            self._log(request_id, PUBLIC_HEALTH_ROUTE, method, HTTP_OK, None)
            return HTTP_OK, dict(health_payload(self._settings)), PUBLIC_HEALTH_ROUTE

        child_id = _match_child_route(path)
        if child_id is not None:
            if method != "GET":
                return 405, _ERROR_BODIES[405], PROTECTED_CHILD_ROUTE
            return self._handle_protected(environ, child_id, request_id)

        # --- 0.5A identity surface --------------------------------------
        #
        # `/pilot/me/children` is tested BEFORE `/pilot/me` and both are exact
        # comparisons, so neither can shadow the other and no prefix rule is
        # introduced. Each checks its own method and 405s otherwise.
        if path == MY_CHILDREN_ROUTE:
            if method != "GET":
                return 405, _ERROR_BODIES[405], MY_CHILDREN_ROUTE
            return self._handle_my_children(environ, request_id)

        if path == ME_ROUTE:
            if method != "GET":
                return 405, _ERROR_BODIES[405], ME_ROUTE
            return self._handle_me(environ, request_id)

        if path == BOOTSTRAP_CAREGIVER_ROUTE:
            if method != "POST":
                return 405, _ERROR_BODIES[405], BOOTSTRAP_CAREGIVER_ROUTE
            return self._handle_bootstrap(environ, request_id)

        session_id = _match_parent_session_route(path)
        if session_id is not None:
            if method != "POST":
                return 405, _ERROR_BODIES[405], LINK_CHILD_ROUTE
            return self._handle_link_child(environ, session_id, request_id)

        if path.rstrip("/") == CONSUME_CLAIM_ROUTE:
            if method != "POST":
                return 405, _ERROR_BODIES[405], CONSUME_CLAIM_ROUTE
            return self._handle_consume_claim(environ, request_id)

        # --- 0.5B provider connection surface ---------------------------
        #
        # Ordering matters and is deliberate: the FIVE-segment templates are
        # tested before their four-segment prefixes, so
        # `/managing-clinician/{provider_id}` cannot be swallowed by
        # `/managing-clinician`. Every matcher demands an exact segment count,
        # so this is belt-and-braces rather than the thing keeping them apart.
        pair = _match_child_pair_route(path, "provider-connections")
        if pair is not None:
            if method != "POST":
                return 405, _ERROR_BODIES[405], INVITE_PROVIDER_ROUTE
            return self._handle_invite_provider(environ, pair[0], pair[1],
                                                request_id)

        managing_pair = _match_child_pair_route(path, "managing-clinician")
        if managing_pair is not None:
            if method != "POST":
                return 405, _ERROR_BODIES[405], ASSIGN_MANAGING_ROUTE
            child, trailing = managing_pair
            # `end` is a reserved trailing segment, distinguishable from a
            # provider id because every provider id carries the `prov_`
            # prefix. Checked rather than assumed: a caller supplying the
            # literal "end" must reach the end handler, and one supplying
            # anything that is not a provider id must not reach assign.
            if trailing == "end":
                return self._handle_end_managing(environ, child, request_id)
            if not trailing.startswith("prov_"):
                self._log(request_id, ASSIGN_MANAGING_ROUTE, method, 404, None)
                return 404, _ERROR_BODIES[404], ASSIGN_MANAGING_ROUTE
            return self._handle_assign_managing(environ, child, trailing,
                                                request_id)

        connections_child = _match_child_subroute(path, "provider-connections")
        if connections_child is not None:
            if method != "GET":
                return 405, _ERROR_BODIES[405], CHILD_CONNECTIONS_ROUTE
            return self._handle_child_connections(environ, connections_child,
                                                  request_id)

        managing_child = _match_child_subroute(path, "managing-clinician")
        if managing_child is not None:
            if method != "GET":
                return 405, _ERROR_BODIES[405], MANAGING_CLINICIAN_ROUTE
            return self._handle_read_managing(environ, managing_child,
                                              request_id)

        action = _match_connection_action_route(path)
        if action is not None:
            if method != "POST":
                return 405, _ERROR_BODIES[405], CONNECTION_ACTION_ROUTE
            return self._handle_connection_action(environ, action[0], action[1],
                                                  request_id)

        # --- 0.5C workflow surface --------------------------------------
        #
        # Five-segment templates are matched before four-segment ones, and
        # every matcher demands an exact segment count, so no path can fall
        # through to a handler it was not written for.
        revision = _match_goal_revisions_route(path)
        if revision is not None:
            if method != "POST":
                return 405, _ERROR_BODIES[405], GOAL_REVISIONS_ROUTE
            return self._handle_goal_revision(environ, revision[0],
                                              revision[1], request_id)

        # 0.5F-B generation trigger. Tested BEFORE the four-segment table so
        # the five-segment form cannot be swallowed by a prefix match.
        generate_child = _match_generate_suggestions_route(path)
        if generate_child is not None:
            if method != "POST":
                return 405, _ERROR_BODIES[405], GENERATE_SUGGESTIONS_ROUTE
            return self._handle_generate_suggestions(environ, generate_child,
                                                     request_id)

        # 0.6A-2 Week 1 release. Tested before the four-segment table for the
        # same reason the generation routes are: a six-segment form must not be
        # swallowed by a shorter matcher.
        release_child = _match_week_one_release_route(path)
        if release_child is not None:
            if method != "POST":
                return 405, _ERROR_BODIES[405], WEEK_ONE_RELEASE_ROUTE
            return self._handle_week_one_release(environ, release_child,
                                                request_id)

        # 0.6A-1G evidence-driven generation. Its own matcher, so the v1 tail
        # cannot prefix-match the v2 path and quietly run the v1 algorithm.
        generate_v2_child = _match_generate_suggestions_v2_route(path)
        if generate_v2_child is not None:
            if method != "POST":
                return 405, _ERROR_BODIES[405], GENERATE_SUGGESTIONS_V2_ROUTE
            return self._handle_generate_suggestions_v2(
                environ, generate_v2_child, request_id)

        for collection, tail, route, verbs in (
                ("children", "goals", CHILD_GOALS_ROUTE, ("GET", "POST")),
                ("children", "goal-suggestions", CHILD_SUGGESTIONS_ROUTE, ("GET",)),
                ("children", "monthly-plan", CHILD_MONTHLY_PLAN_ROUTE, ("GET", "POST")),
                ("children", "current-cycle", CHILD_CURRENT_CYCLE_ROUTE, ("GET",)),
                ("children", "rtm", CHILD_RTM_ROUTE, ("GET",)),
                # 0.6A-2. A caregiver-readable read, so it joins the shared
                # workflow table rather than getting a bespoke matcher.
                ("children", "this-week", THIS_WEEK_ROUTE, ("GET",)),
                ("monthly-plans", "allocations", PLAN_ALLOCATIONS_ROUTE, ("POST",)),
                ("monthly-plans", "activate", PLAN_ACTIVATE_ROUTE, ("POST",)),
                ("cycles", "observations", CYCLE_OBSERVATIONS_ROUTE, ("GET", "POST")),
                ("cycles", "defers", CYCLE_DEFERS_ROUTE, ("POST",)),
                ("rtm-periods", "reviews", PERIOD_REVIEWS_ROUTE, ("POST",)),
                ("rtm-periods", "time-entries", PERIOD_TIME_ROUTE, ("POST",)),
                ("rtm-periods", "interactions", PERIOD_INTERACTIONS_ROUTE, ("POST",)),
                ("rtm-periods", "report", PERIOD_REPORT_ROUTE, ("POST",)),
                ("rtm-reviews", "actions", REVIEW_ACTIONS_ROUTE, ("POST",)),
        ):
            resource = _match_resource_subroute(path, collection, tail)
            if resource is None:
                continue
            if method not in verbs:
                return 405, _ERROR_BODIES[405], route
            return self._handle_workflow(environ, route, method, resource,
                                         request_id)

        # Unregistered. Not public, not served, and no hint that it is neither.
        self._log(request_id, "unrouted", method, 404, None)
        return 404, _ERROR_BODIES[404], "unrouted"

    # -- the protected chain ------------------------------------------------

    def _handle_protected(self, environ: Mapping[str, object], child_id: str,
                          request_id: str) -> Tuple[int, Mapping, str]:
        """Steps 2-11 of the required composition order.

        Note what is NOT read: the request body, the query string, and every
        header other than `Authorization` and `X-Request-Id`. A forged `uid`,
        `role`, `caregiver_id`, `provider_id` or `beta_access_code` is not
        rejected — it is never looked at, because nothing here has a reason to
        parse it.
        """
        # 2. Bearer token extracted from the verified transport header only.
        bearer = environ.get("HTTP_AUTHORIZATION")

        # 3-8. Verify, apply revocation semantics, resolve the application
        # identity, derive the role server-side, and check the child-scoped
        # relationship. One call, so a caller cannot perform half of it.
        decision = authenticate_and_authorize_child(
            bearer if isinstance(bearer, str) else None,
            child_id,
            verifier=self._verifier,
            repos=self._repos,
        )

        # 11. Audit before the response is shaped, for grants and refusals alike.
        if self._recorder is not None:
            self._recorder.record_access_decision(decision, request_id=request_id)

        self._log(request_id, PROTECTED_CHILD_ROUTE, "GET",
                  decision.status_code, decision)

        if not decision.allowed:
            return (decision.status_code,
                    _ERROR_BODIES[decision.status_code],
                    PROTECTED_CHILD_ROUTE)

        # 9. The repository operation runs ONLY on the allow path, and exactly
        # once. The single read of the child record happens inside
        # `authorize_child_access`, AFTER the relationship is proven — so a
        # refused request causes no lookup of the requested id at all, which a
        # counting repository asserts in the tests.
        #
        # An earlier revision re-read the child here to build the response.
        # That was a second read of a record authorization had already
        # fetched: in Firestore a duplicate billed document read on every
        # authorized request, and a second point at which the record could
        # have changed between the check and the use. The identifier the
        # response needs is already on the decision, so the read was pure
        # redundancy. It was found by asserting the expected read COUNT rather
        # than merely that a read had occurred.
        #
        # 10. Minimal non-PHI proof: identifiers and a boolean. `Child` carries
        # no clinical field, and nothing beyond its id is returned.
        return HTTP_OK, {
            "authorized": True,
            "child_id": decision.child_id,
            "actor_role": decision.principal.role.value,
            "request_id": request_id,
        }, PROTECTED_CHILD_ROUTE

    # -- the 0.5A identity chain --------------------------------------------
    #
    # These routes are NOT child-scoped, so `authenticate_and_authorize_child`
    # does not apply — there is no child id in the request to authorize
    # against. The chain they do share is credential -> verified subject ->
    # server-derived identity, and it is implemented once below so no handler
    # can perform half of it.

    def _verified_subject(self, environ: Mapping[str, object]
                          ) -> Tuple[Optional[str], int]:
        """Step 2-4: bearer -> verified subject, or a 401.

        Returns `(subject, 0)` or `(None, 401)`. A revoked or malformed token is
        401 here exactly as it is on the child route; the difference is that no
        application record is required yet, which is what makes bootstrap
        possible for a caller who has none.
        """
        from ..auth.interface import AuthError

        bearer = environ.get("HTTP_AUTHORIZATION")
        try:
            verified = self._verifier.verify(
                bearer if isinstance(bearer, str) else None)
        except AuthError:
            # `RevokedTokenError` is an `AuthError` subclass, so revocation is
            # covered by this one clause and cannot be missed by omission.
            return None, HTTP_UNAUTHORIZED
        subject = (verified.subject or "").strip()
        if not subject:
            return None, HTTP_UNAUTHORIZED
        return subject, 0

    def _principal(self, environ: Mapping[str, object]):
        """Step 2-8 minus the child check: a resolved `Principal`, or a status.

        Returns `(principal, 0)` or `(None, 401|403)`. The 401/403 split is the
        same one `authenticate_and_authorize_child` applies: an invalid
        credential is 401, a valid credential with no active application record
        is 403. A client cannot influence the resolved role — it comes from
        which repository matched the verified subject.
        """
        from ..auth.resolver import PrincipalResolutionError, resolve_principal
        from ..auth.interface import VerifiedToken

        subject, status = self._verified_subject(environ)
        if subject is None:
            return None, status
        try:
            return resolve_principal(VerifiedToken(subject=subject),
                                     self._repos), 0
        except PrincipalResolutionError:
            return None, HTTP_FORBIDDEN

    def _identity_service(self):
        """Build the service per request. Holds no cross-request state."""
        from ..integration.identity_service import IntegrationIdentityService

        return IntegrationIdentityService(
            repos=self._repos, parent_source=self._parent_source,
            recorder=self._recorder)

    def _handle_me(self, environ: Mapping[str, object],
                   request_id: str) -> Tuple[int, Mapping, str]:
        """Who the caller is. Creates NOTHING — notably, no caregiver.

        A caller with a valid token and no application record gets 403, not a
        silent bootstrap: creating an identity is an explicit POST, so an
        ordinary identity read can never have a write as a side effect.
        """
        principal, status = self._principal(environ)
        if principal is None:
            self._log(request_id, ME_ROUTE, "GET", status, None)
            return status, _ERROR_BODIES[status], ME_ROUTE
        payload = self._identity_service().whoami(principal).as_payload()
        self._log_principal(request_id, ME_ROUTE, "GET", HTTP_OK, principal)
        return HTTP_OK, dict(payload, request_id=request_id), ME_ROUTE

    def _handle_my_children(self, environ: Mapping[str, object],
                            request_id: str) -> Tuple[int, Mapping, str]:
        """The CALLER's own canonical children.

        There is no caregiver id in the path, the query or the body, so there is
        no shape of this request that reads somebody else's children. A provider
        is refused with the same constant 403 body as any other refusal.
        """
        principal, status = self._principal(environ)
        if principal is None:
            self._log(request_id, MY_CHILDREN_ROUTE, "GET", status, None)
            return status, _ERROR_BODIES[status], MY_CHILDREN_ROUTE

        # One route, two server-side paths, chosen by the SERVER-DERIVED role.
        #
        # 0.5A served caregivers only and refused providers. 0.5B adds the
        # clinician caseload here rather than at a second path, because
        # "my children" is the same question asked by two kinds of actor.
        #
        # What is NOT shared is the authorization logic: each role goes to a
        # different service method, and each of those answers exactly one
        # question — `my_children` requires a caregiver and filters to the
        # caller's active caregiver relationships, `connected_children`
        # requires a provider and filters to the caller's ACTIVE connections.
        # Neither takes an actor id, so neither can be aimed at someone else.
        # The role comes from `resolve_principal`, which derives it from which
        # repository matched the verified subject, so a client cannot select
        # which branch runs.
        try:
            if principal.role is ActorRole.PROVIDER:
                payload = {
                    "children": [row.as_payload() for row
                                 in self._connection_service()
                                 .connected_children(principal)],
                }
            else:
                payload = {
                    "child_ids": list(
                        self._identity_service().my_children(principal)),
                }
        except IntegrationError:
            self._log_principal(request_id, MY_CHILDREN_ROUTE, "GET",
                                HTTP_FORBIDDEN, principal)
            return HTTP_FORBIDDEN, _ERROR_BODIES[HTTP_FORBIDDEN], MY_CHILDREN_ROUTE
        self._log_principal(request_id, MY_CHILDREN_ROUTE, "GET", HTTP_OK,
                            principal)
        payload["request_id"] = request_id
        return HTTP_OK, payload, MY_CHILDREN_ROUTE

    # =====================================================================
    # 0.5B provider connections
    # =====================================================================

    def _connection_service(self):
        """Built per request. Holds no cross-request state."""
        from ..connections import ProviderConnectionService

        return ProviderConnectionService(
            repos=self._repos, recorder=self._recorder)

    def _refuse(self, route: str, method: str, request_id: str, principal,
                status: int = HTTP_FORBIDDEN) -> Tuple[int, Mapping, str]:
        """One constant-body refusal for every 0.5B failure.

        Every `IntegrationError` the connection service raises renders
        identically: `ProviderNotConnectable`, `ConnectionNotFound`,
        `ConnectionStateConflict` and `DuplicateLiveConnection` are
        indistinguishable over HTTP.

        That is the point. The service already collapses absent-versus-
        not-yours, but it still distinguishes "no such provider" from "illegal
        transition" for its own callers. Preserving that difference in the
        RESPONSE would hand a prober exactly the oracle the service was
        careful not to be: a caller walking `prov_` ids could tell a real
        clinician from a fictional one by which refusal came back.
        """
        self._log_principal(request_id, route, method, status, principal)
        return status, _ERROR_BODIES[status], route

    def _caregiver_or_provider(self, environ, route: str, method: str,
                               request_id: str):
        """Resolve the principal, or return the refusal tuple.

        Returns `(principal, None)` or `(None, response)`.
        """
        principal, status = self._principal(environ)
        if principal is None:
            self._log(request_id, route, method, status, None)
            return None, (status, _ERROR_BODIES[status], route)
        return principal, None

    def _handle_invite_provider(self, environ: Mapping[str, object],
                                child_id: str, provider_id: str,
                                request_id: str) -> Tuple[int, Mapping, str]:
        """A caregiver offers a connection to a provider named by opaque id.

        The provider id is the ONLY thing the caller supplies beyond the child
        id, and it grants nothing: this creates a PENDING row that confers no
        clinical access, which the provider must then accept. An id that does
        not exist, is retired, or sits in an inactive practice produces the
        same 403 as a child the caller does not hold — so neither segment can
        be used to probe for existence.
        """
        route = INVITE_PROVIDER_ROUTE
        principal, refusal = self._caregiver_or_provider(
            environ, route, "POST", request_id)
        if refusal is not None:
            return refusal
        try:
            connection = self._connection_service().invite_provider(
                principal, child_id, provider_id, request_id=request_id)
        except IntegrationError:
            return self._refuse(route, "POST", request_id, principal)

        self._log_principal(request_id, route, "POST", HTTP_OK, principal)
        return HTTP_OK, {
            "connection_id": connection.connection_id,
            "status": connection.status.value,
            "initiated_by": connection.initiated_by.value,
            "request_id": request_id,
        }, route

    def _handle_connection_action(self, environ: Mapping[str, object],
                                  connection_id: str, action: str,
                                  request_id: str) -> Tuple[int, Mapping, str]:
        """One handler for every lifecycle transition.

        The role permitted to ask for each action comes from
        `CONNECTION_ACTIONS`, and the check happens BEFORE the service is
        called. A caregiver asking to `accept` and a provider asking to
        `revoke` are both refused here with the standard constant body — the
        service would refuse them too, but the transport must not depend on
        that to be the thing enforcing it.

        An unknown action renders as the same refusal rather than a 404, so
        the action vocabulary is not enumerable either.
        """
        route = CONNECTION_ACTION_ROUTE
        principal, refusal = self._caregiver_or_provider(
            environ, route, "POST", request_id)
        if refusal is not None:
            return refusal

        required = CONNECTION_ACTIONS.get(action)
        if required is None or principal.role is not required:
            return self._refuse(route, "POST", request_id, principal)

        service = self._connection_service()
        try:
            if action == "accept":
                result = service.accept_invitation(
                    principal, connection_id, request_id=request_id)
            elif action == "decline":
                result = service.decline_invitation(
                    principal, connection_id, request_id=request_id)
            elif action == "pause":
                result = service.pause_connection(
                    principal, connection_id, request_id=request_id)
            elif action == "resume":
                result = service.resume_connection(
                    principal, connection_id, request_id=request_id)
            else:
                from ..domain.enums import ConnectionStatus as _Status

                result = service.revoke_connection(
                    principal, connection_id, request_id=request_id,
                    status=(_Status.ENDED if action == "end"
                            else _Status.REVOKED))
        except IntegrationError:
            return self._refuse(route, "POST", request_id, principal)

        self._log_principal(request_id, route, "POST", HTTP_OK, principal)
        return HTTP_OK, {
            "connection_id": result.connection_id,
            "status": result.status.value,
            "request_id": request_id,
        }, route

    def _handle_child_connections(self, environ: Mapping[str, object],
                                  child_id: str, request_id: str
                                  ) -> Tuple[int, Mapping, str]:
        """Every connection on a child the CALLER holds, closed rows included."""
        route = CHILD_CONNECTIONS_ROUTE
        principal, refusal = self._caregiver_or_provider(
            environ, route, "GET", request_id)
        if refusal is not None:
            return refusal
        try:
            rows = self._connection_service().list_child_connections(
                principal, child_id)
        except IntegrationError:
            return self._refuse(route, "GET", request_id, principal)

        self._log_principal(request_id, route, "GET", HTTP_OK, principal)
        return HTTP_OK, {
            "connections": [{
                "connection_id": row.connection_id,
                "provider_id": row.provider_id,
                "status": row.status.value,
                "initiated_by": row.initiated_by.value,
            } for row in rows],
            "request_id": request_id,
        }, route

    def _handle_assign_managing(self, environ: Mapping[str, object],
                                child_id: str, provider_id: str,
                                request_id: str) -> Tuple[int, Mapping, str]:
        """A caregiver names an ACTIVE-connected provider as managing clinician.

        Separate from accepting a connection, deliberately: an ACTIVE
        connection alone never implies clinical ownership, and this is the
        explicit second decision.
        """
        route = ASSIGN_MANAGING_ROUTE
        principal, refusal = self._caregiver_or_provider(
            environ, route, "POST", request_id)
        if refusal is not None:
            return refusal
        try:
            assignment = self._connection_service().assign_managing_clinician(
                principal, child_id, provider_id, request_id=request_id)
        except IntegrationError:
            return self._refuse(route, "POST", request_id, principal)

        self._log_principal(request_id, route, "POST", HTTP_OK, principal)
        return HTTP_OK, {
            "assignment_id": assignment.assignment_id,
            "provider_id": assignment.provider_id,
            "request_id": request_id,
        }, route

    def _handle_read_managing(self, environ: Mapping[str, object],
                              child_id: str, request_id: str
                              ) -> Tuple[int, Mapping, str]:
        """The child's current managing clinician, or null."""
        route = MANAGING_CLINICIAN_ROUTE
        principal, refusal = self._caregiver_or_provider(
            environ, route, "GET", request_id)
        if refusal is not None:
            return refusal
        try:
            assignment = self._connection_service().current_managing_clinician(
                principal, child_id)
        except IntegrationError:
            return self._refuse(route, "GET", request_id, principal)

        self._log_principal(request_id, route, "GET", HTTP_OK, principal)
        return HTTP_OK, {
            "assignment_id": assignment.assignment_id if assignment else None,
            "provider_id": assignment.provider_id if assignment else None,
            "request_id": request_id,
        }, route

    def _handle_end_managing(self, environ: Mapping[str, object],
                             child_id: str, request_id: str
                             ) -> Tuple[int, Mapping, str]:
        """End the assignment without altering the connection."""
        route = END_MANAGING_ROUTE
        principal, refusal = self._caregiver_or_provider(
            environ, route, "POST", request_id)
        if refusal is not None:
            return refusal
        try:
            ended = self._connection_service().end_managing_clinician(
                principal, child_id, request_id=request_id)
        except IntegrationError:
            return self._refuse(route, "POST", request_id, principal)

        self._log_principal(request_id, route, "POST", HTTP_OK, principal)
        return HTTP_OK, {
            "assignment_id": ended.assignment_id,
            "request_id": request_id,
        }, route

    # =====================================================================
    # 0.5C workflow surface
    # =====================================================================
    #
    # Every handler here is a TRANSLATOR: resolve the principal, read an
    # allowlisted body, call ONE frozen service method, serialise the result.
    # No handler re-implements an authorization rule — `_authorize`,
    # `_require_managing_clinician` and `_require_caregiver` live in the
    # services, already have tests, and are the same functions every prior
    # slice uses. A transport-layer copy would be a second rule set to keep
    # correct, and the one that drifted would be the one nobody tested.

    def _services(self):
        """The frozen services, built per request. No cross-request state."""
        from ..goals.service import GoalService
        from ..planning.service import MonthlyPlanService
        from ..rtm.service import RTMService
        from ..weekly.service import WeeklyService

        shared = {"repos": self._repos, "recorder": self._recorder}
        return (GoalService(**shared), MonthlyPlanService(**shared),
                WeeklyService(**shared), RTMService(**shared))

    def _handle_workflow(self, environ, route: str, method: str,
                         resource: str, request_id: str):
        """One entry point for the thirteen resource-subroute handlers.

        Centralised so the principal resolution and the refusal mapping happen
        in exactly one place. Every domain, service and body error renders as
        the SAME constant 403 body: the services already collapse
        absent-versus-not-yours, and preserving their finer distinctions in the
        response would hand a prober the oracle they were careful not to be.
        """
        from ..transport.body import BodyError

        principal, status = self._principal(environ)
        if principal is None:
            self._log(request_id, route, method, status, None)
            return status, _ERROR_BODIES[status], route

        try:
            payload = self._dispatch_workflow(environ, route, method, resource,
                                              principal, request_id)
        except BodyError:
            # A malformed or over-reaching body. 400 would be more RESTful,
            # but it would also tell a caller which of their fields the server
            # recognises, so it renders as the standard refusal.
            return self._refuse(route, method, request_id, principal)
        except Exception as exc:  # noqa: BLE001 - classified below
            if not getattr(exc, "PHI_SAFE_MESSAGE", False):
                # An unexpected failure. Nothing about it reaches the client.
                self._log(request_id, route, method, 500, None)
                return 500, _ERROR_BODIES[500], route
            return self._refuse(route, method, request_id, principal)

        self._log_principal(request_id, route, method, HTTP_OK, principal)
        payload["request_id"] = request_id
        return HTTP_OK, payload, route

    def _dispatch_workflow(self, environ, route, method, resource, principal,
                           request_id):
        """Route -> one frozen service call. Raises; never returns a status."""
        from ..domain.goals import EditType, GoalKind, GoalRef
        from ..domain.observation import AttemptOutcome, Difficulty, Enjoyment
        from ..domain.rtm_documentation import (
            ClinicalActionType,
            InteractionModality,
            ParticipantType,
        )
        from ..transport.body import (
            enum_member,
            opt_bool,
            opt_int,
            opt_str,
            opt_str_list,
            read_json_body,
        )

        goals, plans, weekly, rtm = self._services()

        if route == CHILD_GOALS_ROUTE and method == "GET":
            # 0.5D: the wording comes with the goal.
            #
            # `current_text` is the EXISTING authorized read — it calls
            # `get_goal`, which authorizes the child, then resolves
            # `current_version_id` against the immutable version chain. So the
            # text is not duplicated onto `ClinicalGoal` and no new rule is
            # introduced here; this is the frozen method, called once per goal.
            #
            # One Firestore read per goal, which is an N+1. Left as-is
            # deliberately: a batched read would be new behaviour in a frozen
            # service, and the pilot lists a handful of goals per child.
            return {"goals": [
                _goal_payload(g, GoalKind.CLINICAL,
                              text=goals.current_text(
                                  principal,
                                  GoalRef(GoalKind.CLINICAL,
                                          g.clinical_goal_id)))
                for g in goals.list_clinical_goals(principal, resource)]}

        if route == CHILD_GOALS_ROUTE:
            body = read_json_body(environ, allowed=[
                "edit_type", "suggestion_id", "text", "reason"])
            goal = goals.approve_clinical_goal(
                principal, resource,
                edit_type=enum_member(EditType, body, "edit_type"),
                suggestion_id=opt_str(body, "suggestion_id") or None,
                text=opt_str(body, "text"),
                reason=opt_str(body, "reason"), request_id=request_id)
            # The approved wording, read back through the same authorized
            # path rather than echoed from the request body — what was
            # PERSISTED is what the UI should render, and `_approved_text`
            # may not have kept the submitted string verbatim.
            return {"goal": _goal_payload(
                goal, GoalKind.CLINICAL,
                text=goals.current_text(
                    principal,
                    GoalRef(GoalKind.CLINICAL, goal.clinical_goal_id)))}

        if route == CHILD_SUGGESTIONS_ROUTE:
            return {"suggestions": [_suggestion_payload(s)
                                    for s in goals.list_suggestions(
                                        principal, resource)]}

        if route == CHILD_MONTHLY_PLAN_ROUTE and method == "GET":
            body = {}
            plan = plans.active_plan(principal, resource,
                                     _requested_month(environ))
            if plan is None:
                return {"plan": None, "allocations": []}
            return {"plan": _plan_payload(plan),
                    "allocations": [_allocation_payload(a) for a in
                                    plans.list_allocations(
                                        principal, plan.focus_plan_id)]}

        if route == CHILD_MONTHLY_PLAN_ROUTE:
            # Creates a DRAFT. Activation is a separate call, because the
            # domain refuses to activate a month with no allocated goal — see
            # PLAN_ACTIVATE_ROUTE. An earlier revision of this handler
            # activated here and failed on exactly that invariant.
            body = read_json_body(
                environ, allowed=["cycle_month", "timezone_of_record"],
                required=["cycle_month", "timezone_of_record"])
            plan = plans.create_plan(
                principal, resource, opt_str(body, "cycle_month"),
                opt_str(body, "timezone_of_record"), request_id=request_id)
            return {"plan": _plan_payload(plan)}

        if route == PLAN_ACTIVATE_ROUTE:
            plan = plans.activate_plan(principal, resource,
                                       request_id=request_id)
            return {"plan": _plan_payload(plan)}

        if route == CHILD_CURRENT_CYCLE_ROUTE:
            plan = plans.active_plan(principal, resource,
                                     _requested_month(environ))
            if plan is None:
                return {"plan": None, "cycle": None, "alignments": [],
                        "coverage_gaps": [], "plan_snapshot": None}
            cycles = weekly.list_cycles(principal, plan.focus_plan_id)
            if not cycles:
                return {"plan": _plan_payload(plan), "cycle": None,
                        "alignments": [], "coverage_gaps": [],
                        "plan_snapshot": None}
            current = cycles[-1]
            # `list_cycles` authorized the child, so the snapshot row read
            # below belongs to a cycle the caller is already proven to hold.
            # Read through the repository because the frozen weekly service
            # exposes no snapshot read and inventing one would be new business
            # logic — the same projection pattern CHILD_RTM_ROUTE uses.
            snapshots = self._repos.weekly_plan_snapshots.list_for_cycle(
                current.cycle_id)
            return {
                "plan": _plan_payload(plan),
                "cycle": _cycle_payload(current),
                "alignments": [_alignment_payload(a) for a in
                               weekly.list_alignments(principal,
                                                      current.cycle_id)],
                "coverage_gaps": [_gap_payload(g) for g in
                                  weekly.list_coverage_gaps(
                                      principal, current.cycle_id)],
                "plan_snapshot": (_snapshot_payload(snapshots[0])
                                  if snapshots else None),
            }

        if route == THIS_WEEK_ROUTE:
            # 0.6A-2. The family's week. `_handle_workflow` has already resolved
            # and authorized the principal for this child, so a CAREGIVER
            # reaches here legitimately — reading a released week is exactly
            # what they are entitled to.
            #
            # The service returns None for "nothing released", including when a
            # DRAFT cycle exists. A draft is deliberately indistinguishable from
            # nothing here: a partially allocated week must never reach a family.
            week = self._week_one_service(goals, plans, weekly).this_week(
                principal, resource)
            return {"child_id": resource, "released": week is not None,
                    "week": week}

        if route == CHILD_RTM_ROUTE:
            # `list_episodes` authorizes the child first, so the period rows
            # read below belong to an episode the caller is already proven to
            # hold. Read through the repository because the frozen RTM service
            # exposes no list-periods method and inventing one would be new
            # business logic — this is a projection of already-authorized
            # rows, not a new rule.
            episodes = rtm.list_episodes(principal, resource)
            payload = []
            for episode in episodes:
                periods = self._repos.rtm_periods.list_for_episode(
                    episode.episode_id)
                entry = _episode_payload(episode)
                entry["periods"] = [_period_payload(p) for p in periods]
                entry["documented_minutes"] = {
                    p.period_id: rtm.documented_minutes_for(principal,
                                                            p.period_id)
                    for p in periods}
                payload.append(entry)
            return {"episodes": payload}

        if route == PLAN_ALLOCATIONS_ROUTE:
            body = read_json_body(environ, allowed=[
                "goal_kind", "goal_id", "priority_rank", "emphasis_weight",
                "min_coverage_per_cycle", "reason"],
                required=["goal_kind", "goal_id"])
            ref = GoalRef(enum_member(GoalKind, body, "goal_kind"),
                          opt_str(body, "goal_id"))
            allocation = plans.allocate_goal(
                principal, resource, ref,
                # `priority_rank` has no service default, so it is always
                # sent; the other two do and are omitted when absent.
                priority_rank=opt_int(body, "priority_rank") or 1,
                reason=opt_str(body, "reason"), request_id=request_id,
                **_present(
                    emphasis_weight=opt_int(body, "emphasis_weight"),
                    min_coverage_per_cycle=opt_int(
                        body, "min_coverage_per_cycle")))
            return {"allocation": _allocation_payload(allocation)}

        if route == CYCLE_OBSERVATIONS_ROUTE and method == "GET":
            return {"observations": [_observation_payload(o) for o in
                                     weekly.list_observations(principal,
                                                              resource)]}

        if route == CYCLE_OBSERVATIONS_ROUTE:
            # NO free-text note field is allowlisted. `observation_text_ref`
            # and `assistance`/`child_response` are deliberately absent: the
            # pilot carries structured evidence only, and an opaque text ref
            # would need an approved store that does not exist.
            body = read_json_body(environ, allowed=[
                "activity_instance_ref", "local_date", "attempt_outcome",
                "difficulty", "enjoyment"],
                required=["activity_instance_ref", "local_date",
                          "attempt_outcome"])
            event = weekly.record_observation(
                principal, resource, opt_str(body, "activity_instance_ref"),
                local_date=opt_str(body, "local_date"),
                attempt_outcome=enum_member(AttemptOutcome, body,
                                            "attempt_outcome"),
                difficulty=(enum_member(Difficulty, body, "difficulty")
                            if opt_str(body, "difficulty") else None),
                enjoyment=(enum_member(Enjoyment, body, "enjoyment")
                           if opt_str(body, "enjoyment") else None),
                request_id=request_id)
            return {"observation": _observation_payload(event)}

        if route == CYCLE_DEFERS_ROUTE:
            body = read_json_body(
                environ, allowed=["activity_instance_ref",
                                  "suppress_for_cycles"],
                required=["activity_instance_ref"])
            record = weekly.defer_activity(
                principal, resource, opt_str(body, "activity_instance_ref"),
                request_id=request_id,
                **_present(suppress_for_cycles=opt_int(
                    body, "suppress_for_cycles")))
            return {"defer": {"defer_id": record.defer_id,
                              "activity_instance_ref":
                                  record.activity_instance_ref}}

        if route == PERIOD_REVIEWS_ROUTE:
            body = read_json_body(
                environ, allowed=["clinical_interpretation",
                                  "reviewed_event_ids", "reviewed_cycle_ids"],
                required=["clinical_interpretation"])
            review = rtm.record_review(
                principal, resource,
                clinical_interpretation=opt_str(body,
                                                "clinical_interpretation"),
                reviewed_event_ids=opt_str_list(body, "reviewed_event_ids"),
                reviewed_cycle_ids=opt_str_list(body, "reviewed_cycle_ids"),
                request_id=request_id)
            return {"review": {"review_id": review.review_id,
                               "reviewed_event_count":
                                   len(review.reviewed_event_ids)}}

        if route == REVIEW_ACTIONS_ROUTE:
            body = read_json_body(environ,
                                  allowed=["action_type", "narrative"],
                                  required=["action_type"])
            action = rtm.record_clinical_action(
                principal, resource,
                action_type=enum_member(ClinicalActionType, body,
                                        "action_type"),
                narrative=opt_str(body, "narrative"), request_id=request_id)
            return {"action": {"action_id": action.action_id,
                               "action_type": action.action_type.value}}

        if route == PERIOD_TIME_ROUTE:
            # Minutes are SUPPLIED, never inferred — the frozen 0.4F/G rule.
            body = read_json_body(
                environ, allowed=["local_date", "minutes",
                                  "activity_description", "source_review_id",
                                  "source_action_id"],
                required=["local_date", "activity_description"])
            entry = rtm.record_time(
                principal, resource, local_date=opt_str(body, "local_date"),
                activity_description=opt_str(body, "activity_description"),
                source_review_id=opt_str(body, "source_review_id") or None,
                source_action_id=opt_str(body, "source_action_id") or None,
                request_id=request_id,
                **_present(minutes=opt_int(body, "minutes")))
            return {"time_entry": {"time_entry_id": entry.time_entry_id,
                                   "minutes": entry.minutes}}

        if route == PERIOD_INTERACTIONS_ROUTE:
            # `real_time_affirmed` is the clinician's explicit attestation.
            # An async message is never a synchronous interaction, and the
            # service refuses one that is not affirmed.
            body = read_json_body(
                environ, allowed=["local_date", "modality", "participant_type",
                                  "duration_minutes", "real_time_affirmed"],
                required=["local_date"])
            interaction = rtm.record_synchronous_interaction(
                principal, resource, local_date=opt_str(body, "local_date"),
                modality=enum_member(InteractionModality, body, "modality"),
                participant_type=enum_member(ParticipantType, body,
                                             "participant_type"),
                real_time_affirmed=bool(opt_bool(body, "real_time_affirmed")),
                request_id=request_id,
                **_present(duration_minutes=opt_int(body, "duration_minutes")))
            return {"interaction": {
                "interaction_id": interaction.interaction_id,
                "modality": interaction.modality.value}}

        if route == PERIOD_REPORT_ROUTE:
            # A PREVIEW, and the three steps are the frozen PREREQUISITE
            # CHAIN, not an orchestration choice: `generate_report` refuses
            # with "generate the evidence and coding summaries before the
            # report". So the coding summary is produced here because the
            # report cannot exist without it — which is why it appears in the
            # Tuesday surface at all.
            #
            # `generate_report` produces a DRAFT and nothing here finalizes
            # it. The coding assistance is a CANDIDATE requiring explicit
            # clinician confirmation: 98979/98980/98981 output carries no
            # reimbursement guarantee and Genex makes no medical-necessity
            # determination. `decide_coding_assistance` is deliberately NOT
            # exposed — confirming a code is not a preview.
            summary = rtm.generate_evidence_summary(principal, resource,
                                                    request_id=request_id)
            coding = rtm.generate_coding_assistance(principal, resource,
                                                    request_id=request_id)
            report = rtm.generate_report(principal, resource,
                                         request_id=request_id)
            return {
                "report": _report_payload(report),
                "evidence_summary": {"summary_id": summary.summary_id},
                "coding_assistance": _coding_payload(coding),
            }

        raise AssertionError(f"unrouted workflow route: {route}")  # pragma: no cover

    def _handle_goal_revision(self, environ, goal_kind: str, goal_id: str,
                              request_id: str):
        """Revise a goal's wording. Appends a version; never rewrites one."""
        from ..domain.goals import EditType, GoalKind, GoalRef
        from ..transport.body import BodyError, enum_member, opt_str, read_json_body

        route = GOAL_REVISIONS_ROUTE
        principal, status = self._principal(environ)
        if principal is None:
            self._log(request_id, route, "POST", status, None)
            return status, _ERROR_BODIES[status], route

        try:
            kind = next((k for k in GoalKind if k.value == goal_kind), None)
            if kind is None:
                # An unrecognised kind renders as the standard refusal rather
                # than a 404, so the kind vocabulary is not enumerable.
                return self._refuse(route, "POST", request_id, principal)
            body = read_json_body(environ,
                                  allowed=["text", "edit_type", "reason"],
                                  required=["text"])
            goals = self._services()[0]
            version = goals.revise_goal(
                principal, GoalRef(kind, goal_id), opt_str(body, "text"),
                edit_type=enum_member(EditType, body, "edit_type"),
                reason=opt_str(body, "reason"), request_id=request_id)
        except BodyError:
            return self._refuse(route, "POST", request_id, principal)
        except Exception as exc:  # noqa: BLE001 - classified below
            if not getattr(exc, "PHI_SAFE_MESSAGE", False):
                self._log(request_id, route, "POST", 500, None)
                return 500, _ERROR_BODIES[500], route
            return self._refuse(route, "POST", request_id, principal)

        self._log_principal(request_id, route, "POST", HTTP_OK, principal)
        return HTTP_OK, {"version_id": version.version_id,
                         "version_number": version.version_number,
                         "request_id": request_id}, route

    def _handle_bootstrap(self, environ: Mapping[str, object],
                          request_id: str) -> Tuple[int, Mapping, str]:
        """Create or resolve the caregiver identity for the VERIFIED subject.

        The request body is NEVER read. Not validated, not parsed, not even
        measured — `wsgi.input` is untouched. A body carrying `role`,
        `caregiver_id`, `provider_id`, `uid` or `auth_subject` therefore has no
        route into this handler at all; the subject comes from the verified
        token and the role is a constant of the operation.

        A `display_name` is not accepted either. It would be the one field a
        client could set, and a caregiver's name is not something 0.5A needs.
        """
        subject, status = self._verified_subject(environ)
        if subject is None:
            self._log(request_id, BOOTSTRAP_CAREGIVER_ROUTE, "POST", status, None)
            return status, _ERROR_BODIES[status], BOOTSTRAP_CAREGIVER_ROUTE

        try:
            caregiver = self._identity_service().bootstrap_caregiver(
                subject, request_id=request_id)
        except IntegrationError:
            # SubjectAlreadyHeld and AmbiguousSubjectState both render as the
            # constant 403 body. A caller learns that they may not bootstrap,
            # not which pre-existing record stopped them.
            self._log(request_id, BOOTSTRAP_CAREGIVER_ROUTE, "POST",
                      HTTP_FORBIDDEN, None)
            return (HTTP_FORBIDDEN, _ERROR_BODIES[HTTP_FORBIDDEN],
                    BOOTSTRAP_CAREGIVER_ROUTE)

        self._log(request_id, BOOTSTRAP_CAREGIVER_ROUTE, "POST", HTTP_OK, None)
        return HTTP_OK, {"actor_id": caregiver.caregiver_id,
                         "caregiver_id": caregiver.caregiver_id,
                         "role": ActorRole.CAREGIVER.value,
                         "request_id": request_id}, BOOTSTRAP_CAREGIVER_ROUTE

    def _handle_link_child(self, environ: Mapping[str, object], session_id: str,
                           request_id: str) -> Tuple[int, Mapping, str]:
        """Bridge an OWNED Parent session to a canonical child.

        The client supplies only the path segment. Ownership is proven
        server-side against the verified subject, and the body is never read —
        so an `owner_uid` in a payload cannot assert ownership of a session.

        `SecondSessionUnresolved` is the one refusal that returns its code: it
        is a product state the client must act on (multi-child selection), not
        a security refusal, and it discloses only that THIS account already has
        a linked session — which that account already knows.
        """
        principal, status = self._principal(environ)
        if principal is None:
            self._log(request_id, LINK_CHILD_ROUTE, "POST", status, None)
            return status, _ERROR_BODIES[status], LINK_CHILD_ROUTE
        try:
            result = self._identity_service().link_parent_session(
                principal, session_id, request_id=request_id)
        except SecondSessionUnresolved:
            self._log_principal(request_id, LINK_CHILD_ROUTE, "POST", 409,
                                principal)
            return 409, {"error": "parent session ambiguous",
                         "code": SecondSessionUnresolved.code}, LINK_CHILD_ROUTE
        except IntegrationError:
            self._log_principal(request_id, LINK_CHILD_ROUTE, "POST",
                                HTTP_FORBIDDEN, principal)
            return HTTP_FORBIDDEN, _ERROR_BODIES[HTTP_FORBIDDEN], LINK_CHILD_ROUTE

        self._log_principal(request_id, LINK_CHILD_ROUTE, "POST", HTTP_OK,
                            principal)
        return HTTP_OK, {"child_id": result.child_id,
                         "source_link_id": result.source_link_id,
                         "created": result.created,
                         "request_id": request_id}, LINK_CHILD_ROUTE

    def _handle_consume_claim(self, environ: Mapping[str, object],
                              request_id: str) -> Tuple[int, Mapping, str]:
        """Redeem a Parent handoff capability (0.5F-A3).

        The caregiver is resolved from the verified token, exactly as every
        other route resolves it. The body supplies ONE field, and
        `read_json_body` rejects every identity field by name — so this endpoint
        cannot be handed a `child_id`, a `caregiver_id` or an `auth_subject`.

        Every unusable-capability state collapses to ONE response. A caller
        cannot tell "no such token" from "expired" from "already spent" from
        "that child is somebody else's", which is what stops this becoming an
        oracle for which Parent sessions exist.
        """
        from ..transport.body import BodyError, read_json_body

        principal, status = self._principal(environ)
        if principal is None:
            self._log(request_id, CONSUME_CLAIM_ROUTE, "POST", status, None)
            return status, _ERROR_BODIES[status], CONSUME_CLAIM_ROUTE

        try:
            body = read_json_body(environ, allowed=CONSUME_CLAIM_FIELDS,
                                  required=("claim_token",))
        except BodyError:
            # The SAME constant 403 as every other refusal here, following the
            # 0.5C rule: a 400 naming the offending field would tell a caller
            # which fields this server recognises. It also means a missing,
            # malformed and simply-wrong token are one response.
            self._log_principal(request_id, CONSUME_CLAIM_ROUTE, "POST",
                                HTTP_FORBIDDEN, principal)
            return (HTTP_FORBIDDEN, _ERROR_BODIES[HTTP_FORBIDDEN],
                    CONSUME_CLAIM_ROUTE)

        try:
            result = self._identity_service().consume_parent_session_claim(
                principal, str(body.get("claim_token") or ""),
                request_id=request_id)
        except SecondSessionUnresolved:
            # The one refusal that returns its code: a product state the client
            # must act on, disclosing only that THIS account already has a
            # linked session — which that account already knows.
            self._log_principal(request_id, CONSUME_CLAIM_ROUTE, "POST", 409,
                                principal)
            return 409, {"error": "parent session ambiguous",
                         "code": SecondSessionUnresolved.code}, CONSUME_CLAIM_ROUTE
        except IntegrationError:
            # ParentSessionClaimUnusable, ParentSessionUnavailable,
            # SubjectAlreadyHeld and ParentSessionLinkContended all land here
            # with ONE status and ONE body, so the distinctions that exist for
            # auditing are not readable from outside.
            self._log_principal(request_id, CONSUME_CLAIM_ROUTE, "POST",
                                HTTP_FORBIDDEN, principal)
            return (HTTP_FORBIDDEN, _ERROR_BODIES[HTTP_FORBIDDEN],
                    CONSUME_CLAIM_ROUTE)

        self._log_principal(request_id, CONSUME_CLAIM_ROUTE, "POST", HTTP_OK,
                            principal)
        return HTTP_OK, {"child_id": result.child_id,
                         "source_link_id": result.source_link_id,
                         "created": result.created,
                         "request_id": request_id}, CONSUME_CLAIM_ROUTE

    def _handle_generate_suggestions(self, environ: Mapping[str, object],
                                     child_id: str, request_id: str
                                     ) -> Tuple[int, Mapping, str]:
        """0.5F-B. Provider-triggered deterministic anchored generation.

        ## The authorization rule, in order

          1. a verified Pilot principal (the protected-route gate above)
          2. `principal.role is ActorRole.PROVIDER` — a caregiver is refused
             here, before any service call, so a family can never trigger
             clinical target selection for their own child
          3. active child access, via `GoalService._authorize`
          4. the caller must BE this child's current managing clinician, via the
             EXISTING `_require_managing_clinician` — no second authorization
             model is introduced for this route

        Step 4 is what makes "provider-triggered" narrow rather than "any
        connected clinician triggered". It reuses the 0.4A assignment service,
        which already refuses when there is no active assignment and when there
        is more than one.

        ## Hannah triggers; she does not author

        Nothing here creates a ClinicalGoal, and the suggestion's status stays
        OFFERED. The developmental target came from the frozen algorithm over the
        immutable projection — her identity is not in the generation key at all.

        ## No body

        The route takes no request body. Every input is derived server-side from
        the child's active Parent source link and its projection, so there is
        nothing a client could supply and no way for one to influence the target.
        """
        from ..domain.roles import ActorRole
        from ..goals.errors import (
            GoalAuthorizationError,
            GoalConflict,
            GoalValidationError,
        )
        from ..integration.baseline_suggestion_generation import (
            BaselineSuggestionGenerationService,
        )
        from ..domain.suggestion_generation import SuggestionGenerationError

        principal, status = self._principal(environ)
        if principal is None:
            self._log(request_id, GENERATE_SUGGESTIONS_ROUTE, "POST", status,
                      None)
            return status, _ERROR_BODIES[status], GENERATE_SUGGESTIONS_ROUTE

        if principal.role is not ActorRole.PROVIDER:
            # A caregiver must not trigger clinical generation for their own
            # child. Refused before any service call.
            self._log_principal(request_id, GENERATE_SUGGESTIONS_ROUTE, "POST",
                                HTTP_FORBIDDEN, principal)
            return (HTTP_FORBIDDEN, _ERROR_BODIES[HTTP_FORBIDDEN],
                    GENERATE_SUGGESTIONS_ROUTE)

        goals, _plans, _weekly, _rtm = self._services()
        rung_source = self._rung_source
        if rung_source is None:
            # No Gold Standard source is configured. A capability gap, and it
            # fails closed: without the real workbook no canonical target can be
            # resolved, and generating an unanchored suggestion here would
            # produce a goal `allocate_goal` later refuses.
            self._log_principal(request_id, GENERATE_SUGGESTIONS_ROUTE, "POST",
                                HTTP_FORBIDDEN, principal)
            return (HTTP_FORBIDDEN, _ERROR_BODIES[HTTP_FORBIDDEN],
                    GENERATE_SUGGESTIONS_ROUTE)

        try:
            goals._authorize(principal, child_id)
            # The EXISTING managing-clinician gate. Reused, not reimplemented.
            goals._require_managing_clinician(principal, child_id)
            outcome = BaselineSuggestionGenerationService(
                repos=self._repos, goals=goals, rung_source=rung_source
            ).generate_for_child(principal, child_id, request_id=request_id)
        except (GoalAuthorizationError, GoalConflict, GoalValidationError,
                SuggestionGenerationError):
            # Every refusal renders as the SAME constant 403, following the
            # 0.5C rule: a finer status would tell a prober which gate stopped
            # them, and the services already collapse absent-versus-not-yours.
            self._log_principal(request_id, GENERATE_SUGGESTIONS_ROUTE, "POST",
                                HTTP_FORBIDDEN, principal)
            return (HTTP_FORBIDDEN, _ERROR_BODIES[HTTP_FORBIDDEN],
                    GENERATE_SUGGESTIONS_ROUTE)

        self._log_principal(request_id, GENERATE_SUGGESTIONS_ROUTE, "POST",
                            HTTP_OK, principal)
        # The response carries ids and the canonical target only — no milestone
        # text, no family-facing wording, nothing clinical. Hannah reads the
        # suggestion itself through the existing unchanged GET.
        return HTTP_OK, {
            "child_id": child_id,
            "created": outcome.created,
            "projection_id": outcome.projection_id,
            "target_rung_ref": outcome.target_rung_ref,
            "target_rung_months": outcome.target_rung_months,
            "suggestion_ids": [s.suggestion_id for s in outcome.suggestions],
            "request_id": request_id,
        }, GENERATE_SUGGESTIONS_ROUTE

    def _handle_generate_suggestions_v2(self, environ: Mapping[str, object],
                                        child_id: str, request_id: str
                                        ) -> Tuple[int, Mapping, str]:
        """0.6A-1G. Provider-triggered EVIDENCE-DRIVEN generation.

        ## Authorization is byte-for-byte v1's, and deliberately so

          1. a verified Pilot principal (the protected-route gate above)
          2. `principal.role is ActorRole.PROVIDER` — a caregiver is refused
             here, before any service call, so a family can never trigger
             clinical target selection for their own child
          3. active child access, via `GoalService._authorize`
          4. the caller must BE this child's current managing clinician, via the
             EXISTING `_require_managing_clinician`

        No new authorization model, no widened gate, and no separate role for v2.
        A richer answer is not a reason to let more people ask the question.

        ## Every refusal is the SAME constant 403

        The 0.5C no-oracle rule. A finer status would tell a prober which gate
        stopped them, and the services already collapse absent-versus-not-yours.

        ## The response can say "nothing to generate" WITHOUT that being a refusal

        Three of the four outcomes produce zero suggestions and are still 200:
        every band mastered, a band unresolved only by `unknown`, and a band whose
        known deficits are all canonically unmappable. Rendering those as 403
        would tell Hannah her request failed when in fact it answered — and would
        push her to retry something that will give the same true result.

        ## No body, and nothing clinical in the response

        Every input is derived server-side from the child's active Parent source
        link and its v2 projection. The response carries canonical refs, ids and
        counts only: no milestone prose from the transient A2 request, no
        caregiver answer, no Parent identifier, and no generation claim id.
        """
        from ..domain.roles import ActorRole
        from ..domain.suggestion_generation import SuggestionGenerationError
        from ..domain.suggestion_generation_v2 import GenerationV2Error
        from ..goals.errors import (
            GoalAuthorizationError,
            GoalConflict,
            GoalValidationError,
        )
        from ..integration.baseline_suggestion_generation_v2 import (
            BaselineSuggestionGenerationV2Service,
        )

        principal, status = self._principal(environ)
        if principal is None:
            self._log(request_id, GENERATE_SUGGESTIONS_V2_ROUTE, "POST", status,
                      None)
            return status, _ERROR_BODIES[status], GENERATE_SUGGESTIONS_V2_ROUTE

        if principal.role is not ActorRole.PROVIDER:
            self._log_principal(request_id, GENERATE_SUGGESTIONS_V2_ROUTE,
                                "POST", HTTP_FORBIDDEN, principal)
            return (HTTP_FORBIDDEN, _ERROR_BODIES[HTTP_FORBIDDEN],
                    GENERATE_SUGGESTIONS_V2_ROUTE)

        goals, _plans, _weekly, _rtm = self._services()
        rung_source = self._rung_source
        if rung_source is None:
            # No Gold Standard source is configured. A capability gap, and it
            # fails closed: without the frozen table no canonical ref can be
            # verified, and anchoring to an unverified ref would produce a goal
            # `allocate_goal` later refuses.
            self._log_principal(request_id, GENERATE_SUGGESTIONS_V2_ROUTE,
                                "POST", HTTP_FORBIDDEN, principal)
            return (HTTP_FORBIDDEN, _ERROR_BODIES[HTTP_FORBIDDEN],
                    GENERATE_SUGGESTIONS_V2_ROUTE)

        try:
            goals._authorize(principal, child_id)
            # The EXISTING managing-clinician gate. Reused, not reimplemented.
            goals._require_managing_clinician(principal, child_id)
            outcome = BaselineSuggestionGenerationV2Service(
                repos=self._repos, goals=goals, rung_source=rung_source
            ).generate_for_child(principal, child_id, request_id=request_id)
        except (GoalAuthorizationError, GoalConflict, GoalValidationError,
                SuggestionGenerationError, GenerationV2Error):
            self._log_principal(request_id, GENERATE_SUGGESTIONS_V2_ROUTE,
                                "POST", HTTP_FORBIDDEN, principal)
            return (HTTP_FORBIDDEN, _ERROR_BODIES[HTTP_FORBIDDEN],
                    GENERATE_SUGGESTIONS_V2_ROUTE)

        self._log_principal(request_id, GENERATE_SUGGESTIONS_V2_ROUTE, "POST",
                            HTTP_OK, principal)
        return HTTP_OK, {
            "child_id": child_id,
            "outcome": outcome.outcome,
            "generation_policy": outcome.generation_policy,
            "projection_id": outcome.projection_id,
            "target_band_months": outcome.target_band_months,
            # One entry per generated target, each with its own `created` flag,
            # so a replay is distinguishable per target rather than collapsed to
            # one boolean for the whole request.
            "generated": [
                {"rung_ref": target.rung_ref,
                 "months": target.months,
                 "created": target.created,
                 "suggestion_ids": list(target.suggestion_ids)}
                for target in outcome.generated],
            "suggestion_ids": list(outcome.suggestion_ids),
            # Canonical deficits with no reconciled activity family. Reported
            # rather than dropped, and NOT rendered as suggestions — a suggestion
            # with no valid anchor would become a goal allocation later refuses.
            "unsupported_target_refs": list(outcome.unsupported_target_refs),
            # Assessed-but-unanswerable skills keeping the band off "mastered".
            "unknown_refs": list(outcome.unknown_refs),
            "request_id": request_id,
        }, GENERATE_SUGGESTIONS_V2_ROUTE

    def _week_one_service(self, goals, plans, weekly):
        """The Week 1 orchestrator over the request's frozen services.

        The activity bank is the COMPOSED one — the same lookup-only static bank
        the browser image carries. `None` means no reviewed content is
        configured, and the callers below fail closed rather than planning a week
        with no activities.
        """
        from ..integration.week_one_release import WeekOneReleaseService

        return WeekOneReleaseService(
            repos=self._repos, goals=goals, plans=plans, weekly=weekly,
            activity_bank=self._activity_bank)

    def _handle_week_one_release(self, environ: Mapping[str, object],
                                 child_id: str, request_id: str
                                 ) -> Tuple[int, Mapping, str]:
        """0.6A-2. ONE managing-provider action: approved goal -> released Week 1.

        ## Authorization, in order, and identical to the generation routes'

          1. a verified Pilot principal
          2. `principal.role is ActorRole.PROVIDER` — a caregiver is refused
             here, before any service call. A family may READ a released week;
             they may never create, allocate or release one.
          3. active child access, via `GoalService._authorize`
          4. the caller must BE this child's current managing clinician

        ## The body carries the family's declared weekly capacity, and nothing else

        `family_declared_capacity` is REQUIRED. It is what the family reports it
        can manage this week, recorded by the provider for planning — not a
        recommended frequency, not a prescribed frequency, not a clinical dosage.
        There is no default: the activity count is a direct function of this
        number, so defaulting it would author a clinical frequency nobody
        approved. An absent or non-integer value is refused.

        Everything else is derived server-side from the child's approved goals,
        so there is nothing else a client could supply.
        """
        from ..domain.roles import ActorRole
        from ..goals.errors import (
            GoalAuthorizationError,
            GoalConflict,
            GoalValidationError,
        )
        from ..integration.activity_bank import ActivityBankError
        from ..integration.week_one_release import WeekOneReleaseError
        from ..weekly.errors import (
            ReleasedPlanImmutable,
            WeeklyConflict,
            WeeklyValidationError,
        )

        principal, status = self._principal(environ)
        if principal is None:
            self._log(request_id, WEEK_ONE_RELEASE_ROUTE, "POST", status, None)
            return status, _ERROR_BODIES[status], WEEK_ONE_RELEASE_ROUTE

        if principal.role is not ActorRole.PROVIDER:
            self._log_principal(request_id, WEEK_ONE_RELEASE_ROUTE, "POST",
                                HTTP_FORBIDDEN, principal)
            return (HTTP_FORBIDDEN, _ERROR_BODIES[HTTP_FORBIDDEN],
                    WEEK_ONE_RELEASE_ROUTE)

        if self._activity_bank is None:
            # No reviewed activity content is configured. A capability gap, and
            # it fails closed: a week with no reviewed activities is not a plan.
            self._log_principal(request_id, WEEK_ONE_RELEASE_ROUTE, "POST",
                                HTTP_FORBIDDEN, principal)
            return (HTTP_FORBIDDEN, _ERROR_BODIES[HTTP_FORBIDDEN],
                    WEEK_ONE_RELEASE_ROUTE)

        # A malformed body renders as the SAME constant 403 as every other
        # refusal on this route. This transport declares no 400 body at all, and
        # the 0.5C no-oracle rule is why: a distinguishable shape error tells a
        # prober that the route exists and that they cleared the auth gates.
        from .body import TransportError, read_json_body

        try:
            body = read_json_body(environ, allowed=WEEK_ONE_RELEASE_FIELDS,
                                  required=WEEK_ONE_RELEASE_FIELDS)
        except TransportError:
            self._log_principal(request_id, WEEK_ONE_RELEASE_ROUTE, "POST",
                                HTTP_FORBIDDEN, principal)
            return (HTTP_FORBIDDEN, _ERROR_BODIES[HTTP_FORBIDDEN],
                    WEEK_ONE_RELEASE_ROUTE)
        capacity = body.get("family_declared_capacity")

        goals, plans, weekly, _rtm = self._services()
        try:
            goals._authorize(principal, child_id)
            # The EXISTING managing-clinician gate. Reused, not reimplemented.
            goals._require_managing_clinician(principal, child_id)
            outcome = self._week_one_service(goals, plans, weekly
                                            ).release_week_one(
                principal, child_id,
                family_declared_capacity=capacity,
                request_id=request_id)
        except (GoalAuthorizationError, GoalConflict, GoalValidationError,
                WeekOneReleaseError, ActivityBankError, ReleasedPlanImmutable,
                WeeklyConflict, WeeklyValidationError):
            # One constant 403, the 0.5C no-oracle rule. A finer status would
            # tell a prober which gate stopped them.
            self._log_principal(request_id, WEEK_ONE_RELEASE_ROUTE, "POST",
                                HTTP_FORBIDDEN, principal)
            return (HTTP_FORBIDDEN, _ERROR_BODIES[HTTP_FORBIDDEN],
                    WEEK_ONE_RELEASE_ROUTE)

        self._log_principal(request_id, WEEK_ONE_RELEASE_ROUTE, "POST",
                            HTTP_OK, principal)
        # `week` is the SAME structured projection the family reads, so a
        # clinician releasing a week sees exactly what was released. No capacity
        # ledger, no weights, no alignment rows, no rung refs.
        return HTTP_OK, {
            "child_id": child_id,
            "created": outcome.created,
            "cycle_id": outcome.cycle_id,
            "focus_plan_id": outcome.focus_plan_id,
            "goal_ids": list(outcome.goal_ids),
            "activity_count": outcome.activity_count,
            "released_at": (outcome.released_at.isoformat()
                            if outcome.released_at else None),
            "week": outcome.parent_week,
            "request_id": request_id,
        }, WEEK_ONE_RELEASE_ROUTE

    # -- logging ------------------------------------------------------------

    def _log(self, request_id: str, route_template: str, method: str,
             status: int, decision) -> None:
        """Emit one validated line. `format_log` raises on an unsafe field.

        `route_template` is always a template constant from this module, never
        the populated `PATH_INFO`, so a child id cannot reach a log line
        through the path. The query string is never logged at all.
        """
        self.log_sink.append(render(format_log(
            request_id=request_id,
            event="http_request",
            route=route_template,
            method=method,
            status=status,
            environment=self._settings.environment.value,
            denial_reason=(decision.denial.value
                           if decision is not None and decision.denial else None),
            actor_id=(decision.principal.application_id
                      if decision is not None and decision.principal else None),
            actor_role=(decision.principal.role.value
                        if decision is not None and decision.principal else None),
        )))

    def _log_principal(self, request_id: str, route_template: str, method: str,
                       status: int, principal) -> None:
        """One validated line for an identity route.

        The identity routes produce a `Principal`, not an `AccessDecision`, so
        they cannot reuse `_log`. Same guarantees hold: the route is a template
        constant, the fields pass through `format_log`, and the auth subject is
        not among them — only the opaque application id and the derived role.
        """
        self.log_sink.append(render(format_log(
            request_id=request_id,
            event="http_request",
            route=route_template,
            method=method,
            status=status,
            environment=self._settings.environment.value,
            actor_id=principal.application_id if principal is not None else None,
            actor_role=principal.role.value if principal is not None else None,
        )))


def build_application(*, settings, repos, verifier, recorder=None,
                      parent_source=None,
                      rung_source=None,
                      activity_bank=None,
                      log_sink: Optional[List[str]] = None) -> PilotWSGIApplication:
    """Composition root for the proof.

    Takes an already-built verifier rather than constructing one, so the
    transport layer has no say in how authentication is configured — that
    decision stays in `auth.build_verifier`, where the prod/dev rules live.
    """
    return PilotWSGIApplication(settings=settings, verifier=verifier, repos=repos,
                                recorder=recorder, parent_source=parent_source,
                                rung_source=rung_source,
                                activity_bank=activity_bank,
                                log_sink=log_sink)


def route_templates() -> Dict[str, bool]:
    """Diagnostics/tests: template -> is_public. Must agree with `apisurface`."""
    table = {template: public for _, template, public in ROUTE_TABLE}
    for template, public in table.items():
        # The two modules must not be able to disagree about what is public.
        assert public == is_public_route(template), template
    return table
