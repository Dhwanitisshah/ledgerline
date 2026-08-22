from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    # Phase 7 adds `env` so a response tells you which deployment answered it --
    # worth having the moment there is more than one.
    assert response.json()["status"] == "ok"
    assert "env" in response.json()
