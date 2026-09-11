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
    """Nominal tool workflow, with the operator having delegated this workload.

    The delegation is now set explicitly: mutating MCP tools go through the
    governance choke point, whose default is validation. Previously the tools
    honoured only the runtime adapter's own mode, which both adapters set to
    delegation, so the operator's choice had no effect at this entry point.
    """
    from agentic_compute.governance import get_governance_store
    from agentic_compute.models import DelegationPolicy

    get_governance_store().set_workload_control(
        "mc-001", "delegation", DelegationPolicy(max_budget_eur=100.0)
    )

    reset_runtime()
    snap = get_runtime_snapshot()
    assert snap["workload"]["allocated_cpu"] == 32

    resized = resize_workload("mc-001", 64)
    assert resized["status"] == "applied"
    assert resized["cpu"] == 64
    assert resized["control_mode"] == "delegation"

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


def test_parse_duration_to_minutes():
    import pytest
    from agentic_compute.capacity_advisor import parse_duration_to_minutes

    assert parse_duration_to_minutes("900s") == 15.0
    assert parse_duration_to_minutes("5400s") == 90.0
    assert parse_duration_to_minutes("90.5s") == pytest.approx(1.5083, rel=1e-3)
    assert parse_duration_to_minutes(3600) == 60.0
    assert parse_duration_to_minutes(90.5) == pytest.approx(1.5083, rel=1e-3)
    assert parse_duration_to_minutes("invalid") is None
    assert parse_duration_to_minutes(None) is None
    assert parse_duration_to_minutes("") is None
    assert parse_duration_to_minutes("-100s") is None


def test_capacity_advisor_partial_live_when_history_fails(monkeypatch):
    import agentic_compute.capacity_advisor as cap_module

    cap_data = {
        "recommendations": [
            {
                "scores": {"obtainability": 0.88, "estimatedUptime": "5400s"},
                "shards": [{"zone": "projects/p/zones/us-central1-a"}],
            }
        ]
    }

    def mock_post(url, *args, **kwargs):
        if "advice/capacityHistory" in url:
            return type("MockResponse", (), {"status_code": 503, "text": "Unavailable", "json": lambda self: {}})()
        return type("MockResponse", (), {"status_code": 200, "text": "ok", "json": lambda self: cap_data})()

    monkeypatch.setattr(cap_module, "HAVE_GOOGLE_AUTH", True)
    monkeypatch.setattr(
        "google.auth.default",
        lambda *args, **kwargs: (type("Creds", (), {"valid": True, "token": "dummy"})(), "project"),
    )
    monkeypatch.setattr("requests.post", mock_post)

    advice = cap_module.query_capacity_advice(machine_types="n2-standard-32", demo_mode=False)

    assert advice["status"] == "partial_live"
    assert advice["is_simulated"] is False
    assert advice["obtainability_score"] == 0.88
    assert advice["historical_preemption_rate_7d_avg"] is None
    assert advice["preemption_risk"] == "UNKNOWN"
    rec = advice["machine_types"][0]
    assert rec["obtainability"] == 0.88
    assert rec["estimated_uptime_minutes"] == 90.0
    assert rec["historical_preemption_rate_7d"] is None
    assert rec["preemption_risk_level"] == "UNKNOWN"


def test_capacity_advisor_zero_preemption_preserved(monkeypatch):
    import agentic_compute.capacity_advisor as cap_module

    cap_data = {
        "recommendations": [
            {
                "scores": {"obtainability": 0.95, "estimatedUptime": "3600s"},
                "shards": [{"zone": "projects/p/zones/us-central1-f"}],
            }
        ]
    }
    hist_data = {
        "preemptionHistory": [{"preemptionRate": 0.0}, {"preemptionRate": 0.0}]
    }

    def mock_post(url, *args, **kwargs):
        if "advice/capacityHistory" in url:
            return type("MockResponse", (), {"status_code": 200, "text": "ok", "json": lambda self: hist_data})()
        return type("MockResponse", (), {"status_code": 200, "text": "ok", "json": lambda self: cap_data})()

    monkeypatch.setattr(cap_module, "HAVE_GOOGLE_AUTH", True)
    monkeypatch.setattr(
        "google.auth.default",
        lambda *args, **kwargs: (type("Creds", (), {"valid": True, "token": "dummy"})(), "project"),
    )
    monkeypatch.setattr("requests.post", mock_post)

    advice = cap_module.query_capacity_advice(machine_types="n4-standard-32", demo_mode=False)

    assert advice["status"] == "live"
    assert advice["obtainability_score"] == 0.95
    assert advice["historical_preemption_rate_7d_avg"] == 0.0
    assert advice["preemption_risk"] == "LOW"
    rec = advice["machine_types"][0]
    assert rec["historical_preemption_rate_7d"] == 0.0
    assert rec["preemption_risk_level"] == "LOW"


def test_mcp_resize_workload_reports_truthful_status(monkeypatch):
    from agentic_compute.mcp_server import resize_workload
    import agentic_compute.mcp_server as mcp_mod
    from agentic_compute.slurm_adapter import SlurmRuntime

    from agentic_compute.governance import get_governance_store
    from agentic_compute.models import DelegationPolicy

    get_governance_store().set_workload_control(
        "mcp-test", "delegation", DelegationPolicy(max_budget_eur=100.0)
    )

    slurm_rt = SlurmRuntime(job_id="mcp-test")
    slurm_rt.job_status = "PENDING"
    monkeypatch.setattr(mcp_mod, "_runtime", slurm_rt)

    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 200, "text": "ok", "json": lambda self: {}})(),
    )

    # Pending verification returned through MCP tool
    res = resize_workload("mcp-test", 60, machine_type="c2-standard-60", provisioning_model="100% Standard")
    assert res["status"] == "pending_verification"
    assert res["slurm_verification"]["status"] == "pending_verification"

    # Unsupported configuration returned through MCP tool
    unsupported_res = resize_workload("mcp-test", 60, machine_type="n4-standard-64")
    assert unsupported_res["status"] == "unsupported"
    assert "unsupported" in unsupported_res["error"].lower()







# ---------------------------------------------------------------------------
# Legacy tools must not be a way around the operator's control mode
# ---------------------------------------------------------------------------

def test_legacy_mcp_tools_cannot_bypass_advisory_mode():
    """The older mutating tools are subject to the same gate as execute_plan_controlled.

    Before this, an agent that wanted to act despite advisory mode only had to
    call ``resize_workload`` or ``submit_job`` instead of the controlled path.
    """
    from agentic_compute.governance import get_governance_store
    from agentic_compute.mcp_server import reset_runtime, resize_workload, submit_job, get_runtime_snapshot

    reset_runtime()
    gov = get_governance_store()
    gov.set_workload_control("mc-001", "advisory")
    gov.set_workload_control("wl-legacy-submit", "advisory")

    before = get_runtime_snapshot()["workload"]["allocated_cpu"]

    resized = resize_workload("mc-001", 64)
    assert resized["status"] == "blocked", resized
    assert resized["control_mode"] == "advisory"
    assert "read-only" in resized["reason"].lower()

    # The cluster must be untouched, not merely reported as blocked.
    assert get_runtime_snapshot()["workload"]["allocated_cpu"] == before

    submitted = submit_job(name="wl-legacy-submit", cpu=8, workload_id="wl-legacy-submit")
    assert submitted["status"] == "blocked", submitted
    assert submitted["control_mode"] == "advisory"


def test_legacy_mcp_tools_require_an_approved_plan_in_validation_mode():
    """Validation mode is the server default, so an unqualified tool call is refused."""
    from agentic_compute.governance import get_governance_store, plan_fingerprint
    from agentic_compute.models import ExecutionPlan
    from agentic_compute.mcp_server import reset_runtime, resize_workload, get_runtime_snapshot

    reset_runtime()
    gov = get_governance_store()
    # No explicit configuration: the server default (validation) applies.
    assert gov.get_workload_control("mc-001")["control_mode"] == "validation"

    before = get_runtime_snapshot()["workload"]["allocated_cpu"]

    no_plan = resize_workload("mc-001", 64)
    assert no_plan["status"] == "blocked", no_plan
    assert "approved plan" in no_plan["reason"].lower()
    assert get_runtime_snapshot()["workload"]["allocated_cpu"] == before

    # An unregistered plan id is not evidence either.
    invented = resize_workload("mc-001", 64, plan_id="plan-i-made-this-up")
    assert invented["status"] == "blocked", invented
    assert "not registered" in invented["reason"].lower()

    # With a registered and approved plan, the resize proceeds.
    plan = ExecutionPlan(
        plan_id="plan-legacy-ok",
        plan_type="balanced_tradeoff",
        title="Legacy path",
        machine_type="n2-standard-64",
        cpu=64,
        estimated_cost_eur=3.0,
        ranking_rationale="test",
    )
    gov.register_plan("mc-001", plan)
    gov.approve_plan(plan.plan_id, workload_id="mc-001")

    allowed = resize_workload("mc-001", 64, plan_id=plan.plan_id)
    assert allowed["status"] == "applied", allowed
    assert allowed["control_mode"] == "validation"
    assert get_runtime_snapshot()["workload"]["allocated_cpu"] == 64

    # Materially changing the plan invalidates the approval.
    changed = plan.model_copy(update={"cpu": 32, "machine_type": "n2-standard-32"})
    assert plan_fingerprint(changed) != plan_fingerprint(plan)
    gov.register_plan("mc-001", changed)
    after_change = resize_workload("mc-001", 32, plan_id=plan.plan_id)
    assert after_change["status"] == "blocked", after_change
