"""The constraints declared on a workload must bind execution, not just planning.

Defects reproduced on the pre-fix code (script ``/tmp/fb/repro.py``), with a
profile stating ``allowed_regions=["europe-west4"]``,
``allow_region_change=False``, ``allow_spot=False``,
``allow_fallback_to_standard=False``:

    === A. submit a plan violating the profile's location + provisioning constraints ===
    {"status": "submitted", "job_id": "agentgrid-wl-loc-2ea0098ca1f3", "verified": true,
     "reason": "Plan 'p-bad' autonomously approved under delegation policy (Cost: 1.00EUR <= 100.00EUR)."}

    === B. fallback ladder offered only out-of-region / forbidden-provisioning plans ===
    status: selected | chosen: p-us | region: us-central1
    reason: Fallback to plan 'p-us' within remaining budget and policy.

    === C. validation mode: does the ladder say approval is still required? ===
    {"status": "selected", "plan": "p-us", ...}   # no mention of approval

Only ``DelegationPolicy`` was consulted before launching. It is optional, it is
separate from the profile, and nothing required the operator to restate their
constraints in it. So a workload pinned to one region ran in another, a workload
that forbade Spot ran on Spot, and the "controlled" fallback was the least
controlled step of all.
"""

from __future__ import annotations

import pytest

from agentic_compute.execution_controller import ExecutionController
from agentic_compute.governance import get_governance_store
from agentic_compute.history import get_history_store
from agentic_compute.models import DelegationPolicy, ExecutionPlan, WorkloadProfile
from agentic_compute.simulator import SimulatedRuntime


def _plan(plan_id: str, region: str, provisioning: str, cost: float = 1.0) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id,
        title=f"plan {plan_id}",
        plan_type="balanced_tradeoff",
        machine_type="n2-standard-8",
        cpu=8,
        gpu=0,
        region=region,
        zone=f"{region}-a",
        provisioning_model=provisioning,
        quantity=1,
        estimated_cost_eur=cost,
        estimated_duration_minutes=30.0,
        command="python train.py",
    )


def _pinned_profile(workload_id: str = "wl-loc", **overrides) -> WorkloadProfile:
    fields = dict(
        workload_id=workload_id,
        name="pinned",
        cpu_requested=8,
        command="python train.py",
        allowed_regions=["europe-west4"],
        allow_region_change=False,
        allow_spot=False,
        allow_fallback_to_standard=False,
    )
    fields.update(overrides)
    return WorkloadProfile(**fields)


def _delegated(workload_id: str, budget: float = 100.0, retries: int = 5) -> ExecutionController:
    get_governance_store().set_workload_control(
        workload_id, "delegation", DelegationPolicy(max_budget_eur=budget, max_retries=retries)
    )
    return ExecutionController()


# --- Submission -------------------------------------------------------------


def test_delegated_submission_refuses_a_region_the_profile_forbids():
    """Case A of the reproduction: this returned `submitted` with a real job id."""
    profile = _pinned_profile()
    controller = _delegated(profile.workload_id)

    result = controller.submit_plan(
        profile=profile,
        plan=_plan("p-bad", "us-central1", "SPOT"),
        control_mode="delegation",
        runtime=SimulatedRuntime(),
    )

    assert result["status"] == "blocked", result
    assert result["job_id"] is None
    assert result["verified"] is False
    assert result["violated_constraint"] == "workload_profile"
    assert "us-central1" in result["reason"]
    assert "allowed_regions" in result["reason"]


def test_delegated_submission_refuses_spot_when_the_profile_forbids_it():
    profile = _pinned_profile(workload_id="wl-nospot", allowed_regions=["europe-west4"])
    controller = _delegated(profile.workload_id)

    result = controller.submit_plan(
        profile=profile,
        plan=_plan("p-spot", "europe-west4", "SPOT"),
        control_mode="delegation",
        runtime=SimulatedRuntime(),
    )

    assert result["status"] == "blocked", result
    assert result["job_id"] is None
    assert "allow_spot=False" in result["reason"]


def test_a_zone_outside_the_allowed_list_is_refused():
    profile = _pinned_profile(
        workload_id="wl-zone",
        allowed_zones=["europe-west4-b"],
        allow_spot=True,
    )
    controller = _delegated(profile.workload_id)

    result = controller.submit_plan(
        profile=profile,
        plan=_plan("p-zone", "europe-west4", "SPOT"),  # zone europe-west4-a
        control_mode="delegation",
        runtime=SimulatedRuntime(),
    )

    assert result["status"] == "blocked", result
    assert "allowed_zones" in result["reason"]


def test_an_operator_approval_does_not_repeal_the_profile_constraints():
    """Validation mode is not a loophole: the approval authorises a plan, not a region change."""
    profile = _pinned_profile(workload_id="wl-approved", allow_spot=True)
    plan = _plan("p-approved", "us-central1", "SPOT")
    controller = ExecutionController()
    gov = controller.governance
    gov.register_plan(profile.workload_id, plan, command=profile.command)
    approval = gov.approve_plan(
        plan.plan_id, workload_id=profile.workload_id, approved_by="operator-under-test"
    )
    assert approval["status"] == "approved", approval

    result = controller.submit_plan(
        profile=profile,
        plan=plan,
        control_mode="validation",
        approved_plan_id=plan.plan_id,
        runtime=SimulatedRuntime(),
    )

    assert result["status"] == "blocked", result
    assert result["violated_constraint"] == "workload_profile"


def test_a_compliant_plan_is_still_submitted():
    """The guard must not become a blanket refusal."""
    profile = _pinned_profile(workload_id="wl-ok", allow_spot=True)
    controller = _delegated(profile.workload_id)

    result = controller.submit_plan(
        profile=profile,
        plan=_plan("p-ok", "europe-west4", "SPOT"),
        control_mode="delegation",
        runtime=SimulatedRuntime(),
    )

    assert result["status"] == "submitted", result
    assert result["job_id"]


# --- Fallback ladder --------------------------------------------------------


def test_fallback_skips_a_rung_that_leaves_the_allowed_region():
    """Case B: the ladder chose us-central1 for a workload pinned to europe-west4."""
    profile = _pinned_profile(workload_id="wl-fb")
    get_history_store().save_workload_profile(profile)
    controller = _delegated(profile.workload_id)

    result = controller.evaluate_fallback_ladder_with_details(
        current_plan=_plan("p-cur", "europe-west4", "SPOT"),
        available_plans=[
            _plan("p-us", "us-central1", "STANDARD", 2.0),
            _plan("p-asia", "asia-east1", "SPOT", 2.0),
        ],
        failure_category="capacity_shortage",
        workload_id=profile.workload_id,
    )

    assert result["status"] == "constraint_breach", result
    assert result["plan"] is None
    assert [s["plan_id"] for s in result["skipped_candidates"]] == ["p-us", "p-asia"]
    assert result["profile_constraints_source"] == "persisted_profile"


def test_fallback_refuses_to_switch_to_standard_when_the_profile_forbids_it():
    profile = _pinned_profile(
        workload_id="wl-nostd",
        allow_spot=True,
        allow_region_change=True,
        allow_fallback_to_standard=False,
        allowed_regions=["europe-west4"],
    )

    controller = ExecutionController()
    result = controller.evaluate_fallback_ladder_with_details(
        current_plan=_plan("p-cur", "europe-west4", "SPOT"),
        available_plans=[_plan("p-std", "europe-west4", "STANDARD", 2.0)],
        failure_category="spot_preemption",
        workload_id=profile.workload_id,
        profile=profile,
    )

    assert result["status"] == "constraint_breach", result
    assert "allow_fallback_to_standard=False" in result["skipped_candidates"][0]["reason"]


def test_fallback_refuses_a_zone_change_when_the_profile_forbids_it():
    profile = _pinned_profile(
        workload_id="wl-nozone",
        allow_spot=True,
        allow_region_change=True,
        allow_zone_change=False,
        allowed_regions=["europe-west4"],
    )

    controller = ExecutionController()
    result = controller.evaluate_fallback_ladder_with_details(
        current_plan=ExecutionPlan(
            plan_id="p-cur",
            title="current",
            plan_type="balanced_tradeoff",
            machine_type="n2-standard-8",
            cpu=8,
            region="europe-west4",
            zone="europe-west4-b",
            provisioning_model="SPOT",
            estimated_cost_eur=1.0,
        ),
        available_plans=[_plan("p-other-zone", "europe-west4", "SPOT", 2.0)],
        failure_category="capacity_shortage",
        workload_id=profile.workload_id,
        profile=profile,
    )

    assert result["status"] == "constraint_breach", result
    assert "allow_zone_change=False" in result["skipped_candidates"][0]["reason"]


def test_a_compliant_rung_further_down_the_ladder_is_still_selected():
    """Skipping a forbidden rung must not abandon a legitimate alternative."""
    profile = _pinned_profile(
        workload_id="wl-fb-ok", allow_spot=True, allowed_regions=["europe-west4"]
    )
    controller = ExecutionController()

    result = controller.evaluate_fallback_ladder_with_details(
        current_plan=_plan("p-cur", "europe-west4", "SPOT"),
        available_plans=[
            _plan("p-us", "us-central1", "STANDARD", 2.0),
            _plan("p-ok", "europe-west4", "SPOT", 3.0),
        ],
        failure_category="capacity_shortage",
        workload_id=profile.workload_id,
        profile=profile,
    )

    assert result["status"] == "selected", result
    assert result["plan"].plan_id == "p-ok"
    assert [s["plan_id"] for s in result["skipped_candidates"]] == ["p-us"]


def test_the_ladder_says_when_it_could_not_evaluate_any_constraint():
    """No profile means unchecked, and the verdict must admit it rather than imply compliance."""
    controller = ExecutionController()

    result = controller.evaluate_fallback_ladder_with_details(
        current_plan=_plan("p-cur", "europe-west4", "SPOT"),
        available_plans=[_plan("p-us", "us-central1", "STANDARD", 2.0)],
        failure_category="capacity_shortage",
        workload_id="wl-never-seen",
    )

    assert result["status"] == "selected", result
    assert result["profile_constraints_source"] == "unavailable"


# --- Honesty of the "selected" verdict --------------------------------------


def test_selected_outside_delegation_states_that_approval_is_still_required():
    """Case C: `selected` read like a decision to launch, in a mode that forbids launching."""
    get_governance_store().set_workload_control("wl-val", "validation")
    controller = ExecutionController()

    result = controller.evaluate_fallback_ladder_with_details(
        current_plan=_plan("p-cur", "europe-west4", "SPOT"),
        available_plans=[_plan("p-next", "europe-west4", "SPOT", 2.0)],
        failure_category="capacity_shortage",
        workload_id="wl-val",
    )

    assert result["status"] == "selected", result
    assert result["control_mode"] == "validation"
    assert result["requires_approval"] is True
    assert "must still be registered and approved" in result["reason"]


def test_selected_under_delegation_does_not_demand_a_second_approval():
    controller = _delegated("wl-del")

    result = controller.evaluate_fallback_ladder_with_details(
        current_plan=_plan("p-cur", "europe-west4", "SPOT"),
        available_plans=[_plan("p-next", "europe-west4", "SPOT", 2.0)],
        failure_category="capacity_shortage",
        workload_id="wl-del",
    )

    assert result["status"] == "selected", result
    assert result["control_mode"] == "delegation"
    assert result["requires_approval"] is False


def test_advisory_still_blocks_the_ladder_outright():
    """Pre-existing guarantee, kept under test now that the ladder has more exits."""
    get_governance_store().set_workload_control("wl-adv", "advisory")
    controller = ExecutionController()

    result = controller.evaluate_fallback_ladder_with_details(
        current_plan=_plan("p-cur", "europe-west4", "SPOT"),
        available_plans=[_plan("p-next", "europe-west4", "SPOT", 2.0)],
        failure_category="capacity_shortage",
        workload_id="wl-adv",
    )

    assert result["status"] == "blocked", result
    assert result["plan"] is None


# --- The checker itself -----------------------------------------------------


@pytest.mark.parametrize(
    "overrides, plan_args, expected_ok",
    [
        ({}, ("europe-west4", "SPOT"), False),  # allow_spot=False
        ({"allow_spot": True}, ("europe-west4", "SPOT"), True),
        ({"allow_spot": True}, ("us-central1", "SPOT"), False),
        ({"allow_spot": True, "allowed_regions": []}, ("us-central1", "SPOT"), True),
    ],
)
def test_check_profile_constraints_matrix(overrides, plan_args, expected_ok):
    profile = _pinned_profile(workload_id="wl-matrix", **overrides)
    ok, _reason = ExecutionController().check_profile_constraints(
        profile, _plan("p", *plan_args)
    )
    assert ok is expected_ok


# --- The profile in the request is not the last word ------------------------


def test_a_widened_request_profile_cannot_defeat_the_persisted_constraints():
    """`/api/execute-plan` takes the profile from the request body.

    A caller that simply restates its constraints more generously at execution
    time would walk through them, exactly as the self-attested approval flags
    used to. The profile persisted when the plans were compared refuses first.
    """
    pinned = _pinned_profile(workload_id="wl-widen", allow_spot=True)
    get_history_store().save_workload_profile(pinned)
    controller = _delegated(pinned.workload_id)

    widened = WorkloadProfile(
        workload_id=pinned.workload_id,
        name="pinned",
        cpu_requested=8,
        command="python train.py",
        allowed_regions=["us-central1", "europe-west4"],
        allow_region_change=True,
        allow_spot=True,
    )

    result = controller.submit_plan(
        profile=widened,
        plan=_plan("p-widened", "us-central1", "SPOT"),
        control_mode="delegation",
        runtime=SimulatedRuntime(),
    )

    assert result["status"] == "blocked", result
    assert result["constraint_source"] == "persisted_profile"
    assert result["job_id"] is None


def test_a_tightened_request_profile_is_still_honoured():
    """A caller may always narrow its own constraints; only widening is refused."""
    stored = _pinned_profile(
        workload_id="wl-tighten", allow_spot=True, allowed_regions=["europe-west4", "us-central1"]
    )
    get_history_store().save_workload_profile(stored)
    controller = _delegated(stored.workload_id)

    tightened = WorkloadProfile(
        workload_id=stored.workload_id,
        name="pinned",
        cpu_requested=8,
        command="python train.py",
        allowed_regions=["europe-west4"],
        allow_spot=True,
    )

    result = controller.submit_plan(
        profile=tightened,
        plan=_plan("p-tight", "us-central1", "SPOT"),  # allowed by the store, not by the request
        control_mode="delegation",
        runtime=SimulatedRuntime(),
    )

    assert result["status"] == "blocked", result
    assert result["constraint_source"] == "request_profile"


def test_comparing_plans_persists_the_profile_that_produced_them():
    """Without this, there is no server-side record of the constraints to enforce."""
    from fastapi.testclient import TestClient

    from compute_agent.app import app

    client = TestClient(app)
    response = client.post(
        "/api/plans/compare",
        json={
            "workload_profile": {
                "workload_id": "wl-persist",
                "name": "persist",
                "cpu_requested": 8,
                "command": "python train.py",
                "allowed_regions": ["europe-west4"],
                "estimated_duration_minutes": 30.0,
            }
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["profile_persisted"] is True

    stored = get_history_store().get_workload_profile("wl-persist")
    assert stored is not None
    assert stored.allowed_regions == ["europe-west4"]
