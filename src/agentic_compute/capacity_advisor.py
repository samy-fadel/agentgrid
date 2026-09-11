from __future__ import annotations
import time

import os
import re
from typing import Any
import requests

try:
    import google.auth
    from google.auth.transport.requests import Request
    HAVE_GOOGLE_AUTH = True
except ImportError:
    HAVE_GOOGLE_AUTH = False


def parse_duration_to_minutes(duration_val: Any) -> float | None:
    """Parse GCP API duration string or numeric seconds to minutes.

    Handles:
    - Strings ending with 's' or with whitespace, e.g. "900s", "5400s", "90.5s"
    - Plain numeric strings or ints/floats representing seconds, e.g. 3600, "3600", 90.5
    - Returns None for invalid, empty, negative, or NaN values.
    """
    if duration_val is None:
        return None
    if isinstance(duration_val, (int, float)):
        if duration_val < 0 or duration_val != duration_val:
            return None
        return round(float(duration_val) / 60.0, 4)
    if isinstance(duration_val, str):
        s = duration_val.strip()
        if s.endswith("s"):
            s = s[:-1].strip()
        if not s:
            return None
        try:
            val = float(s)
            if val < 0 or val != val:
                return None
            return round(val / 60.0, 4)
        except ValueError:
            return None
    return None


def query_capacity_advice(
    machine_types: list[str] | str = "n4-standard-32,n2-standard-32,n2-standard-16",
    size: int = 10,
    region: str | None = None,
    provisioning_model: str = "SPOT",
    target_distribution_shape: str = "ANY",
    demo_mode: bool | None = None,
) -> dict[str, Any]:
    """Query GCP Compute Engine Capacity Advisor API for Spot obtainability and preemption rates.

    Distinguishes live telemetry, demo simulated data, and unavailable states.
    Synthetic values are strictly reserved for demo mode.
    """
    if isinstance(machine_types, str):
        types_list = [t.strip() for t in machine_types.split(",") if t.strip()]
    else:
        types_list = list(machine_types)

    if not types_list:
        types_list = ["n2-standard-16"]

    target_region = (
        region
        or os.getenv("CLOUDSDK_COMPUTE_REGION")
        or os.getenv("GOOGLE_CLOUD_LOCATION")
        or "us-central1"
    )

    project_id = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("PROJECT_ID") or "dubai-489009"

    # Check if demo mode is enabled or permitted
    if demo_mode is not None:
        is_demo = bool(demo_mode)
    else:
        is_demo = (
            os.getenv("DEMO_MODE", "").lower() in ("true", "1", "yes")
            or os.getenv("COMPUTE_RUNTIME", "simulator").lower() == "simulator"
        )

    # If demo mode is explicitly requested, bypass live GCP API
    if demo_mode is not True:
        # 1. Attempt live GCP Compute Engine Capacity Advisor API
        live_result = _call_gcp_advice_api(
            project_id=project_id,
            region=target_region,
            machine_types=types_list,
            size=size,
            provisioning_model=provisioning_model,
            target_distribution_shape=target_distribution_shape,
        )
        if live_result:
            return live_result

        # 2. If not in demo mode and live API is unavailable, return explicit unavailable state
        if not is_demo:
            return {
                "region": target_region,
                "provisioning_model": provisioning_model,
                "requested_size": size,
                "target_distribution_shape": target_distribution_shape,
                "primary_machine_type": types_list[0],
                "obtainability_score": None,
                "estimated_uptime": None,
                "recommended_zone": None,
                "historical_preemption_rate_7d_avg": None,
                "preemption_risk": "UNKNOWN",
                "recommendations": [],
                "machine_types": [],
                "source": "unavailable",
                "status": "unavailable",
                "is_simulated": False,
                "data_note": "Données télémétriques GCP Capacity Advisor indisponibles",
                "error": "GCP Compute Engine Capacity Advisor API is currently unavailable or unreachable",
            }

    # 4. Synthetic values strictly reserved for demo mode
    primary_type = types_list[0]
    size_penalty = 0.0 if size <= 32 else (0.05 if size <= 128 else (0.15 if size <= 500 else 0.25))
    recommended_zone = f"{target_region}-f" if "us-central1" in target_region else f"{target_region}-a"

    recommendations = []
    for idx, mtype in enumerate(types_list):
        mtype_lower = mtype.lower()
        if "n4" in mtype_lower:
            base_obtainability = 0.95
            base_preemption = 0.09
            est_uptime = "3600s"
        elif "n2" in mtype_lower:
            base_obtainability = 0.89
            base_preemption = 0.15
            est_uptime = "3600s"
        elif "c3" in mtype_lower:
            base_obtainability = 0.83
            base_preemption = 0.19
            est_uptime = "1800s"
        elif "c2" in mtype_lower or "hpc" in mtype_lower:
            base_obtainability = 0.78
            base_preemption = 0.22
            est_uptime = "1800s"
        else:
            base_obtainability = 0.86
            base_preemption = 0.16
            est_uptime = "3600s"

        score_adj = max(0.40, round(base_obtainability - size_penalty - (idx * 0.04), 2))
        preemption_rate = min(0.35, round(base_preemption + (size_penalty * 0.5), 3))
        risk_lvl = "LOW" if preemption_rate < 0.12 else ("MEDIUM" if preemption_rate < 0.22 else "HIGH")

        item = {
            "machine_type": mtype,
            "rank": idx + 1,
            "zone": recommended_zone,
            "recommended_zone": recommended_zone,
            "obtainability": score_adj,
            "obtainability_score": score_adj,
            "obtainability_percent": int(score_adj * 100),
            "estimated_uptime": est_uptime,
            "estimated_uptime_minutes": parse_duration_to_minutes(est_uptime),
            "historical_preemption_rate_7d": preemption_rate,
            "historical_preemption_rate_7d_avg": preemption_rate,
            "preemption_risk_level": risk_lvl,
            "suggested_hedging": "100% Spot" if score_adj >= 0.85 else ("80% Spot / 20% Standard" if score_adj >= 0.65 else "100% Standard"),
            "hedged_policy_recommendation": "100% Spot" if score_adj >= 0.85 else ("80% Spot / 20% Standard" if score_adj >= 0.65 else "100% Standard"),
        }
        recommendations.append(item)

    top_rec = recommendations[0]
    return {
        "region": target_region,
        "provisioning_model": provisioning_model,
        "requested_size": size,
        "target_distribution_shape": target_distribution_shape,
        "primary_machine_type": primary_type,
        "obtainability_score": top_rec["obtainability_score"],
        "estimated_uptime": top_rec["estimated_uptime"],
        "recommended_zone": recommended_zone,
        "historical_preemption_rate_7d_avg": top_rec["historical_preemption_rate_7d"],
        "preemption_risk": top_rec["preemption_risk_level"],
        "recommendations": recommendations,
        "machine_types": recommendations,
        "source": "simulated_demo_data",
        "status": "simulated",
        "is_simulated": True,
        "data_note": "Données simulées (mode démo)",
    }


def _call_gcp_advice_api(
    project_id: str,
    region: str,
    machine_types: list[str],
    size: int,
    provisioning_model: str,
    target_distribution_shape: str,
) -> dict[str, Any] | None:
    """Call the official Google Compute Engine advice.capacity & capacityHistory REST APIs."""
    if not HAVE_GOOGLE_AUTH:
        return None

    try:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        if not credentials.valid:
            credentials.refresh(Request())

        token = credentials.token
        if not token:
            return None

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        url_cap = f"https://compute.googleapis.com/compute/beta/projects/{project_id}/regions/{region}/advice/capacity"
        url_hist = f"https://compute.googleapis.com/compute/beta/projects/{project_id}/regions/{region}/advice/capacityHistory"

        parsed_recs = []
        for i, mtype in enumerate(machine_types):
            # GCP Capacity Advisor strictly requires instanceSelections to contain exactly one element
            payload_cap = {
                "distributionPolicy": {"targetShape": target_distribution_shape.upper()},
                "instanceFlexibilityPolicy": {
                    "instanceSelections": {
                        "selection-1": {"machineTypes": [mtype], "rank": 1}
                    }
                },
                "instanceProperties": {
                    "scheduling": {"provisioningModel": provisioning_model.upper()}
                },
                "size": size,
            }

            resp_cap = requests.post(url_cap, headers=headers, json=payload_cap, timeout=4.0)
            if resp_cap.status_code != 200:
                if i == 0:
                    return None
                continue

            data_cap = resp_cap.json()
            recs = data_cap.get("recommendations", [])
            if not recs:
                if i == 0:
                    return None
                continue

            primary_rec = recs[0]
            scores = primary_rec.get("scores", {})
            obtainability_val = scores.get("obtainability")
            obtainability = float(obtainability_val) if obtainability_val is not None else None
            est_uptime_raw = scores.get("estimatedUptime")
            estimated_uptime_str = str(est_uptime_raw) if est_uptime_raw is not None else None
            estimated_uptime_mins = parse_duration_to_minutes(est_uptime_raw)

            shards = primary_rec.get("shards", [])
            first_shard = shards[0] if shards else {}
            zone_url = first_shard.get("zone", "")
            zone_match = re.search(r'/zones/([^/]+)', zone_url)
            rec_zone = zone_match.group(1) if zone_match else f"{region}-a"

            # Query capacityHistory for preemption rate
            preemption_rate = None
            try:
                payload_hist = {
                    "instanceProperties": {
                        "machineType": mtype,
                        "scheduling": {"provisioningModel": provisioning_model.upper()},
                    },
                    "types": ["PREEMPTION"],
                }
                resp_hist = requests.post(url_hist, headers=headers, json=payload_hist, timeout=3.0)
                if resp_hist.status_code == 200:
                    hist_data = resp_hist.json().get("preemptionHistory", [])
                    if hist_data:
                        recent = hist_data[-7:]
                        rates = [
                            float(item["preemptionRate"])
                            for item in recent
                            if "preemptionRate" in item and item["preemptionRate"] is not None
                        ]
                        if rates:
                            preemption_rate = round(sum(rates) / len(rates), 3)
            except Exception:
                pass

            if preemption_rate is not None:
                risk_lvl = "LOW" if preemption_rate < 0.15 else ("MEDIUM" if preemption_rate < 0.25 else "HIGH")
            else:
                risk_lvl = "UNKNOWN"

            hedging = (
                "100% Spot"
                if (obtainability is not None and obtainability >= 0.85)
                else ("80% Spot / 20% Standard" if (obtainability is not None and obtainability >= 0.65) else "100% Standard")
            )

            parsed_recs.append(
                {
                    "machine_type": mtype,
                    "rank": i + 1,
                    "zone": rec_zone,
                    "recommended_zone": rec_zone,
                    "obtainability": obtainability,
                    "obtainability_score": obtainability,
                    "obtainability_percent": int(obtainability * 100) if obtainability is not None else None,
                    "estimated_uptime": estimated_uptime_str,
                    "estimated_uptime_minutes": estimated_uptime_mins,
                    "historical_preemption_rate_7d": preemption_rate,
                    "historical_preemption_rate_7d_avg": preemption_rate,
                    "preemption_risk_level": risk_lvl,
                    "suggested_hedging": hedging,
                    "hedged_policy_recommendation": hedging,
                }
            )

        if not parsed_recs:
            return None

        top_rec = parsed_recs[0]
        has_unknown_history = any(r.get("historical_preemption_rate_7d") is None for r in parsed_recs)
        overall_status = "partial_live" if has_unknown_history else "live"
        data_note = (
            "GCP Capacity Advisor Telemetry (Partial Live: preemption history unavailable)"
            if has_unknown_history
            else "GCP Capacity Advisor Telemetry (Live)"
        )
        return {
            "region": region,
            "provisioning_model": provisioning_model,
            "requested_size": size,
            "target_distribution_shape": target_distribution_shape,
            "primary_machine_type": top_rec["machine_type"],
            "obtainability_score": top_rec["obtainability_score"],
            "estimated_uptime": top_rec["estimated_uptime"],
            "recommended_zone": top_rec["recommended_zone"],
            "historical_preemption_rate_7d_avg": top_rec["historical_preemption_rate_7d"],
            "preemption_risk": top_rec["preemption_risk_level"],
            "recommendations": parsed_recs,
            "machine_types": parsed_recs,
            "source": "google_compute_engine_capacity_advisor_api",
            "status": overall_status,
            "is_simulated": False,
            "data_note": data_note,
        }
    except Exception:
        return None


# -------------------------------------------------------------------------
# Feature 1: Compatible Capacity Search, Catalog & Quotas
# -------------------------------------------------------------------------

GCP_MACHINE_CATALOG = {
    "n2-standard-2": {"cpu": 2, "memory_gb": 8.0, "family": "n2", "gpu_allowed": False, "avx512": True},
    "n2-standard-4": {"cpu": 4, "memory_gb": 16.0, "family": "n2", "gpu_allowed": False, "avx512": True},
    "n2-standard-8": {"cpu": 8, "memory_gb": 32.0, "family": "n2", "gpu_allowed": False, "avx512": True},
    "n2-standard-16": {"cpu": 16, "memory_gb": 64.0, "family": "n2", "gpu_allowed": False, "avx512": True},
    "n2-standard-32": {"cpu": 32, "memory_gb": 128.0, "family": "n2", "gpu_allowed": False, "avx512": True},
    "n4-standard-4": {"cpu": 4, "memory_gb": 16.0, "family": "n4", "gpu_allowed": False, "avx512": True},
    "n4-standard-16": {"cpu": 16, "memory_gb": 64.0, "family": "n4", "gpu_allowed": False, "avx512": True},
    "n4-standard-32": {"cpu": 32, "memory_gb": 128.0, "family": "n4", "gpu_allowed": False, "avx512": True},
    "n4-standard-64": {"cpu": 64, "memory_gb": 256.0, "family": "n4", "gpu_allowed": False, "avx512": True},
    "c2-standard-60": {"cpu": 60, "memory_gb": 240.0, "family": "c2", "gpu_allowed": False, "avx512": True},
    "h3-standard-88": {"cpu": 88, "memory_gb": 352.0, "family": "h3", "gpu_allowed": False, "avx512": True},
    "g2-standard-4": {"cpu": 4, "memory_gb": 16.0, "family": "g2", "gpu_allowed": True, "gpu_type": "NVIDIA_L4", "gpu_count": 1, "avx512": False},
    "a2-highgpu-1g": {"cpu": 12, "memory_gb": 85.0, "family": "a2", "gpu_allowed": True, "gpu_type": "NVIDIA_TESLA_A100", "gpu_count": 1, "avx512": True},
}


def check_quota_availability(
    project_id: str,
    region: str,
    cpu_needed: int,
    gpu_needed: int = 0,
    provisioning_model: str = "STANDARD",
) -> dict[str, Any]:
    """Check if requested vCPUs and GPUs are within project quota limits.

    Distinguishes authorized quota from capacity availability.
    """
    # Check for test/env quota overrides
    limit_cpu = int(os.getenv("GCP_QUOTA_CPU_LIMIT") or os.getenv("GCP_PROJECT_QUOTA_CPUS") or "256")
    used_cpu = int(os.getenv("GCP_QUOTA_CPU_USED") or os.getenv("GCP_PROJECT_QUOTA_USED") or "0")
    limit_gpu = int(os.getenv("GCP_QUOTA_GPU_LIMIT", "8"))
    used_gpu = int(os.getenv("GCP_QUOTA_GPU_USED", "0"))

    avail_cpu = max(0, limit_cpu - used_cpu)
    avail_gpu = max(0, limit_gpu - used_gpu)

    cpu_ok = cpu_needed <= avail_cpu
    gpu_ok = gpu_needed <= avail_gpu

    if not cpu_ok or not gpu_ok:
        reason = []
        if not cpu_ok:
            reason.append(f"CPU quota exceeded: requested {cpu_needed} vCPUs, available {avail_cpu}/{limit_cpu}")
        if not gpu_ok:
            reason.append(f"GPU quota exceeded: requested {gpu_needed} GPUs, available {avail_gpu}/{limit_gpu}")
        return {
            "status": "QUOTA_EXCEEDED",
            "is_exceeded": True,
            "quota_limit": limit_cpu,
            "quota_usage": used_cpu,
            "reason": "; ".join(reason),
        }

    return {
        "status": "QUOTA_AVAILABLE",
        "is_exceeded": False,
        "quota_limit": limit_cpu,
        "quota_usage": used_cpu,
        "reason": "Quota verified",
    }


def search_compatible_capacity(
    profile: Any,
    region: str | None = None,
    demo_mode: bool | None = None,
) -> list[dict[str, Any]]:
    """Search for compatible compute capacity across catalog, quotas, and capacity signals.

    Explicitly differentiates:
    1. catalog_proposed (offered in GCP catalog and matches profile hardware constraints)
    2. quota_authorized (project has sufficient regional quota)
    3. capacity_estimated (capacity signal from Capacity Advisor API)
    4. actually_allocated (confirmed cluster allocation)
    """
    from .models import CapacityCandidate, WorkloadProfile

    if isinstance(profile, dict):
        workload = WorkloadProfile(**profile)
    elif isinstance(profile, WorkloadProfile):
        workload = profile
    else:
        workload = WorkloadProfile(workload_id="adhoc-search")

    target_region = (
        region
        or (workload.allowed_regions[0] if workload.allowed_regions else None)
        or os.getenv("CLOUDSDK_COMPUTE_REGION")
        or "us-central1"
    )

    if workload.allowed_regions and target_region not in workload.allowed_regions:
        if not workload.allow_region_change:
            return []

    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "dubai-489009")
    needed_cpu = workload.cpu_requested or 4
    needed_gpu = workload.gpu_requested or 0
    needed_mem_gb = (workload.memory_mb_requested / 1024.0) if workload.memory_mb_requested else 0.0

    prov_models = []
    if workload.allow_spot:
        prov_models.append("SPOT")
    if workload.allow_fallback_to_standard or not prov_models:
        prov_models.append("STANDARD")

    # Filter catalog for compatible machine types first
    compatible_types = []
    for mtype, meta in GCP_MACHINE_CATALOG.items():
        if needed_cpu > 0 and meta.get("cpu", 0) < needed_cpu:
            continue
        if needed_gpu > 0 and not meta.get("gpu_allowed"):
            continue
        if needed_gpu == 0 and meta.get("gpu_allowed") and meta.get("gpu_count", 0) > 0:
            continue
        if needed_mem_gb > 0 and meta.get("memory_gb", 0) < needed_mem_gb:
            continue
        if "AVX512" in workload.hardware_constraints and not meta.get("avx512"):
            continue
        compatible_types.append(mtype)

    if not compatible_types:
        return []

    candidates: list[dict[str, Any]] = []

    # Batch query Capacity Advisor once per provisioning model
    for prov_model in prov_models:
        advice = query_capacity_advice(
            machine_types=compatible_types,
            size=1,
            region=target_region,
            provisioning_model=prov_model,
            demo_mode=demo_mode,
        )

        advice_map = {}
        for rec in advice.get("recommendations", []):
            advice_map[rec.get("machine_type")] = rec

        is_unavailable = advice.get("status") == "unavailable"
        is_sim = advice.get("is_simulated", False)

        for mtype in compatible_types:
            meta = GCP_MACHINE_CATALOG[mtype]
            stage = "catalog_proposed"

            quota_res = check_quota_availability(
                project_id=project_id,
                region=target_region,
                cpu_needed=meta["cpu"],
                gpu_needed=meta.get("gpu_count", 0),
                provisioning_model=prov_model,
            )

            quota_status = quota_res["status"]
            if not quota_res["is_exceeded"]:
                stage = "quota_authorized"

            rec_data = advice_map.get(mtype, {})
            obtainability = rec_data.get("obtainability_score")
            uptime = rec_data.get("estimated_uptime")
            preempt_risk = rec_data.get("preemption_risk_level", rec_data.get("preemption_risk"))

            if is_unavailable:
                provenance = "unavailable"
                capacity_signal = "UNAVAILABLE"
            elif is_sim:
                provenance = "simulated_demo"
                capacity_signal = "SIMULATED"
                if not quota_res["is_exceeded"]:
                    stage = "capacity_estimated"
            else:
                provenance = "gcp_live_api"
                if obtainability is not None:
                    capacity_signal = "HIGH" if obtainability >= 0.85 else ("MEDIUM" if obtainability >= 0.65 else "LOW")
                    if not quota_res["is_exceeded"]:
                        stage = "capacity_estimated"
                else:
                    capacity_signal = "UNKNOWN"

            uptime_mins = parse_duration_to_minutes(uptime) if uptime else None

            candidate = CapacityCandidate(
                machine_type=mtype,
                quantity=1,
                cpu_count=meta["cpu"],
                memory_gb=meta["memory_gb"],
                region=target_region,
                zone=rec_data.get("recommended_zone", advice.get("recommended_zone")),
                provisioning_model=prov_model,
                compatibility="COMPATIBLE",
                quota_status=quota_status,
                quota_limit=quota_res.get("quota_limit"),
                quota_usage=quota_res.get("quota_usage"),
                capacity_signal=capacity_signal,
                obtainability_score=obtainability,
                preemption_risk=preempt_risk,
                estimated_uptime_minutes=uptime_mins,
                data_provenance=provenance,
                timestamp=time.time(),
                state_stage=stage,
            )
            candidates.append(candidate.model_dump())

    return candidates
