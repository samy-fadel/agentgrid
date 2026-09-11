from __future__ import annotations

import math
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
from .errors import ConfigurationRejected
from .runtime import RuntimeAdapter
from .slurm_adapter import generate_candidate_cpus


#: Machine shapes the simulated cluster is willing to model, with their vCPU
#: count. Requests for anything outside this catalogue are rejected rather than
#: silently accepted, so a plan that names an impossible machine cannot report
#: success.
SIMULATED_MACHINE_CATALOG: dict[str, int] = {
    "n2-standard-2": 2,
    "n2-standard-4": 4,
    "n2-standard-8": 8,
    "n2-standard-16": 16,
    "n2-standard-32": 32,
    "n2-standard-64": 64,
    "n4-standard-32": 32,
    "n4-standard-64": 64,
    "c2-standard-60": 60,
    "h3-standard-88": 88,
}

#: Canonical provisioning model names. Operators and adapters spell these in
#: several ways ("STANDARD", "100% Standard", "standard"); they all describe the
#: same thing and must not produce a spurious mismatch. Anything genuinely
#: unrecognised is rejected instead of being passed through.
_PROVISIONING_ALIASES: dict[str, str] = {
    "standard": "100% Standard",
    "100% standard": "100% Standard",
    "100%standard": "100% Standard",
    "on-demand": "100% Standard",
    "ondemand": "100% Standard",
    "spot": "100% Spot",
    "100% spot": "100% Spot",
    "100%spot": "100% Spot",
    "preemptible": "100% Spot",
    "80% spot / 20% standard": "80% Spot / 20% Standard",
    "80% spot/20% standard": "80% Spot / 20% Standard",
    "hedged": "80% Spot / 20% Standard",
}


def normalize_provisioning_model(value: str | None) -> str:
    """Map a provisioning-model spelling onto its canonical form.

    Returns ``100% Standard`` when nothing is specified. Raises
    :class:`ConfigurationRejected` for a genuinely unknown value so that a typo
    such as ``BANANA`` is refused instead of being stored and later compared
    against real telemetry.
    """
    if value is None or str(value).strip() == "":
        return "100% Standard"
    key = str(value).strip().lower()
    if key in _PROVISIONING_ALIASES:
        return _PROVISIONING_ALIASES[key]
    raise ConfigurationRejected(
        f"Unsupported provisioning model '{value}'. Supported values: "
        f"{', '.join(sorted({v for v in _PROVISIONING_ALIASES.values()}))}.",
        parameter="provisioning_model",
    )


def validate_machine_type(machine_type: str | None, cpu: int) -> str:
    """Validate a machine type against the simulated catalogue.

    When no machine type is supplied, the smallest shape that fits ``cpu`` is
    chosen so callers that do not care still get a coherent answer.
    """
    if machine_type is None or str(machine_type).strip() == "":
        for name, size in sorted(SIMULATED_MACHINE_CATALOG.items(), key=lambda kv: kv[1]):
            if size >= cpu:
                return name
        raise ConfigurationRejected(
            f"No catalogued machine type can provide {cpu} vCPUs.",
            parameter="machine_type",
        )

    mtype = str(machine_type).strip()
    if mtype not in SIMULATED_MACHINE_CATALOG:
        raise ConfigurationRejected(
            f"Unsupported machine type '{mtype}'. Supported types: "
            f"{', '.join(sorted(SIMULATED_MACHINE_CATALOG))}.",
            parameter="machine_type",
        )
    if SIMULATED_MACHINE_CATALOG[mtype] < cpu:
        raise ConfigurationRejected(
            f"Machine type '{mtype}' provides {SIMULATED_MACHINE_CATALOG[mtype]} vCPUs, "
            f"which cannot satisfy the requested {cpu} vCPUs.",
            parameter="machine_type",
        )
    return mtype


def compute_hedged_cost_factor(cpu: int, mix: str) -> float:
    """Compute effective pricing factor based on vCPU capacity and floor(cpu * 0.8) rounding."""
    if "80% Spot" in mix:
        spot_cpu = math.floor(cpu * 0.8)
        standard_cpu = cpu - spot_cpu
        return (spot_cpu * 0.35 + standard_cpu * 1.0) / max(1, cpu)
    elif "Spot" in mix or "SPOT" in mix:
        return 0.35
    return 1.0


@dataclass
class _SimWorkload:
    id: str = "mc-001"
    kind: str = "monte-carlo"
    remaining_work_units: float = 500.0
    allocated_cpu: int = 32
    allocated_gpu: int = 0
    accrued_cost_eur: float = 0.0
    machine_type: str = "n2-standard-32"
    provisioning_mix: str = "100% Spot"
    requested_cpu: int | None = None
    requested_machine_type: str | None = None
    requested_provisioning_mix: str | None = None
    verification_status: str = "verified"
    verification_detail: str | None = "Simulated allocation verified"
    cost_basis: str = "observed_allocation"


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
        super().__init__()
        self.control_mode = "delegation"
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
        # Names this runtime genuinely accepted via submit_job. The seeded demo
        # workload is deliberately absent: it was never submitted, so an
        # ambiguous-timeout recovery must not be able to "find" it.
        self._accepted_submissions: set[str] = set()

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

    def _discover_active_job(self, name: str | None = None) -> str | None:
        """Recover a job created under a *specific* submission identity.

        Only a job that was actually submitted under ``name`` counts. The
        previous version returned the current workload whenever ``name`` was
        None, which let an ambiguous-timeout recovery latch onto the unrelated
        demo workload ``mc-001`` and report it as a fresh successful submission.
        """
        if not name:
            return None
        if getattr(self, "workload", None) is None:
            return None
        if self.workload.id != name:
            return None
        # Only jobs this runtime actually accepted may be rediscovered.
        if name not in self._accepted_submissions:
            return None
        return self.workload.id

    def submit_job(
        self,
        name: str = "mc-002",
        cpu: int | None = None,
        gpu: int = 0,
        partition: str | None = None,
        memory_mb: int | None = None,
        script: str | None = None,
        machine_type: str | None = None,
        provisioning_model: str | None = None,
        approved_by_operator: bool = False,
    ) -> dict[str, Any]:
        """Create a simulated workload under the canonical runtime contract.

        ``machine_type`` and ``provisioning_model`` are part of the contract
        because MCP and the execution controller always send them. Previously
        this signature omitted them, so every controller-driven submission
        raised ``TypeError`` -- which the controller then misread as a network
        timeout and reported as a verified success.
        """
        allocated_cpu = cpu if cpu is not None else 4

        if allocated_cpu <= 0:
            raise ConfigurationRejected(
                f"Requested cpu={allocated_cpu} is invalid; must be a positive integer.",
                parameter="cpu",
            )
        if allocated_cpu > self.total_cpu:
            raise ConfigurationRejected(
                f"Requested {allocated_cpu} vCPUs exceeds simulated cluster capacity "
                f"of {self.total_cpu} vCPUs.",
                parameter="cpu",
            )
        if gpu and gpu > self.total_gpu:
            raise ConfigurationRejected(
                f"Requested {gpu} GPUs but the simulated cluster exposes {self.total_gpu}.",
                parameter="gpu",
            )

        resolved_machine_type = validate_machine_type(machine_type, allocated_cpu)
        resolved_provisioning = normalize_provisioning_model(provisioning_model)

        self.check_execution_permission(
            {
                "action": "submit_job",
                "workload_id": name,
                "cpu": allocated_cpu,
                "machine_type": resolved_machine_type,
                "provisioning_model": resolved_provisioning,
            },
            approved_by_operator=approved_by_operator,
        )

        self.workload = _SimWorkload(id=name, allocated_cpu=allocated_cpu, allocated_gpu=gpu)
        self.current_time_minutes = 0.0
        self.machine_type = resolved_machine_type
        self.provisioning_model = resolved_provisioning
        self._accepted_submissions.add(name)

        return {
            # `job_id` is the canonical key across adapters; `id` is retained
            # for backwards compatibility with existing callers.
            "job_id": name,
            "id": name,
            "cpu": allocated_cpu,
            "gpu": gpu,
            "partition": partition,
            "machine_type": resolved_machine_type,
            "provisioning_model": resolved_provisioning,
        }

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
        budget = self.objective.max_cost_eur

        # Dynamic Slack ratio S = (Deadline - CurrentTime) / ETA
        slack_time = max(0.0, self.objective.deadline_at_minutes - self.current_time_minutes)
        slack_ratio = (slack_time / eta) if eta > 0 else 99.0

        if slack_ratio > 1.5:
            pmix = "100% Spot"
        elif slack_ratio > 1.1:
            pmix = "80% Spot / 20% Standard"
        else:
            pmix = "100% Standard"

        cost_factor = compute_hedged_cost_factor(cpu, pmix)

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

        future_cost = cpu * self.cpu_cost_per_hour_eur * cost_factor * (eta / 60.0)
        projected_cost = self.workload.accrued_cost_eur + future_cost

        return CandidateAllocation(
            cpu=cpu,
            estimated_remaining_minutes=round(eta, 3),
            projected_finish_at_minutes=round(finish, 3),
            projected_total_cost_eur=round(projected_cost, 4),
            meets_deadline=finish <= self.objective.deadline_at_minutes,
            within_budget=(budget is None or projected_cost <= budget),
            machine_type=mtype,
            rank=rank,
            provisioning_mix=pmix,
            obtainability_score=obtainability,
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
                status="COMPLETED" if self.is_done() else "RUNNING",
                failed=False,
                machine_type=self.workload.machine_type,
                provisioning_mix=self.workload.provisioning_mix,
                requested_cpu=self.workload.requested_cpu or self.workload.allocated_cpu,
                requested_machine_type=self.workload.requested_machine_type or self.workload.machine_type,
                requested_provisioning_mix=self.workload.requested_provisioning_mix or self.workload.provisioning_mix,
                observed_cpu=self.workload.allocated_cpu,
                observed_machine_type=self.workload.machine_type,
                observed_provisioning_mix=self.workload.provisioning_mix,
                verification_status=self.workload.verification_status,
                verification_detail=self.workload.verification_detail,
                cost_basis=self.workload.cost_basis,
            ),
            objective=self.objective,
            candidate_allocations=candidates,
        )

    def apply(self, action: Action, approved_by_operator: bool = False) -> None:
        self.check_execution_permission(action, approved_by_operator=approved_by_operator)
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

        self.workload.requested_cpu = action.cpu
        self.workload.allocated_cpu = action.cpu
        if action.machine_type:
            self.workload.requested_machine_type = action.machine_type
            self.workload.machine_type = action.machine_type
        if action.provisioning_model:
            self.workload.requested_provisioning_mix = action.provisioning_model
            self.workload.provisioning_mix = action.provisioning_model

        self.workload.verification_status = "verified"
        self.workload.verification_detail = f"Simulated allocation verified ({action.cpu} CPUs, {self.workload.machine_type}, {self.workload.provisioning_mix})"
        self.workload.cost_basis = "observed_allocation"

        self.last_slurm_action = {
            "action": action.action,
            "job_id": self.workload.id,
            "requested_cpu": action.cpu,
            "observed_cpu": self.workload.allocated_cpu,
            "machine_type": self.workload.machine_type,
            "provisioning_mix": self.workload.provisioning_mix,
            "status_code": 200,
            "status": "applied",
            "verification_status": "verified",
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

        # Active provisioning cost factor based on vCPU capacity and rounding
        cost_factor = compute_hedged_cost_factor(self.workload.allocated_cpu, self.workload.provisioning_mix)

        self.workload.remaining_work_units = max(
            0.0,
            self.workload.remaining_work_units - rate * actual_minutes,
        )
        self.workload.accrued_cost_eur += (
            self.workload.allocated_cpu
            * self.cpu_cost_per_hour_eur
            * cost_factor
            * actual_minutes
            / 60.0
        )
        self.current_time_minutes += actual_minutes

    def is_done(self) -> bool:
        return self.workload.remaining_work_units <= 1e-9
