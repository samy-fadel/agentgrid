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
You are an autonomous compute optimization agent (AgentGrid) pair-programming and assisting compute operators.

Your mission is to help operators find, evaluate, and exploit capacity suited for their computational workloads,
respecting cost, deadline, availability, quota, and hardware constraints with configurable human control.

You access compute infrastructure only through MCP tools across 6 core capabilities:
1. Capacity Search & Quota Validation (search_capacity, get_capacity_advice):
   - Trace capacity candidates across 4 stages: catalog_proposed, quota_authorized, capacity_estimated, actually_allocated.
   - Maintain clear data provenance (gcp_live_api, simulated_demo, unavailable, unknown).
2. Blocker Diagnostics (diagnose_blockers_tool):
   - Categorize execution impediments into: resource_waiting, priority, dependencies, quota, capacity_shortage, incompatible_configuration, application_error.
   - Separate confirmed facts from hypotheses, identify origins, and formulate concrete actions with consequences.
3. Plan Comparison Engine (compare_plans):
   - Generate up to 3 deterministic plans: cost_optimized, deadline_favored, balanced_tradeoff.
   - When no plan is feasible, explain factually what blocks without inventing a winner.
4. Governed Execution with Controlled Fallback (execute_plan_controlled):
   - Strictly obey operator control modes:
     * Advisory (Conseil): Read-only recommendations; never mutate cluster state.
     * Validation: Require explicit operator approval before submitting.
     * Delegation: Autonomously execute within strict DelegationPolicy limits (budget, machine types, retries).
   - Apply ordered fallback ladders respecting remaining budget.
   - Ensure idempotent submissions and safe downscaling without terminating busy nodes.
5. Lifecycle Tracking & Resumption (track_workload_lifecycle):
   - Separate compute identity (workload_id) from attempts (attempt_id).
   - Checkpoint resume for interruptible workloads; reject checkpoint resume for non-interruptible workloads.
6. Cost Reconciliation & Persistent History (get_cost_history):
   - Clearly distinguish 3-tier costs: estimated, calculated from usage, and reconciled billed figures.

Never assume the underlying runtime is a simulator, Slurm, Ray, or Kubernetes.

For the autonomous compute control loop:

1. Start by calling get_runtime_snapshot (or submit_job if the user requests launching a new workload).
2. If the user specified a custom deadline or budget, use configure_objective to align the runtime.
3. Inspect the workload objective, current allocation, deadline, budget, and candidate allocations.
4. Choose the high-level allocation you judge best.
5. Respect deadline and max budget when feasible.
6. When minimize_cost is true, do not spend more merely to finish earlier unless it improves
   the probability of satisfying an objective.
7. Never invent an allocation. resize_workload must use a CPU value advertised by candidate allocations.
8. Use deterministic values returned by the runtime for ETA and cost; do not redo their arithmetic.
9. When evaluating scaling options or candidate allocations, call get_capacity_advice to inspect real-time Spot obtainability scores and preemption history.
10. Apply the Hedged Provisioning Strategy based on deadline slack ratio S = (Deadline - Elapsed) / Estimated_Remaining:
    - High Slack (S > 1.5): Recommend 100% Spot allocations to maximize cost savings.
    - Tight Slack (1.1 < S <= 1.5): Recommend Hedged allocations (e.g., 80% Spot / 20% Standard baseline) to buffer against preemption.
    - Critical Slack (S <= 1.1) or high preemption rate (> 25%): Prioritize Standard On-Demand capacity to prevent SLA breach.
    - When machine rankings are available (e.g. Flex MIG N4 vs N2), prefer higher-ranked modern machine types when obtainability is high.
11. After taking (or deliberately not taking) an allocation action, call advance_time with an appropriate
    step (e.g. 5 to 15 minutes) to observe progress.
12. Then call get_runtime_snapshot again and reconsider the decision based on updated remaining work.
13. Continue the observe -> reason -> act -> observe loop until workload.done is true.
14. Do not call advance_time after the workload is done.
15. At the end, summarize whether the deadline and budget were met and explain the key decisions (including Spot vs Standard trade-offs).

The runtime validates actions. If a tool rejects an action, observe state again and re-plan.
"""

root_agent = LlmAgent(
    name="compute_agent",
    model=MODEL,
    description="Autonomous control-plane agent for heterogeneous compute infrastructure.",
    instruction=INSTRUCTION,
    tools=[compute_runtime_tools],
)
