"""End-to-end HTTP journey and API-level enforcement.

These tests drive the FastAPI application the dashboard actually calls, rather
than the library underneath it. That distinction matters: the audit found the
library gates were bypassed by the HTTP layer, and that ``/api/history`` still
returned 404 for a workload that had just been submitted successfully.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from compute_agent.app import app


@pytest.fixture()
def client():
    return TestClient(app)


def _profile(workload_id: str, **overrides):
    base = {
        "workload_id": workload_id,
        "name": "journey-workload",
        "cpu_requested": 8,
        "memory_mb_requested": 32768,
        "budget_amount": 40.0,
        "deadline_minutes_from_start": 120.0,
        "allowed_regions": ["europe-west1"],
        "allow_spot": True,
        "allow_fallback_to_standard": True,
        "command": "python train.py",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Full journey
# ---------------------------------------------------------------------------

def test_full_journey_define_search_compare_approve_execute_history(client):
    """Define -> search -> compare -> approve the exact plan -> submit once -> history."""
    wl = "wl-http-journey-1"
    profile = _profile(wl)

    # 1. Search compatible capacity.
    search = client.post(
        "/api/capacity-search",
        json={"cpu_requested": 8, "memory_gb_requested": 32.0, "demo_mode": True},
    )
    assert search.status_code == 200, search.text
    assert search.json()["candidates"], "capacity search returned nothing"

    # 2. Compare plans. The server must register them so they become approvable.
    compare = client.post(
        "/api/plans/compare",
        json={"workload_profile": profile, "cluster_total_cpu": 128, "demo_mode": True},
    )
    assert compare.status_code == 200, compare.text
    cmp_body = compare.json()
    assert cmp_body["is_feasible"] is True, cmp_body
    assert cmp_body["registered"] is True
    plans = cmp_body["plans"]
    assert len(plans) == 3
    chosen = plans[0]

    # 3. Executing before approval must be refused, even with forged flags.
    forged = client.post(
        "/api/execute-plan",
        json={
            "workload_profile": profile,
            "plan": chosen,
            "control_mode": "validation",
            "is_operator_approved": True,
            "approved_plan_id": chosen["plan_id"],
        },
    )
    assert forged.status_code == 200
    assert forged.json()["status"] == "blocked", forged.json()

    # 4. Approve the exact plan.
    approve = client.post(
        "/api/plans/approve", json={"plan_id": chosen["plan_id"], "workload_id": wl}
    )
    assert approve.status_code == 200, approve.text
    assert approve.json()["status"] == "approved"

    # 5. Submit once.
    execute = client.post(
        "/api/execute-plan",
        json={"workload_profile": profile, "plan": chosen, "control_mode": "validation"},
    )
    assert execute.status_code == 200, execute.text
    exec_body = execute.json()
    assert exec_body["status"] == "submitted", exec_body
    job_id = exec_body["job_id"]
    assert job_id

    # Identifiers must stay distinct: workload, plan, attempt and job.
    assert exec_body["history_linked"] is True, exec_body.get("history_link_error")
    attempt_id = exec_body["attempt_id"]
    assert len({wl, chosen["plan_id"], attempt_id, job_id}) == 4

    # 6. The submitted workload is findable in history (previously a 404).
    hist = client.get(f"/api/history?workload_id={wl}")
    assert hist.status_code == 200, hist.text
    hist_body = hist.json()
    assert hist_body["record"]["workload_id"] == wl
    assert "comparison" in hist_body

    # 7. A double click must not create a second job.
    again = client.post(
        "/api/execute-plan",
        json={"workload_profile": profile, "plan": chosen, "control_mode": "validation"},
    )
    assert again.status_code == 200
    again_body = again.json()
    assert again_body["status"] == "already_submitted", again_body
    assert again_body["job_id"] == job_id


# ---------------------------------------------------------------------------
# Backend enforcement at the HTTP boundary
# ---------------------------------------------------------------------------

def test_approving_an_unknown_plan_is_an_http_error(client):
    """A phantom approval must not be reported as HTTP 200."""
    resp = client.post("/api/plans/approve", json={"plan_id": "plan-does-not-exist"})
    assert resp.status_code == 404, resp.text
    assert "not registered" in resp.json()["detail"].lower()


def test_approving_a_plan_for_the_wrong_workload_is_rejected(client):
    wl = "wl-http-mismatch"
    profile = _profile(wl)
    compare = client.post(
        "/api/plans/compare",
        json={"workload_profile": profile, "cluster_total_cpu": 128, "demo_mode": True},
    )
    plan_id = compare.json()["plans"][0]["plan_id"]

    resp = client.post(
        "/api/plans/approve", json={"plan_id": plan_id, "workload_id": "someone-elses-workload"}
    )
    assert resp.status_code == 409, resp.text


def test_advisory_mode_blocks_execution_over_http(client):
    """Advisory is read-only, and the server's mode wins over the caller's."""
    wl = "wl-http-advisory"
    profile = _profile(wl)
    compare = client.post(
        "/api/plans/compare",
        json={"workload_profile": profile, "cluster_total_cpu": 128, "demo_mode": True},
    )
    chosen = compare.json()["plans"][0]
    client.post("/api/plans/approve", json={"plan_id": chosen["plan_id"], "workload_id": wl})

    set_mode = client.post(f"/api/workloads/{wl}/control", json={"control_mode": "advisory"})
    assert set_mode.status_code == 200
    assert set_mode.json()["control_mode"] == "advisory"

    # Even asking for delegation with a huge budget cannot widen advisory.
    resp = client.post(
        "/api/execute-plan",
        json={
            "workload_profile": profile,
            "plan": chosen,
            "control_mode": "delegation",
            "delegation_policy": {"max_budget_eur": 9999.0},
        },
    )
    body = resp.json()
    assert body["status"] == "blocked", body
    assert body["control_mode"] == "advisory"


def test_control_endpoint_defaults_to_validation_and_rejects_unknown_modes(client):
    default = client.get("/api/workloads/wl-never-configured/control")
    assert default.status_code == 200
    assert default.json()["control_mode"] == "validation"
    assert default.json()["source"] == "server_default"

    bad = client.post("/api/workloads/wl-bad-mode/control", json={"control_mode": "yolo"})
    assert bad.status_code == 400


def test_execute_plan_requires_identifiers(client):
    missing_wl = client.post("/api/execute-plan", json={"workload_profile": {}, "plan": {"plan_id": "p"}})
    assert missing_wl.status_code == 400

    missing_plan = client.post(
        "/api/execute-plan", json={"workload_profile": {"workload_id": "wl-x"}, "plan": {}}
    )
    assert missing_plan.status_code == 400


def test_compare_without_workload_id_is_a_client_error_not_a_crash(client):
    """A profile with no workload_id used to raise a pydantic error inside the
    endpoint and surface as HTTP 500. Plans cannot be registered or approved
    without a workload, so this is a 400 with an actionable message."""
    resp = client.post(
        "/api/plans/compare",
        json={"workload_profile": {"cpu_requested": 4}, "cluster_total_cpu": 128, "demo_mode": True},
    )
    assert resp.status_code == 400, resp.text
    assert "workload_id" in resp.json()["detail"]


def test_infeasible_request_is_reported_not_crashed(client):
    """An impossible request must return a structured explanation, not a plan."""
    resp = client.post(
        "/api/plans/compare",
        json={
            "workload_profile": {
                "workload_id": "wl-http-absurd",
                "cpu_requested": 10000,
                "gpu_requested": 8,
                "memory_mb_requested": 10_000_000,
                "budget_amount": 5.0,
            },
            "cluster_total_cpu": 128,
            "demo_mode": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_feasible"] is False, body
    assert body["plans"] == []
    assert body["registered"] is False
    assert body.get("blocking_constraints"), body
