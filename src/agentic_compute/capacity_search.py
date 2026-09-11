from __future__ import annotations

import os
from typing import Any, Optional

from .capacity_advisor import (
    GCP_MACHINE_CATALOG,
    check_quota_availability,
    query_capacity_advice,
    search_compatible_capacity as _advisor_search_compatible_capacity,
)
from .models import CapacityCandidate, WorkloadProfile


def check_project_quota(
    region: str,
    required_cpus: int,
    provisioning_model: str = "STANDARD",
) -> tuple[str, Optional[int], Optional[int]]:
    """Inspect project quota availability.

    Returns:
        (quota_status, quota_limit, quota_usage)
        where quota_status in ('QUOTA_AVAILABLE', 'QUOTA_EXCEEDED', 'QUOTA_UNKNOWN').
    """
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "dubai-489009")
    res = check_quota_availability(
        project_id=project_id,
        region=region,
        cpu_needed=required_cpus,
        provisioning_model=provisioning_model,
    )
    return (res["status"], res.get("limit"), res.get("usage"))


def search_compatible_capacity(
    profile: WorkloadProfile | dict[str, Any] | None = None,
    target_region: Optional[str] = None,
    demo_mode: Optional[bool] = None,
    workload_profile: WorkloadProfile | dict[str, Any] | None = None,
    cpu_requested: int | None = None,
    gpu_requested: int | None = None,
    memory_gb_requested: float | None = None,
    allowed_regions: list[str] | None = None,
    allow_spot: bool | None = None,
    allow_standard: bool | None = None,
    **kwargs: Any,
) -> list[CapacityCandidate]:
    """Search compatible capacity candidates matching workload requirements across allowed locations.

    Explicitly differentiates the 4 stages:
    1. catalog_proposed
    2. quota_authorized
    3. capacity_estimated (Capacity Advisor signal)
    4. actually_allocated
    """
    effective_profile = profile or workload_profile
    if effective_profile is None:
        regions = allowed_regions or ([target_region] if target_region else ["us-central1"])
        mem_mb = int((memory_gb_requested or 16.0) * 1024)
        effective_profile = WorkloadProfile(
            workload_id=kwargs.get("workload_id", "workload-search"),
            cpu_requested=cpu_requested or 4,
            gpu_requested=gpu_requested or 0,
            memory_mb_requested=mem_mb,
            allowed_regions=regions,
            allow_spot=True if allow_spot is None else allow_spot,
            allow_fallback_to_standard=True if allow_standard is None else allow_standard,
        )
    elif isinstance(effective_profile, dict):
        if cpu_requested is not None and "cpu_requested" not in effective_profile:
            effective_profile["cpu_requested"] = cpu_requested
        if gpu_requested is not None and "gpu_requested" not in effective_profile:
            effective_profile["gpu_requested"] = gpu_requested
        if memory_gb_requested is not None and "memory_mb_requested" not in effective_profile:
            effective_profile["memory_mb_requested"] = int(memory_gb_requested * 1024)
        if allowed_regions is not None and "allowed_regions" not in effective_profile:
            effective_profile["allowed_regions"] = allowed_regions
        if allow_spot is not None and "allow_spot" not in effective_profile:
            effective_profile["allow_spot"] = allow_spot
        if allow_standard is not None and "allow_fallback_to_standard" not in effective_profile:
            effective_profile["allow_fallback_to_standard"] = allow_standard
        effective_profile = WorkloadProfile(**effective_profile)

    res = _advisor_search_compatible_capacity(
        profile=effective_profile,
        region=target_region,
        demo_mode=demo_mode,
    )
    return [CapacityCandidate(**c) if isinstance(c, dict) else c for c in res]
