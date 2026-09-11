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

    history = importlib.import_module("agentic_compute.history")
    governance = importlib.import_module("agentic_compute.governance")

    # Drop cached singletons so they re-open against the new path.
    history._history_store = None
    governance.reset_governance_store()

    yield db_path

    history._history_store = None
    governance.reset_governance_store()
