"""pilot_runtime/google_oidc.py — verify a Google service identity token.

## WHY THIS IS NOT UNDER `pilot_runtime/auth/`

`auth/` is the intuitive home, and that is exactly the problem:
`pilot_runtime/auth/__init__.py` imports `firebase_decoder`, which imports
`firebase_admin` at module level. Importing anything from that package
therefore pulls the Firebase SDK into the importer's graph — and the
projection image deliberately does not install it, because no end-user
authentication exists on that service.

So this module sits one level up, where `pilot_runtime/__init__.py` imports
nothing. The placement is a dependency boundary, not an organisational
preference.

Defence in depth for the A2 projection boundary. **Cloud Run IAM is the
authoritative caller gate**: the projection service is deployed with no
`allUsers` invoker and a single `roles/run.invoker` binding, so a request from
any other identity is rejected by the platform before it reaches this code.

This module exists because that platform check, while authoritative, is
invisible to the application and untestable from inside it. Re-verifying in the
app means the service also refuses a wrong caller when run locally, in CI, or
behind any future ingress change — and it makes the expected caller an
assertion a reviewer can read.

## WHY APP-LEVEL VERIFICATION IS CLEANLY POSSIBLE HERE

For a Cloud Run service that requires authentication, the platform validates
the ID token and then FORWARDS the original `Authorization: Bearer <id-token>`
header to the container. So the same token is available to verify again. This
is the supported pattern; it needs no custom header and no second credential.

Cloud Run additionally copies the value to `X-Serverless-Authorization` when a
service wants `Authorization` for its own end-user credential. The projection
service has no end-user credential at all, so it reads `Authorization` and
treats `X-Serverless-Authorization` as a fallback only.

Nothing here weakens the platform check. Verification is strictly additional:
a token that fails IAM never arrives, and a token that arrives must also pass
here.

## WHAT IS VERIFIED

  signature   against Google's published certificates, by `google-auth`
  audience    EXACT match against the configured audience, by `google-auth`
  expiry      by `google-auth` (`exp`, with its own small clock skew)
  issuer      must be Google's, checked here
  caller      `email` must EXACTLY equal the expected service account, and
              `email_verified` must be true, checked here

The caller check is an exact string comparison against one configured address.
No suffix matching, no project-prefix matching: `@genex-mvp-2026...` would
match any service account in the Parent project, including the default compute
account that other workloads share.

## NO SECRETS

There is no shared secret, no API key and no service-account key file
anywhere in this path. The caller mints its token from the Cloud Run metadata
server; this side verifies it against Google's public certificates.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

#: The only issuers a Google-signed service identity token may carry.
GOOGLE_ISSUERS = frozenset({
    "https://accounts.google.com",
    "accounts.google.com",
})


class ServiceIdentityError(Exception):
    """The caller is not the expected Parent service. PHI-safe message.

    One error for every failure mode — bad signature, wrong audience, wrong
    caller, expired — so a prober cannot learn WHICH check it failed. The
    operator-facing distinction lives in the exception chain, not the message.
    """

    PHI_SAFE_MESSAGE = True


class GoogleServiceIdentityVerifier:
    """Verify an audience-bound Google ID token from one expected caller.

    Both the audience and the expected caller are REQUIRED at construction.
    There is no default and no "unset means allow": an unconfigured verifier
    cannot be built, so a deployment that forgot to set them fails at startup
    rather than accepting anyone.
    """

    def __init__(self, *, audience: str, expected_service_account: str,
                 verifier: Any = None, transport: Any = None) -> None:
        aud = (audience or "").strip()
        caller = (expected_service_account or "").strip().lower()
        if not aud:
            raise ServiceIdentityError("a projection audience is required")
        if not caller:
            raise ServiceIdentityError(
                "an expected caller service account is required")
        if "@" not in caller:
            raise ServiceIdentityError(
                "the expected caller must be a service-account email")
        self._audience = aud
        self._expected = caller
        #: Injectable purely so the claim checks below can be tested without
        #: reaching Google. Production passes neither.
        self._verifier = verifier
        self._transport = transport

    # -- internals --------------------------------------------------------

    def _verify_signature_and_audience(self, token: str) -> Mapping[str, Any]:
        """Delegate signature, audience and expiry to `google-auth`.

        Not reimplemented here. Verifying a JWT by hand is exactly the kind of
        code that looks right and is subtly wrong, and `google-auth` is
        already a dependency of both the Firestore and Firebase SDKs this
        service uses.
        """
        if self._verifier is not None:
            return self._verifier(token, self._audience)

        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        transport = self._transport or google_requests.Request()
        return id_token.verify_oauth2_token(token, transport,
                                            audience=self._audience)

    # -- the port ---------------------------------------------------------

    def verify(self, authorization_header: Optional[str]) -> Mapping[str, Any]:
        """The verified claims, or `ServiceIdentityError`.

        Takes the raw header so the caller cannot accidentally pass a token it
        parsed with different rules than this one.
        """
        header = (authorization_header or "").strip()
        if not header:
            raise ServiceIdentityError("a service identity token is required")
        scheme, _, raw = header.partition(" ")
        if scheme.lower() != "bearer" or not raw.strip():
            raise ServiceIdentityError("a bearer service identity is required")

        try:
            claims = self._verify_signature_and_audience(raw.strip())
        except ServiceIdentityError:
            raise
        except Exception as exc:
            # Includes an invalid signature, a wrong audience and an expired
            # token. Collapsed to one refusal on purpose; the cause is kept in
            # the chain for a local debugger, never in the message.
            raise ServiceIdentityError("service identity is not valid") from exc

        if not isinstance(claims, Mapping):
            raise ServiceIdentityError("service identity is not valid")

        issuer = str(claims.get("iss", "")).strip()
        if issuer not in GOOGLE_ISSUERS:
            raise ServiceIdentityError("service identity is not valid")

        # A Firebase end-user token would fail the issuer check above
        # (`https://securetoken.google.com/<project>`), so a caregiver or
        # provider credential can never reach the projection path. This is the
        # same conclusion the Pilot's browser API reaches from the other side:
        # a service account matches no caregiver or provider record, so
        # `resolve_principal` refuses it.
        if claims.get("email_verified") is not True:
            raise ServiceIdentityError("service identity is not valid")

        email = str(claims.get("email", "")).strip().lower()
        if not email or email != self._expected:
            raise ServiceIdentityError("service identity is not valid")

        # `aud` is re-read rather than assumed: `verify_oauth2_token` already
        # enforced it, and a mutation that dropped the audience argument would
        # otherwise leave no failing assertion.
        if str(claims.get("aud", "")).strip() != self._audience:
            raise ServiceIdentityError("service identity is not valid")

        return claims
