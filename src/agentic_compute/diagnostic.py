from __future__ import annotations

import re
import time
from typing import Any, Literal

from .models import DiagnosticItem


def diagnose_slurm_job(
    job_id: str | None,
    state: str,
    reason: str | None = None,
    exit_code: int | None = None,
    log_snippet: str | None = None,
    admin_comment: str | None = None,
) -> list[DiagnosticItem]:
    """Diagnose Slurm job state, state_reason, exit code, and logs into categorized DiagnosticItems."""
    diagnostics: list[DiagnosticItem] = []
    now = time.time()
    reason_str = (reason or "").strip()
    state_str = (state or "").upper().strip()

    # 1. Exit code analysis (Application Error / OOM)
    if exit_code is not None and exit_code != 0:
        if exit_code == 137:
            diagnostics.append(
                DiagnosticItem(
                    category="application_error",
                    observed_facts=f"Job exited with code 137 (SIGKILL - commonly Out Of Memory)",
                    source="slurm_controller",
                    timestamp=now,
                    confirmed=True,
                    hypothesis_details="Workload memory usage exceeded partition or node memory limit",
                    possible_actions=[
                        {
                            "action": "Increase memory_mb request or choose higher memory machine type (e.g., highmem)",
                            "consequences": "Allocates more memory per core; may slightly increase hourly instance cost",
                        },
                        {
                            "action": "Enable batch checkpointing to avoid complete data loss",
                            "consequences": "Enables resuming from last valid checkpoint rather than restarting from 0%",
                        },
                    ],
                )
            )
        elif exit_code == 139:
            diagnostics.append(
                DiagnosticItem(
                    category="application_error",
                    observed_facts="Job exited with code 139 (SIGSEGV - Segmentation Fault)",
                    source="slurm_controller",
                    timestamp=now,
                    confirmed=True,
                    hypothesis_details="Low-level memory access violation or corrupted binary/library",
                    possible_actions=[
                        {
                            "action": "Inspect stack trace and binary dependencies in container/environment",
                            "consequences": "Requires developer debugging before rescheduling",
                        }
                    ],
                )
            )
        else:
            diagnostics.append(
                DiagnosticItem(
                    category="application_error",
                    observed_facts=f"Job failed with non-zero exit status {exit_code}",
                    source="slurm_controller",
                    timestamp=now,
                    confirmed=True,
                    hypothesis_details="Workload application or script returned non-zero exit code",
                    possible_actions=[
                        {
                            "action": "Check stderr output logs for exception details",
                            "consequences": "Identify root-cause bug or missing input file",
                        }
                    ],
                )
            )

    # 2. Slurm Reason analysis
    if reason_str:
        norm_reason = reason_str.lower()
        if "badconstraints" in norm_reason or "partitionconfig" in norm_reason or "nodeconfig" in norm_reason:
            diagnostics.append(
                DiagnosticItem(
                    category="incompatible_configuration",
                    observed_facts=f"Slurm rejected placement: reason={reason_str}",
                    source="slurm_controller",
                    timestamp=now,
                    confirmed=True,
                    hypothesis_details="Requested machine type, feature constraints, or partition flags do not match any available nodes",
                    possible_actions=[
                        {
                            "action": "Change requested machine_type to a supported cluster type (e.g. n2-standard-2, c2-standard-60, h3-standard-88)",
                            "consequences": "Allows Slurm scheduler to find compatible partition",
                        },
                        {
                            "action": "Remove conflicting feature tags or unconfigured gres/gpu requirements",
                            "consequences": "Bypasses strict constraint filters",
                        },
                    ],
                )
            )
        elif "qosmaxcpu" in norm_reason or "qosmaxjob" in norm_reason or "assocmax" in norm_reason:
            diagnostics.append(
                DiagnosticItem(
                    category="quota",
                    observed_facts=f"Slurm QOS/User quota exceeded: reason={reason_str}",
                    source="slurm_controller",
                    timestamp=now,
                    confirmed=True,
                    hypothesis_details="Operator or tenant has reached maximum concurrent cores or active jobs",
                    possible_actions=[
                        {
                            "action": "Reduce parallel CPU count to fit within active QOS limit",
                            "consequences": "Workload will run with less parallelism but starts immediately",
                        },
                        {
                            "action": "Wait for currently running jobs of this user to finish",
                            "consequences": "Delays execution until existing workloads release quota",
                        },
                    ],
                )
            )
        elif "priority" in norm_reason:
            diagnostics.append(
                DiagnosticItem(
                    category="priority",
                    observed_facts=f"Job is queued behind higher-priority workloads: reason={reason_str}",
                    source="slurm_controller",
                    timestamp=now,
                    confirmed=True,
                    hypothesis_details="Partition scheduler is serving higher-priority or earlier-submitted jobs first",
                    possible_actions=[
                        {
                            "action": "Wait in queue until higher-priority workloads yield resources",
                            "consequences": "Increases wait time; will run when queue drains",
                        },
                        {
                            "action": "Switch to a less congested partition or alternative zone",
                            "consequences": "May avoid queue wait if secondary partition has idle nodes",
                        },
                    ],
                )
            )
        elif "dependency" in norm_reason or "jobhold" in norm_reason:
            diagnostics.append(
                DiagnosticItem(
                    category="dependencies",
                    observed_facts=f"Job is blocked by workflow dependency or hold: reason={reason_str}",
                    source="slurm_controller",
                    timestamp=now,
                    confirmed=True,
                    hypothesis_details="Dependent prerequisite jobs have not yet completed or job was placed on user/admin hold",
                    possible_actions=[
                        {
                            "action": "Release hold via scontrol release or wait for parent jobs",
                            "consequences": "Allows scheduler to consider job once dependencies satisfy",
                        }
                    ],
                )
            )
        elif "reqnodenotavail" in norm_reason or "resources" in norm_reason:
            # Check if this might be capacity shortage or waiting for node spin-up
            is_node_down = "down" in norm_reason or "drained" in norm_reason
            cat = "capacity_shortage" if is_node_down else "resource_waiting"
            diagnostics.append(
                DiagnosticItem(
                    category=cat,
                    observed_facts=f"Nodes currently unavailable for requested allocation: reason={reason_str}",
                    source="slurm_controller",
                    timestamp=now,
                    confirmed=True,
                    hypothesis_details="Dynamic nodeset is currently scaling up or all partition nodes are busy",
                    possible_actions=[
                        {
                            "action": "Wait for elastic compute autoscaler to power up dynamic GCP nodes (typically 2-4 mins)",
                            "consequences": "Normal cloud cluster spin-up latency",
                        },
                        {
                            "action": "Scale down requested CPU count to fit available idle node capacity immediately",
                            "consequences": "Starts immediately on existing powered-up instances",
                        },
                    ],
                )
            )

    # 3. Log snippet analysis (heuristics / hypothesis)
    if log_snippet:
        snippet_lower = log_snippet.lower()
        if "out of memory" in snippet_lower or "oom-killer" in snippet_lower or "cuda out of memory" in snippet_lower:
            # Check if already caught by exit code 137
            if not any(d.category == "application_error" and "137" in d.observed_facts for d in diagnostics):
                diagnostics.append(
                    DiagnosticItem(
                        category="application_error",
                        observed_facts="Workload log contains Out Of Memory (OOM) error",
                        source="workload_log",
                        timestamp=now,
                        confirmed=False,
                        hypothesis_details="Log pattern indicates memory starvation during execution",
                        possible_actions=[
                            {
                                "action": "Increase memory_mb or reduce batch size in computation",
                                "consequences": "Prevents kernel OOM-killer termination",
                            }
                        ],
                    )
                )
        if "zone_resource_pool_exhausted" in snippet_lower or "stockout" in snippet_lower:
            diagnostics.append(
                DiagnosticItem(
                    category="capacity_shortage",
                    observed_facts="Workload log indicates GCP zone resource pool exhaustion",
                    source="gcp_compute_api",
                    timestamp=now,
                    confirmed=False,
                    hypothesis_details="Underlying hypervisor reported temporary VM stockout in target zone",
                    possible_actions=[
                        {
                            "action": "Failover to alternative zone or standard on-demand instance",
                            "consequences": "Bypasses localized stockout",
                        }
                    ],
                )
            )

    # 4. Fallback if job is in PENDING / FAILED state but no specific items were produced
    if not diagnostics:
        if state_str in ("PENDING", "CONFIGURING"):
            diagnostics.append(
                DiagnosticItem(
                    category="resource_waiting",
                    observed_facts=f"Job {job_id or 'active'} is in {state_str} state awaiting node provisioning",
                    source="slurm_controller",
                    timestamp=now,
                    confirmed=True,
                    hypothesis_details="Standard queue scheduling or cloud autoscaling node spin-up",
                    possible_actions=[
                        {
                            "action": "Monitor cluster autoscaler until nodes enter ready state",
                            "consequences": "Autonomous transition to RUNNING",
                        }
                    ],
                )
            )
        elif state_str in ("FAILED", "NODE_FAIL", "TIMEOUT"):
            diagnostics.append(
                DiagnosticItem(
                    category="application_error" if state_str == "FAILED" else "capacity_shortage",
                    observed_facts=f"Job {job_id or 'active'} entered terminal error state: {state_str}",
                    source="slurm_controller",
                    timestamp=now,
                    confirmed=True,
                    hypothesis_details=f"Slurm reported terminal state {state_str}",
                    possible_actions=[
                        {
                            "action": "Resubmit with fallback configuration or inspect stderr logs",
                            "consequences": "Retry execution with adjusted parameters",
                        }
                    ],
                )
            )

    return diagnostics


def diagnose_gcp_blocker(
    error_message: str,
    region: str | None = None,
    zone: str | None = None,
    machine_type: str | None = None,
) -> DiagnosticItem:
    """Classify a GCP Cloud error message (e.g. quota, zone exhaustion, invalid machine type)."""
    now = time.time()
    msg = error_message or ""
    msg_upper = msg.upper()

    if "QUOTA_EXCEEDED" in msg_upper or "QUOTA" in msg_upper:
        return DiagnosticItem(
            category="quota",
            observed_facts=f"GCP Quota Exceeded: {msg}",
            source="gcp_compute_api",
            timestamp=now,
            confirmed=True,
            hypothesis_details=f"Project quota limit reached for cores/instances in {region or 'region'}",
            possible_actions=[
                {
                    "action": f"Request quota increase via Cloud Console for {region or 'target region'}",
                    "consequences": "Increases quota ceiling (requires approval)",
                },
                {
                    "action": "Switch to alternative region or downscale concurrent instance count",
                    "consequences": "Operates within currently available quota limits",
                },
            ],
        )

    if "ZONE_RESOURCE_POOL_EXHAUSTED" in msg_upper or "STOCKOUT" in msg_upper or "RESOURCE_AVAILABILITY" in msg_upper:
        return DiagnosticItem(
            category="capacity_shortage",
            observed_facts=f"GCP Stockout / Resource Pool Exhausted: {msg}",
            source="gcp_compute_api",
            timestamp=now,
            confirmed=True,
            hypothesis_details=f"Insufficient physical capacity for {machine_type or 'VM'} in {zone or region or 'zone'}",
            possible_actions=[
                {
                    "action": "Failover to secondary zone within region or switch to Standard provisioning",
                    "consequences": "Standard instances take priority over Spot; alternative zones may have idle hardware",
                },
                {
                    "action": "Switch to an alternative machine family (e.g., C2 or H3 instead of N2)",
                    "consequences": "Draws from different hardware rack pool",
                },
            ],
        )

    if "UNSUPPORTED" in msg_upper or "INVALID_ARGUMENT" in msg_upper or "NOT_FOUND" in msg_upper:
        return DiagnosticItem(
            category="incompatible_configuration",
            observed_facts=f"GCP Incompatible Configuration: {msg}",
            source="gcp_compute_api",
            timestamp=now,
            confirmed=True,
            hypothesis_details=f"Machine type {machine_type or ''} or configuration is not offered in {zone or region or 'zone'}",
            possible_actions=[
                {
                    "action": "Select machine type available in this zone's catalog",
                    "consequences": "Satisfies hardware topology requirements",
                }
            ],
        )

    return DiagnosticItem(
        category="unknown",
        observed_facts=f"Unclassified compute error: {msg}",
        source="gcp_compute_api",
        timestamp=now,
        confirmed=False,
        hypothesis_details="Error format did not match known quota or stockout patterns",
        possible_actions=[
            {
                "action": "Review detailed API error logs and cluster operator console",
                "consequences": "Manual operator intervention",
            }
        ],
    )

# Re-export diagnose_blockers from diagnostics
from .diagnostics import diagnose_blockers
