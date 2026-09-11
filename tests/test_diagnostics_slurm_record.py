"""``slurm_job_details`` must actually be read.

Reproduced defect, observed before this file existed. A complete slurmrestd job
record was handed to ``diagnose_blockers`` and to ``POST /api/diagnose``:

    state_reason  : BadConstraints
    admin_comment : ZONE_RESOURCE_POOL_EXHAUSTED: instance a3-highgpu-8g in europe-west4-b
    dependency    : afterok:4700

The answer was:

    category      : unknown
    observed_facts: "Job state: UNKNOWN, Reason: None"
    confirmed     : false

The parameter existed in the signature, the MCP surface forwarded it, and the
function never looked at it. ``POST /api/diagnose`` did not even forward it.
Two ways to lose the same evidence.

Slurm-GCP writes cloud provider failures from the resume script into
``admin_comment``, so that field is often the only place a stockout is
recorded.
"""

from __future__ import annotations

import pytest

from agentic_compute.diagnostics import diagnose_blockers


def _record(**overrides) -> dict:
    """A realistic slurmrestd job record. Scalars come wrapped, states listed."""
    record = {
        "job_id": 4711,
        "job_state": ["PENDING"],
        "state_reason": "BadConstraints",
        "partition": "compute",
        "cpus": {"number": 64, "set": True, "infinite": False},
        "node_count": {"number": 8, "set": True, "infinite": False},
        "qos": "normal",
        "admin_comment": (
            "ZONE_RESOURCE_POOL_EXHAUSTED: instance a3-highgpu-8g in europe-west4-b"
        ),
        "exit_code": {"return_code": {"number": 0, "set": True}},
    }
    record.update(overrides)
    return record


def _categories(items) -> set[str]:
    return {item["category"] for item in items}


def test_a_job_record_alone_is_enough_to_diagnose():
    """The exact reproduction above: no longer 'unknown'."""
    items = diagnose_blockers(slurm_job_details=_record())

    assert "unknown" not in _categories(items)
    assert "capacity_shortage" in _categories(items)
    assert "incompatible_configuration" in _categories(items)


def test_the_admin_comment_is_where_the_stockout_is_recorded():
    """Only admin_comment carries the provider error; it must be classified."""
    items = diagnose_blockers(
        slurm_job_details={
            "job_id": 22,
            "job_state": ["PENDING"],
            "admin_comment": "ZONE_RESOURCE_POOL_EXHAUSTED in europe-west4-b",
        }
    )

    capacity = [i for i in items if i["category"] == "capacity_shortage"]
    assert capacity, _categories(items)
    assert "ZONE_RESOURCE_POOL_EXHAUSTED" in capacity[0]["observed_facts"]
    # The origin must survive: this is a cloud fact, not a scheduler fact.
    assert capacity[0]["source"] == "gcp_capacity_advisor"


def test_a_quota_error_in_the_admin_comment_is_classified_as_quota():
    items = diagnose_blockers(
        slurm_job_details={
            "job_id": 23,
            "job_state": ["PENDING"],
            "admin_comment": "QUOTA_EXCEEDED: CPUS_ALL_REGIONS limit 128 reached",
        }
    )

    quota = [i for i in items if i["category"] == "quota"]
    assert quota, _categories(items)
    assert quota[0]["source"] == "gcp_compute_quota"
    assert "CPUS_ALL_REGIONS" in quota[0]["observed_facts"]


def test_the_evidence_behind_a_finding_is_reported():
    """A diagnosis has to say what it was based on."""
    items = diagnose_blockers(slurm_job_details=_record())

    incompatible = [i for i in items if i["category"] == "incompatible_configuration"][0]
    facts = incompatible["observed_facts"]
    assert "job 4711" in facts
    assert "partition 'compute'" in facts
    assert "64 vCPU requested" in facts
    assert "8 node(s) requested" in facts


def test_the_admin_comment_is_not_printed_twice():
    """When it is already the headline fact, it is not repeated as evidence."""
    items = diagnose_blockers(slurm_job_details=_record())
    capacity = [i for i in items if i["category"] == "capacity_shortage"][0]

    assert capacity["observed_facts"].count("ZONE_RESOURCE_POOL_EXHAUSTED") == 1


def test_an_exit_code_from_the_record_is_read_through_its_wrapper():
    """slurmrestd nests it: exit_code.return_code.number."""
    items = diagnose_blockers(
        slurm_job_details={
            "job_id": 24,
            "job_state": ["FAILED"],
            "exit_code": {"return_code": {"number": 137, "set": True}},
        }
    )

    app_errors = [i for i in items if i["category"] == "application_error"]
    assert app_errors, _categories(items)
    assert "137" in app_errors[0]["observed_facts"]
    # 137 is the OOM signature, so the memory action must be offered.
    actions = " ".join(a["action"] for a in app_errors[0]["possible_actions"])
    assert "memory" in actions.lower()


def test_an_unset_scalar_is_not_read_as_zero():
    """``{"set": false}`` means unknown. Reading .number regardless invents a 0.

    A zero exit code would silently mean success; a zero vCPU count would be
    printed as a confident fact about a request nobody made.
    """
    items = diagnose_blockers(
        slurm_job_details={
            "job_id": 25,
            "job_state": ["PENDING"],
            "state_reason": "Resources",
            "cpus": {"number": 0, "set": False, "infinite": False},
            "exit_code": {"return_code": {"number": 0, "set": False}},
        }
    )

    facts = " ".join(i["observed_facts"] for i in items)
    assert "0 vCPU requested" not in facts
    assert "vCPU requested" not in facts
    assert "application_error" not in _categories(items)


def test_an_infinite_scalar_is_not_a_number():
    items = diagnose_blockers(
        slurm_job_details={
            "job_id": 26,
            "job_state": ["PENDING"],
            "state_reason": "Resources",
            "cpus": {"number": 0, "set": True, "infinite": True},
        }
    )

    assert "vCPU requested" not in " ".join(i["observed_facts"] for i in items)


def test_an_unmet_dependency_in_the_record_is_surfaced():
    """The dependency string is evidence even when state_reason says otherwise."""
    items = diagnose_blockers(
        slurm_job_details={
            "job_id": 27,
            "job_state": ["PENDING"],
            "state_reason": "Resources",
            "dependency": "afterok:4700",
        }
    )

    deps = [i for i in items if i["category"] == "dependencies"]
    assert deps, _categories(items)
    assert "afterok:4700" in deps[0]["observed_facts"]
    assert deps[0]["source"] == "slurm_controller"


def test_a_dependency_on_a_running_job_is_not_reported_as_a_blocker():
    """A satisfied dependency on an already-running job blocks nothing."""
    items = diagnose_blockers(
        job_state="RUNNING",
        slurm_job_details={"job_id": 28, "job_state": ["RUNNING"], "dependency": "afterok:4700"},
    )

    assert "dependencies" not in _categories(items)


def test_explicit_arguments_win_over_the_record():
    """The record fills gaps; it does not override what the caller stated."""
    items = diagnose_blockers(
        job_state="FAILED",
        state_reason="OutOfMemory",
        exit_code=1,
        slurm_job_details=_record(),
    )

    app_errors = [i for i in items if i["category"] == "application_error"]
    assert app_errors
    assert "exit code 1" in app_errors[0]["observed_facts"]
    assert "Job state: FAILED" in app_errors[0]["observed_facts"]


def test_no_record_at_all_still_works():
    """Non-regression: the parameter is optional and adds nothing when absent."""
    items = diagnose_blockers(job_state="PENDING", state_reason="Priority")

    assert _categories(items) == {"priority"}
    assert "Slurm record" not in items[0]["observed_facts"]


# ----------------------------------------------------------------------------
# The HTTP route dropped the field entirely, so fixing the function alone would
# not have fixed the dashboard.
# ----------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from compute_agent.app import app

    return TestClient(app)


def test_the_http_route_forwards_the_job_record(client):
    response = client.post("/api/diagnose", json={"slurm_job_details": _record()})

    assert response.status_code == 200
    categories = {d["category"] for d in response.json()["diagnostics"]}
    assert "unknown" not in categories
    assert "capacity_shortage" in categories


def test_the_http_route_survives_a_query_string_that_cannot_carry_an_object(client):
    """A GET can only send a string; it must be ignored, not crash the route."""
    response = client.get("/api/diagnose", params={"slurm_job_details": "not-an-object"})

    assert response.status_code == 200
    assert response.json()["diagnostics"]
