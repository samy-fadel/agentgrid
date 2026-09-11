#!/usr/bin/env python3
"""Render the dashboard's React component in an embedded JS engine.

Why this exists
---------------
There is no Node.js and no headless browser in this environment, so the usual
"open it and click around" verification is unavailable. A JSX *parse* check
(``tools/check_jsx.py``) proves the file compiles, but says nothing about
whether the component actually renders: a reference to an undefined variable, a
handler that does not exist, or a tab that renders nothing would all still
compile.

This script goes one step further. It transpiles the ``text/babel`` block with
the TypeScript compiler bundled in ``dukpy``, then executes it inside duktape
against a deliberately tiny React implementation:

* ``useState`` returns the initial value and a no-op setter;
* ``useEffect`` / ``useRef`` are recorded but effects are **not** run, because
  they perform network calls;
* ``createElement`` builds a plain tree, so the whole render path is executed.

The component is rendered once per tab. Any ``ReferenceError`` or ``TypeError``
raised while building the tree is a real defect that would appear in a browser
console. The rendered tree is then searched for the text markers each tab is
expected to produce, which catches a tab that silently renders nothing.

Limitations, stated plainly: this is not a browser. It does not exercise CSS,
event dispatch, network behaviour, or effect-driven state transitions. It
verifies that the render path is free of undefined references and that each tab
produces content.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    import dukpy
except ImportError:  # pragma: no cover
    print("dukpy is not installed; cannot run the render check", file=sys.stderr)
    raise SystemExit(2)

from check_jsx import DEFAULT_TARGET, extract_babel_blocks  # type: ignore

TS_PATH = Path(dukpy.__file__).parent / "jsmodules" / "typescriptServices.js"

TRANSPILE_DRIVER = """
var out = ts.transpileModule(dukpy.tscode, {
  reportDiagnostics: false,
  compilerOptions: { jsx: 2, target: 1, allowJs: true, module: 1 }
});
out.outputText;
"""

# A minimal React that is enough to walk the render path.
HARNESS_PREFIX = r"""
var __ACTIVE_TAB__ = "%(tab)s";
var __errors = [];

function __mkEl(type, props, children) {
  return { type: type, props: props || {}, children: children };
}

var React = {
  useState: function (initial) {
    // The active tab is injected so every panel can be rendered in turn.
    if (React.__stateIndex === React.__tabStateIndex) {
      React.__stateIndex++;
      return [__ACTIVE_TAB__, function () {}];
    }
    React.__stateIndex++;
    return [initial, function () {}];
  },
  useEffect: function () {},
  useRef: function (v) { return { current: v }; },
  useMemo: function (f) { return f(); },
  useCallback: function (f) { return f; },
  createElement: function (type, props) {
    var children = Array.prototype.slice.call(arguments, 2);
    if (typeof type === "function") {
      try {
        return type(props || {});
      } catch (e) {
        __errors.push(String(e));
        return __mkEl("error", {}, []);
      }
    }
    return __mkEl(type, props, children);
  },
  Fragment: "fragment"
};

var ReactDOM = {
  createRoot: function () {
    return {
      render: function (element) { __root = element; }
    };
  }
};

var document = {
  getElementById: function () { return {}; },
  addEventListener: function () {}
};
var window = { location: { origin: "http://localhost:8080" }, addEventListener: function () {} };
var console = { log: function () {}, error: function () {}, warn: function () {} };
var fetch = function () { return { then: function () { return this; }, catch: function () { return this; } }; };
var setInterval = function () { return 0; };
var clearInterval = function () {};
var setTimeout = function () { return 0; };
var EventSource = function () { this.close = function () {}; };
var navigator = { clipboard: { writeText: function () {} } };
var __root = null;
"""

HARNESS_SUFFIX = r"""
function __flatten(node, acc) {
  if (node === null || node === undefined || node === false || node === true) return acc;
  if (typeof node === "string" || typeof node === "number") {
    acc.push(String(node));
    return acc;
  }
  if (Object.prototype.toString.call(node) === "[object Array]") {
    for (var i = 0; i < node.length; i++) __flatten(node[i], acc);
    return acc;
  }
  if (node.type) acc.push("<" + (typeof node.type === "string" ? node.type : "component") + ">");
  if (node.props) {
    for (var k in node.props) {
      if (k === "children") __flatten(node.props[k], acc);
    }
  }
  if (node.children) __flatten(node.children, acc);
  return acc;
}

JSON.stringify({ errors: __errors, text: __flatten(__root, []).join(" ") });
"""


def _transpile(source: str) -> str:
    ts_source = TS_PATH.read_text(encoding="utf-8")
    return dukpy.evaljs((ts_source, TRANSPILE_DRIVER), tscode=source)


def _find_tab_state_index(source: str) -> int:
    """Index of the ``useState`` call that holds the active tab."""
    calls = [m.start() for m in re.finditer(r"useState\s*\(", source)]
    marker = re.search(r'useState\s*\(\s*"mission"', source)
    if marker is None:
        raise RuntimeError('could not locate the useState call holding the active tab')
    return calls.index(marker.start())


def render_tab(source: str, tab: str) -> dict:
    js = _transpile(source)
    tab_index = _find_tab_state_index(source)
    prefix = HARNESS_PREFIX % {"tab": tab}
    prefix += "React.__stateIndex = 0;\nReact.__tabStateIndex = %d;\n" % tab_index
    result = dukpy.evaljs(prefix + js + HARNESS_SUFFIX)
    return json.loads(result)


TAB_MARKERS = {
    "mission": ["Mission"],
    "plans": ["Comparaison de plans"],
    "capacity": ["Recherche de capacité compatible"],
    "diagnostics": ["Diagnostic des blocages"],
    "history": ["Coûts réels"],
}


def check_file(path: Path) -> list[str]:
    blocks = extract_babel_blocks(path.read_text(encoding="utf-8"))
    if not blocks:
        return [f"{path}: no <script type=\"text/babel\"> block found"]
    first = blocks[0]
    source = first[1] if isinstance(first, tuple) else first

    problems: list[str] = []
    for tab, markers in TAB_MARKERS.items():
        outcome = render_tab(source, tab)
        for err in outcome["errors"]:
            problems.append(f"{path}[tab={tab}]: render error: {err}")
        text = outcome["text"]
        if not text.strip():
            problems.append(f"{path}[tab={tab}]: rendered nothing")
            continue
        for marker in markers:
            if marker not in text:
                problems.append(
                    f"{path}[tab={tab}]: expected content {marker!r} was not rendered"
                )
    return problems


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else DEFAULT_TARGET
    problems = check_file(target)
    if problems:
        for p in problems:
            print(p, file=sys.stderr)
        return 1
    print(f"OK: {target} renders every tab without a render-time error")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
