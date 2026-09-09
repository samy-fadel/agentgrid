from __future__ import annotations

from mcp.server.fastmcp import FastMCP

import os

from .models import Action
from .runtime import RuntimeAdapter
from .simulator import SimulatedRuntime
from .slurm_adapter import SlurmRuntime
from .capacity_advisor import query_capacity_advice


from mcp.server.transport_security import TransportSecuritySettings

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

    advice = query_capacity_advice(
        machine_types=machine_types,
        size=size,
        region=region,
        provisioning_model=provisioning_model,
        target_distribution_shape=target_distribution_shape,
    )
    return JSONResponse(advice)


def _create_runtime() -> RuntimeAdapter:
    runtime_type = os.getenv("COMPUTE_RUNTIME", "simulator").lower()
    if runtime_type == "slurm":
        return SlurmRuntime()
    return SimulatedRuntime()


_runtime: RuntimeAdapter = _create_runtime()


@mcp.tool()
def submit_job(
    name: str = "agentgrid-workload",
    cpu: int | None = None,
    gpu: int = 0,
    partition: str | None = None,
    memory_mb: int | None = None,
    script: str | None = None,
) -> dict:
    """Submit or initialize a new compute workload to the cluster.

    Registers a new workload with optional CPU, GPU, partition, memory, and script parameters.
    If CPU is omitted, the cluster selects an optimal baseline allocation.
    """
    if hasattr(_runtime, "submit_job"):
        try:
            res = _runtime.submit_job(
                name=name,
                cpu=cpu,
                gpu=gpu,
                partition=partition,
                memory_mb=memory_mb,
                script=script,
            )
            return {"status": "submitted", "job": res, "snapshot": _runtime.snapshot().model_dump()}
        except Exception as exc:
            return {
                "status": "error",
                "error": str(exc),
                "snapshot": _runtime.snapshot().model_dump(),
            }
    return {"status": "submitted", "snapshot": _runtime.snapshot().model_dump()}


@mcp.tool()
def get_runtime_snapshot() -> dict:
    """Observe the current compute runtime.

    Returns cluster capacity, the active workload, its objective, and
    deterministic candidate allocations with predicted finish time and cost.
    Call this before making a compute decision and again after advancing time.
    """
    return _runtime.snapshot().model_dump()


@mcp.tool()
def resize_workload(workload_id: str, cpu: int) -> dict:
    """Resize a workload to one of the CPU allocations advertised by the snapshot.

    This is a high-level control-plane action. The runtime validates the request.
    Always call get_runtime_snapshot first and choose an advertised candidate.
    """
    action = Action(
        action="resize_workload",
        workload_id=workload_id,
        cpu=cpu,
        reason="requested by compute agent through MCP",
    )
    try:
        _runtime.apply(action)
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
            "workload_id": workload_id,
            "cpu": cpu,
            "slurm_verification": getattr(_runtime, "last_slurm_action", None),
            "snapshot": _runtime.snapshot().model_dump(),
        }
    return {
        "status": "applied",
        "workload_id": workload_id,
        "cpu": cpu,
        "slurm_verification": getattr(_runtime, "last_slurm_action", None),
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
    )


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

