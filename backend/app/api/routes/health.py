from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


def _request_id(request: Request) -> str:
    return str(request.state.request_id)


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    return {
        "request_id": _request_id(request),
        "status": "healthy",
        "service": "citevyn-ai-backend",
    }


@router.get("/health/dependencies")
def health_dependencies(request: Request) -> dict[str, Any]:
    return {
        "request_id": _request_id(request),
        "status": "healthy",
        "dependencies": {},
        "message": "No external dependencies are configured in Slice 1.",
    }


@router.get("/health/index")
def health_index(request: Request) -> dict[str, Any]:
    return {
        "request_id": _request_id(request),
        "status": "pre_index",
        "active_index": None,
        "previous_good_index": None,
        "message": "No active index exists before Slice 2 persistence and indexing.",
    }
