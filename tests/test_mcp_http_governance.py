"""The MCP HTTP surface must obey the same governance as the main API.

These tests exist because the MCP custom routes were a second, ungoverned door
into the product:

* ``POST /plans/compare`` produced plan ids the governance store had never seen,
  so no plan compared through MCP could ever be approved or executed.
* ``POST /plans/approve`` wrote only into the history store, which the execution
  gate does not read, and answered HTTP 200 with ``{"status": "not_found"}`` for
  a plan that does not exist. A caller checking the HTTP status believed an
  approval had been recorded when nothing had.

Each test below fails against the pre-fix routes.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient


@pytest.fixture()
def client():
    from agentic_compute.mcp_server import app

    return TestClient(app)


def _profile(workload_id: str = "wl-mcp-http") -> dict:
    return {
        "workload_id": workload_id,
        "name": "mcp http journey",
        "cpu_requested": 8,
        "memory_mb_requested": 16384,
        "estimated_duration_minutes": 60,
        "budget_amount": 500.0,
        "command": "python train.py",
    }


def test_compare_registers_plans_so_they_can_actually_be_approved(client):
    from agentic_compute.governance import get_governance_store

    resp = client.post("/plans/compare", json={"workload_profile": _profile()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_feasible"] is True
    assert body["plans"], "a feasible comparison must return plans"
    assert body["registered"] is True
    assert body["workload_id"] == "wl-mcp-http"

    gov = get_governance_store()
    for plan in body["plans"]:
        registered = gov.get_registered_plan(plan["plan_id"])
        assert registered is not None, (
            f"{plan['plan_id']} was returned to the caller but never registered, "
            "so the execution gate would refuse it as unknown"
        )
        assert registered["workload_id"] == "wl-mcp-http"


def test_compare_refuses_a_profile_without_a_workload_id(client):
    resp = client.post("/plans/compare", json={"workload_profile": {"name": "anonymous"}})
    assert resp.status_code == 400
    assert "workload_id" in resp.json()["error"]


def test_approving_an_unknown_plan_is_a_404_not_a_200(client):
    resp = client.post("/plans/approve", json={"plan_id": "plan-invented-by-the-agent"})
    assert resp.status_code == 404, (
        "returning 200 with a not_found body let a caller record a phantom approval"
    )
    assert resp.json()["status"] == "not_found"


def test_approval_through_mcp_unlocks_execution_through_the_gate(client):
    from agentic_compute.execution_controller import ExecutionController
    from agentic_compute.simulator import SimulatedRuntime

    profile = _profile("wl-mcp-gate")
    plans = client.post("/plans/compare", json={"workload_profile": profile}).json()["plans"]
    plan = plans[0]

    controller = ExecutionController(runtime=SimulatedRuntime())

    # Before approval the gate must refuse, even though the plan is registered.
    allowed, reason = controller.evaluate_control_gate(
        control_mode="validation",
        plan=plan,
        workload_id=profile["workload_id"],
        command=profile["command"],
    )
    assert allowed is False, reason

    resp = client.post(
        "/plans/approve",
        json={"plan_id": plan["plan_id"], "workload_id": profile["workload_id"]},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    allowed, reason = controller.evaluate_control_gate(
        control_mode="validation",
        plan=plan,
        workload_id=profile["workload_id"],
        command=profile["command"],
    )
    assert allowed is True, (
        "an approval recorded through MCP must be the same approval the gate reads; "
        f"gate said: {reason}"
    )


def test_approving_a_plan_for_the_wrong_workload_is_a_conflict(client):
    profile = _profile("wl-mcp-owner")
    plans = client.post("/plans/compare", json={"workload_profile": profile}).json()["plans"]
    resp = client.post(
        "/plans/approve",
        json={"plan_id": plans[0]["plan_id"], "workload_id": "wl-somebody-else"},
    )
    assert resp.status_code == 409
    assert resp.json()["status"] == "workload_mismatch"


def test_an_infeasible_comparison_says_nothing_was_registered(client):
    profile = _profile("wl-mcp-infeasible")
    profile["budget_amount"] = 0.0
    resp = client.post("/plans/compare", json={"workload_profile": profile})
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_feasible"] is False
    assert body["plans"] == []
    assert body["registered"] is False
    assert body["registration_skipped_reason"]
    assert body["blocking_constraints"]


def test_a_misspelled_constraint_is_rejected_instead_of_silently_dropped(client):
    """`budget_eur` is not a field. Pydantic used to drop it and answer "feasible".

    A dropped constraint is the worst kind of false success: the operator sees a
    plan that respects a budget the engine never received.
    """
    profile = _profile("wl-mcp-typo")
    profile.pop("budget_amount")
    profile["budget_eur"] = 0.01  # plausible, and wrong

    resp = client.post("/plans/compare", json={"workload_profile": profile})
    assert resp.status_code == 400
    assert "budget_eur" in resp.json()["error"]
