"""The controlled fallback must be reachable, not just implemented.

`ExecutionController.evaluate_fallback_ladder_with_details` was written and
tested, and nothing could call it: no HTTP endpoint, no MCP tool, no dashboard
control.

    $ grep -rn "evaluate_fallback_ladder" src/agentic_compute/mcp_server.py compute_agent/app.py
    (no match)

Meanwhile ``execute_plan_controlled`` advertised "fallback handling" in its
docstring. Capability 4 of the product scope is "execution with a *controlled
fallback plan*"; an operator or agent had no way to obtain one.

These tests pin the exposed surface, and the fact that exposing it did not
weaken any of the ladder's rules.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentic_compute import mcp_server
from agentic_compute.governance import get_governance_store
from agentic_compute.models import DelegationPolicy, ExecutionPlan
from compute_agent.app import app

WORKLOAD = "wl-fallback-surface"


def _plan(plan_id: str, region: str, cost: float, provisioning: str = "SPOT") -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id,
        title=f"plan {plan_id}",
        plan_type="cost_optimized",
        machine_type="n2-standard-8",
        cpu=8,
        region=region,
        zone=f"{region}-a",
        provisioning_model=provisioning,
        estimated_cost_eur=cost,
        estimated_duration_minutes=30.0,
        command="python train.py",
    )


@pytest.fixture()
def registered_plans():
    gov = get_governance_store()
    plans = [
        _plan("p-cur", "europe-west4", 5.0),
        _plan("p-next", "europe-west1", 6.0),
    ]
    for plan in plans:
        gov.register_plan(WORKLOAD, plan, command="python train.py")
    return plans


@pytest.fixture()
def client():
    return TestClient(app)


def test_the_endpoint_exists_and_proposes_a_registered_rung(client, registered_plans):
    response = client.post(
        f"/api/workloads/{WORKLOAD}/fallback",
        json={"current_plan_id": "p-cur", "failure_category": "spot_preemption"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "selected", body
    assert body["plan"]["plan_id"] == "p-next"
    assert body["available_plans_source"] == "registered_plans"
    assert body["candidates_considered"] == 2


def test_the_endpoint_never_submits(client, registered_plans):
    """Deciding a rung and launching it are different acts."""
    response = client.post(
        f"/api/workloads/{WORKLOAD}/fallback", json={"current_plan_id": "p-cur"}
    )
    body = response.json()

    assert body["submitted"] is False
    assert body["requires_approval"] is True
    assert "/api/execute-plan" in body["next_step"]
    # And indeed nothing was claimed in the ledger.
    assert get_governance_store().list_submissions(WORKLOAD) == []


def test_advisory_mode_refuses_through_the_endpoint(client, registered_plans):
    get_governance_store().set_workload_control(WORKLOAD, "advisory")

    body = client.post(
        f"/api/workloads/{WORKLOAD}/fallback", json={"current_plan_id": "p-cur"}
    ).json()

    assert body["status"] == "blocked"
    assert body["plan"] is None
    assert "read-only" in body["reason"]


def test_delegation_does_not_ask_for_another_approval(client, registered_plans):
    get_governance_store().set_workload_control(
        WORKLOAD, "delegation", DelegationPolicy(max_budget_eur=100.0)
    )

    body = client.post(
        f"/api/workloads/{WORKLOAD}/fallback", json={"current_plan_id": "p-cur"}
    ).json()

    assert body["status"] == "selected"
    assert body["requires_approval"] is False
    assert body["control_mode"] == "delegation"


def test_the_endpoint_honours_the_workload_constraints(client, registered_plans):
    """A rung outside the allowed region is skipped, with its reason."""
    body = client.post(
        f"/api/workloads/{WORKLOAD}/fallback",
        json={
            "current_plan_id": "p-cur",
            "workload_profile": {
                "workload_id": WORKLOAD,
                "cpu_requested": 8,
                "command": "python train.py",
                "allowed_regions": ["europe-west4"],
            },
        },
    ).json()

    assert body["status"] == "constraint_breach", body
    assert [s["plan_id"] for s in body["skipped_candidates"]] == ["p-next"]
    assert body["profile_constraints_source"] == "caller_supplied"


def test_an_unknown_current_plan_is_a_404(client, registered_plans):
    response = client.post(
        f"/api/workloads/{WORKLOAD}/fallback", json={"current_plan_id": "p-nope"}
    )

    assert response.status_code == 404
    assert "not registered" in response.json()["detail"]


def test_a_missing_current_plan_is_a_400(client, registered_plans):
    response = client.post(f"/api/workloads/{WORKLOAD}/fallback", json={})

    assert response.status_code == 400
    assert "current_plan" in response.json()["detail"]


def test_a_budget_breach_is_reported_rather_than_silently_downgraded(client, registered_plans):
    body = client.post(
        f"/api/workloads/{WORKLOAD}/fallback",
        json={
            "current_plan_id": "p-cur",
            "budget_limit_eur": 4.0,
            "accumulated_cost_eur": 0.0,
        },
    ).json()

    assert body["status"] == "budget_breach", body
    assert body["plan"] is None
    assert body["rejected_plan_id"] == "p-next"


# --- MCP surface ------------------------------------------------------------


def test_the_mcp_tool_proposes_the_same_rung(registered_plans):
    result = mcp_server.select_fallback_plan(
        workload_id=WORKLOAD, current_plan_id="p-cur", failure_category="spot_preemption"
    )

    assert result["status"] == "selected", result
    assert result["plan"]["plan_id"] == "p-next"
    assert result["submitted"] is False
    assert result["requires_approval"] is True


def test_the_mcp_tool_refuses_an_unregistered_plan(registered_plans):
    result = mcp_server.select_fallback_plan(workload_id=WORKLOAD, current_plan_id="ghost")

    assert result["status"] == "not_found"
    assert result["plan"] is None


def test_the_mcp_tool_requires_something_to_replace(registered_plans):
    result = mcp_server.select_fallback_plan(workload_id=WORKLOAD)

    assert result["status"] == "invalid_request"
    assert result["plan"] is None
