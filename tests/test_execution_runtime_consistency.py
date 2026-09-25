"""The runtime the dashboard *executes* on must be the one it *shows* and plans for.

Found by an end-to-end run against the deployed services on a real Slurm
cluster (partitions n2-standard-2 / c2-standard-60 / h3-standard-88, Standard
only):

* ``/api/plans/compare`` never passed ``runtime_kind``, so the Slurm filter in
  the plan engine was dead code: the dashboard recommended an
  ``n2-standard-16 / 100% Spot`` plan and labelled it ``executable_on_runtime``.
* ``/api/snapshot`` proxied the MCP server (Slurm) while ``/api/execute-plan``
  submitted to the agent service's in-process simulator, so a "verified"
  submission never reached the cluster and the Slurm view never moved.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentic_compute.plan_engine import SLURM_EXECUTABLE_MACHINE_TYPES, evaluate_and_compare_plans

SPOT_TOLERANT_PROFILE: dict[str, Any] = {
    "workload_id": "wl-runtime-consistency",
    "name": "runtime-consistency",
    "command": "sleep 20",
    "cpu_requested": 2,
    "memory_mb_requested": 4096,
    "deadline_minutes_from_start": 60,
    "budget_amount": 5,
    "is_parallelizable": True,
    "is_interruptible": True,
    "allow_spot": True,
}


def _runnable_on_slurm(plan: dict[str, Any]) -> bool:
    return (
        plan["machine_type"] in SLURM_EXECUTABLE_MACHINE_TYPES
        and "Spot" not in plan["provisioning_model"]
    )


def test_slurm_runtime_never_offers_a_plan_the_cluster_rejects() -> None:
    res = evaluate_and_compare_plans(profile=SPOT_TOLERANT_PROFILE, runtime_kind="slurm")
    assert res["plans"], res.get("unfeasible_explanation")
    for plan in res["plans"]:
        assert plan["executable_on_runtime"] is True
        assert _runnable_on_slurm(plan), (plan["machine_type"], plan["provisioning_model"])


def test_simulator_runtime_still_offers_spot_plans() -> None:
    """Control: the filter is runtime-specific, not a global Spot ban."""
    res = evaluate_and_compare_plans(profile=SPOT_TOLERANT_PROFILE, runtime_kind="simulator")
    assert any("Spot" in p["provisioning_model"] for p in res["plans"])


def test_compare_endpoint_plans_for_the_runtime_it_executes_on(monkeypatch) -> None:
    from compute_agent.app import app

    monkeypatch.setenv("COMPUTE_RUNTIME", "slurm")
    body = TestClient(app).post(
        "/api/plans/compare", json={"workload_profile": SPOT_TOLERANT_PROFILE}
    ).json()
    assert body["plans"]
    offending = [
        (p["machine_type"], p["provisioning_model"]) for p in body["plans"] if not _runnable_on_slurm(p)
    ]
    assert not offending, f"plans the Slurm cluster would reject: {offending}"


def test_compare_endpoint_without_runtime_setting_keeps_the_simulator(monkeypatch) -> None:
    from compute_agent.app import app

    monkeypatch.delenv("COMPUTE_RUNTIME", raising=False)
    body = TestClient(app).post(
        "/api/plans/compare", json={"workload_profile": SPOT_TOLERANT_PROFILE}
    ).json()
    assert any("Spot" in p["provisioning_model"] for p in body["plans"])


class _Resp:
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return dict(self._payload)


def test_snapshot_flags_a_slurm_view_next_to_simulated_execution(monkeypatch) -> None:
    """The exact production shape: MCP on Slurm, agent service on the simulator."""
    import compute_agent.app as agent_app
    import compute_agent.auth as auth

    monkeypatch.delenv("COMPUTE_RUNTIME", raising=False)
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.run.app/sse")
    monkeypatch.setattr(auth, "get_gcp_id_token", lambda audience: None)
    calls: list[str] = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return _Resp(200, {"runtime": "slurm", "snapshot": {"cluster": {}}, "slurm_verification": None})

    monkeypatch.setattr(agent_app.requests, "get", fake_get)

    body = TestClient(agent_app.app).get("/api/snapshot").json()
    assert calls == ["https://mcp.example.run.app/snapshot"]
    assert body["runtime"] == "slurm"
    assert body["execution_runtime"] == "simulator"
    assert body["runtime_mismatch"] is True
    assert "never reach" in body["runtime_warning"]


def test_snapshot_reads_the_local_runtime_when_it_executes_on_slurm(monkeypatch) -> None:
    """When this service submits to Slurm itself, it must watch its own jobs."""
    import agentic_compute.mcp_server as mcp_server
    import compute_agent.app as agent_app

    monkeypatch.setenv("COMPUTE_RUNTIME", "slurm")
    monkeypatch.setenv("MCP_SERVER_URL", "https://mcp.example.run.app/sse")

    class _Snap:
        def model_dump(self) -> dict[str, Any]:
            return {"workload": {"id": "4242", "status": "RUNNING"}}

    class _StubRuntime:
        last_slurm_action = {"job_id": "4242", "verification_status": "verified"}

        def snapshot(self) -> _Snap:
            return _Snap()

    # The module-level runtime is built at import time from whatever
    # COMPUTE_RUNTIME held then; stub it so the test does not depend on order.
    monkeypatch.setattr(mcp_server, "_runtime", _StubRuntime())

    def must_not_proxy(*args, **kwargs):  # pragma: no cover - failure path
        raise AssertionError("the snapshot was proxied instead of read from the executing runtime")

    monkeypatch.setattr(agent_app.requests, "get", must_not_proxy)

    body = TestClient(agent_app.app).get("/api/snapshot").json()
    assert body["snapshot_source"] == "local_runtime"
    assert body["execution_runtime"] == "slurm"
    assert body["snapshot"]["workload"]["id"] == "4242"
    assert body["slurm_verification"]["job_id"] == "4242"
    assert "runtime_mismatch" not in body


@pytest.mark.parametrize("value, expected", [(None, "simulator"), ("SLURM", "slurm"), ("  ", "simulator")])
def test_execution_runtime_kind_normalises_the_setting(monkeypatch, value, expected) -> None:
    from compute_agent.app import _execution_runtime_kind

    if value is None:
        monkeypatch.delenv("COMPUTE_RUNTIME", raising=False)
    else:
        monkeypatch.setenv("COMPUTE_RUNTIME", value)
    assert _execution_runtime_kind() == expected
