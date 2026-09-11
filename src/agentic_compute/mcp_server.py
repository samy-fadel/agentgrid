from __future__ import annotations

from mcp.server.fastmcp import FastMCP

import logging
import os

from .models import Action
from .runtime import RuntimeAdapter
from .simulator import SimulatedRuntime
from .slurm_adapter import SlurmRuntime
from .capacity_advisor import query_capacity_advice


from mcp.server.transport_security import TransportSecuritySettings

logger = logging.getLogger(__name__)

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

# Disable DNS rebinding check to allow Cloud Run domains (*.run.app)
_security_settings = TransportSecuritySettings(enable_dns_rebinding_protection=False)

mcp = FastMCP(
    "Agentic Compute Runtime",
    json_response=True,
    host=HOST,
    port=PORT,
    transport_security=_security_settings,
)


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    """Health check endpoint for Cloud Run liveness/readiness probes."""
    from starlette.responses import JSONResponse

    runtime_type = os.getenv("COMPUTE_RUNTIME", "simulator").lower()
    return JSONResponse(
        {
            "status": "healthy",
            "service": "agentic-compute-mcp",
            "runtime": runtime_type,
        }
    )


@mcp.custom_route("/snapshot", methods=["GET"])
async def snapshot_route(request):
    """Snapshot endpoint for web dashboard and monitoring."""
    from starlette.responses import JSONResponse

    runtime_type = os.getenv("COMPUTE_RUNTIME", "simulator").lower()
    return JSONResponse(
        {
            "runtime": runtime_type,
            "snapshot": _runtime.snapshot().model_dump(),
            "slurm_verification": getattr(_runtime, "last_slurm_action", None),
        }
    )


@mcp.custom_route("/reset", methods=["POST"])
async def reset_route(request):
    """Reset runtime endpoint."""
    from starlette.responses import JSONResponse

    _runtime.reset()
    runtime_type = os.getenv("COMPUTE_RUNTIME", "simulator").lower()
    return JSONResponse(
        {
            "status": "reset",
            "runtime": runtime_type,
            "snapshot": _runtime.snapshot().model_dump(),
        }
    )


@mcp.custom_route("/capacity-advice", methods=["GET"])
async def capacity_advice_route(request):
    """Capacity advice endpoint for dashboard and external monitoring."""
    from starlette.responses import JSONResponse

    params = request.query_params
    machine_types = params.get("machine_types", "n4-standard-32,n2-standard-32,n2-standard-16")
    try:
        size = int(params.get("size", 10))
    except (ValueError, TypeError):
        size = 10
    region = params.get("region") or None
    provisioning_model = params.get("provisioning_model", "SPOT")
    target_distribution_shape = params.get("target_distribution_shape", "ANY")
    demo_param = params.get("demo_mode")
    demo_mode = demo_param.lower() in ("true", "1", "yes") if demo_param is not None else None

    advice = query_capacity_advice(
        machine_types=machine_types,
        size=size,
        region=region,
        provisioning_model=provisioning_model,
        target_distribution_shape=target_distribution_shape,
        demo_mode=demo_mode,
    )
    return JSONResponse(advice)


def _create_runtime() -> RuntimeAdapter:
    runtime_type = os.getenv("COMPUTE_RUNTIME", "simulator").lower()
    if runtime_type == "slurm":
        return SlurmRuntime()
    return SimulatedRuntime()


_runtime: RuntimeAdapter = _create_runtime()


from .capacity_search import search_compatible_capacity
from .diagnostic import diagnose_blockers
from .execution_controller import ExecutionController
from .governance import MutationBlocked, enforce_mutation, get_governance_store
from .history import get_history_store
from .lifecycle_manager import default_lifecycle_manager
from .models import (
    DelegationPolicy,
    DiagnosticItem,
    ExecutionPlan,
    WorkloadProfile,
)
from .plan_engine import evaluate_and_compare_plans

_execution_controller = ExecutionController(_runtime)
_history_store = get_history_store()


@mcp.custom_route("/search-capacity", methods=["POST", "GET"])
async def search_capacity_route(request):
    """Search compatible capacity candidates across catalog, quotas, and availability signals."""
    from starlette.responses import JSONResponse
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
    else:
        body = dict(request.query_params)

    candidates = search_compatible_capacity(
        workload_profile=body.get("workload_profile"),
        cpu_requested=int(body.get("cpu_requested", 4)),
        gpu_requested=int(body.get("gpu_requested", 0)),
        memory_gb_requested=float(body.get("memory_gb_requested", 16.0)),
        allowed_regions=body.get("allowed_regions") if isinstance(body.get("allowed_regions"), list) else None,
        allow_spot=str(body.get("allow_spot", "true")).lower() in ("true", "1"),
        allow_standard=str(body.get("allow_standard", "true")).lower() in ("true", "1"),
        demo_mode=str(body.get("demo_mode", "false")).lower() in ("true", "1") if "demo_mode" in body else None,
    )
    return JSONResponse({"candidates": [c.model_dump() for c in candidates]})


@mcp.custom_route("/diagnose", methods=["POST", "GET"])
async def diagnose_route(request):
    """Diagnose execution blockers and categorize impediment causes."""
    from starlette.responses import JSONResponse
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
    else:
        body = dict(request.query_params)

    exit_code = int(body["exit_code"]) if body.get("exit_code") is not None else None
    items = diagnose_blockers(
        job_state=body.get("job_state"),
        state_reason=body.get("state_reason"),
        exit_code=exit_code,
        gcp_error=body.get("gcp_error"),
        error_log=body.get("error_log"),
        workload_profile=body.get("workload_profile"),
        slurm_job_details=body.get("slurm_job_details"),
    )
    return JSONResponse({"diagnostics": items})


@mcp.custom_route("/plans/compare", methods=["POST"])
async def compare_plans_route(request):
    """Evaluate, compare, price *and register* execution plans deterministically.

    Registration is what makes a plan approvable later. Without it this route was
    a second door into the product that produced plan ids the execution gate had
    never seen, so nothing compared here could ever be executed.
    """
    from starlette.responses import JSONResponse
    from .governance import get_governance_store
    from .models import ExecutionPlan
    try:
        body = await request.json()
    except Exception:
        body = {}

    profile = body.get("workload_profile") or body
    if not isinstance(profile, dict) or not profile.get("workload_id"):
        return JSONResponse(
            {
                "error": (
                    "workload_profile.workload_id is required: plans are registered "
                    "against a workload so they can later be approved and executed."
                )
            },
            status_code=400,
        )

    cluster_cpu = int(body.get("cluster_total_cpu", 128))
    try:
        result = evaluate_and_compare_plans(
            profile=profile,
            cluster_total_cpu=cluster_cpu,
            demo_mode=body.get("demo_mode"),
        )
    except ValueError as exc:
        return JSONResponse({"error": f"Invalid workload profile: {exc}"}, status_code=400)

    workload_id = profile["workload_id"]
    command = profile.get("command") or profile.get("script")

    # Persist the profile the plans were built from, so its declared constraints
    # remain enforceable against a submission that arrives with a wider profile.
    try:
        from .models import WorkloadProfile as _WorkloadProfile

        _history_store.save_workload_profile(_WorkloadProfile(**profile))
        result["profile_persisted"] = True
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not persist workload profile %s: %s", workload_id, exc)
        result["profile_persisted"] = False

    if result.get("plans"):
        gov = get_governance_store()
        for plan_dict in result["plans"]:
            try:
                gov.register_plan(workload_id, ExecutionPlan(**plan_dict), command=command)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Could not register plan %s: %s", plan_dict.get("plan_id"), exc)
        result["registered"] = True
    else:
        result["registered"] = False
        result["registration_skipped_reason"] = (
            "No compatible plan was produced, so nothing was registered for approval."
        )
    result["workload_id"] = workload_id
    return JSONResponse(result)


@mcp.custom_route("/plans/approve", methods=["POST"])
async def approve_plan_route(request):
    """Human-in-the-loop endpoint to approve a concrete execution plan.

    The approval is written to the governance store, which is the only source the
    execution gate consults. Writing only to history, as this route used to do,
    produced an approval that looked recorded but unlocked nothing.
    """
    from starlette.responses import JSONResponse
    from .governance import get_governance_store
    try:
        body = await request.json()
    except Exception:
        body = {}
    plan_id = body.get("plan_id")
    if not plan_id:
        return JSONResponse({"error": "plan_id is required"}, status_code=400)

    gov = get_governance_store()
    result = gov.approve_plan(
        plan_id,
        workload_id=body.get("workload_id"),
        approved_by=body.get("approved_by") or "operator",
    )
    if result["status"] == "not_found":
        return JSONResponse(result, status_code=404)
    if result["status"] == "workload_mismatch":
        return JSONResponse(result, status_code=409)

    try:
        _history_store.approve_execution_plan(plan_id)
        result["history_updated"] = True
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("History mirror of approval %s failed: %s", plan_id, exc)
        result["history_updated"] = False
    return JSONResponse(result)


@mcp.custom_route("/history", methods=["GET"])
async def history_route(request):
    """Query persistent execution history records and cost reconciliation."""
    from starlette.responses import JSONResponse
    params = request.query_params
    workload_id = params.get("workload_id")
    if workload_id:
        rec = _history_store.get_history_record(workload_id)
        if not rec:
            return JSONResponse({"error": f"Workload '{workload_id}' not found"}, status_code=404)
        comp = _history_store.reconcile_costs(workload_id)
        return JSONResponse({"record": rec.model_dump(), "comparison": comp})

    records = _history_store.list_history_records(limit=int(params.get("limit", 50)))
    return JSONResponse({"records": [r.model_dump() for r in records]})


@mcp.custom_route("/benchmarks", methods=["GET"])
async def benchmarks_route(request):
    """Get continuous benchmark metrics for runtime calibration."""
    from starlette.responses import JSONResponse
    key = request.query_params.get("key", "default")
    metrics = _history_store.get_benchmark_metrics(key)
    return JSONResponse({"benchmark": metrics})



@mcp.tool()
def submit_job(
    name: str = "agentgrid-workload",
    cpu: int | None = None,
    gpu: int = 0,
    partition: str | None = None,
    memory_mb: int | None = None,
    script: str | None = None,
    machine_type: str | None = None,
    provisioning_model: str | None = None,
    workload_id: str | None = None,
    plan_id: str | None = None,
) -> dict:
    """Submit or initialize a new compute workload to the cluster.

    Registers a new workload with optional CPU, GPU, partition, memory, machine type,
    provisioning model (SPOT/STANDARD), and script parameters.
    If CPU is omitted, the cluster selects an optimal baseline allocation.

    This is a direct mutation, so it is subject to the control mode the operator
    configured for the workload. In validation mode an approved ``plan_id`` is
    required; prefer ``execute_plan_controlled``, which also gives idempotency
    and history linkage.
    """
    target_workload = workload_id or name
    try:
        governing = enforce_mutation(target_workload, "submit_job", plan_id=plan_id)
    except MutationBlocked as blocked:
        payload = blocked.as_dict()
        payload["snapshot"] = _runtime.snapshot().model_dump()
        return payload

    if hasattr(_runtime, "submit_job"):
        try:
            res = _runtime.submit_job(
                name=name,
                cpu=cpu,
                gpu=gpu,
                partition=partition,
                memory_mb=memory_mb,
                script=script,
                machine_type=machine_type,
                provisioning_model=provisioning_model,
            )
            return {
                "status": "submitted",
                "job": res,
                "control_mode": governing["control_mode"],
                "snapshot": _runtime.snapshot().model_dump(),
            }
        except Exception as exc:
            return {
                "status": "error",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "control_mode": governing["control_mode"],
                "snapshot": _runtime.snapshot().model_dump(),
            }
    # No submit_job on the adapter is a real gap, not a success.
    return {
        "status": "unsupported",
        "reason": (
            f"The configured runtime adapter ({type(_runtime).__name__}) exposes no "
            f"submit_job; nothing was submitted."
        ),
        "control_mode": governing["control_mode"],
        "snapshot": _runtime.snapshot().model_dump(),
    }


@mcp.tool()
def get_runtime_snapshot() -> dict:
    """Observe the current compute runtime.

    Returns cluster capacity, the active workload, its objective, and
    deterministic candidate allocations with predicted finish time and cost.
    Call this before making a compute decision and again after advancing time.
    """
    return _runtime.snapshot().model_dump()


@mcp.tool()
def resize_workload(
    workload_id: str,
    cpu: int,
    machine_type: str | None = None,
    provisioning_model: str | None = None,
    reason: str = "requested by compute agent through MCP",
    plan_id: str | None = None,
) -> dict:
    """Resize a workload to one of the CPU allocations advertised by the snapshot.

    Allows scaling CPU cores, selecting prioritized machine types (e.g. N4 vs N2),
    and applying hedged provisioning models (e.g., SPOT, STANDARD, or 80% Spot / 20% Standard).
    Always call get_runtime_snapshot first and choose an advertised candidate.

    Subject to the operator's control mode: advisory refuses the resize, and
    validation requires an approved ``plan_id`` for this workload.
    """
    try:
        governing = enforce_mutation(workload_id, "resize_workload", plan_id=plan_id)
    except MutationBlocked as blocked:
        payload = blocked.as_dict()
        payload["cpu"] = cpu
        payload["snapshot"] = _runtime.snapshot().model_dump()
        return payload

    action = Action(
        action="resize_workload",
        workload_id=workload_id,
        cpu=cpu,
        reason=reason,
        machine_type=machine_type,
        provisioning_model=provisioning_model,
    )
    try:
        _runtime.apply(action)
    except Exception as exc:
        is_unsupported = "unsupported" in str(exc).lower() or (
            hasattr(_runtime, "verification_status") and getattr(_runtime, "verification_status") == "unsupported"
        )
        return {
            "status": "unsupported" if is_unsupported else "error",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "control_mode": governing["control_mode"],
            "workload_id": workload_id,
            "cpu": cpu,
            "machine_type": machine_type,
            "provisioning_model": provisioning_model,
            "slurm_verification": getattr(_runtime, "last_slurm_action", None),
            "snapshot": _runtime.snapshot().model_dump(),
        }
    verification = getattr(_runtime, "last_slurm_action", None)
    status = verification.get("status", "applied") if verification else "applied"
    return {
        "status": status,
        "control_mode": governing["control_mode"],
        "workload_id": workload_id,
        "cpu": cpu,
        "machine_type": machine_type,
        "provisioning_model": provisioning_model,
        "slurm_verification": verification,
        "snapshot": _runtime.snapshot().model_dump(),
    }


@mcp.tool()
def advance_time(minutes: float = 5.0) -> dict:
    """Advance the simulated runtime after a compute decision.

    In a real runtime this tool will disappear or become a wait/observe operation.
    For the simulator, use small increments (normally 5 minutes), then observe again.
    """
    max_step = float(os.getenv("MAX_ADVANCE_MINUTES", "60.0"))
    if minutes > max_step:
        raise ValueError(f"Advance time step {minutes}m exceeds maximum permitted ({max_step}m)")
    if minutes <= 0.0:
        raise ValueError("Advance time minutes must be greater than 0")
    _runtime.tick(minutes)
    return _runtime.snapshot().model_dump()


@mcp.tool()
def configure_objective(
    deadline_minutes: float | None = None,
    max_cost_eur: float | None = None,
    minimize_cost: bool | None = None,
) -> dict:
    """Configure or update the business objective (deadline, budget, strategy).

    Sets the targets against which candidate allocations and scheduling trade-offs
    are computed.
    """
    if hasattr(_runtime, "configure_objective"):
        _runtime.configure_objective(
            deadline_minutes=deadline_minutes,
            max_cost_eur=max_cost_eur,
            minimize_cost=minimize_cost,
        )
    return _runtime.snapshot().model_dump()


@mcp.tool()
def reset_runtime() -> dict:
    """Reset the demo runtime to its initial state."""
    _runtime.reset()
    return _runtime.snapshot().model_dump()


@mcp.tool()
def get_capacity_advice(
    machine_types: str = "n4-standard-32,n2-standard-32,n2-standard-16",
    size: int = 10,
    region: str | None = None,
    provisioning_model: str = "SPOT",
    target_distribution_shape: str = "ANY",
    demo_mode: bool | None = None,
) -> dict:
    """Get real-time Spot capacity advice, obtainability scores, and preemption risk from Google Cloud.

    Queries GCP Compute Engine Capacity Advisor (advice.capacity & advice.capacityHistory)
    to assess the likelihood of successfully provisioning Spot VMs, recommended zones,
    and historical preemption rates. Use this tool before making scaling decisions.
    """
    return query_capacity_advice(
        machine_types=machine_types,
        size=size,
        region=region,
        provisioning_model=provisioning_model,
        target_distribution_shape=target_distribution_shape,
        demo_mode=demo_mode,
    )




@mcp.tool()
def search_capacity(
    workload_id: str = "workload-1",
    cpu_requested: int = 4,
    gpu_requested: int = 0,
    memory_gb_requested: float = 16.0,
    allowed_regions: list[str] | None = None,
    allow_spot: bool = True,
    allow_standard: bool = True,
    demo_mode: bool | None = None,
) -> dict:
    """Search compatible cloud capacity across catalog, quotas, and Capacity Advisor availability signals.

    Traces candidates through a 4-stage lifecycle:
    1. catalog_proposed (hardware compatibility)
    2. quota_authorized (vCPU and region limits)
    3. capacity_estimated (preemption risk and obtainability score)
    4. actually_allocated (scheduled onto runtime)
    """
    candidates = search_compatible_capacity(
        workload_profile={"workload_id": workload_id, "cpu_requested": cpu_requested},
        cpu_requested=cpu_requested,
        gpu_requested=gpu_requested,
        memory_gb_requested=memory_gb_requested,
        allowed_regions=allowed_regions,
        allow_spot=allow_spot,
        allow_standard=allow_standard,
        demo_mode=demo_mode,
    )
    return {"candidates": [c.model_dump() for c in candidates]}


@mcp.tool()
def diagnose_blockers_tool(
    job_state: str | None = None,
    state_reason: str | None = None,
    exit_code: int | None = None,
    gcp_error: str | None = None,
    error_log: str | None = None,
    workload_profile: dict | None = None,
) -> dict:
    """Analyze telemetry to diagnose and categorize execution blockers.

    Categories: resource_waiting, priority, dependencies, quota,
    capacity_shortage, incompatible_configuration, application_error.
    Returns confirmed facts, origin sources, timestamps, and concrete remediation actions.
    """
    findings = diagnose_blockers(
        job_state=job_state,
        state_reason=state_reason,
        exit_code=exit_code,
        gcp_error=gcp_error,
        error_log=error_log,
        workload_profile=workload_profile,
    )
    return {"diagnostics": findings}


@mcp.tool()
def compare_plans(
    workload_profile: dict,
    cluster_total_cpu: int = 128,
    demo_mode: bool | None = None,
) -> dict:
    """Deterministically generate and compare up to 3 execution plans:
    1. cost_optimized: Minimal spend meeting deadline and budget
    2. deadline_favored: Fastest completion within budget
    3. balanced_tradeoff: Hedged allocation balancing cost and preemption SLA

    If no plan satisfies constraints, returns an explicit explanation of what blocks
    and suggested relaxations without inventing a winning plan.
    """
    return evaluate_and_compare_plans(
        profile=workload_profile,
        cluster_total_cpu=cluster_total_cpu,
        demo_mode=demo_mode,
    )


@mcp.tool()
def execute_plan_controlled(
    workload_profile: dict,
    plan: dict,
    control_mode: str = "validation",
    delegation_policy: dict | None = None,
    is_operator_approved: bool = False,
    approved_plan_id: str | None = None,
) -> dict:
    """Execute a plan governed by one of three control modes:
    - advisory: Strictly read-only; blocks all execution attempts
    - validation: Requires explicit operator approval for the concrete plan_id
    - delegation: Autonomous execution strictly bounded by DelegationPolicy guardrails

    Includes post-action verification, anti-duplicate idempotent checks, and fallback handling.
    """
    res = _execution_controller.submit_plan(
        profile=workload_profile,
        plan=plan,
        control_mode=control_mode,
        delegation_policy=delegation_policy,
        is_operator_approved=is_operator_approved,
        approved_plan_id=approved_plan_id,
        runtime=_runtime,
    )
    return res


@mcp.tool()
def track_workload_lifecycle(
    workload_id: str,
    progress_percent: float | None = None,
    job_state: str | None = None,
    # None, not 0.0: a status query that carries no figure must not overwrite the
    # recorded cost and duration with zeros.
    elapsed_minutes: float | None = None,
    cost_incurred_eur: float | None = None,
) -> dict:
    """Track workload state transitions, attempts, and observable progress.

    Reports progress percent if measured, or explicitly 'unavailable' if unmeasured.
    Handles checkpoint recovery when supported and enforces bounded retry limits.
    """
    # This used to call register_workload() for any workload missing from process
    # memory. After a restart that meant rebuilding an empty record on top of a
    # real run and then persisting it: a completed workload lost its cost, its
    # attempts and its final status. Storage is consulted instead, and a
    # genuinely unknown workload is reported as unknown.
    try:
        default_lifecycle_manager._require(workload_id)
    except KeyError:
        return {
            "workload_id": workload_id,
            "status": "unknown_workload",
            "reason": (
                f"No workload '{workload_id}' is registered or persisted. Define it "
                "before tracking it; inventing one here would overwrite nothing "
                "useful and hide the mistake."
            ),
        }

    rec = default_lifecycle_manager.update_progress(
        workload_id=workload_id,
        progress_percent=progress_percent,
        job_state=job_state,
        elapsed_minutes=elapsed_minutes,
        cost_incurred_eur=cost_incurred_eur,
        # Whatever arrives through this tool was stated by the caller -- often a
        # language model. A stated figure is recorded but never labelled as an
        # observation; the runtime is the only source of a measured cost.
        metrics_source="caller_declared",
    )
    return {
        "workload_id": workload_id,
        "status": "tracked",
        "state": rec["state"],
        "progress_percent": rec["progress_percent"],
        "progress_status": rec["progress_status"],
        "total_calculated_cost_eur": rec["total_calculated_cost_eur"],
        "total_elapsed_minutes": rec["total_elapsed_minutes"],
        "metrics_source": rec.get("metrics_source", "caller_declared"),
        "metrics_caveat": (
            "Figures passed to this tool are declared by the caller, not measured. "
            "They are stored with cost_status='estimated'. Only the runtime adapter "
            "produces a cost labelled calculated_from_usage."
        ),
    }


@mcp.tool()
def get_cost_history(
    workload_id: str | None = None,
    reconcile_billed_eur: float | None = None,
) -> dict:
    """Query persistent execution history, comparing 3-tier costs:
    - estimated_cost
    - calculated_from_usage
    - reconciled_billed (explicitly distinguished from calculated)

    A figure passed as ``reconcile_billed_eur`` arrives from whoever called this
    tool, which includes the language model. It is recorded as
    ``caller_supplied_unverified``, never as a reconciliation: there is no
    billing export integration behind it.
    """
    if workload_id:
        rec = _history_store.get_history_record(workload_id)
        comp = _history_store.reconcile_costs(
            workload_id,
            billed_cost_eur=reconcile_billed_eur,
            billed_cost_source="caller_supplied" if reconcile_billed_eur is not None else None,
        )
        result = {
            "record": rec.model_dump() if rec else None,
            "reconciliation": comp,
        }
        if reconcile_billed_eur is not None:
            result["billed_figure_caveat"] = (
                "The billed figure was supplied by the caller and has not been checked "
                "against any billing export."
            )
        return result
    records = _history_store.list_history_records()
    return {"records": [r.model_dump() for r in records]}


# Expose ASGI application for Uvicorn / Cloud Run
app = mcp.sse_app()


def main() -> None:
    # transport defaults to stdio locally, but switches to sse on Cloud Run via MCP_TRANSPORT=sse
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()
    if transport in ("sse", "http"):
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

