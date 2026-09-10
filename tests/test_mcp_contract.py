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


def test_capacity_advisor_demo_mode_explicit_labeling(monkeypatch):
    import agentic_compute.capacity_advisor as cap_module

    # Mock _call_gcp_advice_api to return None so it falls back to demo mode
    monkeypatch.setattr(cap_module, "_call_gcp_advice_api", lambda *args, **kwargs: None)

    advice = cap_module.query_capacity_advice(
        machine_types="n4-standard-32,n2-standard-32",
        demo_mode=True,
    )
    assert advice["status"] == "simulated"
    assert advice["is_simulated"] is True
    assert advice["source"] == "simulated_demo_data"
    assert "démo" in advice["data_note"].lower()
    assert len(advice["machine_types"]) == 2


def test_capacity_advisor_unavailable_mode_when_not_demo(monkeypatch):
    import agentic_compute.capacity_advisor as cap_module

    # Mock _call_gcp_advice_api to simulate offline/unreachable GCP API
    monkeypatch.setattr(cap_module, "_call_gcp_advice_api", lambda *args, **kwargs: None)

    advice = cap_module.query_capacity_advice(
        machine_types="n4-standard-32",
        demo_mode=False,
    )
    assert advice["status"] == "unavailable"
    assert advice["is_simulated"] is False
    assert advice["source"] == "unavailable"
    assert advice["obtainability_score"] is None
    assert advice["recommendations"] == []
    assert advice["machine_types"] == []
    assert "indisponibles" in advice["data_note"].lower()


def test_capacity_advisor_live_telemetry_labeled(monkeypatch):
    import agentic_compute.capacity_advisor as cap_module

    live_payload = {
        "region": "us-central1",
        "primary_machine_type": "n4-standard-32",
        "obtainability_score": 0.92,
        "recommendations": [{"machine_type": "n4-standard-32", "rank": 1}],
        "machine_types": [{"machine_type": "n4-standard-32", "rank": 1}],
        "source": "google_compute_engine_capacity_advisor_api",
        "status": "live",
        "is_simulated": False,
        "data_note": "GCP Capacity Advisor Telemetry (Live)",
    }
    monkeypatch.setattr(cap_module, "_call_gcp_advice_api", lambda *args, **kwargs: live_payload)

    advice = cap_module.query_capacity_advice(
        machine_types="n4-standard-32",
        demo_mode=False,
    )
    assert advice["status"] == "live"
    assert advice["is_simulated"] is False
    assert advice["source"] == "google_compute_engine_capacity_advisor_api"
    assert advice["obtainability_score"] == 0.92




