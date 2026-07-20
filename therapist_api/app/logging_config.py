"""Structured JSON logging with a per-request correlation id.

Logs are intentionally content-blind: we log request metadata (method, path,
status, duration, request id, environment) but never request bodies, tokens,
secrets, or any (fictional-for-now) child/parent/clinical content.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Optional

# Correlation id for the in-flight request; "-" when outside a request.
_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


def set_request_id(request_id: str) -> None:
    _request_id_ctx.set(request_id)


def get_request_id() -> str:
    return _request_id_ctx.get()


class JsonLogFormatter(logging.Formatter):
    """Render each log record as a single JSON line, with the request id."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "request_id": get_request_id(),
        }
        # Attach whitelisted structured extras only.
        for key in ("method", "path", "status", "duration_ms", "environment", "event"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure root logging once and return the service logger."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    return logging.getLogger("therapist_api")


def log_request(
    logger: logging.Logger,
    *,
    method: str,
    path: str,
    status: int,
    duration_ms: float,
    environment: Optional[str],
) -> None:
    logger.info(
        "request",
        extra={
            "event": "http_request",
            "method": method,
            "path": path,
            "status": status,
            "duration_ms": round(duration_ms, 2),
            "environment": environment,
        },
    )
