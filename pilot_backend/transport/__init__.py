"""pilot_backend.transport — minimal HTTP composition proof.

NOT the product API. This exists to prove, over a real HTTP boundary, that the
protected-request chain cannot be short-circuited. See `wsgi_app.py`.
"""

from .wsgi_app import (
    PROTECTED_CHILD_ROUTE,
    ROUTE_TABLE,
    PilotWSGIApplication,
    build_application,
)

__all__ = [
    "PilotWSGIApplication",
    "build_application",
    "ROUTE_TABLE",
    "PROTECTED_CHILD_ROUTE",
]
