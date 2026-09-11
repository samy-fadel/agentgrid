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

    def _rehydrate(self, workload_id: str) -> dict[str, Any] | None:
        """Rebuild the in-memory record for a workload from persisted state.

        The manager used to be purely in-memory, so a workload submitted by one
        request was unknown to the next one and to any restarted process: the
        journey submit -> track -> resume broke with a ``KeyError``. Persisted
        rows are the source of truth, so they are replayed here.

        Returns ``None`` when nothing was ever persisted for this workload; that
        is a genuine "unknown workload", not a lost one.
        """
        prof = self.history_store.get_workload_profile(workload_id)
        if prof is None:
            return None

        attempts = self.history_store.get_attempts(workload_id)
        hist = self.history_store.get_history_record(workload_id)

        last = attempts[-1] if attempts else None
        record: dict[str, Any] = {
            "workload_id": workload_id,
            "profile": prof,
            "state": (hist.final_status if hist is not None else "DEFINED"),
            # ExecutionAttempt carries no progress figure, so progress after a
            # restart is genuinely unknown. Saying "0%" would be an invention.
            "progress_percent": None,
            "progress_status": "unavailable",
            "attempts": attempts,
            "current_attempt": last,
            "approved_plan": (
                hist.approved_plan
                if hist is not None and hist.approved_plan is not None
                else self.history_store.get_approved_plan(workload_id)
            ),
            "total_calculated_cost_eur": (
                hist.final_calculated_cost_eur if hist is not None else 0.0
            ),
            "total_elapsed_minutes": (
                hist.final_actual_duration_minutes if hist is not None else 0.0
            ),
            "created_at": (hist.created_at if hist is not None else time.time()),
            "updated_at": time.time(),
            "rehydrated": True,
        }
        self._workloads[workload_id] = record
        return record

    def _require(self, workload_id: str) -> dict[str, Any]:
        """Return the record for a workload, rehydrating from storage if needed."""
        rec = self._workloads.get(workload_id)
        if rec is None:
            rec = self._rehydrate(workload_id)
        if rec is None:
            raise KeyError(f"Workload '{workload_id}' is not registered.")
        return rec

    def transition_state(self, workload_id: str, new_state: LifecycleState, reason: str = "") -> dict[str, Any]:
        """Safely transition workload state."""
        rec = self._require(workload_id)

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
        rec = self._require(workload_id)

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
        rec = self._require(workload_id)

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
        rec = self._require(workload_id)

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

    def verify_checkpoint(self, workload_id: str) -> dict[str, Any]:
        """Look at the declared checkpoint location and report what is really there.

        Returns ``verification`` as one of:

        * ``verified_present`` -- at least one object/file exists at the location.
        * ``verified_absent``  -- the location was reachable and is empty or missing.
        * ``unverified``       -- the location could not be inspected here; the
          reason says why. This is deliberately *not* reported as present:
          claiming a checkpoint exists because a boolean said so is how a
          "resume" promise turns into a silent restart from zero.
        """
        import os
        from urllib.parse import urlparse

        try:
            rec = self._require(workload_id)
        except KeyError:
            return {
                "location": None,
                "verification": "unverified",
                "exists": None,
                "reason": "Workload not found.",
                "checked_by": None,
            }

        prof: WorkloadProfile = rec["profile"]
        location = prof.checkpoint_location
        result: dict[str, Any] = {
            "location": location,
            "verification": "unverified",
            "exists": None,
            "reason": "",
            "checked_by": None,
        }

        if not location:
            result["reason"] = "No checkpoint_location was declared."
            return result

        parsed = urlparse(location)
        scheme = parsed.scheme

        if scheme in ("", "file"):
            path = parsed.path if scheme == "file" else location
            result["checked_by"] = "local_filesystem"
            try:
                if not os.path.exists(path):
                    result["verification"] = "verified_absent"
                    result["exists"] = False
                    result["reason"] = f"'{path}' does not exist on this filesystem."
                elif os.path.isdir(path):
                    entries = os.listdir(path)
                    result["exists"] = bool(entries)
                    result["verification"] = (
                        "verified_present" if entries else "verified_absent"
                    )
                    result["reason"] = (
                        f"'{path}' contains {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}."
                    )
                else:
                    size = os.path.getsize(path)
                    result["exists"] = size > 0
                    result["verification"] = (
                        "verified_present" if size > 0 else "verified_absent"
                    )
                    result["reason"] = f"'{path}' is a {size}-byte file."
            except OSError as exc:
                result["reason"] = f"'{path}' could not be inspected: {exc}"
            return result

        if scheme == "gs":
            result["checked_by"] = "google_cloud_storage"
            try:
                from google.cloud import storage  # type: ignore
            except Exception as exc:  # pragma: no cover - depends on the host
                result["reason"] = (
                    "A gs:// checkpoint cannot be inspected here: the "
                    f"google-cloud-storage client is unavailable ({exc}). The "
                    "checkpoint is neither confirmed present nor confirmed absent."
                )
                return result
            bucket_name = parsed.netloc
            prefix = parsed.path.lstrip("/")
            try:
                client = storage.Client()
                blobs = list(client.list_blobs(bucket_name, prefix=prefix, max_results=1))
                result["exists"] = bool(blobs)
                result["verification"] = "verified_present" if blobs else "verified_absent"
                result["reason"] = (
                    f"gs://{bucket_name}/{prefix} "
                    + ("holds at least one object." if blobs else "holds no object.")
                )
            except Exception as exc:  # pragma: no cover - depends on credentials
                result["reason"] = (
                    f"gs://{bucket_name}/{prefix} could not be listed ({exc}); the "
                    "checkpoint is neither confirmed present nor confirmed absent."
                )
            return result

        result["reason"] = (
            f"Checkpoint scheme '{scheme}' is not one this deployment knows how to "
            "inspect, so the checkpoint cannot be confirmed."
        )
        return result

    def can_resume_from_checkpoint(self, workload_id: str) -> tuple[bool, str]:
        """Determine if workload can resume from saved checkpoint or must restart from scratch."""
        try:
            rec = self._require(workload_id)
        except KeyError:
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

        # The flags above only say what the workload *claims*. Look at the storage.
        evidence = self.verify_checkpoint(workload_id)
        attempts = rec["attempts"]
        last_attempt_note = (
            f"Previous attempt #{attempts[-1].attempt_number} is on record. "
            if attempts
            else ""
        )

        if evidence["verification"] == "verified_present":
            return (
                True,
                f"Checkpoint verified at '{prof.checkpoint_location}': {evidence['reason']} "
                f"{last_attempt_note}Resume can start from the saved state.",
            )

        if evidence["verification"] == "verified_absent":
            if attempts:
                return (
                    False,
                    f"No checkpoint exists at '{prof.checkpoint_location}': "
                    f"{evidence['reason']} {last_attempt_note}"
                    "The previous attempt left nothing to resume from, so the next "
                    "execution restarts from 0%.",
                )
            return (
                True,
                f"Checkpointing is enabled and '{prof.checkpoint_location}' is currently "
                f"empty ({evidence['reason']}), which is expected before the first "
                "attempt writes a checkpoint.",
            )

        return (
            True,
            f"Checkpointing is declared at '{prof.checkpoint_location}', but its "
            f"presence could NOT be verified from here: {evidence['reason']} "
            f"{last_attempt_note}Treat resume as unproven: the run may restart from 0%.",
        )

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
