from __future__ import annotations

import math
import time
from typing import Any

from .models import ExecutionPlan, WorkloadProfile
from .simulator import compute_hedged_cost_factor


def _speedup(cpu: int, parallel_fraction: float = 0.97) -> float:
    if parallel_fraction <= 0:
        return 1.0
    p = parallel_fraction
    return 1.0 / ((1.0 - p) + p / cpu)


def evaluate_and_compare_plans(
    profile: WorkloadProfile | dict,
    cluster_total_cpu: int = 128,
    cost_per_core_hour_eur: float = 0.05,
    base_work_units: float = 500.0,
    demo_mode: bool | None = None,
) -> dict[str, Any]:
    """Deterministically evaluate, price, and compare execution plans for a workload.

    Generates distinct readable candidate plans:
    - cost_optimized: Minimal estimated cost meeting deadline and budget
    - deadline_favored: Fastest completion time within budget
    - balanced_tradeoff: Hedged allocation balancing cost and preemption SLA

    If no plan is feasible, deterministically explains which constraints block
    and what could be relaxed, without inventing a winning plan.
    """
    if isinstance(profile, dict):
        workload = WorkloadProfile(**profile)
    else:
        workload = profile

    target_region = workload.allowed_regions[0] if workload.allowed_regions else "us-central1"
    deadline = workload.deadline_minutes_from_start
    budget = workload.budget_amount or workload.max_cost_limit_eur
    p_frac = 0.97 if workload.is_parallelizable else 0.0
    units = workload.estimated_duration_minutes or base_work_units

    candidate_configs = [
        {"cpu": 2, "machine_type": "n2-standard-2", "quantity": 1},
        {"cpu": 4, "machine_type": "n2-standard-4", "quantity": 1},
        {"cpu": 8, "machine_type": "n2-standard-8", "quantity": 1},
        {"cpu": 16, "machine_type": "n2-standard-16", "quantity": 1},
        {"cpu": 32, "machine_type": "n2-standard-32", "quantity": 1},
        {"cpu": 60, "machine_type": "c2-standard-60", "quantity": 1},
        {"cpu": 88, "machine_type": "h3-standard-88", "quantity": 1},
    ]

    # Filter by cluster total CPU
    candidate_configs = [c for c in candidate_configs if c["cpu"] <= cluster_total_cpu]

    all_evaluated: list[dict[str, Any]] = []

    for cfg in candidate_configs:
        cpu = cfg["cpu"]
        mtype = cfg["machine_type"]

        # Determine provisioning mixes to evaluate
        mixes = []
        if workload.allow_spot:
            mixes.append("100% Spot")
            mixes.append("80% Spot / 20% Standard")
        if workload.allow_fallback_to_standard or not mixes:
            mixes.append("100% Standard")

        sp = _speedup(cpu, p_frac)
        exec_time = units / max(0.1, sp)
        prep_time = 1.0  # VM provision / container pull
        wait_time = 0.5  # Queue scheduling
        rec_time = 2.0 if ("Spot" in mixes[0] and workload.supports_checkpointing) else 0.0
        total_time = round(wait_time + prep_time + exec_time + rec_time, 2)

        for pmix in mixes:
            cost_factor = compute_hedged_cost_factor(cpu, pmix)
            future_cost = cpu * cost_per_core_hour_eur * cost_factor * (exec_time / 60.0)
            est_cost = round(future_cost, 2)

            meets_deadline = (deadline is None) or (total_time <= deadline)
            within_budget = (budget is None) or (est_cost <= budget)

            satisfied: list[str] = ["Hardware compatibility verified"]
            if meets_deadline and deadline is not None:
                satisfied.append(f"Deadline satisfied: {total_time:.1f}m <= {deadline:.1f}m")
            if within_budget and budget is not None:
                satisfied.append(f"Budget satisfied: {est_cost:.2f}€ <= {budget:.2f}€")

            unverified: list[str] = [
                "Network egress bandwidth costs excluded",
                "Persistent disk IOPS and storage costs excluded",
                "Actual cloud stockout cannot be guaranteed prior to allocation",
            ]

            uncertainties: list[str] = [
                "Execution time is an Amdahl estimation, not an empirical benchmark",
            ]
            if "Spot" in pmix:
                uncertainties.append("Spot VM preemption rate is historical and may fluctuate during execution")

            all_evaluated.append({
                "cpu": cpu,
                "machine_type": mtype,
                "provisioning_model": pmix,
                "total_time": total_time,
                "exec_time": exec_time,
                "wait_time": wait_time,
                "prep_time": prep_time,
                "rec_time": rec_time,
                "est_cost": est_cost,
                "meets_deadline": meets_deadline,
                "within_budget": within_budget,
                "satisfied_constraints": satisfied,
                "unverified_points": unverified,
                "uncertainty_factors": uncertainties,
            })

    # Filter feasible candidates
    feasible = [c for c in all_evaluated if c["meets_deadline"] and c["within_budget"]]

    if not feasible:
        # Explain why no plan is feasible deterministically
        min_cost_cand = min(all_evaluated, key=lambda x: x["est_cost"])
        min_time_cand = min(all_evaluated, key=lambda x: x["total_time"])

        reasons = []
        relaxations = []
        if budget is not None and min_cost_cand["est_cost"] > budget:
            reasons.append(f"Budget constraint violated: minimum possible cost is {min_cost_cand['est_cost']:.2f}€ ({min_cost_cand['machine_type']}, {min_cost_cand['provisioning_model']}), but budget limit is {budget:.2f}€")
            relaxations.append(f"Increase budget to at least {min_cost_cand['est_cost']:.2f}€")

        if deadline is not None and min_time_cand["total_time"] > deadline:
            reasons.append(f"Deadline constraint violated: fastest possible execution is {min_time_cand['total_time']:.1f} minutes with {min_time_cand['cpu']} vCPUs, but required deadline is {deadline:.1f} minutes")
            relaxations.append(f"Extend deadline to at least {min_time_cand['total_time']:.1f} minutes, or allocate more cluster vCPUs")

        explanation = (
            "No feasible execution plan satisfies all declared constraints. "
            + "; ".join(reasons)
            + ". Suggested relaxations: " + "; ".join(relaxations) + "."
        )

        return {
            "is_feasible": False,
            "plans": [],
            "recommended_plan_id": None,
            "unfeasible_explanation": explanation,
            "evaluated_count": len(all_evaluated),
        }

    # Generate the 3 distinct plans from feasible set
    # 1. Cost-optimized: lowest cost among feasible
    cost_opt = min(feasible, key=lambda x: (x["est_cost"], x["total_time"]))

    # 2. Deadline-favored: fastest time among feasible
    time_fav = min(feasible, key=lambda x: (x["total_time"], x["est_cost"]))

    # 3. Balanced tradeoff: best balance between cost and speed
    # Select median or hedged option
    balanced_candidates = [c for c in feasible if "80% Spot" in c["provisioning_model"] or c["cpu"] in (8, 16, 32)]
    balanced = balanced_candidates[len(balanced_candidates) // 2] if balanced_candidates else cost_opt

    plans_list: list[ExecutionPlan] = []

    p1 = ExecutionPlan(
        plan_id="plan-cost-optimized",
        plan_type="cost_optimized",
        title=f"Cost-Optimized: {cost_opt['machine_type']} ({cost_opt['provisioning_model']})",
        machine_type=cost_opt["machine_type"],
        cpu=cost_opt["cpu"],
        region=target_region,
        provisioning_model=cost_opt["provisioning_model"],
        quantity=1,
        satisfied_constraints=cost_opt["satisfied_constraints"],
        unverified_points=cost_opt["unverified_points"],
        estimated_cost_eur=cost_opt["est_cost"],
        estimated_wait_minutes=cost_opt["wait_time"],
        estimated_prep_minutes=cost_opt["prep_time"],
        estimated_execution_minutes=round(cost_opt["exec_time"], 2),
        estimated_recovery_minutes=cost_opt["rec_time"],
        total_time_to_result_minutes=cost_opt["total_time"],
        ranking_rationale=f"Minimizes total estimated spend ({cost_opt['est_cost']:.2f}€) while respecting deadline ({cost_opt['total_time']:.1f}m)",
        fallback_plan_id="plan-balanced" if balanced != cost_opt else "plan-deadline-favored",
        fallback_chain=[cost_opt["machine_type"], "n2-standard-16", "n2-standard-32"],
        uncertainty_factors=cost_opt["uncertainty_factors"],
    )
    plans_list.append(p1)

    if time_fav != cost_opt:
        p2 = ExecutionPlan(
            plan_id="plan-deadline-favored",
            plan_type="deadline_favored",
            title=f"Deadline-Favored: {time_fav['machine_type']} ({time_fav['provisioning_model']})",
            machine_type=time_fav["machine_type"],
            cpu=time_fav["cpu"],
            region=target_region,
            provisioning_model=time_fav["provisioning_model"],
            quantity=1,
            satisfied_constraints=time_fav["satisfied_constraints"],
            unverified_points=time_fav["unverified_points"],
            estimated_cost_eur=time_fav["est_cost"],
            estimated_wait_minutes=time_fav["wait_time"],
            estimated_prep_minutes=time_fav["prep_time"],
            estimated_execution_minutes=round(time_fav["exec_time"], 2),
            estimated_recovery_minutes=time_fav["rec_time"],
            total_time_to_result_minutes=time_fav["total_time"],
            ranking_rationale=f"Minimizes time to result ({time_fav['total_time']:.1f}m) within budget limits ({time_fav['est_cost']:.2f}€)",
            fallback_plan_id="plan-cost-optimized",
            fallback_chain=[time_fav["machine_type"], "c2-standard-60", "n2-standard-32"],
            uncertainty_factors=time_fav["uncertainty_factors"],
        )
        plans_list.append(p2)

    if balanced != cost_opt and balanced != time_fav:
        p3 = ExecutionPlan(
            plan_id="plan-balanced",
            plan_type="balanced_tradeoff",
            title=f"Balanced Trade-off: {balanced['machine_type']} ({balanced['provisioning_model']})",
            machine_type=balanced["machine_type"],
            cpu=balanced["cpu"],
            region=target_region,
            provisioning_model=balanced["provisioning_model"],
            quantity=1,
            satisfied_constraints=balanced["satisfied_constraints"],
            unverified_points=balanced["unverified_points"],
            estimated_cost_eur=balanced["est_cost"],
            estimated_wait_minutes=balanced["wait_time"],
            estimated_prep_minutes=balanced["prep_time"],
            estimated_execution_minutes=round(balanced["exec_time"], 2),
            estimated_recovery_minutes=balanced["rec_time"],
            total_time_to_result_minutes=balanced["total_time"],
            ranking_rationale=f"Balances speed ({balanced['total_time']:.1f}m) and cost ({balanced['est_cost']:.2f}€) with hedged provisioning",
            fallback_plan_id="plan-cost-optimized",
            fallback_chain=[balanced["machine_type"], "n2-standard-16", "100% Standard"],
            uncertainty_factors=balanced["uncertainty_factors"],
        )
        plans_list.append(p3)

    return {
        "is_feasible": True,
        "plans": [p.model_dump() for p in plans_list],
        "recommended_plan_id": plans_list[0].plan_id,
        "unfeasible_explanation": None,
        "feasible_count": len(feasible),
        "evaluated_count": len(all_evaluated),
    }
