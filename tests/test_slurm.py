import pytest
from agentic_compute.models import Action
from agentic_compute.slurm_adapter import SlurmRuntime


def test_slurm_adapter_snapshot_and_apply(monkeypatch):
    monkeypatch.setenv("MOCK_SLURM", "true")
    # Mock requests.get and requests.post to ensure instant offline test execution
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 404, "text": "not found", "json": lambda self: {}})(),
    )
    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 200, "text": "ok", "json": lambda self: {"job_id": "12345"}})(),
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
    assert runtime.last_slurm_action is not None
    assert runtime.last_slurm_action["requested_cpu"] == 64
    assert runtime.last_slurm_action["status"] == "applied"

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


def test_slurm_adapter_apply_failure_raises(monkeypatch):
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 404, "text": "not found", "json": lambda self: {}})(),
    )
    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 500, "text": "Internal Server Error", "json": lambda self: {}})(),
    )
    runtime = SlurmRuntime()
    initial_cpu = runtime.allocated_cpu

    action = Action(
        action="resize_workload",
        workload_id="1",
        cpu=96,
        reason="scale up on Slurm",
    )
    with pytest.raises(RuntimeError, match="Slurm rejected CPU resize"):
        runtime.apply(action)

    # Verify state was not falsely updated to success
    assert runtime.allocated_cpu == initial_cpu
    assert runtime.last_slurm_action["status"] == "failed"
    assert "Internal Server Error" in runtime.last_slurm_action["error"]


def test_slurm_adapter_submit_failure_raises(monkeypatch):
    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 400, "text": "Invalid partition requested", "json": lambda self: {}})(),
    )
    runtime = SlurmRuntime()

    with pytest.raises(RuntimeError, match="Slurm submission rejected"):
        runtime.submit_job(name="failing-job", partition="non-existent")


def test_slurm_adapter_truthful_job_lifecycle(monkeypatch):
    # 1. Job is RUNNING in Slurm: tick() must not artificially mark job done
    running_job_response = {
        "jobs": [
            {
                "job_id": 999,
                "job_state": ["RUNNING"],
                "job_resources": {"allocated_cpus": 16},
                "time": {"elapsed": 600},
            }
        ]
    }
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 200, "text": "ok", "json": lambda self: running_job_response})(),
    )
    runtime = SlurmRuntime(job_id="999")
    runtime.tick(5.0)

    assert runtime.is_done() is False
    assert runtime.allocated_cpu == 16
    assert runtime.elapsed_minutes == 10.0  # 600s / 60 = 10 min

    # 2. Job is COMPLETED in Slurm: tick() must mark job done
    completed_job_response = {
        "jobs": [
            {
                "job_id": 999,
                "job_state": ["COMPLETED"],
                "job_resources": {"allocated_cpus": 16},
                "time": {"elapsed": 1200},
            }
        ]
    }
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 200, "text": "ok", "json": lambda self: completed_job_response})(),
    )
    runtime.tick(5.0)

    assert runtime.is_done() is True
    assert runtime.snapshot().workload.remaining_work_units == 0.0


def test_slurm_deadline_change_does_not_alter_workload_eta():
    runtime = SlurmRuntime()
    snap1 = runtime.snapshot()
    initial_eta = snap1.workload.estimated_remaining_minutes

    # Double the deadline
    runtime.configure_objective(deadline_minutes=snap1.objective.deadline_at_minutes * 2.0)
    snap2 = runtime.snapshot()

    # Workload ETA must be invariant to user SLA deadline changes
    assert snap2.workload.estimated_remaining_minutes == initial_eta
    assert snap2.objective.deadline_at_minutes == snap1.objective.deadline_at_minutes * 2.0


def test_slurm_duplicate_elapsed_time_does_not_double_cost(monkeypatch):
    # Two successive observations of the same Slurm elapsed time (600s)
    job_response = {
        "jobs": [
            {
                "job_id": 1001,
                "job_state": ["RUNNING"],
                "job_resources": {"allocated_cpus": 16},
                "time": {"elapsed": 600},
            }
        ]
    }
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 200, "text": "ok", "json": lambda self: job_response})(),
    )
    runtime = SlurmRuntime(job_id="1001")
    runtime.tick(5.0)
    cost_first_obs = runtime.accrued_cost_eur
    assert cost_first_obs > 0.0

    # Second observation with the exact same elapsed time (600s)
    runtime.tick(5.0)
    cost_second_obs = runtime.accrued_cost_eur

    # Cost must not double or accrue when Slurm elapsed time has not advanced
    assert cost_second_obs == cost_first_obs


def test_slurm_unreachable_without_mock_raises_error(monkeypatch):
    # Slurm cluster is down/unreachable and MOCK_SLURM is not enabled
    monkeypatch.delenv("MOCK_SLURM", raising=False)
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 503, "text": "Service Unavailable", "json": lambda self: {}})(),
    )
    runtime = SlurmRuntime(job_id="real-cluster-job")
    with pytest.raises(RuntimeError, match="Slurm cluster is unreachable"):
        runtime.tick(5.0)


