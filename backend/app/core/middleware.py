import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings
from app.core.logging import build_log_event

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_current_request_id() -> str | None:
    return request_id_context.get()


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        settings = get_settings()
        request_id = request.headers.get(settings.request_id_header) or f"req_{uuid.uuid4().hex}"
        request.state.request_id = request_id
        token = request_id_context.set(request_id)
        started_at = time.perf_counter()

        try:
            response = await call_next(request)
        finally:
            request_id_context.reset(token)

        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        response.headers[settings.request_id_header] = request_id

        logging.getLogger("citevyn.request").info(
            build_log_event(
                "request_completed",
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                latency_ms=duration_ms,
            )
        )
        return response
