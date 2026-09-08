from agentic_compute.slurm_adapter import SlurmRuntime
from agentic_compute.models import Action


def test_slurm_adapter_snapshot_and_apply(monkeypatch):
    # Mock requests.get to ensure instant offline test execution
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 404, "json": lambda self: {}})(),
    )
    runtime = SlurmRuntime()
    snapshot = runtime.snapshot()
    assert snapshot.cluster.total_cpu == 128
    assert snapshot.workload.id == "1"
    assert len(snapshot.candidate_allocations) > 0

    # Test applying resize action
    action = Action(
        action="resize_workload",
        workload_id="1",
        cpu=64,
        reason="scale up on Slurm",
    )
    runtime.apply(action)
    updated = runtime.snapshot()
    assert updated.workload.allocated_cpu == 64

    # Test tick progress burn-down
    runtime.tick(5.0)
    ticked = runtime.snapshot()
    assert ticked.workload.remaining_work_units < 100.0
    assert ticked.cluster.current_time_minutes == 5.0

    # Test submit_job with custom cpu
    job_res = runtime.submit_job(name="custom-workload", cpu=48, gpu=2, partition="debug")
    assert job_res["allocated_cpu"] == 48
    assert job_res["allocated_gpu"] == 2
    assert job_res["partition"] == "debug"
    assert runtime.snapshot().workload.id == job_res["job_id"]
    assert runtime.snapshot().workload.allocated_cpu == 48

    # Test submit_job with auto/None cpu (defaults to baseline 4)
    job_auto = runtime.submit_job(name="auto-workload", cpu=None)
    assert job_auto["allocated_cpu"] == 4
    assert runtime.snapshot().workload.allocated_cpu == 4

    # Test configure_objective
    runtime.configure_objective(deadline_minutes=50.0, max_cost_eur=15.0, minimize_cost=False)
    snap = runtime.snapshot()
    assert snap.objective.deadline_at_minutes == 50.0
    assert snap.objective.max_cost_eur == 15.0
    assert snap.objective.minimize_cost is False

