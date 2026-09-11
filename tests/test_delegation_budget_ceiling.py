"""The delegated budget must be a cumulative ceiling the server can prove.

Reproduced defect (D-CUMUL), observed on the code before this file existed:

    submission 1: cost=8.0 -> status=submitted   job=agentgrid-wl-cumul-4e145d946d88
    submission 2: cost=8.0 -> status=submitted   job=agentgrid-wl-cumul-5c0a456c220f
    submission 3: cost=8.0 -> status=submitted   job=agentgrid-wl-cumul-a240cf247f41

With ``max_budget_eur = 10.00`` the operator had delegated ten euros and got
three jobs worth twenty-four. ``check_policy_bounds`` did accept an
``accumulated_cost_eur`` argument, but nothing outside the fallback ladder ever
passed one, so the cap only ever saw a single plan in isolation. That is the
same self-attestation family as the approval flags: a bound that depends on the
caller volunteering the inconvenient number is not a bound.

These tests pin the corrected behaviour: the cumulative figure is derived from
the submission ledger, and the caller may only ever tighten it.
"""

from __future__ import annotations

import pytest

from agentic_compute.execution_controller import ExecutionController
from agentic_compute.governance import get_governance_store, reset_governance_store
from agentic_compute.simulator import SimulatedRuntime

WORKLOAD = "wl-ceiling"

# Three genuinely different plans. They must differ in a *material* field or
# they would share a fingerprint and be treated as a replay of one another --
# which is exactly what happened on the first attempt to reproduce this.
REGIONS = ["europe-west4", "europe-west1", "europe-west3"]


def _profile(workload_id: str = WORKLOAD) -> dict:
    return {
        "workload_id": workload_id,
        "name": "budget ceiling probe",
        "cpu_requested": 4,
        "command": "echo hi",
    }


def _plan(plan_id: str, cost: float, region: str = "europe-west4") -> dict:
    return {
        "plan_id": plan_id,
        "plan_type": "cost_optimized",
        "title": f"plan {plan_id}",
        "machine_type": "n2-standard-4",
        "region": region,
        "zone": f"{region}-a",
        "quantity": 1,
        "cpu": 4,
        "gpu": 0,
        "provisioning_model": "SPOT",
        "estimated_cost_eur": cost,
        "estimated_duration_minutes": 30,
    }


@pytest.fixture()
def delegated() -> ExecutionController:
    """A workload the operator has delegated with a 10 EUR ceiling."""
    gov = get_governance_store()
    gov.set_workload_control(
        WORKLOAD, "delegation", {"max_budget_eur": 10.0, "max_retries": 5}
    )
    return ExecutionController(runtime=SimulatedRuntime())


def test_second_distinct_plan_is_refused_once_the_ceiling_is_reached(delegated):
    """The original defect: 8 + 8 must not both pass a 10 EUR delegation."""
    first = delegated.submit_plan(
        profile=_profile(), plan=_plan("plan-1", 8.0, REGIONS[0])
    )
    assert first["status"] == "submitted", first["reason"]

    second = delegated.submit_plan(
        profile=_profile(), plan=_plan("plan-2", 8.0, REGIONS[1])
    )

    assert second["status"] == "blocked"
    assert second["job_id"] is None
    assert "cumulative 16.00EUR" in second["reason"]
    assert "already spent 8.00EUR" in second["reason"]
    assert second["accumulated_cost_eur"] == pytest.approx(8.0)
    assert second["accumulated_cost_source"] == "server_submission_ledger"
    assert second["prior_submissions_counted"] == 1

    # And nothing was created for the refused plan.
    ledger = get_governance_store().list_submissions(WORKLOAD)
    assert [row["plan_id"] for row in ledger] == ["plan-1"]


def test_a_caller_cannot_understate_what_was_already_committed(delegated):
    """Passing accumulated_cost_eur=0.0 must not reset the ceiling."""
    delegated.submit_plan(profile=_profile(), plan=_plan("plan-1", 8.0, REGIONS[0]))

    result = delegated.submit_plan(
        profile=_profile(),
        plan=_plan("plan-2", 8.0, REGIONS[1]),
        accumulated_cost_eur=0.0,
    )

    assert result["status"] == "blocked"
    assert result["accumulated_cost_eur"] == pytest.approx(8.0)
    assert result["accumulated_cost_source"] == "server_submission_ledger"


def test_a_caller_may_still_tighten_the_ceiling(delegated):
    """max() means a caller who knows about extra spend is believed."""
    result = delegated.submit_plan(
        profile=_profile(),
        plan=_plan("plan-1", 4.0, REGIONS[0]),
        accumulated_cost_eur=9.0,
    )

    assert result["status"] == "blocked"
    assert result["accumulated_cost_eur"] == pytest.approx(9.0)
    assert result["accumulated_cost_source"] == "caller_supplied"
    assert result["prior_submissions_counted"] == 0


def test_a_replay_is_not_charged_against_itself(delegated):
    """The same plan submitted twice must stay a replay, not become a breach.

    Without ``exclude_key`` the second call would count its own claim as prior
    spend, turn 6 + 6 into a 12 EUR breach and answer 'blocked' where the
    honest answer is 'already_submitted'.
    """
    plan = _plan("plan-1", 6.0, REGIONS[0])
    first = delegated.submit_plan(profile=_profile(), plan=plan)
    assert first["status"] == "submitted"

    replay = delegated.submit_plan(profile=_profile(), plan=plan)

    assert replay["status"] == "already_submitted"
    assert replay["job_id"] == first["job_id"]


def test_a_failed_submission_does_not_consume_the_delegated_budget(delegated):
    """Nothing was created, so nothing is committed."""
    rejected = _plan("plan-bad", 8.0, REGIONS[0])
    rejected["cpu"] = 64  # n2-standard-4 cannot satisfy this; runtime rejects it.

    failed = delegated.submit_plan(profile=_profile(), plan=rejected)
    assert failed["status"] == "failed"

    commitments = get_governance_store().get_commitments(WORKLOAD)
    assert commitments["committed_cost_eur"] == pytest.approx(0.0)

    # The budget is therefore still available for a real plan.
    ok = delegated.submit_plan(profile=_profile(), plan=_plan("plan-1", 8.0, REGIONS[1]))
    assert ok["status"] == "submitted", ok["reason"]


def test_an_uncertain_submission_still_counts_against_the_budget():
    """An ambiguous outcome may have created a job, so it must be charged."""
    gov = get_governance_store()
    gov.set_workload_control(WORKLOAD, "delegation", {"max_budget_eur": 10.0})

    gov.claim_submission(
        key="agentgrid-wl-ceiling-deadbeef",
        workload_id=WORKLOAD,
        plan_id="plan-ambiguous",
        fingerprint="deadbeef",
        estimated_cost_eur=7.0,
    )
    gov.record_submission("agentgrid-wl-ceiling-deadbeef", state="uncertain")

    commitments = gov.get_commitments(WORKLOAD)
    assert commitments["committed_cost_eur"] == pytest.approx(7.0)

    controller = ExecutionController(runtime=SimulatedRuntime())
    result = controller.submit_plan(
        profile=_profile(), plan=_plan("plan-1", 8.0, REGIONS[0])
    )
    assert result["status"] == "blocked"
    assert "already spent 7.00EUR" in result["reason"]


def test_the_ceiling_survives_a_restart(delegated):
    """The figure comes from SQLite, so a fresh process cannot forget it."""
    delegated.submit_plan(profile=_profile(), plan=_plan("plan-1", 8.0, REGIONS[0]))

    reset_governance_store()  # simulate a process restart
    fresh = ExecutionController(runtime=SimulatedRuntime())

    result = fresh.submit_plan(profile=_profile(), plan=_plan("plan-2", 8.0, REGIONS[1]))
    assert result["status"] == "blocked"
    assert result["accumulated_cost_source"] == "server_submission_ledger"


def test_releasing_a_claim_returns_its_budget(delegated):
    """An operator-authorised retry must not be charged twice."""
    failing = _plan("plan-1", 8.0, REGIONS[0])
    delegated.submit_plan(profile=_profile(), plan=failing)

    gov = get_governance_store()
    key = gov.list_submissions(WORKLOAD)[0]["submission_key"]
    gov.release_submission(key)

    assert gov.get_commitments(WORKLOAD)["committed_cost_eur"] == pytest.approx(0.0)


def test_the_fallback_ladder_uses_the_server_figure_too(delegated):
    """A fallback is a fresh mutation and is bound by the same cumulative cap."""
    delegated.submit_plan(profile=_profile(), plan=_plan("plan-1", 8.0, REGIONS[0]))

    outcome = delegated.evaluate_fallback_ladder_with_details(
        current_plan=_plan("plan-1", 8.0, REGIONS[0]),
        available_plans=[_plan("plan-2", 8.0, REGIONS[1])],
        failure_category="preemption",
        accumulated_cost_eur=0.0,  # the caller claims nothing was spent
        workload_id=WORKLOAD,
    )

    assert outcome["status"] == "policy_breach"
    assert outcome["plan"] is None
    assert outcome["accumulated_cost_source"] == "server_submission_ledger"
    assert outcome["accumulated_cost_eur"] == pytest.approx(8.0)


def test_a_legacy_ledger_row_is_reported_as_unpriced_rather_than_free():
    """Rows written before the ledger priced submissions must not read as 0 EUR.

    They are counted in ``unpriced_submissions`` so the gap is visible instead
    of silently loosening the ceiling.
    """
    gov = get_governance_store()
    with gov._connect() as conn:  # noqa: SLF001 - exercising the migration path
        conn.execute(
            """
            INSERT INTO submission_ledger
                (submission_key, workload_id, plan_id, fingerprint, job_id,
                 state, detail, attempt_id, estimated_cost_eur, created_at, updated_at)
            VALUES ('legacy-key', ?, 'plan-legacy', 'abc', 'job-legacy',
                    'submitted', NULL, NULL, NULL, 1.0, 1.0)
            """,
            (WORKLOAD,),
        )
        conn.commit()

    commitments = gov.get_commitments(WORKLOAD)
    assert commitments["submission_count"] == 1
    assert commitments["unpriced_submissions"] == 1
    assert commitments["committed_cost_eur"] == pytest.approx(0.0)


# ----------------------------------------------------------------------------
# The same guarantee at the HTTP boundary, which is where an agent actually
# reaches the system. A fix that only holds inside the controller would be
# bypassed by anything talking to the API.
# ----------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from compute_agent.app import app

    return TestClient(app)


def _execute(client, plan: dict) -> dict:
    return client.post(
        "/api/execute-plan",
        json={"workload_profile": _profile(), "plan": plan},
    ).json()


def test_http_execute_plan_enforces_the_cumulative_ceiling(client):
    control = client.post(
        f"/api/workloads/{WORKLOAD}/control",
        json={
            "control_mode": "delegation",
            "delegation_policy": {"max_budget_eur": 10.0, "max_retries": 5},
        },
    )
    assert control.status_code == 200
    assert control.json()["remaining_delegated_budget_eur"] == pytest.approx(10.0)

    first = _execute(client, _plan("plan-1", 8.0, REGIONS[0]))
    assert first["status"] == "submitted", first.get("reason")

    # The operator can now see the headroom shrink, from the server's records.
    state = client.get(f"/api/workloads/{WORKLOAD}/control").json()
    assert state["committed_cost_eur"] == pytest.approx(8.0)
    assert state["remaining_delegated_budget_eur"] == pytest.approx(2.0)
    assert state["committed_cost_source"] == "server_submission_ledger"

    second = _execute(client, _plan("plan-2", 8.0, REGIONS[1]))
    assert second["status"] == "blocked"
    assert second["job_id"] is None
    assert "already spent 8.00EUR" in second["reason"]
