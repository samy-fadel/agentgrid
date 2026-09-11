"""Plan comparison must carry the capacity dimension it claims to compare.

Reproduced on the previous commit (script ``/tmp/cap/repro.py``), a comparison
for a 64 vCPU workload in ``europe-west4`` returned::

    plans: 2
      plan-wl-cap-cost_optimized-1fe4858121ac: cpu=88 region=europe-west4 cost=0.06
        time=3.96 | capacity-related keys: []
      plan-wl-cap-balanced_tradeoff-cd7ba3307b3b: cpu=88 region=europe-west4 cost=0.09
        time=3.96 | capacity-related keys: []

``ExecutionPlan``'s own docstring says it compares "cost, latency, and
capacity", and there was no capacity field at all: the operator was asked to
choose between two 88 vCPU plans with nothing said about whether 88 vCPUs can be
obtained. Against the real project the same request now answers
``QUOTA_EXCEEDED: requested 88 vCPUs, available 0/0``.

The verdict must stay honest in both directions: an unreadable quota source is
``QUOTA_UNKNOWN``, which is not ``QUOTA_AVAILABLE``.
"""

from __future__ import annotations

import pytest

from agentic_compute import capacity_advisor, plan_engine
from agentic_compute.plan_engine import evaluate_and_compare_plans

PROFILE = {
    "workload_id": "wl-capacity",
    "name": "capacity dimension",
    "cpu_requested": 16,
    "command": "python train.py",
    "allowed_regions": ["europe-west4"],
    "estimated_duration_minutes": 60.0,
}


@pytest.fixture()
def quota_enabled(monkeypatch):
    """Turn the capacity check back on, with a project and no live API call."""
    monkeypatch.setenv("AGENTGRID_PLAN_CAPACITY_CHECK", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "project-under-test")

    calls: list[dict] = []

    def _record(verdict):
        def _stub(**kwargs):
            calls.append(kwargs)
            return verdict

        monkeypatch.setattr(capacity_advisor, "check_quota_availability", _stub)
        return calls

    return _record


def test_without_a_quota_source_the_plans_say_not_checked():
    """The suite disables the check; the answer must be NOT_CHECKED, never 'available'."""
    result = evaluate_and_compare_plans(profile=PROFILE)

    assert result["is_feasible"] is True
    assert result["capacity_checked"] is False
    assert "not consulted" in result["capacity_note"]
    for plan in result["plans"]:
        assert plan["capacity_status"] == "NOT_CHECKED"
        assert plan["capacity_source"] == "not_checked"


def test_an_available_quota_is_reported_with_its_source(quota_enabled):
    quota_enabled(
        {
            "status": "QUOTA_AVAILABLE",
            "reason": "Request fits within quota (verified against live Compute Engine regional quota)",
            "data_provenance": "gcp_live_api",
        }
    )

    result = evaluate_and_compare_plans(profile=PROFILE)

    assert result["capacity_checked"] is True
    for plan in result["plans"]:
        assert plan["capacity_status"] == "QUOTA_AVAILABLE"
        assert plan["capacity_source"] == "gcp_live_api"
        assert "verified against live Compute Engine regional quota" in plan["capacity_detail"]


def test_an_exceeded_quota_is_visible_on_the_plan(quota_enabled):
    quota_enabled(
        {
            "status": "QUOTA_EXCEEDED",
            "reason": "CPU quota exceeded: requested 88 vCPUs, available 0/0",
            "data_provenance": "gcp_live_api",
        }
    )

    result = evaluate_and_compare_plans(profile=PROFILE)

    for plan in result["plans"]:
        assert plan["capacity_status"] == "QUOTA_EXCEEDED"
        assert "available 0/0" in plan["capacity_detail"]
        # The refusal is also folded into the plan's stated uncertainties, so a
        # reader of the plan alone cannot miss it.
        assert any(
            "quota does not currently authorise" in point.lower()
            for point in plan["unverified_points"]
        ), plan["unverified_points"]


def test_an_unreadable_quota_is_unknown_and_not_available(quota_enabled):
    quota_enabled(
        {
            "status": "QUOTA_UNKNOWN",
            "reason": "No credentials available to read Compute Engine quota",
            "data_provenance": "unavailable",
        }
    )

    result = evaluate_and_compare_plans(profile=PROFILE)

    for plan in result["plans"]:
        assert plan["capacity_status"] == "QUOTA_UNKNOWN"
        assert plan["capacity_status"] != "QUOTA_AVAILABLE"
        assert plan["capacity_source"] == "unavailable"


def test_a_failing_quota_lookup_does_not_break_the_comparison(quota_enabled, monkeypatch):
    monkeypatch.setenv("AGENTGRID_PLAN_CAPACITY_CHECK", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "project-under-test")

    def _boom(**kwargs):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(capacity_advisor, "check_quota_availability", _boom)

    result = evaluate_and_compare_plans(profile=PROFILE)

    assert result["is_feasible"] is True, "a quota outage must not suppress the plans"
    for plan in result["plans"]:
        assert plan["capacity_status"] == "QUOTA_UNKNOWN"
        assert "network unreachable" in plan["capacity_detail"]


def test_without_a_configured_project_the_built_in_default_is_named(monkeypatch):
    """The plan check and the capacity search must read the same project.

    Both now go through ``resolve_quota_project``. With nothing configured they
    fall back to the built-in demo project, and the verdict says which project
    was read, instead of one feature answering "no project configured" while the
    other silently read a hardcoded id and called the answer verified.
    """
    monkeypatch.setenv("AGENTGRID_PLAN_CAPACITY_CHECK", "true")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("PROJECT_ID", raising=False)

    seen: list[dict] = []

    def _stub(**kwargs):
        seen.append(kwargs)
        return {
            "status": "QUOTA_UNKNOWN",
            "reason": "no credentials",
            "data_provenance": "unavailable",
        }

    monkeypatch.setattr(capacity_advisor, "check_quota_availability", _stub)

    evaluate_and_compare_plans(profile=PROFILE)

    assert seen, "the quota reader should still be consulted"
    assert seen[0]["project_id"] == capacity_advisor.BUILT_IN_DEMO_PROJECT
    assert seen[0]["project_source"] == "built_in_default"


def test_a_default_project_figure_says_which_project_it_came_from():
    """`check_quota_availability` stamps the project on every verdict."""
    verdict = capacity_advisor.check_quota_availability(
        region="europe-west4", cpu_needed=8, demo_mode=True
    )

    assert verdict["quota_project"] == capacity_advisor.BUILT_IN_DEMO_PROJECT
    assert verdict["quota_project_source"] == "built_in_default"


def test_an_explicit_project_is_reported_as_the_caller_s_choice():
    verdict = capacity_advisor.check_quota_availability(
        project_id="operator-project", region="europe-west4", cpu_needed=8, demo_mode=True
    )

    assert verdict["quota_project"] == "operator-project"
    assert verdict["quota_project_source"] == "caller"


def test_identical_shapes_are_looked_up_once(quota_enabled):
    calls = quota_enabled(
        {"status": "QUOTA_AVAILABLE", "reason": "ok", "data_provenance": "gcp_live_api"}
    )

    result = evaluate_and_compare_plans(profile=PROFILE)

    distinct = {
        (p["region"], p["cpu"] * max(p["quantity"], 1), p["gpu"] * max(p["quantity"], 1),
         "SPOT" if "spot" in p["provisioning_model"].lower() else "STANDARD")
        for p in result["plans"]
    }
    assert len(calls) == len(distinct), calls


def test_the_explicit_argument_overrides_the_environment(monkeypatch):
    monkeypatch.setenv("AGENTGRID_PLAN_CAPACITY_CHECK", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "project-under-test")
    monkeypatch.setattr(
        capacity_advisor,
        "check_quota_availability",
        lambda **kwargs: {"status": "QUOTA_AVAILABLE", "reason": "ok", "data_provenance": "x"},
    )

    disabled = evaluate_and_compare_plans(profile=PROFILE, check_capacity=False)

    assert disabled["capacity_checked"] is False
    assert all(p["capacity_status"] == "NOT_CHECKED" for p in disabled["plans"])


def test_the_http_comparison_surfaces_the_verdict(monkeypatch):
    from fastapi.testclient import TestClient

    from compute_agent.app import app

    monkeypatch.setenv("AGENTGRID_PLAN_CAPACITY_CHECK", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "project-under-test")
    monkeypatch.setattr(
        capacity_advisor,
        "check_quota_availability",
        lambda **kwargs: {
            "status": "QUOTA_EXCEEDED",
            "reason": "CPU quota exceeded: requested 88 vCPUs, available 0/0",
            "data_provenance": "gcp_live_api",
        },
    )

    response = TestClient(app).post("/api/plans/compare", json={"workload_profile": PROFILE})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["capacity_checked"] is True
    assert body["plans"]
    for plan in body["plans"]:
        assert plan["capacity_status"] == "QUOTA_EXCEEDED"


def test_the_helper_is_importable_and_idempotent(quota_enabled):
    """`annotate_plans_with_capacity` is also usable on plans obtained elsewhere."""
    quota_enabled(
        {"status": "QUOTA_AVAILABLE", "reason": "ok", "data_provenance": "gcp_live_api"}
    )
    from agentic_compute.models import ExecutionPlan

    plan = ExecutionPlan(
        plan_id="p-1",
        plan_type="cost_optimized",
        title="t",
        machine_type="n2-standard-8",
        cpu=8,
        region="europe-west4",
        estimated_cost_eur=1.0,
    )
    assert plan.capacity_status == "NOT_CHECKED"

    plan_engine.annotate_plans_with_capacity([plan])
    assert plan.capacity_status == "QUOTA_AVAILABLE"

    plan_engine.annotate_plans_with_capacity([plan])
    assert plan.capacity_status == "QUOTA_AVAILABLE"
