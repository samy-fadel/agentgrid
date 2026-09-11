from __future__ import annotations

import time
from typing import Any

from .models import DiagnosticItem, WorkloadProfile


def _slurm_number(value: Any) -> int | None:
    """Unwrap a slurmrestd scalar.

    slurmrestd does not return bare numbers: it returns
    ``{"number": 64, "set": true, "infinite": false}``. Reading ``["number"]``
    without checking ``set`` is how an unset field becomes a confident zero.
    """
    if isinstance(value, dict):
        if value.get("infinite"):
            return None
        if value.get("set") is False:
            return None
        value = value.get("number")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _slurm_state(value: Any) -> str | None:
    """Normalise ``job_state``, which slurmrestd returns as a list."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _slurm_exit_code(detail: dict[str, Any]) -> int | None:
    """Read the return code from a job record, without inventing a zero."""
    raw = detail.get("exit_code")
    if isinstance(raw, dict) and "return_code" in raw:
        raw = raw["return_code"]
    return _slurm_number(raw)


def _slurm_evidence(detail: dict[str, Any], include_admin_comment: bool = True) -> str:
    """Summarise the parts of a job record that support a diagnosis.

    Only fields actually present are reported: an absent partition must not be
    printed as an empty one. ``include_admin_comment`` is turned off where the
    comment is already the headline fact, so it is not printed twice.
    """
    if not detail:
        return ""

    parts: list[str] = []
    job_id = detail.get("job_id")
    if job_id is not None:
        parts.append(f"job {job_id}")
    if detail.get("partition"):
        parts.append(f"partition '{detail['partition']}'")

    cpus = _slurm_number(detail.get("cpus"))
    if cpus is not None:
        parts.append(f"{cpus} vCPU requested")
    nodes = _slurm_number(detail.get("node_count"))
    if nodes is not None:
        parts.append(f"{nodes} node(s) requested")
    if detail.get("qos"):
        parts.append(f"QoS '{detail['qos']}'")
    if detail.get("dependency"):
        parts.append(f"dependency '{detail['dependency']}'")
    if include_admin_comment and detail.get("admin_comment"):
        parts.append(f"admin_comment: {str(detail['admin_comment'])[:200]}")

    return f" Slurm record: {'; '.join(parts)}." if parts else ""


# Slurm publishes the reason a job is held in ``state_reason``. The QoS and
# association families all begin with one of these prefixes -- ``QOSGrpCpuLimit``,
# ``QOSMaxJobsPerUserLimit``, ``AssocMaxCpuMinsPerJobLimit``,
# ``AssocGrpCPURunMinutes``... The previous code matched the substrings ``qos``
# and ``assocmax``, which both over-matched (any log line mentioning QoS) and
# under-matched (every ``AssocGrp*`` reason fell through to "unknown").
_SCHEDULER_LIMIT_PREFIXES = ("qos", "assocmax", "assocgrp", "assocjob", "assocnode")


def _is_scheduler_limit_reason(state_reason: str | None) -> bool:
    """True when the scheduler is holding the job against an accounting ceiling.

    These ceilings live in slurmdbd and belong to the cluster administrator. A
    cloud quota increase does not affect them, so they must never be reported
    with a cloud source or a cloud remedy.
    """
    if not isinstance(state_reason, str):
        return False
    normalised = state_reason.strip().lower()
    return normalised.startswith(_SCHEDULER_LIMIT_PREFIXES)


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

    # ``slurm_job_details`` used to be accepted and then never read. A caller
    # that passed a complete slurmrestd job record -- state BadConstraints, an
    # admin_comment carrying ZONE_RESOURCE_POOL_EXHAUSTED -- got back
    # "Job state: UNKNOWN, Reason: None". The record is now the source of
    # anything the caller did not spell out, and its evidence is attached to the
    # findings it supports.
    detail = slurm_job_details if isinstance(slurm_job_details, dict) else {}

    job_state = job_state or _slurm_state(detail.get("job_state"))
    state_reason = state_reason or detail.get("state_reason") or None
    if exit_code is None:
        exit_code = _slurm_exit_code(detail)

    # Slurm-GCP writes cloud provider failures from the resume script into
    # admin_comment. Ignoring it is how a stockout came back as "unknown".
    admin_comment = detail.get("admin_comment") or ""

    slurm_evidence = _slurm_evidence(detail)
    # Used where admin_comment is already the headline fact.
    slurm_evidence_terse = _slurm_evidence(detail, include_admin_comment=False)

    # 1. Check for Quota Errors (GCP Provider Error or Slurm Reason)
    err_text = (
        (gcp_error or "")
        + " "
        + (state_reason or "")
        + " "
        + (error_log or "")
        + " "
        + admin_comment
    ).lower()

    # A cloud quota and a cluster accounting limit are two different blockers
    # with two different owners. They used to be merged: any text containing
    # "qos" or "assocmax" produced a finding sourced to ``gcp_compute_quota``
    # advising a GCP quota increase. Nothing a cloud administrator can grant
    # will move a ``QOSMaxJobsPerUserLimit``, which lives in slurmdbd.
    is_gcp_quota = (
        "quota_exceeded" in err_text
        or "quota exceeded" in err_text
        or "ratelimitexceeded" in err_text
        or "cpus_all_regions" in err_text
        or "gpus_all_regions" in err_text
        or (state_reason and "quota" in state_reason.lower())
    )

    # Detected on ``state_reason`` only. Slurm publishes these in a dedicated
    # field; scanning the free-form log for the substring "qos" turned a
    # traceback that merely printed the QoS name into a fabricated limit.
    is_scheduler_limit = _is_scheduler_limit_reason(state_reason)

    if is_scheduler_limit:
        findings.append(
            DiagnosticItem(
                category="quota",
                observed_facts=(
                    f"Slurm accounting limit reached: the scheduler is holding the "
                    f"job under '{state_reason}'. This ceiling is held by the "
                    f"cluster's accounting database (slurmdbd), not by the cloud "
                    f"provider."
                ) + slurm_evidence,
                source="slurm_controller",
                timestamp=ts,
                confirmed=True,
                hypothesis_details=None,
                possible_actions=[
                    {
                        "action": (
                            "Reduce the requested parallelism (vCPU count, node count "
                            "or number of concurrent jobs) to fit inside the current "
                            "QoS/association ceiling"
                        ),
                        "consequences": (
                            "Starts without any administrative request, at lower "
                            "parallelism and therefore longer wall-clock time"
                        ),
                    },
                    {
                        "action": (
                            "Wait for this account's own running jobs to finish and "
                            "release the ceiling"
                        ),
                        "consequences": (
                            "No cost and no configuration change; start time depends "
                            "on the account's current jobs, not on the cluster's "
                            "total free capacity"
                        ),
                    },
                    {
                        "action": (
                            "Ask the cluster administrator to raise the QoS or "
                            "association limit for this account (sacctmgr)"
                        ),
                        "consequences": (
                            "Removes the ceiling for future submissions; requires a "
                            "cluster administrator, not a cloud quota request"
                        ),
                    },
                ],
            )
        )

    if is_gcp_quota:
        findings.append(
            DiagnosticItem(
                category="quota",
                observed_facts=(
                    gcp_error
                    or admin_comment
                    or f"Quota limit reached. Slurm state reason: {state_reason}"
                ) + (slurm_evidence_terse if admin_comment else slurm_evidence),
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

    # Only a *cloud* quota error suppresses the capacity finding: the two are
    # usually the same provisioning failure described twice. A cluster QoS
    # ceiling is an independent blocker and must not hide a real stockout.
    if is_capacity and not is_gcp_quota:
        findings.append(
            DiagnosticItem(
                category="capacity_shortage",
                observed_facts=(
                    gcp_error
                    or admin_comment
                    or f"Cloud capacity unavailable in target zone/region. Reason: {state_reason}"
                ) + (slurm_evidence_terse if admin_comment else slurm_evidence),
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
        facts += slurm_evidence

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
                observed_facts=(
                    gcp_error or f"Job configuration rejected by scheduler: {state_reason}"
                ) + slurm_evidence,
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
    has_dependency = bool(state_reason and state_reason.startswith("Dependency")) or (
        bool(detail.get("dependency"))
        and (job_state or "").upper() in ("PENDING", "", "UNKNOWN")
    )
    if has_dependency:
        findings.append(
            DiagnosticItem(
                category="dependencies",
                observed_facts=(
                    f"Job is waiting on upstream job dependencies. "
                    f"State reason: {state_reason}"
                ) + slurm_evidence,
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
                observed_facts=(
                    f"Job queued behind higher priority workloads. "
                    f"State reason: {state_reason}"
                ) + slurm_evidence,
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
                observed_facts=(
                    f"Job is waiting for available cluster capacity. "
                    f"Slurm state reason: {state_reason or 'Resources'}"
                ) + slurm_evidence,
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
        status_str = (
            f"Job state: {job_state or 'UNKNOWN'}, Reason: {state_reason or 'None'}"
        ) + slurm_evidence
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
