from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from .models import (
    Action,
    DelegationPolicy,
    ExecutionAttempt,
    ExecutionHistoryRecord,
    ExecutionPlan,
    RuntimeSnapshot,
    WorkloadProfile,
)


class RuntimeAdapter(ABC):
    """Universal boundary implemented by every compute runtime."""

    def __init__(self) -> None:
        self.control_mode: str = "validation"  # "advisory" (conseil), "validation", "delegation"
        self.approved_plan: ExecutionPlan | None = None
        self.delegation_policy: DelegationPolicy = DelegationPolicy()
        self.workload_profile: WorkloadProfile | None = None
        self.attempts: list[ExecutionAttempt] = []
        self.current_attempt_id: str | None = None

    def set_control_mode(
        self,
        mode: str,
        approved_plan: ExecutionPlan | dict | None = None,
        policy: DelegationPolicy | dict | None = None,
    ) -> None:
        """Configure operator governance mode: advisory (conseil), validation, or delegation."""
        m = mode.lower()
        if m in ("advisory", "conseil"):
            self.control_mode = "advisory"
        elif m in ("validation",):
            self.control_mode = "validation"
        elif m in ("delegation",):
            self.control_mode = "delegation"
        else:
            raise ValueError(f"Unknown control mode: {mode}. Must be advisory, validation, or delegation.")

        if approved_plan:
            self.approved_plan = (
                approved_plan
                if isinstance(approved_plan, ExecutionPlan)
                else ExecutionPlan(**approved_plan)
            )
        if policy:
            self.delegation_policy = (
                policy
                if isinstance(policy, DelegationPolicy)
                else DelegationPolicy(**policy)
            )

    def check_execution_permission(
        self,
        plan_or_action: Any,
        approved_by_operator: bool = False,
        delegation_policy: DelegationPolicy | dict | None = None,
    ) -> None:
        """Enforce operator governance checks at the backend level."""
        mode = self.control_mode

        # 1. Mode Conseil (Advisory): Strictly no mutation allowed
        if mode in ("advisory", "conseil"):
            raise PermissionError(
                "Mutation rejected: Runtime is in advisory (conseil) mode. No infrastructure mutation permitted."
            )

        # 2. Mode Validation: Requires explicit operator approval of concrete plan
        if mode == "validation":
            if not approved_by_operator and self.approved_plan is None:
                raise PermissionError(
                    "Mutation rejected: Validation mode requires explicit operator plan approval before execution."
                )

            # Check if execution materially deviates from approved plan
            if self.approved_plan is not None:
                plan_cpu = getattr(plan_or_action, "cpu", None)
                if plan_cpu is not None and self.approved_plan.cpu is not None:
                    # Allow minor deviation up to 10%, reject material deviation
                    if abs(plan_cpu - self.approved_plan.cpu) > max(4, self.approved_plan.cpu * 0.2):
                        raise PermissionError(
                            f"Mutation rejected: Requested CPU ({plan_cpu}) materially deviates from approved plan CPU ({self.approved_plan.cpu})."
                        )

        # 3. Mode Délégation: Check against DelegationPolicy guardrails
        if mode == "delegation":
            policy = self.delegation_policy
            if delegation_policy is not None:
                policy = (
                    delegation_policy
                    if isinstance(delegation_policy, DelegationPolicy)
                    else DelegationPolicy(**delegation_policy)
                )

            def _get_field(obj: Any, name: str) -> Any:
                if isinstance(obj, dict):
                    return obj.get(name)
                return getattr(obj, name, None)

            est_cost = _get_field(plan_or_action, "estimated_cost_eur")
            if est_cost is not None and est_cost > policy.max_budget_eur:
                raise PermissionError(
                    f"Mutation rejected: Plan cost ({est_cost:.2f}€) exceeds delegation policy budget limit ({policy.max_budget_eur:.2f}€)."
                )

            mtype = _get_field(plan_or_action, "machine_type")
            if mtype and policy.allowed_machine_types and len(policy.allowed_machine_types) > 0:
                if mtype not in policy.allowed_machine_types:
                    raise PermissionError(
                        f"Mutation rejected: Machine type '{mtype}' is not authorized in delegation policy."
                    )

            pmix = _get_field(plan_or_action, "provisioning_model")
            if pmix and policy.allowed_provisioning_models and len(policy.allowed_provisioning_models) > 0:
                if pmix not in policy.allowed_provisioning_models:
                    raise PermissionError(
                        f"Mutation rejected: Provisioning model '{pmix}' is not authorized in delegation policy."
                    )

            if len(self.attempts) >= policy.max_retries:
                raise PermissionError(
                    f"Mutation rejected: Maximum delegation retries ({policy.max_retries}) reached."
                )

    @abstractmethod
    def snapshot(self) -> RuntimeSnapshot:
        raise NotImplementedError

    @abstractmethod
    def apply(self, action: Action) -> None:
        raise NotImplementedError

    @abstractmethod
    def tick(self, minutes: float) -> None:
        raise NotImplementedError

    @abstractmethod
    def is_done(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def reset(self) -> None:
        raise NotImplementedError
