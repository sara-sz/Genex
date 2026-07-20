"""Health + safe config responses."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.constants import DISPLAY_DOMAINS
from app.main import create_app
from tests.conftest import dev_settings


def _client() -> TestClient:
    return TestClient(create_app(dev_settings()))


def test_health_ok():
    r = _client().get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "genex-api-therapist"
    assert body["environment"] == "dev"
    assert "X-Request-ID" in r.headers


def test_config_is_safe_and_complete():
    r = _client().get("/api/v1/app/config")
    assert r.status_code == 200
    body = r.json()
    # Exposes non-secret client config.
    assert body["environment"] == "dev"
    assert body["api_version"] == "v1"
    assert body["display_domains"] == list(DISPLAY_DOMAINS)
    assert "recommendation_states" in body
    # Must NOT leak infra/secrets.
    blob = r.text.lower()
    for leaked in ("project", "secret", "token", "bucket", "service_account", "digest"):
        assert leaked not in blob, f"config leaked '{leaked}'"


def test_request_id_is_echoed():
    r = _client().get("/health", headers={"X-Request-ID": "abc123"})
    assert r.headers["X-Request-ID"] == "abc123"
