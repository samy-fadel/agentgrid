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
