"""Defect 2-C: a Slurm scheduler limit was reported as a GCP quota.

``diagnose_blockers`` matched ``qos`` / ``assocmax`` anywhere in the collected
error text and emitted a finding whose ``source`` was ``gcp_compute_quota`` and
whose first remedy was "Request GCP quota increase for the target region".

A ``QOSMaxJobsPerUserLimit`` is a limit held by the *cluster's* accounting
database. No amount of GCP quota changes it, and asking a cloud administrator
for more vCPUs is a wasted round trip -- the operator has to reduce parallelism,
wait for their own jobs, or have the QoS raised in slurmdbd.

The sibling module ``agentic_compute.diagnostic`` already got this right
(``source="slurm_controller"``, cluster-side remedies), so the two diagnostic
paths contradicted each other for the same input.

A second half of the same defect: the reason family was matched with
``assocmax`` only, so ``AssocGrpCPURunMinutes`` -- a very common blocker on a
shared cluster -- fell through to ``category="unknown"``.
"""

import pytest

from agentic_compute.diagnostics import diagnose_blockers


SCHEDULER_REASONS = [
    "QOSMaxJobsPerUserLimit",
    "QOSMaxCpuPerUserLimit",
    "QOSGrpCpuLimit",
    "QOSGrpBillingMinutes",
    "AssocMaxJobsLimit",
    "AssocMaxCpuMinsPerJobLimit",
    "AssocGrpCPURunMinutes",
    "AssocGrpCpuLimit",
]


def _only(findings, category):
    return [f for f in findings if f["category"] == category]


@pytest.mark.parametrize("reason", SCHEDULER_REASONS)
def test_scheduler_limit_is_not_attributed_to_gcp(reason):
    findings = diagnose_blockers(job_state="PENDING", state_reason=reason)
    sources = {f["source"] for f in findings}
    assert "gcp_compute_quota" not in sources, (
        f"{reason} is a Slurm accounting limit; it must not be sourced to GCP. "
        f"Got {findings}"
    )


@pytest.mark.parametrize("reason", SCHEDULER_REASONS)
def test_scheduler_limit_is_recognised_not_unknown(reason):
    findings = diagnose_blockers(job_state="PENDING", state_reason=reason)
    categories = {f["category"] for f in findings}
    assert "unknown" not in categories, (
        f"{reason} is a documented Slurm state reason and must be diagnosed. "
        f"Got {findings}"
    )
    assert categories == {"quota"}, findings


@pytest.mark.parametrize("reason", SCHEDULER_REASONS)
def test_scheduler_limit_does_not_advise_a_cloud_quota_increase(reason):
    findings = diagnose_blockers(job_state="PENDING", state_reason=reason)
    actions = " ".join(
        a["action"] for f in findings for a in f["possible_actions"]
    ).lower()
    assert "gcp quota increase" not in actions, (
        f"{reason} cannot be resolved by a cloud quota request. Got {actions}"
    )


@pytest.mark.parametrize("reason", SCHEDULER_REASONS)
def test_scheduler_limit_names_the_reason_and_its_holder(reason):
    findings = diagnose_blockers(job_state="PENDING", state_reason=reason)
    facts = " ".join(f["observed_facts"] for f in findings)
    assert reason in facts, f"the exact state reason must survive: {facts}"
    assert "slurm" in facts.lower(), (
        f"the operator must be told the limit is held by the cluster: {facts}"
    )


def test_real_gcp_quota_error_keeps_its_gcp_source_and_remedy():
    """The fix must not disarm the genuine GCP quota path."""
    findings = diagnose_blockers(
        job_state="PENDING",
        gcp_error="QUOTA_EXCEEDED: CPUS_ALL_REGIONS limit is 32",
    )
    quota = _only(findings, "quota")
    assert len(quota) == 1
    assert quota[0]["source"] == "gcp_compute_quota"
    actions = " ".join(a["action"] for a in quota[0]["possible_actions"]).lower()
    assert "gcp quota increase" in actions


def test_gcp_quota_in_admin_comment_keeps_its_gcp_source():
    findings = diagnose_blockers(
        job_state="PENDING",
        state_reason="NodesSpecError",
        slurm_job_details={
            "job_id": 4242,
            "admin_comment": "gcloud error: QUOTA_EXCEEDED for GPUS_ALL_REGIONS",
        },
    )
    quota = _only(findings, "quota")
    assert len(quota) == 1
    assert quota[0]["source"] == "gcp_compute_quota"


def test_both_limits_at_once_are_reported_separately():
    """A cluster QoS limit and a cloud quota error are two different blockers.

    Collapsing them into one finding forces the operator to guess which of the
    two remedies applies.
    """
    findings = diagnose_blockers(
        job_state="PENDING",
        state_reason="QOSMaxJobsPerUserLimit",
        gcp_error="QUOTA_EXCEEDED: CPUS_ALL_REGIONS limit is 32",
    )
    quota = _only(findings, "quota")
    sources = sorted(f["source"] for f in quota)
    assert sources == ["gcp_compute_quota", "slurm_controller"], findings


def test_scheduler_limit_carries_the_slurm_record_as_evidence():
    findings = diagnose_blockers(
        state_reason="AssocGrpCPURunMinutes",
        slurm_job_details={"job_id": 991, "partition": "compute", "qos": "normal"},
    )
    quota = _only(findings, "quota")
    assert len(quota) == 1
    facts = quota[0]["observed_facts"]
    assert "job 991" in facts and "partition 'compute'" in facts, facts


def test_scheduler_limit_suppresses_the_generic_waiting_finding():
    """Before the fix the generic 'waiting for capacity' branch was suppressed
    only because the QoS reason was (wrongly) matched as a quota. The
    suppression must survive the reclassification, or the operator gets two
    contradictory explanations."""
    findings = diagnose_blockers(job_state="PENDING", state_reason="AssocGrpCPURunMinutes")
    assert _only(findings, "resource_waiting") == [], findings


def test_the_word_qos_alone_does_not_manufacture_a_limit():
    """``"qos" in err_text`` matched any log line that merely mentioned QoS.

    A crash log that happens to print the QoS name is not evidence that a QoS
    limit was reached.
    """
    findings = diagnose_blockers(
        job_state="FAILED",
        exit_code=1,
        error_log="Traceback: submitted under qos=normal, then ValueError in train.py",
    )
    assert _only(findings, "quota") == [], findings
    assert _only(findings, "application_error"), findings


def test_two_diagnostic_paths_agree_on_the_holder_of_a_qos_limit():
    """``diagnostic.diagnose_slurm_job`` and ``diagnostics.diagnose_blockers``
    must not contradict each other for the same observation."""
    from agentic_compute.diagnostic import diagnose_slurm_job

    legacy = diagnose_slurm_job(
        job_id="7", state="PENDING", reason="QOSMaxJobsPerUserLimit"
    )
    current = diagnose_blockers(
        job_state="PENDING", state_reason="QOSMaxJobsPerUserLimit"
    )
    # ``diagnose_slurm_job`` returns model objects, ``diagnose_blockers`` dicts.
    legacy_quota = [d for d in legacy if d.category == "quota"]
    current_quota = _only(current, "quota")
    assert legacy_quota and current_quota
    assert legacy_quota[0].source == current_quota[0]["source"] == "slurm_controller"
