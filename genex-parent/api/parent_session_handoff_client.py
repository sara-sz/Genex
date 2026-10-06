"""api/parent_session_handoff_client.py — Parent mints the handoff capability.

Phase A of the 0.5F-A3 handoff. Parent proves the authenticated caregiver owns a
Parent session, mints a one-time capability token, registers only its DIGEST with
the Pilot server-to-server, and returns the raw token to that browser alone.

## WHY THIS FILE IS NOT NAMED "...claim_client"

`tests/test_parent_24_integrity.py` bans a module whose filename contains
"claim" anywhere in the Parent tree, alongside "rtm", "cpt", "billing",
"payer" and "attestation". That guard is right: in the Parent/RTM vocabulary a
CLAIM is a billing claim, and the Parent tree must stay free of reimbursement
functionality.

The capability here is an identity handoff and has nothing to do with billing,
but the guard cannot know that from a filename — and widening it to admit
"claim" would defeat it for the case it exists to catch. So this module is named
for what it does: a session HANDOFF. The Pilot side keeps "claim" naming, where
`identity_claims.py` and `ClaimKind` already make the word mean a write-time
uniqueness claim.

## WHY PARENT REGISTERS, RATHER THAN PILOT ASKING

The alternative was for the Pilot to call Parent at redemption time to redeem the
token. That was rejected for two reasons:

  * it would make canonical Pilot child creation depend on Parent being
    reachable at that moment, so a Parent outage would block Pilot identity;
  * it would create a Pilot -> Parent service dependency, inverting the
    one-way Parent -> Pilot direction every other boundary maintains.

Parent pushing the pending claim keeps the arrow pointing one way. Nothing in
the Pilot ever calls Parent.

## THE RAW TOKEN GOES EXACTLY ONE PLACE

It is returned to the authenticated Parent browser and nowhere else:

    NOT persisted  - no Parent session field, no database, no file. Parent keeps
                     no copy, so Parent cannot replay it either.
    NOT sent to the Pilot - only `sha256(domain || token)` crosses, so a
                     compromise of Pilot storage or Pilot logs yields nothing
                     redeemable.
    NOT logged     - this module constructs no logger and calls no print, and a
                     test asserts that over its AST.

That is the whole point of hashing: the Pilot can RECOGNISE the capability when
it is presented, and can never PRODUCE one.

## NO CREDENTIAL OF ITS OWN

The service-to-service call carries an audience-bound, Google-signed ID token
minted by the Cloud Run metadata server for this service's runtime identity. No
key file, no shared secret, no API key, and nothing a browser could hold. Same
mechanism the A2 projection client uses, and the A2 staging activation proved it
end to end.

## WHAT IS SENT

`claim_digest`, `source_session_id`, and `ttl_seconds`. Three fields.

No Parent uid, no caregiver identifier, no `external_owner_ref`, no child name,
no age, and no clinical content of any kind — this is identity bootstrap, and a
baseline has no business in it. The Pilot's registration validator refuses every
one of those names explicitly.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, Mapping, Optional, Tuple

#: The endpoint on the Pilot bootstrap service. Identifier-free by design: the
#: digest and session id travel in the body, because Cloud Run logs the path.
BOOTSTRAP_PATH = "/internal/parent-session-claims"

#: Required environment variables. NONE has a default — absence means this
#: deployment cannot mint a capability, which is the correct state for any
#: deployment not deliberately configured for it.
URL_ENV_VAR = "PILOT_BOOTSTRAP_URL"
AUDIENCE_ENV_VAR = "PILOT_BOOTSTRAP_AUDIENCE"
PAIRING_ENV_VAR = "GENEX_PILOT_PROJECTION_PAIRING"

#: The ONLY permitted environment pairing, shared with the A2 projection client
#: on purpose: one variable governs whether THIS Parent deployment may talk to
#: THAT Pilot deployment at all, so the two boundaries cannot drift into
#: disagreeing about which pairing is live.
PERMITTED_PAIRINGS: Tuple[str, ...] = ("parent-staging->pilot-staging",)

#: Domain separation for the digest. MUST equal
#: `pilot_backend.domain.parent_session_claim.CLAIM_DIGEST_DOMAIN`; a
#: cross-system test pins that both sides agree byte for byte, because a
#: mismatch would make every token unredeemable.
CLAIM_DIGEST_DOMAIN = "genex-parent-session-claim-v1"

#: Bytes of entropy in a raw token. 32 bytes = 256 bits.
CLAIM_TOKEN_BYTES = 32

#: The handoff window, in seconds. Mirrors the Pilot's compiled-in ceiling; the
#: Pilot enforces it independently and refuses anything longer, so this value
#: can only ever be at or below the real limit.
CLAIM_TTL_SECONDS = 600

DEFAULT_TIMEOUT_SECONDS = 10


class ClaimNotConfigured(Exception):
    """This Parent deployment is not configured to mint claims. Fail closed."""


class ClaimPairingForbidden(Exception):
    """The declared environment pairing is not permitted."""


class ClaimUnavailable(Exception):
    """The Pilot could not be reached or refused transiently. RETRYABLE.

    A retry mints a NEW token rather than re-registering the old one, which is
    correct: nothing was handed to the browser, so the unregistered token is
    simply discarded and can never be redeemed.
    """


class ClaimRejected(Exception):
    """The Pilot refused this registration on its merits. NOT retryable."""


def generate_claim_token() -> str:
    """A fresh opaque capability token from the OS CSPRNG.

    `secrets.token_urlsafe` draws from `os.urandom`. Deliberately not `random`,
    which is a Mersenne Twister and reconstructible from its output — a mistake
    that would make every outstanding capability predictable.
    """
    import secrets

    return secrets.token_urlsafe(CLAIM_TOKEN_BYTES)


def claim_digest(token: str) -> str:
    """The digest the Pilot stores. Must match the Pilot's own function.

    NUL-separated after a domain tag, the same discipline the rest of the
    system uses: the separator cannot occur in a token, so no other digest can
    collide with one from this namespace.
    """
    raw = (token or "").strip()
    if len(raw) < 32:
        raise ClaimRejected("a claim token is required")
    joined = f"{CLAIM_DIGEST_DOMAIN}\x00{raw}".encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


def claim_config(env: Optional[Mapping[str, str]] = None) -> Tuple[str, str]:
    """`(url, audience)`, or a refusal. The pairing guard lives here.

    Checked in this order on purpose: pairing first, so a deployment that has
    not declared a permitted pairing is refused before its URL is even read.
    """
    source = env if env is not None else os.environ

    pairing = (source.get(PAIRING_ENV_VAR) or "").strip()
    if not pairing:
        raise ClaimNotConfigured(
            "no projection pairing is declared for this deployment")
    if pairing not in PERMITTED_PAIRINGS:
        raise ClaimPairingForbidden(
            "the declared projection pairing is not permitted")

    url = (source.get(URL_ENV_VAR) or "").strip()
    audience = (source.get(AUDIENCE_ENV_VAR) or "").strip()
    if not url or not audience:
        raise ClaimNotConfigured("no bootstrap target is configured")
    if not url.startswith("https://"):
        # A plaintext target would put a capability digest on the wire. Refused
        # rather than upgraded, because silently rewriting a configured URL
        # hides a misconfiguration instead of surfacing it.
        raise ClaimNotConfigured("the bootstrap target must be https")
    if not url.endswith(BOOTSTRAP_PATH):
        raise ClaimNotConfigured(
            "the bootstrap target is not the bootstrap endpoint")
    if not audience.startswith("https://") or audience.endswith("/"):
        raise ClaimNotConfigured("the bootstrap audience is malformed")
    if not url.startswith(audience + "/"):
        # The audience must be the service the URL actually addresses, or a
        # token minted for one service could be posted to another.
        raise ClaimNotConfigured(
            "the bootstrap audience does not match its target")
    return url, audience


def _identity_token(audience: str, *, fetcher: Any = None) -> str:
    """An audience-bound Google ID token for this service's own identity.

    From the Cloud Run metadata server. No key file is involved and none is
    possible: the metadata server mints only for the identity the service
    already runs as.
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
        # Includes "not running on Google infrastructure", the normal local case.
        #
        # The INJECTED fetcher is wrapped too, deliberately: a test seam whose
        # failures propagate differently from the real path would make every
        # error test reassuring and wrong.
        raise ClaimUnavailable(
            "a service identity token is unavailable") from exc
    if not token:
        raise ClaimUnavailable("a service identity token is unavailable")
    return token


def mint_session_claim(*, source_session_id: str,
                       env: Optional[Mapping[str, str]] = None,
                       fetcher: Any = None, poster: Any = None,
                       token_factory: Any = None,
                       ttl_seconds: int = CLAIM_TTL_SECONDS,
                       timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Mint one capability and register its digest. Returns the RAW token.

    The caller is responsible for having proven that the authenticated caregiver
    owns `source_session_id` — this module performs no authorization and must
    not appear to. In `api/main.py` that proof is `_require_session`, which
    404s on a missing session and 403s on one owned by somebody else.

    NOT retry-safe in the idempotent sense, and deliberately so: a retry mints
    a NEW token. The previous one was never returned to the browser, so it can
    never be redeemed, and re-registering a token Parent might have already
    handed out would be the dangerous choice.
    """
    url, audience = claim_config(env)
    raw_token = (token_factory or generate_claim_token)()
    digest = claim_digest(raw_token)
    service_token = _identity_token(audience, fetcher=fetcher)

    body = {
        "claim_digest": digest,
        "source_session_id": source_session_id,
        "ttl_seconds": int(ttl_seconds),
    }

    def _send():
        if poster is not None:
            return poster(url, body, service_token, timeout)

        import requests

        response = requests.post(
            url, json=body, timeout=timeout,
            headers={"Authorization": f"Bearer {service_token}",
                     "Content-Type": "application/json"})
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {}

    try:
        status, payload = _send()
    except Exception as exc:
        # No exception text is propagated. A `requests` error quotes the full
        # URL — which identifies the private bootstrap service — and sometimes
        # the request body, which carries the capability digest.
        raise ClaimUnavailable(
            "the bootstrap service is unavailable") from exc

    if status in (200, 201):
        # The raw token is returned to the CALLER (the authenticated browser)
        # and is deliberately absent from every other return path in this
        # module. It is not stored, not logged, and not echoed to the Pilot.
        return {
            "claim_token": raw_token,
            "expires_at": str(payload.get("expires_at") or ""),
            "created": bool(payload.get("created")),
        }
    if status in (400, 404, 409):
        raise ClaimRejected("the claim was not accepted")
    raise ClaimUnavailable("the bootstrap service is unavailable")
