"""What the agent can actually reach through MCP must match what the code can do.

Two gaps found by inspecting the tool surface rather than the library:

* ``diagnose_blockers`` was taught to read a full ``slurm_job_details`` record
  (defect 2-A), and ``POST /api/diagnose`` forwards it, but the MCP tool
  ``diagnose_blockers_tool`` never exposed the parameter. For the agent -- the
  only consumer of the MCP surface -- the fix was unreachable::

      diagnose_blockers_tool params: ['job_state', 'state_reason', 'exit_code',
                                      'gcp_error', 'error_log', 'workload_profile']
      diagnose_blockers      params: [..., 'slurm_job_details', 'timestamp']

* ``capacity_search.check_project_quota`` read ``res["limit"]`` and
  ``res["usage"]`` while the verdict exposes ``quota_limit`` / ``quota_usage``,
  so it always returned ``(status, None, None)``: the numbers behind the verdict
  were silently dropped.
"""

from __future__ import annotations

import inspect

from agentic_compute import mcp_server
from agentic_compute.capacity_search import check_project_quota
from agentic_compute.diagnostics import diagnose_blockers

SLURM_RECORD = {
    "job_state": ["PENDING"],
    "state_reason": "BadConstraints",
    "admin_comment": "ZONE_RESOURCE_POOL_EXHAUSTED: no n2-standard-16 left",
    "dependency": "afterok:4700",
}


def test_the_mcp_tool_exposes_every_diagnostic_input_the_library_accepts():
    tool_params = set(inspect.signature(mcp_server.diagnose_blockers_tool).parameters)
    library_params = set(inspect.signature(diagnose_blockers).parameters)

    missing = library_params - tool_params - {"timestamp"}
    assert not missing, f"the agent cannot pass: {sorted(missing)}"


def test_the_mcp_tool_actually_reads_the_slurm_record():
    result = mcp_server.diagnose_blockers_tool(slurm_job_details=SLURM_RECORD)

    findings = result["diagnostics"]
    assert findings, result
    categories = {f["category"] for f in findings}
    assert "unknown" not in categories, findings
    # The provider error that Slurm-GCP writes into admin_comment must be classified.
    assert "capacity_shortage" in categories, categories
    assert any("ZONE_RESOURCE_POOL_EXHAUSTED" in f["observed_facts"] for f in findings)


def test_the_mcp_tool_and_the_library_agree():
    """Same input, same verdict: the tool must not be a lossy re-implementation."""
    through_tool = mcp_server.diagnose_blockers_tool(slurm_job_details=SLURM_RECORD)["diagnostics"]
    direct = diagnose_blockers(slurm_job_details=SLURM_RECORD)

    assert [f["category"] for f in through_tool] == [f["category"] for f in direct]


def test_check_project_quota_returns_the_numbers_it_promises():
    status, limit, usage = check_project_quota(
        region="us-central1", required_cpus=4, provisioning_model="STANDARD"
    )

    assert status in ("QUOTA_AVAILABLE", "QUOTA_EXCEEDED", "QUOTA_UNKNOWN")
    if status != "QUOTA_UNKNOWN":
        assert limit is not None, "a known verdict must carry the limit it was derived from"
        assert usage is not None
