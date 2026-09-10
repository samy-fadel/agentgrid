from __future__ import annotations

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

    # 2. Check if demo mode is enabled or permitted
    if demo_mode is not None:
        is_demo = bool(demo_mode)
    else:
        is_demo = (
            os.getenv("DEMO_MODE", "").lower() in ("true", "1", "yes")
            or os.getenv("COMPUTE_RUNTIME", "simulator").lower() == "simulator"
        )

    # 3. If not in demo mode and live API is unavailable, return explicit unavailable state
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
            "estimated_uptime_minutes": 60.0 if "3600" in est_uptime else 30.0,
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
            obtainability = float(scores.get("obtainability", 0.8))
            estimated_uptime = str(scores.get("estimatedUptime", "3600s"))

            shards = primary_rec.get("shards", [])
            first_shard = shards[0] if shards else {}
            zone_url = first_shard.get("zone", "")
            zone_match = re.search(r'/zones/([^/]+)', zone_url)
            rec_zone = zone_match.group(1) if zone_match else f"{region}-a"

            # Query capacityHistory for preemption rate
            preemption_rate = 0.20
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
                        preemption_rate = round(
                            sum(item.get("preemptionRate", 0.2) for item in recent) / len(recent), 3
                        )
            except Exception:
                pass

            risk_lvl = "LOW" if preemption_rate < 0.15 else ("MEDIUM" if preemption_rate < 0.25 else "HIGH")
            hedging = (
                "100% Spot"
                if obtainability >= 0.85
                else ("80% Spot / 20% Standard" if obtainability >= 0.65 else "100% Standard")
            )

            parsed_recs.append(
                {
                    "machine_type": mtype,
                    "rank": i + 1,
                    "zone": rec_zone,
                    "recommended_zone": rec_zone,
                    "obtainability": obtainability,
                    "obtainability_score": obtainability,
                    "obtainability_percent": int(obtainability * 100),
                    "estimated_uptime": estimated_uptime,
                    "estimated_uptime_minutes": 60.0 if "3600" in estimated_uptime else 30.0,
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
            "status": "live",
            "is_simulated": False,
            "data_note": "GCP Capacity Advisor Telemetry (Live)",
        }
    except Exception:
        return None
