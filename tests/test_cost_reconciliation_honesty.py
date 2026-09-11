"""Cost reconciliation must not present declared figures as measurements.

Defect reproduced on the pre-fix code (script ``/tmp/dcumul/recon.py``):
a workload with two attempts, 4.00 EUR recorded with
``cost_status="calculated_from_usage"`` and 9.00 EUR recorded with
``cost_status="estimated"`` (the status the MCP tool assigns to a figure the
caller merely states), produced::

    tier2 (claimed 'calculated from usage'): 13.0
    note: Observed cost is calculated from authoritative active node-hours and a known rate; ...
    caller-supplied billed figure -> reconciled_billed 99.0

Two lies in one payload: a stated number was summed into the "calculated from
usage" tier under a note asserting the figure was authoritative, and an
arbitrary number handed to the function was labelled as reconciled billing
although this project has no billing export integration.

These tests pin the corrected behaviour.
"""

from __future__ import annotations

import pytest

from agentic_compute.history import get_history_store
from agentic_compute.models import (
    ExecutionAttempt,
    ExecutionHistoryRecord,
    WorkloadProfile,
)


def _seed(workload_id: str, attempts: list[tuple[str, float, float]], estimated: float = 10.0):
    """Create a workload with the given ``(cost_status, cost, minutes)`` attempts."""
    store = get_history_store()
    profile = WorkloadProfile(
        workload_id=workload_id,
        name=f"name-{workload_id}",
        cpu_requested=4,
        command="python train.py",
    )
    store.save_workload_profile(profile)
    store.save_history_record(
        ExecutionHistoryRecord(
            workload_id=workload_id,
            workload_name=profile.name,
            profile=profile,
            initial_estimated_cost_eur=estimated,
            initial_estimated_duration_minutes=60.0,
        )
    )
    for index, (status, cost, minutes) in enumerate(attempts, start=1):
        store.record_attempt(
            ExecutionAttempt(
                attempt_id=f"{workload_id}-{index}",
                workload_id=workload_id,
                attempt_number=index,
                cost_calculated_eur=cost,
                elapsed_minutes=minutes,
                cost_status=status,
            )
        )
    return store


def test_declared_attempt_is_excluded_from_the_measured_tier():
    """The exact pre-fix scenario: 4.00 measured + 9.00 declared must not read 13.00 measured."""
    store = _seed(
        "wl-recon-mix",
        [("calculated_from_usage", 4.0, 30.0), ("estimated", 9.0, 40.0)],
    )

    out = store.reconcile_costs("wl-recon-mix")

    assert out["cost_tiers"]["tier2_calculated_from_usage_eur"] == 4.0
    assert out["declared_unverified_cost_eur"] == 9.0
    assert out["declared_attempt_ids"] == ["wl-recon-mix-2"]


def test_nothing_is_lost_when_the_tiers_are_split():
    """Splitting the basis must not drop money: the recorded total still holds every attempt."""
    store = _seed(
        "wl-recon-total",
        [("calculated_from_usage", 4.0, 30.0), ("estimated", 9.0, 40.0)],
    )

    out = store.reconcile_costs("wl-recon-total")

    assert out["total_recorded_cost_eur"] == 13.0
    assert out["cost_breakdown"]["total_recorded_cost_eur"] == 13.0
    # The delta against the estimate is computed on everything recorded, and
    # says so, rather than silently comparing the estimate to a partial sum.
    assert out["cost_breakdown"]["cost_delta_basis"] == "total_recorded_cost_eur"
    assert out["cost_breakdown"]["cost_delta_eur"] == pytest.approx(3.0)


def test_mixed_basis_note_names_both_parts():
    store = _seed(
        "wl-recon-note-mixed",
        [("calculated_from_usage", 4.0, 30.0), ("estimated", 9.0, 40.0)],
    )

    note = store.reconcile_costs("wl-recon-note-mixed")["cost_breakdown"]["cost_basis_note"]

    assert "Mixed basis" in note
    assert "4.00EUR calculated from observed node-hours" in note
    assert "9.00EUR declared by a caller and never verified" in note


def test_fully_declared_note_does_not_claim_any_measurement():
    store = _seed("wl-recon-note-declared", [("estimated", 9.0, 40.0)])

    out = store.reconcile_costs("wl-recon-note-declared")
    note = out["cost_breakdown"]["cost_basis_note"]

    assert out["cost_tiers"]["tier2_calculated_from_usage_eur"] == 0.0
    assert "All 9.00EUR was declared by a caller and never verified" in note
    assert "calculated from observed node-hours" not in note


def test_fully_measured_note_keeps_the_measured_wording():
    store = _seed("wl-recon-note-measured", [("calculated_from_usage", 4.0, 30.0)])

    out = store.reconcile_costs("wl-recon-note-measured")
    note = out["cost_breakdown"]["cost_basis_note"]

    assert out["cost_tiers"]["tier2_calculated_from_usage_eur"] == 4.0
    assert out["declared_unverified_cost_eur"] == 0.0
    assert "calculated from active node-hours" in note
    # Even here the note must not pretend billing was reconciled.
    assert "external GCP Cloud Billing export" in note


def test_unqualified_billed_figure_is_reported_as_caller_supplied():
    store = _seed("wl-recon-billed", [("calculated_from_usage", 4.0, 30.0)])

    out = store.reconcile_costs("wl-recon-billed", billed_cost_eur=99.0)

    assert out["billed_reconciliation_status"] == "caller_supplied_unverified"
    assert out["tier3_source"] == "caller_supplied"
    # The number is still surfaced -- it is simply not called a reconciliation.
    assert out["cost_tiers"]["tier3_reconciled_billed_eur"] == 99.0
    assert out["cost_breakdown"]["reconciliation_status"] == "caller_supplied_unverified"


def test_only_a_billing_export_source_yields_a_reconciliation():
    store = _seed("wl-recon-export", [("calculated_from_usage", 4.0, 30.0)])

    out = store.reconcile_costs(
        "wl-recon-export", billed_cost_eur=4.15, billed_cost_source="gcp_billing_export"
    )

    assert out["billed_reconciliation_status"] == "reconciled_billed"
    assert out["tier3_source"] == "gcp_billing_export"
    assert out["cost_tiers"]["tier3_reconciled_billed_eur"] == 4.15


def test_absent_billed_figure_states_the_source_is_not_integrated():
    store = _seed("wl-recon-absent", [("calculated_from_usage", 4.0, 30.0)])

    out = store.reconcile_costs("wl-recon-absent")

    assert out["billed_reconciliation_status"] == "source_not_integrated"
    assert out["tier3_source"] is None
    assert out["cost_tiers"]["tier3_reconciled_billed_eur"] is None


def test_attempt_recorded_as_reconciled_billed_is_neither_measured_nor_declared():
    store = _seed(
        "wl-recon-billedattempt",
        [
            ("calculated_from_usage", 4.0, 30.0),
            ("estimated", 9.0, 40.0),
            ("reconciled_billed", 2.0, 10.0),
        ],
    )

    out = store.reconcile_costs("wl-recon-billedattempt")

    assert out["cost_tiers"]["tier2_calculated_from_usage_eur"] == 4.0
    assert out["declared_unverified_cost_eur"] == 9.0
    assert out["total_recorded_cost_eur"] == 15.0


def test_mcp_get_cost_history_flags_the_caller_supplied_figure(monkeypatch):
    """The MCP tool must not let a model-stated number pass as reconciled billing."""
    from agentic_compute import mcp_server

    store = _seed("wl-recon-mcp", [("calculated_from_usage", 4.0, 30.0)])
    monkeypatch.setattr(mcp_server, "_history_store", store)

    result = mcp_server.get_cost_history(workload_id="wl-recon-mcp", reconcile_billed_eur=42.0)

    assert result["reconciliation"]["billed_reconciliation_status"] == "caller_supplied_unverified"
    assert result["reconciliation"]["tier3_source"] == "caller_supplied"
    assert "supplied by the caller" in result["billed_figure_caveat"]


def test_mcp_get_cost_history_without_a_figure_has_no_caveat(monkeypatch):
    from agentic_compute import mcp_server

    store = _seed("wl-recon-mcp2", [("calculated_from_usage", 4.0, 30.0)])
    monkeypatch.setattr(mcp_server, "_history_store", store)

    result = mcp_server.get_cost_history(workload_id="wl-recon-mcp2")

    assert "billed_figure_caveat" not in result
    assert result["reconciliation"]["billed_reconciliation_status"] == "source_not_integrated"
