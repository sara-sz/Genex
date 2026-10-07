"""api/parent_baseline_projection_v2_client.py — Parent's outbound A2 v2 call.

Sends ONE finalized v2 functional baseline to the Pilot's internal projection
service, server to server. Parent stays the system of record; this is a one-way
copy and nothing from the Pilot is ever written back into a Parent session.

## A SEPARATE MODULE, AND A2 v1 IS UNTOUCHED

v1's client is not modified and not subclassed. The two differ in their path,
their body shape and their required configuration, so a shared module would mean
one function with two contracts and a branch deciding which product it was
speaking. The one thing that IS shared is `canonical_source_digest`, imported
rather than copied — see below.

## THE DIGEST IS IMPORTED, NOT REIMPLEMENTED

`canonical_source_digest` comes from the v1 client. Both sides of the A2
boundary must agree on it byte for byte or every projection becomes an integrity
conflict, and two copies of one hashing convention is exactly how that
divergence happens. Importing it means there is one implementation for both
generations; it is a pure function of its argument and reading it changes
nothing about v1.

Note WHAT is digested here, though: the full canonical `record_to_state_v2`
dict, which includes every per-skill evidence row. So changed evidence yields a
different digest, hence a different projection id, hence a new immutable
document — never a silent overwrite of an existing one.

## FAIL CLOSED BY ABSENCE, AND v2 MUST BE OPTED INTO SEPARATELY

Three variables are required and none has a default. `PILOT_PROJECTION_V2_URL`
is NEW and deliberately not derived from the v1 URL or from the audience: a
Parent deployment already configured for A2 v1 must not start projecting v2
merely because this code shipped. Enabling a new cross-system data flow is a
deployment decision, so absence means "this service cannot project v2 at all",
which is the correct state for any deployment nobody has deliberately
configured.

The pairing token and audience ARE shared with v1, because they describe the
same thing: which Pilot service this Parent is permitted to talk to. The v2
route lives on that same service, so inventing a second pairing vocabulary
would imply a second trust relationship that does not exist.

## CLOUD RUN IAM REMAINS THE AUTHORITATIVE GATE

The token is an audience-bound, Google-signed ID token minted by the metadata
server for this service's own runtime identity. No key file, no shared secret,
no API key, and nothing a browser could ever hold. The browser asks Parent to
project; it never sees the token, the payload, or the Pilot URL. The projection
service has no `allUsers` invoker, so a wrong caller is refused by Cloud Run
before a byte reaches Python.

## WHAT IS SENT, AND WHAT IS ONLY BORROWED

Sent: the session id, the record digest, the seven compatibility summary
fields, one row per assessed skill, and Parent's per-band declared-track totals.

BORROWED: `milestone` and `subdomain`. The Pilot needs them to resolve its own
canonical `rung_ref` — Parent cannot compute that ref, because the hash lives in
`pilot_backend` and the dependency runs one way only. They are consumed at the
boundary and never persisted on the Pilot side.

NEVER SENT: the raw caregiver answer, `asked`, `skill_key`, any question id, the
Parent or Firebase uid, the child's name, diagnosis, concerns, `qna`,
chronological age, or the entry-choice label. The payload is built ADDITIVELY by
name in `functional_baseline_v2_api.projection_payload`, so a field added to the
Parent record stays home until someone adds it there deliberately.

## LOGGING

Nothing here logs. Not the payload, not the session id, not the digest, not the
response, and not on failure. This module constructs no logger and calls no
print; a test asserts that over its AST. The body carries milestone prose, which
is clinical content — there is nothing in it worth the risk of recording.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional, Tuple

from api.parent_baseline_projection_client import (
    ProjectionNotConfigured,
    ProjectionPairingForbidden,
    ProjectionRejected,
    ProjectionUnavailable,
    canonical_source_digest,
)

#: The v2 endpoint on the Pilot projection service. Identifier-free by design:
#: `source_session_id` travels in the BODY, because Cloud Run logs the path and
#: this application cannot redact the platform's own request log.
PROJECTION_V2_PATH = "/internal/parent-baseline-projections-v2"

#: NEW and required. v2 projection is opted into explicitly; see the module
#: docstring on why it is neither defaulted nor derived.
URL_ENV_VAR = "PILOT_PROJECTION_V2_URL"

#: SHARED with v1 — the same Pilot service, the same trust relationship.
AUDIENCE_ENV_VAR = "PILOT_PROJECTION_AUDIENCE"
PAIRING_ENV_VAR = "GENEX_PILOT_PROJECTION_PAIRING"

#: The only permitted environment pairing. Compiled in, so the deployment
#: declares which pairing it is and the CODE decides whether that pairing is
#: allowed — rather than the deployment deciding both. Production receives its
#: own target and its own token after PRE-PHI approval; until then there is no
#: value of this variable that lets a Parent service reach a production Pilot,
#: because no such pairing exists here.
PERMITTED_PAIRINGS: Tuple[str, ...] = ("parent-staging->pilot-staging",)

#: Top-level body keys, exactly. The Pilot's codec refuses any other key set in
#: both directions, so this tuple and the Pilot's must agree.
BODY_KEYS: Tuple[str, ...] = (
    "source_session_id", "source_record_digest", "summary", "skills",
    "band_totals",
)

DEFAULT_TIMEOUT_SECONDS = 10


def projection_v2_config(env: Optional[Mapping[str, str]] = None
                         ) -> Tuple[str, str]:
    """`(url, audience)`, or a refusal. The whole pairing guard lives here.

    Checked pairing FIRST on purpose: a deployment that has not declared a
    permitted pairing is refused before its URL is even read, so a half-configured
    service cannot reveal which target it was pointed at by failing differently.
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
        raise ProjectionNotConfigured("no v2 projection target is configured")
    if not url.startswith("https://"):
        # A plaintext target would put milestone prose on the wire. Refused
        # rather than upgraded: silently rewriting a configured URL hides a
        # misconfiguration instead of surfacing it.
        raise ProjectionNotConfigured("the projection target must be https")
    if not url.endswith(PROJECTION_V2_PATH):
        # Also rules out a v1 URL: `/internal/parent-baseline-projections` does
        # not end with the v2 path, so a copy-paste of the v1 variable is
        # refused rather than posting a v2 body to the v1 route.
        raise ProjectionNotConfigured(
            "the projection target is not the v2 projection endpoint")
    if not audience.startswith("https://") or audience.endswith("/"):
        raise ProjectionNotConfigured("the projection audience is malformed")
    if not url.startswith(audience + "/"):
        # The audience must be the service the URL actually addresses, or a
        # token minted for one service could be posted to another — precisely
        # the confusion audience binding exists to prevent.
        raise ProjectionNotConfigured(
            "the projection audience does not match its target")
    return url, audience


def _identity_token(audience: str, *, fetcher: Any = None) -> str:
    """An audience-bound Google ID token for this service's own identity.

    From the Cloud Run metadata server. No key file is involved and none is
    possible: the metadata server will only mint a token for the identity the
    service already runs as.
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
        # Includes "not running on Google infrastructure", the normal local
        # case. Retryable: the deployment may simply not be on Cloud Run yet.
        #
        # The INJECTED fetcher is wrapped too, deliberately. A test seam whose
        # failures propagated differently from the real path would make every
        # error test reassuring and wrong.
        raise ProjectionUnavailable(
            "a service identity token is unavailable") from exc
    if not token:
        raise ProjectionUnavailable("a service identity token is unavailable")
    return token


def build_body(*, source_session_id: str, record: Mapping[str, Any],
               payload: Mapping[str, Any]) -> Dict[str, Any]:
    """The exact five-key request body.

    `record` is the full canonical stored v2 dict and is used ONLY to compute
    the digest — none of it is copied into the body. `payload` is what
    `functional_baseline_v2_api.projection_payload` already built additively by
    name, so this function adds provenance and nothing clinical.
    """
    missing = [k for k in ("summary", "skills", "band_totals")
               if k not in payload]
    if missing:
        raise ProjectionRejected("the projection payload is incomplete")
    return {
        "source_session_id": source_session_id,
        "source_record_digest": canonical_source_digest(record),
        "summary": dict(payload["summary"]),
        "skills": [dict(s) for s in payload["skills"]],
        "band_totals": [dict(b) for b in payload["band_totals"]],
    }


def project_baseline_v2(*, source_session_id: str, record: Mapping[str, Any],
                        payload: Mapping[str, Any],
                        env: Optional[Mapping[str, str]] = None,
                        fetcher: Any = None, poster: Any = None,
                        timeout: int = DEFAULT_TIMEOUT_SECONDS
                        ) -> Dict[str, Any]:
    """Project one finalized v2 baseline. Returns the Pilot's derived ids.

    RETRY-SAFE: the Pilot keys a v2 projection on
    (source_session_id, domain, source_record_digest), so an identical retry
    returns the existing projection and writes nothing. This function therefore
    holds no local idempotency state, and a caller may retry
    `ProjectionUnavailable` without bookkeeping.

    The session id is a PARAMETER rather than something read from a token,
    because the caller has already proved ownership of it — this module performs
    no authorization and must not appear to.
    """
    url, audience = projection_v2_config(env)
    body = build_body(source_session_id=source_session_id, record=record,
                      payload=payload)
    token = _identity_token(audience, fetcher=fetcher)

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
        status, response_payload = _send()
    except Exception as exc:
        # No exception text is propagated. A `requests` error quotes the full
        # URL — which identifies the Pilot projection service — and sometimes
        # the request body, which here carries milestone prose.
        #
        # The INJECTED poster is wrapped too, for the same reason the fetcher
        # is.
        raise ProjectionUnavailable(
            "the projection service is unavailable") from exc

    if status in (200, 201):
        # Only the derived ids. The Pilot returns no baseline values, so there
        # is nothing clinical to hand on to the caller or to the browser.
        return {
            "projection_id": str(response_payload.get("projection_id") or ""),
            "child_id": str(response_payload.get("child_id") or ""),
            "created": bool(response_payload.get("created")),
        }
    if status in (400, 404, 409, 413):
        raise ProjectionRejected("the projection was not accepted")
    raise ProjectionUnavailable("the projection service is unavailable")


__all__ = [
    "AUDIENCE_ENV_VAR",
    "BODY_KEYS",
    "PAIRING_ENV_VAR",
    "PERMITTED_PAIRINGS",
    "PROJECTION_V2_PATH",
    "URL_ENV_VAR",
    "ProjectionNotConfigured",
    "ProjectionPairingForbidden",
    "ProjectionRejected",
    "ProjectionUnavailable",
    "build_body",
    "canonical_source_digest",
    "project_baseline_v2",
    "projection_v2_config",
]
