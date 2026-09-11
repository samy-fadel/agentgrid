#!/usr/bin/env python3
"""Compile the dashboard's inline `text/babel` blocks to catch JSX syntax errors.

Why this exists
---------------
`compute_agent/static/index.html` ships its React app as an inline
`<script type="text/babel">` block that the browser hands to @babel/standalone
at load time. When that block has a syntax error the page renders blank and the
only signal is a browser console message -- every Python-side test still passes.
That is exactly how the "Adjacent JSX elements must be wrapped in an enclosing
tag" regression shipped unnoticed.

This script performs an equivalent parse offline so the failure surfaces in the
normal test suite.

Implementation note
-------------------
We use the TypeScript compiler bundled inside `dukpy` rather than dukpy's Babel.
dukpy ships Babel 6.26, which predates optional chaining (`?.`) and reports
false positives on valid modern source. The bundled TypeScript (5.7.x) parses
current JSX + ES2020 syntax and reports real syntax diagnostics, including
`JSX expressions must have one parent element` (TS2657), which is the exact
class of error @babel/standalone raises as "Adjacent JSX elements...".

Only *syntax* diagnostics are considered. Type errors are irrelevant here: the
source is plain JSX, not TypeScript, so unresolved names are expected.

Exit code 0 = every babel block parses. Exit code 1 = syntax error found.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGET = REPO_ROOT / "compute_agent" / "static" / "index.html"

BABEL_BLOCK_RE = re.compile(
    r'<script[^>]*type=["\']text/babel["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# TypeScript diagnostic codes that indicate a genuine parse/syntax failure.
# 1002-1499 is the syntactic range; 2657 is the JSX multi-root error which
# TypeScript classifies as semantic but which Babel raises as a syntax error.
_JSX_PARENT_ERROR = 2657


_TS_DRIVER = """
var out = ts.transpileModule(dukpy.tscode, {
  reportDiagnostics: true,
  compilerOptions: { jsx: 2, target: 1, allowJs: true, module: 1 },
  fileName: "dashboard.jsx"
});
var diags = (out.diagnostics || []).map(function (d) {
  var lc = (d.file && d.start != null)
    ? ts.getLineAndCharacterOfPosition(d.file, d.start)
    : null;
  return {
    code: d.code,
    line: lc ? lc.line + 1 : null,
    column: lc ? lc.character + 1 : null,
    msg: ts.flattenDiagnosticMessageText(d.messageText, " ")
  };
});
JSON.stringify(diags);
"""


class JsxCheckError(RuntimeError):
    """Raised when the JSX toolchain itself is unavailable."""


def _typescript_services_path() -> Path:
    import dukpy

    path = Path(dukpy.__file__).parent / "jsmodules" / "typescriptServices.js"
    if not path.exists():
        raise JsxCheckError(f"typescriptServices.js not found at {path}")
    return path


def extract_babel_blocks(html: str) -> list[str]:
    """Return the raw source of every inline `text/babel` script block."""
    return BABEL_BLOCK_RE.findall(html)


def syntax_diagnostics(source: str) -> list[dict]:
    """Parse `source` as JSX and return only syntax-level diagnostics."""
    try:
        from dukpy import evaljs
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise JsxCheckError(
            "dukpy is not installed; cannot verify JSX. "
            "Install with: python3 -m pip install --user dukpy"
        ) from exc

    ts_source = _typescript_services_path().read_text(encoding="utf-8")
    raw = evaljs((ts_source, _TS_DRIVER), tscode=source)
    diagnostics = json.loads(raw) if isinstance(raw, str) else (raw or [])

    return [
        d
        for d in diagnostics
        if (1002 <= int(d.get("code", 0)) < 1500) or int(d.get("code", 0)) == _JSX_PARENT_ERROR
    ]


def check_file(path: Path) -> list[str]:
    """Return a list of human-readable errors (empty when the file is clean)."""
    if not path.exists():
        return [f"{path}: file not found"]

    blocks = extract_babel_blocks(path.read_text(encoding="utf-8"))
    if not blocks:
        return [f'{path}: no <script type="text/babel"> block found']

    errors: list[str] = []
    for index, block in enumerate(blocks, start=1):
        for diag in syntax_diagnostics(block):
            errors.append(
                f"{path} (babel block #{index}) "
                f"line {diag.get('line')}:{diag.get('column')} "
                f"TS{diag.get('code')}: {diag.get('msg')}"
            )
    return errors


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv[1:]] or [DEFAULT_TARGET]

    all_errors: list[str] = []
    for target in targets:
        try:
            all_errors.extend(check_file(target))
        except JsxCheckError as exc:
            print(f"SKIP: {exc}", file=sys.stderr)
            return 2

    if all_errors:
        for err in all_errors:
            print(f"FAIL: {err}", file=sys.stderr)
        return 1

    for target in targets:
        print(f"OK: {target} JSX parses cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
