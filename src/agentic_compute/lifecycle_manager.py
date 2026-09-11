from __future__ import annotations

import logging
import time
from typing import Any, Literal

from .history import HistoryStore, get_history_store
from .models import (
    ExecutionAttempt,
    ExecutionHistoryRecord,
    ExecutionPlan,
    WorkloadProfile,
)

logger = logging.getLogger("agentic_compute.lifecycle_manager")

LifecycleState = Literal[
    "DEFINED",
    "PLANNING",
    "READY_FOR_APPROVAL",
    "SUBMITTING",
    "QUEUED",
    "RUNNING",
    "COMPLETED",
    "PREEMPTED",
    "FAILED",
    "CANCELLED",
]


class LifecycleManager:
    """Manages the lifecycle of a compute workload across attempts, checkpoint restarts, and bounded retries."""

    def __init__(self, history_store: HistoryStore | None = None) -> None:
        self.history_store = history_store or get_history_store()
        # In-memory tracking cache: workload_id -> state dict
        self._workloads: dict[str, dict[str, Any]] = {}

    def register_workload(self, profile: WorkloadProfile | dict[str, Any]) -> dict[str, Any]:
        """Register a new workload in DEFINED state."""
        prof = profile if isinstance(profile, WorkloadProfile) else WorkloadProfile(**profile)
        self.history_store.save_workload_profile(prof)

        record = {
            "workload_id": prof.workload_id,
            "profile": prof,
            "state": "DEFINED",
            "progress_percent": None,
            "progress_status": "unavailable",  # "measured" or "unavailable"
            "attempts": [],
            "current_attempt": None,
            "approved_plan": None,
            "total_calculated_cost_eur": 0.0,
            "total_elapsed_minutes": 0.0,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._workloads[prof.workload_id] = record
        return record

    def transition_state(self, workload_id: str, new_state: LifecycleState, reason: str = "") -> dict[str, Any]:
        """Safely transition workload state."""
        rec = self._workloads.get(workload_id)
        if not rec:
            raise KeyError(f"Workload '{workload_id}' is not registered.")

        old_state = rec["state"]
        rec["state"] = new_state
        rec["updated_at"] = time.time()
        logger.info("Workload %s transitioned: %s -> %s (reason: %s)", workload_id, old_state, new_state, reason)

        # Sync to history record
        self._sync_to_history(workload_id)
        return rec

    def start_attempt(
        self,
        workload_id: str,
        plan: ExecutionPlan | dict[str, Any],
        job_id: str | None = None,
        checkpoint_recovered_from: str | None = None,
    ) -> ExecutionAttempt:
        """Start a new physical execution attempt, verifying retry bounds and checkpointing."""
        rec = self._workloads.get(workload_id)
        if not rec:
            raise KeyError(f"Workload '{workload_id}' is not registered.")

        prof: WorkloadProfile = rec["profile"]
        p = plan if isinstance(plan, ExecutionPlan) else ExecutionPlan(**plan)
        attempts: list[ExecutionAttempt] = rec["attempts"]

        attempt_num = len(attempts) + 1
        if attempt_num > prof.max_retries:
            self.transition_state(
                workload_id,
                "FAILED",
                f"Exceeded maximum allowed retry limit ({prof.max_retries} attempts). Execution aborted to protect budget.",
            )
            raise RuntimeError(
                f"Workload '{workload_id}' exceeded max retries limit ({prof.max_retries}). Aborting further attempts."
            )

        # Validate checkpoint resumption constraints
        if checkpoint_recovered_from is not None:
            if not prof.is_interruptible:
                raise ValueError(
                    f"Cannot resume workload '{workload_id}': non-interruptible workloads do not support checkpoint recovery."
                )
            if not prof.supports_checkpointing:
                raise ValueError(
                    f"Cannot resume workload '{workload_id}': checkpointing is not supported by this workload profile."
                )

        # Determine checkpoint recovery
        recovered_ckpt = checkpoint_recovered_from
        if not recovered_ckpt and prof.is_interruptible and prof.supports_checkpointing and prof.checkpoint_location:
            # If previous attempt ran, recover from existing checkpoint
            if len(attempts) > 0:
                recovered_ckpt = f"{prof.checkpoint_location}/step_latest"

        attempt = ExecutionAttempt(
            attempt_id=f"{workload_id}-att-{attempt_num}",
            workload_id=workload_id,
            attempt_number=attempt_num,
            job_id=job_id,
            status="PENDING",
            started_at=time.time(),
            allocated_machine_type=p.machine_type,
            allocated_cpu=p.cpu,
            allocated_gpu=p.gpu,
            provisioning_mode=p.provisioning_model,
            region=p.region,
            zone=p.zone,
            checkpoint_recovered_from=recovered_ckpt,
            events=[{"time": time.time(), "event": "attempt_started", "attempt_number": attempt_num}],
        )

        attempts.append(attempt)
        rec["current_attempt"] = attempt
        rec["approved_plan"] = p
        self.transition_state(workload_id, "QUEUED", f"Attempt #{attempt_num} scheduled (job_id: {job_id})")

        self.history_store.record_attempt(attempt)
        return attempt

    def update_progress(
        self,
        workload_id: str,
        progress_percent: float | None = None,
        job_state: str | None = None,
        elapsed_minutes: float = 0.0,
        cost_incurred_eur: float = 0.0,
    ) -> dict[str, Any]:
        """Update workload execution metrics with explicit observable vs unavailable progress."""
        rec = self._workloads.get(workload_id)
        if not rec:
            raise KeyError(f"Workload '{workload_id}' is not registered.")

        curr_att: ExecutionAttempt | None = rec.get("current_attempt")
        if curr_att:
            curr_att.elapsed_minutes = elapsed_minutes
            curr_att.cost_calculated_eur = cost_incurred_eur
            if job_state:
                curr_att.status = job_state

        rec["total_calculated_cost_eur"] = sum(a.cost_calculated_eur for a in rec["attempts"])
        rec["total_elapsed_minutes"] = sum(a.elapsed_minutes for a in rec["attempts"])

        if progress_percent is not None:
            rec["progress_percent"] = max(0.0, min(100.0, progress_percent))
            rec["progress_status"] = "measured"
        else:
            rec["progress_percent"] = None
            rec["progress_status"] = "unavailable"

        if job_state == "RUNNING" and rec["state"] != "RUNNING":
            self.transition_state(workload_id, "RUNNING", "Job entered execution state")

        self._sync_to_history(workload_id)
        return rec

    def finish_attempt(
        self,
        workload_id: str,
        final_status: Literal["COMPLETED", "FAILED", "PREEMPTED", "CANCELLED"],
        failure_reason: str | None = None,
        total_cost_eur: float | None = None,
        actual_duration_minutes: float | None = None,
    ) -> dict[str, Any]:
        """Record the conclusion of the current attempt and determine if resumption or retry is valid."""
        rec = self._workloads.get(workload_id)
        if not rec:
            raise KeyError(f"Workload '{workload_id}' is not registered.")

        prof: WorkloadProfile = rec["profile"]
        curr_att: ExecutionAttempt | None = rec.get("current_attempt")
        now = time.time()

        if curr_att:
            curr_att.ended_at = now
            curr_att.status = final_status
            curr_att.failure_reason = failure_reason
            if total_cost_eur is not None:
                curr_att.cost_calculated_eur = total_cost_eur
            if actual_duration_minutes is not None:
                curr_att.elapsed_minutes = actual_duration_minutes
            curr_att.events.append({"time": now, "event": "attempt_finished", "status": final_status, "reason": failure_reason})
            self.history_store.record_attempt(curr_att)

        if final_status == "COMPLETED":
            rec["progress_percent"] = 100.0
            rec["progress_status"] = "measured"
            self.transition_state(workload_id, "COMPLETED", "Workload finished successfully.")
        elif final_status == "PREEMPTED":
            self.transition_state(workload_id, "PREEMPTED", failure_reason or "Spot instance preemption occurred.")
        elif final_status == "CANCELLED":
            self.transition_state(workload_id, "CANCELLED", failure_reason or "Job was cancelled by operator.")
        else:
            self.transition_state(workload_id, "FAILED", failure_reason or "Job failed.")

        self._sync_to_history(workload_id)
        return rec

    def can_resume_from_checkpoint(self, workload_id: str) -> tuple[bool, str]:
        """Determine if workload can resume from saved checkpoint or must restart from scratch."""
        rec = self._workloads.get(workload_id)
        if not rec:
            return (False, "Workload not found.")

        prof: WorkloadProfile = rec["profile"]
        if not prof.is_interruptible:
            return (
                False,
                "Workload is non-interruptible (is_interruptible=False). "
                "Partial resume or recovery is rejected; non-interruptible workloads require continuous execution.",
            )

        if not prof.supports_checkpointing:
            return (
                False,
                "Workload does not support checkpointing (supports_checkpointing=False). "
                "Any interrupted progress is lost; next execution must restart from scratch.",
            )

        if not prof.checkpoint_location:
            return (
                False,
                "Checkpointing is enabled but no checkpoint_location URI was provided.",
            )

        # If previous attempts exist
        if rec["attempts"]:
            last_att = rec["attempts"][-1]
            return (
                True,
                f"Checkpoint resumption available at '{prof.checkpoint_location}'. "
                f"Previous attempt #{last_att.attempt_number} can be recovered without restarting from 0%.",
            )

        return (True, f"Checkpointing enabled; target directory '{prof.checkpoint_location}'.")

    def _sync_to_history(self, workload_id: str) -> None:
        rec = self._workloads.get(workload_id)
        if not rec:
            return

        prof: WorkloadProfile = rec["profile"]
        plan: ExecutionPlan | None = rec.get("approved_plan")
        attempts: list[ExecutionAttempt] = rec.get("attempts", [])

        total_cost = sum(a.cost_calculated_eur for a in attempts)
        total_dur = sum(a.elapsed_minutes for a in attempts)
        est_cost = plan.estimated_cost_eur if plan else (prof.budget_amount or 0.0)
        est_dur = plan.total_time_to_result_minutes if plan else (prof.estimated_duration_minutes or 0.0)

        hist_record = ExecutionHistoryRecord(
            workload_id=workload_id,
            workload_name=prof.name,
            profile=prof,
            plans_evaluated=[plan] if plan else [],
            approved_plan=plan,
            control_mode="validation",
            attempts=attempts,
            final_status=rec["state"],
            initial_estimated_cost_eur=est_cost,
            final_calculated_cost_eur=total_cost,
            cost_comparison_delta_eur=round(total_cost - est_cost, 3),
            initial_estimated_duration_minutes=est_dur,
            final_actual_duration_minutes=total_dur,
            duration_comparison_delta_minutes=round(total_dur - est_dur, 2),
            reconciliation_status="calculated_from_usage",
        )
        self.history_store.save_history_record(hist_record)


default_lifecycle_manager = LifecycleManager()
