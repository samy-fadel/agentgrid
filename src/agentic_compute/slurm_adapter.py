from __future__ import annotations

import os
import time
from typing import Any
import requests

import re

from .models import (
    Action,
    CandidateAllocation,
    ClusterState,
    Objective,
    RuntimeSnapshot,
    WorkloadState,
)
from .runtime import RuntimeAdapter


def generate_candidate_cpus(total_cpu: int) -> list[int]:
    """Generate elastic candidate CPU allocations up to cluster capacity."""
    env_override = os.getenv("CANDIDATE_CPUS")
    if env_override:
        try:
            return sorted([int(x.strip()) for x in env_override.split(",") if x.strip()])
        except Exception:
            pass
    standard_steps = [2, 4, 8, 16, 32, 48, 64, 96, 128, 192, 256, 384, 512, 1024]
    candidates = [c for c in standard_steps if c <= total_cpu]
    if not candidates:
        return [max(1, total_cpu)]
    if total_cpu not in candidates and total_cpu > candidates[0]:
        candidates.append(total_cpu)
        candidates.sort()
    return candidates


class SlurmRuntime(RuntimeAdapter):
    """RuntimeAdapter connecting to a real Slurm cluster via slurmrestd on GCP with dynamic lifecycle tracking."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        jwt_token: str | None = None,
        job_id: str | None = None,
        user_name: str | None = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.getenv("SLURM_REST_URL", "http://10.0.0.4:6842/slurm/v0.0.41")
        ).rstrip("/")
        self.jwt_token = jwt_token or os.getenv("SLURM_JWT_TOKEN", "")
        self.user_name = user_name or os.getenv("SLURM_USER", "slurm")
        self.total_cpu = int(os.getenv("SLURM_TOTAL_CPU", "128"))
        self.cpu_cost_per_hour_eur = float(os.getenv("CPU_COST_PER_HOUR_EUR", "0.05"))
        self.deadline_minutes = float(os.getenv("DEADLINE_MINUTES", "30.0"))
        self.max_cost_eur = float(os.getenv("MAX_COST_EUR", "10.0"))
        self.minimize_cost = os.getenv("MINIMIZE_COST", "true").lower() in ("true", "1", "yes")
        self.baseline_workload_duration_minutes = float(os.getenv("BASELINE_WORKLOAD_DURATION_MINUTES", "25.0"))
        self.machine_type = "n2-standard-16"
        self.provisioning_mix = "100% Spot"
        self.job_status = "RUNNING"
        self.job_failed = False
        self.last_slurm_elapsed_secs: float | None = None
        self.active_job_id = job_id or os.getenv("SLURM_JOB_ID") or "1"
        self.reset()

    def reset(self) -> None:
        self.start_time = time.time()
        self.elapsed_minutes = 0.0
        self.allocated_cpu = int(os.getenv("INITIAL_WORKLOAD_CPU", "4"))
        self.allocated_gpu = 0
        self.remaining_work_units = 100.0
        self.accrued_cost_eur = 0.0
        self.job_done = False
        self.job_status = "RUNNING"
        self.job_failed = False
        self.is_real_slurm_job = False
        self.last_slurm_action = None
        self.machine_type = "n2-standard-16"
        self.provisioning_mix = "100% Spot"
        self.last_slurm_elapsed_secs = None

    def configure_objective(
        self,
        deadline_minutes: float | None = None,
        max_cost_eur: float | None = None,
        minimize_cost: bool | None = None,
    ) -> None:
        """Dynamically configure workload objective constraints."""
        if deadline_minutes is not None:
            self.deadline_minutes = float(deadline_minutes)
        if max_cost_eur is not None:
            self.max_cost_eur = float(max_cost_eur)
        if minimize_cost is not None:
            self.minimize_cost = bool(minimize_cost)

    def _discover_active_job(self) -> str | None:
        try:
            resp = requests.get(f"{self.base_url}/jobs", headers=self._headers(), timeout=2.0)
            if resp.status_code == 200:
                jobs = resp.json().get("jobs", [])
                for j in jobs:
                    state = j.get("job_state", ["RUNNING"])
                    state_str = state[0] if isinstance(state, list) and state else str(state)
                    if state_str in ("RUNNING", "PENDING"):
                        return str(j.get("job_id", ""))
        except Exception:
            pass
        return None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.jwt_token:
            headers["X-SLURM-USER-TOKEN"] = self.jwt_token
            headers["X-SLURM-USER-NAME"] = self.user_name
        return headers

    def _get_nodes(self) -> dict[str, Any]:
        try:
            resp = requests.get(f"{self.base_url}/nodes", headers=self._headers(), timeout=2.0)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {"nodes": []}

    def _get_job(self) -> dict[str, Any]:
        if not self.active_job_id or self.active_job_id == "1":
            discovered = self._discover_active_job()
            if discovered:
                self.active_job_id = discovered
        try:
            resp = requests.get(
                f"{self.base_url}/job/{self.active_job_id}",
                headers=self._headers(),
                timeout=2.0,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {"jobs": []}

    def submit_job(
        self,
        name: str = "agentgrid-workload",
        cpu: int | None = None,
        gpu: int = 0,
        partition: str | None = None,
        memory_mb: int | None = None,
        script: str | None = None,
        machine_type: str | None = None,
        provisioning_model: str | None = None,
    ) -> dict[str, Any]:
        """Submit or initialize a new workload on the Slurm cluster with elastic parameters."""
        allocated_cpu = cpu if cpu is not None else int(os.getenv("INITIAL_WORKLOAD_CPU", "4"))
        target_machine_type = machine_type or self.machine_type
        target_provisioning_mix = provisioning_model or self.provisioning_mix
        comment_str = f"machine_type={target_machine_type};provisioning_model={target_provisioning_mix}"

        payload_job: dict[str, Any] = {
            "name": name,
            "tasks": 1,
            "cpus_per_task": allocated_cpu,
            "current_working_directory": "/tmp",
            "environment": ["PATH=/bin:/usr/bin:/usr/local/bin"],
            "script": script or "#!/bin/bash\nsleep 3600\n",
            "features": target_machine_type,
            "constraints": target_machine_type,
            "comment": comment_str,
            "admin_comment": comment_str,
        }
        if partition:
            payload_job["partition"] = partition
        if memory_mb:
            payload_job["memory_per_node"] = memory_mb
        if gpu > 0:
            payload_job["tres_per_task"] = f"gres/gpu:{gpu}"

        payload = {"job": payload_job}
        submitted_id = None
        try:
            resp = requests.post(
                f"{self.base_url}/job/submit",
                headers=self._headers(),
                json=payload,
                timeout=5.0,
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                submitted_id = str(data.get("job_id") or data.get("job_submit_response_msg", {}).get("job_id", ""))
            else:
                if os.getenv("MOCK_SLURM", "").lower() in ("true", "1"):
                    submitted_id = f"mock-{int(time.time()) % 100000}"
                else:
                    raise RuntimeError(
                        f"Slurm submission rejected (HTTP {resp.status_code}): {resp.text}"
                    )
        except requests.RequestException as exc:
            if os.getenv("MOCK_SLURM", "").lower() in ("true", "1"):
                submitted_id = f"mock-{int(time.time()) % 100000}"
            else:
                raise RuntimeError(f"Could not connect to Slurm cluster at {self.base_url}: {exc}") from exc

        if not submitted_id:
            raise RuntimeError(f"Slurm did not return a valid job_id from {self.base_url}/job/submit")

        self.active_job_id = submitted_id
        self.allocated_cpu = allocated_cpu
        self.allocated_gpu = gpu
        self.machine_type = target_machine_type
        self.provisioning_mix = target_provisioning_mix
        self.remaining_work_units = 100.0
        self.elapsed_minutes = 0.0
        self.accrued_cost_eur = 0.0
        self.job_done = False
        self.job_status = "RUNNING"
        self.job_failed = False
        self.last_slurm_elapsed_secs = None
        self.is_real_slurm_job = not submitted_id.startswith("mock-")

        return {
            "job_id": self.active_job_id,
            "allocated_cpu": self.allocated_cpu,
            "allocated_gpu": self.allocated_gpu,
            "partition": partition,
            "machine_type": self.machine_type,
            "provisioning_mix": self.provisioning_mix,
            "is_real_slurm_job": self.is_real_slurm_job,
        }

    def _get_cost_factor(self) -> float:
        if "80% Spot" in self.provisioning_mix:
            return 0.48
        elif "Spot" in self.provisioning_mix or "SPOT" in self.provisioning_mix:
            return 0.35
        else:
            return 1.0

    def _sync_job_state(self, job: dict[str, Any]) -> None:
        """Synchronize local runtime state with authoritative Slurm job telemetry."""
        # 1. Update allocated CPU from Slurm job resources if present
        res = job.get("job_resources", {})
        if isinstance(res, dict) and res.get("allocated_cpus") is not None:
            self.allocated_cpu = int(res["allocated_cpus"])

        # 2. Synchronize elapsed time and accrue cost for delta elapsed seconds
        time_info = job.get("time", {})
        slurm_elapsed_secs = time_info.get("elapsed", 0)
        if isinstance(slurm_elapsed_secs, (int, float)) and slurm_elapsed_secs >= 0:
            if self.last_slurm_elapsed_secs is None:
                delta_secs = float(slurm_elapsed_secs)
            else:
                delta_secs = max(0.0, float(slurm_elapsed_secs) - self.last_slurm_elapsed_secs)
            self.last_slurm_elapsed_secs = float(slurm_elapsed_secs)
            self.elapsed_minutes = float(slurm_elapsed_secs) / 60.0

            cost_factor = self._get_cost_factor()
            if delta_secs > 0:
                self.accrued_cost_eur += (
                    self.allocated_cpu
                    * self.cpu_cost_per_hour_eur
                    * cost_factor
                    * (delta_secs / 3600.0)
                )

        # 3. Synchronize lifecycle status
        state = job.get("job_state", ["RUNNING"])
        state_str = state[0] if isinstance(state, list) and state else str(state)
        self.job_status = state_str

        if state_str == "COMPLETED":
            self.job_done = True
            self.job_failed = False
            self.remaining_work_units = 0.0
        elif state_str in ("FAILED", "TIMEOUT", "CANCELLED", "BOOT_FAIL", "NODE_FAIL", "DEADLINE"):
            self.job_done = True
            self.job_failed = True
            # Do NOT reset remaining_work_units to 0.0 on failure: preserves unfinished work indicator
        else:
            # RUNNING or PENDING
            self.job_done = False
            self.job_failed = False
            expected_total_secs = (
                self.baseline_workload_duration_minutes * 60.0 * (16.0 / max(1, self.allocated_cpu))
            )
            elapsed = float(slurm_elapsed_secs) if isinstance(slurm_elapsed_secs, (int, float)) else 0.0
            progress = min(1.0, elapsed / max(1.0, expected_total_secs))
            self.remaining_work_units = max(0.1, round(100.0 * (1.0 - progress), 2))

    def snapshot(self) -> RuntimeSnapshot:
        # Check real job if active
        job_info = self._get_job()
        if "jobs" in job_info and job_info["jobs"]:
            self.is_real_slurm_job = True
            self._sync_job_state(job_info["jobs"][0])

        # Dynamic discovery of cluster nodes, total CPU, and GPUs from Slurm
        nodes_info = self._get_nodes()
        nodes_list = nodes_info.get("nodes", [])
        cluster_total_cpu = (
            sum(n.get("cpus", 0) for n in nodes_list if isinstance(n, dict))
            if nodes_list
            else self.total_cpu
        )

        cluster_total_gpu = 0
        for n in nodes_list:
            if isinstance(n, dict):
                gres = str(n.get("gres", "") or n.get("tres", ""))
                if "gpu" in gres.lower():
                    match = re.search(r'gpu(?::[^:]*)?:(\d+)', gres, re.IGNORECASE)
                    if match:
                        cluster_total_gpu += int(match.group(1))

        # Dynamic candidate allocations scaled from current allocation
        candidate_cpu_steps = generate_candidate_cpus(cluster_total_cpu)
        candidates = []
        curr_cpu = max(1, self.allocated_cpu)
        if self.job_done:
            curr_remaining = 0.0
        else:
            # Case 4.1 fix: decoupled from deadline_minutes; based on intrinsic baseline duration
            curr_remaining = max(
                0.0,
                self.baseline_workload_duration_minutes * (self.remaining_work_units / 100.0) * (16.0 / curr_cpu),
            )

        for cpu in candidate_cpu_steps:
            speedup = cpu / curr_cpu
            est_remaining = round(max(0.0, curr_remaining / speedup), 2) if not self.job_done else 0.0
            finish_at = round(self.elapsed_minutes + est_remaining, 2)

            # Dynamic Slack Ratio S = (Deadline - CurrentElapsed) / ETA
            slack_time = max(0.0, self.deadline_minutes - self.elapsed_minutes)
            slack_ratio = (slack_time / est_remaining) if est_remaining > 0 else 99.0

            # Dynamic Spot / Standard hedging mix and pricing factor
            if slack_ratio > 1.5:
                pmix = "100% Spot"
                cost_factor = 0.35
            elif slack_ratio > 1.1:
                pmix = "80% Spot / 20% Standard"
                cost_factor = 0.48
            else:
                pmix = "100% Standard"
                cost_factor = 1.0

            if cpu >= 64:
                mtype = f"n4-standard-{min(64, cpu)}"
                rank = 1
                obtainability = 0.92 if slack_ratio > 1.5 else 0.88
            elif cpu >= 16:
                mtype = f"n2-standard-{cpu}"
                rank = 2
                obtainability = 0.90 if slack_ratio > 1.5 else 0.85
            else:
                mtype = f"n2-standard-{max(4, cpu)}"
                rank = 2
                obtainability = 0.95 if slack_ratio > 1.5 else 0.90

            est_cost = round(
                self.accrued_cost_eur + (cpu * self.cpu_cost_per_hour_eur * cost_factor * (est_remaining / 60.0)),
                4,
            )
            candidates.append(
                CandidateAllocation(
                    cpu=cpu,
                    estimated_remaining_minutes=est_remaining,
                    projected_finish_at_minutes=finish_at,
                    projected_total_cost_eur=est_cost,
                    meets_deadline=finish_at <= self.deadline_minutes,
                    within_budget=est_cost <= self.max_cost_eur,
                    machine_type=mtype,
                    rank=rank,
                    provisioning_mix=pmix,
                    obtainability_score=obtainability,
                )
            )

        return RuntimeSnapshot(
            cluster=ClusterState(
                current_time_minutes=round(self.elapsed_minutes, 2),
                total_cpu=cluster_total_cpu,
                free_cpu=max(0, cluster_total_cpu - self.allocated_cpu),
                total_gpu=cluster_total_gpu,
                free_gpu=max(0, cluster_total_gpu - self.allocated_gpu),
            ),
            workload=WorkloadState(
                id=self.active_job_id,
                kind="slurm-batch",
                remaining_work_units=round(self.remaining_work_units, 2),
                allocated_cpu=self.allocated_cpu,
                allocated_gpu=self.allocated_gpu,
                estimated_remaining_minutes=round(curr_remaining, 2),
                accrued_cost_eur=round(self.accrued_cost_eur, 4),
                done=self.job_done,
                status=self.job_status,
                failed=self.job_failed,
                machine_type=self.machine_type,
                provisioning_mix=self.provisioning_mix,
            ),
            objective=Objective(
                deadline_at_minutes=self.deadline_minutes,
                minimize_cost=self.minimize_cost,
                max_cost_eur=self.max_cost_eur,
            ),
            candidate_allocations=candidates,
        )

    def apply(self, action: Action) -> None:
        if action.action == "noop":
            return
        if not action.cpu:
            return

        target_cpu = action.cpu
        target_machine_type = action.machine_type or self.machine_type
        target_provisioning_mix = action.provisioning_model or self.provisioning_mix

        job_patch: dict[str, Any] = {"cpus_per_task": target_cpu}
        if action.machine_type:
            job_patch["features"] = action.machine_type
            job_patch["constraints"] = action.machine_type

        # Propagate machine_type and provisioning_model to Slurm comments
        comment_str = f"machine_type={target_machine_type};provisioning_model={target_provisioning_mix}"
        job_patch["comment"] = comment_str
        job_patch["admin_comment"] = comment_str

        resp = None
        status_code = None
        error_msg = None
        try:
            resp = requests.post(
                f"{self.base_url}/job/{self.active_job_id}",
                headers=self._headers(),
                json={"job": job_patch},
                timeout=5.0,
            )
            status_code = resp.status_code
        except Exception as e:
            error_msg = str(e)

        if resp is not None and resp.status_code in (200, 201):
            self.allocated_cpu = target_cpu
            self.machine_type = target_machine_type
            self.provisioning_mix = target_provisioning_mix
            self.last_slurm_action = {
                "action": action.action,
                "job_id": self.active_job_id,
                "requested_cpu": target_cpu,
                "machine_type": self.machine_type,
                "provisioning_mix": self.provisioning_mix,
                "status_code": status_code,
                "status": "applied",
                "timestamp": time.time(),
            }
        elif os.getenv("MOCK_SLURM", "").lower() in ("true", "1") or str(self.active_job_id).startswith("mock-"):
            # Explicit mock fallback only for tests
            self.allocated_cpu = target_cpu
            self.machine_type = target_machine_type
            self.provisioning_mix = target_provisioning_mix
            self.last_slurm_action = {
                "action": action.action,
                "job_id": self.active_job_id,
                "requested_cpu": target_cpu,
                "machine_type": self.machine_type,
                "provisioning_mix": self.provisioning_mix,
                "status_code": status_code or 200,
                "status": "applied",
                "simulated": True,
                "timestamp": time.time(),
            }
        else:
            detail = resp.text if resp is not None else error_msg
            self.last_slurm_action = {
                "action": action.action,
                "job_id": self.active_job_id,
                "requested_cpu": target_cpu,
                "requested_machine_type": action.machine_type,
                "requested_provisioning_mix": action.provisioning_model,
                "machine_type": self.machine_type,
                "provisioning_mix": self.provisioning_mix,
                "status_code": status_code,
                "status": "failed",
                "error": detail,
                "timestamp": time.time(),
            }
            raise RuntimeError(
                f"Slurm rejected CPU resize to {target_cpu} for job {self.active_job_id} (HTTP {status_code}): {detail}"
            )

    def tick(self, minutes: float) -> None:
        if self.job_done:
            return

        cost_factor = self._get_cost_factor()

        # 1. Query real Slurm status first as primary source of truth
        job_info = self._get_job()
        if "jobs" in job_info and job_info["jobs"]:
            self.is_real_slurm_job = True
            self._sync_job_state(job_info["jobs"][0])
            return

        # Case 4.3 fix: If Slurm is inaccessible, never silently simulate completion unless explicit mock mode
        is_mock_enabled = os.getenv("MOCK_SLURM", "").lower() in ("true", "1", "yes") or str(self.active_job_id).startswith("mock-")
        if not is_mock_enabled:
            raise RuntimeError(
                f"Slurm cluster is unreachable at {self.base_url} (no active job {self.active_job_id} found) and MOCK_SLURM is not enabled."
            )

        # Fallback for mock/test runs with explicit mock mode
        self.elapsed_minutes += minutes
        self.accrued_cost_eur += (
            self.allocated_cpu * self.cpu_cost_per_hour_eur * cost_factor * (minutes / 60.0)
        )
        speedup = self.allocated_cpu / 16.0
        work_completed = (minutes / self.baseline_workload_duration_minutes) * 100.0 * speedup
        self.remaining_work_units = max(0.0, self.remaining_work_units - work_completed)
        if self.remaining_work_units <= 0.0:
            self.job_done = True
            self.job_status = "COMPLETED"
            self.job_failed = False
            self.remaining_work_units = 0.0

    def is_done(self) -> bool:
        return self.job_done
