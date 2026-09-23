"""Guard the dashboard's inline JSX against syntax regressions.

The React app in `compute_agent/static/index.html` is compiled in the browser by
@babel/standalone. A syntax error there yields a blank page and a console-only
error, which every Python-level test would otherwise miss -- this is exactly how
the unterminated `{activeTab === 'mission' && (` block shipped.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.check_jsx import (  # noqa: E402
    DEFAULT_TARGET,
    JsxCheckError,
    check_file,
    extract_babel_blocks,
    syntax_diagnostics,
)


def _require_toolchain() -> None:
    try:
        syntax_diagnostics("const x = 1;")
    except JsxCheckError as exc:
        pytest.skip(f"JSX toolchain unavailable: {exc}")


def test_dashboard_jsx_compiles() -> None:
    """The shipped dashboard must parse as valid JSX."""
    _require_toolchain()
    errors = check_file(DEFAULT_TARGET)
    assert errors == [], "Dashboard JSX failed to compile:\n" + "\n".join(errors)


def test_dashboard_has_single_babel_block() -> None:
    """Sanity check that the checker is actually inspecting the app source."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    blocks = extract_babel_blocks(html)
    assert len(blocks) == 1
    # The block must contain the app, not just a stub.
    assert "function App" in blocks[0] or "const App" in blocks[0]
    assert "ReactDOM.createRoot" in blocks[0]


def test_checker_detects_adjacent_jsx_elements() -> None:
    """Negative control: the exact error class that shipped must be caught.

    Without this, a checker that silently returns "no diagnostics" for every
    input would pass `test_dashboard_jsx_compiles` while providing no coverage.
    """
    _require_toolchain()
    broken = """
    const Broken = () => {
      return (
        <div>first</div>
        <div>second</div>
      );
    };
    """
    diags = syntax_diagnostics(broken)
    assert diags, "checker failed to flag adjacent JSX elements"
    assert any(d["code"] == 2657 for d in diags), diags


def test_checker_detects_unterminated_conditional_block() -> None:
    """Negative control for the unclosed `{cond && (` pattern."""
    _require_toolchain()
    broken = """
    const Broken = () => {
      return (
        <div>
          {show && (
            <span>content</span>
        </div>
      );
    };
    """
    diags = syntax_diagnostics(broken)
    assert diags, "checker failed to flag an unterminated conditional block"


def test_checker_accepts_modern_syntax() -> None:
    """Optional chaining and template literals must not be false positives."""
    _require_toolchain()
    ok = """
    const Fine = ({ snap }) => {
      const label = snap?.workload?.status ?? "unknown";
      return <div className={`tag ${label}`}>{label}</div>;
    };
    """
    assert syntax_diagnostics(ok) == []


# ---------------------------------------------------------------------------
# Render-time verification
#
# There is no browser in this environment, so "it compiles" is not enough: an
# undefined variable or a missing handler compiles fine and only fails at
# render time. tools/render_check.py executes the component in an embedded JS
# engine against a minimal React. These tests drive it, including a negative
# control proving the harness would actually catch such a defect.
# ---------------------------------------------------------------------------

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))


def test_every_dashboard_tab_renders_without_error():
    import render_check

    problems = render_check.check_file(render_check.DEFAULT_TARGET)
    assert problems == [], "\n".join(problems)


def test_render_harness_detects_an_undefined_reference():
    """Negative control: the harness must fail on a genuinely broken component."""
    import render_check

    source = """
      const { useState } = React;
      function App() {
        const [activeTab, setActiveTab] = useState("mission");
        return <div>{thisVariableDoesNotExist}</div>;
      }
      ReactDOM.createRoot(document.getElementById("root")).render(<App />);
    """
    outcome = render_check.render_tab(source, "mission")
    assert outcome["errors"], "an undefined reference was not reported"
    assert "ReferenceError" in outcome["errors"][0]


def test_render_harness_detects_a_tab_that_renders_nothing():
    """Negative control: a tab with no panel must be reported, not silently pass."""
    import render_check

    source = """
      const { useState } = React;
      function App() {
        const [activeTab, setActiveTab] = useState("mission");
        return <div>{activeTab === "mission" && (<span>Mission</span>)}</div>;
      }
      ReactDOM.createRoot(document.getElementById("root")).render(<App />);
    """
    mission = render_check.render_tab(source, "mission")
    assert "Mission" in mission["text"]

    plans = render_check.render_tab(source, "plans")
    assert "Comparaison de plans" not in plans["text"]


def test_dashboard_exposes_a_panel_for_every_navigation_tab():
    """Every tab button must have a matching conditional panel.

    A visible tab with no panel behind it is exactly the "button with no
    effect" the acceptance criteria forbid.
    """
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    for tab in ("mission", "plans", "capacity", "diagnostics", "history", "finops"):
        assert f'setActiveTab("{tab}")' in html, f"no navigation button for tab {tab}"
        assert f"activeTab === '{tab}' &&" in html, f"no rendered panel for tab {tab}"


def test_dashboard_control_mode_selector_is_pushed_to_the_server():
    """The mode is enforced server-side, so the selector must call the API."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    for mode in ("advisory", "validation", "delegation"):
        assert f'applyControlMode("{mode}")' in html
    assert "/api/workloads/${WORKLOAD_ID}/control" in html


def test_dashboard_sends_the_operator_values_not_hard_coded_ones():
    """Budget, deadline and size must each be transmitted from their own field."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "budget_amount: Number(budget)" in html
    assert "deadline_minutes_from_start: Number(deadline)" in html
    assert "cpu_requested: Number(cpuRequested)" in html
    # The previously hard-coded search parameters must be gone.
    assert "cpu_requested=16&memory_gb_requested=32" not in html


def test_dashboard_reports_the_server_verdict_for_approval_and_execution():
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    # Approval is only shown when the server said "approved".
    assert 'body.status === "approved"' in html
    assert "setApprovalError" in html
    # Execution shows whatever the server returned, including "blocked".
    assert "setExecutionResult(body)" in html
    assert "already_submitted" in html


# ---------------------------------------------------------------------------
# UI <-> model contract
#
# A React panel that reads a field the API never returns renders an empty cell
# and passes every syntax and render check. This caught real defects: the
# capacity table read `c.cpu` (the model exposes `cpu_count`), the diagnostics
# panel read `d.title`/`d.detail`/`d.severity` (the model exposes `category`/
# `observed_facts`/`confirmed`), and the history table read `r.state` and
# `r.estimated_cost_eur` (the model exposes `final_status` and
# `initial_estimated_cost_eur`).
# ---------------------------------------------------------------------------

import re

_PANEL_START = re.compile(r"\{activeTab === '([a-z]+)' &&")


def _panel_blocks(html: str) -> dict[str, str]:
    """Split the render tree into one source slice per tab panel.

    Scoping matters: the loop variable ``c`` is bound to a ``CandidateAllocation``
    in the mission tab and to a ``CapacityCandidate`` in the capacity tab. A
    whole-file scan therefore reports every mission field as missing from
    ``CapacityCandidate`` and drowns the real defects in noise.
    """
    starts = [(m.group(1), m.start()) for m in _PANEL_START.finditer(html)]
    blocks: dict[str, str] = {}
    for index, (tab, start) in enumerate(starts):
        end = starts[index + 1][1] if index + 1 < len(starts) else len(html)
        blocks[tab] = html[start:end]
    return blocks


def _fields_read_on(html: str, variable: str) -> set[str]:
    """Property names read from `variable` inside the dashboard source."""
    return set(re.findall(rf"\b{re.escape(variable)}\.([A-Za-z_][A-Za-z0-9_]*)", html))


def _contract_problems(blocks: dict[str, str]) -> list[str]:
    from agentic_compute.models import (
        CapacityCandidate,
        DiagnosticItem,
        ExecutionHistoryRecord,
        ExecutionPlan,
    )

    # (tab, loop variable) -> the model the panel actually iterates over.
    bindings = {
        ("plans", "plan"): ExecutionPlan,
        ("capacity", "c"): CapacityCandidate,
        ("diagnostics", "d"): DiagnosticItem,
        ("history", "r"): ExecutionHistoryRecord,
    }
    # Names that are JS built-ins or local helpers rather than model fields.
    js_builtins = {"map", "length", "filter", "toFixed", "join", "slice", "props"}

    problems = []
    for (tab, variable), model in bindings.items():
        block = blocks.get(tab)
        if block is None:
            problems.append(f"the `{tab}` panel is missing from the render tree")
            continue
        allowed = set(model.model_fields) | js_builtins
        for field in sorted(_fields_read_on(block, variable)):
            if field not in allowed:
                problems.append(
                    f"the `{tab}` panel reads `{variable}.{field}` but "
                    f"{model.__name__} has no such field"
                )
    return problems


def test_panels_only_read_fields_the_api_actually_returns():
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    problems = _contract_problems(_panel_blocks(html))
    assert not problems, "\n".join(problems)


def test_the_contract_check_detects_a_field_that_does_not_exist():
    """Negative control: without this, the test above could pass while blind."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    blocks = _panel_blocks(html)
    blocks["history"] = blocks["history"] + "\n{r.definitely_not_a_field}\n"
    problems = _contract_problems(blocks)
    assert any("definitely_not_a_field" in p for p in problems), problems


def test_capacity_panel_shows_quota_and_provenance():
    """Quota state and data provenance must be visible, not implied."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "c.quota_status" in html
    assert "c.data_provenance" in html
    assert "c.state_stage" in html


def test_diagnostics_panel_keeps_the_source_of_each_finding():
    """A Slurm QoS limit and a GCP quota are different problems."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "d.source" in html
    assert "d.confirmed" in html


def test_history_panel_compares_estimated_and_observed_cost():
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "r.initial_estimated_cost_eur" in html
    assert "r.final_calculated_cost_eur" in html
    assert "r.cost_comparison_delta_eur" in html
    assert "r.reconciliation_status" in html


# ---------------------------------------------------------------------------
# Visualization & experience wiring
#
# Several analytics endpoints were computed server-side and read by nothing:
# /api/portfolio/finops, /api/workloads/{id}/anomalies, and the shock parameters
# of /api/plans/what-if. A panel that exists but calls no endpoint, or calls one
# and reads keys it does not return, is the same defect class the contract tests
# above already guard for the older tabs.
# ---------------------------------------------------------------------------


def test_finops_panel_calls_the_portfolio_endpoint():
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "/api/portfolio/finops" in html
    assert "setFinops(" in html
    assert "setFinopsError" in html


def test_finops_panel_reads_keys_the_analytics_payload_returns():
    """The board must read the aggregator's own key names, not invented ones."""
    from agentic_compute.anomaly_detector import compute_portfolio_finops_analytics

    payload = compute_portfolio_finops_analytics(records=[])
    html = DEFAULT_TARGET.read_text(encoding="utf-8")

    for group in ("financial_tiers", "sustainability_metrics", "reliability_and_governance"):
        assert f"finops.{group}" in html, f"the FinOps board never reads {group}"

    # Every dotted read of a known group must name a key the payload carries.
    for group in ("financial_tiers", "sustainability_metrics", "reliability_and_governance"):
        read = set(re.findall(rf"finops\.{group}\.([A-Za-z_][A-Za-z0-9_]*)", html))
        unknown = read - set(payload[group])
        assert not unknown, f"FinOps board reads unknown {group} keys: {sorted(unknown)}"

    # The three reconciliation tiers must stay distinguishable on screen.
    for tier in (
        "tier1_estimated_total_eur",
        "tier2_verified_usage_total_eur",
        "tier3_reconciled_billed_total_eur",
    ):
        assert tier in html, f"tier {tier} is not displayed"


def test_mission_tab_monitors_live_drift_from_observed_telemetry():
    """The health ribbon must report the observed run, not the form values."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "/api/workloads/${WORKLOAD_ID}/anomalies" in html
    # Elapsed time and spend come from the runtime snapshot.
    assert "elapsed_minutes: num(clk.current_time_minutes)" in html
    assert "current_cost_eur: num(wl.accrued_cost_eur)" in html
    # The verdict displayed is the server's own.
    assert "anomalyReport.health_status" in html
    assert "anomalyReport.anomalies.map" in html


def test_anomaly_ribbon_reads_telemetry_keys_the_detector_returns():
    from agentic_compute.anomaly_detector import detect_workload_anomalies

    report = detect_workload_anomalies(workload_id="wl-ui-contract")
    html = DEFAULT_TARGET.read_text(encoding="utf-8")

    read = set(re.findall(r"anomalyReport\.telemetry\.([A-Za-z_][A-Za-z0-9_]*)", html))
    assert read, "the ribbon displays no telemetry at all"
    unknown = read - set(report["telemetry"])
    assert not unknown, f"the ribbon reads unknown telemetry keys: {sorted(unknown)}"

    # Young-Daly exposure is the reason the ribbon exists for Spot workloads.
    assert "young_daly_optimal_checkpoint_minutes" in read
    assert "uncommitted_minutes_at_risk" in read


def test_what_if_sliders_send_the_operator_shock_parameters():
    """The engine always accepted shocks; the UI used to send only defaults."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "/api/plans/what-if" in html
    assert "forced_preemptions: Number(whatIfPreemptions)" in html
    assert "budget_shock_pct: Number(whatIfBudgetShock)" in html
    assert "deadline_compression_pct: Number(whatIfDeadlineCompression)" in html
    # Each shock needs its own control.
    for setter in (
        "setWhatIfPreemptions(Number(e.target.value))",
        "setWhatIfBudgetShock(Number(e.target.value))",
        "setWhatIfDeadlineCompression(Number(e.target.value))",
    ):
        assert setter in html, f"missing slider wiring: {setter}"
    # A live run must be distinguishable from the seeded default run.
    assert "scénario opérateur" in html
    assert "scénario par défaut" in html


def test_what_if_panel_reads_every_scenario_the_simulator_returns():
    """Reporting only the Spot storm hides two of the three shocks."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    for scenario in (
        "spot_storm_scenario",
        "budget_shock_scenario",
        "deadline_compression_scenario",
    ):
        assert f"sr.{scenario}.survives" in html, f"{scenario} verdict is never displayed"


def test_pareto_scatter_is_fed_by_the_compared_plans():
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "function ParetoScatter" in html
    assert "plans={planComparison.plans}" in html
    # The four axes of the 4D analysis must all reach the plot.
    for axis in (
        "estimated_cost_eur",
        "total_time_to_result_minutes",
        "interruption_risk_score",
        "green_tier",
    ):
        assert axis in html, f"the scatter ignores the {axis} dimension"
    assert "is_pareto_optimal" in html


def test_execution_opens_an_impact_preview_before_submitting():
    """Actuation must be a confirmed step, not a single unguarded click."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "setPendingExecution(plan)" in html
    assert "pendingExecution &&" in html
    # The direct-submit binding on the card button must be gone.
    assert "onClick={() => handleExecutePlan(plan)}" not in html
    # Confirming is what actually submits.
    assert "handleExecutePlan(plan);" in html


def test_impact_preview_states_the_server_enforced_envelope():
    """The preview must show the mode and approval the server holds."""
    html = DEFAULT_TARGET.read_text(encoding="utf-8")
    assert "Mode appliqué par le serveur" in html
    assert "Approbation enregistrée" in html
    assert "remaining_delegated_budget_eur" in html
    # And it must not claim to be the gate.
    assert "Le serveur revérifiera" in html


# ---------------------------------------------------------------------------
# Populated render
#
# `test_every_dashboard_tab_renders_without_error` renders each tab with the
# component's declared initial state, which is `null` for every data-driven
# panel. The Pareto scatter maths, the drift gauges, the reconciliation bars
# and the impact preview are therefore never executed by it: a TypeError on a
# nested key, or a NaN reaching an SVG coordinate, would pass unnoticed.
#
# These tests seed the state with responses from the real app and render again.
# ---------------------------------------------------------------------------

_WL = "workload-gui"

_PROFILE = {
    "workload_id": _WL,
    "name": "compute-simulation",
    "cpu_requested": 16,
    "memory_mb_requested": 32768,
    "deadline_minutes_from_start": 25,
    "budget_amount": 5.0,
    "is_parallelizable": True,
    "supports_checkpointing": True,
    "checkpoint_location": "gs://agentgrid-checkpoints/workload-gui",
}


def _live_seeds() -> dict:
    """Collect the payloads the new panels consume, from the real endpoints."""
    from fastapi.testclient import TestClient

    from compute_agent.app import app

    client = TestClient(app)

    comparison = client.post(
        "/api/plans/compare",
        json={"workload_profile": _PROFILE, "cluster_total_cpu": 128},
    ).json()
    plans = comparison["plans"]
    assert plans, "the comparison returned no plan to render"

    what_if = client.post(
        "/api/plans/what-if",
        json={
            "workload_id": _WL,
            "plans": plans,
            "workload_profile": _PROFILE,
            # Deliberately not the defaults: the panel must show the operator's
            # own scenario, not the one baked into the comparison payload.
            "forced_preemptions": 5,
            "budget_shock_pct": -60.0,
            "deadline_compression_pct": -50.0,
        },
    ).json()

    # Telemetry chosen to trip every detector, so the ribbon renders its
    # populated branch rather than the "no anomaly" one.
    anomalies = client.post(
        f"/api/workloads/{_WL}/anomalies",
        json={
            "workload_profile": _PROFILE,
            "elapsed_minutes": 18.0,
            "current_cost_eur": 4.4,
            "progress_pct": 25.0,
            "minutes_since_last_checkpoint": 18.0,
        },
    ).json()
    assert anomalies["anomaly_count"] > 0, "expected the seeded run to be drifting"

    control = client.post(
        f"/api/workloads/{_WL}/control",
        json={"control_mode": "delegation", "delegation_policy": {"max_budget_eur": 10.0}},
    ).json()

    # An empty portfolio renders only the placeholder, so the board is fed a
    # populated aggregate built through the real aggregator.
    from agentic_compute.anomaly_detector import compute_portfolio_finops_analytics

    finops = compute_portfolio_finops_analytics(
        records=[
            {
                "workload_id": "wl-a",
                "control_mode": "delegation",
                "initial_estimated_cost_eur": 4.0,
                "final_calculated_cost_eur": 3.6,
                "reconciliation_status": "calculated_from_usage",
                "final_actual_duration_minutes": 22.0,
                "approved_plan": {
                    "cpu": 16,
                    "gpu": 0,
                    "machine_type": "n2-standard-16",
                    "region": "europe-west1",
                    "provisioning_model": "100% Spot",
                },
                "profile": {"deadline_minutes_from_start": 25.0},
            },
            {
                "workload_id": "wl-b",
                "control_mode": "validation",
                "initial_estimated_cost_eur": 8.4,
                "final_calculated_cost_eur": 7.3,
                "reconciliation_status": "reconciled_billed",
                "final_actual_duration_minutes": 48.0,
                "approved_plan": {
                    "cpu": 32,
                    "gpu": 0,
                    "machine_type": "n2-standard-32",
                    "region": "asia-east1",
                    "provisioning_model": "80% Spot / 20% Standard",
                },
                "profile": {"deadline_minutes_from_start": 40.0},
            },
        ]
    )
    assert finops["financial_tiers"]["spot_hedging_savings_eur"] > 0

    return {
        "planComparison": comparison,
        "whatIfResult": what_if,
        "anomalyReport": anomalies,
        "serverControl": control,
        "finops": finops,
        # State-driven, not tab-driven: the modal must render on every tab.
        "pendingExecution": plans[0],
    }


_POPULATED_MARKERS = {
    "mission": ["Santé du workload en direct", "CRITICAL_DRIFT", "Young-Daly"],
    "plans": ["Frontière de Pareto 4D", "Simulateur de stress What-If", "scénario opérateur"],
    "capacity": ["Recherche de capacité compatible"],
    "diagnostics": ["Diagnostic des blocages"],
    "history": ["Coûts réels"],
    "finops": [
        "Portefeuille FinOps",
        "Réconciliation financière à 3 tiers",
        "Bilan carbone et routage vert",
        "Répartition des modes de gouvernance",
    ],
}


def test_populated_panels_render_without_error():
    """Every panel must survive being handed the data it was built for."""
    import render_check

    source = extract_babel_blocks(DEFAULT_TARGET.read_text(encoding="utf-8"))[0]
    seeded = render_check.seed_state(source, _live_seeds())

    problems: list[str] = []
    for tab, markers in _POPULATED_MARKERS.items():
        outcome = render_check.render_tab(seeded, tab)
        problems.extend(f"[{tab}] render error: {e}" for e in outcome["errors"])
        text = outcome["text"]
        for marker in markers:
            if marker not in text:
                problems.append(f"[{tab}] expected {marker!r} in the rendered output")
        if "Prévisualisation d'impact" not in text:
            problems.append(f"[{tab}] the impact preview modal did not render")
        # A NaN in a coordinate or a figure is a rendered defect, not a warning.
        if "NaN" in text:
            problems.append(f"[{tab}] rendered a NaN")

    assert not problems, "\n".join(problems)


def test_seeding_fails_loudly_when_a_state_hook_is_renamed():
    """Negative control: a silent no-op seed would revert the test to empty state."""
    import render_check

    source = extract_babel_blocks(DEFAULT_TARGET.read_text(encoding="utf-8"))[0]
    with pytest.raises(RuntimeError, match="could not seed state"):
        render_check.seed_state(source, {"noSuchState": {"a": 1}})


def test_populated_render_detects_a_missing_nested_key():
    """Negative control: the populated render must actually catch a bad read."""
    import render_check

    source = extract_babel_blocks(DEFAULT_TARGET.read_text(encoding="utf-8"))[0]
    # A FinOps payload missing `financial_tiers` is exactly the shape drift the
    # board would hit if the aggregator renamed a group.
    broken = render_check.seed_state(
        source,
        {"finops": {"total_workloads": 2, "sustainability_metrics": {}}},
    )
    outcome = render_check.render_tab(broken, "finops")
    assert outcome["errors"], "a missing payload group was not reported"


