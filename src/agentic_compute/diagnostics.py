from __future__ import annotations

import time
from typing import Any

from .models import DiagnosticItem, WorkloadProfile


def diagnose_blockers(
    job_state: str | None = None,
    state_reason: str | None = None,
    exit_code: int | None = None,
    gcp_error: str | None = None,
    error_log: str | None = None,
    workload_profile: WorkloadProfile | dict | None = None,
    slurm_job_details: dict[str, Any] | None = None,
    timestamp: float | None = None,
) -> list[dict[str, Any]]:
    """Analyze Slurm observations and GCP telemetry to diagnose execution blockers.

    Identifies and classifies:
    - resource_waiting (cluster partition capacity saturation, normal queue wait)
    - priority (job queued behind higher priority workloads)
    - dependencies (waiting for prerequisite job IDs)
    - quota (GCP regional vCPU / GPU / IP quota exceeded)
    - capacity_shortage (cloud stockout, preemption spikes, insufficient instance capacity)
    - incompatible_configuration (unsupported machine type, memory > partition, invalid partition)
    - application_error (non-zero exit code, OOM killer 137, script/binary faults)

    Returns structured DiagnosticItems with confirmed facts, origins, timestamps,
    and actionable steps with their consequences.
    """
    ts = timestamp or time.time()
    findings: list[DiagnosticItem] = []

    # 1. Check for Quota Errors (GCP Provider Error or Slurm Reason)
    err_text = ((gcp_error or "") + " " + (state_reason or "") + " " + (error_log or "")).lower()
    is_quota = (
        "quota_exceeded" in err_text
        or "quota exceeded" in err_text
        or "ratelimitexceeded" in err_text
        or "cpus_all_regions" in err_text
        or "gpus_all_regions" in err_text
        or (state_reason and "quota" in state_reason.lower())
        or "qos" in err_text or "qosmax" in err_text or "assocmax" in err_text
    )

    if is_quota:
        findings.append(
            DiagnosticItem(
                category="quota",
                observed_facts=gcp_error or f"Quota limit reached. Slurm state reason: {state_reason}",
                source="gcp_compute_quota",
                timestamp=ts,
                confirmed=True,
                hypothesis_details=None,
                possible_actions=[
                    {
                        "action": "Request GCP quota increase for the target region",
                        "consequences": "Unblocks provisioning once approved without needing to modify workload architecture",
                    },
                    {
                        "action": "Downscale requested vCPU/GPU allocation to fit within existing quota",
                        "consequences": "Allows immediate scheduling within project limits but may increase execution duration",
                    },
                ],
            )
        )

    # 2. Check for Cloud Capacity Shortage / Preemption (Stockout, Preemption, InsufficientInstanceCapacity)
    is_capacity = (
        "zone_resource_pool_exhausted" in err_text
        or "stockout" in err_text
        or "insufficientinstancecapacity" in err_text
        or "preempted" in err_text
        or "preemption" in err_text
        or (state_reason and state_reason in ("NodesSpecError", "NodeDown"))
    )

    if is_capacity and not is_quota:
        findings.append(
            DiagnosticItem(
                category="capacity_shortage",
                observed_facts=gcp_error or f"Cloud capacity unavailable in target zone/region. Reason: {state_reason}",
                source="gcp_capacity_advisor",
                timestamp=ts,
                confirmed=True,
                hypothesis_details=None,
                possible_actions=[
                    {
                        "action": "Failover to alternative compatible machine family (e.g. N2 or C2 fallback)",
                        "consequences": "Taps into separate cloud hardware pool without waiting for current pool recovery",
                    },
                    {
                        "action": "Switch provisioning model from Spot to Standard On-Demand if budget permits",
                        "consequences": "Eliminates preemption risk and accesses higher-priority capacity pool at higher cost",
                    },
                    {
                        "action": "Migrate to alternative approved zone within the same region",
                        "consequences": "Accesses spare capacity in neighboring zone while preserving regional network latency",
                    },
                ],
            )
        )

    # 3. Check for Application Error (Non-zero exit code or FAILED state)
    if (exit_code is not None and exit_code != 0) or (job_state and job_state.upper() == "FAILED"):
        is_oom = exit_code == 137 or "oom" in err_text or "out of memory" in err_text
        facts = f"Workload terminated with exit code {exit_code}. Job state: {job_state}."
        if error_log:
            facts += f" Diagnostic log: {error_log[:300]}"

        actions = [
            {
                "action": "Inspect application logs and fix software defects before retrying. DO NOT blindly re-run.",
                "consequences": "Prevents infinite failure loops and wasted compute spend on deterministic crashes",
            }
        ]
        if is_oom:
            actions.append(
                {
                    "action": "Increase allocated memory (--mem or larger memory machine type)",
                    "consequences": "Prevents Linux OOM-killer (exit 137) from terminating the process",
                }
            )

        findings.append(
            DiagnosticItem(
                category="application_error",
                observed_facts=facts,
                source="workload_log" if error_log else "slurm_controller",
                timestamp=ts,
                confirmed=True,
                hypothesis_details=None,
                possible_actions=actions,
            )
        )

    # 4. Check for Incompatible Configuration
    is_incompatible = (
        "invalidpartition" in err_text
        or "badconstraints" in err_text
        or "invalidfeature" in err_text
        or "unsupported" in err_text
        or "not supported" in err_text
        or "invalid_argument" in err_text
        or "invalid argument" in err_text
        or "invalid machine type" in err_text
        or "incompatible" in err_text
    )

    if is_incompatible:
        findings.append(
            DiagnosticItem(
                category="incompatible_configuration",
                observed_facts=gcp_error or f"Job configuration rejected by scheduler: {state_reason}",
                source="slurm_controller",
                timestamp=ts,
                confirmed=True,
                hypothesis_details=None,
                possible_actions=[
                    {
                        "action": "Select a valid cluster partition and supported machine type (e.g. debug, compute, h3)",
                        "consequences": "Passes Slurm cluster admission checks",
                    }
                ],
            )
        )

    # 5. Check for Dependencies Wait
    if state_reason and state_reason.startswith("Dependency"):
        findings.append(
            DiagnosticItem(
                category="dependencies",
                observed_facts=f"Job is waiting on upstream job dependencies. State reason: {state_reason}",
                source="slurm_controller",
                timestamp=ts,
                confirmed=True,
                hypothesis_details=None,
                possible_actions=[
                    {
                        "action": "Monitor status of upstream dependent jobs",
                        "consequences": "Job will start automatically once upstream jobs complete successfully",
                    }
                ],
            )
        )

    # 6. Check for Priority Queueing
    if state_reason in ("Priority", "JobHeldUser", "JobHeldAdmin"):
        findings.append(
            DiagnosticItem(
                category="priority",
                observed_facts=f"Job queued behind higher priority workloads. State reason: {state_reason}",
                source="slurm_controller",
                timestamp=ts,
                confirmed=True,
                hypothesis_details=None,
                possible_actions=[
                    {
                        "action": "Adjust Slurm nice/priority value if operator has administrative permissions",
                        "consequences": "Elevates queue position without requiring extra cloud VMs",
                    },
                    {
                        "action": "Submit to an alternative under-utilized partition if workload supports it",
                        "consequences": "Bypasses head-of-line queue delay on busy partition",
                    },
                ],
            )
        )

    # 7. Check for Resource Waiting (Standard Cluster Saturation)
    if (
        not findings
        and job_state
        and job_state.upper() == "PENDING"
        and state_reason in ("Resources", "ReqNodeNotAvail", "None", "WaitingForScheduling", None)
    ):
        findings.append(
            DiagnosticItem(
                category="resource_waiting",
                observed_facts=f"Job is waiting for available cluster capacity. Slurm state reason: {state_reason or 'Resources'}",
                source="slurm_controller",
                timestamp=ts,
                confirmed=True,
                hypothesis_details="Cluster nodes currently saturated by active jobs; may clear naturally as running jobs finish",
                possible_actions=[
                    {
                        "action": "Wait for active jobs to complete and release resources naturally",
                        "consequences": "Zero additional cloud infrastructure cost",
                    },
                    {
                        "action": "Trigger cloud autoscaling to add compute nodes to the partition if deadline is near",
                        "consequences": "Reduces queue wait time at the expense of additional cloud VM spend",
                    },
                ],
            )
        )

    # Fallback if no specific condition matched
    if not findings:
        status_str = f"Job state: {job_state or 'UNKNOWN'}, Reason: {state_reason or 'None'}"
        findings.append(
            DiagnosticItem(
                category="unknown",
                observed_facts=status_str,
                source="slurm_controller",
                timestamp=ts,
                confirmed=False,
                hypothesis_details="No active blockers detected; workload is either running or uninitialized",
                possible_actions=[
                    {
                        "action": "Observe runtime snapshot and verify controller connectivity",
                        "consequences": "Ensures telemetry accurately reflects cluster state",
                    }
                ],
            )
        )

    return [item.model_dump() for item in findings]


# Re-export diagnostic helpers for module interchangeability
from .diagnostic import diagnose_gcp_blocker, diagnose_slurm_job
