"""Multi-objective 4D Pareto Frontier Optimizer, Carbon Footprint Estimator,
Young-Daly Checkpoint Cadence Calculator, and What-If Stress Simulator.

Extends AgentGrid's 3D (Cost / Delay / Capacity) planner with:
1. Regional grid carbon intensity & hardware power modeling (gCO2eq & kWh).
2. Young-Daly mathematically optimal checkpoint cadence & expected Spot preemption
   cost/time overhead.
3. 4D Pareto dominance analysis (Cost, Latency, Interruption Risk, Carbon).
4. Interactive What-If stress testing across preemption spikes, budget cuts, and
   deadline compressions.
"""
from __future__ import annotations

import math
from typing import Any

from .models import ExecutionPlan, WorkloadProfile

# Reference regional carbon intensity (gCO2eq / kWh) based on published cloud grid data.
REGIONAL_CARBON_INTENSITY_G_KWH: dict[str, float] = {
    "europe-north1": 28.0,      # Finland (Hydro / Wind / Nuclear)
    "europe-west9": 58.0,       # Paris (Nuclear / Low-carbon)
    "us-west1": 78.0,           # Oregon (Hydro)
    "northamerica-northeast1": 32.0,  # Montreal (Hydro)
    "southamerica-east1": 95.0, # Sao Paulo (Renewable heavy)
    "europe-west1": 112.0,      # Belgium
    "europe-west2": 195.0,      # London
    "europe-west3": 310.0,      # Frankfurt
    "europe-west4": 295.0,      # Netherlands
    "us-east4": 340.0,          # Northern Virginia
    "us-central1": 365.0,       # Iowa
    "us-east1": 410.0,          # South Carolina
    "asia-northeast1": 450.0,   # Tokyo
    "asia-southeast1": 405.0,   # Singapore
    "asia-east1": 515.0,        # Taiwan
}

DEFAULT_CARBON_INTENSITY_G_KWH = 350.0
FLEET_PUE = 1.10  # Power Usage Effectiveness


def get_region_carbon_intensity(region: str | None) -> float:
    """Return grid carbon intensity in gCO2eq/kWh for a cloud region."""
    if not region:
        return DEFAULT_CARBON_INTENSITY_G_KWH
    norm = str(region).strip().lower()
    return REGIONAL_CARBON_INTENSITY_G_KWH.get(norm, DEFAULT_CARBON_INTENSITY_G_KWH)


def compute_carbon_footprint(
    cpu: int,
    gpu: int = 0,
    machine_type: str | None = None,
    region: str = "us-central1",
    duration_minutes: float = 60.0,
    quantity: int = 1,
    allowed_regions: list[str] | None = None,
    allow_region_change: bool = False,
) -> dict[str, Any]:
    """Estimate energy consumption (kWh) and carbon emissions (gCO2eq) for a plan."""
    mtype = (machine_type or "n2-standard-4").lower()
    qty = max(1, int(quantity or 1))
    cpus = max(1, int(cpu or 1))
    gpus = max(0, int(gpu or 0))
    dur_hours = max(0.0, float(duration_minutes or 0.0)) / 60.0

    # Per-vCPU active TDP estimate by family
    if mtype.startswith("c2-") or mtype.startswith("h3-"):
        watts_per_cpu = 5.5
    elif mtype.startswith("a2-"):
        watts_per_cpu = 5.0
    else:
        watts_per_cpu = 4.2

    # Memory estimate (approx 4 GB per vCPU if not highgpu)
    mem_gb = cpus * (7.0 if mtype.startswith("a2-") else 4.0)
    mem_watts = mem_gb * 0.375

    # GPU TDP estimate
    if gpus > 0:
        gpu_watts_unit = 72.0 if mtype.startswith("g2-") else 350.0
    else:
        gpu_watts_unit = 0.0

    it_power_watts = (cpus * watts_per_cpu) + mem_watts + (gpus * gpu_watts_unit)
    total_power_watts = round(it_power_watts * FLEET_PUE * qty, 2)
    energy_kwh = round((total_power_watts / 1000.0) * dur_hours, 4)

    intensity = get_region_carbon_intensity(region)
    emissions_g = round(energy_kwh * intensity, 2)

    if intensity <= 100.0:
        green_tier = "LOW_CARBON"
    elif intensity <= 300.0:
        green_tier = "MODERATE_CARBON"
    else:
        green_tier = "HIGH_CARBON"

    # Identify greenest alternative region
    candidate_pool = list(allowed_regions or [])
    if allow_region_change:
        for green_reg in ("europe-north1", "europe-west9", "us-west1", "europe-west1"):
            if green_reg not in candidate_pool:
                candidate_pool.append(green_reg)
    if not candidate_pool:
        candidate_pool = [region]

    best_region = min(candidate_pool, key=get_region_carbon_intensity)
    best_intensity = get_region_carbon_intensity(best_region)
    potential_savings_g = max(0.0, round(energy_kwh * (intensity - best_intensity), 2))
    potential_savings_pct = (
        round((potential_savings_g / emissions_g) * 100.0, 1) if emissions_g > 0 else 0.0
    )

    return {
        "region": region,
        "carbon_intensity_g_per_kwh": intensity,
        "power_draw_watts": total_power_watts,
        "energy_kwh": energy_kwh,
        "carbon_emissions_g_co2": emissions_g,
        "green_tier": green_tier,
        "pue_factor": FLEET_PUE,
        "greenest_candidate_region": best_region,
        "greenest_candidate_intensity_g_kwh": best_intensity,
        "potential_co2_reduction_g": potential_savings_g,
        "potential_co2_reduction_pct": potential_savings_pct,
    }


def compute_young_daly_checkpoint_schedule(
    execution_minutes: float,
    base_cost_eur: float,
    provisioning_model: str = "100% Spot",
    supports_checkpointing: bool = False,
    checkpoint_interval_minutes: float | None = None,
    checkpoint_write_minutes: float = 1.2,
    restart_recovery_minutes: float = 2.0,
    quantity: int = 1,
) -> dict[str, Any]:
    """Compute Young-Daly optimal checkpoint interval and expected Spot preemption overhead.

    Formula:
        tau_opt = sqrt(2 * C * MTBI) - C
    where C is the checkpoint write duration and MTBI is the Mean Time Between Interruptions.
    """
    exec_min = max(1.0, float(execution_minutes or 1.0))
    cost = max(0.0, float(base_cost_eur or 0.0))
    qty = max(1, int(quantity or 1))
    prov = (provisioning_model or "100% Standard").lower()

    # Hourly interruption rate per node
    if "100% spot" in prov or prov == "spot":
        hourly_rate_per_node = 0.085
        risk_score = 0.65
    elif "80% spot" in prov or "hedged" in prov:
        hourly_rate_per_node = 0.025
        risk_score = 0.25
    else:
        hourly_rate_per_node = 0.001
        risk_score = 0.02

    fleet_hourly_rate = hourly_rate_per_node * qty
    mtbi_minutes = round(60.0 / max(0.0001, fleet_hourly_rate), 1)
    expected_interruptions = round(exec_min / mtbi_minutes, 3)

    # Young-Daly optimal interval: sqrt(2 * C * MTBI) - C
    c_write = max(0.2, float(checkpoint_write_minutes))
    optimal_interval = max(
        5.0,
        round(math.sqrt(2.0 * c_write * mtbi_minutes) - c_write, 1),
    )

    active_interval = (
        float(checkpoint_interval_minutes)
        if (supports_checkpointing and checkpoint_interval_minutes and checkpoint_interval_minutes > 0)
        else (optimal_interval if supports_checkpointing else None)
    )

    # Compute expected wasted time and checkpoint I/O overhead
    if supports_checkpointing and active_interval is not None:
        num_checkpoints = math.floor(exec_min / active_interval)
        ckpt_io_minutes = round(num_checkpoints * c_write, 2)
        wasted_per_interruption = (active_interval / 2.0) + c_write + restart_recovery_minutes
    else:
        ckpt_io_minutes = 0.0
        # Without checkpoints, an interruption loses on average half the entire progress!
        wasted_per_interruption = (exec_min * 0.5) + restart_recovery_minutes

    expected_wasted_minutes = round(expected_interruptions * wasted_per_interruption, 2)
    expected_total_minutes = round(exec_min + ckpt_io_minutes + expected_wasted_minutes, 2)
    overhead_ratio = (expected_wasted_minutes + ckpt_io_minutes) / exec_min
    expected_total_cost_eur = round(cost * (1.0 + overhead_ratio), 2)

    # Calculate what optimal checkpointing would cost for comparison
    opt_num_ckpts = math.floor(exec_min / optimal_interval)
    opt_io_min = opt_num_ckpts * c_write
    opt_wasted_min = expected_interruptions * ((optimal_interval / 2.0) + c_write + restart_recovery_minutes)
    opt_total_cost_eur = round(cost * (1.0 + (opt_io_min + opt_wasted_min) / exec_min), 2)
    potential_ckpt_savings_eur = max(0.0, round(expected_total_cost_eur - opt_total_cost_eur, 2))

    # Effective interruption risk decreases when checkpointing limits blast radius
    effective_risk_score = round(
        risk_score * (0.45 if supports_checkpointing else 1.0), 3
    )

    return {
        "mtbi_minutes": mtbi_minutes,
        "expected_interruptions": expected_interruptions,
        "interruption_risk_score": effective_risk_score,
        "supports_checkpointing": supports_checkpointing,
        "configured_checkpoint_interval_minutes": active_interval,
        "young_daly_optimal_interval_minutes": optimal_interval,
        "checkpoint_io_overhead_minutes": ckpt_io_minutes,
        "expected_wasted_compute_minutes": expected_wasted_minutes,
        "expected_total_duration_minutes": expected_total_minutes,
        "expected_total_cost_with_preemption_eur": expected_total_cost_eur,
        "optimal_checkpoint_cost_eur": opt_total_cost_eur,
        "potential_checkpoint_savings_eur": potential_ckpt_savings_eur,
    }


def analyze_pareto_frontier(
    plans: list[ExecutionPlan | dict[str, Any]],
    workload_profile: WorkloadProfile | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate 4D Pareto dominance (Cost, Latency, Risk, Carbon) across candidate plans."""
    if isinstance(workload_profile, dict):
        try:
            profile = WorkloadProfile(**workload_profile)
        except Exception:
            profile = None
    else:
        profile = workload_profile

    supports_ckpt = bool(getattr(profile, "supports_checkpointing", False)) if profile else False
    ckpt_int = getattr(profile, "checkpoint_interval_minutes", None) if profile else None
    allowed_regs = getattr(profile, "allowed_regions", None) if profile else None
    allow_reg_change = bool(getattr(profile, "allow_region_change", False)) if profile else False

    enriched: list[dict[str, Any]] = []
    for raw in plans:
        p = raw.model_dump() if isinstance(raw, ExecutionPlan) else dict(raw)
        plan_id = str(p.get("plan_id") or p.get("plan_type") or "plan")
        exec_min = float(p.get("estimated_execution_minutes") or p.get("total_time_to_result_minutes") or 60.0)
        total_min = float(p.get("total_time_to_result_minutes") or exec_min)
        est_cost = float(p.get("estimated_cost_eur") or 0.0)

        carbon = compute_carbon_footprint(
            cpu=int(p.get("cpu", 4)),
            gpu=int(p.get("gpu", 0)),
            machine_type=p.get("machine_type"),
            region=str(p.get("region", "us-central1")),
            duration_minutes=exec_min,
            quantity=int(p.get("quantity", 1)),
            allowed_regions=allowed_regs,
            allow_region_change=allow_reg_change,
        )
        resilience = compute_young_daly_checkpoint_schedule(
            execution_minutes=exec_min,
            base_cost_eur=est_cost,
            provisioning_model=str(p.get("provisioning_model", "100% Standard")),
            supports_checkpointing=supports_ckpt,
            checkpoint_interval_minutes=ckpt_int,
            quantity=int(p.get("quantity", 1)),
        )

        p["carbon_metrics"] = carbon
        p["resilience_metrics"] = resilience
        p["_obj"] = {
            "cost": est_cost,
            "latency": total_min,
            "risk": resilience["interruption_risk_score"],
            "carbon": carbon["carbon_emissions_g_co2"],
        }
        p["plan_id"] = plan_id
        enriched.append(p)

    # Compute Pareto dominance
    frontier_ids: list[str] = []
    for i, cand_a in enumerate(enriched):
        dominated_by: list[str] = []
        obj_a = cand_a["_obj"]
        for j, cand_b in enumerate(enriched):
            if i == j:
                continue
            obj_b = cand_b["_obj"]
            # B dominates A if B is <= A on all 4 axes and strictly < A on at least 1 axis
            all_le = (
                obj_b["cost"] <= obj_a["cost"]
                and obj_b["latency"] <= obj_a["latency"]
                and obj_b["risk"] <= obj_a["risk"]
                and obj_b["carbon"] <= obj_a["carbon"]
            )
            any_lt = (
                obj_b["cost"] < obj_a["cost"]
                or obj_b["latency"] < obj_a["latency"]
                or obj_b["risk"] < obj_a["risk"]
                or obj_b["carbon"] < obj_a["carbon"]
            )
            if all_le and any_lt:
                dominated_by.append(cand_b["plan_id"])

        is_optimal = len(dominated_by) == 0
        cand_a["is_pareto_optimal"] = is_optimal
        cand_a["dominated_by"] = dominated_by
        cand_a["pareto_rank"] = 1 if is_optimal else 1 + len(dominated_by)
        if is_optimal:
            frontier_ids.append(cand_a["plan_id"])

    # Normalize utility scores (0..100, higher is better)
    if enriched:
        max_cost = max(max(c["_obj"]["cost"] for c in enriched), 0.01)
        max_lat = max(max(c["_obj"]["latency"] for c in enriched), 0.01)
        max_carb = max(max(c["_obj"]["carbon"] for c in enriched), 0.01)

        for c in enriched:
            o = c["_obj"]
            s_cost = round(100.0 * (1.0 - (o["cost"] / max_cost) * 0.85), 1)
            s_speed = round(100.0 * (1.0 - (o["latency"] / max_lat) * 0.85), 1)
            s_resil = round(100.0 * (1.0 - o["risk"]), 1)
            s_green = round(100.0 * (1.0 - (o["carbon"] / max_carb) * 0.85), 1)
            s_balanced = round(0.35 * s_cost + 0.30 * s_speed + 0.20 * s_resil + 0.15 * s_green, 1)
            c["utility_scores"] = {
                "cost_efficiency": s_cost,
                "speed_score": s_speed,
                "resilience_score": s_resil,
                "carbon_score": s_green,
                "balanced_utility": s_balanced,
            }
            del c["_obj"]

    recommendations_by_objective: dict[str, str | None] = {
        "min_cost": min(enriched, key=lambda x: x["estimated_cost_eur"])["plan_id"] if enriched else None,
        "min_latency": min(enriched, key=lambda x: x["total_time_to_result_minutes"])["plan_id"] if enriched else None,
        "min_carbon": min(enriched, key=lambda x: x["carbon_metrics"]["carbon_emissions_g_co2"])["plan_id"] if enriched else None,
        "max_resilience": min(enriched, key=lambda x: x["resilience_metrics"]["interruption_risk_score"])["plan_id"] if enriched else None,
        "balanced_utility": max(enriched, key=lambda x: x["utility_scores"]["balanced_utility"])["plan_id"] if enriched else None,
    }

    return {
        "plans": enriched,
        "pareto_frontier_plan_ids": frontier_ids,
        "pareto_optimal_count": len(frontier_ids),
        "total_evaluated": len(enriched),
        "recommendations_by_objective": recommendations_by_objective,
    }


def simulate_what_if_scenarios(
    plans: list[ExecutionPlan | dict[str, Any]],
    workload_profile: WorkloadProfile | dict[str, Any] | None = None,
    forced_preemptions: int | None = None,
    budget_shock_pct: float = -20.0,
    deadline_compression_pct: float = -25.0,
) -> dict[str, Any]:
    """Run deterministic What-If stress tests across candidate execution plans.

    Evaluates how each plan survives:
    1. Spot Preemption Storm (e.g., 2 forced Spot preemptions mid-flight).
    2. Budget Shock (e.g., -20% cut to declared budget ceiling).
    3. Deadline Compression (e.g., SLA deadline tightened by 25%).
    """
    if isinstance(workload_profile, dict):
        try:
            profile = WorkloadProfile(**workload_profile)
        except Exception:
            profile = None
    else:
        profile = workload_profile

    base_budget = getattr(profile, "budget_amount", None) or getattr(profile, "max_cost_limit_eur", None)
    base_deadline = getattr(profile, "deadline_minutes_from_start", None)
    supports_ckpt = bool(getattr(profile, "supports_checkpointing", False)) if profile else False
    ckpt_int = getattr(profile, "checkpoint_interval_minutes", None) if profile else None

    k_preempt = forced_preemptions if forced_preemptions is not None else 2
    shocked_budget = (
        round(float(base_budget) * (1.0 + budget_shock_pct / 100.0), 2)
        if base_budget is not None
        else None
    )
    compressed_deadline = (
        round(float(base_deadline) * (1.0 + deadline_compression_pct / 100.0), 2)
        if base_deadline is not None
        else None
    )

    scenario_results: list[dict[str, Any]] = []

    for raw in plans:
        p = raw.model_dump() if isinstance(raw, ExecutionPlan) else dict(raw)
        plan_id = str(p.get("plan_id") or p.get("plan_type") or "plan")
        prov = str(p.get("provisioning_model", "100% Standard"))
        exec_min = float(p.get("estimated_execution_minutes") or p.get("total_time_to_result_minutes") or 60.0)
        total_min = float(p.get("total_time_to_result_minutes") or exec_min)
        est_cost = float(p.get("estimated_cost_eur") or 0.0)

        # Scenario 1: Spot Preemption Storm
        is_spot_exposed = "spot" in prov.lower()
        actual_preemptions = k_preempt if is_spot_exposed else 0
        if "80% spot" in prov.lower():
            # Hedged keeps 20% standard core baseline alive, halving recovery penalty
            effective_preemptions = actual_preemptions * 0.5
        else:
            effective_preemptions = float(actual_preemptions)

        yd = compute_young_daly_checkpoint_schedule(
            execution_minutes=exec_min,
            base_cost_eur=est_cost,
            provisioning_model=prov,
            supports_checkpointing=supports_ckpt,
            checkpoint_interval_minutes=ckpt_int,
        )
        interval_used = yd["configured_checkpoint_interval_minutes"] or yd["young_daly_optimal_interval_minutes"]
        if supports_ckpt:
            wasted_per_hit = (interval_used / 2.0) + 2.5
        else:
            wasted_per_hit = (exec_min * 0.5) + 3.0

        storm_extra_minutes = round(effective_preemptions * wasted_per_hit, 2)
        storm_duration = round(total_min + storm_extra_minutes, 2)
        storm_cost = round(est_cost * (1.0 + (storm_extra_minutes / max(1.0, exec_min))), 2)
        survives_storm = (
            (base_deadline is None or storm_duration <= base_deadline)
            and (base_budget is None or storm_cost <= base_budget)
        )

        # Scenario 2: Budget Shock
        survives_budget_shock = (shocked_budget is None) or (est_cost <= shocked_budget)

        # Scenario 3: Deadline Compression
        survives_deadline_crunch = (compressed_deadline is None) or (total_min <= compressed_deadline)

        resilience_verdict = "HIGHLY_RESILIENT"
        if not survives_storm or not survives_budget_shock or not survives_deadline_crunch:
            if survives_storm and (survives_budget_shock or survives_deadline_crunch):
                resilience_verdict = "MODERATELY_RESILIENT"
            else:
                resilience_verdict = "VULNERABLE_UNDER_STRESS"

        mitigations: list[str] = []
        if is_spot_exposed and not supports_ckpt:
            mitigations.append(
                f"Enable checkpointing (optimal interval: {yd['young_daly_optimal_interval_minutes']}m) "
                f"to reduce Spot storm penalty from +{storm_extra_minutes:.1f}m to "
                f"+{effective_preemptions * ((yd['young_daly_optimal_interval_minutes'] / 2.0) + 2.5):.1f}m."
            )
        if not survives_budget_shock and shocked_budget is not None:
            mitigations.append(
                f"Plan exceeds shocked budget ({est_cost:.2f}EUR > {shocked_budget:.2f}EUR); "
                f"switch to 100% Spot or reduce vCPU count."
            )
        if not survives_deadline_crunch and compressed_deadline is not None:
            mitigations.append(
                f"Plan exceeds compressed deadline ({total_min:.1f}m > {compressed_deadline:.1f}m); "
                f"scale up vCPU parallelism."
            )

        scenario_results.append({
            "plan_id": plan_id,
            "title": p.get("title", plan_id),
            "machine_type": p.get("machine_type"),
            "provisioning_model": prov,
            "baseline_cost_eur": est_cost,
            "baseline_duration_minutes": total_min,
            "resilience_verdict": resilience_verdict,
            "spot_storm_scenario": {
                "forced_preemptions": actual_preemptions,
                "storm_duration_minutes": storm_duration,
                "storm_cost_eur": storm_cost,
                "extra_delay_minutes": storm_extra_minutes,
                "survives": survives_storm,
            },
            "budget_shock_scenario": {
                "shock_pct": budget_shock_pct,
                "shocked_budget_eur": shocked_budget,
                "survives": survives_budget_shock,
                "headroom_eur": round((shocked_budget - est_cost), 2) if shocked_budget is not None else None,
            },
            "deadline_compression_scenario": {
                "compression_pct": deadline_compression_pct,
                "compressed_deadline_minutes": compressed_deadline,
                "survives": survives_deadline_crunch,
                "slack_minutes": round((compressed_deadline - total_min), 2) if compressed_deadline is not None else None,
            },
            "mitigation_recommendations": mitigations,
        })

    most_resilient_id = None
    if scenario_results:
        most_resilient = max(
            scenario_results,
            key=lambda r: (
                int(r["spot_storm_scenario"]["survives"])
                + int(r["budget_shock_scenario"]["survives"])
                + int(r["deadline_compression_scenario"]["survives"]),
                -r["spot_storm_scenario"]["storm_cost_eur"],
            ),
        )
        most_resilient_id = most_resilient["plan_id"]

    return {
        "scenarios_evaluated": ["spot_storm", "budget_shock", "deadline_compression"],
        "parameters": {
            "forced_preemptions": k_preempt,
            "budget_shock_pct": budget_shock_pct,
            "shocked_budget_eur": shocked_budget,
            "deadline_compression_pct": deadline_compression_pct,
            "compressed_deadline_minutes": compressed_deadline,
        },
        "most_resilient_plan_id": most_resilient_id,
        "plan_stress_results": scenario_results,
    }
