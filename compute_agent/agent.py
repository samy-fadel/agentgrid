from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv(override=True)

# Argolis / Vertex AI settings: disable mTLS proxy fallback and ensure clean auth
os.environ.setdefault("GOOGLE_API_USE_MTLS_ENDPOINT", "never")
os.environ.setdefault("GOOGLE_API_USE_CLIENT_CERTIFICATE", "false")
if os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE":
    os.environ.pop("GEMINI_API_KEY", None)

from google.adk.agents import LlmAgent
from google.adk.tools import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import SseConnectionParams, StdioConnectionParams
from mcp import StdioServerParameters

from .auth import get_gcp_id_token


MODEL = os.getenv("AGENTIC_COMPUTE_MODEL", "gemini-2.5-flash")


def get_mcp_toolset() -> McpToolset:
    """Configure McpToolset for either remote Cloud Run (SSE) or local subprocess (stdio)."""
    mcp_server_url = os.getenv("MCP_SERVER_URL")

    if mcp_server_url:
        # Remote Cloud Run MCP instance via SSE
        sse_url = (
            mcp_server_url
            if mcp_server_url.endswith("/sse")
            else f"{mcp_server_url.rstrip('/')}/sse"
        )
        headers: dict[str, str] = {}

        # Extract base service URL as the GCP audience for IAM OIDC token
        from urllib.parse import urlparse

        parsed = urlparse(mcp_server_url)
        base_audience = f"{parsed.scheme}://{parsed.netloc}"

        token = get_gcp_id_token(base_audience)
        if token:
            headers["Authorization"] = f"Bearer {token}"

        return McpToolset(
            connection_params=SseConnectionParams(
                url=sse_url,
                headers=headers,
                timeout=float(os.getenv("MCP_TIMEOUT", "60.0")),
                sse_read_timeout=float(os.getenv("MCP_SSE_READ_TIMEOUT", "1800.0")),
            )
        )

    # Local development / fallback via stdio subprocess
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=["-m", "agentic_compute.mcp_server"],
            ),
            timeout=float(os.getenv("MCP_TIMEOUT", "10.0")),
        )
    )


compute_runtime_tools = get_mcp_toolset()


INSTRUCTION = """
You are an autonomous compute optimization agent.

Your job is NOT to perform HPC scheduling at the task/node level.
Your job is to choose high-level compute strategies that satisfy workload objectives.

You access compute infrastructure only through MCP tools.
Never assume the underlying runtime is a simulator, Slurm, Ray, or Kubernetes.

For the current V0 demo:

1. Start by calling get_runtime_snapshot.
2. Inspect the workload objective, current allocation, deadline, budget, and candidate allocations.
3. Choose the high-level allocation you judge best.
4. Respect deadline and max budget when feasible.
5. When minimize_cost is true, do not spend more merely to finish earlier unless it improves
   the probability of satisfying an objective.
6. Never invent an allocation. resize_workload must use a CPU value advertised by the snapshot.
7. Use deterministic values returned by the runtime for ETA and cost; do not redo their arithmetic.
8. After taking (or deliberately not taking) an allocation action, call advance_time with at most
   5 minutes.
9. Then call get_runtime_snapshot again and reconsider the decision.
10. Continue the observe -> reason -> act -> observe loop until workload.done is true.
11. Do not call advance_time after the workload is done.
12. At the end, summarize whether the deadline and budget were met and explain the key decisions.

The runtime validates actions. If a tool rejects an action, observe state again and re-plan.
"""

root_agent = LlmAgent(
    name="compute_agent",
    model=MODEL,
    description="Autonomous control-plane agent for heterogeneous compute infrastructure.",
    instruction=INSTRUCTION,
    tools=[compute_runtime_tools],
)
