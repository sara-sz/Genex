"""The real Firebase Admin adapter's translation behaviour.

This file tests `FirebaseTokenDecoder` — the actual production class — with a
stand-in for the `firebase_admin.auth` MODULE. That seam is deliberate and
narrow: what needs proving is the mapping from each SDK exception to this
system's safe failure model, and which claims are forwarded. Reaching a real
Identity Platform project would need credentials and would test Google's
verification rather than ours.

The initialization rules ARE tested against the real `firebase_admin` library.
"""

from __future__ import annotations

import pytest

from pilot_backend.auth.interface import AuthError, RevokedTokenError
from pilot_runtime.auth.firebase_decoder import (
    FirebaseInitError,
    FirebaseTokenDecoder,
    initialize_firebase_app,
)

from .test_sentinels import ALL_SENTINELS, SENTINEL_NOTE, SENTINEL_SECRET, SENTINEL_TOKEN


class FakeFirebaseAuth:
    """Stands in for `firebase_admin.auth`, with its real exception hierarchy."""

    class InvalidIdTokenError(Exception):
        pass

    class ExpiredIdTokenError(InvalidIdTokenError):
        pass

    class RevokedIdTokenError(InvalidIdTokenError):
        pass

    class UserDisabledError(InvalidIdTokenError):
        pass

    class CertificateFetchError(Exception):
        pass

    def __init__(self, *, claims=None, raises=None) -> None:
        self._claims = claims
        self._raises = raises
        self.calls = []

    def verify_id_token(self, token, app=None, check_revoked=False):
        self.calls.append({"token": token, "check_revoked": check_revoked, "app": app})
        if self._raises is not None:
            raise self._raises
        return dict(self._claims or {})


def decoder_for(**kwargs) -> tuple:
    fake = FakeFirebaseAuth(**kwargs)
    return FirebaseTokenDecoder(app=object(), verify_module=fake), fake


# ===========================================================================
# the exception translation table
# ===========================================================================

def test_revoked_token_becomes_revoked_token_error():
    fake_cls = FakeFirebaseAuth
    decoder, _ = decoder_for(raises=fake_cls.RevokedIdTokenError(SENTINEL_NOTE))
    with pytest.raises(RevokedTokenError):
        decoder("t", check_revoked=True)


def test_disabled_account_is_treated_as_a_withdrawn_credential():
    """A disabled user must stop working now, not at token expiry."""
    decoder, _ = decoder_for(raises=FakeFirebaseAuth.UserDisabledError("disabled"))
    with pytest.raises(RevokedTokenError):
        decoder("t", check_revoked=True)


@pytest.mark.parametrize("error", [
    FakeFirebaseAuth.ExpiredIdTokenError("expired"),
    FakeFirebaseAuth.InvalidIdTokenError("bad signature"),
    FakeFirebaseAuth.CertificateFetchError("could not fetch certs"),
    RuntimeError("something else entirely"),
    ValueError("malformed"),
])
def test_every_other_failure_is_a_plain_auth_error(error):
    decoder, _ = decoder_for(raises=error)
    with pytest.raises(AuthError) as raised:
        decoder("t", check_revoked=True)
    assert not isinstance(raised.value, RevokedTokenError)


def test_certificate_fetch_failure_fails_closed():
    """Being unable to fetch signing keys must never read as a valid token."""
    decoder, _ = decoder_for(raises=FakeFirebaseAuth.CertificateFetchError("network"))
    with pytest.raises(AuthError):
        decoder("t", check_revoked=True)


def test_sdk_exception_text_never_escapes():
    decoder, _ = decoder_for(raises=FakeFirebaseAuth.InvalidIdTokenError(
        f"token {SENTINEL_TOKEN} payload {SENTINEL_NOTE} key {SENTINEL_SECRET}"))
    with pytest.raises(AuthError) as raised:
        decoder("t", check_revoked=True)
    message = str(raised.value)
    for sentinel in ALL_SENTINELS:
        assert sentinel not in message
    assert raised.value.__cause__ is None, "the original exception must be dropped"


# ===========================================================================
# revocation is passed through, not assumed
# ===========================================================================

@pytest.mark.parametrize("check_revoked", [True, False])
def test_revocation_flag_reaches_the_sdk(check_revoked):
    decoder, fake = decoder_for(claims={"uid": "subject-1"})
    decoder("t", check_revoked=check_revoked)
    assert fake.calls[0]["check_revoked"] is check_revoked


def test_the_named_app_is_passed_to_the_sdk():
    """Verification must use THIS service's app, not the ambient default."""
    app = object()
    fake = FakeFirebaseAuth(claims={"uid": "subject-1"})
    FirebaseTokenDecoder(app=app, verify_module=fake)("t", check_revoked=True)
    assert fake.calls[0]["app"] is app


# ===========================================================================
# claim forwarding
# ===========================================================================

def test_only_the_needed_claims_are_forwarded():
    decoder, _ = decoder_for(claims={
        "uid": "subject-1", "iss": "https://securetoken.google.com/demo",
        "aud": "demo", "email": "fictional@example.invalid", "email_verified": True,
        "iat": 1, "exp": 2,
        # Everything below must be dropped.
        "role": "provider", "admin": True, "caregiver_id": "cgvr_forged",
        "firebase": {"sign_in_provider": "password"}, "custom_claims": {"x": 1},
    })
    claims = decoder("t", check_revoked=True)
    assert set(claims) == {"uid", "iss", "aud", "email", "email_verified", "iat", "exp"}


def test_a_custom_role_claim_cannot_reach_the_application():
    """Role is derived from an application record, never asserted by a token."""
    decoder, _ = decoder_for(claims={"uid": "subject-1", "role": "provider",
                                     "admin": True})
    claims = decoder("t", check_revoked=True)
    assert "role" not in claims and "admin" not in claims


def test_a_token_without_a_subject_is_refused():
    for claims in ({}, {"uid": ""}, {"uid": "   "}, {"email": "x@example.invalid"}):
        decoder, _ = decoder_for(claims=claims)
        with pytest.raises(AuthError):
            decoder("t", check_revoked=True)


def test_a_non_mapping_result_is_refused():
    class Weird(FakeFirebaseAuth):
        def verify_id_token(self, token, app=None, check_revoked=False):
            return ["not", "a", "mapping"]

    decoder = FirebaseTokenDecoder(app=object(), verify_module=Weird())
    with pytest.raises(AuthError):
        decoder("t", check_revoked=True)


def test_sub_is_accepted_when_uid_is_absent():
    decoder, _ = decoder_for(claims={"sub": "subject-1"})
    assert decoder("t", check_revoked=True)["sub"] == "subject-1"


# ===========================================================================
# initialization — against the REAL firebase_admin library
# ===========================================================================

def test_initialization_requires_an_explicit_project_id():
    """No silent Application Default fallback to whatever project is ambient."""
    for project in ("", "   ", None):
        with pytest.raises(FirebaseInitError):
            initialize_firebase_app(project_id=project)


def test_initialization_uses_a_named_app_bound_to_that_project():
    import firebase_admin
    from firebase_admin import credentials

    name = "pilot-test-app"
    app = initialize_firebase_app(
        project_id="demo-genex-pilot", app_name=name,
        credential=credentials.AnonymousCredentials()
        if hasattr(credentials, "AnonymousCredentials") else None)
    try:
        assert app.name == name, "must not mutate the default app"
        assert app.project_id == "demo-genex-pilot"
        # Re-initializing returns the same app rather than raising.
        assert initialize_firebase_app(project_id="demo-genex-pilot",
                                       app_name=name) is app
    finally:
        firebase_admin.delete_app(app)


def test_decoder_requires_an_initialized_app():
    with pytest.raises(FirebaseInitError):
        FirebaseTokenDecoder(app=None)


def test_the_adapter_satisfies_the_token_decoder_port():
    from pilot_backend.auth.interface import TokenDecoder

    decoder, _ = decoder_for(claims={"uid": "subject-1"})
    assert isinstance(decoder, TokenDecoder)
