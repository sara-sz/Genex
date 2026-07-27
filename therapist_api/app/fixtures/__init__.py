"""Fictional read-only fixtures for the therapist alpha (dev/test only).

Everything here is invented — no real child, parent, therapist, diagnosis,
email, message, or connection. Emails use the reserved `.example` domain.
"""

from .loader import load_fixtures

__all__ = ["load_fixtures"]
