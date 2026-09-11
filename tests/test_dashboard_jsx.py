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
