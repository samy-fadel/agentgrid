"""Live Workload Drift & Anomaly Detector and Fleet FinOps/Carbon Portfolio Analytics.

Provides:
1. Real-time detection of burn-rate drift, projected budget breaches, throughput stalls
   (zombie compute jobs), SLA deadline breach trajectories, and Young-Daly checkpoint
   exposure windows.
2. Fleet-wide FinOps & Carbon Portfolio aggregation across the 3-tier financial
   reconciliation store and server-side governance ledger.
"""
from __future__ import annotations

import time
from typing import Any

from .models import ExecutionHistoryRecord, ExecutionPlan, WorkloadProfile
from .pareto_optimizer import (
    compute_carbon_footprint,
    compute_young_daly_checkpoint_schedule,
)


def detect_workload_anomalies(
    workload_id: str,
    profile: WorkloadProfile | dict[str, Any] | None = None,
    plan: ExecutionPlan | dict[str, Any] | None = None,
    elapsed_minutes: float = 0.0,
    current_cost_eur: float = 0.0,
    progress_pct: float | None = None,
    minutes_since_last_checkpoint: float | None = None,
) -> dict[str, Any]:
    """Analyze runtime telemetry for cost drift, throughput stalls, and checkpoint exposure."""
    if isinstance(profile, dict):
        try:
            prof = WorkloadProfile(**profile)
        except Exception:
            prof = None
    else:
        prof = profile

    p_dict = plan.model_dump() if isinstance(plan, ExecutionPlan) else (dict(plan) if isinstance(plan, dict) else {})

    planned_exec_min = float(
        p_dict.get("estimated_execution_minutes")
        or p_dict.get("total_time_to_result_minutes")
        or getattr(prof, "estimated_duration_minutes", None)
        or 60.0
    )
    planned_cost_eur = float(p_dict.get("estimated_cost_eur") or 0.0)
    budget_eur = (
        getattr(prof, "budget_amount", None)
        if prof and getattr(prof, "budget_amount", None) is not None
        else (getattr(prof, "max_cost_limit_eur", None) if prof else None)
    )
    deadline_min = getattr(prof, "deadline_minutes_from_start", None) if prof else None
    supports_ckpt = bool(getattr(prof, "supports_checkpointing", False)) if prof else False
    ckpt_int = getattr(prof, "checkpoint_interval_minutes", None) if prof else None
    prov_model = str(p_dict.get("provisioning_model") or "100% Standard")

    elapsed = max(0.0, float(elapsed_minutes or 0.0))
    accrued = max(0.0, float(current_cost_eur or 0.0))

    expected_progress_pct = min(100.0, round((elapsed / max(1.0, planned_exec_min)) * 100.0, 1))
    actual_progress_pct = float(progress_pct) if progress_pct is not None else expected_progress_pct
    actual_progress_pct = max(0.0, min(100.0, actual_progress_pct))

    # Burn rates (EUR / hour)
    planned_burn_rate_hr = round((planned_cost_eur / max(1.0, planned_exec_min)) * 60.0, 4)
    actual_burn_rate_hr = round((accrued / max(0.5, elapsed)) * 60.0, 4) if elapsed > 0 else planned_burn_rate_hr

    burn_rate_drift_pct = (
        round(((actual_burn_rate_hr - planned_burn_rate_hr) / planned_burn_rate_hr) * 100.0, 1)
        if planned_burn_rate_hr > 0
        else 0.0
    )

    # Projected totals based on actual progress velocity
    if actual_progress_pct >= 1.0 and elapsed > 0:
        projected_total_duration_min = round(elapsed / (actual_progress_pct / 100.0), 2)
        projected_total_cost_eur = round(accrued / (actual_progress_pct / 100.0), 2)
    else:
        projected_total_duration_min = round(max(planned_exec_min, elapsed), 2)
        projected_total_cost_eur = round(max(planned_cost_eur, accrued), 2)

    # Young-Daly checkpoint analysis
    yd = compute_young_daly_checkpoint_schedule(
        execution_minutes=planned_exec_min,
        base_cost_eur=planned_cost_eur,
        provisioning_model=prov_model,
        supports_checkpointing=supports_ckpt,
        checkpoint_interval_minutes=ckpt_int,
    )
    opt_ckpt_min = yd["young_daly_optimal_interval_minutes"]
    uncommitted_min = (
        float(minutes_since_last_checkpoint)
        if minutes_since_last_checkpoint is not None
        else (min(elapsed, opt_ckpt_min) if supports_ckpt else elapsed)
    )
    uncommitted_cost_at_risk_eur = round((uncommitted_min / 60.0) * actual_burn_rate_hr, 3)

    anomalies: list[dict[str, Any]] = []

    # 1. Burn-rate & Cost Drift Anomaly
    if burn_rate_drift_pct > 15.0:
        anomalies.append({
            "code": "COST_BURN_RATE_DRIFT",
            "severity": "HIGH" if burn_rate_drift_pct > 35.0 else "MEDIUM",
            "title": f"Hourly burn rate exceeds plan by +{burn_rate_drift_pct:.1f}%",
            "observed_facts": (
                f"Actual burn rate is {actual_burn_rate_hr:.3f} EUR/h vs planned {planned_burn_rate_hr:.3f} EUR/h "
                f"(projected final cost: {projected_total_cost_eur:.2f} EUR vs estimated {planned_cost_eur:.2f} EUR)."
            ),
            "recommended_action": "Evaluate fallback plan or downscale vCPU allocation if SLA slack permits.",
        })

    # 2. Projected Budget Breach
    if budget_eur is not None and projected_total_cost_eur > float(budget_eur):
        breach_delta = round(projected_total_cost_eur - float(budget_eur), 2)
        anomalies.append({
            "code": "PROJECTED_BUDGET_BREACH",
            "severity": "CRITICAL",
            "title": f"Projected final cost ({projected_total_cost_eur:.2f} EUR) breaches budget ceiling ({float(budget_eur):.2f} EUR)",
            "observed_facts": (
                f"At current progress ({actual_progress_pct:.1f}%) and spend ({accrued:.2f} EUR), "
                f"workload will exceed budget by +{breach_delta:.2f} EUR before reaching 100%."
            ),
            "recommended_action": "Switch immediately to 100% Spot fallback rung or trigger checkpoint & pause.",
        })

    # 3. Throughput Stall / Zombie Compute Risk
    progress_lag_pct = round(expected_progress_pct - actual_progress_pct, 1)
    if elapsed >= 5.0 and progress_lag_pct >= 25.0:
        anomalies.append({
            "code": "THROUGHPUT_STALL_DETECTED",
            "severity": "HIGH" if progress_lag_pct >= 45.0 else "MEDIUM",
            "title": f"Workload progress lags schedule by -{progress_lag_pct:.1f}%",
            "observed_facts": (
                f"After {elapsed:.1f}m elapsed, expected progress is {expected_progress_pct:.1f}% "
                f"but observed progress is {actual_progress_pct:.1f}%."
            ),
            "recommended_action": "Inspect node I/O bottleneck, deadlock, or memory thrashing via blocker diagnostics.",
        })

    # 4. SLA Deadline Breach Trajectory
    if deadline_min is not None and projected_total_duration_min > float(deadline_min):
        overrun_min = round(projected_total_duration_min - float(deadline_min), 1)
        anomalies.append({
            "code": "SLA_DEADLINE_BREACH_IMMINENT",
            "severity": "CRITICAL",
            "title": f"Projected completion ({projected_total_duration_min:.1f}m) misses SLA deadline ({float(deadline_min):.1f}m)",
            "observed_facts": (
                f"Trajectory predicts a +{overrun_min:.1f}m SLA overrun at current compute velocity."
            ),
            "recommended_action": "Promote workload to Deadline-Favored high-vCPU plan or Standard priority partition.",
        })

    # 5. Young-Daly Checkpoint Exposure Window
    if "spot" in prov_model.lower() and (
        uncommitted_min > (opt_ckpt_min * 1.1) or (not supports_ckpt and uncommitted_min >= 15.0)
    ):
        anomalies.append({
            "code": "CHECKPOINT_CADENCE_EXPOSURE",
            "severity": "MEDIUM" if supports_ckpt else "HIGH",
            "title": f"Uncommitted work window ({uncommitted_min:.1f}m) exposes Spot workload (Young-Daly optimum: {opt_ckpt_min:.1f}m)",
            "observed_facts": (
                f"Running on {prov_model} with {uncommitted_min:.1f}m of unsaved progress "
                f"({uncommitted_cost_at_risk_eur:.3f} EUR of compute at immediate preemption risk)."
            ),
            "recommended_action": f"Trigger immediate state snapshot and set checkpoint_interval_minutes={opt_ckpt_min}.",
        })

    health_status = "HEALTHY"
    if any(a["severity"] == "CRITICAL" for a in anomalies):
        health_status = "CRITICAL_DRIFT"
    elif any(a["severity"] == "HIGH" for a in anomalies):
        health_status = "DEGRADED_DRIFT"
    elif anomalies:
        health_status = "WARNING_DRIFT"

    return {
        "workload_id": workload_id,
        "health_status": health_status,
        "anomaly_count": len(anomalies),
        "anomalies": anomalies,
        "telemetry": {
            "elapsed_minutes": elapsed,
            "expected_progress_pct": expected_progress_pct,
            "actual_progress_pct": actual_progress_pct,
            "planned_burn_rate_eur_hr": planned_burn_rate_hr,
            "actual_burn_rate_eur_hr": actual_burn_rate_hr,
            "burn_rate_drift_pct": burn_rate_drift_pct,
            "projected_total_duration_minutes": projected_total_duration_min,
            "projected_total_cost_eur": projected_total_cost_eur,
            "young_daly_optimal_checkpoint_minutes": opt_ckpt_min,
            "uncommitted_minutes_at_risk": round(uncommitted_min, 1),
            "uncommitted_cost_at_risk_eur": uncommitted_cost_at_risk_eur,
        },
        "timestamp": time.time(),
    }


def compute_portfolio_finops_analytics(
    records: list[ExecutionHistoryRecord | dict[str, Any]],
    governance_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute executive FinOps, SLA reliability, and Carbon footprint metrics across all workloads."""
    total_workloads = len(records)
    tier1_estimated_eur = 0.0
    tier2_verified_eur = 0.0
    tier3_billed_eur = 0.0
    standard_baseline_eur = 0.0
    total_energy_kwh = 0.0
    total_carbon_g = 0.0
    potential_green_savings_g = 0.0
    sla_met_count = 0
    sla_evaluated_count = 0
    measured_count = 0

    control_counts = {"advisory": 0, "validation": 0, "delegation": 0}

    for rec in records:
        r = rec.model_dump() if isinstance(rec, ExecutionHistoryRecord) else dict(rec)
        mode = str(r.get("control_mode") or "validation").lower()
        if mode in control_counts:
            control_counts[mode] += 1

        est = float(r.get("initial_estimated_cost_eur") or 0.0)
        calc = float(r.get("final_calculated_cost_eur") or 0.0)
        rec_status = str(r.get("reconciliation_status") or "calculated_from_usage")
        # A usage figure needs a usage measurement behind it. Rows written
        # before the history record distinguished "estimated" carry
        # "calculated_from_usage" with nothing observed; summing them made a
        # queued run read as a measured 0.00.
        measured_usage = rec_status == "reconciled_billed" or (
            rec_status == "calculated_from_usage"
            and float(r.get("final_actual_duration_minutes") or 0.0) > 0
        )

        tier1_estimated_eur += est
        if measured_usage:
            tier2_verified_eur += calc
            measured_count += 1
        if rec_status == "reconciled_billed":
            tier3_billed_eur += calc

        # Estimate what 100% Standard would have cost
        approved = r.get("approved_plan") or {}
        prov = str(approved.get("provisioning_model") or "100% Standard")
        active_cost = calc if calc > 0 else est
        if "100% spot" in prov.lower():
            standard_baseline_eur += round(active_cost / 0.35, 2)
        elif "80% spot" in prov.lower():
            standard_baseline_eur += round(active_cost / 0.48, 2)
        else:
            standard_baseline_eur += active_cost

        # Carbon calculation
        cpu = int(approved.get("cpu") or 4)
        gpu = int(approved.get("gpu") or 0)
        region = str(approved.get("region") or "us-central1")
        dur = float(r.get("final_actual_duration_minutes") or r.get("initial_estimated_duration_minutes") or 30.0)

        carb = compute_carbon_footprint(
            cpu=cpu,
            gpu=gpu,
            machine_type=approved.get("machine_type"),
            region=region,
            duration_minutes=dur,
            allow_region_change=True,
        )
        total_energy_kwh += carb["energy_kwh"]
        total_carbon_g += carb["carbon_emissions_g_co2"]
        potential_green_savings_g += carb["potential_co2_reduction_g"]

        # SLA evaluation. Only a measured duration says whether the deadline
        # held: falling back to the estimate made every queued run "compliant"
        # by construction, since plans are only offered when their estimate
        # already fits the deadline.
        prof = r.get("profile") or {}
        deadline = prof.get("deadline_minutes_from_start")
        measured_dur = float(r.get("final_actual_duration_minutes") or 0.0)
        if deadline is not None and measured_dur > 0:
            sla_evaluated_count += 1
            if measured_dur <= float(deadline):
                sla_met_count += 1

    effective_spend = tier2_verified_eur if tier2_verified_eur > 0 else tier1_estimated_eur
    spot_savings_eur = max(0.0, round(standard_baseline_eur - effective_spend, 2))
    spot_savings_pct = (
        round((spot_savings_eur / standard_baseline_eur) * 100.0, 1)
        if standard_baseline_eur > 0
        else 0.0
    )
    # No measured run means no compliance figure, not a perfect one.
    sla_rate = (
        round((sla_met_count / sla_evaluated_count) * 100.0, 1)
        if sla_evaluated_count > 0
        else None
    )

    return {
        "total_workloads": total_workloads,
        "financial_tiers": {
            "tier1_estimated_total_eur": round(tier1_estimated_eur, 2),
            "tier2_verified_usage_total_eur": round(tier2_verified_eur, 2),
            "measured_workloads": measured_count,
            "tier3_reconciled_billed_total_eur": round(tier3_billed_eur, 2),
            "on_demand_baseline_equivalent_eur": round(standard_baseline_eur, 2),
            "spot_hedging_savings_eur": spot_savings_eur,
            "spot_hedging_savings_pct": spot_savings_pct,
        },
        "sustainability_metrics": {
            "total_energy_kwh": round(total_energy_kwh, 4),
            "total_carbon_emissions_g_co2": round(total_carbon_g, 2),
            "potential_green_routing_savings_g_co2": round(potential_green_savings_g, 2),
        },
        "reliability_and_governance": {
            "sla_evaluated_workloads": sla_evaluated_count,
            "sla_compliant_workloads": sla_met_count,
            "sla_compliance_rate_pct": sla_rate,
            "control_mode_distribution": control_counts,
            "governance_ledger_summary": governance_summary or {},
        },
        "generated_at": time.time(),
    }
