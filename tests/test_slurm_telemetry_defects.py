"""Targeted reproductions of the Slurm / telemetry defects raised by the audit.

Each test names the defect it reproduces. They are written to fail on the
pre-fix behaviour, so they are evidence of a correction rather than of a status
string. Nothing here talks to a real cluster: every HTTP call is stubbed, and
the tests assert on what the adapter *claims to observe*, which is precisely
where the dishonesty was.
"""

from __future__ import annotations

import pytest

from agentic_compute.models import Action
from agentic_compute.slurm_adapter import SlurmRuntime


class _Resp:
    def __init__(self, status_code=200, payload=None, text="ok"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def _offline(monkeypatch, get_payload=None, get_status=404, post_status=200, post_payload=None):
    monkeypatch.setenv("MOCK_SLURM", "true")
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: _Resp(get_status, get_payload),
    )
    monkeypatch.setattr(
        "requests.post",
        lambda *a, **k: _Resp(post_status, post_payload or {"job_id": "12345"}),
    )


# ---------------------------------------------------------------------------
# Case 1: resize with the wrong workload identity
# ---------------------------------------------------------------------------

def test_case1_resize_with_wrong_workload_id_is_rejected_before_any_mutation(monkeypatch):
    """A resize aimed at another job must be refused before any API call.

    ``apply`` used to ignore ``action.workload_id`` entirely and mutate whatever
    job the adapter happened to track.
    """
    posted: list = []
    _offline(monkeypatch)
    monkeypatch.setattr(
        "requests.post", lambda *a, **k: (posted.append(a), _Resp(200, {}))[1]
    )

    rt = SlurmRuntime(job_id="job-real-1")
    rt.job_status = "PENDING"

    with pytest.raises(ValueError, match="workload"):
        rt.apply(
            Action(
                action="resize_workload",
                workload_id="job-somebody-else",
                cpu=64,
                machine_type="c2-standard-60",
                reason="typo in the workload id",
            )
        )

    assert posted == [], "no mutation must be attempted for an unknown workload"
    assert rt.requested_cpu is None
    assert rt.verification_status != "pending_verification"


# ---------------------------------------------------------------------------
# Case 2: impossible CPU request
# ---------------------------------------------------------------------------

def test_case2_request_beyond_cluster_capacity_is_explicitly_rejected(monkeypatch):
    """999,999 CPUs on a 128-CPU cluster must be refused with a clear reason."""
    posted: list = []
    _offline(monkeypatch)
    monkeypatch.setattr(
        "requests.post", lambda *a, **k: (posted.append(a), _Resp(200, {}))[1]
    )

    rt = SlurmRuntime(job_id="job-real-2")
    rt.job_status = "PENDING"
    rt.total_cpu = 128

    with pytest.raises(RuntimeError) as exc:
        rt.apply(
            Action(
                action="resize_workload",
                workload_id="job-real-2",
                cpu=999_999,
                machine_type="c2-standard-60",
                reason="absurd request",
            )
        )

    message = str(exc.value).lower()
    assert "999999" in message or "999,999" in message
    assert "128" in message
    assert posted == [], "an impossible resize must not reach the cluster"
    assert rt.last_slurm_action["status"] in ("unsupported", "rejected")


# ---------------------------------------------------------------------------
# Case 3: provisioning-model spellings
# ---------------------------------------------------------------------------

def test_case3_standard_spellings_are_equivalent_and_banana_is_rejected(monkeypatch):
    """'STANDARD' and '100% Standard' describe the same thing.

    They must not produce a verification mismatch, while a genuinely unknown
    value must be refused instead of stored.
    """
    _offline(monkeypatch)

    rt = SlurmRuntime(job_id="job-real-3")
    rt.job_status = "PENDING"
    rt.apply(
        Action(
            action="resize_workload",
            workload_id="job-real-3",
            cpu=60,
            machine_type="c2-standard-60",
            provisioning_model="STANDARD",
            reason="spelled in upper case",
        )
    )
    assert rt.verification_status == "pending_verification"

    # The controller reports the compute partition, i.e. "100% Standard".
    rt._sync_job_state(
        {"job_id": "job-real-3", "job_resources": {"allocated_cpus": 60}, "partition": "compute"}
    )
    assert rt.verification_status == "verified", rt.verification_detail
    assert "mismatch" not in (rt.verification_detail or "").lower()

    # An unknown provisioning model is refused.
    rt2 = SlurmRuntime(job_id="job-real-3b")
    rt2.job_status = "PENDING"
    with pytest.raises(RuntimeError, match="(?i)banana|unsupported"):
        rt2.apply(
            Action(
                action="resize_workload",
                workload_id="job-real-3b",
                cpu=60,
                machine_type="c2-standard-60",
                provisioning_model="BANANA",
                reason="typo",
            )
        )


# ---------------------------------------------------------------------------
# Case 4: cost accrued without any observed allocation
# ---------------------------------------------------------------------------

def test_case4_elapsed_time_without_observed_allocation_does_not_bill_local_defaults(monkeypatch):
    """An hour of elapsed time with no allocation must not be priced.

    The adapter used to multiply elapsed time by its *local* ``allocated_cpu``
    default (4), and quietly book about 0.20 EUR for compute nobody observed.
    """
    _offline(monkeypatch)

    rt = SlurmRuntime(job_id="job-real-4")
    rt.allocated_cpu = 4  # purely local default, never confirmed by Slurm
    rt.accrued_cost_eur = 0.0

    rt._sync_job_state(
        {
            "job_id": "job-real-4",
            "job_state": ["PENDING"],
            "time": {"elapsed": 3600},
            # no job_resources: nothing was allocated
        }
    )

    assert rt.observed_cpu is None
    assert rt.accrued_cost_eur == 0.0, (
        f"billed {rt.accrued_cost_eur} EUR without any observed allocation"
    )
    assert rt.cost_basis == "unverified"

    snap = rt.snapshot()
    assert snap.workload.cost_basis == "unverified"
    assert snap.workload.observed_cpu is None
    # The uncertainty must stay visible rather than being rounded away.
    assert rt.unpriced_elapsed_minutes >= 59.0


# ---------------------------------------------------------------------------
# Case 5: free-CPU accounting across all jobs
# ---------------------------------------------------------------------------

def test_case5_free_cpu_accounts_for_every_job_not_just_the_tracked_one(monkeypatch):
    """Nodes fully occupied by several jobs must not be reported as mostly free.

    Free CPU was ``total - our_job``, so a fully busy 128-CPU cluster looked
    like it had 124 CPUs available while we held 4.
    """
    nodes_payload = {
        "nodes": [
            {"name": "node-1", "cpus": 64, "alloc_cpus": 64, "idle_cpus": 0},
            {"name": "node-2", "cpus": 64, "alloc_cpus": 64, "idle_cpus": 0},
        ]
    }
    monkeypatch.setenv("MOCK_SLURM", "true")
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(200, nodes_payload))
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp(200, {}))

    rt = SlurmRuntime(job_id="job-real-5")
    rt.allocated_cpu = 4

    snap = rt.snapshot()
    assert snap.cluster.total_cpu == 128
    assert snap.cluster.free_cpu == 0, (
        f"reported {snap.cluster.free_cpu} free CPUs on a fully allocated cluster"
    )


def test_case5b_partially_busy_cluster_reports_real_idle_cpus(monkeypatch):
    nodes_payload = {
        "nodes": [
            {"name": "node-1", "cpus": 64, "alloc_cpus": 60, "idle_cpus": 4},
            {"name": "node-2", "cpus": 64, "alloc_cpus": 0, "idle_cpus": 64},
        ]
    }
    monkeypatch.setenv("MOCK_SLURM", "true")
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(200, nodes_payload))
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp(200, {}))

    rt = SlurmRuntime(job_id="job-real-5b")
    rt.allocated_cpu = 60
    snap = rt.snapshot()
    assert snap.cluster.total_cpu == 128
    assert snap.cluster.free_cpu == 68


# ---------------------------------------------------------------------------
# Case 6: loss of allocation observation
# ---------------------------------------------------------------------------

def test_case6_losing_allocation_telemetry_does_not_reinject_a_stale_value(monkeypatch):
    """Once telemetry stops reporting resources, observed_cpu must stay unknown."""
    _offline(monkeypatch)

    rt = SlurmRuntime(job_id="job-real-6")
    rt._sync_job_state(
        {"job_id": "job-real-6", "job_resources": {"allocated_cpus": 60}, "partition": "compute"}
    )
    assert rt.observed_cpu == 60

    # Telemetry degrades: the controller no longer reports job_resources.
    rt._sync_job_state({"job_id": "job-real-6", "job_state": ["RUNNING"]})

    assert rt.observed_cpu is None
    assert rt.cost_basis == "unverified"
    snap = rt.snapshot()
    assert snap.workload.observed_cpu is None
    assert snap.workload.verification_status == "unverified"


# ---------------------------------------------------------------------------
# Case 7: remote runtime configured but unavailable
# ---------------------------------------------------------------------------

def test_case7_unavailable_slurm_is_an_error_not_a_silent_local_simulation(monkeypatch):
    """A configured but unreachable Slurm must never be replaced by the simulator."""
    monkeypatch.delenv("MOCK_SLURM", raising=False)

    def boom(*a, **k):
        raise ConnectionError("connection refused")

    monkeypatch.setattr("requests.get", boom)
    monkeypatch.setattr("requests.post", boom)

    rt = SlurmRuntime(job_id="job-real-7")
    with pytest.raises(RuntimeError) as exc:
        rt.snapshot()
    message = str(exc.value).lower()
    assert "slurm" in message
    assert "simulat" not in message or "not" in message


def test_case7b_runtime_factory_does_not_substitute_a_simulator(monkeypatch):
    """COMPUTE_RUNTIME=slurm must yield a Slurm adapter, never a simulator."""
    import agentic_compute.mcp_server as mcp_mod
    from agentic_compute.simulator import SimulatedRuntime

    monkeypatch.setenv("COMPUTE_RUNTIME", "slurm")
    rt = mcp_mod._create_runtime()
    assert isinstance(rt, SlurmRuntime)
    assert not isinstance(rt, SimulatedRuntime)
