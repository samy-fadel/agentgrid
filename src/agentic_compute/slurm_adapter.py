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
        self.active_job_id = job_id or os.getenv("SLURM_JOB_ID") or self._discover_active_job() or "1"
        self.reset()

    def reset(self) -> None:
        self.start_time = time.time()
        self.elapsed_minutes = 0.0
        self.allocated_cpu = int(os.getenv("INITIAL_WORKLOAD_CPU", "4"))
        self.allocated_gpu = 0
        self.remaining_work_units = 100.0
        self.accrued_cost_eur = 0.0
        self.job_done = False
        self.is_real_slurm_job = False

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
            resp = requests.get(f"{self.base_url}/jobs", headers=self._headers(), timeout=5.0)
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
            resp = requests.get(f"{self.base_url}/nodes", headers=self._headers(), timeout=5.0)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {"nodes": []}

    def _get_job(self) -> dict[str, Any]:
        try:
            resp = requests.get(
                f"{self.base_url}/job/{self.active_job_id}",
                headers=self._headers(),
                timeout=5.0,
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
    ) -> dict[str, Any]:
        """Submit or initialize a new workload on the Slurm cluster with elastic parameters."""
        allocated_cpu = cpu if cpu is not None else int(os.getenv("INITIAL_WORKLOAD_CPU", "4"))
        payload_job: dict[str, Any] = {
            "name": name,
            "tasks": 1,
            "cpus_per_task": allocated_cpu,
            "current_working_directory": "/tmp",
            "environment": ["PATH=/bin:/usr/bin:/usr/local/bin"],
            "script": script or "#!/bin/bash\nsleep 3600\n",
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
        except Exception:
            pass

        self.active_job_id = submitted_id or str(int(time.time()) % 100000)
        self.allocated_cpu = allocated_cpu
        self.allocated_gpu = gpu
        self.remaining_work_units = 100.0
        self.elapsed_minutes = 0.0
        self.accrued_cost_eur = 0.0
        self.job_done = False
        self.is_real_slurm_job = bool(submitted_id)

        return {
            "job_id": self.active_job_id,
            "allocated_cpu": self.allocated_cpu,
            "allocated_gpu": self.allocated_gpu,
            "partition": partition,
            "is_real_slurm_job": self.is_real_slurm_job,
        }

    def snapshot(self) -> RuntimeSnapshot:
        # Check real job if active
        job_info = self._get_job()
        if "jobs" in job_info and job_info["jobs"]:
            self.is_real_slurm_job = True
            job = job_info["jobs"][0]
            self.allocated_cpu = job.get("job_resources", {}).get("allocated_cpus", self.allocated_cpu)
            state = job.get("job_state", ["RUNNING"])
            state_str = state[0] if isinstance(state, list) and state else str(state)
            if state_str in ("COMPLETED", "FAILED", "TIMEOUT", "CANCELLED"):
                self.job_done = True
                self.remaining_work_units = 0.0

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

        # Dynamic candidate allocations scaled to cluster capacity
        candidate_cpu_steps = generate_candidate_cpus(cluster_total_cpu)
        candidates = []
        base_time = (self.remaining_work_units / 100.0) * self.deadline_minutes
        for cpu in candidate_cpu_steps:
            speedup = cpu / max(1, self.allocated_cpu)
            est_remaining = round(max(0.0, (self.remaining_work_units / 100.0) * (self.deadline_minutes * 0.8) / (cpu / 32.0)), 2)
            finish_at = round(self.elapsed_minutes + est_remaining, 2)
            est_cost = round(self.accrued_cost_eur + (cpu * self.cpu_cost_per_hour_eur * (est_remaining / 60.0)), 4)
            candidates.append(
                CandidateAllocation(
                    cpu=cpu,
                    estimated_remaining_minutes=est_remaining,
                    projected_finish_at_minutes=finish_at,
                    projected_total_cost_eur=est_cost,
                    meets_deadline=finish_at <= self.deadline_minutes,
                    within_budget=est_cost <= self.max_cost_eur,
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
                estimated_remaining_minutes=round(max(0.0, (self.remaining_work_units / 100.0) * (self.deadline_minutes * 0.8) / (self.allocated_cpu / 32.0)), 2),
                accrued_cost_eur=round(self.accrued_cost_eur, 4),
                done=self.job_done,
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
        if action.cpu:
            self.allocated_cpu = action.cpu
            # Attempt Slurm REST update if real job exists
            try:
                requests.post(
                    f"{self.base_url}/job/{self.active_job_id}",
                    headers=self._headers(),
                    json={"job": {"cpus_per_task": action.cpu}},
                    timeout=5.0,
                )
            except Exception:
                pass

    def tick(self, minutes: float) -> None:
        if self.job_done:
            return
        self.elapsed_minutes += minutes
        speedup = self.allocated_cpu / 32.0
        # 100 units completed in 25 min at 32 cpus -> 4 units/min at speedup 1.0
        work_completed = minutes * 4.0 * speedup
        self.remaining_work_units = max(0.0, self.remaining_work_units - work_completed)
        self.accrued_cost_eur += (self.allocated_cpu * self.cpu_cost_per_hour_eur * (minutes / 60.0))
        if self.remaining_work_units <= 0.0:
            self.job_done = True
            self.remaining_work_units = 0.0

    def is_done(self) -> bool:
        return self.job_done
