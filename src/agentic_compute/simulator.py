from __future__ import annotations

from dataclasses import dataclass
import time

from .models import (
    Action,
    CandidateAllocation,
    ClusterState,
    Objective,
    RuntimeSnapshot,
    WorkloadState,
)
from .runtime import RuntimeAdapter
from .slurm_adapter import generate_candidate_cpus


@dataclass
class _SimWorkload:
    id: str = "mc-001"
    kind: str = "monte-carlo"
    remaining_work_units: float = 500.0
    allocated_cpu: int = 32
    allocated_gpu: int = 0
    accrued_cost_eur: float = 0.0


class SimulatedRuntime(RuntimeAdapter):
    """Deterministic runtime with no planning intelligence."""

    def __init__(
        self,
        *,
        total_cpu: int = 128,
        cpu_cost_per_hour_eur: float = 0.05,
        base_rate_units_per_minute: float = 1.0,
        parallel_fraction: float = 0.97,
        deadline_minutes_from_start: float = 25.0,
        max_cost_eur: float | None = 5.0,
    ) -> None:
        self.total_cpu = total_cpu
        self.total_gpu = 0
        self.cpu_cost_per_hour_eur = cpu_cost_per_hour_eur
        self.base_rate = base_rate_units_per_minute
        self.parallel_fraction = parallel_fraction
        self.deadline_minutes_from_start = deadline_minutes_from_start
        self.max_cost_eur = max_cost_eur
        self.reset()

    def reset(self) -> None:
        self.current_time_minutes = 0.0
        self.workload = _SimWorkload()
        self.objective = Objective(
            deadline_at_minutes=self.deadline_minutes_from_start,
            minimize_cost=True,
            max_cost_eur=self.max_cost_eur,
        )
        self.last_slurm_action = None

    def configure_objective(
        self,
        deadline_minutes: float | None = None,
        max_cost_eur: float | None = None,
        minimize_cost: bool | None = None,
    ) -> None:
        """Dynamically configure workload objective constraints."""
        deadline = deadline_minutes if deadline_minutes is not None else self.objective.deadline_at_minutes
        budget = max_cost_eur if max_cost_eur is not None else self.objective.max_cost_eur
        minimize = minimize_cost if minimize_cost is not None else self.objective.minimize_cost
        self.objective = Objective(
            deadline_at_minutes=deadline,
            minimize_cost=minimize,
            max_cost_eur=budget,
        )

    def submit_job(
        self,
        name: str = "mc-002",
        cpu: int | None = None,
        gpu: int = 0,
        partition: str | None = None,
        memory_mb: int | None = None,
        script: str | None = None,
    ) -> dict[str, Any]:
        """Initialize or reset workload with custom parameters."""
        allocated_cpu = cpu if cpu is not None else 4
        self.workload = _SimWorkload(id=name, allocated_cpu=allocated_cpu, allocated_gpu=gpu)
        self.current_time_minutes = 0.0
        return {"id": name, "cpu": allocated_cpu, "gpu": gpu, "partition": partition}

    def _speedup(self, cpu: int) -> float:
        p = self.parallel_fraction
        return 1.0 / ((1.0 - p) + p / cpu)

    def _rate(self, cpu: int) -> float:
        return self.base_rate * self._speedup(cpu)

    def _eta_minutes(self, cpu: int) -> float:
        if self.workload.remaining_work_units <= 0:
            return 0.0
        return self.workload.remaining_work_units / self._rate(cpu)

    def _future_cost(self, cpu: int, eta_minutes: float) -> float:
        return cpu * self.cpu_cost_per_hour_eur * (eta_minutes / 60.0)

    def _candidate(self, cpu: int) -> CandidateAllocation:
        eta = self._eta_minutes(cpu)
        finish = self.current_time_minutes + eta
        projected_cost = self.workload.accrued_cost_eur + self._future_cost(cpu, eta)
        budget = self.objective.max_cost_eur
        return CandidateAllocation(
            cpu=cpu,
            estimated_remaining_minutes=round(eta, 3),
            projected_finish_at_minutes=round(finish, 3),
            projected_total_cost_eur=round(projected_cost, 4),
            meets_deadline=finish <= self.objective.deadline_at_minutes,
            within_budget=(budget is None or projected_cost <= budget),
        )

    def snapshot(self) -> RuntimeSnapshot:
        allocated = 0 if self.is_done() else self.workload.allocated_cpu
        candidate_steps = generate_candidate_cpus(self.total_cpu)
        candidates = [self._candidate(cpu) for cpu in candidate_steps]
        return RuntimeSnapshot(
            cluster=ClusterState(
                current_time_minutes=round(self.current_time_minutes, 3),
                total_cpu=self.total_cpu,
                free_cpu=self.total_cpu - allocated,
                total_gpu=self.total_gpu,
                free_gpu=self.total_gpu,
            ),
            workload=WorkloadState(
                id=self.workload.id,
                kind=self.workload.kind,
                remaining_work_units=round(self.workload.remaining_work_units, 3),
                allocated_cpu=self.workload.allocated_cpu,
                allocated_gpu=self.workload.allocated_gpu,
                estimated_remaining_minutes=round(
                    self._eta_minutes(self.workload.allocated_cpu), 3
                ),
                accrued_cost_eur=round(self.workload.accrued_cost_eur, 4),
                done=self.is_done(),
            ),
            objective=self.objective,
            candidate_allocations=candidates,
        )

    def apply(self, action: Action) -> None:
        if action.workload_id != self.workload.id:
            raise ValueError(f"Unknown workload: {action.workload_id}")

        if action.action == "noop":
            return

        assert action.cpu is not None

        if action.cpu > self.total_cpu:
            raise ValueError(
                f"Requested {action.cpu} CPUs, cluster has only {self.total_cpu}"
            )

        allowed = {c.cpu for c in self.snapshot().candidate_allocations}
        if action.cpu not in allowed:
            raise ValueError(
                f"CPU allocation {action.cpu} is not allowed. Choose from {sorted(allowed)}"
            )

        self.workload.allocated_cpu = action.cpu
        self.last_slurm_action = {
            "action": action.action,
            "job_id": self.workload.id,
            "requested_cpu": action.cpu,
            "status_code": 200,
            "simulated": True,
            "timestamp": time.time(),
        }

    def tick(self, minutes: float) -> None:
        if minutes <= 0:
            raise ValueError("minutes must be > 0")
        if self.is_done():
            return

        rate = self._rate(self.workload.allocated_cpu)
        time_to_finish = self.workload.remaining_work_units / rate
        actual_minutes = min(minutes, time_to_finish)

        self.workload.remaining_work_units = max(
            0.0,
            self.workload.remaining_work_units - rate * actual_minutes,
        )
        self.workload.accrued_cost_eur += (
            self.workload.allocated_cpu
            * self.cpu_cost_per_hour_eur
            * actual_minutes
            / 60.0
        )
        self.current_time_minutes += actual_minutes

    def is_done(self) -> bool:
        return self.workload.remaining_work_units <= 1e-9
