import json
import logging
import os
import time
import uuid
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import BaseModel, Field

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


@app.get("/")
def get_service_info() -> dict[str, Any]:
    """Service status and configuration info."""
    return {
        "service": "agentic-compute-agent",
        "agent_name": root_agent.name,
        "model": MODEL,
        "mcp_server_url": os.getenv("MCP_SERVER_URL", "local-stdio"),
        "status": "ready",
    }


@app.get("/health")
def health() -> dict[str, str]:
    """Cloud Run health check probe."""
    return {
        "status": "healthy",
        "service": "agentic-compute-agent",
    }


@app.post("/optimize", response_model=OptimizeResponse)
@app.post("/run", response_model=OptimizeResponse)
async def optimize(req: OptimizeRequest) -> OptimizeResponse:
    """Execute the compute optimization loop with Gemini and MCP tools."""
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

            # Record model text responses
            if event.message and event.message.parts:
                for part in event.message.parts:
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
    objective: Optional[str] = None,
    session_id: Optional[str] = None,
    user_id: str = "cloud-run-user",
) -> StreamingResponse:
    """Stream optimization progress events in real-time via Server-Sent Events (SSE)."""
    async def event_generator():
        start_time = time.time()
        sid = session_id or f"session-{uuid.uuid4().hex[:8]}"
        prompt = objective or (
            "Run the compute workload autonomously. "
            "Meet its deadline and budget while minimizing cost. "
            "Continue observing and adapting until it finishes, then summarize the result."
        )

        try:
            try:
                await runner.session_service.create_session(
                    app_name=runner.app_name,
                    user_id=user_id,
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
                user_id=user_id,
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

                # Emit model text chunks
                if event.message and event.message.parts:
                    for part in event.message.parts:
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
