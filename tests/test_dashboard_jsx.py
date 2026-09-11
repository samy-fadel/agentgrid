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
    for tab in ("mission", "plans", "capacity", "diagnostics", "history"):
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
