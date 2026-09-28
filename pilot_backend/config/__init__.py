"""pilot_backend.config — explicit, environment-aware, fail-closed settings."""

from .errors import ConfigError
from .settings import (
    Environment,
    PilotSettings,
    FORBIDDEN_LEGACY_RESOURCES,
    REQUIRED_PROD_KEYS,
    DEV_RESOURCE_MARKERS,
)

__all__ = [
    "ConfigError",
    "Environment",
    "PilotSettings",
    "FORBIDDEN_LEGACY_RESOURCES",
    "REQUIRED_PROD_KEYS",
    "DEV_RESOURCE_MARKERS",
]
