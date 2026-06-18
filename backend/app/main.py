"""FastAPI application factory.

Wires the request-id middleware, the public + admin routers, and the
uniform error envelope. Every 4xx/5xx response flows through
:mod:`app.core.errors` so the client can parse errors with one shape.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.answer.orchestrator import OrchestratorError
from app.api.routes.health import router as health_router
from app.api.routes.messages import router as messages_router
from app.api.routes.sessions import router as sessions_router
from app.core.errors import (
    APIErrorCode,
    ErrorDetail,
    ErrorEnvelope,
    status_code_for,
)
from app.core.logging import configure_logging
from app.core.middleware import RequestIDMiddleware, get_current_request_id


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title="CiteVyn AI Backend",
        version="0.1.0",
        description=(
            "Slice 7 backend: HTTP routes for sessions and messages, "
            "wired to the Slice 4–6 answer engine."
        ),
    )
    app.add_middleware(RequestIDMiddleware)
    app.include_router(health_router)
    app.include_router(sessions_router)
    app.include_router(messages_router)
    _register_exception_handlers(app)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    """Register uniform error handlers for transport-level failures.

    The handlers always emit the standard envelope at the top level
    so clients can parse one shape regardless of which layer raised
    the failure.
    """

    @app.exception_handler(OrchestratorError)
    async def _orchestrator_error_handler(request: Request, exc: OrchestratorError) -> JSONResponse:
        """Map an orchestrator failure to a 500 with the standard envelope.

        The cause string is preserved in ``error.details.reason`` so an
        SRE can correlate the HTTP response with the underlying log
        line without the client having to parse a stack trace.
        """
        request_id = str(getattr(request.state, "request_id", "") or get_current_request_id() or "")
        envelope = ErrorEnvelope(
            request_id=request_id,
            error=ErrorDetail(
                code=APIErrorCode.internal_error,
                message=(
                    "The answer engine is currently unavailable. Please retry in a few seconds."
                ),
                details={"reason": str(exc)},
            ),
        )
        return JSONResponse(
            status_code=status_code_for(APIErrorCode.internal_error),
            content=envelope.model_dump(mode="json"),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Map a FastAPI request-validation error to 422 with the envelope.

        FastAPI's default 422 shape is not the same as the standard
        envelope; we re-shape it so the client only needs one parser.
        """
        request_id = str(getattr(request.state, "request_id", "") or get_current_request_id() or "")
        envelope = ErrorEnvelope(
            request_id=request_id,
            error=ErrorDetail(
                code=APIErrorCode.validation_error,
                message="Request body or parameters failed validation.",
                details={"errors": exc.errors()},
            ),
        )
        return JSONResponse(
            status_code=status_code_for(APIErrorCode.validation_error),
            content=envelope.model_dump(mode="json"),
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Last-resort handler so the envelope is always uniform.

        Logs the traceback via the standard logger and returns a 500
        with the standard envelope. Without this handler FastAPI
        would emit its own ``Internal Server Error`` HTML body.
        """
        request_id = str(getattr(request.state, "request_id", "") or get_current_request_id() or "")
        import logging

        logging.getLogger("citevyn.request").exception(
            "unhandled_exception",
            extra={"request_id": request_id, "path": request.url.path},
        )
        envelope = ErrorEnvelope(
            request_id=request_id,
            error=ErrorDetail(
                code=APIErrorCode.internal_error,
                message="An unexpected error occurred.",
            ),
        )
        return JSONResponse(
            status_code=status_code_for(APIErrorCode.internal_error),
            content=envelope.model_dump(mode="json"),
        )


app = create_app()
