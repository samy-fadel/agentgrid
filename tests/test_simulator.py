import pytest

from agentic_compute.models import Action
from agentic_compute.simulator import SimulatedRuntime


def test_default_current_allocation_misses_deadline():
    runtime = SimulatedRuntime()
    snapshot = runtime.snapshot()
    current = next(
        c for c in snapshot.candidate_allocations
        if c.cpu == snapshot.workload.allocated_cpu
    )
    assert current.meets_deadline is False


def test_runtime_has_at_least_one_deadline_feasible_candidate():
    runtime = SimulatedRuntime()
    snapshot = runtime.snapshot()
    assert any(c.meets_deadline for c in snapshot.candidate_allocations)


def test_runtime_rejects_invalid_cpu_allocation():
    runtime = SimulatedRuntime()
    with pytest.raises(ValueError):
        runtime.apply(
            Action(
                action="resize_workload",
                workload_id="mc-001",
                cpu=256,
                reason="invalid test action",
            )
        )


def test_resize_then_tick_changes_state():
    runtime = SimulatedRuntime()
    before = runtime.snapshot()

    runtime.apply(
        Action(
            action="resize_workload",
            workload_id="mc-001",
            cpu=64,
            reason="test",
        )
    )
    runtime.tick(5)

    after = runtime.snapshot()
    assert after.cluster.current_time_minutes > before.cluster.current_time_minutes
    assert after.workload.remaining_work_units < before.workload.remaining_work_units
    assert after.workload.accrued_cost_eur > before.workload.accrued_cost_eur


def test_configure_objective_updates_snapshot():
    runtime = SimulatedRuntime()
    runtime.configure_objective(deadline_minutes=45.0, max_cost_eur=12.5, minimize_cost=False)
    snap = runtime.snapshot()
    assert snap.objective.deadline_at_minutes == 45.0
    assert snap.objective.max_cost_eur == 12.5
    assert snap.objective.minimize_cost is False


def test_submitted_job_reports_the_plan_it_was_given():
    """The observed allocation must be the submitted shape, not dataclass defaults.

    Every submission used to surface as a *verified* n2-standard-32 / 100% Spot
    job, so the dashboard confirmed an allocation nobody had asked for.
    """
    runtime = SimulatedRuntime()
    runtime.submit_job(
        name="job-standard-2",
        cpu=2,
        machine_type="n2-standard-2",
        provisioning_model="100% Standard",
        approved_by_operator=True,
    )

    workload = runtime.snapshot().workload
    assert workload.id == "job-standard-2"
    assert workload.observed_cpu == 2
    assert workload.observed_machine_type == "n2-standard-2"
    assert workload.observed_provisioning_mix == "100% Standard"
    assert workload.requested_machine_type == "n2-standard-2"
    assert workload.verification_status == "verified"
    assert "n2-standard-2" in workload.verification_detail
    assert "100% Standard" in workload.verification_detail


def test_submitted_job_accrues_cost_at_its_own_provisioning_rate():
    """A Standard job must not be billed at the Spot discount."""

    def cost_after_ten_minutes(provisioning_model: str) -> float:
        runtime = SimulatedRuntime()
        runtime.submit_job(
            name="job-cost",
            cpu=16,
            machine_type="n2-standard-16",
            provisioning_model=provisioning_model,
            approved_by_operator=True,
        )
        runtime.tick(10)
        return runtime.snapshot().workload.accrued_cost_eur

    standard = cost_after_ten_minutes("100% Standard")
    spot = cost_after_ten_minutes("100% Spot")
    assert spot > 0
    assert standard > spot
