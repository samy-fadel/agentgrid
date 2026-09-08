# Agentic Compute

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

**An open-source autonomous control plane for heterogeneous compute infrastructure.**

> Give the system an objective, not a resource request.

This version is intentionally **Google-native**:

- **Gemini** = reasoning model
- **Google ADK** = agent framework
- **MCP** = boundary between the agent and compute runtimes
- **Vertex AI** = recommended model backend
- **Simulator** = built-in zero-dependency simulation engine (local / dev)
- **Slurm** = production GCP HPC runtime adapter via slurmrestd

## Architecture

```text
                 User objective
                       |
                       v
              +------------------+
              | Gemini via ADK   |
              | Compute Agent    |
              +--------+---------+
                       |
                  McpToolset
                       |
              MCP (stdio or SSE)
                       |
              +--------+---------+
              | Compute MCP      |
              | Server           |
              +--------+---------+
                       |
                 RuntimeAdapter
                       |
             +---------+---------+
             |                   |
       SimulatedRuntime      SlurmRuntime
        (Local / Dev)       (GCP / Prod)
```

The agent never sees Slurm commands, Ray APIs, Kubernetes objects, or simulator internals.
It sees a stable **compute semantic model**.

## Core abstractions

The runtime is represented with a small universal vocabulary:

- `ClusterState`
- `WorkloadState`
- `Objective`
- `CandidateAllocation`
- `RuntimeSnapshot`
- `Action`

That model is deliberately independent from Gemini, ADK, MCP and Slurm.

## Why both ADK and MCP?

They solve different problems.

**ADK** manages the agent:
- Gemini model
- reasoning loop
- tool use
- sessions
- evaluation/deployment path

**MCP** decouples that agent from the runtime:
- tool discovery
- structured tool calls
- runtime boundary
- future Slurm/Ray/Kubernetes adapters

The system supports **dual transports**:
- **Local development**: Launches the MCP server as a local subprocess over **stdio**.
- **Production (Cloud Run)**: Connects remotely to the MCP service over **SSE (Server-Sent Events)** with Google IAM OIDC authentication.

## Runtimes: Simulator vs Real Slurm

| Capability | `COMPUTE_RUNTIME=simulator` | `COMPUTE_RUNTIME=slurm` |
| :--- | :--- | :--- |
| **Purpose** | Fast local testing, CI/CD verification, zero setup | Live Slurm cluster on Google Cloud |
| **Prerequisites** | None (pure Python discrete-event simulation) | GCP VPC with `slurmrestd` |
| **Workload** | Synthetic Monte Carlo task (`mc-001`) | Real batch job ID (`SLURM_JOB_ID`) |
| **Transport** | `stdio` (local subprocess) or `sse` | `sse` on Cloud Run via Direct VPC Egress |
| **Actions** | Deterministic state progression | Real Slurm updates via REST API |

## What the demo does

The simulated cluster starts with:

- 128 CPUs
- one Monte-Carlo-style workload
- 32 CPUs currently allocated
- a deadline
- a maximum cost
- candidate allocations with deterministic runtime/cost predictions

The MCP server exposes these tools:

```text
get_runtime_snapshot()
resize_workload(workload_id, cpu)
advance_time(minutes)
reset_runtime()
```

The agent is instructed to:

1. observe the runtime;
2. reason about the workload objective;
3. choose a high-level allocation;
4. act through MCP;
5. advance simulated time;
6. observe again;
7. adapt until the workload finishes.

The **LLM makes the strategy decision**.
The simulator only measures, predicts, validates and executes.

## Setup with Google Cloud / Vertex AI

Python 3.11+ is required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Authenticate with Google Cloud:

```bash
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
```

Then edit `.env`:

```text
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
GOOGLE_CLOUD_LOCATION=us-central1
AGENTIC_COMPUTE_MODEL=gemini-2.5-flash
```

## Run the MCP server alone

Useful for inspecting the runtime boundary:

```bash
agentic-compute-mcp
```

Or with the MCP development tooling:

```bash
mcp dev src/agentic_compute/mcp_server.py
```

## Run the ADK agent

From the repository root:

```bash
adk web .
```

Open the ADK web UI and select `compute_agent`.

Ask:

```text
Run the simulated workload autonomously.
Meet its deadline and budget while minimizing cost.
Continue observing and adapting until it finishes, then summarize the result.
```

You can also use the ADK CLI:

```bash
adk run compute_agent
```

## The agent

`compute_agent/agent.py` is deliberately small.

It contains:
- Gemini model selection
- system instruction
- one `McpToolset`

It contains **no runtime-specific implementation**.

That is the architectural test.

## Project structure

```text
├── cloudbuild.yaml           # CI/CD pipeline for Cloud Run deployment
├── Dockerfile.agent          # Container for Cloud Run Agent service
├── Dockerfile.mcp            # Container for Cloud Run MCP Server
├── pyproject.toml            # Package configuration & dependencies
│
├── compute_agent/            # ADK Agent Service
│   ├── agent.py              # Google ADK root_agent (supports SSE & stdio)
│   ├── app.py                # FastAPI HTTP entrypoint for Cloud Run
│   └── auth.py               # GCP OIDC ID token helper for IAM auth
│
├── src/agentic_compute/      # Domain models & MCP Server
│   ├── models.py             # Universal compute semantics
│   ├── runtime.py            # RuntimeAdapter base interface
│   ├── simulator.py          # Deterministic simulated backend
│   ├── slurm_adapter.py      # Slurm REST adapter for GCP Slurm
│   └── mcp_server.py         # MCP boundary (FastMCP over SSE & stdio)
│
├── scripts/
│   └── setup_ci_cd.sh        # Automation script to set up GCP trigger & IAM
│
└── tests/                    # Automated test suite
    ├── test_agent_service.py # Tests for Cloud Run agent API
    ├── test_mcp_contract.py  # Tests for MCP server and health probes
    ├── test_simulator.py     # Tests for simulator runtime
    └── test_slurm.py         # Tests for Slurm adapter
```

## Deterministic vs agentic responsibilities

```text
DETERMINISTIC                       AGENTIC

measure cluster state               choose strategy
predict candidate ETA               trade off objectives
predict candidate cost              adapt after new observations
validate requested action           decide when to resize
apply action safely                 explain decision
```

The project should never ask Gemini to do arithmetic that a deterministic function can do reliably.

## Cloud Run & Slurm Architecture

In production on Google Cloud:

```text
[ GitHub Repository ]
         │ (git push to main)
         ▼
  [ Cloud Build ] ── Runs pytest ── Builds & Pushes Containers
         │
         ├─── Deploy ──► [ Cloud Run: Agent Service ]
         │                      │
         │                      ▼ (HTTPS + IAM OIDC)
         └─── Deploy ──► [ Cloud Run: MCP Server ]
                                │
                                ▼ (Direct VPC Egress)
                         [ Slurm Cluster (GCP VPC) ]
```

### Setting up CI/CD with Cloud Build

1. Configure prerequisites and create the GitHub trigger:

```bash
chmod +x scripts/setup_ci_cd.sh
./scripts/setup_ci_cd.sh
```

2. Every push to `main` on [github.com/samy-fadel/agentgrid](https://github.com/samy-fadel/agentgrid) will automatically:
   - Run unit and integration tests (`pytest`)
   - Build container images for both services
   - Push images to Google Artifact Registry
   - Deploy the MCP Server to Cloud Run with Direct VPC Egress into your Slurm VPC
   - Deploy the Agent to Cloud Run and configure IAM service-to-service authorization

### Using the Agent on Cloud Run

#### Option 1: Interactive Web UI (Swagger UI)
Visit the interactive Swagger UI directly in your browser:
```text
https://<YOUR_AGENT_SERVICE_URL>.a.run.app/docs
```
You can inspect schemas, test custom objectives, and trigger optimization runs with a single click.

#### Option 2: HTTP API
```bash
curl -X POST https://<YOUR_AGENT_SERVICE_URL>.a.run.app/optimize \
  -H "Content-Type: application/json" \
  -d '{"objective": "Run the compute workload autonomously, minimize cost and respect deadline."}'
```


## License

This project is licensed under the Apache License, Version 2.0. See the [LICENSE](LICENSE) file for details.
