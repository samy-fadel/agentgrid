"""Server-side source of truth for operator control, plan approval and submission identity.

Why this module exists
----------------------
Before this module, three security-relevant decisions were taken from values the
*caller* supplied:

1. The control mode travelled as a function argument, so an agent could send
   ``control_mode="delegation"`` and escape the operator's chosen mode.
2. ``evaluate_control_gate`` accepted ``is_operator_approved=True`` -- or an
   ``approved_plan_id`` equal to the plan being submitted -- as proof of human
   approval. A model can trivially produce both.
3. Submission de-duplication lived in a per-instance dict, and the controller was
   re-created on every HTTP request, so idempotency never survived a request
   boundary, let alone a restart.

Everything here is persisted in the same SQLite database as the execution
history, so the answers to "what mode is this workload in", "was this exact plan
approved by a human", and "did we already submit this" survive process restarts.

Threat model note
-----------------
This module defends against a *confused or over-eager agent*, not against a
malicious operator: application-level authentication is explicitly out of scope
for this intervention. Anyone who can reach the approval endpoint is treated as
the operator. What it does guarantee is that the execution path cannot approve
itself -- approval must be a separate, recorded, prior act.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from typing import Any, Optional

from .models import DelegationPolicy, ExecutionPlan

# Operator control modes, ordered from most restrictive to least.
CONTROL_MODES = ("advisory", "validation", "delegation")

#: Fields whose change makes an approved plan materially different, so a prior
#: approval must no longer apply. Ordered for a stable fingerprint.
MATERIAL_PLAN_FIELDS = (
    "cpu",
    "gpu",
    "machine_type",
    "region",
    "zone",
    "provisioning_model",
    "quantity",
    "estimated_cost_eur",
)


def default_control_mode() -> str:
    """Server default when a workload has no explicit governance row.

    Defaults to ``validation`` (human approval required) so that the safe
    behaviour is the one you get by forgetting to configure anything.
    """
    mode = (os.getenv("AGENTGRID_DEFAULT_CONTROL_MODE") or "validation").lower().strip()
    return mode if mode in CONTROL_MODES else "validation"


def normalize_control_mode(mode: str | None) -> str:
    """Map operator-facing synonyms onto the canonical mode names."""
    if mode is None:
        return default_control_mode()
    m = str(mode).lower().strip()
    if m in ("advisory", "conseil", "read_only", "readonly"):
        return "advisory"
    if m in ("validation", "validate", "human_in_the_loop"):
        return "validation"
    if m in ("delegation", "delegated", "autonomous"):
        return "delegation"
    raise ValueError(
        f"Unknown control mode '{mode}'. Expected one of: {', '.join(CONTROL_MODES)}."
    )


def plan_fingerprint(
    plan: ExecutionPlan | dict[str, Any],
    command: str | None = None,
) -> str:
    """Return a stable digest of a plan's materially-executable properties.

    Any change to CPU, GPU, machine type, region, zone, provisioning model,
    quantity, cost or command yields a different fingerprint, which invalidates
    a previously recorded approval. Cosmetic fields (title, rationale, narrative
    text) are deliberately excluded so that re-rendering a plan does not force
    the operator to approve it again.
    """
    data = plan.model_dump() if isinstance(plan, ExecutionPlan) else dict(plan)
    material = {key: data.get(key) for key in MATERIAL_PLAN_FIELDS}
    material["command"] = command
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def make_plan_id(workload_id: str, plan_type: str, fingerprint: str) -> str:
    """Build a plan id that is unique per (workload, plan shape).

    The previous engine emitted bare ids such as ``plan-cost-optimized`` for
    every workload, so approving a plan for one workload also matched a
    different workload's plan of the same type.
    """
    return f"plan-{workload_id}-{plan_type}-{fingerprint[:12]}"


def submission_key(workload_id: str, fingerprint: str) -> str:
    """Stable submission identity used to recover from an ambiguous timeout.

    This is what gets attached to the job so that, if the network drops after
    the scheduler accepted the request, we can look for *this specific*
    submission instead of "any job that happens to exist".
    """
    return f"agentgrid-{workload_id}-{fingerprint[:12]}"


class GovernanceError(RuntimeError):
    """Base class for governance failures."""


class ControlModeViolation(GovernanceError):
    """Raised when an action is not permitted by the workload's control mode."""


class PolicyViolation(GovernanceError):
    """Raised when a delegated action exceeds its DelegationPolicy boundaries."""


class GovernanceStore:
    """Persistent store for control modes, plan approvals and submission claims."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        from .history import get_db_path

        self.db_path = db_path or get_db_path()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS workload_governance (
                    workload_id TEXT PRIMARY KEY,
                    control_mode TEXT NOT NULL,
                    delegation_policy_json TEXT,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS registered_plans (
                    plan_id TEXT PRIMARY KEY,
                    workload_id TEXT NOT NULL,
                    plan_type TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS plan_approvals (
                    plan_id TEXT PRIMARY KEY,
                    workload_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    approved_at REAL NOT NULL,
                    revoked_at REAL
                );

                CREATE TABLE IF NOT EXISTS submission_ledger (
                    submission_key TEXT PRIMARY KEY,
                    workload_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    job_id TEXT,
                    state TEXT NOT NULL,
                    detail TEXT,
                    attempt_id TEXT,
                    estimated_cost_eur REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_submission_workload
                    ON submission_ledger (workload_id);

                -- Releasing a claim used to delete the row outright, which
                -- erased the fact that a launch had happened at all. A retry
                -- ceiling cannot be enforced against a history that deletes
                -- itself, and an `uncertain` outcome that may have created a
                -- job must not stop being charged just because the operator
                -- authorised another try.
                CREATE TABLE IF NOT EXISTS submission_archive (
                    archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    submission_key TEXT NOT NULL,
                    workload_id TEXT NOT NULL,
                    plan_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    job_id TEXT,
                    state TEXT NOT NULL,
                    detail TEXT,
                    attempt_id TEXT,
                    estimated_cost_eur REAL,
                    created_at REAL NOT NULL,
                    archived_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_archive_workload
                    ON submission_archive (workload_id);
                """
            )

            # Migration for databases created before the ledger priced its
            # rows. CREATE TABLE IF NOT EXISTS silently keeps the old shape, so
            # the column has to be added explicitly or every cumulative-spend
            # query would raise "no such column" on an existing install.
            existing_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(submission_ledger)").fetchall()
            }
            if "estimated_cost_eur" not in existing_columns:
                conn.execute(
                    "ALTER TABLE submission_ledger ADD COLUMN estimated_cost_eur REAL"
                )

            conn.commit()

    # ------------------------------------------------------------------
    # Control mode / policy
    # ------------------------------------------------------------------

    def set_workload_control(
        self,
        workload_id: str,
        control_mode: str,
        delegation_policy: DelegationPolicy | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record the operator's chosen mode and policy for a workload."""
        mode = normalize_control_mode(control_mode)
        policy_json = None
        if delegation_policy is not None:
            policy = (
                delegation_policy
                if isinstance(delegation_policy, DelegationPolicy)
                else DelegationPolicy(**delegation_policy)
            )
            policy_json = policy.model_dump_json()

        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workload_governance
                    (workload_id, control_mode, delegation_policy_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(workload_id) DO UPDATE SET
                    control_mode=excluded.control_mode,
                    delegation_policy_json=COALESCE(
                        excluded.delegation_policy_json,
                        workload_governance.delegation_policy_json
                    ),
                    updated_at=excluded.updated_at
                """,
                (workload_id, mode, policy_json, now),
            )
            conn.commit()
        return self.get_workload_control(workload_id)

    def get_workload_control(self, workload_id: str) -> dict[str, Any]:
        """Return the effective mode/policy, falling back to the server default."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT control_mode, delegation_policy_json FROM workload_governance WHERE workload_id = ?",
                (workload_id,),
            ).fetchone()

        if row is None:
            return {
                "workload_id": workload_id,
                "control_mode": default_control_mode(),
                "delegation_policy": DelegationPolicy(),
                "source": "server_default",
            }

        policy = DelegationPolicy()
        if row["delegation_policy_json"]:
            try:
                policy = DelegationPolicy(**json.loads(row["delegation_policy_json"]))
            except Exception:
                policy = DelegationPolicy()

        return {
            "workload_id": workload_id,
            "control_mode": row["control_mode"],
            "delegation_policy": policy,
            "source": "operator_configured",
        }

    # ------------------------------------------------------------------
    # Plan registration and approval
    # ------------------------------------------------------------------

    def register_plan(
        self,
        workload_id: str,
        plan: ExecutionPlan | dict[str, Any],
        command: str | None = None,
    ) -> dict[str, Any]:
        """Persist a plan so it can later be approved and executed.

        Registration is what makes a plan *referenceable*. Execution refuses
        plans it has never seen, which stops an agent from inventing a plan id.
        """
        p = plan if isinstance(plan, ExecutionPlan) else ExecutionPlan(**plan)
        fingerprint = plan_fingerprint(p, command=command)
        now = time.time()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO registered_plans
                    (plan_id, workload_id, plan_type, fingerprint, plan_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    workload_id=excluded.workload_id,
                    plan_type=excluded.plan_type,
                    fingerprint=excluded.fingerprint,
                    plan_json=excluded.plan_json
                """,
                (
                    p.plan_id,
                    workload_id,
                    p.plan_type,
                    fingerprint,
                    p.model_dump_json(),
                    now,
                ),
            )
            # A materially changed plan can no longer rely on an old approval.
            conn.execute(
                """
                UPDATE plan_approvals SET revoked_at = ?
                WHERE plan_id = ? AND fingerprint != ? AND revoked_at IS NULL
                """,
                (now, p.plan_id, fingerprint),
            )
            conn.commit()

        return {"plan_id": p.plan_id, "workload_id": workload_id, "fingerprint": fingerprint}

    def get_registered_plan(self, plan_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM registered_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "plan_id": row["plan_id"],
            "workload_id": row["workload_id"],
            "plan_type": row["plan_type"],
            "fingerprint": row["fingerprint"],
            "plan": json.loads(row["plan_json"]),
            "created_at": row["created_at"],
        }

    def list_registered_plans(self, workload_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT plan_id FROM registered_plans WHERE workload_id = ? ORDER BY created_at",
                (workload_id,),
            ).fetchall()
        return [self.get_registered_plan(r["plan_id"]) for r in rows]

    def approve_plan(
        self,
        plan_id: str,
        workload_id: str | None = None,
        approved_by: str = "operator",
    ) -> dict[str, Any]:
        """Record a human approval for a previously registered plan.

        This is the *only* way to authorise execution in validation mode.
        """
        registered = self.get_registered_plan(plan_id)
        if registered is None:
            return {
                "status": "not_found",
                "plan_id": plan_id,
                "reason": (
                    f"Plan '{plan_id}' is not registered. Compare plans first so the "
                    f"server records the exact plan being approved."
                ),
            }

        if workload_id is not None and registered["workload_id"] != workload_id:
            return {
                "status": "workload_mismatch",
                "plan_id": plan_id,
                "reason": (
                    f"Plan '{plan_id}' belongs to workload '{registered['workload_id']}', "
                    f"not '{workload_id}'."
                ),
            }

        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO plan_approvals
                    (plan_id, workload_id, fingerprint, approved_by, approved_at, revoked_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                ON CONFLICT(plan_id) DO UPDATE SET
                    workload_id=excluded.workload_id,
                    fingerprint=excluded.fingerprint,
                    approved_by=excluded.approved_by,
                    approved_at=excluded.approved_at,
                    revoked_at=NULL
                """,
                (plan_id, registered["workload_id"], registered["fingerprint"], approved_by, now),
            )
            conn.commit()

        return {
            "status": "approved",
            "plan_id": plan_id,
            "workload_id": registered["workload_id"],
            "fingerprint": registered["fingerprint"],
            "approved_by": approved_by,
            "approved_at": now,
        }

    def revoke_approval(self, plan_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE plan_approvals SET revoked_at = ? WHERE plan_id = ? AND revoked_at IS NULL",
                (time.time(), plan_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def check_approval(
        self,
        plan_id: str,
        workload_id: str,
        fingerprint: str,
    ) -> tuple[bool, str]:
        """Return (approved, reason) for the exact plan the caller wants to run."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM plan_approvals WHERE plan_id = ?", (plan_id,)
            ).fetchone()

        if row is None:
            return (
                False,
                f"No recorded operator approval for plan '{plan_id}'. "
                f"Validation mode requires an approval registered on the server before execution.",
            )
        if row["revoked_at"] is not None:
            return (False, f"Approval for plan '{plan_id}' was revoked.")
        if row["workload_id"] != workload_id:
            return (
                False,
                f"Approval for plan '{plan_id}' is bound to workload "
                f"'{row['workload_id']}', not '{workload_id}'.",
            )
        if row["fingerprint"] != fingerprint:
            return (
                False,
                f"Plan '{plan_id}' changed materially since it was approved "
                f"(approved fingerprint {row['fingerprint'][:12]}, "
                f"submitted {fingerprint[:12]}). Re-approval is required.",
            )
        return (True, f"Plan '{plan_id}' has a recorded operator approval.")

    # ------------------------------------------------------------------
    # Submission ledger (persistent idempotency)
    # ------------------------------------------------------------------

    def claim_submission(
        self,
        key: str,
        workload_id: str,
        plan_id: str,
        fingerprint: str,
        estimated_cost_eur: float | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Atomically claim the right to submit.

        Returns ``(True, row)`` when this caller won the claim and must perform
        the submission, or ``(False, row)`` when a claim already exists -- in
        which case the existing row describes what happened previously.

        The INSERT is the lock: SQLite's primary key constraint makes concurrent
        double-clicks resolve to exactly one winner.

        ``estimated_cost_eur`` is stored on the row so that :meth:`get_commitments`
        can rebuild the cumulative committed spend from the server's own records
        instead of trusting a figure supplied by the caller.
        """
        won, row, _refusal = self.claim_submission_within_limits(
            key=key,
            workload_id=workload_id,
            plan_id=plan_id,
            fingerprint=fingerprint,
            estimated_cost_eur=estimated_cost_eur,
        )
        return won, row

    def claim_submission_within_limits(
        self,
        key: str,
        workload_id: str,
        plan_id: str,
        fingerprint: str,
        estimated_cost_eur: float | None = None,
        max_launches: int | None = None,
        max_committed_cost_eur: float | None = None,
        caller_accumulated_cost_eur: float = 0.0,
        caller_attempts_used: int = 0,
    ) -> tuple[bool, dict[str, Any], Optional[str]]:
        """Check the cumulative ceilings and claim, in a single transaction.

        Returns ``(won, row, refusal)``. ``refusal`` is a sentence when a
        delegated ceiling forbids the claim, and ``None`` otherwise.

        Checking the ceilings on one connection and inserting on another is a
        time-of-check/time-of-use race, and it is not theoretical: four
        concurrent submissions of four *different* plans do not share a
        submission key, so the primary key does not separate them. They all read
        "0 launches used" and all four were accepted under ``max_retries: 1``.
        ``BEGIN IMMEDIATE`` takes the write lock before the count, so the
        ceilings are evaluated against a state nobody else can be changing.
        """
        now = time.time()
        if estimated_cost_eur is None:
            estimated_cost_eur = self._registered_plan_cost(plan_id)
        plan_cost = float(estimated_cost_eur or 0.0)

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            existing = conn.execute(
                "SELECT * FROM submission_ledger WHERE submission_key = ?", (key,)
            ).fetchone()
            if existing is not None:
                conn.rollback()
                return False, dict(existing), None

            if max_launches is not None or max_committed_cost_eur is not None:
                commitments = self._commitments_on(conn, workload_id, exclude_key=key)

                if max_launches is not None:
                    attempts = max(caller_attempts_used, commitments["launched_attempts"])
                    if attempts >= max_launches:
                        conn.rollback()
                        return (
                            False,
                            {},
                            (
                                f"Execution blocked: delegated retry budget exhausted "
                                f"({attempts}/{max_launches} attempts used). "
                                f"Human validation required."
                            ),
                        )

                if max_committed_cost_eur is not None:
                    spent = max(
                        caller_accumulated_cost_eur, commitments["committed_cost_eur"]
                    )
                    projected = spent + plan_cost
                    if projected > max_committed_cost_eur:
                        detail = (
                            f"cumulative {projected:.2f}EUR (already spent {spent:.2f}EUR "
                            f"+ plan {plan_cost:.2f}EUR)"
                            if spent
                            else f"{plan_cost:.2f}EUR"
                        )
                        conn.rollback()
                        return (
                            False,
                            {},
                            (
                                f"Execution blocked: Plan estimated cost {detail} exceeds "
                                f"delegated policy budget limit "
                                f"({max_committed_cost_eur:.2f}EUR). "
                                f"Human validation required."
                            ),
                        )

            try:
                conn.execute(
                    """
                    INSERT INTO submission_ledger
                        (submission_key, workload_id, plan_id, fingerprint,
                         job_id, state, detail, attempt_id, estimated_cost_eur,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, NULL, 'claimed', NULL, NULL, ?, ?, ?)
                    """,
                    (key, workload_id, plan_id, fingerprint, estimated_cost_eur, now, now),
                )
                conn.commit()
                won = True
            except sqlite3.IntegrityError:
                conn.rollback()
                won = False

            row = conn.execute(
                "SELECT * FROM submission_ledger WHERE submission_key = ?", (key,)
            ).fetchone()
            return won, (dict(row) if row else {}), None
        finally:
            conn.close()

    def _registered_plan_cost(self, plan_id: str) -> Optional[float]:
        """Best-effort price for a plan that was registered before submission."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT plan_json FROM registered_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        if not row:
            return None
        try:
            return float(json.loads(row["plan_json"]).get("estimated_cost_eur"))
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    #: Ledger states that represent money the operator is already on the hook
    #: for. ``failed`` is excluded because nothing was created; ``uncertain`` is
    #: included precisely because it may have created something.
    COMMITTING_STATES = ("claimed", "submitted", "uncertain")

    #: Archived (released) states that still represent money. A `failed` claim
    #: created nothing and is refunded; a released `submitted` or `uncertain`
    #: one is not.
    RETAINED_ARCHIVE_STATES = ("submitted", "uncertain")

    def get_commitments(
        self, workload_id: str, exclude_key: str | None = None
    ) -> dict[str, Any]:
        """Rebuild what a workload has already committed, from the ledger.

        This exists because a delegated budget that only counts what the caller
        volunteers is not a ceiling at all: an agent can submit plan after plan,
        each individually under the limit, and never hit the cap. The figure
        returned here is derived from the server's own submission records, so
        the caller cannot understate it.

        ``exclude_key`` omits one submission key, which is what makes a replay
        of an already-claimed submission avoid counting itself twice.
        """
        with self._connect() as conn:
            return self._commitments_on(conn, workload_id, exclude_key)

    def _commitments_on(
        self, conn: sqlite3.Connection, workload_id: str, exclude_key: str | None = None
    ) -> dict[str, Any]:
        """Aggregate commitments on an existing connection.

        Split out so the atomic claim can evaluate the ceilings inside the very
        transaction that inserts the claim. Evaluating them on a separate
        connection first is a time-of-check/time-of-use race: four concurrent
        submissions all read "0 launches" and all proceed.
        """
        placeholders = ", ".join("?" for _ in self.COMMITTING_STATES)
        sql = (
            f"SELECT submission_key, plan_id, state, estimated_cost_eur "
            f"FROM submission_ledger "
            f"WHERE workload_id = ? AND state IN ({placeholders})"
        )
        params: list[Any] = [workload_id, *self.COMMITTING_STATES]
        if exclude_key:
            sql += " AND submission_key != ?"
            params.append(exclude_key)

        launched_sql = "SELECT COUNT(*) AS n FROM submission_ledger WHERE workload_id = ?"
        launched_params: list[Any] = [workload_id]
        if exclude_key:
            launched_sql += " AND submission_key != ?"
            launched_params.append(exclude_key)

        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        live_launches = conn.execute(launched_sql, launched_params).fetchone()["n"]
        archived = [
            dict(r)
            for r in conn.execute(
                "SELECT submission_key, plan_id, state, estimated_cost_eur "
                "FROM submission_archive WHERE workload_id = ?",
                (workload_id,),
            ).fetchall()
        ]

        committed = 0.0
        unpriced = 0

        def _price(row: dict[str, Any]) -> float | None:
            cost = row.get("estimated_cost_eur")
            if cost is None:
                # Row written before the ledger priced submissions, or a plan
                # whose price was never known. Try the registered plan, then
                # admit ignorance rather than silently treating it as free.
                cost = self._registered_plan_cost(row["plan_id"])
            return None if cost is None else float(cost)

        for row in rows:
            cost = _price(row)
            if cost is None:
                unpriced += 1
                continue
            committed += cost

        # A released claim still counts if something may have been created by
        # it. `failed` and `claimed` created nothing; `submitted` created a job
        # and `uncertain` may have, so authorising a retry does not refund them.
        retained = [r for r in archived if r["state"] in self.RETAINED_ARCHIVE_STATES]
        for row in retained:
            cost = _price(row)
            if cost is None:
                unpriced += 1
                continue
            committed += cost

        return {
            "workload_id": workload_id,
            "committed_cost_eur": round(committed, 4),
            "submission_count": len(rows) + len(retained),
            "unpriced_submissions": unpriced,
            "counted_states": list(self.COMMITTING_STATES),
            # Every claim that was ever won is a launch, including the ones
            # since released. This is what bounds a retry loop.
            "launched_attempts": live_launches + len(archived),
            "released_launches": len(archived),
            "retained_released_commitments": len(retained),
        }

    def record_submission(
        self,
        key: str,
        state: str,
        job_id: str | None = None,
        detail: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        """Update a submission claim with its outcome.

        States: ``claimed`` (in flight), ``submitted`` (accepted, job known),
        ``uncertain`` (ambiguous timeout, outcome genuinely unknown),
        ``failed`` (rejected before any job existed).
        """
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE submission_ledger
                SET state = ?,
                    job_id = COALESCE(?, job_id),
                    detail = ?,
                    attempt_id = COALESCE(?, attempt_id),
                    updated_at = ?
                WHERE submission_key = ?
                """,
                (state, job_id, detail, attempt_id, now, key),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM submission_ledger WHERE submission_key = ?", (key,)
            ).fetchone()
        return dict(row) if row else {}

    def get_submission(self, key: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM submission_ledger WHERE submission_key = ?", (key,)
            ).fetchone()
        return dict(row) if row else None

    def release_submission(self, key: str) -> bool:
        """Archive a claim so an explicitly authorised retry may submit again.

        Used when an operator (or a bounded retry policy) decides a failed or
        uncertain submission should be attempted afresh. This is deliberately
        distinct from a replay: a replay must never release the claim.

        The row is copied to ``submission_archive`` before being removed. A
        straight delete erased the evidence that a launch had happened, so a
        retry ceiling could be walked past indefinitely and an `uncertain`
        submission -- which may well have created a job -- stopped being
        charged against the delegated budget.
        """
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM submission_ledger WHERE submission_key = ?", (key,)
            ).fetchone()
            if row is None:
                return False

            conn.execute(
                """
                INSERT INTO submission_archive
                    (submission_key, workload_id, plan_id, fingerprint, job_id,
                     state, detail, attempt_id, estimated_cost_eur,
                     created_at, archived_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["submission_key"],
                    row["workload_id"],
                    row["plan_id"],
                    row["fingerprint"],
                    row["job_id"],
                    row["state"],
                    row["detail"],
                    row["attempt_id"],
                    row["estimated_cost_eur"],
                    row["created_at"],
                    now,
                ),
            )
            cur = conn.execute("DELETE FROM submission_ledger WHERE submission_key = ?", (key,))
            conn.commit()
            return cur.rowcount > 0

    def list_archived_submissions(self, workload_id: str) -> list[dict[str, Any]]:
        """Released claims, kept so a retry history cannot delete itself."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM submission_archive WHERE workload_id = ? ORDER BY archived_at",
                (workload_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_submissions(self, workload_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM submission_ledger WHERE workload_id = ? ORDER BY created_at",
                (workload_id,),
            ).fetchall()
        return [dict(r) for r in rows]


_governance_store: Optional[GovernanceStore] = None


def get_governance_store() -> GovernanceStore:
    """Return the process-wide governance store (created lazily)."""
    global _governance_store
    if _governance_store is None:
        _governance_store = GovernanceStore()
    return _governance_store


def reset_governance_store() -> None:
    """Drop the cached store so a new AGENTGRID_DB_PATH takes effect (tests)."""
    global _governance_store
    _governance_store = None


class MutationBlocked(PermissionError):
    """Raised when the governing control mode forbids a mutation.

    Subclasses :class:`PermissionError` so existing callers that already catch
    the runtime adapters' permission errors keep behaving sensibly.
    """

    def __init__(self, reason: str, control_mode: str, workload_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.control_mode = control_mode
        self.workload_id = workload_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "blocked",
            "reason": self.reason,
            "control_mode": self.control_mode,
            "workload_id": self.workload_id,
        }


def enforce_mutation(
    workload_id: str | None,
    action: str,
    plan_id: str | None = None,
    store: GovernanceStore | None = None,
) -> dict[str, Any]:
    """Authorise a cluster mutation that does not go through ``submit_plan``.

    Several entry points -- the legacy MCP tools in particular -- mutate the
    cluster directly. They previously honoured only whatever mode the runtime
    adapter happened to hold, which defaults to ``delegation`` in both adapters.
    That made the operator's choice advisory in name only.

    This is the shared choke point:

    * ``advisory``   -- every mutation is refused;
    * ``validation`` -- refused unless an approval is recorded on the server for
      ``plan_id``, bound to this workload;
    * ``delegation`` -- allowed; policy limits on cost/shape are enforced by the
      caller that knows the plan (``check_policy_bounds``).

    Raises :class:`MutationBlocked` when refused, and returns the governing
    state when allowed, so the caller can report which mode authorised it.
    """
    gov = store or get_governance_store()
    state = gov.get_workload_control(workload_id or "")
    mode = state["control_mode"]

    if mode == "advisory":
        raise MutationBlocked(
            f"Advisory mode is read-only: '{action}' is refused for workload "
            f"'{workload_id}'. Switch to validation or delegation to mutate the cluster.",
            control_mode=mode,
            workload_id=workload_id,
        )

    if mode == "validation":
        if not plan_id:
            raise MutationBlocked(
                f"Validation mode requires an approved plan: '{action}' was requested for "
                f"workload '{workload_id}' without a plan_id, so there is nothing an "
                f"operator could have approved.",
                control_mode=mode,
                workload_id=workload_id,
            )
        registered = gov.get_registered_plan(plan_id)
        if registered is None:
            raise MutationBlocked(
                f"Validation mode: plan '{plan_id}' is not registered on the server, so "
                f"'{action}' cannot be authorised.",
                control_mode=mode,
                workload_id=workload_id,
            )
        approved, reason = gov.check_approval(
            plan_id=plan_id,
            workload_id=workload_id or registered["workload_id"],
            fingerprint=registered["fingerprint"],
        )
        if not approved:
            raise MutationBlocked(
                f"Validation mode: {reason}", control_mode=mode, workload_id=workload_id
            )

    return {
        "control_mode": mode,
        "control_mode_source": state["source"],
        "delegation_policy": state["delegation_policy"],
        "workload_id": workload_id,
    }
