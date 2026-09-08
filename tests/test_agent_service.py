from fastapi.testclient import TestClient

from compute_agent.app import app
from compute_agent.auth import get_gcp_id_token


def test_agent_health_endpoint():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["service"] == "agentic-compute-agent"


def test_agent_root_info():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["service"] == "agentic-compute-agent"
    assert data["agent_name"] == "compute_agent"
    assert "model" in data


def test_auth_token_override(monkeypatch):
    monkeypatch.setenv("GCP_ID_TOKEN", "mock-token-xyz")
    token = get_gcp_id_token("https://example.run.app")
    assert token == "mock-token-xyz"


def test_optimize_stream_endpoint_structure():
    client = TestClient(app)
    # Testing GET /optimize/stream connects and returns event-stream header
    with client.stream("GET", "/optimize/stream?objective=test") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

