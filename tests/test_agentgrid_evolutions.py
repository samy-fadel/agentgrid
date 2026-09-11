from __future__ import annotations

import os
import tempfile
import time
import pytest

from agentic_compute.models import (
    DelegationPolicy,
    DiagnosticItem,
    ExecutionAttempt,
    ExecutionHistoryRecord,
    ExecutionPlan,
    WorkloadProfile,
)
from agentic_compute.capacity_search import search_compatible_capacity
from agentic_compute.diagnostic import diagnose_blockers, diagnose_slurm_job, diagnose_gcp_blocker
from agentic_compute.plan_engine import evaluate_and_compare_plans
from agentic_compute.execution_controller import ExecutionController
from agentic_compute.lifecycle_manager import LifecycleManager
from agentic_compute.history import HistoryStore


# ---------------------------------------------------------------------------
# Feature 1: Recherche de capacité compatible (Catalog, Quotas, Signals, Alloc)
# ---------------------------------------------------------------------------

def test_feature1_capacity_search_lifecycle_and_filtering():
    """Verify capacity candidates follow 4-stage lifecycle and filter out incompatible configs."""
    # Search for a standard compute workload
    candidates = search_compatible_capacity(
        cpu_requested=8,
        memory_gb_requested=16.0,
        gpu_requested=0,
        allowed_regions=["us-central1"],
        allow_spot=True,
        allow_standard=True,
        demo_mode=True,
    )
    assert len(candidates) > 0
    for cand in candidates:
        assert cand.compatibility == "COMPATIBLE"
        assert cand.cpu_count >= 8
        assert cand.memory_gb >= 16.0
        # 4-stage lifecycle attribute exists
        assert cand.state_stage in ("catalog_proposed", "quota_authorized", "capacity_estimated", "actually_allocated")
        assert cand.data_provenance in ("gcp_live_api", "simulated_demo", "unavailable")


def test_feature1_capacity_search_rejects_incompatible():
    """Verify that requests with incompatible requirements (e.g. 1000 CPUs or excessive GPUs) return no compatible candidates."""
    candidates = search_compatible_capacity(
        cpu_requested=5000,  # exceeds any single VM in catalog
        memory_gb_requested=100000.0,
        demo_mode=True,
    )
    assert len(candidates) == 0


def test_feature1_provenance_explicitly_labeled():
    """Verify that simulated vs live vs unavailable provenance is never spoofed."""
    # When demo_mode=True, provenance must be simulated_demo
    sim_cands = search_compatible_capacity(cpu_requested=4, demo_mode=True)
    assert all(c.data_provenance == "simulated_demo" for c in sim_cands)

    # When demo_mode=False and no credentials, capacity signal is properly marked
    offline_cands = search_compatible_capacity(cpu_requested=4, demo_mode=False)
    assert all(c.data_provenance in ("gcp_live_api", "unavailable") for c in offline_cands)


# ---------------------------------------------------------------------------
# Feature 2: Diagnostic des blocages (Categorization, facts, origin, actions)
# ---------------------------------------------------------------------------

def test_feature2_diagnostic_categorization_and_actions():
    """Verify Slurm reasons and exit codes are categorized into required buckets with concrete remediations."""
    # 1. Resource Waiting
    d_res = diagnose_blockers(job_state="PENDING", state_reason="ReqNodeNotAvail")
    assert len(d_res) > 0
    assert d_res[0]["category"] in ("resource_waiting", "capacity_shortage")
    assert d_res[0]["confirmed"] is True
    assert len(d_res[0]["possible_actions"]) > 0

    # 2. Priority
    d_prio = diagnose_blockers(job_state="PENDING", state_reason="Priority")
    assert any(d["category"] == "priority" for d in d_prio)

    # 3. Dependencies
    d_dep = diagnose_blockers(job_state="PENDING", state_reason="Dependency")
    assert any(d["category"] == "dependencies" for d in d_dep)

    # 4. Quota
    d_quota = diagnose_blockers(job_state="PENDING", state_reason="QOSMaxCpuPerUserLimit")
    assert any(d["category"] == "quota" for d in d_quota)

    # 5. Incompatible Configuration
    d_incomp = diagnose_blockers(job_state="FAILED", state_reason="BadConstraints")
    assert any(d["category"] == "incompatible_configuration" for d in d_incomp)

    # 6. Application Error (Exit Code 137 / OOM)
    d_oom = diagnose_blockers(job_state="FAILED", exit_code=137)
    assert any(d["category"] == "application_error" for d in d_oom)
    assert any("memory" in str(d["possible_actions"]).lower() for d in d_oom)

    # 7. GCP Stockout / Capacity Shortage
    d_cap = diagnose_blockers(job_state="PENDING", gcp_error="ZONE_RESOURCE_POOL_EXHAUSTED in us-central1-a")
    assert any(d["category"] == "capacity_shortage" for d in d_cap)


# ---------------------------------------------------------------------------
# Feature 3: Comparaison de plans (Deterministic, Amdahl, Breakdowns, Unfeasible)
# ---------------------------------------------------------------------------

def test_feature3_plan_comparison_distinct_plans():
    """Verify comparator generates distinct Cost-Optimized, Deadline-Favored, and Balanced plans."""
    profile = WorkloadProfile(
        workload_id="wl-test-plans",
        name="test-sim",
        deadline_minutes_from_start=60.0,
        budget_amount=10.0,
        is_parallelizable=True,
        estimated_duration_minutes=120.0,
    )
    result = evaluate_and_compare_plans(profile, cluster_total_cpu=128, demo_mode=True)
    assert result["is_feasible"] is True
    plans = result["plans"]
    assert len(plans) >= 2

    types = [p["plan_type"] for p in plans]
    assert "cost_optimized" in types
    assert "deadline_favored" in types

    cost_opt = next(p for p in plans if p["plan_type"] == "cost_optimized")
    dl_fav = next(p for p in plans if p["plan_type"] == "deadline_favored")

    # Cost-optimized must have <= cost than deadline-favored
    assert cost_opt["estimated_cost_eur"] <= dl_fav["estimated_cost_eur"]
    # Deadline-favored must have <= time than cost-optimized
    assert dl_fav["total_time_to_result_minutes"] <= cost_opt["total_time_to_result_minutes"]

    # Inclusions / Exclusions breakdown
    assert "vm_compute_hourly" in cost_opt["cost_scope_included"]
    assert "network_egress" in cost_opt["cost_exclusions_known"]
    # Time breakdown
    assert cost_opt["total_time_to_result_minutes"] > 0
    assert cost_opt["estimated_execution_minutes"] > 0


def test_feature3_plan_comparison_unfeasible_explanation():
    """Verify that when constraints cannot be satisfied, plans is empty and explicit reason is given (no hallucination)."""
    # Impossible constraints: 1 minute deadline on 120m sequential work with 0.50€ budget
    profile = WorkloadProfile(
        workload_id="wl-impossible",
        name="impossible-job",
        deadline_minutes_from_start=1.0,
        budget_amount=0.01,
        is_parallelizable=False,
        estimated_duration_minutes=120.0,
    )
    result = evaluate_and_compare_plans(profile, cluster_total_cpu=128, demo_mode=True)
    assert result["is_feasible"] is False
    assert result["plans"] == []
    assert result["unfeasible_explanation"] is not None
    assert "violated" in result["unfeasible_explanation"].lower()


# ---------------------------------------------------------------------------
# Feature 4: Exécution et repli contrôlés (Control modes, Fallback, Idempotency)
# ---------------------------------------------------------------------------

def test_feature4_control_modes_enforcement():
    """Verify advisory mode blocks mutations, validation requires approval, delegation enforces policy."""
    controller = ExecutionController()
    plan = ExecutionPlan(
        plan_id="plan-1",
        plan_type="cost_optimized",
        title="Test Plan",
        machine_type="n2-standard-4",
        cpu=4,
        estimated_cost_eur=2.50,
        total_time_to_result_minutes=30.0,
        ranking_rationale="test",
    )
    profile = WorkloadProfile(workload_id="wl-ctrl", name="wl-ctrl")

    # 1. Advisory Mode: MUST block execution
    res_adv = controller.submit_plan(profile, plan, control_mode="advisory")
    assert res_adv["status"] == "blocked"
    assert "advisory mode is read-only" in res_adv["reason"].lower()

    # 2. Validation Mode: MUST block if not operator-approved
    res_val_unapproved = controller.submit_plan(profile, plan, control_mode="validation", is_operator_approved=False)
    assert res_val_unapproved["status"] == "blocked"
    assert "operator approval" in res_val_unapproved["reason"].lower()

    # 3. Validation Mode: MUST succeed when approved
    res_val_approved = controller.submit_plan(profile, plan, control_mode="validation", is_operator_approved=True)
    assert res_val_approved["status"] == "submitted"

    # 4. Delegation Mode: Autonomous within budget
    policy = DelegationPolicy(max_budget_eur=5.0, allowed_machine_types=["n2-standard-4"])
    res_del_ok = controller.submit_plan(profile, plan, control_mode="delegation", delegation_policy=policy)
    # Note: might be 'already_submitted' due to idempotency for same workload_id
    assert res_del_ok["status"] in ("submitted", "already_submitted")

    # 5. Delegation Mode: MUST block when plan exceeds policy budget
    expensive_plan = ExecutionPlan(
        plan_id="plan-exp",
        plan_type="deadline_favored",
        title="Expensive Plan",
        machine_type="c2-standard-60",
        cpu=60,
        estimated_cost_eur=15.00,
        total_time_to_result_minutes=10.0,
        ranking_rationale="fast",
    )
    res_del_blocked = controller.submit_plan(
        WorkloadProfile(workload_id="wl-exp"), expensive_plan, control_mode="delegation", delegation_policy=policy
    )
    assert res_del_blocked["status"] == "blocked"
    assert "exceeds delegated policy budget" in res_del_blocked["reason"].lower()


def test_feature4_fallback_ladder_selection():
    """Verify fallback ladder picks viable alternative within remaining budget."""
    controller = ExecutionController()
    spot_plan = ExecutionPlan(
        plan_id="plan-spot",
        plan_type="cost_optimized",
        title="Spot Plan",
        machine_type="n2-standard-4",
        provisioning_model="100% Spot",
        cpu=4,
        estimated_cost_eur=1.00,
        fallback_plan_id="plan-standard",
        ranking_rationale="spot",
    )
    standard_plan = ExecutionPlan(
        plan_id="plan-standard",
        plan_type="fallback_alternative",
        title="Standard Fallback",
        machine_type="n2-standard-4",
        provisioning_model="100% Standard",
        cpu=4,
        estimated_cost_eur=3.00,
        ranking_rationale="std",
    )

    fallback = controller.evaluate_fallback_ladder(
        current_plan=spot_plan,
        available_plans=[spot_plan, standard_plan],
        failure_category="capacity_shortage",
        budget_limit_eur=5.00,
        accumulated_cost_eur=0.50,
    )
    assert fallback is not None
    assert fallback.plan_id == "plan-standard"


def test_feature4_safe_downscale():
    """Verify safe downscaling cleans active tracking."""
    controller = ExecutionController()
    controller._submitted_job_hashes["wl-downscale"] = "job-999"
    res = controller.safe_downscale("wl-downscale")
    assert res["status"] == "downscaled"
    assert "wl-downscale" not in controller._submitted_job_hashes


# ---------------------------------------------------------------------------
# Feature 5: Suivi et reprise (Lifecycle states, Checkpoints, Bounded retries)
# ---------------------------------------------------------------------------

def test_feature5_lifecycle_transitions_and_observable_progress():
    """Verify state transitions and that progress is explicitly measured or unavailable."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_hist.db")
        store = HistoryStore(db_path=db_path)
        mgr = LifecycleManager(history_store=store)

        prof = WorkloadProfile(workload_id="wl-life-1", name="life-workload", supports_checkpointing=True)
        rec = mgr.register_workload(prof)
        assert rec["state"] == "DEFINED"
        assert rec["progress_status"] == "unavailable"

        # Plan & submit
        plan = ExecutionPlan(
            plan_id="plan-life",
            plan_type="cost_optimized",
            title="Life Plan",
            machine_type="n2-standard-2",
            cpu=2,
            estimated_cost_eur=1.0,
            ranking_rationale="test",
        )
        attempt = mgr.start_attempt("wl-life-1", plan, job_id="job-101")
        assert attempt.attempt_number == 1
        assert mgr._workloads["wl-life-1"]["state"] == "QUEUED"

        # Unmeasured progress update: must report unavailable
        mgr.update_progress("wl-life-1", progress_percent=None, job_state="RUNNING", elapsed_minutes=5.0)
        assert mgr._workloads["wl-life-1"]["progress_status"] == "unavailable"
        assert mgr._workloads["wl-life-1"]["progress_percent"] is None

        # Measured progress update
        mgr.update_progress("wl-life-1", progress_percent=45.0, job_state="RUNNING", elapsed_minutes=10.0)
        assert mgr._workloads["wl-life-1"]["progress_status"] == "measured"
        assert mgr._workloads["wl-life-1"]["progress_percent"] == 45.0

        # Finish attempt
        mgr.finish_attempt("wl-life-1", final_status="COMPLETED", total_cost_eur=0.85, actual_duration_minutes=20.0)
        assert mgr._workloads["wl-life-1"]["state"] == "COMPLETED"
        assert mgr._workloads["wl-life-1"]["progress_percent"] == 100.0


def test_feature5_checkpoint_resumption_vs_restart_scratch():
    """Verify checkpoint resumption when supported vs mandatory restart from scratch when unsupported."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = HistoryStore(db_path=os.path.join(tmpdir, "hist.db"))
        mgr = LifecycleManager(history_store=store)

        # Workload WITHOUT checkpoint support
        p_no_ckpt = WorkloadProfile(workload_id="wl-no-ckpt", supports_checkpointing=False)
        mgr.register_workload(p_no_ckpt)
        can_res, reason = mgr.can_resume_from_checkpoint("wl-no-ckpt")
        assert can_res is False
        assert "does not support checkpointing" in reason.lower()

        # Workload WITH checkpoint support
        p_ckpt = WorkloadProfile(
            workload_id="wl-ckpt",
            supports_checkpointing=True,
            checkpoint_location="gs://bucket/checkpoints",
        )
        mgr.register_workload(p_ckpt)
        can_res2, reason2 = mgr.can_resume_from_checkpoint("wl-ckpt")
        assert can_res2 is True


def test_feature5_bounded_retries_aborts_excess():
    """Verify that exceeding max_retries aborts execution to protect operator budget."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = HistoryStore(db_path=os.path.join(tmpdir, "hist.db"))
        mgr = LifecycleManager(history_store=store)

        prof = WorkloadProfile(workload_id="wl-retry-test", max_retries=2)
        mgr.register_workload(prof)
        plan = ExecutionPlan(
            plan_id="p1", plan_type="cost_optimized", title="P1", machine_type="n2-standard-2", cpu=2,
            estimated_cost_eur=1.0, ranking_rationale="t"
        )

        mgr.start_attempt("wl-retry-test", plan)
        mgr.finish_attempt("wl-retry-test", final_status="PREEMPTED")

        mgr.start_attempt("wl-retry-test", plan)
        mgr.finish_attempt("wl-retry-test", final_status="PREEMPTED")

        # 3rd attempt exceeds max_retries=2
        with pytest.raises(RuntimeError) as exc_info:
            mgr.start_attempt("wl-retry-test", plan)
        assert "exceeded max retries" in str(exc_info.value).lower()
        assert mgr._workloads["wl-retry-test"]["state"] == "FAILED"


# ---------------------------------------------------------------------------
# Feature 6: Coût réel, historique persistant et comparaison (3-Tier Cost)
# ---------------------------------------------------------------------------

def test_feature6_history_persistence_and_three_tier_reconciliation():
    """Verify SQLite persistence across restarts and 3-tier cost breakdown (estimated, calculated, billed)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = os.path.join(tmpdir, "persisted_history.db")

        # Instance 1: write record
        store1 = HistoryStore(db_path=db_file)
        prof = WorkloadProfile(workload_id="wl-p1", name="persisted-job", budget_amount=5.0)
        store1.save_workload_profile(prof)

        plan = ExecutionPlan(
            plan_id="plan-init",
            plan_type="cost_optimized",
            title="Initial Plan",
            machine_type="n2-standard-4",
            cpu=4,
            estimated_cost_eur=3.50,
            ranking_rationale="opt",
        )
        store1.save_execution_plan(plan, workload_id="wl-p1", is_approved=True)

        att = ExecutionAttempt(
            attempt_id="wl-p1-att-1",
            workload_id="wl-p1",
            status="COMPLETED",
            elapsed_minutes=40.0,
            cost_calculated_eur=3.10,
            cost_status="calculated_from_usage",
        )
        store1.record_attempt(att)

        record = ExecutionHistoryRecord(
            workload_id="wl-p1",
            workload_name="persisted-job",
            profile=prof,
            approved_plan=plan,
            initial_estimated_cost_eur=3.50,
            final_calculated_cost_eur=3.10,
            final_actual_duration_minutes=40.0,
            reconciliation_status="calculated_from_usage",
        )
        store1.save_history_record(record)

        # Instance 2 (simulating application restart): read from disk
        store2 = HistoryStore(db_path=db_file)
        retrieved = store2.get_history_record("wl-p1")
        assert retrieved is not None
        assert retrieved.workload_id == "wl-p1"
        assert retrieved.initial_estimated_cost_eur == 3.50
        assert retrieved.final_calculated_cost_eur == 3.10

        # Test 3-Tier Reconciliation:
        # Tier 1: Initial estimated
        # Tier 2: Calculated from usage
        # Tier 3: Reconciled billed (external billing)
        reconciled = store2.reconcile_costs("wl-p1", billed_cost_eur=3.25)
        assert reconciled["cost_tiers"]["tier1_estimated_cost_eur"] == 3.50
        assert reconciled["cost_tiers"]["tier2_calculated_from_usage_eur"] == 3.10
        assert reconciled["cost_tiers"]["tier3_reconciled_billed_eur"] == 3.25
        assert reconciled["billed_reconciliation_status"] == "reconciled_billed"

        # Continuous benchmark metric update
        store2.record_workload_benchmark("persisted-job:n2-standard-4:SPOT", work_units=100.0, duration_minutes=40.0)
        bm = store2.get_benchmark_metrics("persisted-job:n2-standard-4:SPOT")
        assert bm is not None
        assert bm["sample_size"] == 1
        assert bm["avg_minutes_per_unit"] == 0.40
