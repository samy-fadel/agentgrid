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

    # Test applying resize action on a pending job
    runtime.job_status = "PENDING"
    action = Action(
        action="resize_workload",
        workload_id="1",
        cpu=64,
        machine_type="c2-standard-60",
        provisioning_model="100% Standard",
        reason="scale up on Slurm",
    )
    runtime.apply(action)
    # HTTP 200 places job in pending_verification without prematurely altering observed allocation
    assert runtime.last_slurm_action is not None
    assert runtime.last_slurm_action["status"] == "pending_verification"
    assert runtime.allocated_cpu == 4

    # Controller confirms allocation
    runtime._sync_job_state({
        "job_id": "1",
        "job_resources": {"allocated_cpus": 64},
        "partition": "compute",
    })
    updated = runtime.snapshot()
    assert updated.workload.allocated_cpu == 64
    assert runtime.last_slurm_action["status"] == "applied"
    assert updated.workload.verification_status == "verified"

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
    runtime.job_status = "PENDING"
    initial_cpu = runtime.allocated_cpu

    action = Action(
        action="resize_workload",
        workload_id="1",
        cpu=96,
        machine_type="c2-standard-60",
        provisioning_model="100% Standard",
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


def test_slurm_apply_payload_includes_machine_type_and_provisioning_mix(monkeypatch):
    captured_payloads = []

    def mock_post(url, *args, **kwargs):
        captured_payloads.append(kwargs.get("json", {}))
        return type("MockResponse", (), {"status_code": 200, "text": "ok", "json": lambda self: {"job_id": "777"}})()

    monkeypatch.setattr("requests.post", mock_post)
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 200, "text": "ok", "json": lambda self: {"jobs": []}})(),
    )
    runtime = SlurmRuntime(job_id="777")
    runtime.job_status = "PENDING"
    action = Action(
        action="resize_workload",
        workload_id="777",
        cpu=60,
        machine_type="c2-standard-60",
        provisioning_model="100% Standard",
        reason="scale up to c2 standard on compute partition",
    )
    runtime.apply(action)

    assert len(captured_payloads) == 1
    job_payload = captured_payloads[0].get("job", {})
    assert job_payload.get("cpus_per_task") == 60
    assert job_payload.get("features") == "c2-standard-60"
    assert job_payload.get("constraints") == "c2-standard-60"
    assert job_payload.get("partition") == "compute"
    assert "machine_type=c2-standard-60" in job_payload.get("comment", "")
    assert "provisioning_model=100% Standard" in job_payload.get("comment", "")
    assert runtime.last_slurm_action["status"] == "pending_verification"

    # When Slurm telemetry confirms the new allocation
    runtime._sync_job_state({
        "job_id": "777",
        "job_resources": {"allocated_cpus": 60},
        "partition": "compute",
    })
    assert runtime.machine_type == "c2-standard-60"
    assert runtime.provisioning_mix == "100% Standard"
    assert runtime.last_slurm_action["status"] == "applied"


def test_slurm_apply_failure_preserves_previous_metadata(monkeypatch):
    monkeypatch.delenv("MOCK_SLURM", raising=False)
    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 500, "text": "Slurm Controller Error", "json": lambda self: {}})(),
    )
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 200, "text": "ok", "json": lambda self: {"jobs": []}})(),
    )
    runtime = SlurmRuntime(job_id="888")
    runtime.job_status = "PENDING"
    original_mtype = runtime.machine_type
    original_mix = runtime.provisioning_mix
    original_cpu = runtime.allocated_cpu

    action = Action(
        action="resize_workload",
        workload_id="888",
        cpu=60,
        machine_type="c2-standard-60",
        provisioning_model="100% Standard",
        reason="scale attempt that fails",
    )

    with pytest.raises(RuntimeError, match="Slurm rejected CPU resize"):
        runtime.apply(action)

    # Local state must NOT be mutated to fictitious configuration upon failure
    assert runtime.allocated_cpu == original_cpu
    assert runtime.machine_type == original_mtype
    assert runtime.provisioning_mix == original_mix
    assert runtime.last_slurm_action["status"] == "failed"


def test_slurm_completed_job_accrues_final_elapsed_delta_and_cost(monkeypatch):
    # Step 1: Job is RUNNING at 10 minutes (elapsed = 600s)
    running_resp = {
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
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 200, "text": "ok", "json": lambda self: running_resp})(),
    )
    runtime = SlurmRuntime(job_id="999")
    runtime.tick(5.0)

    cost_at_10m = runtime.accrued_cost_eur
    assert runtime.elapsed_minutes == 10.0
    assert cost_at_10m > 0.0
    assert runtime.is_done() is False

    # Step 2: Job transitions to COMPLETED at 20 minutes (elapsed = 1200s)
    completed_resp = {
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
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 200, "text": "ok", "json": lambda self: completed_resp})(),
    )
    runtime.tick(5.0)

    # Final elapsed time and cost delta MUST be accounted for upon terminal tick
    assert runtime.is_done() is True
    assert runtime.elapsed_minutes == 20.0
    assert runtime.accrued_cost_eur > cost_at_10m
    # 1200s is 2x 600s, so accrued cost should be exactly 2x
    assert round(runtime.accrued_cost_eur, 5) == round(cost_at_10m * 2.0, 5)
    snapshot = runtime.snapshot()
    assert snapshot.workload.done is True
    assert snapshot.workload.failed is False
    assert snapshot.workload.status == "COMPLETED"
    assert snapshot.workload.remaining_work_units == 0.0


def test_slurm_failed_job_exposed_truthfully(monkeypatch):
    # Job failed in Slurm at 12 minutes with remaining work
    failed_resp = {
        "jobs": [
            {
                "job_id": 999,
                "job_state": ["FAILED"],
                "job_resources": {"allocated_cpus": 16},
                "time": {"elapsed": 720},
            }
        ]
    }
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 200, "text": "ok", "json": lambda self: failed_resp})(),
    )
    runtime = SlurmRuntime(job_id="999")
    runtime.tick(5.0)

    assert runtime.is_done() is True
    assert runtime.job_failed is True
    assert runtime.job_status == "FAILED"
    # Remaining work units must NOT be zeroed out for failed jobs
    assert runtime.remaining_work_units > 0.0

    snap = runtime.snapshot()
    assert snap.workload.done is True
    assert snap.workload.failed is True
    assert snap.workload.status == "FAILED"
    assert snap.workload.remaining_work_units > 0.0


def test_slurm_unsupported_configuration_rejected(monkeypatch):
    monkeypatch.delenv("MOCK_SLURM", raising=False)
    runtime = SlurmRuntime(job_id="901")
    runtime.job_status = "PENDING"
    original_cpu = runtime.allocated_cpu
    original_mtype = runtime.machine_type
    original_mix = runtime.provisioning_mix

    # 1. Unsupported machine type
    action_mtype = Action(
        action="resize_workload",
        workload_id="901",
        cpu=64,
        machine_type="n4-standard-64",
        provisioning_model="100% Standard",
        reason="request unsupported N4",
    )
    with pytest.raises(RuntimeError, match="Unsupported configuration on Slurm cluster"):
        runtime.apply(action_mtype)

    assert runtime.verification_status == "unsupported"
    assert runtime.last_slurm_action["status"] == "unsupported"
    assert runtime.allocated_cpu == original_cpu
    assert runtime.machine_type == original_mtype

    # 2. Unsupported Spot mix on Slurm cluster
    action_spot = Action(
        action="resize_workload",
        workload_id="901",
        cpu=60,
        machine_type="c2-standard-60",
        provisioning_model="80% Spot / 20% Standard",
        reason="request unsupported spot mix",
    )
    with pytest.raises(RuntimeError, match="Unsupported configuration on Slurm cluster"):
        runtime.apply(action_spot)

    assert runtime.verification_status == "unsupported"
    assert runtime.last_slurm_action["status"] == "unsupported"
    assert runtime.allocated_cpu == original_cpu
    assert runtime.provisioning_mix == original_mix


def test_slurm_running_job_hot_resize_rejected(monkeypatch):
    runtime = SlurmRuntime(job_id="902")
    runtime.job_status = "RUNNING"
    original_cpu = runtime.allocated_cpu

    action = Action(
        action="resize_workload",
        workload_id="902",
        cpu=60,
        machine_type="c2-standard-60",
        provisioning_model="100% Standard",
        reason="hot-resize running job",
    )
    with pytest.raises(RuntimeError, match="Hot-resizing an active running Slurm job"):
        runtime.apply(action)

    assert runtime.verification_status == "unsupported"
    assert runtime.last_slurm_action["status"] == "unsupported"
    assert runtime.allocated_cpu == original_cpu


def test_slurm_pending_job_http_200_remains_pending_verification(monkeypatch):
    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 200, "text": "ok", "json": lambda self: {}})(),
    )
    monkeypatch.setattr(
        "requests.get",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 200, "text": "ok", "json": lambda self: {"jobs": []}})(),
    )
    runtime = SlurmRuntime(job_id="903")
    runtime.job_status = "PENDING"
    assert runtime.allocated_cpu == 4

    action = Action(
        action="resize_workload",
        workload_id="903",
        cpu=60,
        machine_type="c2-standard-60",
        provisioning_model="100% Standard",
        reason="resize pending job",
    )
    runtime.apply(action)

    # HTTP 200 from Slurm must NOT claim applied yet
    assert runtime.verification_status == "pending_verification"
    assert runtime.last_slurm_action["status"] == "pending_verification"
    assert runtime.allocated_cpu == 4  # Observed CPU has NOT changed
    assert runtime.requested_cpu == 60
    snap = runtime.snapshot()
    assert snap.workload.allocated_cpu == 4
    assert snap.workload.requested_cpu == 60
    assert snap.workload.verification_status == "pending_verification"


def test_slurm_observed_change_transitions_to_applied_and_verified(monkeypatch):
    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 200, "text": "ok", "json": lambda self: {}})(),
    )
    runtime = SlurmRuntime(job_id="904")
    runtime.job_status = "PENDING"
    action = Action(
        action="resize_workload",
        workload_id="904",
        cpu=60,
        machine_type="c2-standard-60",
        provisioning_model="100% Standard",
        reason="resize pending job",
    )
    runtime.apply(action)
    assert runtime.last_slurm_action["status"] == "pending_verification"

    # Authoritative telemetry arrives from Slurm showing allocated_cpus: 60
    runtime._sync_job_state({
        "job_id": 904,
        "job_state": ["RUNNING"],
        "job_resources": {"allocated_cpus": 60},
        "partition": "compute",
    })

    assert runtime.verification_status == "verified"
    assert runtime.last_slurm_action["status"] == "applied"
    assert runtime.allocated_cpu == 60
    assert runtime.machine_type == "c2-standard-60"
    assert runtime.snapshot().workload.allocated_cpu == 60


def test_slurm_cost_accrual_strictly_depends_on_observed_allocation(monkeypatch):
    runtime = SlurmRuntime(job_id="905")
    runtime.job_status = "PENDING"
    runtime.allocated_cpu = 4
    runtime.cpu_cost_per_hour_eur = 0.05
    runtime.provisioning_mix = "100% Standard"

    # Resize requested to 60 CPUs, remains pending
    monkeypatch.setattr(
        "requests.post",
        lambda *args, **kwargs: type("MockResponse", (), {"status_code": 200, "text": "ok", "json": lambda self: {}})(),
    )
    action = Action(
        action="resize_workload",
        workload_id="905",
        cpu=60,
        machine_type="c2-standard-60",
        provisioning_model="100% Standard",
        reason="request 60 CPUs",
    )
    runtime.apply(action)
    assert runtime.verification_status == "pending_verification"

    # Simulate 1 hour elapsed (3600 seconds) observed with 4 CPUs
    runtime._sync_job_state({
        "job_id": 905,
        "job_state": ["RUNNING"],
        "job_resources": {"allocated_cpus": 4},
        "time": {"elapsed": 3600},
    })

    # Expected cost: 4 vCPUs * 0.05 EUR/h * 1.0 factor * 1.0 h = 0.20 EUR
    # If it had used requested_cpu (60), cost would have been 3.00 EUR.
    assert round(runtime.accrued_cost_eur, 4) == 0.2000
    assert runtime.snapshot().workload.cost_basis == "observed_allocation"



