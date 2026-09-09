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


def test_agent_ui_endpoint():
    client = TestClient(app)
    resp = client.get("/ui")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "AgentGrid" in resp.text


def test_agent_api_snapshot_endpoint():
    client = TestClient(app)
    resp = client.get("/api/snapshot")
    assert resp.status_code == 200
    data = resp.json()
    assert "snapshot" in data


def test_agent_api_capacity_advice_endpoint():
    client = TestClient(app)
    resp = client.get("/api/capacity-advice?size=10")
    assert resp.status_code == 200
    data = resp.json()
    assert "machine_types" in data
    assert "recommendations" in data


def test_optimize_stream_endpoint_json_body():
    client = TestClient(app)
    # Testing POST /optimize/stream with JSON body payload connects and returns event-stream header
    with client.stream(
        "POST",
        "/optimize/stream",
        json={"objective": "Scale workload to finish under 15 minutes", "session_id": "test-session"},
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")


def test_agent_api_key_protection(monkeypatch):
    import compute_agent.app as agent_app
    monkeypatch.setattr(agent_app, "AGENTGRID_API_KEY", "secret-agent-key-123")
    client = TestClient(app)

    # 1. Mutating endpoint rejected without key
    unauth_resp = client.post("/api/reset")
    assert unauth_resp.status_code == 401

    # 2. Mutating endpoint accepted with valid X-API-Key
    auth_resp = client.post("/api/reset", headers={"X-API-Key": "secret-agent-key-123"})
    assert auth_resp.status_code == 200

    # 3. Mutating endpoint accepted with Bearer token
    bearer_resp = client.post("/api/reset", headers={"Authorization": "Bearer secret-agent-key-123"})
    assert bearer_resp.status_code == 200

