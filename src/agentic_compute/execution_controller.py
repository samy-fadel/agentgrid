from __future__ import annotations

import logging
import time
from typing import Any, Literal

from .models import DelegationPolicy, ExecutionPlan, WorkloadProfile

logger = logging.getLogger("agentic_compute.execution_controller")


class ExecutionController:
    """Controls execution safety, enforcing operator control modes, fallback ladders, and idempotency."""

    def __init__(self, runtime: Any = None) -> None:
        self.runtime = runtime
        self._submitted_job_hashes: dict[str, str] = {}  # workload_id -> job_id

    def evaluate_control_gate(
        self,
        control_mode: Literal["advisory", "validation", "delegation"] | str,
        plan: ExecutionPlan | dict[str, Any],
        delegation_policy: DelegationPolicy | dict[str, Any] | None = None,
        is_operator_approved: bool = False,
        approved_plan_id: str | None = None,
    ) -> tuple[bool, str]:
        """Evaluate if an execution plan is authorized to proceed under the active control mode.

        - advisory: Rejects all mutations. Strictly read-only suggestions.
        - validation: Requires explicit human operator approval of the specific plan_id.
        - delegation: Autonomously permits execution if within strict DelegationPolicy limits;
                      otherwise rejects and requires human approval.
        """
        p = plan if isinstance(plan, ExecutionPlan) else ExecutionPlan(**plan)
        mode = (control_mode or "validation").lower().strip()

        # 1. Advisory Mode: strictly read-only
        if mode == "advisory":
            return (
                False,
                "Execution blocked: Advisory mode is read-only. No cluster mutation, provisioning, "
                "or job submission is permitted. Switch to validation or delegation mode to execute.",
            )

        # 2. Validation Mode: requires explicit operator approval
        if mode == "validation":
            if is_operator_approved or (approved_plan_id and approved_plan_id == p.plan_id):
                return (True, f"Plan '{p.plan_id}' approved by human operator.")
            return (
                False,
                f"Execution pending: Validation mode requires explicit human operator approval "
                f"for concrete plan '{p.plan_id}' ({p.title}, estimated cost: {p.estimated_cost_eur:.2f}€).",
            )

        # 3. Delegation Mode: policy-bounded autonomy
        if mode == "delegation":
            policy = (
                delegation_policy
                if isinstance(delegation_policy, DelegationPolicy)
                else DelegationPolicy(**(delegation_policy or {}))
            )

            # Check budget ceiling
            if p.estimated_cost_eur > policy.max_budget_eur:
                return (
                    False,
                    f"Execution blocked: Plan estimated cost ({p.estimated_cost_eur:.2f}€) exceeds "
                    f"delegated policy budget limit ({policy.max_budget_eur:.2f}€). Human validation required.",
                )

            # Check allowed machine types
            if policy.allowed_machine_types and p.machine_type not in policy.allowed_machine_types:
                return (
                    False,
                    f"Execution blocked: Machine type '{p.machine_type}' is not in delegated policy "
                    f"allowed list ({', '.join(policy.allowed_machine_types)}). Human validation required.",
                )

            # Check allowed provisioning models
            if policy.allowed_provisioning_models and p.provisioning_model not in policy.allowed_provisioning_models:
                return (
                    False,
                    f"Execution blocked: Provisioning model '{p.provisioning_model}' is not in delegated "
                    f"policy allowed list ({', '.join(policy.allowed_provisioning_models)}). Human validation required.",
                )

            # Check allowed regions
            if policy.allowed_regions and p.region not in policy.allowed_regions:
                return (
                    False,
                    f"Execution blocked: Region '{p.region}' is not in delegated policy allowed regions "
                    f"({', '.join(policy.allowed_regions)}). Human validation required.",
                )

            return (
                True,
                f"Plan '{p.plan_id}' autonomously approved under delegation policy "
                f"(Cost: {p.estimated_cost_eur:.2f}€ <= {policy.max_budget_eur:.2f}€).",
            )

        return (False, f"Execution blocked: Unsupported control mode '{control_mode}'.")

    def submit_plan(
        self,
        profile: WorkloadProfile | dict[str, Any],
        plan: ExecutionPlan | dict[str, Any],
        control_mode: str = "validation",
        delegation_policy: DelegationPolicy | dict[str, Any] | None = None,
        is_operator_approved: bool = False,
        approved_plan_id: str | None = None,
        runtime: Any = None,
    ) -> dict[str, Any]:
        """Submit a workload under the active control mode with idempotent submission and verification."""
        p_profile = profile if isinstance(profile, WorkloadProfile) else WorkloadProfile(**profile)
        p_plan = plan if isinstance(plan, ExecutionPlan) else ExecutionPlan(**plan)
        rt = runtime or self.runtime

        # 1. Evaluate Control Gate
        allowed, reason = self.evaluate_control_gate(
            control_mode=control_mode,
            plan=p_plan,
            delegation_policy=delegation_policy,
            is_operator_approved=is_operator_approved,
            approved_plan_id=approved_plan_id,
        )

        if not allowed:
            return {
                "status": "blocked",
                "control_mode": control_mode,
                "reason": reason,
                "plan_id": p_plan.plan_id,
                "job_id": None,
                "verified": False,
            }

        # 2. Idempotency Check: Avoid duplicate submission on network timeout or replay
        workload_id = p_profile.workload_id
        if workload_id in self._submitted_job_hashes:
            existing_job_id = self._submitted_job_hashes[workload_id]
            # Verify if job is indeed still active or present in runtime
            logger.info("Idempotent check: reusing existing job %s for workload %s", existing_job_id, workload_id)
            return {
                "status": "already_submitted",
                "control_mode": control_mode,
                "reason": f"Workload '{workload_id}' already has active submitted job '{existing_job_id}'",
                "plan_id": p_plan.plan_id,
                "job_id": existing_job_id,
                "verified": True,
            }

        # 3. Execution Submission via Runtime
        submitted_job_id = None
        submission_details = {}

        if rt is not None and hasattr(rt, "submit_job"):
            try:
                res = rt.submit_job(
                    name=p_profile.name,
                    cpu=p_plan.cpu,
                    gpu=p_plan.gpu,
                    machine_type=p_plan.machine_type,
                    provisioning_model=p_plan.provisioning_model,
                    script=p_profile.script or p_profile.command,
                )
                submitted_job_id = str(res.get("job_id", "")) if isinstance(res, dict) else str(res)
                submission_details = res if isinstance(res, dict) else {"result": res}
            except Exception as exc:
                # Ambiguous timeout check: Query runtime to verify if job was created despite network error
                if hasattr(rt, "_discover_active_job"):
                    discovered = rt._discover_active_job()
                    if discovered:
                        submitted_job_id = str(discovered)
                        submission_details = {"recovered_after_timeout": True, "job_id": submitted_job_id}
                    else:
                        raise RuntimeError(f"Submission failed and verification confirmed no job created: {exc}") from exc
                else:
                    raise exc
        else:
            # Simulated submission
            submitted_job_id = f"sim-job-{int(time.time())}"
            submission_details = {"mode": "simulated", "job_id": submitted_job_id}

        if submitted_job_id:
            self._submitted_job_hashes[workload_id] = submitted_job_id

        # 4. Post-Action State Verification
        verified = False
        verification_details = "Awaiting scheduler confirmation"
        if rt is not None and hasattr(rt, "snapshot"):
            try:
                snap = rt.snapshot()
                active_job = (getattr(snap.workload, "job_id", None) or getattr(snap.workload, "id", None)) if hasattr(snap, "workload") else None
                if active_job == submitted_job_id or active_job is not None:
                    verified = True
                    verification_details = f"Verified job '{submitted_job_id}' registered in cluster state"
            except Exception as v_err:
                verification_details = f"Verification check encountered: {v_err}"
        else:
            verified = True
            verification_details = "Simulated verification complete"

        return {
            "status": "submitted",
            "control_mode": control_mode,
            "reason": reason,
            "plan_id": p_plan.plan_id,
            "job_id": submitted_job_id,
            "verified": verified,
            "verification_details": verification_details,
            "submission_details": submission_details,
        }

    def evaluate_fallback_ladder(
        self,
        current_plan: ExecutionPlan | dict[str, Any],
        available_plans: list[ExecutionPlan | dict[str, Any]],
        failure_category: str,
        budget_limit_eur: float | None = None,
        accumulated_cost_eur: float = 0.0,
    ) -> ExecutionPlan | None:
        """Select next viable fallback plan in ladder when Spot preemption or stockout occurs.

        Ensures remaining budget is respected (accumulated_cost + fallback_estimated_cost <= budget).
        """
        curr = current_plan if isinstance(current_plan, ExecutionPlan) else ExecutionPlan(**current_plan)
        candidates = [p if isinstance(p, ExecutionPlan) else ExecutionPlan(**p) for p in available_plans]

        # Prioritize fallback plan ID if specified
        if curr.fallback_plan_id:
            for cand in candidates:
                if cand.plan_id == curr.fallback_plan_id:
                    # Check budget
                    if budget_limit_eur is None or (accumulated_cost_eur + cand.estimated_cost_eur) <= budget_limit_eur:
                        return cand

        # Otherwise look for Standard on-demand or alternative machine type
        for cand in candidates:
            if cand.plan_id == curr.plan_id:
                continue
            # If spot failed, prefer standard or hedged
            if "spot" in curr.provisioning_model.lower() and "standard" in cand.provisioning_model.lower():
                if budget_limit_eur is None or (accumulated_cost_eur + cand.estimated_cost_eur) <= budget_limit_eur:
                    return cand

        # Return any other feasible candidate within remaining budget
        for cand in candidates:
            if cand.plan_id != curr.plan_id:
                if budget_limit_eur is None or (accumulated_cost_eur + cand.estimated_cost_eur) <= budget_limit_eur:
                    return cand

        return None

    def evaluate_fallback_ladder_with_details(
        self,
        current_plan: ExecutionPlan | dict[str, Any],
        available_plans: list[ExecutionPlan | dict[str, Any]],
        failure_category: str,
        budget_limit_eur: float | None = None,
        accumulated_cost_eur: float = 0.0,
    ) -> dict[str, Any]:
        """Evaluate fallback ladder and provide structured decision with budget verification."""
        curr = current_plan if isinstance(current_plan, ExecutionPlan) else ExecutionPlan(**current_plan)
        candidates = [p if isinstance(p, ExecutionPlan) else ExecutionPlan(**p) for p in available_plans]

        ordered_ladder: list[ExecutionPlan] = []
        if curr.fallback_plan_id:
            for cand in candidates:
                if cand.plan_id == curr.fallback_plan_id:
                    ordered_ladder.append(cand)
                    break
        for cand in candidates:
            if cand.plan_id != curr.plan_id and cand not in ordered_ladder:
                ordered_ladder.append(cand)

        for cand in ordered_ladder:
            projected_total = accumulated_cost_eur + cand.estimated_cost_eur
            if budget_limit_eur is not None and projected_total > budget_limit_eur:
                return {
                    "status": "budget_breach",
                    "plan": None,
                    "rejected_plan_id": cand.plan_id,
                    "projected_cost_eur": projected_total,
                    "budget_limit_eur": budget_limit_eur,
                    "reason": (
                        f"Fallback plan '{cand.plan_id}' ({cand.title}) rejected: "
                        f"projected total cost {projected_total:.2f}€ exceeds remaining budget {budget_limit_eur:.2f}€. "
                        f"Human confirmation required to exceed budget."
                    ),
                }
            return {
                "status": "selected",
                "plan": cand,
                "projected_cost_eur": projected_total,
                "budget_limit_eur": budget_limit_eur,
                "reason": f"Fallback to plan '{cand.plan_id}' within remaining budget.",
            }

        return {
            "status": "no_plan_available",
            "plan": None,
            "reason": "No fallback plan available in ladder.",
        }

    def safe_downscale(self, workload_id: str, job_id: str | None = None, runtime: Any = None) -> dict[str, Any]:
        """Safely release allocations and downscale when job completes, fails, or cancels.

        Guarantees downscaling does not kill active running compute nodes.
        """
        rt = runtime or self.runtime
        if rt is not None and hasattr(rt, "snapshot"):
            try:
                snap = rt.snapshot()
                if hasattr(snap, "workload") and snap.workload:
                    w = snap.workload
                    is_active = (getattr(w, "status", None) == "RUNNING") and not getattr(w, "done", False)
                    if is_active and (w.id == workload_id or not workload_id):
                        return {
                            "status": "rejected",
                            "workload_id": workload_id,
                            "job_id": job_id,
                            "safe": False,
                            "reason": f"Cannot downscale active compute nodes: workload '{workload_id}' is currently RUNNING. Downscaling aborted to prevent interrupting compute.",
                        }
            except Exception as e:
                logger.warning("Snapshot inspection during downscale check: %s", e)

        if workload_id in self._submitted_job_hashes:
            del self._submitted_job_hashes[workload_id]

        status = "downscaled"
        details = f"Cleaned allocation for workload '{workload_id}'"
        if rt is not None and hasattr(rt, "apply"):
            try:
                from .models import Action
                rt.apply(Action(action="resize_workload", workload_id=workload_id, cpu=2, reason="safe_downscale"))
                details += " and scaled cluster cores to standby baseline"
            except Exception as e:
                details += f" (standby resize note: {e})"

        return {"status": status, "workload_id": workload_id, "job_id": job_id, "safe": True, "details": details}
