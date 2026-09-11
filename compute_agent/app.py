import json
import logging
import os
import time
import uuid
from typing import Any, Optional

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import BaseModel, Field

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
INDEX_HTML_PATH = os.path.join(STATIC_DIR, "index.html")

from .agent import MODEL, root_agent

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("agentic_compute.agent_service")

AGENTGRID_API_KEY: Optional[str] = os.getenv("AGENTGRID_API_KEY")


def verify_agent_auth(request: Request) -> None:
    """Verify incoming request credentials if AGENTGRID_API_KEY is configured."""
    key = globals().get("AGENTGRID_API_KEY") or os.getenv("AGENTGRID_API_KEY")
    if not key:
        return
    auth_header = request.headers.get("Authorization", "")
    key_header = request.headers.get("X-API-Key", "")
    expected = key.strip()

    if key_header == expected:
        return
    if auth_header.startswith("Bearer ") and auth_header[7:].strip() == expected:
        return

    raise HTTPException(
        status_code=401,
        detail="Unauthorized: invalid or missing AgentGrid authentication token or API key",
    )

app = FastAPI(
    title="Agentic Compute Control Plane - Agent Service",
    description="Cloud Run service running the Google ADK Agent that controls heterogeneous compute infrastructure via MCP.",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

runner = InMemoryRunner(agent=root_agent, app_name="agentic-compute")


class OptimizeRequest(BaseModel):
    objective: Optional[str] = Field(
        default=None,
        description="High-level optimization objective for the agent.",
        json_schema_extra={"example": "Run the compute workload autonomously. Meet its deadline and budget while minimizing cost."},
    )
    session_id: Optional[str] = Field(
        default=None,
        description="Optional session ID for tracing or multi-turn interaction.",
    )
    user_id: str = Field(
        default="cloud-run-user",
        description="Identifier of the user or system triggering optimization.",
    )


class ToolCallLog(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class OptimizeResponse(BaseModel):
    status: str
    session_id: str
    summary: str
    tool_calls: list[ToolCallLog]
    duration_seconds: float
    model: str
    mcp_server_url: Optional[str] = None


@app.get("/", response_model=None)
@app.get("/ui", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def get_dashboard_or_info(request: Request):
    """Serve the interactive AgentGrid UI for browsers, or API metadata for JSON clients."""
    accept = request.headers.get("accept", "")
    # If path is explicitly /ui or /dashboard, or if browser navigation requesting HTML
    if request.url.path in ("/ui", "/dashboard") or ("text/html" in accept and "application/json" not in accept):
        if os.path.exists(INDEX_HTML_PATH):
            with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
        return HTMLResponse("<h1>AgentGrid Dashboard</h1>")

    return {
        "service": "agentic-compute-agent",
        "agent_name": root_agent.name,
        "model": MODEL,
        "mcp_server_url": os.getenv("MCP_SERVER_URL", "local-stdio"),
        "status": "ready",
    }


@app.get("/api/snapshot")
def get_snapshot() -> dict[str, Any]:
    """Fetch current compute cluster and workload snapshot from MCP runtime."""
    mcp_server_url = os.getenv("MCP_SERVER_URL")
    if mcp_server_url:
        from urllib.parse import urlparse
        from .auth import get_gcp_id_token

        parsed = urlparse(mcp_server_url)
        base_audience = f"{parsed.scheme}://{parsed.netloc}"
        headers: dict[str, str] = {}
        token = get_gcp_id_token(base_audience)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = requests.get(f"{base_audience}/snapshot", headers=headers, timeout=5.0)
            if resp.status_code == 200:
                return resp.json()
        except Exception as exc:
            logger.warning("Failed to fetch snapshot from remote MCP: %s", exc)

    # Local development fallback
    try:
        from agentic_compute.mcp_server import _runtime
        return {
            "runtime": os.getenv("COMPUTE_RUNTIME", "simulator").lower(),
            "snapshot": _runtime.snapshot().model_dump(),
            "slurm_verification": getattr(_runtime, "last_slurm_action", None),
        }
    except Exception as exc:
        logger.error("Error accessing local runtime: %s", exc)
        return {"error": str(exc)}


@app.post("/api/reset")
def reset_runtime_state(request: Request) -> dict[str, Any]:
    """Reset the runtime and workload state."""
    verify_agent_auth(request)
    mcp_server_url = os.getenv("MCP_SERVER_URL")
    if mcp_server_url:
        from urllib.parse import urlparse
        from .auth import get_gcp_id_token

        parsed = urlparse(mcp_server_url)
        base_audience = f"{parsed.scheme}://{parsed.netloc}"
        headers: dict[str, str] = {}
        token = get_gcp_id_token(base_audience)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = requests.post(f"{base_audience}/reset", headers=headers, timeout=5.0)
            if resp.status_code == 200:
                return resp.json()
        except Exception as exc:
            logger.warning("Failed to reset remote MCP runtime: %s", exc)

    try:
        from agentic_compute.mcp_server import _runtime
        _runtime.reset()
        return {
            "status": "reset",
            "runtime": os.getenv("COMPUTE_RUNTIME", "simulator").lower(),
            "snapshot": _runtime.snapshot().model_dump(),
        }
    except Exception as exc:
        logger.error("Error resetting local runtime: %s", exc)
        return {"error": str(exc)}


@app.get("/api/capacity-advice")
def get_capacity_advice_endpoint(
    machine_types: str = "n4-standard-32,n2-standard-32,n2-standard-16",
    size: int = 10,
    region: Optional[str] = None,
    provisioning_model: str = "SPOT",
    target_distribution_shape: str = "ANY",
    demo_mode: Optional[bool] = None,
) -> dict[str, Any]:
    """Expose real-time GCP Spot Capacity Advisor to the UI and external callers."""
    mcp_server_url = os.getenv("MCP_SERVER_URL")
    if mcp_server_url:
        from urllib.parse import urlparse
        from .auth import get_gcp_id_token

        parsed = urlparse(mcp_server_url)
        base_audience = f"{parsed.scheme}://{parsed.netloc}"
        headers: dict[str, str] = {}
        token = get_gcp_id_token(base_audience)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            params: dict[str, Any] = {
                "machine_types": machine_types,
                "size": size,
                "region": region or "",
                "provisioning_model": provisioning_model,
                "target_distribution_shape": target_distribution_shape,
            }
            if demo_mode is not None:
                params["demo_mode"] = str(demo_mode).lower()
            resp = requests.get(
                f"{base_audience}/capacity-advice",
                headers=headers,
                params=params,
                timeout=5.0,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as exc:
            logger.warning("Failed to fetch capacity advice from remote MCP: %s", exc)

    from agentic_compute.capacity_advisor import query_capacity_advice
    return query_capacity_advice(
        machine_types=machine_types,
        size=size,
        region=region,
        provisioning_model=provisioning_model,
        target_distribution_shape=target_distribution_shape,
        demo_mode=demo_mode,
    )



@app.post("/api/capacity-search")
@app.get("/api/capacity-search")
async def search_capacity_endpoint(request: Request) -> dict[str, Any]:
    """Search compatible capacity candidates across catalog, quota, and capacity signals."""
    from agentic_compute.capacity_search import search_compatible_capacity
    data: dict[str, Any] = {}
    if request.method == "POST":
        try:
            data = await request.json()
        except Exception:
            data = {}

    qp = dict(request.query_params)
    merged = {**qp, **data}
    cpu_req = int(merged.get("cpu_requested", 4))
    gpu_req = int(merged.get("gpu_requested", 0))
    mem_req = float(merged.get("memory_gb_requested", 16.0))
    allow_spot = str(merged.get("allow_spot", "true")).lower() in ("true", "1")
    allow_std = str(merged.get("allow_standard", "true")).lower() in ("true", "1")
    demo_mode = str(merged.get("demo_mode", "false")).lower() in ("true", "1") if "demo_mode" in merged else None

    candidates = search_compatible_capacity(
        cpu_requested=cpu_req,
        gpu_requested=gpu_req,
        memory_gb_requested=mem_req,
        allow_spot=allow_spot,
        allow_standard=allow_std,
        demo_mode=demo_mode,
    )
    return {"candidates": [c.model_dump() for c in candidates]}


@app.post("/api/diagnose")
@app.get("/api/diagnose")
async def diagnose_endpoint(request: Request) -> dict[str, Any]:
    """Diagnose blockers across Slurm, GCP APIs, and application logs."""
    from agentic_compute.diagnostic import diagnose_blockers
    data = {}
    if request.method == "POST":
        try:
            data = await request.json()
        except Exception:
            data = {}
    else:
        data = dict(request.query_params)

    exit_code = int(data["exit_code"]) if data.get("exit_code") is not None else None
    items = diagnose_blockers(
        job_state=data.get("job_state"),
        state_reason=data.get("state_reason"),
        exit_code=exit_code,
        gcp_error=data.get("gcp_error"),
        error_log=data.get("error_log"),
        workload_profile=data.get("workload_profile"),
    )
    return {"diagnostics": items}


@app.post("/api/plans/compare")
async def compare_plans_endpoint(request: Request) -> dict[str, Any]:
    """Deterministically generate and compare execution plans."""
    from agentic_compute.plan_engine import evaluate_and_compare_plans
    try:
        body = await request.json()
    except Exception:
        body = {}

    profile = body.get("workload_profile") or body
    cluster_cpu = int(body.get("cluster_total_cpu", 128))
    res = evaluate_and_compare_plans(
        profile=profile,
        cluster_total_cpu=cluster_cpu,
        demo_mode=body.get("demo_mode"),
    )
    return res


@app.post("/api/plans/approve")
async def approve_plan_endpoint(request: Request) -> dict[str, Any]:
    """Approve a plan for execution in validation mode."""
    from agentic_compute.history import get_history_store
    try:
        body = await request.json()
    except Exception:
        body = {}
    plan_id = body.get("plan_id")
    if not plan_id:
        raise HTTPException(status_code=400, detail="plan_id is required")
    store = get_history_store()
    success = store.approve_execution_plan(plan_id)
    return {"status": "approved" if success else "not_found", "plan_id": plan_id}


@app.post("/api/execute-plan")
async def execute_plan_endpoint(request: Request) -> dict[str, Any]:
    """Execute a plan with control mode enforcement."""
    from agentic_compute.execution_controller import ExecutionController
    from agentic_compute.mcp_server import _runtime
    try:
        body = await request.json()
    except Exception:
        body = {}

    profile = body.get("workload_profile") or {}
    plan = body.get("plan") or {}
    control_mode = body.get("control_mode", "validation")
    delegation_policy = body.get("delegation_policy")
    is_approved = bool(body.get("is_operator_approved", False))
    approved_plan_id = body.get("approved_plan_id")

    controller = ExecutionController(_runtime)
    res = controller.submit_plan(
        profile=profile,
        plan=plan,
        control_mode=control_mode,
        delegation_policy=delegation_policy,
        is_operator_approved=is_approved,
        approved_plan_id=approved_plan_id,
        runtime=_runtime,
    )
    return res


@app.get("/api/history")
def get_history_endpoint(workload_id: Optional[str] = None, limit: int = 50) -> dict[str, Any]:
    """Fetch persistent execution history and cost reconciliations."""
    from agentic_compute.history import get_history_store
    store = get_history_store()
    if workload_id:
        rec = store.get_history_record(workload_id)
        if not rec:
            raise HTTPException(status_code=404, detail=f"Workload '{workload_id}' not found")
        comp = store.reconcile_costs(workload_id)
        return {"record": rec.model_dump(), "comparison": comp}
    records = store.list_history_records(limit=limit)
    return {"records": [r.model_dump() for r in records]}


@app.get("/api/benchmarks")
def get_benchmarks_endpoint(key: str = "default") -> dict[str, Any]:
    """Fetch continuous benchmark metrics."""
    from agentic_compute.history import get_history_store
    store = get_history_store()
    metrics = store.get_benchmark_metrics(key)
    return {"benchmark": metrics}


@app.get("/health")
def health() -> dict[str, str]:
    """Cloud Run health check probe."""
    return {
        "status": "healthy",
        "service": "agentic-compute-agent",
    }


@app.post("/optimize", response_model=OptimizeResponse)
@app.post("/run", response_model=OptimizeResponse)
async def optimize(req: OptimizeRequest, request: Request) -> OptimizeResponse:
    """Execute the compute optimization loop with Gemini and MCP tools."""
    verify_agent_auth(request)
    start_time = time.time()
    session_id = req.session_id or f"session-{uuid.uuid4().hex[:8]}"

    prompt = req.objective or (
        "Run the compute workload autonomously. "
        "Meet its deadline and budget while minimizing cost. "
        "Continue observing and adapting until it finishes, then summarize the result."
    )

    try:
        # Ensure session exists
        try:
            await runner.session_service.create_session(
                app_name=runner.app_name,
                user_id=req.user_id,
                session_id=session_id,
            )
        except Exception:
            # Session might already exist if re-used
            pass

        user_content = types.Content(
            role="user",
            parts=[types.Part.from_text(text=prompt)],
        )

        tool_calls: list[ToolCallLog] = []
        collected_texts: list[str] = []

        async for event in runner.run_async(
            user_id=req.user_id,
            session_id=session_id,
            new_message=user_content,
        ):
            # Record function calls made by Gemini
            for fc in event.get_function_calls():
                tool_calls.append(
                    ToolCallLog(
                        tool=fc.name,
                        args=fc.args or {},
                    )
                )

            # Record model text responses per Google ADK event spec (event.content)
            content = getattr(event, "content", None) or getattr(event, "message", None)
            if content and hasattr(content, "parts") and content.parts:
                for part in content.parts:
                    if getattr(part, "text", None):
                        collected_texts.append(part.text)

        summary = (
            "".join(collected_texts)
            if collected_texts
            else "Optimization run completed without textual summary."
        )

        duration = round(time.time() - start_time, 2)
        return OptimizeResponse(
            status="completed",
            session_id=session_id,
            summary=summary,
            tool_calls=tool_calls,
            duration_seconds=duration,
            model=MODEL,
            mcp_server_url=os.getenv("MCP_SERVER_URL"),
        )

    except Exception as exc:
        logger.exception("Error executing optimization loop: %s", exc)
        duration = round(time.time() - start_time, 2)
        raise HTTPException(
            status_code=500,
            detail={
                "error": str(exc),
                "duration_seconds": duration,
                "session_id": session_id,
            },
        )


@app.post("/optimize/stream")
@app.get("/optimize/stream")
async def optimize_stream(
    request: Request,
    objective: Optional[str] = None,
    session_id: Optional[str] = None,
    user_id: str = "cloud-run-user",
) -> StreamingResponse:
    """Stream optimization progress events in real-time via Server-Sent Events (SSE)."""
    verify_agent_auth(request)

    # Accept objective, session_id, and user_id from JSON body for POST requests
    final_objective = objective
    final_session_id = session_id
    final_user_id = user_id

    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                if body.get("objective"):
                    final_objective = body["objective"]
                if body.get("session_id"):
                    final_session_id = body["session_id"]
                if body.get("user_id"):
                    final_user_id = body["user_id"]
        except Exception:
            pass

    async def event_generator():
        start_time = time.time()
        sid = final_session_id or f"session-{uuid.uuid4().hex[:8]}"
        prompt = final_objective or (
            "Run the compute workload autonomously. "
            "Meet its deadline and budget while minimizing cost. "
            "Continue observing and adapting until it finishes, then summarize the result."
        )

        try:
            try:
                await runner.session_service.create_session(
                    app_name=runner.app_name,
                    user_id=final_user_id,
                    session_id=sid,
                )
            except Exception:
                pass

            user_content = types.Content(
                role="user",
                parts=[types.Part.from_text(text=prompt)],
            )

            # Send initial started event
            yield f"data: {json.dumps({'event': 'started', 'session_id': sid, 'model': MODEL})}\n\n"

            collected_texts: list[str] = []

            async for event in runner.run_async(
                user_id=final_user_id,
                session_id=sid,
                new_message=user_content,
            ):
                # Emit function calls as they occur
                for fc in event.get_function_calls():
                    payload = {
                        "event": "tool_call",
                        "tool": fc.name,
                        "args": fc.args or {},
                        "timestamp": round(time.time() - start_time, 2),
                    }
                    yield f"data: {json.dumps(payload)}\n\n"

                # Emit function responses (tool output/snapshots) if present
                if hasattr(event, "get_function_responses"):
                    for fr in event.get_function_responses():
                        payload = {
                            "event": "tool_response",
                            "tool": fr.name,
                            "response": fr.response or {},
                            "timestamp": round(time.time() - start_time, 2),
                        }
                        yield f"data: {json.dumps(payload)}\n\n"

                # Emit model text chunks per Google ADK event spec (event.content)
                content = getattr(event, "content", None) or getattr(event, "message", None)
                if content and hasattr(content, "parts") and content.parts:
                    for part in content.parts:
                        if getattr(part, "text", None):
                            collected_texts.append(part.text)
                            payload = {
                                "event": "thinking",
                                "text": part.text,
                                "timestamp": round(time.time() - start_time, 2),
                            }
                            yield f"data: {json.dumps(payload)}\n\n"

            summary = "".join(collected_texts) if collected_texts else "Optimization run completed."
            duration = round(time.time() - start_time, 2)
            completion_payload = {
                "event": "completed",
                "status": "completed",
                "session_id": sid,
                "duration_seconds": duration,
                "summary": summary,
            }
            yield f"data: {json.dumps(completion_payload)}\n\n"

        except Exception as exc:
            logger.exception("Error in optimize_stream: %s", exc)
            err_payload = {
                "event": "error",
                "error": str(exc),
                "duration_seconds": round(time.time() - start_time, 2),
            }
            yield f"data: {json.dumps(err_payload)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def main() -> None:
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("compute_agent.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()

