from __future__ import annotations

from mcp.server.fastmcp import FastMCP

import os

from .models import Action
from .runtime import RuntimeAdapter
from .simulator import SimulatedRuntime
from .slurm_adapter import SlurmRuntime


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



def _create_runtime() -> RuntimeAdapter:
    runtime_type = os.getenv("COMPUTE_RUNTIME", "simulator").lower()
    if runtime_type == "slurm":
        return SlurmRuntime()
    return SimulatedRuntime()


_runtime: RuntimeAdapter = _create_runtime()


@mcp.tool()
def submit_job(name: str = "agentgrid-workload", cpu: int = 32) -> dict:
    """Submit or initialize a new compute workload to the cluster.

    Registers a new workload with the requested CPU allocation and returns the
    initial runtime snapshot.
    """
    if hasattr(_runtime, "submit_job"):
        res = _runtime.submit_job(name=name, cpu=cpu)
        return {"status": "submitted", "job": res, "snapshot": _runtime.snapshot().model_dump()}
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
    _runtime.apply(action)
    return {
        "status": "applied",
        "workload_id": workload_id,
        "cpu": cpu,
        "snapshot": _runtime.snapshot().model_dump(),
    }


@mcp.tool()
def advance_time(minutes: float = 5.0) -> dict:
    """Advance the simulated runtime after a compute decision.

    In a real runtime this tool will disappear or become a wait/observe operation.
    For the simulator, use small increments (normally 5 minutes), then observe again.
    """
    if minutes > 5.0:
        raise ValueError("V0 simulator permits at most 5 minutes per control-loop step")
    _runtime.tick(minutes)
    return _runtime.snapshot().model_dump()


@mcp.tool()
def reset_runtime() -> dict:
    """Reset the demo runtime to its initial state."""
    _runtime.reset()
    return _runtime.snapshot().model_dump()


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

