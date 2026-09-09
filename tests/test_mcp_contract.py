from agentic_compute.mcp_server import (
    advance_time,
    get_capacity_advice,
    get_runtime_snapshot,
    reset_runtime,
    resize_workload,
)
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


def test_candidate_allocation_enriched_fields():
    runtime = SimulatedRuntime()
    snapshot = runtime.snapshot()
    candidate = snapshot.candidate_allocations[0]
    assert hasattr(candidate, "machine_type")
    assert hasattr(candidate, "rank")
    assert hasattr(candidate, "provisioning_mix")
    assert hasattr(candidate, "obtainability_score")
    assert candidate.obtainability_score is not None
    assert candidate.machine_type is not None


def test_get_capacity_advice_tool():
    advice = get_capacity_advice(machine_types="n4-standard-32,n2-standard-32", size=10)
    assert "region" in advice
    assert "machine_types" in advice
    assert len(advice["machine_types"]) >= 2
    top = advice["machine_types"][0]
    assert "machine_type" in top
    assert "rank" in top
    assert "obtainability_score" in top
    assert "historical_preemption_rate_7d" in top


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

    # Test error handling when invalid CPU is requested
    error_res = resize_workload("mc-001", 999)
    assert error_res["status"] == "error"
    assert "error" in error_res


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

    cap_resp = client.get("/capacity-advice?size=10")
    assert cap_resp.status_code == 200
    cap_data = cap_resp.json()
    assert "machine_types" in cap_data



