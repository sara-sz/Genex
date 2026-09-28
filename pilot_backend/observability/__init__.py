"""pilot_backend.observability — allowlisted, PHI-free structured logging."""

from .safe_logging import (
    ALLOWED_LOG_FIELDS,
    LogFieldError,
    SafeLogRecord,
    describe_exception,
    format_log,
    redact_bearer,
)

__all__ = [
    "ALLOWED_LOG_FIELDS",
    "LogFieldError",
    "SafeLogRecord",
    "format_log",
    "describe_exception",
    "redact_bearer",
]
