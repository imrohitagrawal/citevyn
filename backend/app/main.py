from fastapi import FastAPI

from app.api.routes.health import router as health_router
from app.core.logging import configure_logging
from app.core.middleware import RequestIDMiddleware


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title="CiteVyn AI Backend",
        version="0.1.0",
        description="Slice 1 backend foundation for CiteVyn AI.",
    )
    app.add_middleware(RequestIDMiddleware)
    app.include_router(health_router)
    return app


app = create_app()
