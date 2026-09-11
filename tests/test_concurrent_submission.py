"""Idempotency under genuine concurrency, not just sequential replay.

Every other test in this repository submits twice in sequence, which only
proves the ledger is consulted. It does not prove the claim is atomic. The
interesting case is two requests racing: a double click on a slow button, two
dashboard tabs, or a client retrying while the first call is still in flight.

The lock is the SQLite PRIMARY KEY INSERT in ``claim_submission``. These tests
run real threads against one shared database file to show it holds.
"""

from __future__ import annotations

import threading

import pytest

from agentic_compute.execution_controller import ExecutionController
from agentic_compute.governance import get_governance_store
from agentic_compute.simulator import SimulatedRuntime

WORKLOAD = "wl-race"


def _profile() -> dict:
    return {
        "workload_id": WORKLOAD,
        "name": "race",
        "cpu_requested": 4,
        "command": "echo hi",
        # The racing plans span several regions to get distinct fingerprints,
        # so the profile has to allow them: submission enforces the profile's
        # own `allowed_regions` (default ["us-central1"]).
        "allowed_regions": [
            "europe-west4",
            "europe-west1",
            "europe-west2",
            "europe-west3",
        ],
    }


def _plan() -> dict:
    return {
        "plan_id": "plan-race",
        "plan_type": "cost_optimized",
        "title": "race",
        "machine_type": "n2-standard-4",
        "region": "europe-west4",
        "zone": "europe-west4-a",
        "quantity": 1,
        "cpu": 4,
        "gpu": 0,
        "provisioning_model": "SPOT",
        "estimated_cost_eur": 3.0,
        "estimated_duration_minutes": 20,
    }


def _run_concurrently(fn, count: int) -> list:
    """Start `count` threads and release them together."""
    start = threading.Barrier(count)
    results: list = [None] * count
    errors: list = []

    def worker(index: int) -> None:
        try:
            start.wait(timeout=10)
            results[index] = fn(index)
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    return results


def test_eight_concurrent_claims_produce_exactly_one_winner():
    """The INSERT is the lock. Anything else and two jobs get created."""
    gov = get_governance_store()

    def claim(_: int):
        won, row = gov.claim_submission(
            key="agentgrid-wl-race-abcdef", workload_id=WORKLOAD,
            plan_id="plan-race", fingerprint="abcdef", estimated_cost_eur=3.0,
        )
        return won

    outcomes = _run_concurrently(claim, 8)

    assert sum(1 for won in outcomes if won) == 1, outcomes
    assert len(gov.list_submissions(WORKLOAD)) == 1


def test_concurrent_submissions_create_exactly_one_job():
    """End to end: eight racing submit_plan calls, one job id, one runtime call."""
    gov = get_governance_store()
    gov.set_workload_control(WORKLOAD, "delegation", {"max_budget_eur": 100.0, "max_retries": 5})

    runtime = SimulatedRuntime()
    submitted: list[str] = []
    lock = threading.Lock()
    real_submit = runtime.submit_job

    def counting_submit(*args, **kwargs):
        result = real_submit(*args, **kwargs)
        with lock:
            submitted.append(str(result))
        return result

    runtime.submit_job = counting_submit  # type: ignore[method-assign]

    # A fresh controller per thread, exactly as an HTTP request would build one.
    def submit(_: int):
        controller = ExecutionController(runtime=runtime)
        return controller.submit_plan(profile=_profile(), plan=_plan())

    results = _run_concurrently(submit, 8)

    statuses = [r["status"] for r in results]
    assert statuses.count("submitted") == 1, statuses

    # The losers are told one of two honest things, depending on whether the
    # winner had finished by the time they looked: the job id, or that a
    # submission is in flight. Never a second job, and never a fabricated id.
    losers = [r for r in results if r["status"] != "submitted"]
    assert {r["status"] for r in losers} <= {"in_flight", "already_submitted"}, statuses

    winner = next(r for r in results if r["status"] == "submitted")
    for loser in losers:
        if loser["status"] == "already_submitted":
            # The winner had finished: the replay reports the real job id and
            # may legitimately have verified it against the runtime.
            assert loser["job_id"] == winner["job_id"]
        else:
            # Still in flight: no job id yet, and nothing is claimed as verified.
            assert loser["job_id"] is None
            assert loser["verified"] is False
            assert "did not submit again" in loser["reason"]

    # And the runtime was touched exactly once.
    assert len(submitted) == 1, submitted
    assert len(gov.list_submissions(WORKLOAD)) == 1


def test_the_budget_is_charged_once_under_a_race():
    """Seven losing threads must not each add the plan's cost."""
    gov = get_governance_store()
    gov.set_workload_control(WORKLOAD, "delegation", {"max_budget_eur": 100.0, "max_retries": 5})
    runtime = SimulatedRuntime()

    _run_concurrently(
        lambda _: ExecutionController(runtime=runtime).submit_plan(
            profile=_profile(), plan=_plan()
        ),
        8,
    )

    commitments = gov.get_commitments(WORKLOAD)
    assert commitments["committed_cost_eur"] == pytest.approx(3.0)
    assert commitments["launched_attempts"] == 1


def test_the_retry_ceiling_is_not_bypassed_by_racing():
    """max_retries=1 plus a race must still leave exactly one launch."""
    gov = get_governance_store()
    gov.set_workload_control(WORKLOAD, "delegation", {"max_budget_eur": 100.0, "max_retries": 1})
    runtime = SimulatedRuntime()

    regions = ["europe-west4", "europe-west1", "europe-west3", "europe-west2"]

    def submit(index: int):
        # Four *different* plans racing: they do not share a submission key, so
        # the PK lock does not help. Only the retry ceiling can stop them.
        plan = _plan()
        plan["region"] = regions[index]
        plan["zone"] = regions[index] + "-a"
        plan["plan_id"] = f"plan-{index}"
        return ExecutionController(runtime=runtime).submit_plan(
            profile=_profile(), plan=plan
        )

    results = _run_concurrently(submit, 4)
    statuses = [r["status"] for r in results]

    assert statuses.count("submitted") == 1, statuses
    assert all(
        "retry budget exhausted" in r["reason"] for r in results if r["status"] == "blocked"
    ), [r["reason"] for r in results]
