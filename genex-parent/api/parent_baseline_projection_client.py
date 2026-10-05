"""api/parent_baseline_projection_client.py — Parent's outbound projection.

Sends ONE finalized functional baseline to the Pilot's internal projection
service, as a server-to-server call. Parent stays the system of record; this is
a one-way copy and nothing from the Pilot is ever written back into a Parent
session.

## THE PAIRING GUARD, AND AN HONEST LIMITATION

The requirement is that `Parent prod -> Pilot staging` be impossible by
CONFIGURATION rather than by convention. Two independent layers deliver that,
and only the second is authoritative:

  1. HERE — fail closed by ABSENCE. Three environment variables are required
     and none has a default. An unconfigured Parent service cannot project at
     all: no URL, no audience, no pairing token, no request. `PAIRING_TOKEN`
     must additionally equal one compiled-in permitted value, so a typo or a
     pointer at some other target is refused locally.

  2. ON THE PILOT SIDE — the authoritative gate. The projection service has no
     `allUsers` invoker and exactly one `roles/run.invoker` binding, to the
     Parent STAGING runtime service account. Parent prod runs as a different
     identity, so even a fully misconfigured prod deployment is rejected by
     Cloud Run before reaching the application, and rejected again by the
     application's expected-caller check.

LIMITATION, stated plainly rather than papered over: the Parent runtime has NO
environment concept. It exposes ten environment variables and not one of them
names an environment, and there is no `is_prod` anywhere in the code. So this
module CANNOT detect that it is running on production — it can only refuse
unless explicitly configured for the single permitted pairing. Inferring the
environment from the hostname or from the bucket name was considered and
rejected: both are the same class of guess.

That is why layer 2 matters. The guarantee rests on CALLER IDENTITY, which is
set by the Cloud Run service's runtime service account and cannot be changed by
an application environment variable.

## NO CREDENTIAL OF ITS OWN

The token is an audience-bound, Google-signed ID token minted by the Cloud Run
metadata server for this service's runtime identity. No key file, no shared
secret, no API key, and nothing a browser could ever hold. The browser asks
Parent to project; it never sees the token, the payload or the Pilot URL.

## WHAT IS SENT

`source_session_id`, `source_record_digest`, and exactly the seven allowlisted
baseline fields. The digest is computed over the FULL canonical finalized
record, so it attests the `asked` history without transmitting it.

## LOGGING

Nothing here logs. Not the payload, not the session id, not the digest, not
the response body, and not on failure. The module constructs no logger and
calls no print, and a test asserts that over its AST. A projection body is
clinical content; there is nothing in it worth the risk of recording.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Mapping, Optional, Tuple

#: The endpoint on the Pilot projection service. Identifier-free by design:
#: `source_session_id` travels in the body, because Cloud Run logs the path.
PROJECTION_PATH = "/internal/parent-baseline-projections"

#: Required environment variables. NONE has a default — absence means the
#: service cannot project, which is the correct state for any deployment that
#: has not been deliberately configured for it.
URL_ENV_VAR = "PILOT_PROJECTION_URL"
AUDIENCE_ENV_VAR = "PILOT_PROJECTION_AUDIENCE"
PAIRING_ENV_VAR = "GENEX_PILOT_PROJECTION_PAIRING"

#: The ONLY permitted environment pairing for 0.5F-A2. Compiled in, so the
#: deployment declares which pairing it is and the code decides whether that
#: pairing is allowed — rather than the deployment deciding both.
#:
#: Production will receive its own token and its own projection target after
#: PRE-PHI approval; until then there is no value of this variable that lets a
#: Parent service reach a production Pilot, because no such pairing exists here.
PERMITTED_PAIRINGS: Tuple[str, ...] = ("parent-staging->pilot-staging",)

#: Exactly the seven fields the Pilot accepts. Built by name, so a field added
#: to the Parent record cannot start travelling by accident.
PROJECTED_FIELDS: Tuple[str, ...] = (
    "domain", "area_id", "entry_choice_id", "routing_anchor_months",
    "not_demonstrated_months", "status", "baseline_version",
)

DEFAULT_TIMEOUT_SECONDS = 10


class ProjectionNotConfigured(Exception):
    """This Parent deployment is not configured to project. Fail closed."""


class ProjectionPairingForbidden(Exception):
    """The declared environment pairing is not permitted."""


class ProjectionUnavailable(Exception):
    """The Pilot could not be reached or refused. RETRYABLE.

    Distinct from the two above: those are "this deployment must not project",
    which no retry fixes. This one is "not right now", and because the Pilot
    endpoint is idempotent on (session, domain, digest) an identical retry is
    always safe.
    """


class ProjectionRejected(Exception):
    """The Pilot refused this projection on its merits. NOT retryable.

    A shape failure or an integrity conflict. Retrying an unchanged payload
    would get the same answer, so the caller is told to stop rather than loop.
    """


def canonical_source_digest(record: Mapping[str, Any]) -> str:
    """Digest of the finalized record, computed the way the Pilot computes it.

    Sorted keys remove dict-order dependence, compact separators remove
    whitespace drift, and `ensure_ascii=False` keeps the value stable whether
    or not a milestone contains a non-ASCII character. A cross-system test
    pins that both sides agree byte for byte — if they ever diverge, every
    projection becomes an integrity conflict, which is loud but useless.
    """
    canonical = json.dumps(record, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_projection(record: Mapping[str, Any]) -> Dict[str, Any]:
    """The seven-field payload, read by name from the finalized record.

    Never `dict(record)` minus keys: a subtractive copy sends any field added
    to the Parent record later, which is how clinical content leaks. This is
    additive, so a new Parent field stays home until someone adds it here.
    """
    missing = [f for f in PROJECTED_FIELDS if f not in record]
    if missing:
        raise ProjectionRejected("the finalized record is missing fields")
    return {field: record[field] for field in PROJECTED_FIELDS}


def projection_config(env: Optional[Mapping[str, str]] = None
                      ) -> Tuple[str, str]:
    """`(url, audience)`, or a refusal. The whole pairing guard lives here.

    Checked in this order on purpose: pairing first, so a deployment that has
    not declared a permitted pairing is refused before its URL is even read.
    """
    source = env if env is not None else os.environ

    pairing = (source.get(PAIRING_ENV_VAR) or "").strip()
    if not pairing:
        raise ProjectionNotConfigured(
            "no projection pairing is declared for this deployment")
    if pairing not in PERMITTED_PAIRINGS:
        raise ProjectionPairingForbidden(
            "the declared projection pairing is not permitted")

    url = (source.get(URL_ENV_VAR) or "").strip()
    audience = (source.get(AUDIENCE_ENV_VAR) or "").strip()
    if not url or not audience:
        raise ProjectionNotConfigured("no projection target is configured")
    if not url.startswith("https://"):
        # A plaintext target would put a clinical payload on the wire. Refused
        # rather than upgraded, because silently rewriting a configured URL
        # hides a misconfiguration instead of surfacing it.
        raise ProjectionNotConfigured("the projection target must be https")
    if not url.endswith(PROJECTION_PATH):
        raise ProjectionNotConfigured(
            "the projection target is not the projection endpoint")
    if not audience.startswith("https://") or audience.endswith("/"):
        raise ProjectionNotConfigured("the projection audience is malformed")
    if not url.startswith(audience + "/"):
        # The audience must be the service the URL actually addresses.
        # Otherwise a token minted for one service could be posted to another,
        # which is precisely the confusion audience binding exists to prevent.
        raise ProjectionNotConfigured(
            "the projection audience does not match its target")
    return url, audience


def _identity_token(audience: str, *, fetcher: Any = None) -> str:
    """An audience-bound Google ID token for this service's own identity.

    From the Cloud Run metadata server via `google.oauth2.id_token`. No key
    file is involved and none is possible: the metadata server will only mint
    a token for the identity the service already runs as.

    `google-auth` is declared directly in `requirements.api.txt` rather than
    relied on transitively, because this module imports it by name.
    """
    def _fetch() -> str:
        if fetcher is not None:
            return fetcher(audience)

        from google.auth.transport.requests import Request
        from google.oauth2 import id_token

        return id_token.fetch_id_token(Request(), audience)

    try:
        token = _fetch()
    except Exception as exc:
        # Includes "not running on Google infrastructure", which is the normal
        # local-development case. Retryable: the deployment may simply not be
        # on Cloud Run yet.
        #
        # The INJECTED fetcher is wrapped too, deliberately. A test seam whose
        # failures propagate differently from the real path would make every
        # error test reassuring and wrong.
        raise ProjectionUnavailable(
            "a service identity token is unavailable") from exc
    if not token:
        raise ProjectionUnavailable("a service identity token is unavailable")
    return token


def project_baseline(*, source_session_id: str, record: Mapping[str, Any],
                     env: Optional[Mapping[str, str]] = None,
                     fetcher: Any = None, poster: Any = None,
                     timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Project one finalized baseline. Returns the Pilot's derived ids.

    RETRY-SAFE: the Pilot keys a projection on
    (source_session_id, domain, source_record_digest), so an identical retry
    returns the existing projection and writes nothing. This function
    therefore needs no local idempotency state, and the caller may retry on
    `ProjectionUnavailable` without bookkeeping.

    The session id is a parameter rather than something read from a token,
    because the caller has already proved ownership of it — this module
    performs no authorization and must not appear to.
    """
    url, audience = projection_config(env)
    projection = build_projection(record)
    digest = canonical_source_digest(record)
    token = _identity_token(audience, fetcher=fetcher)

    body = {
        "source_session_id": source_session_id,
        "source_record_digest": digest,
        "projection": projection,
    }

    def _send():
        if poster is not None:
            return poster(url, body, token, timeout)

        import requests

        response = requests.post(
            url, json=body, timeout=timeout,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {}

    try:
        status, payload = _send()
    except Exception as exc:
        # No exception text is propagated. A `requests` error quotes the full
        # URL — which identifies the Pilot projection service — and sometimes
        # the request body, which is a clinical baseline.
        #
        # The INJECTED poster is wrapped too, for the same reason the fetcher
        # is: a test seam whose failures propagate differently from the real
        # path would make every error test reassuring and wrong.
        raise ProjectionUnavailable(
            "the projection service is unavailable") from exc

    if status in (200, 201):
        # Only the derived ids are returned. The Pilot sends back no baseline
        # values, so there is nothing clinical to hand on.
        return {
            "projection_id": str(payload.get("projection_id") or ""),
            "child_id": str(payload.get("child_id") or ""),
            "created": bool(payload.get("created")),
        }
    if status in (400, 404, 409):
        raise ProjectionRejected("the projection was not accepted")
    raise ProjectionUnavailable("the projection service is unavailable")
