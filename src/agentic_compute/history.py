from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Optional

from .models import (
    ExecutionAttempt,
    ExecutionHistoryRecord,
    ExecutionPlan,
    WorkloadProfile,
)


def get_db_path() -> str:
    """Resolve database path from environment or default to local storage."""
    env_path = os.getenv("AGENTGRID_DB_PATH")
    if env_path:
        return env_path
    
    home = os.path.expanduser("~")
    base_dir = os.path.join(home, ".agentgrid")
    try:
        os.makedirs(base_dir, exist_ok=True)
        return os.path.join(base_dir, "agentgrid_history.db")
    except Exception:
        return os.path.join(os.getcwd(), "agentgrid_history.db")


class HistoryStore:
    """Thread-safe, persistent SQLite store for workloads, attempts, plans, and cost metrics."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or get_db_path()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS workload_profiles (
                    workload_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    profile_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS execution_plans (
                    plan_id TEXT PRIMARY KEY,
                    workload_id TEXT NOT NULL,
                    plan_type TEXT NOT NULL,
                    is_approved INTEGER NOT NULL DEFAULT 0,
                    plan_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS execution_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    workload_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    job_id TEXT,
                    status TEXT NOT NULL,
                    started_at REAL,
                    ended_at REAL,
                    elapsed_minutes REAL NOT NULL DEFAULT 0.0,
                    cost_calculated_eur REAL NOT NULL DEFAULT 0.0,
                    cost_status TEXT NOT NULL DEFAULT 'calculated_from_usage',
                    attempt_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS history_records (
                    workload_id TEXT PRIMARY KEY,
                    workload_name TEXT NOT NULL,
                    control_mode TEXT NOT NULL,
                    final_status TEXT NOT NULL,
                    initial_estimated_cost_eur REAL NOT NULL DEFAULT 0.0,
                    final_calculated_cost_eur REAL NOT NULL DEFAULT 0.0,
                    cost_comparison_delta_eur REAL NOT NULL DEFAULT 0.0,
                    initial_estimated_duration_minutes REAL NOT NULL DEFAULT 0.0,
                    final_actual_duration_minutes REAL NOT NULL DEFAULT 0.0,
                    duration_comparison_delta_minutes REAL NOT NULL DEFAULT 0.0,
                    reconciliation_status TEXT NOT NULL DEFAULT 'calculated_from_usage',
                    record_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workload_benchmarks (
                    benchmark_key TEXT PRIMARY KEY,
                    sample_size INTEGER NOT NULL DEFAULT 0,
                    total_work_units REAL NOT NULL DEFAULT 0.0,
                    total_execution_minutes REAL NOT NULL DEFAULT 0.0,
                    avg_minutes_per_unit REAL NOT NULL DEFAULT 0.0,
                    interruption_count INTEGER NOT NULL DEFAULT 0,
                    observed_interruption_rate REAL NOT NULL DEFAULT 0.0,
                    updated_at REAL NOT NULL
                );
                """
            )
            conn.commit()

    # --- Workload Profile Operations ---

    def save_workload_profile(self, profile: WorkloadProfile) -> None:
        now = time.time()
        payload = profile.model_dump_json()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO workload_profiles (workload_id, name, profile_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(workload_id) DO UPDATE SET
                    name=excluded.name,
                    profile_json=excluded.profile_json,
                    updated_at=excluded.updated_at
                """,
                (profile.workload_id, profile.name, payload, now, now),
            )
            conn.commit()

    def get_workload_profile(self, workload_id: str) -> Optional[WorkloadProfile]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT profile_json FROM workload_profiles WHERE workload_id = ?",
                (workload_id,),
            ).fetchone()
            if row:
                data = json.loads(row["profile_json"])
                return WorkloadProfile.model_validate(data)
        return None

    # --- Execution Plan Operations ---

    def save_execution_plan(
        self, plan: ExecutionPlan, workload_id: str, is_approved: bool = False
    ) -> None:
        now = time.time()
        payload = plan.model_dump_json()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO execution_plans (plan_id, workload_id, plan_type, is_approved, plan_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    is_approved=excluded.is_approved,
                    plan_json=excluded.plan_json
                """,
                (plan.plan_id, workload_id, plan.plan_type, 1 if is_approved else 0, payload, now),
            )
            conn.commit()

    def approve_execution_plan(self, plan_id: str) -> bool:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT workload_id FROM execution_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            if not row:
                return False
            workload_id = row["workload_id"]
            conn.execute(
                "UPDATE execution_plans SET is_approved = 0 WHERE workload_id = ?", (workload_id,)
            )
            conn.execute(
                "UPDATE execution_plans SET is_approved = 1 WHERE plan_id = ?", (plan_id,)
            )
            conn.commit()
            return True

    def get_execution_plans(self, workload_id: str) -> list[ExecutionPlan]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT plan_json FROM execution_plans WHERE workload_id = ? ORDER BY created_at ASC",
                (workload_id,),
            ).fetchall()
            return [ExecutionPlan.model_validate(json.loads(r["plan_json"])) for r in rows]

    def get_approved_plan(self, workload_id: str) -> Optional[ExecutionPlan]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT plan_json FROM execution_plans WHERE workload_id = ? AND is_approved = 1",
                (workload_id,),
            ).fetchone()
            if row:
                return ExecutionPlan.model_validate(json.loads(row["plan_json"]))
        return None

    # --- Execution Attempt Operations ---

    def record_attempt(self, attempt: ExecutionAttempt) -> None:
        now = time.time()
        payload = attempt.model_dump_json()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO execution_attempts (
                    attempt_id, workload_id, attempt_number, job_id, status,
                    started_at, ended_at, elapsed_minutes, cost_calculated_eur,
                    cost_status, attempt_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET
                    job_id=excluded.job_id,
                    status=excluded.status,
                    ended_at=excluded.ended_at,
                    elapsed_minutes=excluded.elapsed_minutes,
                    cost_calculated_eur=excluded.cost_calculated_eur,
                    cost_status=excluded.cost_status,
                    attempt_json=excluded.attempt_json,
                    updated_at=excluded.updated_at
                """,
                (
                    attempt.attempt_id,
                    attempt.workload_id,
                    attempt.attempt_number,
                    attempt.job_id,
                    attempt.status,
                    attempt.started_at or now,
                    attempt.ended_at,
                    attempt.elapsed_minutes,
                    attempt.cost_calculated_eur,
                    attempt.cost_status,
                    payload,
                    now,
                    now,
                ),
            )
            conn.commit()

    def get_attempts(self, workload_id: str) -> list[ExecutionAttempt]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT attempt_json FROM execution_attempts WHERE workload_id = ? ORDER BY attempt_number ASC",
                (workload_id,),
            ).fetchall()
            return [ExecutionAttempt.model_validate(json.loads(r["attempt_json"])) for r in rows]

    # --- History Record & Cost Reconciliation ---

    def save_history_record(self, record: ExecutionHistoryRecord) -> None:
        now = time.time()
        payload = record.model_dump_json()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO history_records (
                    workload_id, workload_name, control_mode, final_status,
                    initial_estimated_cost_eur, final_calculated_cost_eur, cost_comparison_delta_eur,
                    initial_estimated_duration_minutes, final_actual_duration_minutes, duration_comparison_delta_minutes,
                    reconciliation_status, record_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workload_id) DO UPDATE SET
                    control_mode=excluded.control_mode,
                    final_status=excluded.final_status,
                    initial_estimated_cost_eur=excluded.initial_estimated_cost_eur,
                    final_calculated_cost_eur=excluded.final_calculated_cost_eur,
                    cost_comparison_delta_eur=excluded.cost_comparison_delta_eur,
                    initial_estimated_duration_minutes=excluded.initial_estimated_duration_minutes,
                    final_actual_duration_minutes=excluded.final_actual_duration_minutes,
                    duration_comparison_delta_minutes=excluded.duration_comparison_delta_minutes,
                    reconciliation_status=excluded.reconciliation_status,
                    record_json=excluded.record_json,
                    updated_at=excluded.updated_at
                """,
                (
                    record.workload_id,
                    record.workload_name,
                    record.control_mode,
                    record.final_status,
                    record.initial_estimated_cost_eur,
                    record.final_calculated_cost_eur,
                    record.cost_comparison_delta_eur,
                    record.initial_estimated_duration_minutes,
                    record.final_actual_duration_minutes,
                    record.duration_comparison_delta_minutes,
                    record.reconciliation_status,
                    payload,
                    record.created_at or now,
                    now,
                ),
            )
            conn.commit()

    def get_history_record(self, workload_id: str) -> Optional[ExecutionHistoryRecord]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT record_json FROM history_records WHERE workload_id = ?",
                (workload_id,),
            ).fetchone()
            if row:
                return ExecutionHistoryRecord.model_validate(json.loads(row["record_json"]))
        return None

    def list_history_records(self, limit: int = 50) -> list[ExecutionHistoryRecord]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT record_json FROM history_records ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [ExecutionHistoryRecord.model_validate(json.loads(r["record_json"])) for r in rows]

    def reconcile_costs(
        self, workload_id: str, billed_cost_eur: Optional[float] = None
    ) -> dict[str, Any]:
        """Perform 3-tier cost and duration comparison distinguishing estimates, observed usage, and billed figures."""
        record = self.get_history_record(workload_id)
        attempts = self.get_attempts(workload_id)
        profile = self.get_workload_profile(workload_id)

        total_observed_cost = sum(a.cost_calculated_eur for a in attempts)
        total_observed_duration = sum(a.elapsed_minutes for a in attempts)

        estimated_cost = record.initial_estimated_cost_eur if record else 0.0
        estimated_duration = record.initial_estimated_duration_minutes if record else 0.0

        if billed_cost_eur is not None:
            billed_status = "reconciled_billed"
            billed_val = billed_cost_eur
        else:
            billed_status = "source_not_integrated"
            billed_val = None

        cost_delta = round(total_observed_cost - estimated_cost, 4)
        duration_delta = round(total_observed_duration - estimated_duration, 2)

        return {
            "workload_id": workload_id,
            "cost_tiers": {
                "tier1_estimated_cost_eur": estimated_cost,
                "tier2_calculated_from_usage_eur": round(total_observed_cost, 4),
                "tier3_reconciled_billed_eur": billed_val,
            },
            "billed_reconciliation_status": billed_status,
            "workload_name": profile.name if profile else (record.workload_name if record else "unknown"),
            "cost_breakdown": {
                "initial_estimated_cost_eur": estimated_cost,
                "observed_calculated_cost_eur": round(total_observed_cost, 4),
                "cost_delta_eur": cost_delta,
                "reconciled_billed_cost_eur": billed_val,
                "reconciliation_status": billed_status,
                "cost_basis_note": (
                    "Observed cost is calculated from authoritative active node-hours and known rate; "
                    "reconciled billing requires external GCP Cloud Billing export integration."
                ),
                "known_exclusions": [
                    "network_egress",
                    "persistent_disk_storage",
                    "idle_provisioning_margin",
                ],
            },
            "duration_breakdown": {
                "estimated_duration_minutes": estimated_duration,
                "actual_duration_minutes": round(total_observed_duration, 2),
                "duration_delta_minutes": duration_delta,
            },
            "attempts_count": len(attempts),
            "attempts": [a.model_dump() for a in attempts],
        }

    # --- Historical Benchmarks for Continuous Improvement ---

    def record_workload_benchmark(
        self,
        benchmark_key: str,
        work_units: float,
        duration_minutes: float,
        was_interrupted: bool = False,
    ) -> None:
        """Record completed workload execution metrics to improve future estimations with verifiable sample sizes."""
        if work_units <= 0 or duration_minutes <= 0:
            return

        now = time.time()
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT sample_size, total_work_units, total_execution_minutes, interruption_count FROM workload_benchmarks WHERE benchmark_key = ?",
                (benchmark_key,),
            ).fetchone()

            if row:
                sample_size = row["sample_size"] + 1
                total_units = row["total_work_units"] + work_units
                total_minutes = row["total_execution_minutes"] + duration_minutes
                interruptions = row["interruption_count"] + (1 if was_interrupted else 0)
                avg_min_per_unit = total_minutes / total_units if total_units > 0 else 0.0
                interruption_rate = interruptions / sample_size if sample_size > 0 else 0.0

                conn.execute(
                    """
                    UPDATE workload_benchmarks SET
                        sample_size = ?,
                        total_work_units = ?,
                        total_execution_minutes = ?,
                        avg_minutes_per_unit = ?,
                        interruption_count = ?,
                        observed_interruption_rate = ?,
                        updated_at = ?
                    WHERE benchmark_key = ?
                    """,
                    (
                        sample_size,
                        total_units,
                        total_minutes,
                        round(avg_min_per_unit, 4),
                        interruptions,
                        round(interruption_rate, 4),
                        now,
                        benchmark_key,
                    ),
                )
            else:
                avg_min_per_unit = duration_minutes / work_units
                interruption_rate = 1.0 if was_interrupted else 0.0
                conn.execute(
                    """
                    INSERT INTO workload_benchmarks (
                        benchmark_key, sample_size, total_work_units, total_execution_minutes,
                        avg_minutes_per_unit, interruption_count, observed_interruption_rate, updated_at
                    )
                    VALUES (?, 1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        benchmark_key,
                        work_units,
                        duration_minutes,
                        round(avg_min_per_unit, 4),
                        1 if was_interrupted else 0,
                        interruption_rate,
                        now,
                    ),
                )
            conn.commit()

    def get_benchmark_metrics(self, benchmark_key: str) -> Optional[dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT sample_size, avg_minutes_per_unit, observed_interruption_rate, updated_at
                FROM workload_benchmarks WHERE benchmark_key = ?
                """,
                (benchmark_key,),
            ).fetchone()
            if row and row["sample_size"] > 0:
                return {
                    "benchmark_key": benchmark_key,
                    "sample_size": row["sample_size"],
                    "avg_minutes_per_unit": row["avg_minutes_per_unit"],
                    "observed_interruption_rate": row["observed_interruption_rate"],
                    "provenance": "historical_execution_telemetry",
                    "updated_at": row["updated_at"],
                }
        return None


    def get_comparable_metrics(
        self,
        workload_name: str | None = None,
        machine_type: str | None = None,
    ) -> dict[str, Any]:
        """Extract duration and cost metrics from comparable historical completed runs."""
        records = self.list_history_records()
        matching = []
        for r in records:
            if r.final_status != "COMPLETED":
                continue
            if workload_name and r.workload_name == workload_name:
                matching.append(r)
            elif machine_type:
                for att in r.attempts:
                    if att.allocated_machine_type == machine_type:
                        matching.append(r)
                        break

        if not matching:
            return {
                "sample_size": 0,
                "provenance": f"sqlite_history_store_{self.db_path}",
                "average_duration_minutes": None,
                "average_cost_eur": None,
                "message": "No comparable historical runs found for calibration",
            }

        avg_dur = sum(m.final_actual_duration_minutes for m in matching) / len(matching)
        avg_cost = sum(m.final_calculated_cost_eur for m in matching) / len(matching)

        return {
            "sample_size": len(matching),
            "provenance": f"sqlite_history_store_{self.db_path}",
            "average_duration_minutes": round(avg_dur, 2),
            "average_cost_eur": round(avg_cost, 3),
        }


_history_store: Optional[HistoryStore] = None


def get_history_store() -> HistoryStore:
    global _history_store
    if _history_store is None:
        _history_store = HistoryStore()
    return _history_store
