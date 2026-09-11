from __future__ import annotations

import math
import os
import time
from typing import Any

from .governance import make_plan_id, plan_fingerprint
from .models import ExecutionPlan, WorkloadProfile
from .simulator import (
    SIMULATED_MACHINE_CATALOG,
    compute_hedged_cost_factor,
    normalize_provisioning_model,
)


def _speedup(cpu: int, parallel_fraction: float = 0.97) -> float:
    if parallel_fraction <= 0:
        return 1.0
    p = parallel_fraction
    return 1.0 / ((1.0 - p) + p / cpu)


#: Shapes the planner may propose, with the hardware each one actually provides.
#: Memory and GPU are modelled explicitly so a request for 8 GPUs can no longer
#: be "satisfied" by a plan carrying zero GPUs.
PLANNER_CATALOG: list[dict[str, Any]] = [
    {"cpu": 2, "machine_type": "n2-standard-2", "memory_gb": 8.0, "gpu": 0, "gpu_type": None},
    {"cpu": 4, "machine_type": "n2-standard-4", "memory_gb": 16.0, "gpu": 0, "gpu_type": None},
    {"cpu": 8, "machine_type": "n2-standard-8", "memory_gb": 32.0, "gpu": 0, "gpu_type": None},
    {"cpu": 16, "machine_type": "n2-standard-16", "memory_gb": 64.0, "gpu": 0, "gpu_type": None},
    {"cpu": 32, "machine_type": "n2-standard-32", "memory_gb": 128.0, "gpu": 0, "gpu_type": None},
    {"cpu": 60, "machine_type": "c2-standard-60", "memory_gb": 240.0, "gpu": 0, "gpu_type": None},
    {"cpu": 88, "machine_type": "h3-standard-88", "memory_gb": 352.0, "gpu": 0, "gpu_type": None},
    {"cpu": 4, "machine_type": "g2-standard-4", "memory_gb": 16.0, "gpu": 1, "gpu_type": "NVIDIA_L4"},
    {"cpu": 12, "machine_type": "a2-highgpu-1g", "memory_gb": 85.0, "gpu": 1, "gpu_type": "NVIDIA_TESLA_A100"},
    {"cpu": 48, "machine_type": "a2-highgpu-4g", "memory_gb": 340.0, "gpu": 4, "gpu_type": "NVIDIA_TESLA_A100"},
]

#: Machine types the Slurm adapter can actually run. A plan naming anything else
#: is still allowed to appear, but must be labelled as not executable by that
#: runtime rather than being silently offered as runnable.
SLURM_EXECUTABLE_MACHINE_TYPES = {"n2-standard-2", "c2-standard-60", "h3-standard-88"}


def _resolve_budget(workload: WorkloadProfile) -> tuple[float | None, str | None]:
    """Return (budget, invalid_reason).

    ``budget_amount or max_cost_limit_eur`` was wrong: ``0.0`` is falsy, so a
    zero budget silently became "no budget constraint" and every paid plan was
    accepted. Zero is a real constraint and is preserved here.
    """
    budget = workload.budget_amount
    if budget is None:
        budget = workload.max_cost_limit_eur
    if budget is None:
        return None, None
    try:
        value = float(budget)
    except (TypeError, ValueError):
        return None, f"Budget value '{budget}' is not a number."
    if value != value or value in (float("inf"), float("-inf")):
        return None, f"Budget value '{budget}' is not a finite number."
    if value < 0:
        return None, f"Budget must not be negative (received {value})."
    return value, None


def _compatibility_report(
    cfg: dict[str, Any],
    workload: WorkloadProfile,
    cluster_total_cpu: int,
) -> list[str]:
    """Return the reasons ``cfg`` cannot serve ``workload`` (empty == compatible)."""
    reasons: list[str] = []

    needed_cpu = workload.cpu_requested or 0
    if needed_cpu and cfg["cpu"] < needed_cpu:
        reasons.append(
            f"provides {cfg['cpu']} vCPUs but {needed_cpu} were requested"
        )

    needed_gpu = workload.gpu_requested or 0
    if needed_gpu and cfg["gpu"] < needed_gpu:
        reasons.append(
            f"provides {cfg['gpu']} GPUs but {needed_gpu} were requested"
        )
    if not needed_gpu and cfg["gpu"]:
        reasons.append("carries GPUs that the workload did not request")

    if workload.memory_mb_requested:
        needed_gb = workload.memory_mb_requested / 1024.0
        if cfg["memory_gb"] < needed_gb:
            reasons.append(
                f"provides {cfg['memory_gb']:.0f} GB RAM but {needed_gb:.0f} GB were requested"
            )

    if cfg["cpu"] > cluster_total_cpu:
        reasons.append(
            f"needs {cfg['cpu']} vCPUs but the cluster exposes only {cluster_total_cpu}"
        )

    allowed_machines = getattr(workload, "allowed_machine_types", None) or []
    if allowed_machines and cfg["machine_type"] not in allowed_machines:
        reasons.append(
            f"machine type '{cfg['machine_type']}' is not in the allowed list"
        )

    for constraint in workload.hardware_constraints or []:
        token = str(constraint).strip().upper()
        if token == "AVX512" and cfg["machine_type"].startswith("g2-"):
            reasons.append("does not provide AVX-512")

    return reasons


def _provisioning_options(workload: WorkloadProfile) -> list[str]:
    """Provisioning models this workload may legitimately run under.

    Spot is only offered when the workload can actually tolerate interruption.
    Cheapness is not a reason to assume a job is preemptible.
    """
    options: list[str] = []
    tolerates_interruption = bool(workload.is_interruptible)

    if workload.allow_spot and tolerates_interruption:
        options.append("100% Spot")
        options.append("80% Spot / 20% Standard")

    if workload.allow_fallback_to_standard or not options:
        options.append("100% Standard")

    # De-duplicate while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for opt in options:
        if opt not in seen:
            seen.add(opt)
            ordered.append(opt)
    return ordered


def _capacity_check_enabled(explicit: bool | None) -> bool:
    """Whether to consult a quota source while comparing plans.

    The check calls the Compute Engine API, so it is switched off in the test
    suite (``tests/conftest.py``) the same way ``MOCK_SLURM`` switches off the
    real controller. Production keeps it on: a comparison that omits the
    capacity axis is a comparison of two of the three declared dimensions.
    """
    if explicit is not None:
        return explicit
    return os.getenv("AGENTGRID_PLAN_CAPACITY_CHECK", "true").lower() not in (
        "false",
        "0",
        "no",
    )


def annotate_plans_with_capacity(
    plans: list[ExecutionPlan],
    project_id: str | None = None,
    demo_mode: bool | None = None,
) -> list[ExecutionPlan]:
    """Attach a quota verdict to each plan, one lookup per distinct request.

    Never invents a verdict: an unreachable or silent quota source leaves
    ``QUOTA_UNKNOWN`` with the reason attached, which is a different statement
    from ``QUOTA_AVAILABLE``.
    """
    from .capacity_advisor import check_quota_availability

    project = project_id or os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("PROJECT_ID")
    if not project:
        for plan in plans:
            plan.capacity_status = "QUOTA_UNKNOWN"
            plan.capacity_source = "no_project_configured"
            plan.capacity_detail = (
                "No GCP project is configured (GOOGLE_CLOUD_PROJECT / PROJECT_ID), "
                "so no quota could be read for this plan."
            )
        return plans

    cache: dict[tuple[str, int, int, str], dict[str, Any]] = {}
    for plan in plans:
        total_cpu = plan.cpu * max(plan.quantity, 1)
        total_gpu = plan.gpu * max(plan.quantity, 1)
        model = "SPOT" if "spot" in (plan.provisioning_model or "").lower() else "STANDARD"
        key = (plan.region, total_cpu, total_gpu, model)
        if key not in cache:
            try:
                cache[key] = check_quota_availability(
                    project_id=project,
                    region=plan.region,
                    cpu_needed=total_cpu,
                    gpu_needed=total_gpu,
                    provisioning_model=model,
                    demo_mode=demo_mode,
                )
            except Exception as exc:  # pragma: no cover - defensive
                cache[key] = {
                    "status": "QUOTA_UNKNOWN",
                    "reason": f"Quota lookup failed: {type(exc).__name__}: {exc}",
                    "data_provenance": "unavailable",
                }
        verdict = cache[key]
        plan.capacity_status = verdict.get("status", "QUOTA_UNKNOWN")
        plan.capacity_detail = verdict.get("reason")
        plan.capacity_source = verdict.get("data_provenance", "unknown")
        if plan.capacity_status == "QUOTA_EXCEEDED":
            plan.unverified_points = list(plan.unverified_points) + [
                f"Project quota does not currently authorise {total_cpu} vCPU"
                + (f" and {total_gpu} GPU" if total_gpu else "")
                + f" in {plan.region}."
            ]
    return plans


def evaluate_and_compare_plans(
    profile: WorkloadProfile | dict,
    cluster_total_cpu: int = 128,
    cost_per_core_hour_eur: float = 0.05,
    base_work_units: float = 500.0,
    demo_mode: bool | None = None,
    runtime_kind: str | None = None,
    check_capacity: bool | None = None,
) -> dict[str, Any]:
    """Deterministically evaluate, price and compare execution plans.

    Produces up to three readable candidates -- ``cost_optimized``,
    ``deadline_favored`` and ``balanced_tradeoff`` -- drawn only from shapes that
    are genuinely compatible with the workload profile and the target runtime.

    When nothing satisfies the declared constraints, it returns an empty plan
    list plus a structured explanation of what blocks and what could be relaxed.
    It never invents a winner, and it never raises on degenerate input such as a
    one-CPU cluster or a zero budget.
    """
    if isinstance(profile, dict):
        workload = WorkloadProfile(**profile)
    else:
        workload = profile

    target_region = workload.allowed_regions[0] if workload.allowed_regions else "us-central1"
    target_zone = workload.allowed_zones[0] if workload.allowed_zones else None
    deadline = workload.deadline_minutes_from_start
    budget, budget_error = _resolve_budget(workload)
    p_frac = 0.97 if workload.is_parallelizable else 0.0
    units = workload.estimated_duration_minutes or base_work_units

    runtime_kind = (runtime_kind or "simulator").lower().strip()

    #: Costs here are modelled, not quoted. Labelled explicitly so a synthetic
    #: rate is never mistaken for an observed GCP price.
    cost_basis = "modelled_flat_rate_hypothesis"
    cost_basis_detail = (
        f"Costs are a deterministic model at {cost_per_core_hour_eur:.3f} EUR per vCPU-hour, "
        f"not an observed or quoted cloud price."
    )

    structural_blockers: list[str] = []
    relaxations: list[str] = []

    if budget_error:
        structural_blockers.append(f"Invalid budget: {budget_error}")
        relaxations.append("Provide a non-negative numeric budget, or omit it entirely.")

    if cluster_total_cpu is None or cluster_total_cpu < 1:
        structural_blockers.append(
            f"Cluster capacity is {cluster_total_cpu} vCPUs, which cannot host any workload."
        )
        relaxations.append("Provision at least one usable compute node.")

    if deadline is not None and deadline <= 0:
        structural_blockers.append(f"Deadline of {deadline} minutes leaves no time to execute.")
        relaxations.append("Set a positive deadline.")

    # ------------------------------------------------------------------
    # Compatibility filtering: only shapes that can actually serve the request.
    # ------------------------------------------------------------------
    compatible: list[dict[str, Any]] = []
    incompatible: list[dict[str, str]] = []

    for cfg in PLANNER_CATALOG:
        reasons = _compatibility_report(cfg, workload, cluster_total_cpu or 0)
        if reasons:
            incompatible.append(
                {"machine_type": cfg["machine_type"], "reason": "; ".join(reasons)}
            )
        else:
            compatible.append(cfg)

    provisioning_options = _provisioning_options(workload)

    if not compatible:
        needed = []
        if workload.cpu_requested:
            needed.append(f"{workload.cpu_requested} vCPUs")
        if workload.gpu_requested:
            needed.append(f"{workload.gpu_requested} GPUs")
        if workload.memory_mb_requested:
            needed.append(f"{workload.memory_mb_requested / 1024.0:.0f} GB RAM")
        requirement = ", ".join(needed) if needed else "the declared requirements"

        structural_blockers.append(
            f"No catalogued machine shape can provide {requirement} within a "
            f"{cluster_total_cpu}-vCPU cluster."
        )
        relaxations.append(
            "Reduce the requested resources, split the workload across nodes, "
            "or extend the cluster with a larger machine family."
        )

    if structural_blockers:
        explanation = (
            "No feasible execution plan satisfies all declared constraints. "
            + "; ".join(structural_blockers)
            + ". Suggested relaxations: "
            + "; ".join(relaxations or ["Relax at least one declared constraint."])
            + "."
        )
        return {
            "is_feasible": False,
            "plans": [],
            "recommended_plan_id": None,
            "unfeasible_explanation": explanation,
            "blocking_constraints": structural_blockers,
            "suggested_relaxations": relaxations,
            "incompatible_candidates": incompatible,
            "evaluated_count": 0,
            "cost_basis": cost_basis,
            "cost_basis_detail": cost_basis_detail,
        }

    # ------------------------------------------------------------------
    # Cost / time evaluation over compatible shapes only.
    # ------------------------------------------------------------------
    all_evaluated: list[dict[str, Any]] = []

    for cfg in compatible:
        cpu = cfg["cpu"]
        mtype = cfg["machine_type"]

        sp = _speedup(cpu, p_frac)
        exec_time = units / max(0.1, sp)
        prep_time = 1.0
        wait_time = 0.5

        executable_here = (
            runtime_kind != "slurm" or mtype in SLURM_EXECUTABLE_MACHINE_TYPES
        )

        for pmix in provisioning_options:
            rec_time = 2.0 if ("Spot" in pmix and workload.supports_checkpointing) else 0.0
            total_time = round(wait_time + prep_time + exec_time + rec_time, 2)

            cost_factor = compute_hedged_cost_factor(cpu, pmix)
            est_cost = round(cpu * cost_per_core_hour_eur * cost_factor * (exec_time / 60.0), 2)

            meets_deadline = (deadline is None) or (total_time <= deadline)
            within_budget = (budget is None) or (est_cost <= budget)

            satisfied: list[str] = [
                f"Hardware compatible: {cfg['cpu']} vCPUs, {cfg['memory_gb']:.0f} GB RAM, "
                f"{cfg['gpu']} GPU(s)"
            ]
            if meets_deadline and deadline is not None:
                satisfied.append(f"Deadline satisfied: {total_time:.1f}m <= {deadline:.1f}m")
            if within_budget and budget is not None:
                satisfied.append(f"Budget satisfied: {est_cost:.2f}EUR <= {budget:.2f}EUR")
            if not workload.is_interruptible:
                satisfied.append("Non-interruptible workload: Spot provisioning excluded")

            unverified: list[str] = [
                "Network egress bandwidth costs excluded",
                "Persistent disk IOPS and storage costs excluded",
                "Actual cloud stockout cannot be guaranteed prior to allocation",
                cost_basis_detail,
            ]
            if not executable_here:
                unverified.append(
                    f"Machine type '{mtype}' is not executable by the active Slurm runtime"
                )

            uncertainties: list[str] = [
                "Execution time is an Amdahl estimation, not an empirical benchmark",
            ]
            if "Spot" in pmix:
                uncertainties.append(
                    "Spot VM preemption rate is historical and may fluctuate during execution"
                )

            all_evaluated.append({
                "cpu": cpu,
                "gpu": cfg["gpu"],
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
                "executable_on_runtime": executable_here,
                "satisfied_constraints": satisfied,
                "unverified_points": unverified,
                "uncertainty_factors": uncertainties,
            })


    # ------------------------------------------------------------------
    # Feasibility selection
    # ------------------------------------------------------------------
    feasible = [
        c for c in all_evaluated
        if c["meets_deadline"] and c["within_budget"] and c["executable_on_runtime"]
    ]

    if not feasible:
        reasons: list[str] = []
        relax: list[str] = []

        runnable = [c for c in all_evaluated if c["executable_on_runtime"]]
        if not runnable and all_evaluated:
            reasons.append(
                f"No compatible machine type is executable by the '{runtime_kind}' runtime "
                f"(it accepts only: {', '.join(sorted(SLURM_EXECUTABLE_MACHINE_TYPES))})"
            )
            relax.append("Target a runtime that supports the required machine family")

        pool = runnable or all_evaluated
        if pool:
            min_cost_cand = min(pool, key=lambda x: x["est_cost"])
            min_time_cand = min(pool, key=lambda x: x["total_time"])

            if budget is not None and min_cost_cand["est_cost"] > budget:
                reasons.append(
                    f"Budget constraint violated: minimum possible cost is "
                    f"{min_cost_cand['est_cost']:.2f}EUR ({min_cost_cand['machine_type']}, "
                    f"{min_cost_cand['provisioning_model']}), but budget limit is {budget:.2f}EUR"
                )
                relax.append(f"Increase budget to at least {min_cost_cand['est_cost']:.2f}EUR")

            if deadline is not None and min_time_cand["total_time"] > deadline:
                reasons.append(
                    f"Deadline constraint violated: fastest possible execution is "
                    f"{min_time_cand['total_time']:.1f} minutes with {min_time_cand['cpu']} vCPUs, "
                    f"but required deadline is {deadline:.1f} minutes"
                )
                relax.append(
                    f"Extend deadline to at least {min_time_cand['total_time']:.1f} minutes, "
                    f"or allocate more cluster vCPUs"
                )

        if not reasons:
            reasons.append(
                "No evaluated configuration satisfied the combination of declared constraints"
            )
            relax.append("Relax the deadline, the budget, or the hardware requirements")

        explanation = (
            "No feasible execution plan satisfies all declared constraints. "
            + "; ".join(reasons)
            + ". Suggested relaxations: "
            + "; ".join(relax)
            + "."
        )

        return {
            "is_feasible": False,
            "plans": [],
            "recommended_plan_id": None,
            "unfeasible_explanation": explanation,
            "blocking_constraints": reasons,
            "suggested_relaxations": relax,
            "incompatible_candidates": incompatible,
            "evaluated_count": len(all_evaluated),
            "cost_basis": cost_basis,
            "cost_basis_detail": cost_basis_detail,
        }

    cost_opt = min(feasible, key=lambda x: (x["est_cost"], x["total_time"]))
    time_fav = min(feasible, key=lambda x: (x["total_time"], x["est_cost"]))

    hedged = [c for c in feasible if "80% Spot" in c["provisioning_model"]]
    mid = [c for c in feasible if c["cpu"] in (8, 16, 32)]
    balanced_pool = hedged or mid or feasible
    balanced = balanced_pool[len(balanced_pool) // 2]

    def _build(
        cand: dict[str, Any],
        plan_type: str,
        title_prefix: str,
        rationale: str,
    ) -> ExecutionPlan:
        """Materialise a plan with a workload-scoped, content-derived id."""
        draft = {
            "cpu": cand["cpu"],
            "gpu": cand["gpu"],
            "machine_type": cand["machine_type"],
            "region": target_region,
            "zone": target_zone,
            "provisioning_model": cand["provisioning_model"],
            "quantity": 1,
            "estimated_cost_eur": cand["est_cost"],
        }
        fingerprint = plan_fingerprint(draft, command=workload.command or workload.script)
        return ExecutionPlan(
            plan_id=make_plan_id(workload.workload_id, plan_type, fingerprint),
            plan_type=plan_type,
            title=f"{title_prefix}: {cand['machine_type']} ({cand['provisioning_model']})",
            machine_type=cand["machine_type"],
            cpu=cand["cpu"],
            gpu=cand["gpu"],
            region=target_region,
            zone=target_zone,
            provisioning_model=cand["provisioning_model"],
            quantity=1,
            satisfied_constraints=cand["satisfied_constraints"],
            unverified_points=cand["unverified_points"],
            estimated_cost_eur=cand["est_cost"],
            cost_basis=cost_basis,
            cost_basis_detail=cost_basis_detail,
            executable_on_runtime=bool(cand.get("executable_on_runtime", True)),
            estimated_wait_minutes=cand["wait_time"],
            estimated_prep_minutes=cand["prep_time"],
            estimated_execution_minutes=round(cand["exec_time"], 2),
            estimated_recovery_minutes=cand["rec_time"],
            total_time_to_result_minutes=cand["total_time"],
            ranking_rationale=rationale,
            uncertainty_factors=cand["uncertainty_factors"],
        )

    plans_list: list[ExecutionPlan] = [
        _build(
            cost_opt,
            "cost_optimized",
            "Cost-Optimized",
            f"Minimizes total estimated spend ({cost_opt['est_cost']:.2f}EUR) while respecting "
            f"the deadline ({cost_opt['total_time']:.1f}m)",
        )
    ]

    if time_fav != cost_opt:
        plans_list.append(
            _build(
                time_fav,
                "deadline_favored",
                "Deadline-Favored",
                f"Minimizes time to result ({time_fav['total_time']:.1f}m) within budget "
                f"({time_fav['est_cost']:.2f}EUR)",
            )
        )

    if balanced != cost_opt and balanced != time_fav:
        plans_list.append(
            _build(
                balanced,
                "balanced_tradeoff",
                "Balanced Trade-off",
                f"Balances speed ({balanced['total_time']:.1f}m) and cost "
                f"({balanced['est_cost']:.2f}EUR) with hedged provisioning",
            )
        )

    # Third comparison dimension: can this capacity actually be obtained?
    capacity_checked = _capacity_check_enabled(check_capacity)
    if capacity_checked:
        annotate_plans_with_capacity(plans_list, demo_mode=demo_mode)

    # Wire the fallback ladder using the real generated ids.
    by_type = {p.plan_type: p for p in plans_list}
    for plan in plans_list:
        chain = [
            other.plan_id
            for other in plans_list
            if other.plan_id != plan.plan_id
        ]
        plan.fallback_chain = chain
        plan.fallback_plan_id = chain[0] if chain else None

    return {
        "is_feasible": True,
        "plans": [p.model_dump() for p in plans_list],
        "recommended_plan_id": plans_list[0].plan_id,
        "unfeasible_explanation": None,
        "feasible_count": len(feasible),
        "evaluated_count": len(all_evaluated),
        "incompatible_candidates": incompatible,
        "cost_basis": cost_basis,
        "cost_basis_detail": cost_basis_detail,
        "capacity_checked": capacity_checked,
        "capacity_note": (
            "Each plan carries a quota verdict read from the Compute Engine API; "
            "QUOTA_UNKNOWN means the source could not be consulted, not that capacity is free."
            if capacity_checked
            else "Quota was not consulted for these plans (capacity check disabled), so every "
            "plan reports NOT_CHECKED rather than an assumed availability."
        ),
    }
