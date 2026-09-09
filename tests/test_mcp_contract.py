from agentic_compute.mcp_server import advance_time, get_runtime_snapshot, reset_runtime, resize_workload
from agentic_compute.models import RuntimeSnapshot
from agentic_compute.simulator import SimulatedRuntime


def test_snapshot_serializes_as_universal_contract():
    runtime = SimulatedRuntime()
    payload = runtime.snapshot().model_dump()
    rebuilt = RuntimeSnapshot.model_validate(payload)

    assert rebuilt.cluster.total_cpu == 128
    assert rebuilt.workload.id == "mc-001"
    assert rebuilt.objective.minimize_cost is True
    assert rebuilt.candidate_allocations


def test_mcp_server_tools_workflow():
    reset_runtime()
    snap = get_runtime_snapshot()
    assert snap["workload"]["allocated_cpu"] == 32

    resized = resize_workload("mc-001", 64)
    assert resized["status"] == "applied"
    assert resized["cpu"] == 64

    advanced = advance_time(5.0)
    assert advanced["cluster"]["current_time_minutes"] == 5.0
    assert advanced["workload"]["remaining_work_units"] < snap["workload"]["remaining_work_units"]

    reset_snap = reset_runtime()
    assert reset_snap["cluster"]["current_time_minutes"] == 0.0
    assert reset_snap["workload"]["allocated_cpu"] == 32


def test_mcp_health_endpoint():
    from starlette.testclient import TestClient
    from agentic_compute.mcp_server import app

    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "agentic-compute-mcp"


def test_mcp_snapshot_and_reset_endpoints():
    from starlette.testclient import TestClient
    from agentic_compute.mcp_server import app

    client = TestClient(app)
    snap_resp = client.get("/snapshot")
    assert snap_resp.status_code == 200
    snap_data = snap_resp.json()
    assert "snapshot" in snap_data
    assert "runtime" in snap_data

    reset_resp = client.post("/reset")
    assert reset_resp.status_code == 200
    reset_data = reset_resp.json()
    assert reset_data["status"] == "reset"
    assert "snapshot" in reset_data


