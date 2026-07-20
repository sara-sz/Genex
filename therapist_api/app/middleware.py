"""Request-id + structured-logging middleware."""

from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from .logging_config import configure_logging, log_request, set_request_id

_REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, times the request, and emits one structured log line."""

    def __init__(self, app, environment: str) -> None:
        super().__init__(app)
        self._environment = environment
        self._logger = configure_logging()

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(_REQUEST_ID_HEADER) or uuid.uuid4().hex
        set_request_id(request_id)

        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0

        response.headers[_REQUEST_ID_HEADER] = request_id
        log_request(
            self._logger,
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
            environment=self._environment,
        )
        return response
