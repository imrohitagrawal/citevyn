from fastapi.testclient import TestClient


def test_request_id_is_generated_when_missing(client: TestClient) -> None:
    response = client.get("/health")

    header_request_id = response.headers["X-Request-ID"]
    assert header_request_id.startswith("req_")
    assert response.json()["request_id"] == header_request_id


def test_request_id_header_is_preserved(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "req_test_123"})

    assert response.headers["X-Request-ID"] == "req_test_123"
    assert response.json()["request_id"] == "req_test_123"
