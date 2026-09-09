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


def verify_agent_auth(request: Request) -> None:
    """Verify authorization token or API key if AGENTGRID_API_KEY is configured."""
    expected_key = os.getenv("AGENTGRID_API_KEY")
    if not expected_key:
        return

    auth_header = request.headers.get("Authorization", "")
    api_key_header = request.headers.get("X-API-Key", "")

    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif api_key_header:
        token = api_key_header.strip()

    if token != expected_key:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Valid X-API-Key or Bearer token required to operate the compute control plane.",
        )


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
            resp = requests.get(
                f"{base_audience}/capacity-advice",
                headers=headers,
                params={
                    "machine_types": machine_types,
                    "size": size,
                    "region": region or "",
                    "provisioning_model": provisioning_model,
                    "target_distribution_shape": target_distribution_shape,
                },
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
    )


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

