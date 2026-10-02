"""HTTP edge concerns for a DEPLOYED pilot service.

Nothing here is product logic. The frozen `pilot_backend.transport` decides
what a route does and who may call it; this package only carries the things a
browser needs from an origin server and a WSGI container needs from a process.
"""

from .cors import CorsMiddleware, PREFLIGHT_MAX_AGE_SECONDS

__all__ = ["CorsMiddleware", "PREFLIGHT_MAX_AGE_SECONDS"]
