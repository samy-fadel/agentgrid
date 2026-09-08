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
        self.active_job_id = job_id or os.getenv("SLURM_JOB_ID", "1")
        self.total_cpu = int(os.getenv("SLURM_TOTAL_CPU", "128"))
        self.cpu_cost_per_hour_eur = float(os.getenv("CPU_COST_PER_HOUR_EUR", "0.05"))
        self.deadline_minutes = float(os.getenv("DEADLINE_MINUTES", "25.0"))
        self.reset()

    def reset(self) -> None:
        self.start_time = time.time()
        self.elapsed_minutes = 0.0
        self.allocated_cpu = int(os.getenv("INITIAL_WORKLOAD_CPU", "32"))
        self.remaining_work_units = 100.0
        self.accrued_cost_eur = 0.0
        self.job_done = False
        self.is_real_slurm_job = False

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
        cpu: int = 32,
        script: str | None = None,
    ) -> dict[str, Any]:
        """Submit or initialize a new workload on the Slurm cluster."""
        payload = {
            "job": {
                "name": name,
                "tasks": 1,
                "cpus_per_task": cpu,
                "current_working_directory": "/tmp",
                "environment": ["PATH=/bin:/usr/bin:/usr/local/bin"],
                "script": script or "#!/bin/bash\nsleep 3600\n",
            }
        }
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
        self.allocated_cpu = cpu
        self.remaining_work_units = 100.0
        self.elapsed_minutes = 0.0
        self.accrued_cost_eur = 0.0
        self.job_done = False
        self.is_real_slurm_job = bool(submitted_id)

        return {
            "job_id": self.active_job_id,
            "allocated_cpu": self.allocated_cpu,
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

        # Candidate allocations based on current remaining work
        candidates = []
        base_time = (self.remaining_work_units / 100.0) * 25.0
        for cpu in (16, 32, 48, 64, 96, 128):
            speedup = cpu / 32.0
            est_remaining = round(max(0.0, base_time / speedup), 2)
            finish_at = round(self.elapsed_minutes + est_remaining, 2)
            est_cost = round(self.accrued_cost_eur + (cpu * self.cpu_cost_per_hour_eur * (est_remaining / 60.0)), 4)
            candidates.append(
                CandidateAllocation(
                    cpu=cpu,
                    estimated_remaining_minutes=est_remaining,
                    projected_finish_at_minutes=finish_at,
                    projected_total_cost_eur=est_cost,
                    meets_deadline=finish_at <= self.deadline_minutes,
                    within_budget=est_cost <= 5.0,
                )
            )

        # Dynamic discovery of cluster nodes and total CPU from Slurm
        nodes_info = self._get_nodes()
        nodes_list = nodes_info.get("nodes", [])
        cluster_total_cpu = (
            sum(n.get("cpus", 0) for n in nodes_list if isinstance(n, dict))
            if nodes_list
            else self.total_cpu
        )

        return RuntimeSnapshot(
            cluster=ClusterState(
                current_time_minutes=round(self.elapsed_minutes, 2),
                total_cpu=cluster_total_cpu,
                free_cpu=max(0, cluster_total_cpu - self.allocated_cpu),
                total_gpu=0,
                free_gpu=0,
            ),
            workload=WorkloadState(
                id=self.active_job_id,
                kind="slurm-batch",
                remaining_work_units=round(self.remaining_work_units, 2),
                allocated_cpu=self.allocated_cpu,
                allocated_gpu=0,
                estimated_remaining_minutes=round(max(0.0, base_time / (self.allocated_cpu / 32.0)), 2),
                accrued_cost_eur=round(self.accrued_cost_eur, 4),
                done=self.job_done,
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
