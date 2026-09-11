"""Shared pytest fixtures for the AgentGrid suite.

Rationale
---------
Governance and history now persist state in SQLite (approvals, submission
ledger, execution attempts).  Without isolation, a plan approved by one test
would still be approved in the next one, and the idempotency ledger would make
a legitimate submission look like a replay.  Both effects would *hide* real
regressions, which is exactly what this suite must not do.

The autouse fixture therefore gives every test a private database file and
resets the module-level singletons that cache a connection to it.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def isolated_agentgrid_state(tmp_path, monkeypatch):
    """Point governance/history at a per-test SQLite file."""
    db_path = tmp_path / "agentgrid_test.db"
    monkeypatch.setenv("AGENTGRID_DB_PATH", str(db_path))

    # Plan comparison consults the Compute Engine quota API for its capacity
    # dimension. Credentials exist on some developer machines, so left enabled
    # the suite would make live calls and its results would depend on a real
    # project's quota. Switched off here the same way MOCK_SLURM switches off
    # the real controller; the tests that exercise the capacity dimension turn
    # it back on explicitly and stub the quota reader.
    monkeypatch.setenv("AGENTGRID_PLAN_CAPACITY_CHECK", "false")

    history = importlib.import_module("agentic_compute.history")
    governance = importlib.import_module("agentic_compute.governance")
    lifecycle = importlib.import_module("agentic_compute.lifecycle_manager")
    capacity_advisor = importlib.import_module("agentic_compute.capacity_advisor")

    # Drop cached singletons so they re-open against the new path.
    history._history_store = None
    governance.reset_governance_store()
    # The module-level manager also caches workloads in memory. Left in place,
    # a workload registered by an earlier test stays visible after the database
    # has been swapped, and a test could pass on stale memory instead of on
    # persisted state.
    lifecycle.default_lifecycle_manager._workloads.clear()
    # Quota reads are cached for a short window so that one search does not
    # re-fetch the same regional document once per machine type. Across tests
    # that window is a leak: a stubbed response from one test would be served
    # to the next, which asserts on a *different* stub.
    capacity_advisor.reset_quota_cache()

    yield db_path

    history._history_store = None
    governance.reset_governance_store()
    lifecycle.default_lifecycle_manager._workloads.clear()
    capacity_advisor.reset_quota_cache()
