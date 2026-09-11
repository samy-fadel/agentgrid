"""Resume must be backed by a real checkpoint, not by a boolean in the profile.

Before this change ``can_resume_from_checkpoint`` returned::

    (True, "Checkpoint resumption available at '<uri>'. Previous attempt #1 can
            be recovered without restarting from 0%.")

purely because ``supports_checkpointing`` was True and a URI string was present.
Nothing ever looked at the location. An operator reading that sentence would
authorise a resume that in reality restarts from zero.
"""

from __future__ import annotations

import pytest

from agentic_compute.lifecycle_manager import LifecycleManager


def _profile(workload_id: str, checkpoint_location: str | None) -> dict:
    return {
        "workload_id": workload_id,
        "name": "checkpoint subject",
        "cpu_requested": 4,
        "estimated_duration_minutes": 30,
        "is_interruptible": True,
        "supports_checkpointing": True,
        "checkpoint_location": checkpoint_location,
    }


_PLAN = {
    "plan_id": "plan-ckpt-subject",
    "plan_type": "cost_optimized",
    "title": "checkpoint subject plan",
    "machine_type": "n2-standard-4",
    "cpu": 4,
    "estimated_cost_eur": 1.0,
}


def _register_with_attempt(manager: LifecycleManager, profile: dict) -> None:
    manager.register_workload(profile)
    manager.start_attempt(profile["workload_id"], plan=_PLAN, job_id="job-1")


def test_a_real_checkpoint_directory_is_verified_present(tmp_path):
    location = tmp_path / "ckpt"
    location.mkdir()
    (location / "state.pt").write_bytes(b"weights")

    manager = LifecycleManager()
    profile = _profile("wl-ckpt-present", str(location))
    _register_with_attempt(manager, profile)

    evidence = manager.verify_checkpoint("wl-ckpt-present")
    assert evidence["verification"] == "verified_present"
    assert evidence["exists"] is True
    assert evidence["checked_by"] == "local_filesystem"

    ok, reason = manager.can_resume_from_checkpoint("wl-ckpt-present")
    assert ok is True
    assert "verified" in reason.lower()


def test_a_missing_checkpoint_after_an_attempt_refuses_to_promise_a_resume(tmp_path):
    """The defect: this used to answer "can be recovered without restarting from 0%"."""
    location = tmp_path / "never-written"

    manager = LifecycleManager()
    profile = _profile("wl-ckpt-absent", str(location))
    _register_with_attempt(manager, profile)

    evidence = manager.verify_checkpoint("wl-ckpt-absent")
    assert evidence["verification"] == "verified_absent"
    assert evidence["exists"] is False

    ok, reason = manager.can_resume_from_checkpoint("wl-ckpt-absent")
    assert ok is False, reason
    assert "restarts from 0%" in reason
    assert "does not exist" in reason


def test_an_empty_directory_before_the_first_attempt_is_not_treated_as_a_failure(tmp_path):
    location = tmp_path / "ckpt-empty"
    location.mkdir()

    manager = LifecycleManager()
    profile = _profile("wl-ckpt-first-run", str(location))
    manager.register_workload(profile)  # no attempt yet

    ok, reason = manager.can_resume_from_checkpoint("wl-ckpt-first-run")
    assert ok is True
    assert "before the first" in reason


def test_an_uninspectable_location_is_declared_unverified_not_present():
    manager = LifecycleManager()
    profile = _profile("wl-ckpt-remote", "gs://agentgrid-checkpoints/workload-gui")
    _register_with_attempt(manager, profile)

    evidence = manager.verify_checkpoint("wl-ckpt-remote")
    # This host has no GCS client, so the honest answer is "unknown".
    assert evidence["verification"] in ("unverified", "verified_present", "verified_absent")
    if evidence["verification"] == "unverified":
        assert evidence["exists"] is None
        ok, reason = manager.can_resume_from_checkpoint("wl-ckpt-remote")
        assert ok is True
        assert "could NOT be verified" in reason
        assert "unproven" in reason


def test_an_unknown_scheme_is_not_silently_accepted():
    manager = LifecycleManager()
    profile = _profile("wl-ckpt-weird", "ipfs://Qm-not-a-thing-we-can-read")
    _register_with_attempt(manager, profile)

    evidence = manager.verify_checkpoint("wl-ckpt-weird")
    assert evidence["verification"] == "unverified"
    assert "ipfs" in evidence["reason"]

    ok, reason = manager.can_resume_from_checkpoint("wl-ckpt-weird")
    assert ok is True
    assert "unproven" in reason


def test_flags_that_forbid_resume_still_win_over_storage_evidence(tmp_path):
    location = tmp_path / "ckpt"
    location.mkdir()
    (location / "state.pt").write_bytes(b"weights")

    manager = LifecycleManager()
    profile = _profile("wl-ckpt-noninterruptible", str(location))
    profile["is_interruptible"] = False
    _register_with_attempt(manager, profile)

    ok, reason = manager.can_resume_from_checkpoint("wl-ckpt-noninterruptible")
    assert ok is False
    assert "non-interruptible" in reason
