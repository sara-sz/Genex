"""pilot_runtime/auth/firebase_decoder.py — real ID-token verification.

Implements the BACKEND 0.2 `TokenDecoder` port over the official
`firebase_admin` SDK. `IdentityPlatformVerifier` already owns the policy —
revocation, verified-email, claim translation, fail-closed selection — so this
module does exactly one thing: call the SDK and hand back claims, or raise.

## Initialization is explicit and environment-aware

`firebase_admin.initialize_app()` with no arguments falls back to Application
Default Credentials, which resolve to whatever project the ambient environment
happens to point at — a developer's own project, or another service's. For an
authentication component that is not a convenience, it is a silent
cross-project trust relationship: tokens issued by the wrong project would
verify successfully.

So `initialize_firebase_app` requires an explicit project id, names the app
rather than mutating the default, and refuses to start when the id is missing.
Nothing here embeds a service-account key; production supplies credentials
ambiently through its runtime service account, which is a deployment contract.

## Every SDK exception is translated, and its text is discarded

`firebase_admin` raises a family of errors whose messages can contain the
token, the request, or provider detail. The mapping is:

    ExpiredIdTokenError  -> AuthError        (401)
    RevokedIdTokenError  -> RevokedTokenError(401, countable as revocation)
    UserDisabledError    -> RevokedTokenError(401 — a disabled account is a
                                              withdrawn credential, and must
                                              stop working immediately)
    InvalidIdTokenError  -> AuthError        (401)
    CertificateFetchError-> AuthError        (401 — fail closed; an inability
                                              to fetch signing keys must never
                                              be read as a valid token)
    anything else        -> AuthError        (401)

In every case the original exception is dropped with `from None` and only this
module's own wording is raised. That wording is what the HTTP layer and the
PHI-safe logger see.

## The caller is never trusted

Only the verified claims are returned, and only the fields the auth
abstraction needs. A uid, role, caregiver id, provider id or email appearing
in a request body, header or query string is not consulted here or anywhere
downstream.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials as firebase_credentials

from pilot_backend.auth.interface import AuthError, RevokedTokenError

#: Claims passed onward. Deliberately narrow: `IdentityPlatformVerifier` needs
#: the subject, issuer, audience, email state and validity window, and nothing
#: else. Custom claims are NOT forwarded — a role must be derived from an
#: application record, never asserted by a token.
_FORWARDED_CLAIMS = ("uid", "sub", "iss", "aud", "email", "email_verified", "iat", "exp")


class FirebaseInitError(Exception):
    """Firebase Admin could not be initialized safely.

    PHI-safe: names the configuration problem, never a credential.
    """

    PHI_SAFE_MESSAGE = True


def initialize_firebase_app(*, project_id: str, app_name: str = "pilot",
                            credential: Optional[Any] = None) -> "firebase_admin.App":
    """Initialize (or fetch) a NAMED Firebase app bound to one explicit project.

    A named app is used rather than the default so this service cannot collide
    with, or silently inherit, another component's initialization.
    """
    if not (project_id or "").strip():
        raise FirebaseInitError("Firebase initialization requires an explicit project id")

    try:
        return firebase_admin.get_app(app_name)
    except ValueError:
        pass  # not yet initialized — fall through and create it

    try:
        return firebase_admin.initialize_app(
            credential or firebase_credentials.ApplicationDefault(),
            {"projectId": project_id.strip()},
            name=app_name,
        )
    except Exception:
        # The SDK's message can include environment and credential detail.
        raise FirebaseInitError("Firebase Admin initialization failed") from None


class FirebaseTokenDecoder:
    """`TokenDecoder` over `firebase_admin.auth.verify_id_token`.

    Satisfies the port's contract: verify signature, issuer, audience and
    expiry; consult revocation when asked; raise on any failure; never return
    a partial result.
    """

    def __init__(self, app: "firebase_admin.App", *,
                 verify_module: Any = firebase_auth) -> None:
        if app is None:
            raise FirebaseInitError("FirebaseTokenDecoder requires an initialized app")
        self._app = app
        #: Injectable purely so the translation table below can be tested
        #: without a Firebase project. Production always uses the real module.
        self._auth = verify_module

    def __call__(self, token: str, *, check_revoked: bool) -> Mapping[str, object]:
        try:
            claims = self._auth.verify_id_token(
                token, app=self._app, check_revoked=check_revoked)
        except self._auth.RevokedIdTokenError:
            raise RevokedTokenError("token has been revoked") from None
        except self._auth.UserDisabledError:
            # A disabled account is a withdrawn credential. Reported as a
            # revocation so it is countable as one, and refused immediately
            # rather than at token expiry.
            raise RevokedTokenError("token has been revoked") from None
        except self._auth.ExpiredIdTokenError:
            raise AuthError("invalid token") from None
        except self._auth.InvalidIdTokenError:
            raise AuthError("invalid token") from None
        except self._auth.CertificateFetchError:
            # Fail closed. Being unable to fetch signing keys must never be
            # treated as a verified token.
            raise AuthError("invalid token") from None
        except Exception:
            raise AuthError("invalid token") from None

        if not isinstance(claims, Mapping):
            raise AuthError("invalid token")

        forwarded = {key: claims[key] for key in _FORWARDED_CLAIMS if key in claims}
        if not str(forwarded.get("uid") or forwarded.get("sub") or "").strip():
            raise AuthError("invalid token")
        return forwarded
