from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.security import require_demo_api_key


def _protected_app() -> FastAPI:
    app = FastAPI()

    @app.get("/protected")
    def protected(user_id: Annotated[str, Depends(require_demo_api_key)]) -> dict[str, str]:
        return {"user_id": user_id}

    return app


def test_missing_bearer_token_is_rejected() -> None:
    response = TestClient(_protected_app()).get("/protected")

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing bearer token."


def test_invalid_bearer_token_is_rejected() -> None:
    response = TestClient(_protected_app()).get(
        "/protected",
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid bearer token."


def test_valid_bearer_token_is_accepted() -> None:
    response = TestClient(_protected_app()).get(
        "/protected",
        headers={"Authorization": "Bearer local-demo-key"},
    )

    assert response.status_code == 200
    assert response.json() == {"user_id": "demo_user"}
