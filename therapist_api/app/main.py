"""FastAPI application for the therapist service (alpha foundation).

Only two routes exist in this phase:
  * GET /health              — liveness; no secrets
  * GET /api/v1/app/config   — safe, non-secret client config

The app is built via a factory that runs fail-closed environment validation. If
validation fails, the app is NOT created and the process cannot serve traffic.

No real Firebase verification, no real Firestore, no recommendation endpoints,
no plan mutation, no parent-service or parent-GCS access, no genex_core import.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .constants import API_VERSION, DOMAIN_TAXONOMY_VERSION, SERVICE_NAME
from .domain.domains import DISPLAY_DOMAINS
from .domain.provenance import ALLOWED_PROGRESS_LABELS
from .domain.recommendation_state import RecommendationState
from .env_validation import validate_settings
from .middleware import RequestContextMiddleware
from .settings import Settings


def create_app(settings: Settings) -> FastAPI:
    """Build the app for a validated Settings. Raises if the environment is unsafe."""
    validate_settings(settings)  # fail-closed: no app without a coherent environment

    app = FastAPI(
        title="Genex Therapist API",
        version=__version__,
        docs_url=None if settings.is_prod else "/docs",
        redoc_url=None,
    )

    app.add_middleware(RequestContextMiddleware, environment=settings.environment)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "service": SERVICE_NAME,
            "environment": settings.environment,
            "version": __version__,
            "time": datetime.now(timezone.utc).isoformat(),
        }

    @app.get(f"/api/{API_VERSION}/app/config")
    async def app_config() -> dict:
        # Safe, non-secret client config only. No project ids, no secrets, no flags
        # that would leak infra posture.
        return {
            "environment": settings.environment,
            "api_version": API_VERSION,
            "registration_policy": settings.registration_policy,
            "domain_taxonomy_version": DOMAIN_TAXONOMY_VERSION,
            "display_domains": list(DISPLAY_DOMAINS),
            "progress_labels": list(ALLOWED_PROGRESS_LABELS),
            "recommendation_states": [s.value for s in RecommendationState],
        }

    return app


def load_validated_settings() -> Settings:
    return Settings.from_env(os.environ)


def create_asgi_app() -> FastAPI:
    """Factory for uvicorn (`uvicorn app.main:create_asgi_app --factory`)."""
    return create_app(load_validated_settings())
