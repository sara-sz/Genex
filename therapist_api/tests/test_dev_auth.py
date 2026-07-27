"""Dev-auth adapter safety: dev/test only, disabled by default, never prod."""

from __future__ import annotations

import pytest

from app.auth.dev_adapter import DevAuthVerifier, build_verifier
from app.auth.interface import AuthError, FailClosedAuthVerifier
from app.env_validation import EnvironmentValidationError, validate_settings
from app.settings import Settings
from tests.conftest import dev_env, prod_env


def test_dev_auth_enabled_only_in_dev_with_flag():
    v = build_verifier("dev", dev_auth_enabled=True)
    assert isinstance(v, DevAuthVerifier)
    assert v.verify("dev-hannah").uid == "dev-hannah"


def test_dev_auth_disabled_by_default():
    # flag off -> deny-all even in dev
    v = build_verifier("dev", dev_auth_enabled=False)
    assert isinstance(v, FailClosedAuthVerifier)
    with pytest.raises(AuthError):
        v.verify("dev-hannah")


def test_dev_auth_never_in_prod():
    v = build_verifier("prod", dev_auth_enabled=True)
    assert isinstance(v, FailClosedAuthVerifier)
    with pytest.raises(AuthError):
        v.verify("dev-hannah")


def test_dev_auth_unknown_token_denied():
    v = build_verifier("dev", dev_auth_enabled=True)
    with pytest.raises(AuthError):
        v.verify("not-a-real-token")
    with pytest.raises(AuthError):
        v.verify(None)


def test_env_validation_forbids_dev_auth_in_prod():
    s = Settings.from_env(prod_env(DEV_AUTH_ENABLED="true"))
    with pytest.raises(EnvironmentValidationError) as e:
        validate_settings(s)
    assert "dev-auth" in str(e.value)


def test_env_validation_allows_dev_auth_in_dev():
    validate_settings(Settings.from_env(dev_env(DEV_AUTH_ENABLED="true")))  # no raise
