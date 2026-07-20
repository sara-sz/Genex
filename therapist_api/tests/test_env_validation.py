"""Fail-closed environment validation."""

from __future__ import annotations

import pytest

from app.env_validation import EnvironmentValidationError, validate_settings
from app.main import create_app
from app.settings import Settings
from tests.conftest import dev_env, dev_settings, prod_env, prod_settings


def test_valid_dev_passes():
    validate_settings(dev_settings())  # no raise


def test_valid_prod_passes():
    validate_settings(prod_settings())  # no raise


def test_missing_environment_fails():
    s = Settings.from_env({k: v for k, v in dev_env().items() if k != "ENVIRONMENT"})
    with pytest.raises(EnvironmentValidationError) as e:
        validate_settings(s)
    assert "ENVIRONMENT is required" in str(e.value)


def test_project_environment_mismatch_fails():
    s = Settings.from_env(dev_env(GCP_PROJECT_ID="genex-provider-prod-2026"))
    with pytest.raises(EnvironmentValidationError) as e:
        validate_settings(s)
    assert "does not match the canonical project" in str(e.value)


def test_firebase_issuer_mismatch_fails():
    s = Settings.from_env(dev_env(FIREBASE_PROJECT_ID="genex-provider-prod-2026"))
    with pytest.raises(EnvironmentValidationError):
        validate_settings(s)


def test_prod_with_seed_enabled_fails():
    s = Settings.from_env(prod_env(SEED_ENABLED="true"))
    with pytest.raises(EnvironmentValidationError) as e:
        validate_settings(s)
    assert "must not enable seeding" in str(e.value)


def test_prod_with_debug_bypass_fails():
    s = Settings.from_env(prod_env(DEBUG_PANELS="true"))
    with pytest.raises(EnvironmentValidationError) as e:
        validate_settings(s)
    assert "debug bypass" in str(e.value)


def test_wildcard_cors_fails():
    s = Settings.from_env(dev_env(ALLOWED_ORIGINS="*"))
    with pytest.raises(EnvironmentValidationError) as e:
        validate_settings(s)
    assert "Wildcard CORS" in str(e.value)


def test_mixed_dev_prod_origins_fail():
    mixed = "https://genex-therapist-dev.lovable.app,https://genex-therapist-prod.lovable.app"
    s = Settings.from_env(dev_env(ALLOWED_ORIGINS=mixed))
    with pytest.raises(EnvironmentValidationError) as e:
        validate_settings(s)
    assert "must not be mixed" in str(e.value) or "must not allow the Prod" in str(e.value)


def test_prod_localhost_origin_fails():
    s = Settings.from_env(
        prod_env(ALLOWED_ORIGINS="https://genex-therapist-prod.lovable.app,http://localhost:5173")
    )
    with pytest.raises(EnvironmentValidationError) as e:
        validate_settings(s)
    assert "localhost" in str(e.value)


def test_create_app_refuses_invalid_env():
    s = Settings.from_env({k: v for k, v in dev_env().items() if k != "ENVIRONMENT"})
    with pytest.raises(EnvironmentValidationError):
        create_app(s)
