"""pilot_backend/config/errors.py — configuration failure type.

Separate module so every other package can raise/catch `ConfigError` without
importing the settings machinery (and without a circular import).
"""

from __future__ import annotations


class ConfigError(ValueError):
    """Required configuration is missing, malformed, or unsafe for the environment.

    Raised at construction time, not at first use. A service that cannot be
    configured safely must refuse to start rather than run degraded: a missing
    Firestore database name should be a failed deploy, not a silent fallback to
    a development database holding real patient records.

    This exception is PHI-safe by declaration (see `observability.safe_logging`):
    it is built only from configuration KEY names and environment names, never
    from secret values or clinical content.
    """

    PHI_SAFE_MESSAGE = True
