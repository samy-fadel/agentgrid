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
