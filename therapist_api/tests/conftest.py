"""Shared test helpers."""

from __future__ import annotations

from typing import Dict

from app.settings import Settings


def dev_env(**overrides: str) -> Dict[str, str]:
    env = {
        "ENVIRONMENT": "dev",
        "GCP_PROJECT_ID": "genex-provider-dev-2026",
        "FIREBASE_PROJECT_ID": "genex-provider-dev-2026",
        "FIRESTORE_PROJECT_ID": "genex-provider-dev-2026",
        "REGION": "us-central1",
        "ALLOWED_ORIGINS": "https://genex-therapist-dev.lovable.app,http://localhost:5173",
        "REGISTRATION_POLICY": "open-dev",
        "SEED_ENABLED": "true",
        "DEBUG_PANELS": "true",
    }
    env.update(overrides)
    return env


def prod_env(**overrides: str) -> Dict[str, str]:
    env = {
        "ENVIRONMENT": "prod",
        "GCP_PROJECT_ID": "genex-provider-prod-2026",
        "FIREBASE_PROJECT_ID": "genex-provider-prod-2026",
        "FIRESTORE_PROJECT_ID": "genex-provider-prod-2026",
        "REGION": "us-central1",
        "ALLOWED_ORIGINS": "https://genex-therapist-prod.lovable.app",
        "REGISTRATION_POLICY": "invite-only",
        "SEED_ENABLED": "false",
        "DEBUG_PANELS": "false",
    }
    env.update(overrides)
    return env


def dev_settings(**overrides: str) -> Settings:
    return Settings.from_env(dev_env(**overrides))


def prod_settings(**overrides: str) -> Settings:
    return Settings.from_env(prod_env(**overrides))


def read_slice_client(**overrides: str):
    """A TestClient for the dev app with the fictional dev-auth adapter enabled."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    env = dev_env(DEV_AUTH_ENABLED="true", **overrides)
    return TestClient(create_app(Settings.from_env(env)))


# Fictional dev-auth bearer tokens (dev/test only).
HANNAH = {"Authorization": "Bearer dev-hannah"}
ELENA = {"Authorization": "Bearer dev-elena"}
UNCONNECTED = {"Authorization": "Bearer dev-unconnected-therapist"}
