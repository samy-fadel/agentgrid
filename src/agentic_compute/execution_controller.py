from __future__ import annotations

import logging
import time
from typing import Any, Literal

from .errors import AmbiguousSubmission, ConfigurationRejected, RuntimeUnavailable
from .governance import (
    GovernanceStore,
    get_governance_store,
    normalize_control_mode,
    plan_fingerprint,
    submission_key,
)
from .models import DelegationPolicy, ExecutionPlan, WorkloadProfile

logger = logging.getLogger("agentic_compute.execution_controller")


class ExecutionController:
    """Enforces operator control modes, fallback ladders and submission identity.

    All authority decisions are read from the persistent
    :class:`~agentic_compute.governance.GovernanceStore` rather than from
    caller-supplied arguments, so neither the agent nor a replayed HTTP request
    can grant itself permission or submit the same workload twice.
    """

    def __init__(self, runtime: Any = None, governance: GovernanceStore | None = None) -> None:
        self.runtime = runtime
        self._governance = governance
        # Retained only as a process-local fast path; the persistent ledger in
        # the governance store is the authority for idempotency.
        self._submitted_job_hashes: dict[str, str] = {}

    @property
    def governance(self) -> GovernanceStore:
        """Resolve the governance store lazily so tests can repoint the DB path."""
        if self._governance is None:
            self._governance = get_governance_store()
        return self._governance

    def resolve_control(
        self,
        workload_id: str,
        requested_mode: str | None = None,
        requested_policy: DelegationPolicy | dict[str, Any] | None = None,
    ) -> tuple[str, DelegationPolicy, str]:
        """Return the (mode, policy, source) that actually governs this workload.

        A mode recorded by the operator always wins. A caller-supplied mode is
        honoured only when the operator has not configured the workload, and
        even then it can never be *less* restrictive than the server default --
        an agent cannot promote itself from validation to delegation.
        """
        stored = self.governance.get_workload_control(workload_id)
        if stored["source"] == "operator_configured":
            return stored["control_mode"], stored["delegation_policy"], "operator_configured"

        if requested_mode is None:
            return stored["control_mode"], stored["delegation_policy"], "server_default"

        requested = normalize_control_mode(requested_mode)
        default_mode = stored["control_mode"]
        rank = {"advisory": 0, "validation": 1, "delegation": 2}
        if rank[requested] > rank[default_mode]:
            # Caller asked for more autonomy than the server grants by default
            # and no operator has authorised it. Clamp to the default.
            return (
                default_mode,
                stored["delegation_policy"],
                "clamped_to_server_default",
            )

        policy = stored["delegation_policy"]
        if requested_policy is not None:
            candidate = (
                requested_policy
                if isinstance(requested_policy, DelegationPolicy)
                else DelegationPolicy(**requested_policy)
            )
            # A caller may tighten the policy but never loosen the budget ceiling.
            policy = DelegationPolicy(
                max_budget_eur=min(candidate.max_budget_eur, policy.max_budget_eur),
                allowed_machine_types=candidate.allowed_machine_types or policy.allowed_machine_types,
                allowed_provisioning_models=(
                    candidate.allowed_provisioning_models or policy.allowed_provisioning_models
                ),
                allowed_regions=candidate.allowed_regions or policy.allowed_regions,
                max_retries=min(candidate.max_retries, policy.max_retries),
                auto_approve_if_within_policy=candidate.auto_approve_if_within_policy,
            )
        return requested, policy, "caller_requested"

    def evaluate_control_gate(
        self,
        control_mode: Literal["advisory", "validation", "delegation"] | str,
        plan: ExecutionPlan | dict[str, Any],
        delegation_policy: DelegationPolicy | dict[str, Any] | None = None,
        is_operator_approved: bool = False,
        approved_plan_id: str | None = None,
        workload_id: str | None = None,
        command: str | None = None,
    ) -> tuple[bool, str]:
        """Decide whether this exact plan may execute under the governing mode.

        - ``advisory``   : never authorises a mutation.
        - ``validation`` : requires an approval **recorded on the server** for
          this plan id, bound to this workload, matching the plan's current
          fingerprint. ``is_operator_approved`` / ``approved_plan_id`` are
          caller-supplied and are therefore *not* accepted as evidence.
        - ``delegation`` : authorises autonomously only inside the policy
          boundaries the operator configured.
        """
        p = plan if isinstance(plan, ExecutionPlan) else ExecutionPlan(**plan)
        wl_id = workload_id

        try:
            mode = normalize_control_mode(control_mode)
        except ValueError as exc:
            return (False, f"Execution blocked: {exc}")

        # 1. Advisory: strictly read-only.
        if mode == "advisory":
            return (
                False,
                "Execution blocked: Advisory mode is read-only. No cluster mutation, provisioning, "
                "or job submission is permitted. Switch to validation or delegation mode to execute.",
            )

        # 2. Validation: only a persisted, fingerprint-matched approval counts.
        if mode == "validation":
            # Caller-supplied approval evidence is never authoritative, so say so
            # on *every* refusal path -- otherwise a caller that passed the flags
            # reads "plan not registered" and assumes a lookup glitch.
            hint = ""
            if is_operator_approved or approved_plan_id:
                hint = (
                    " Note: caller-supplied approval flags are ignored; approval must be "
                    "recorded on the server before execution."
                )

            registered = self.governance.get_registered_plan(p.plan_id)
            if registered is None:
                return (
                    False,
                    f"Execution blocked: plan '{p.plan_id}' is not registered on the server. "
                    f"Generate plans via the comparison endpoint so the exact plan is recorded, "
                    f"then approve it.{hint}",
                )

            effective_workload = wl_id or registered["workload_id"]
            if registered["workload_id"] != effective_workload:
                return (
                    False,
                    f"Execution blocked: plan '{p.plan_id}' belongs to workload "
                    f"'{registered['workload_id']}', not '{effective_workload}'.{hint}",
                )

            fingerprint = plan_fingerprint(p, command=command)
            approved, reason = self.governance.check_approval(
                plan_id=p.plan_id,
                workload_id=effective_workload,
                fingerprint=fingerprint,
            )
            if not approved:
                return (False, f"Execution blocked: {reason}{hint}")
            return (True, f"Plan '{p.plan_id}' has a recorded operator approval.")

        # 3. Delegation: bounded autonomy.
        if mode == "delegation":
            policy = (
                delegation_policy
                if isinstance(delegation_policy, DelegationPolicy)
                else DelegationPolicy(**(delegation_policy or {}))
            )
            ok, reason = self.check_policy_bounds(p, policy)
            if not ok:
                return (False, reason)
            return (
                True,
                f"Plan '{p.plan_id}' autonomously approved under delegation policy "
                f"(Cost: {p.estimated_cost_eur:.2f}EUR <= {policy.max_budget_eur:.2f}EUR).",
            )

        return (False, f"Execution blocked: Unsupported control mode '{control_mode}'.")

    def check_policy_bounds(
        self,
        plan: ExecutionPlan | dict[str, Any],
        policy: DelegationPolicy | dict[str, Any],
        accumulated_cost_eur: float = 0.0,
        attempts_used: int = 0,
    ) -> tuple[bool, str]:
        """Check a plan against delegation boundaries.

        Applied identically to a first submission and to every fallback step, so
        a fallback cannot quietly exceed limits the initial plan respected.
        ``accumulated_cost_eur`` makes the budget a *cumulative* ceiling rather
        than a per-action one.
        """
        p = plan if isinstance(plan, ExecutionPlan) else ExecutionPlan(**plan)
        pol = policy if isinstance(policy, DelegationPolicy) else DelegationPolicy(**policy)

        projected = accumulated_cost_eur + p.estimated_cost_eur
        if projected > pol.max_budget_eur:
            detail = (
                f"cumulative {projected:.2f}EUR (already spent {accumulated_cost_eur:.2f}EUR "
                f"+ plan {p.estimated_cost_eur:.2f}EUR)"
                if accumulated_cost_eur
                else f"{p.estimated_cost_eur:.2f}EUR"
            )
            return (
                False,
                f"Execution blocked: Plan estimated cost {detail} exceeds delegated policy "
                f"budget limit ({pol.max_budget_eur:.2f}EUR). Human validation required.",
            )

        if pol.allowed_machine_types and p.machine_type not in pol.allowed_machine_types:
            return (
                False,
                f"Execution blocked: Machine type '{p.machine_type}' is not in delegated policy "
                f"allowed list ({', '.join(pol.allowed_machine_types)}). Human validation required.",
            )

        if pol.allowed_provisioning_models and p.provisioning_model not in pol.allowed_provisioning_models:
            return (
                False,
                f"Execution blocked: Provisioning model '{p.provisioning_model}' is not in delegated "
                f"policy allowed list ({', '.join(pol.allowed_provisioning_models)}). "
                f"Human validation required.",
            )

        if pol.allowed_regions and p.region not in pol.allowed_regions:
            return (
                False,
                f"Execution blocked: Region '{p.region}' is not in delegated policy allowed regions "
                f"({', '.join(pol.allowed_regions)}). Human validation required.",
            )

        if attempts_used >= pol.max_retries:
            return (
                False,
                f"Execution blocked: delegated retry budget exhausted "
                f"({attempts_used}/{pol.max_retries} attempts used). Human validation required.",
            )

        return (True, "Within delegated policy boundaries.")

    def submit_plan(
        self,
        profile: WorkloadProfile | dict[str, Any],
        plan: ExecutionPlan | dict[str, Any],
        control_mode: str | None = None,
        delegation_policy: DelegationPolicy | dict[str, Any] | None = None,
        is_operator_approved: bool = False,
        approved_plan_id: str | None = None,
        runtime: Any = None,
        allow_resubmit: bool = False,
        accumulated_cost_eur: float = 0.0,
    ) -> dict[str, Any]:
        """Submit a workload once, under the governing control mode.

        Guarantees:

        * The control mode comes from the server, not from the caller.
        * A submission is claimed atomically in the persistent ledger before the
          runtime is touched, so a double click or a replayed request cannot
          create two jobs -- across processes and across restarts.
        * A configuration error is reported as a failure, never disguised as an
          ambiguous timeout.
        * ``verified`` is true only when the runtime reports *this* job id.
        """
        p_profile = profile if isinstance(profile, WorkloadProfile) else WorkloadProfile(**profile)
        p_plan = plan if isinstance(plan, ExecutionPlan) else ExecutionPlan(**plan)
        rt = runtime or self.runtime
        workload_id = p_profile.workload_id
        command = p_profile.command or p_profile.script

        # 1. Resolve the governing mode/policy from the server.
        mode, policy, mode_source = self.resolve_control(
            workload_id, requested_mode=control_mode, requested_policy=delegation_policy
        )

        # 2. Authorise this exact plan.
        allowed, reason = self.evaluate_control_gate(
            control_mode=mode,
            plan=p_plan,
            delegation_policy=policy,
            is_operator_approved=is_operator_approved,
            approved_plan_id=approved_plan_id,
            workload_id=workload_id,
            command=command,
        )
        if not allowed:
            return {
                "status": "blocked",
                "control_mode": mode,
                "control_mode_source": mode_source,
                "reason": reason,
                "plan_id": p_plan.plan_id,
                "workload_id": workload_id,
                "job_id": None,
                "verified": False,
            }

        if mode == "delegation":
            within, policy_reason = self.check_policy_bounds(
                p_plan, policy, accumulated_cost_eur=accumulated_cost_eur
            )
            if not within:
                return {
                    "status": "blocked",
                    "control_mode": mode,
                    "control_mode_source": mode_source,
                    "reason": policy_reason,
                    "plan_id": p_plan.plan_id,
                    "workload_id": workload_id,
                    "job_id": None,
                    "verified": False,
                }

        # 3. Claim the submission slot. The ledger, not a local dict, is the
        #    authority, so idempotency survives request boundaries and restarts.
        fingerprint = plan_fingerprint(p_plan, command=command)
        key = submission_key(workload_id, fingerprint)

        if allow_resubmit:
            # An explicitly authorised retry after a terminal failure. A replay
            # never reaches this branch, which is what keeps the two distinct.
            existing = self.governance.get_submission(key)
            if existing and existing["state"] in ("failed", "uncertain"):
                self.governance.release_submission(key)

        won, existing = self.governance.claim_submission(
            key=key, workload_id=workload_id, plan_id=p_plan.plan_id, fingerprint=fingerprint
        )

        if not won:
            return self._describe_existing_submission(existing, mode, mode_source, rt)

        # 4. Perform the submission.
        try:
            submitted_job_id, submission_details = self._invoke_submit(
                rt=rt, profile=p_profile, plan=p_plan, key=key
            )
        except ConfigurationRejected as exc:
            # Deterministic rejection: nothing was created. Record it as failed
            # so a later observation can never promote it to "applied".
            self.governance.record_submission(key, state="failed", detail=str(exc))
            return {
                "status": "failed",
                "control_mode": mode,
                "control_mode_source": mode_source,
                "reason": f"Runtime rejected the configuration: {exc}",
                "rejected_parameter": getattr(exc, "parameter", None),
                "plan_id": p_plan.plan_id,
                "workload_id": workload_id,
                "job_id": None,
                "verified": False,
                "ambiguous": False,
            }
        except AmbiguousSubmission as exc:
            # Genuine transport ambiguity: look for *this* submission identity.
            recovered = self._recover_submission(rt, key)
            if recovered:
                self.governance.record_submission(
                    key, state="submitted", job_id=recovered,
                    detail="recovered after ambiguous timeout",
                )
                verified, detail = self._verify_job(rt, recovered)
                return {
                    "status": "submitted",
                    "control_mode": mode,
                    "control_mode_source": mode_source,
                    "reason": "Submission recovered after an ambiguous timeout.",
                    "plan_id": p_plan.plan_id,
                    "workload_id": workload_id,
                    "job_id": recovered,
                    "verified": verified,
                    "verification_details": detail,
                    "recovered_after_timeout": True,
                }
            self.governance.record_submission(key, state="uncertain", detail=str(exc))
            return {
                "status": "uncertain",
                "control_mode": mode,
                "control_mode_source": mode_source,
                "reason": (
                    f"Submission outcome is unknown: {exc}. No job matching submission "
                    f"identity '{key}' could be found. The cluster may or may not have "
                    f"accepted it; resolve manually before retrying."
                ),
                "plan_id": p_plan.plan_id,
                "workload_id": workload_id,
                "job_id": None,
                "verified": False,
                "ambiguous": True,
                "submission_key": key,
            }
        except RuntimeUnavailable as exc:
            self.governance.record_submission(key, state="failed", detail=str(exc))
            return {
                "status": "failed",
                "control_mode": mode,
                "control_mode_source": mode_source,
                "reason": f"Runtime unavailable: {exc}",
                "plan_id": p_plan.plan_id,
                "workload_id": workload_id,
                "job_id": None,
                "verified": False,
                "ambiguous": False,
            }
        except Exception as exc:
            # Fail closed. An unclassified error is treated as a definite
            # failure, never as a maybe-it-worked timeout.
            self.governance.record_submission(
                key, state="failed", detail=f"{type(exc).__name__}: {exc}"
            )
            return {
                "status": "failed",
                "control_mode": mode,
                "control_mode_source": mode_source,
                "reason": (
                    f"Submission failed with an unclassified error "
                    f"({type(exc).__name__}: {exc}). Treated as a definite failure."
                ),
                "plan_id": p_plan.plan_id,
                "workload_id": workload_id,
                "job_id": None,
                "verified": False,
                "ambiguous": False,
            }

        self.governance.record_submission(key, state="submitted", job_id=submitted_job_id)
        self._submitted_job_hashes[workload_id] = submitted_job_id

        # 5. Verify strictly against the job we actually submitted.
        verified, verification_details = self._verify_job(rt, submitted_job_id)

        return {
            "status": "submitted",
            "control_mode": mode,
            "control_mode_source": mode_source,
            "reason": reason,
            "plan_id": p_plan.plan_id,
            "workload_id": workload_id,
            "job_id": submitted_job_id,
            "submission_key": key,
            "verified": verified,
            "verification_details": verification_details,
            "submission_details": submission_details,
        }

    def _invoke_submit(
        self,
        rt: Any,
        profile: WorkloadProfile,
        plan: ExecutionPlan,
        key: str,
    ) -> tuple[str, dict[str, Any]]:
        """Call the runtime's submit_job under the canonical contract.

        The submission identity ``key`` is used as the job name so that an
        ambiguous timeout can be resolved by looking for that exact job.
        """
        if rt is None or not hasattr(rt, "submit_job"):
            raise RuntimeUnavailable(
                "No runtime adapter is configured; refusing to fabricate a submission."
            )

        res = rt.submit_job(
            name=key,
            cpu=plan.cpu,
            gpu=plan.gpu,
            machine_type=plan.machine_type,
            provisioning_model=plan.provisioning_model,
            script=profile.script or profile.command,
        )

        if isinstance(res, dict):
            job_id = res.get("job_id") or res.get("id")
            if not job_id:
                raise ConfigurationRejected(
                    f"Runtime returned no job identifier for submission '{key}': {res}"
                )
            return str(job_id), res
        if not res:
            raise ConfigurationRejected(f"Runtime returned an empty result for submission '{key}'.")
        return str(res), {"result": res}

    def _recover_submission(self, rt: Any, key: str) -> str | None:
        """Look for a job created under a specific submission identity."""
        if rt is None or not hasattr(rt, "_discover_active_job"):
            return None
        try:
            found = rt._discover_active_job(name=key)
        except TypeError:
            # Adapter predates the named lookup: a generic search is not
            # acceptable evidence, so treat it as "not found".
            return None
        except Exception as exc:
            logger.warning("Submission recovery lookup failed for %s: %s", key, exc)
            return None
        return str(found) if found else None

    def _verify_job(self, rt: Any, submitted_job_id: str | None) -> tuple[bool, str]:
        """Confirm the runtime reports *this* job.

        The previous implementation accepted ``active_job is not None``, so any
        pre-existing job verified any submission. Verification now requires an
        identity match.
        """
        if not submitted_job_id:
            return False, "No job id to verify."
        if rt is None or not hasattr(rt, "snapshot"):
            return False, "Runtime exposes no snapshot; submission remains unverified."

        try:
            snap = rt.snapshot()
        except Exception as exc:
            return False, f"Verification could not read cluster state: {exc}"

        workload = getattr(snap, "workload", None)
        if workload is None:
            return False, "Cluster snapshot exposes no workload; submission remains unverified."

        observed = getattr(workload, "job_id", None) or getattr(workload, "id", None)
        if observed is None:
            return False, "Cluster snapshot reports no job identity; submission remains unverified."

        if str(observed) != str(submitted_job_id):
            return (
                False,
                f"Cluster reports job '{observed}', which is not the submitted job "
                f"'{submitted_job_id}'. Submission remains unverified.",
            )

        return True, f"Verified job '{submitted_job_id}' is the job registered in cluster state."

    def _describe_existing_submission(
        self,
        existing: dict[str, Any],
        mode: str,
        mode_source: str,
        rt: Any,
    ) -> dict[str, Any]:
        """Describe a submission that a previous request already claimed."""
        state = existing.get("state")
        job_id = existing.get("job_id")

        if state == "submitted" and job_id:
            verified, detail = self._verify_job(rt, job_id)
            return {
                "status": "already_submitted",
                "control_mode": mode,
                "control_mode_source": mode_source,
                "reason": (
                    f"Workload '{existing.get('workload_id')}' was already submitted as job "
                    f"'{job_id}'. This request was treated as a replay and did not submit again."
                ),
                "plan_id": existing.get("plan_id"),
                "workload_id": existing.get("workload_id"),
                "job_id": job_id,
                "verified": verified,
                "verification_details": detail,
                "submission_key": existing.get("submission_key"),
            }

        if state == "uncertain":
            return {
                "status": "uncertain",
                "control_mode": mode,
                "control_mode_source": mode_source,
                "reason": (
                    "A previous submission for this workload and plan ended in an unknown "
                    "state. Resolve it before submitting again; retrying blindly risks a "
                    "duplicate job."
                ),
                "plan_id": existing.get("plan_id"),
                "workload_id": existing.get("workload_id"),
                "job_id": None,
                "verified": False,
                "ambiguous": True,
                "submission_key": existing.get("submission_key"),
            }

        if state == "failed":
            return {
                "status": "failed",
                "control_mode": mode,
                "control_mode_source": mode_source,
                "reason": (
                    f"A previous submission for this workload and plan failed: "
                    f"{existing.get('detail')}. Pass allow_resubmit=True to authorise a new attempt."
                ),
                "plan_id": existing.get("plan_id"),
                "workload_id": existing.get("workload_id"),
                "job_id": None,
                "verified": False,
                "ambiguous": False,
                "submission_key": existing.get("submission_key"),
            }

        # state == "claimed": another request is mid-flight.
        return {
            "status": "in_flight",
            "control_mode": mode,
            "control_mode_source": mode_source,
            "reason": (
                "Another request is currently submitting this workload and plan. "
                "This request did not submit again."
            ),
            "plan_id": existing.get("plan_id"),
            "workload_id": existing.get("workload_id"),
            "job_id": None,
            "verified": False,
            "submission_key": existing.get("submission_key"),
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
        control_mode: str | None = None,
        delegation_policy: DelegationPolicy | dict[str, Any] | None = None,
        workload_id: str | None = None,
    ) -> dict[str, Any]:
        """Choose the next rung of the fallback ladder, subject to the same rules.

        A fallback is a fresh mutation, so it is bound by the same authority as
        the original submission: the cumulative budget, the delegation policy,
        and the control mode. An unauthorised fallback stops for validation
        instead of proceeding.
        """
        curr = current_plan if isinstance(current_plan, ExecutionPlan) else ExecutionPlan(**current_plan)
        candidates = [p if isinstance(p, ExecutionPlan) else ExecutionPlan(**p) for p in available_plans]

        # Resolve governing mode when a workload is named, so that a fallback
        # cannot execute in a mode the operator did not grant.
        effective_mode = control_mode
        effective_policy = delegation_policy
        if workload_id:
            effective_mode, resolved_policy, _ = self.resolve_control(
                workload_id, requested_mode=control_mode, requested_policy=delegation_policy
            )
            effective_policy = resolved_policy

        if effective_mode is not None and normalize_control_mode(effective_mode) == "advisory":
            return {
                "status": "blocked",
                "plan": None,
                "reason": (
                    "Fallback blocked: advisory mode is read-only, so no alternative plan "
                    "may be launched automatically."
                ),
            }

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
                        f"projected total cost {projected_total:.2f}EUR exceeds remaining budget "
                        f"{budget_limit_eur:.2f}EUR. Human confirmation required to exceed budget."
                    ),
                }

            # The delegated policy constrains fallbacks exactly as it constrains
            # the first submission, using the cumulative spend.
            if effective_policy is not None:
                within, policy_reason = self.check_policy_bounds(
                    cand, effective_policy, accumulated_cost_eur=accumulated_cost_eur
                )
                if not within:
                    return {
                        "status": "policy_breach",
                        "plan": None,
                        "rejected_plan_id": cand.plan_id,
                        "projected_cost_eur": projected_total,
                        "budget_limit_eur": budget_limit_eur,
                        "reason": (
                            f"Fallback plan '{cand.plan_id}' is not authorised by the delegated "
                            f"policy: {policy_reason} Stopping for human validation."
                        ),
                    }

            return {
                "status": "selected",
                "plan": cand,
                "projected_cost_eur": projected_total,
                "budget_limit_eur": budget_limit_eur,
                "reason": f"Fallback to plan '{cand.plan_id}' within remaining budget and policy.",
            }

        return {
            "status": "no_plan_available",
            "plan": None,
            "reason": "No fallback plan available in ladder.",
        }

    def safe_downscale(
        self,
        workload_id: str,
        job_id: str | None = None,
        runtime: Any = None,
    ) -> dict[str, Any]:
        """Release an allocation only when it is genuinely safe to do so.

        Returns honestly: a refusal is reported as ``rejected`` and a failed
        resize as ``failed``. The previous implementation returned
        ``downscaled`` with ``safe=True`` even when the resize call raised,
        merely appending the error to a details string.
        """
        rt = runtime or self.runtime

        if not workload_id:
            return {
                "status": "rejected",
                "workload_id": workload_id,
                "job_id": job_id,
                "safe": False,
                "reason": "Refusing to downscale without an explicit workload identity.",
            }

        if rt is None:
            return {
                "status": "failed",
                "workload_id": workload_id,
                "job_id": job_id,
                "safe": False,
                "reason": "No runtime adapter configured; cannot verify workload state before downscaling.",
            }

        # Verify identity and state before touching anything.
        if hasattr(rt, "snapshot"):
            try:
                snap = rt.snapshot()
            except Exception as exc:
                return {
                    "status": "failed",
                    "workload_id": workload_id,
                    "job_id": job_id,
                    "safe": False,
                    "reason": f"Could not read cluster state before downscaling: {exc}",
                }

            workload = getattr(snap, "workload", None)
            if workload is None:
                return {
                    "status": "failed",
                    "workload_id": workload_id,
                    "job_id": job_id,
                    "safe": False,
                    "reason": "Cluster snapshot exposes no workload; refusing to downscale blindly.",
                }

            observed_id = getattr(workload, "job_id", None) or getattr(workload, "id", None)
            if str(observed_id) != str(workload_id):
                return {
                    "status": "rejected",
                    "workload_id": workload_id,
                    "job_id": job_id,
                    "safe": False,
                    "reason": (
                        f"Refusing to downscale: cluster is running workload '{observed_id}', "
                        f"not '{workload_id}'."
                    ),
                }

            is_running = getattr(workload, "status", None) == "RUNNING"
            is_done = getattr(workload, "done", False)
            if hasattr(rt, "is_done"):
                try:
                    is_done = bool(rt.is_done())
                except Exception:
                    pass

            if is_running and not is_done:
                return {
                    "status": "rejected",
                    "workload_id": workload_id,
                    "job_id": job_id,
                    "safe": False,
                    "reason": (
                        f"Cannot downscale active compute nodes: workload '{workload_id}' is "
                        f"currently RUNNING. Downscaling aborted to prevent interrupting compute."
                    ),
                }

        if not hasattr(rt, "apply"):
            return {
                "status": "failed",
                "workload_id": workload_id,
                "job_id": job_id,
                "safe": False,
                "reason": "Runtime exposes no apply(); cannot downscale.",
            }

        from .models import Action

        try:
            rt.apply(
                Action(
                    action="resize_workload",
                    workload_id=workload_id,
                    cpu=2,
                    reason="safe_downscale",
                )
            )
        except Exception as exc:
            return {
                "status": "failed",
                "workload_id": workload_id,
                "job_id": job_id,
                "safe": False,
                "reason": f"Downscale resize was rejected by the runtime: {exc}",
            }

        self._submitted_job_hashes.pop(workload_id, None)

        return {
            "status": "downscaled",
            "workload_id": workload_id,
            "job_id": job_id,
            "safe": True,
            "details": f"Released allocation for workload '{workload_id}' to standby baseline.",
        }
