from __future__ import annotations

import json
import os
import time
from typing import Any

from .models import ExecutionHistoryRecord, WorkloadProfile


DEFAULT_HISTORY_FILE = os.getenv(
    "AGENTGRID_HISTORY_FILE",
    os.path.join(os.path.expanduser("~/.agentgrid"), "history.json"),
)


class HistoryStore:
    """Persistent file-based store preserving execution history across application restarts."""

    def __init__(self, filepath: str | None = None) -> None:
        self.filepath = filepath or DEFAULT_HISTORY_FILE
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        parent_dir = os.path.dirname(self.filepath)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)
        if not os.path.exists(self.filepath):
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump({"records": []}, f, indent=2)

    def _load_all(self) -> list[dict[str, Any]]:
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("records", [])
        except Exception:
            return []

    def _save_all(self, records: list[dict[str, Any]]) -> None:
        self._ensure_storage()
        temp_file = f"{self.filepath}.tmp.{os.getpid()}"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump({"records": records}, f, indent=2)
        os.replace(temp_file, self.filepath)

    def save_record(self, record: ExecutionHistoryRecord | dict[str, Any]) -> dict[str, Any]:
        rec_dict = record.model_dump() if isinstance(record, ExecutionHistoryRecord) else dict(record)
        rec_dict["updated_at"] = time.time()

        # Compute comparison deltas
        est_cost = rec_dict.get("initial_estimated_cost_eur", 0.0)
        act_cost = rec_dict.get("final_calculated_cost_eur", 0.0)
        rec_dict["cost_comparison_delta_eur"] = round(act_cost - est_cost, 3)

        est_dur = rec_dict.get("initial_estimated_duration_minutes", 0.0)
        act_dur = rec_dict.get("final_actual_duration_minutes", 0.0)
        rec_dict["duration_comparison_delta_minutes"] = round(act_dur - est_dur, 2)

        records = self._load_all()
        # Update existing record or append
        updated = False
        for idx, r in enumerate(records):
            if r.get("workload_id") == rec_dict.get("workload_id"):
                records[idx] = rec_dict
                updated = True
                break
        if not updated:
            records.append(rec_dict)

        self._save_all(records)
        return rec_dict

    def get_record(self, workload_id: str) -> dict[str, Any] | None:
        records = self._load_all()
        for r in records:
            if r.get("workload_id") == workload_id:
                return r
        return None

    def list_records(self, limit: int = 50) -> list[dict[str, Any]]:
        records = self._load_all()
        return sorted(records, key=lambda x: x.get("updated_at", 0), reverse=True)[:limit]

    def compare_estimated_vs_observed(self, workload_id: str) -> dict[str, Any]:
        rec = self.get_record(workload_id)
        if not rec:
            return {"error": f"Workload {workload_id} not found in history"}

        est_cost = rec.get("initial_estimated_cost_eur", 0.0)
        calc_cost = rec.get("final_calculated_cost_eur", 0.0)
        est_dur = rec.get("initial_estimated_duration_minutes", 0.0)
        act_dur = rec.get("final_actual_duration_minutes", 0.0)
        attempts = rec.get("attempts", [])

        # Sum elapsed wait and recovery from attempts
        total_wait = sum(att.get("events", [{}])[0].get("wait_time", 0.0) for att in attempts if att.get("events"))

        return {
            "workload_id": workload_id,
            "status": rec.get("final_status", "UNKNOWN"),
            "cost": {
                "initial_estimated_eur": est_cost,
                "calculated_observed_usage_eur": calc_cost,
                "reconciled_billed_eur": None,  # Explicitly None unless external billing API is integrated
                "variance_eur": round(calc_cost - est_cost, 3),
                "variance_pct": round(((calc_cost - est_cost) / est_cost * 100), 1) if est_cost > 0 else 0.0,
                "status": rec.get("reconciliation_status", "calculated_from_usage"),
                "notice": "Coût calculé sur usage observé et tarif connu. Non facturé réconcilié.",
            },
            "duration": {
                "estimated_minutes": est_dur,
                "actual_minutes": act_dur,
                "variance_minutes": round(act_dur - est_dur, 2),
                "variance_pct": round(((act_dur - est_dur) / est_dur * 100), 1) if est_dur > 0 else 0.0,
            },
            "attempts_count": len(attempts),
            "retries_count": max(0, len(attempts) - 1),
        }

    def get_comparable_metrics(
        self,
        workload_name: str | None = None,
        machine_type: str | None = None,
    ) -> dict[str, Any]:
        """Extract aggregate duration, cost, and throughput metrics from comparable historical runs."""
        records = self._load_all()
        matching = []
        for r in records:
            if r.get("final_status") != "COMPLETED":
                continue
            if workload_name and r.get("workload_name") == workload_name:
                matching.append(r)
            elif machine_type:
                for att in r.get("attempts", []):
                    if att.get("allocated_machine_type") == machine_type:
                        matching.append(r)
                        break

        if not matching:
            return {
                "sample_size": 0,
                "provenance": "no_historical_runs",
                "average_duration_minutes": None,
                "average_cost_eur": None,
                "message": "No comparable historical runs found for calibration",
            }

        avg_dur = sum(m.get("final_actual_duration_minutes", 0.0) for m in matching) / len(matching)
        avg_cost = sum(m.get("final_calculated_cost_eur", 0.0) for m in matching) / len(matching)

        return {
            "sample_size": len(matching),
            "provenance": f"local_execution_history_{self.filepath}",
            "average_duration_minutes": round(avg_dur, 2),
            "average_cost_eur": round(avg_cost, 3),
            "last_run_timestamp": max(m.get("updated_at", 0) for m in matching),
        }

    def clear(self) -> None:
        self._save_all([])


default_history_store = HistoryStore()
