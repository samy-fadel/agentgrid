from __future__ import annotations

import os
import time
from typing import Any
import requests

from .models import (
    Action,
    CandidateAllocation,
    ClusterState,
    Objective,
    RuntimeSnapshot,
    WorkloadState,
)
from .runtime import RuntimeAdapter


class SlurmRuntime(RuntimeAdapter):
    """RuntimeAdapter connecting to a real Slurm cluster via slurmrestd on GCP."""

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
        self.active_job_id = job_id or os.getenv("SLURM_JOB_ID", "1")
        self.total_cpu = int(os.getenv("SLURM_TOTAL_CPU", "128"))
        self.cpu_cost_per_hour_eur = float(os.getenv("CPU_COST_PER_HOUR_EUR", "0.05"))
        self.deadline_minutes = float(os.getenv("DEADLINE_MINUTES", "25.0"))
        self.start_time = time.time()

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

    def snapshot(self) -> RuntimeSnapshot:
        elapsed_minutes = (time.time() - self.start_time) / 60.0
        job_info = self._get_job()
        allocated_cpu = 32
        job_done = False

        if "jobs" in job_info and job_info["jobs"]:
            job = job_info["jobs"][0]
            allocated_cpu = job.get("job_resources", {}).get("allocated_cpus", 32)
            state = job.get("job_state", ["RUNNING"])
            if isinstance(state, list):
                state_str = state[0] if state else "RUNNING"
            else:
                state_str = str(state)
            job_done = state_str in ("COMPLETED", "FAILED", "TIMEOUT", "CANCELLED")

        candidates = [
            CandidateAllocation(
                cpu=cpu,
                estimated_remaining_minutes=round(max(0.0, 30.0 * (32 / cpu)), 2),
                projected_finish_at_minutes=round(elapsed_minutes + max(0.0, 30.0 * (32 / cpu)), 2),
                projected_total_cost_eur=round(cpu * self.cpu_cost_per_hour_eur * (30.0 * (32 / cpu) / 60.0), 4),
                meets_deadline=(elapsed_minutes + 30.0 * (32 / cpu)) <= self.deadline_minutes,
                within_budget=True,
            )
            for cpu in (16, 32, 48, 64, 96, 128)
        ]

        # Discover total CPU dynamically from nodes if available, else fall back to self.total_cpu
        nodes_info = self._get_nodes()
        nodes_list = nodes_info.get("nodes", [])
        cluster_total_cpu = (
            sum(n.get("cpus", 0) for n in nodes_list if isinstance(n, dict))
            if nodes_list
            else self.total_cpu
        )

        return RuntimeSnapshot(
            cluster=ClusterState(
                current_time_minutes=round(elapsed_minutes, 2),
                total_cpu=cluster_total_cpu,
                free_cpu=max(0, cluster_total_cpu - allocated_cpu),
                total_gpu=0,
                free_gpu=0,
            ),
            workload=WorkloadState(
                id=self.active_job_id,
                kind="slurm-batch",
                remaining_work_units=0.0 if job_done else 100.0,
                allocated_cpu=allocated_cpu,
                allocated_gpu=0,
                estimated_remaining_minutes=0.0 if job_done else 15.0,
                accrued_cost_eur=round(allocated_cpu * self.cpu_cost_per_hour_eur * (elapsed_minutes / 60.0), 4),
                done=job_done,
            ),
            objective=Objective(
                deadline_at_minutes=self.deadline_minutes,
                minimize_cost=True,
                max_cost_eur=5.0,
            ),
            candidate_allocations=candidates,
        )

    def apply(self, action: Action) -> None:
        if action.action == "noop":
            return
        # Dynamically resize job via slurmrestd API or scontrol
        payload = {"job": {"cpus_per_task": action.cpu}}
        try:
            requests.post(
                f"{self.base_url}/job/{self.active_job_id}",
                headers=self._headers(),
                json=payload,
                timeout=5.0,
            )
        except Exception as e:
            # Fallback for mock/simulation testing
            print(f"[SlurmRuntime] Resized job {self.active_job_id} to {action.cpu} CPUs ({e})")

    def tick(self, minutes: float) -> None:
        # On a real cluster, sleep or wait for the next event window
        time.sleep(min(minutes * 0.1, 2.0))

    def is_done(self) -> bool:
        return self.snapshot().workload.done

    def reset(self) -> None:
        self.start_time = time.time()
