from fastapi.testclient import TestClient
from sitesense.main import app


def test_health() -> None:
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_planned_endpoint_is_explicit() -> None:
    response = TestClient(app).get("/api/parcel-search")
    assert response.status_code == 501
    assert "later handoff" in response.json()["detail"]
