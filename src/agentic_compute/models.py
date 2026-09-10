from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ClusterState(BaseModel):
    current_time_minutes: float = Field(ge=0)
    total_cpu: int = Field(gt=0)
    free_cpu: int = Field(ge=0)
    total_gpu: int = Field(ge=0, default=0)
    free_gpu: int = Field(ge=0, default=0)


class WorkloadState(BaseModel):
    id: str
    kind: str
    remaining_work_units: float = Field(ge=0)
    allocated_cpu: int = Field(gt=0)
    allocated_gpu: int = Field(ge=0, default=0)
    estimated_remaining_minutes: float = Field(ge=0)
    accrued_cost_eur: float = Field(ge=0)
    done: bool = False
    status: str = "RUNNING"
    failed: bool = False
    machine_type: str | None = None
    provisioning_mix: str | None = None
    # Requested vs Observed configuration separation
    requested_cpu: int | None = None
    requested_machine_type: str | None = None
    requested_provisioning_mix: str | None = None
    observed_cpu: int | None = None
    observed_machine_type: str | None = None
    observed_provisioning_mix: str | None = None
    verification_status: Literal[
        "verified", "pending_verification", "unverified", "mismatch", "unsupported", "failed"
    ] = "unverified"
    verification_detail: str | None = None
    cost_basis: str = "unverified"

    @model_validator(mode="after")
    def sync_observed(self) -> "WorkloadState":
        if self.verification_status in ("verified", "mismatch"):
            if self.observed_cpu is None:
                self.observed_cpu = self.allocated_cpu
            if self.observed_machine_type is None and self.machine_type is not None:
                self.observed_machine_type = self.machine_type
            if self.observed_provisioning_mix is None and self.provisioning_mix is not None:
                self.observed_provisioning_mix = self.provisioning_mix
        return self


class Objective(BaseModel):
    deadline_at_minutes: float = Field(gt=0)
    minimize_cost: bool = True
    max_cost_eur: float | None = Field(default=None, gt=0)


class CandidateAllocation(BaseModel):
    cpu: int = Field(gt=0)
    estimated_remaining_minutes: float = Field(ge=0)
    projected_finish_at_minutes: float = Field(ge=0)
    projected_total_cost_eur: float = Field(ge=0)
    meets_deadline: bool
    within_budget: bool
    machine_type: str | None = None
    rank: int = 1
    provisioning_mix: str | None = None
    obtainability_score: float | None = None


class RuntimeSnapshot(BaseModel):
    cluster: ClusterState
    workload: WorkloadState
    objective: Objective
    candidate_allocations: list[CandidateAllocation]


class Action(BaseModel):
    action: Literal["resize_workload", "noop"]
    workload_id: str
    cpu: int | None = Field(default=None, gt=0)
    reason: str = Field(min_length=1, max_length=1000)
    machine_type: str | None = None
    provisioning_model: str | None = None

    @model_validator(mode="after")
    def validate_cpu_for_resize(self) -> "Action":
        if self.action == "resize_workload" and self.cpu is None:
            raise ValueError("cpu is required for resize_workload")
        return self

