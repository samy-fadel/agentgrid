"""Cost must be counted once per attempt, and never claimed twice.

Feature 6 promises "cout reel, historique et comparaison aux estimations". That
promise only holds if the arithmetic is right across retries, across a process
restart, and across repeated persistence of the same attempt.

Three defects are pinned here:

* ``finish_attempt`` never refreshed the cumulative totals, which were computed
  only inside ``update_progress``. A workload that finished without a progress
  poll reported the cost of one attempt while several had been paid. The MCP
  tracking tool returns exactly that field.
* a retried attempt recorded ``checkpoint_recovered_from =
  "<location>/step_latest"``, a path assembled from a template that nobody had
  looked for, so the history claimed a recovery that never happened.
* a plan without a computed ETA made the history compare an observed duration
  against "0 minutes estimated".
"""

from __future__ import annotations

import pytest

from agentic_compute.history import get_history_store
from agentic_compute.lifecycle_manager import LifecycleManager

PLAN = {
    "plan_id": "plan-cost-journey",
    "plan_type": "cost_optimized",
    "title": "cost journey",
    "machine_type": "n2-standard-8",
    "cpu": 8,
    "estimated_cost_eur": 4.0,
}


def _profile(workload_id: str, checkpoint_location: str | None) -> dict:
    return {
        "workload_id": workload_id,
        "name": "cost journey",
        "cpu_requested": 8,
        "estimated_duration_minutes": 60,
        "is_interruptible": True,
        "supports_checkpointing": True,
        "checkpoint_location": checkpoint_location,
        "max_retries": 3,
    }


def test_two_attempts_are_summed_once_each_and_survive_a_restart(tmp_path):
    manager = LifecycleManager()
    workload_id = "wl-cost-journey"
    manager.register_workload(_profile(workload_id, str(tmp_path / "ckpt")))

    manager.start_attempt(workload_id, plan=PLAN, job_id="job-a")
    manager.update_progress(
        workload_id, progress_percent=50, job_state="RUNNING",
        elapsed_minutes=30, cost_incurred_eur=2.0,
    )
    manager.finish_attempt(
        workload_id, "PREEMPTED", failure_reason="spot reclaimed",
        total_cost_eur=3.0, actual_duration_minutes=45,
    )

    # The defect: this reported 2.0, the last polled figure, not the 3.0 paid.
    assert manager._workloads[workload_id]["total_calculated_cost_eur"] == pytest.approx(3.0)

    manager.start_attempt(workload_id, plan=PLAN, job_id="job-b")
    manager.finish_attempt(
        workload_id, "COMPLETED", total_cost_eur=2.0, actual_duration_minutes=20,
    )

    assert manager._workloads[workload_id]["total_calculated_cost_eur"] == pytest.approx(5.0)
    assert manager._workloads[workload_id]["total_elapsed_minutes"] == pytest.approx(65.0)

    store = get_history_store()
    record = store.get_history_record(workload_id)
    assert record.final_calculated_cost_eur == pytest.approx(5.0)
    assert len(record.attempts) == 2
    assert len(store.get_attempts(workload_id)) == 2, (
        "each attempt is persisted twice (start and finish); an INSERT instead of "
        "an upsert would double every cost"
    )

    # A restart must reach the same total, not a doubled one.
    restarted = LifecycleManager()
    rec = restarted._require(workload_id)
    assert rec["total_calculated_cost_eur"] == pytest.approx(5.0)
    assert len(rec["attempts"]) == 2

    # And re-syncing after the restart must not add the attempts a second time.
    restarted.update_progress(workload_id, elapsed_minutes=20, cost_incurred_eur=2.0)
    assert store.get_history_record(workload_id).final_calculated_cost_eur == pytest.approx(5.0)
    assert len(store.get_attempts(workload_id)) == 2


def test_the_cost_comparison_uses_a_real_estimate_not_zero(tmp_path):
    manager = LifecycleManager()
    workload_id = "wl-cost-estimate"
    manager.register_workload(_profile(workload_id, str(tmp_path / "ckpt")))
    manager.start_attempt(workload_id, plan=PLAN, job_id="job-a")
    manager.finish_attempt(
        workload_id, "COMPLETED", total_cost_eur=5.0, actual_duration_minutes=65,
    )

    comparison = get_history_store().reconcile_costs(workload_id)
    assert comparison["cost_breakdown"]["initial_estimated_cost_eur"] == pytest.approx(4.0)
    assert comparison["cost_breakdown"]["observed_calculated_cost_eur"] == pytest.approx(5.0)
    assert comparison["cost_breakdown"]["cost_delta_eur"] == pytest.approx(1.0)
    # PLAN carries no ETA, so the profile's 60 minutes is the honest estimate.
    assert comparison["duration_breakdown"]["estimated_duration_minutes"] == pytest.approx(60.0)
    assert comparison["duration_breakdown"]["duration_delta_minutes"] == pytest.approx(5.0)
    # Billing is not integrated; the report must say so rather than imply it is.
    assert comparison["billed_reconciliation_status"] == "source_not_integrated"
    assert comparison["cost_tiers"]["tier3_reconciled_billed_eur"] is None


def test_a_retry_does_not_claim_a_recovery_that_never_happened(tmp_path):
    """The second attempt used to record '<location>/step_latest' unconditionally."""
    location = tmp_path / "never-written"
    manager = LifecycleManager()
    workload_id = "wl-cost-fake-recovery"
    manager.register_workload(_profile(workload_id, str(location)))

    manager.start_attempt(workload_id, plan=PLAN, job_id="job-a")
    manager.finish_attempt(workload_id, "PREEMPTED", failure_reason="reclaimed")
    second = manager.start_attempt(workload_id, plan=PLAN, job_id="job-b")

    assert second.checkpoint_recovered_from is None, (
        f"attempt 2 claims recovery from {second.checkpoint_recovered_from!r}, but "
        "nothing was ever written there"
    )


def test_a_retry_records_the_recovery_when_the_checkpoint_is_really_there(tmp_path):
    location = tmp_path / "ckpt"
    location.mkdir()
    manager = LifecycleManager()
    workload_id = "wl-cost-real-recovery"
    manager.register_workload(_profile(workload_id, str(location)))

    manager.start_attempt(workload_id, plan=PLAN, job_id="job-a")
    manager.finish_attempt(workload_id, "PREEMPTED", failure_reason="reclaimed")
    (location / "step_000123.pt").write_bytes(b"state")

    second = manager.start_attempt(workload_id, plan=PLAN, job_id="job-b")
    assert second.checkpoint_recovered_from == str(location)


def test_an_explicit_resume_from_a_missing_checkpoint_is_refused(tmp_path):
    location = tmp_path / "never-written"
    manager = LifecycleManager()
    workload_id = "wl-cost-bad-resume"
    manager.register_workload(_profile(workload_id, str(location)))
    manager.start_attempt(workload_id, plan=PLAN, job_id="job-a")
    manager.finish_attempt(workload_id, "PREEMPTED", failure_reason="reclaimed")

    with pytest.raises(ValueError, match="does not exist"):
        manager.start_attempt(
            workload_id, plan=PLAN, job_id="job-b",
            checkpoint_recovered_from=str(location / "step_latest"),
        )


def test_a_figure_stated_by_the_caller_is_not_recorded_as_a_measurement(tmp_path):
    """A language model can produce a plausible cost. It must not become a measurement."""
    manager = LifecycleManager()
    workload_id = "wl-declared"
    manager.register_workload(_profile(workload_id, str(tmp_path / "ckpt")))
    manager.start_attempt(workload_id, plan=PLAN, job_id="job-a")

    manager.update_progress(
        workload_id, progress_percent=73.0, cost_incurred_eur=1.23,
        metrics_source="caller_declared",
    )
    rec = manager._workloads[workload_id]
    assert rec["progress_status"] == "declared_unverified"
    assert rec["current_attempt"].cost_status == "estimated"

    # The same figure read from the runtime is a measurement.
    manager.update_progress(
        workload_id, progress_percent=73.0, cost_incurred_eur=1.23,
        metrics_source="observed",
    )
    rec = manager._workloads[workload_id]
    assert rec["progress_status"] == "measured"
    assert rec["current_attempt"].cost_status == "calculated_from_usage"


def test_the_mcp_tracking_tool_labels_its_input_as_declared(tmp_path):
    import agentic_compute.mcp_server as mcp_server

    manager = mcp_server.default_lifecycle_manager
    workload_id = "wl-mcp-declared"
    manager.register_workload(_profile(workload_id, str(tmp_path / "ckpt")))
    manager.start_attempt(workload_id, plan=PLAN, job_id="job-a")

    out = mcp_server.track_workload_lifecycle(
        workload_id, progress_percent=90.0, cost_incurred_eur=42.0
    )
    assert out["status"] == "tracked"
    assert out["metrics_source"] == "caller_declared"
    assert out["progress_status"] == "declared_unverified"
    assert "not measured" in out["metrics_caveat"]


def test_tracking_an_unknown_workload_says_so_instead_of_inventing_one():
    import agentic_compute.mcp_server as mcp_server

    out = mcp_server.track_workload_lifecycle("wl-never-defined")
    assert out["status"] == "unknown_workload"
    assert "is registered or persisted" in out["reason"]


def test_tracking_after_a_restart_does_not_erase_the_recorded_run(tmp_path):
    """The defect: cost 7.50 EUR / 1 attempt / COMPLETED became 0.00 / 0 / DEFINED."""
    import agentic_compute.mcp_server as mcp_server

    workload_id = "wl-restart-track"
    manager = LifecycleManager()
    manager.register_workload(_profile(workload_id, str(tmp_path / "ckpt")))
    manager.start_attempt(workload_id, plan=PLAN, job_id="job-a")
    manager.finish_attempt(
        workload_id, "COMPLETED", total_cost_eur=7.5, actual_duration_minutes=50,
    )

    store = get_history_store()
    before = store.get_history_record(workload_id)
    assert before.final_calculated_cost_eur == pytest.approx(7.5)
    assert before.final_status == "COMPLETED"

    # Simulate the restart: the MCP process has an empty in-memory cache.
    mcp_server.default_lifecycle_manager._workloads.clear()
    out = mcp_server.track_workload_lifecycle(workload_id)
    assert out["status"] == "tracked"
    assert out["state"] == "COMPLETED"

    after = store.get_history_record(workload_id)
    assert after.final_status == "COMPLETED"
    assert after.workload_name == before.workload_name
    assert len(after.attempts) == len(before.attempts) == 1
    assert after.final_calculated_cost_eur == pytest.approx(7.5), (
        "a read-only tracking call must not destroy the recorded cost"
    )
