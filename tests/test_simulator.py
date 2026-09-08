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
