"""pilot_backend.apisurface — the public/protected boundary and CORS policy."""

from .surface import (
    PUBLIC_ROUTES,
    CorsPolicy,
    RouteGuard,
    SurfaceError,
    cors_policy_for,
    health_payload,
    is_public_route,
)

__all__ = [
    "PUBLIC_ROUTES",
    "is_public_route",
    "health_payload",
    "CorsPolicy",
    "cors_policy_for",
    "RouteGuard",
    "SurfaceError",
]
