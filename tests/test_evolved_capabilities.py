from __future__ import annotations

import os
import tempfile
import time
import pytest
from unittest.mock import MagicMock

from agentic_compute.capacity_search import search_compatible_capacity
from agentic_compute.diagnostic import diagnose_blockers
from agentic_compute.execution_controller import ExecutionController
from agentic_compute.history import HistoryStore
from agentic_compute.lifecycle_manager import LifecycleManager
from agentic_compute.models import (
    Action,
    DelegationPolicy,
    ExecutionAttempt,
    ExecutionHistoryRecord,
    ExecutionPlan,
    WorkloadProfile,
)
from agentic_compute.plan_engine import evaluate_and_compare_plans
from agentic_compute.simulator import SimulatedRuntime
from agentic_compute.slurm_adapter import SlurmRuntime


def test_scenario_1_existing_regression_baseline():
    """Scenario 1: Baseline endpoints and runtime contract remain preserved without regression."""
    runtime = SimulatedRuntime()
    snapshot = runtime.snapshot()
    assert snapshot.cluster.total_cpu == 128
    assert snapshot.workload.allocated_cpu == 32
    assert len(snapshot.candidate_allocations) > 0

    # Ensure default delegation control mode permits standard apply without regression
    runtime.apply(Action(action="resize_workload", workload_id="mc-001", cpu=16, reason="baseline_test"))
    updated = runtime.snapshot()
    assert updated.workload.allocated_cpu == 16


def test_scenario_2_nominal_end_to_end_journey():
    """Scenario 2: Full nominal path: Profile -> Search -> 3 Plans -> Validation Approval -> Execution -> Lifecycle -> Cost Reconciliation."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        store = HistoryStore(db_path=db_path)
        mgr = LifecycleManager(history_store=store)
        runtime = SimulatedRuntime()
        controller = ExecutionController(runtime)

        # 1. Declare complete workload profile
        profile = WorkloadProfile(
            workload_id="wl-nominal-42",
            name="genomics-batch-pipeline",
            cpu_requested=8,
            memory_mb_requested=32768,
            budget_amount=25.0,
            deadline_minutes_from_start=90.0,
            allowed_regions=["europe-west1"],
            allow_spot=True,
            allow_fallback_to_standard=True,
            supports_checkpointing=True,
            checkpoint_location="gs://agentgrid-checkpoints/wl-nominal-42",
        )
        mgr.register_workload(profile)

        # 2. Search capacity across 4 stages
        candidates = search_compatible_capacity(profile=profile, demo_mode=True)
        assert len(candidates) > 0
        stages = {c.state_stage for c in candidates}
        assert "capacity_estimated" in stages or "quota_authorized" in stages

        # 3. Deterministically generate 3 distinct execution plans
        plan_result = evaluate_and_compare_plans(profile=profile, cluster_total_cpu=128, demo_mode=True)
        assert plan_result["is_feasible"] is True
        plans = plan_result["plans"]
        assert len(plans) == 3
        plan_types = {p["plan_type"] for p in plans}
        assert plan_types == {"cost_optimized", "deadline_favored", "balanced_tradeoff"}

        # 4. Human validation approval.
        #    The history store records the plan for reporting, but authorisation
        #    lives in the governance store: that is the only source the execution
        #    gate trusts. Approving only in history must NOT unlock execution.
        selected_plan_dict = plans[0]
        selected_plan = ExecutionPlan(**selected_plan_dict)
        store.save_execution_plan(selected_plan, workload_id=profile.workload_id)
        store.approve_execution_plan(selected_plan.plan_id)

        premature = controller.submit_plan(
            profile=profile,
            plan=selected_plan,
            control_mode="validation",
            is_operator_approved=True,
            approved_plan_id=selected_plan.plan_id,
            runtime=runtime,
        )
        assert premature["status"] == "blocked", premature
        assert "caller-supplied approval flags are ignored" in premature["reason"].lower()

        gov = controller.governance
        gov.register_plan(profile.workload_id, selected_plan, command=profile.command or profile.script)
        approval = gov.approve_plan(
            selected_plan.plan_id, workload_id=profile.workload_id, approved_by="operator-under-test"
        )
        assert approval["status"] == "approved", approval

        # 5. Execute the exact plan the operator approved.
        exec_res = controller.submit_plan(
            profile=profile,
            plan=selected_plan,
            control_mode="validation",
            runtime=runtime,
        )
        assert exec_res["status"] == "submitted", exec_res
        assert exec_res["verified"] is True
        assert exec_res["job_id"]

        # 6. Lifecycle tracking: Queued -> Running -> Progress -> Completed
        att = mgr.start_attempt(workload_id=profile.workload_id, plan=selected_plan, job_id=exec_res["job_id"])
        assert att.attempt_number == 1

        mgr.update_progress(
            workload_id=profile.workload_id,
            progress_percent=50.0,
            job_state="RUNNING",
            elapsed_minutes=15.0,
            cost_incurred_eur=3.50,
        )
        rec = mgr.finish_attempt(
            workload_id=profile.workload_id,
            final_status="COMPLETED",
            total_cost_eur=7.20,
            actual_duration_minutes=31.0,
        )
        assert rec["state"] == "COMPLETED"
        assert rec["progress_percent"] == 100.0

        # 7. Reconcile costs & variance vs estimate
        comp = store.reconcile_costs(profile.workload_id, billed_cost_eur=7.15)
        assert comp["workload_id"] == profile.workload_id
        assert comp["cost_breakdown"]["observed_calculated_cost_eur"] == 7.20
        assert comp["cost_breakdown"]["reconciled_billed_cost_eur"] == 7.15
        assert comp["cost_breakdown"]["reconciliation_status"] == "reconciled_billed"
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_scenario_3_capacity_shortage_and_compatible_alternatives():
    """Scenario 3: Distinguish temporary capacity shortage (stockout) from permanent incompatible configuration."""
    # 1. Capacity shortage (temporary stockout / preemption)
    findings_shortage = diagnose_blockers(
        gcp_error="ZONE_RESOURCE_POOL_EXHAUSTED: The resource pool of n2-standard-32 instances is exhausted",
    )
    assert len(findings_shortage) > 0
    item1 = findings_shortage[0]
    assert item1["category"] == "capacity_shortage"
    assert item1["confirmed"] is True
    assert any("alternative machine" in act["action"].lower() or "zone" in act["action"].lower() for act in item1["possible_actions"])

    # 2. Incompatible configuration (permanent configuration error)
    findings_incompat = diagnose_blockers(
        gcp_error="INVALID_ARGUMENT: Machine type custom-999-123 is not supported in zone us-central1-a",
    )
    assert len(findings_incompat) > 0
    item2 = findings_incompat[0]
    assert item2["category"] == "incompatible_configuration"
    assert item2["confirmed"] is True


def test_scenario_4_quota_insufficient_vs_physical_capacity():
    """Scenario 4: Quota insufficient clearly identified from physical capacity, with remedies and no futile submits."""
    findings = diagnose_blockers(
        gcp_error="QUOTA_EXCEEDED: Quota 'CPUS' exceeded. Limit: 256.0 in region us-central1",
    )
    assert len(findings) > 0
    item = findings[0]
    assert item["category"] == "quota"
    assert item["source"] == "gcp_compute_quota"
    assert item["confirmed"] is True
    assert any("increase" in act["action"].lower() or "region" in act["action"].lower() for act in item["possible_actions"])

    # Capacity search flags quota status
    os.environ["GCP_PROJECT_QUOTA_CPUS"] = "64"
    os.environ["GCP_PROJECT_QUOTA_USED"] = "60"
    try:
        profile = WorkloadProfile(workload_id="wl-quota", cpu_requested=16, allowed_regions=["us-central1"])
        cands = search_compatible_capacity(profile=profile, demo_mode=True)
        exceeded_cands = [c for c in cands if c.quota_status == "QUOTA_EXCEEDED"]
        assert len(exceeded_cands) > 0
        # When quota is exceeded, stage remains catalog_proposed (not quota_authorized)
        for c in exceeded_cands:
            assert c.state_stage == "catalog_proposed"
    finally:
        os.environ.pop("GCP_PROJECT_QUOTA_CPUS", None)
        os.environ.pop("GCP_PROJECT_QUOTA_USED", None)


def test_scenario_5_unfeasible_workload_deterministic_explanation():
    """Scenario 5: Unfeasible workload produces deterministic explanation of blocking constraints without inventing a winner."""
    impossible_profile = WorkloadProfile(
        workload_id="wl-impossible",
        budget_amount=0.01,
        deadline_minutes_from_start=0.5,
        cpu_requested=64,
    )
    result = evaluate_and_compare_plans(impossible_profile, cluster_total_cpu=128, demo_mode=True)
    assert result["is_feasible"] is False
    assert result["plans"] == []
    assert result["recommended_plan_id"] is None
    assert "unfeasible_explanation" in result
    explanation = result["unfeasible_explanation"]
    assert "budget constraint violated" in explanation.lower()
    assert "deadline constraint violated" in explanation.lower()
    assert "suggested relaxations" in explanation.lower()


def test_scenario_6_missing_telemetry_visible_uncertainty():
    """Scenario 6: Missing or unavailable telemetry displays explicit uncertainty without silent fallback to demo data."""
    # When demo_mode=False and no credentials/mock, signals report unavailable/unknown explicitly
    profile = WorkloadProfile(workload_id="wl-uncertain", cpu_requested=4, allowed_regions=["us-central1"])
    # Pass demo_mode=False explicitly to test truthful telemetry handling
    candidates = search_compatible_capacity(profile=profile, demo_mode=False)
    assert len(candidates) > 0
    # Every candidate must have explicit truthful provenance
    for c in candidates:
        assert c.data_provenance in ("gcp_live_api", "unavailable", "unknown")
        assert c.data_provenance != "simulated_demo"

    # Lifecycle unmeasured progress
    mgr = LifecycleManager()
    mgr.register_workload(profile)
    rec = mgr.update_progress(workload_id=profile.workload_id, progress_percent=None)
    assert rec["progress_status"] == "unavailable"
    assert rec["progress_percent"] is None


def test_scenario_7_human_control_modes_strict_enforcement():
    """Scenario 7: Advisory / Validation / Delegation enforced from server state.

    Reworked from the original, which unlocked validation mode with
    ``is_operator_approved=True`` and entered delegation mode simply by asking
    for it. Both are caller-controlled, so neither demonstrated enforcement.
    The scenario now records approvals and modes through ``GovernanceStore`` and
    adds an escalation check: a caller must not be able to widen the operator's
    mode.
    """
    runtime = SimulatedRuntime()
    controller = ExecutionController(runtime)
    gov = controller.governance
    plan = ExecutionPlan(
        plan_id="plan-test-7",
        title="Test Plan",
        plan_type="cost_optimized",
        cpu=16,
        machine_type="n2-standard-16",
        provisioning_model="100% Spot",
        estimated_cost_eur=15.0,
    )
    profile = WorkloadProfile(workload_id="wl-gov-7", cpu_requested=16)

    # 1. Mode Conseil (Advisory): Strictly blocks mutations
    res_advisory = controller.submit_plan(profile=profile, plan=plan, control_mode="advisory", runtime=runtime)
    assert res_advisory["status"] == "blocked"
    assert "advisory mode is read-only" in res_advisory["reason"].lower()

    # Also at runtime adapter level
    runtime.set_control_mode("advisory")
    with pytest.raises(PermissionError, match="advisory.*mode"):
        runtime.apply(Action(action="resize_workload", workload_id="mc-001", cpu=16, reason="test"))
    runtime.set_control_mode("delegation")

    # 2. Mode Validation: requires an approval recorded on the server.
    res_val_unapproved = controller.submit_plan(
        profile=profile,
        plan=plan,
        control_mode="validation",
        is_operator_approved=False,
        runtime=runtime,
    )
    assert res_val_unapproved["status"] == "blocked"
    assert "not registered on the server" in res_val_unapproved["reason"].lower()

    # Registering a plan is not approving it.
    gov.register_plan(profile.workload_id, plan)
    res_registered_only = controller.submit_plan(
        profile=profile, plan=plan, control_mode="validation", runtime=runtime
    )
    assert res_registered_only["status"] == "blocked"
    assert "approval" in res_registered_only["reason"].lower()

    gov.approve_plan(plan.plan_id, workload_id=profile.workload_id)
    res_val_approved = controller.submit_plan(
        profile=profile,
        plan=plan,
        control_mode="validation",
        runtime=runtime,
    )
    assert res_val_approved["status"] == "submitted", res_val_approved

    # 2b. No escalation: with the server on its default (validation), a caller
    #     asking for delegation must not obtain autonomous execution.
    escalating = controller.submit_plan(
        profile=WorkloadProfile(workload_id="wl-gov-7-escalate", cpu_requested=16),
        plan=plan,
        control_mode="delegation",
        delegation_policy=DelegationPolicy(max_budget_eur=9999.0),
        runtime=runtime,
    )
    assert escalating["status"] == "blocked", escalating
    assert escalating["control_mode"] == "validation"

    # 3. Mode Délégation: policy-bounded autonomy, configured by the operator.
    profile_ok = WorkloadProfile(workload_id="wl-gov-7c", cpu_requested=16)
    gov.set_workload_control(profile_ok.workload_id, "delegation", DelegationPolicy(max_budget_eur=20.0))
    res_del_ok = controller.submit_plan(
        profile=profile_ok,
        plan=plan,
        control_mode="delegation",
        delegation_policy=DelegationPolicy(max_budget_eur=20.0),
        runtime=runtime,
    )
    assert res_del_ok["status"] == "submitted", res_del_ok

    # Policy with 10€ budget rejects 15€ plan
    profile_blocked = WorkloadProfile(workload_id="wl-gov-7b", cpu_requested=16)
    gov.set_workload_control(profile_blocked.workload_id, "delegation", DelegationPolicy(max_budget_eur=10.0))
    res_del_blocked = controller.submit_plan(
        profile=profile_blocked,
        plan=plan,
        control_mode="delegation",
        delegation_policy=DelegationPolicy(max_budget_eur=10.0),
        runtime=runtime,
    )
    assert res_del_blocked["status"] == "blocked"
    assert "exceeds delegated policy budget limit" in res_del_blocked["reason"].lower()


def test_scenario_8_ordered_fallback_ladder_and_budget_breach():
    """Scenario 8: Ordered fallback ladder execution and refusal when exceeding remaining budget."""
    controller = ExecutionController()
    spot_plan = ExecutionPlan(
        plan_id="plan-spot",
        title="Spot Primary",
        plan_type="cost_optimized",
        cpu=32,
        machine_type="n2-standard-32",
        provisioning_model="100% Spot",
        estimated_cost_eur=4.0,
        fallback_plan_id="plan-standard",
    )
    standard_plan = ExecutionPlan(
        plan_id="plan-standard",
        title="Standard Fallback",
        plan_type="deadline_favored",
        cpu=32,
        machine_type="n2-standard-32",
        provisioning_model="100% Standard",
        estimated_cost_eur=12.0,
    )

    available_plans = [spot_plan, standard_plan]

    # Case A: Remaining budget is 20€, accumulated 2€ -> Total 14€ <= 20€ -> Fallback succeeds
    fallback_res = controller.evaluate_fallback_ladder_with_details(
        current_plan=spot_plan,
        available_plans=available_plans,
        failure_category="capacity_shortage",
        budget_limit_eur=20.0,
        accumulated_cost_eur=2.0,
    )
    assert fallback_res["status"] == "selected"
    assert fallback_res["plan"].plan_id == "plan-standard"

    # Case B: Remaining budget is 10€, accumulated 2€ -> Total 14€ > 10€ -> Budget breach rejected
    breach_res = controller.evaluate_fallback_ladder_with_details(
        current_plan=spot_plan,
        available_plans=available_plans,
        failure_category="capacity_shortage",
        budget_limit_eur=10.0,
        accumulated_cost_eur=2.0,
    )
    assert breach_res["status"] == "budget_breach"
    assert breach_res["plan"] is None
    assert "budget" in breach_res["reason"].lower()


def test_scenario_9_submission_timeout_and_idempotency():
    """Scenario 9: an ambiguous timeout must never create a second job.

    The original test only called ``submit_plan`` twice and never produced a
    timeout, so the behaviour named in its own title was untested. It now:

    * sets delegation on the server (a caller can no longer grant it to itself),
    * raises a real :class:`AmbiguousSubmission` *after* the runtime accepted the
      job, and checks recovery finds that same job by submission identity,
    * replays through a brand-new controller to prove idempotency survives a
      process restart,
    * checks an explicitly authorised retry is distinguishable from a replay.
    """
    from agentic_compute.errors import AmbiguousSubmission
    from agentic_compute.governance import plan_fingerprint, submission_key

    runtime = SimulatedRuntime()
    controller = ExecutionController(runtime)
    plan = ExecutionPlan(
        plan_id="plan-idemp-1",
        title="Idempotent Plan",
        plan_type="cost_optimized",
        cpu=16,
        machine_type="n2-standard-16",
        provisioning_model="100% Spot",
        estimated_cost_eur=5.0,
    )
    profile = WorkloadProfile(workload_id="wl-idemp-99", cpu_requested=16)
    controller.governance.set_workload_control(
        profile.workload_id, "delegation", DelegationPolicy(max_budget_eur=50.0)
    )

    # First submission
    res1 = controller.submit_plan(
        profile=profile,
        plan=plan,
        control_mode="delegation",
        runtime=runtime,
    )
    assert res1["status"] == "submitted", res1
    first_job_id = res1["job_id"]
    assert first_job_id

    # Second submission with same workload_id -> Reuses existing job, avoids duplicate
    res2 = controller.submit_plan(
        profile=profile,
        plan=plan,
        control_mode="delegation",
        runtime=runtime,
    )
    assert res2["status"] == "already_submitted"
    assert res2["job_id"] == first_job_id

    # A brand new controller (i.e. a restarted process) must reach the same
    # verdict: the ledger, not in-memory state, is the authority.
    restarted = ExecutionController(SimulatedRuntime())
    res3 = restarted.submit_plan(profile=profile, plan=plan, control_mode="delegation")
    assert res3["status"] == "already_submitted"
    assert res3["job_id"] == first_job_id

    # Ambiguous timeout on a different workload: the runtime really accepts the
    # job, then the transport fails. Recovery must find that exact job and must
    # not submit again.
    amb_runtime = SimulatedRuntime()
    amb_controller = ExecutionController(amb_runtime)
    amb_profile = WorkloadProfile(workload_id="wl-idemp-timeout", cpu_requested=16)
    amb_controller.governance.set_workload_control(
        amb_profile.workload_id, "delegation", DelegationPolicy(max_budget_eur=50.0)
    )

    real_submit = amb_runtime.submit_job
    calls: list[str] = []

    def flaky_submit(**kwargs):
        calls.append(kwargs.get("name", ""))
        real_submit(**kwargs)  # the cluster genuinely accepts it
        raise AmbiguousSubmission(
            "gateway timeout while awaiting the submission acknowledgement",
            submission_key=kwargs.get("name"),
        )

    amb_runtime.submit_job = flaky_submit
    res_amb = amb_controller.submit_plan(
        profile=amb_profile, plan=plan, control_mode="delegation", runtime=amb_runtime
    )
    amb_runtime.submit_job = real_submit

    expected_key = submission_key(
        amb_profile.workload_id,
        plan_fingerprint(plan, command=amb_profile.command or amb_profile.script),
    )
    assert len(calls) == 1, "the runtime must be called exactly once"
    assert calls[0] == expected_key
    assert res_amb["status"] == "submitted", res_amb
    assert res_amb["recovered_after_timeout"] is True
    assert res_amb["job_id"] == expected_key

    # Replaying after the ambiguous timeout still must not duplicate.
    res_amb_replay = amb_controller.submit_plan(
        profile=amb_profile, plan=plan, control_mode="delegation", runtime=amb_runtime
    )
    assert res_amb_replay["status"] == "already_submitted"
    assert res_amb_replay["job_id"] == res_amb["job_id"]

    # A definite failure followed by an explicitly authorised retry is a
    # different thing from a replay, and must be allowed to run again.
    fail_runtime = SimulatedRuntime()
    fail_controller = ExecutionController(fail_runtime)
    fail_profile = WorkloadProfile(workload_id="wl-idemp-retry", cpu_requested=16)
    fail_controller.governance.set_workload_control(
        fail_profile.workload_id, "delegation", DelegationPolicy(max_budget_eur=50.0)
    )

    def broken_submit(**kwargs):
        raise ConnectionError("cluster endpoint refused the connection")

    fail_runtime.submit_job = broken_submit
    res_fail = fail_controller.submit_plan(
        profile=fail_profile, plan=plan, control_mode="delegation", runtime=fail_runtime
    )
    assert res_fail["status"] == "failed", res_fail
    assert res_fail["ambiguous"] is False

    # Without authorisation, the failed slot is not silently reused.
    res_replay_failed = fail_controller.submit_plan(
        profile=fail_profile, plan=plan, control_mode="delegation", runtime=fail_runtime
    )
    assert res_replay_failed["status"] != "submitted"

    fail_runtime.submit_job = SimulatedRuntime.submit_job.__get__(fail_runtime)
    res_retry = fail_controller.submit_plan(
        profile=fail_profile,
        plan=plan,
        control_mode="delegation",
        runtime=fail_runtime,
        allow_resubmit=True,
    )
    assert res_retry["status"] == "submitted", res_retry


def test_scenario_10_interruption_checkpoint_resume_and_non_interruptible_rejection():
    """Scenario 10: Checkpoint resume supported for interruptible workloads; rejected for non-interruptible workloads."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        store = HistoryStore(db_path=db_path)
        mgr = LifecycleManager(history_store=store)
        plan = ExecutionPlan(
            plan_id="plan-ckpt",
            title="Checkpoint Plan",
            plan_type="balanced_tradeoff",
            cpu=16,
            machine_type="n2-standard-16",
            provisioning_model="100% Spot",
            estimated_cost_eur=8.0,
        )

        # 1a. Interruptible workload whose checkpoint location cannot be inspected
        # from here (no GCS client). The old behaviour was to synthesise
        # "<location>/step_latest" and record it as a recovery, so the attempt
        # history claimed progress that nothing supported. The honest answer is
        # that the resume is unproven and no recovery is recorded.
        ckpt_profile = WorkloadProfile(
            workload_id="wl-ckpt-ok",
            is_interruptible=True,
            supports_checkpointing=True,
            checkpoint_location="gs://bucket/checkpoints/wl-ckpt-ok",
        )
        mgr.register_workload(ckpt_profile)
        att1 = mgr.start_attempt(workload_id="wl-ckpt-ok", plan=plan, job_id="job-1")
        mgr.finish_attempt(workload_id="wl-ckpt-ok", final_status="PREEMPTED", failure_reason="Spot preemption")

        can_resume, msg = mgr.can_resume_from_checkpoint("wl-ckpt-ok")
        assert can_resume is True
        assert "checkpoints/wl-ckpt-ok" in msg

        evidence = mgr.verify_checkpoint("wl-ckpt-ok")
        att2 = mgr.start_attempt(workload_id="wl-ckpt-ok", plan=plan, job_id="job-2")
        if evidence["verification"] == "verified_present":
            assert att2.checkpoint_recovered_from == "gs://bucket/checkpoints/wl-ckpt-ok"
        else:
            assert att2.checkpoint_recovered_from is None, (
                "an unverifiable checkpoint must not be recorded as a recovery"
            )
            assert "could NOT be verified" in msg

        # 1b. A checkpoint that genuinely exists does produce a recorded recovery.
        with tempfile.TemporaryDirectory() as ckpt_dir:
            local_profile = WorkloadProfile(
                workload_id="wl-ckpt-local",
                is_interruptible=True,
                supports_checkpointing=True,
                checkpoint_location=ckpt_dir,
            )
            mgr.register_workload(local_profile)
            mgr.start_attempt(workload_id="wl-ckpt-local", plan=plan, job_id="job-l1")
            mgr.finish_attempt(
                workload_id="wl-ckpt-local", final_status="PREEMPTED", failure_reason="Spot preemption"
            )
            with open(os.path.join(ckpt_dir, "step_000042.pt"), "wb") as ckpt:
                ckpt.write(b"state")

            resumable, local_msg = mgr.can_resume_from_checkpoint("wl-ckpt-local")
            assert resumable is True
            assert "verified" in local_msg.lower()
            att_local = mgr.start_attempt(workload_id="wl-ckpt-local", plan=plan, job_id="job-l2")
            assert att_local.checkpoint_recovered_from == ckpt_dir

        # 2. Non-interruptible workload
        non_int_profile = WorkloadProfile(
            workload_id="wl-non-interruptible",
            is_interruptible=False,
            supports_checkpointing=False,
        )
        mgr.register_workload(non_int_profile)
        can_resume2, msg2 = mgr.can_resume_from_checkpoint("wl-non-interruptible")
        assert can_resume2 is False
        assert "non-interruptible" in msg2.lower()

        with pytest.raises(ValueError, match="non-interruptible"):
            mgr.start_attempt(
                workload_id="wl-non-interruptible",
                plan=plan,
                job_id="job-bad",
                checkpoint_recovered_from="gs://bucket/fake",
            )
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_scenario_11_scaling_and_safe_downscaling():
    """Scenario 11: Cluster elastic sizing and safe downscaling protecting active running compute nodes."""
    runtime = SimulatedRuntime()
    controller = ExecutionController(runtime)

    # Workload is currently RUNNING and not done
    runtime.workload.id = "active-job-77"
    runtime.workload.remaining_work_units = 250.0  # Not done!

    # Attempting to downscale an active running workload must be rejected to protect nodes
    res = controller.safe_downscale(workload_id="active-job-77", runtime=runtime)
    assert res["status"] == "rejected"
    assert res["safe"] is False
    assert "currently running" in res["reason"].lower()

    # Complete the workload
    runtime.workload.remaining_work_units = 0.0
    assert runtime.is_done() is True

    # Now downscaling safely succeeds
    res_done = controller.safe_downscale(workload_id="active-job-77", runtime=runtime)
    assert res_done["status"] == "downscaled"
    assert res_done["safe"] is True


def test_scenario_12_persistence_and_restart_recovery():
    """Scenario 12: Persistent storage preserves history and cost variance across application restarts."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        # Instance 1: write record before restart
        store1 = HistoryStore(db_path=db_path)
        prof = WorkloadProfile(workload_id="wl-persist-88", name="climate-model", cpu_requested=32)
        rec = ExecutionHistoryRecord(
            workload_id="wl-persist-88",
            workload_name="climate-model",
            profile=prof,
            final_status="COMPLETED",
            initial_estimated_cost_eur=45.0,
            final_calculated_cost_eur=48.20,
            cost_comparison_delta_eur=3.20,
            initial_estimated_duration_minutes=120.0,
            final_actual_duration_minutes=128.0,
            duration_comparison_delta_minutes=8.0,
            reconciliation_status="calculated_from_usage",
        )
        store1.save_history_record(rec)
        del store1  # Simulate process shutdown

        # Instance 2: new process reopens database
        store2 = HistoryStore(db_path=db_path)
        recovered = store2.get_history_record("wl-persist-88")
        assert recovered is not None
        assert recovered.workload_id == "wl-persist-88"
        assert recovered.final_calculated_cost_eur == 48.20
        assert recovered.cost_comparison_delta_eur == 3.20

        # Calibration metrics are accessible
        metrics = store2.get_comparable_metrics(machine_type="n2-standard-32")
        assert "provenance" in metrics
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)
