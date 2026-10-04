#!/usr/bin/env python3
"""Static guard: flag Python constructs that crash under cron's python3.9 (/usr/bin/python3).

Cron and launchd invoke several scripts with a bare `python3` that, in a minimal-PATH
environment, resolves to /usr/bin/python3 = 3.9.6 (see bin/nightly-review.sh's pin comment).
These 3.10+ / 3.12+ constructs silently break there:

  1. PEP-604 `X | Y` type unions in ANY EVALUATED position — an annotation (def arg/return, or a
     module-/class-level variable annotation), a plain value (`Number = int | None`, a default
     value, a class base, a decorator arg, a bare expression, a cast/return value…). On 3.9 the
     `|` evaluates `type.__or__` and raises `TypeError: unsupported operand type(s) for |`.
  2. 3.10+ SYNTAX: `match` statements.
  3. 3.12+ SYNTAX: PEP-695 `type X = …` alias statements and `def f[T](…)` / `class C[T]` generics.

Detected STATICALLY via ast (runs under any 3.9+ interpreter — does NOT need a 3.9 interpreter
installed). A PEP-604 union is identified structurally, NOT by "is it in an annotation": a `X | Y`
is treated as a *type* union (vs an integer/set/IntFlag bitwise-or) iff one operand is the literal
`None` or BOTH operands are type-ish (a builtin type name, a subscripted generic of one, or a
nested union). So `int | None`, `str | int`, `list[int] | None` are flagged; `Perm.R | Perm.W`,
`0 | 1`, `flags_a | flags_b` are not — which both catches the common landmines AND avoids
false-positiving on real bitwise-or.

Two positions are exempt because they are never evaluated on 3.9:
  - an annotation in a file with `from __future__ import annotations` (PEP 563 → lazy string).
    NOTE: that future-import only lazifies ANNOTATIONS, not value expressions — `X = int | None`
    still crashes, so value-position unions are flagged regardless.
  - a function-LOCAL variable annotation (`def f(): x: int | None` — never evaluated in any Python).
  - anything inside an `if TYPE_CHECKING:` block (dead at runtime).

Known limitations (documented, not bugs): a union of two *custom* classes with no `None`
(`Foo | Bar`) is not recognized as type-shaped (indistinguishable from IntFlag `A | B` statically);
`sys.version_info` guards are not special-cased.

Wired into the pytest suite as tests/test_py39_compat.py (the repo's CI gate). Also a CLI:

    python3 bin/scan_py39_compat.py            # scan the project; exit 1 if any RUNTIME violation
    python3 bin/scan_py39_compat.py --all      # exit 1 on ANY violation (incl. deprecated/, tests/)
    python3 bin/scan_py39_compat.py path/to/dir

Categories: `runtime` (active cron/import path — the gate), `test` (under a tests/ directory; runs
under framework 3.14 via pytest, not a cron risk), `deprecated` (bin/deprecated/, guarded to refuse
execution). Only `runtime` fails the default gate.

Kept 3.9-clean itself (no PEP-604, no match) so it runs under the very 3.9 it protects against.
"""
import argparse
import ast
import os
import sys
from typing import List, Optional, Tuple

SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".pytest_cache", ".mypy_cache", ".venv"}

Finding = Tuple[int, str]   # (lineno, message)

# ast.Match (3.10) / ast.TypeAlias (3.12) don't exist on 3.9. getattr(...,()) → isinstance(x,())
# is always False, keeping this scanner runnable on the 3.9 it guards (a `match`/`type` statement is
# a SyntaxError there, caught by scan_source's parse-error path instead).
_MATCH_NODE = getattr(ast, "Match", ())
_TYPEALIAS_NODE = getattr(ast, "TypeAlias", ())

_BUILTIN_TYPE_NAMES = {
    "int", "str", "float", "bool", "bytes", "bytearray", "complex", "list", "dict", "set",
    "frozenset", "tuple", "object", "type", "memoryview", "range", "slice",
}


def _has_future_annotations(tree):
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "__future__":
            if any(a.name == "annotations" for a in n.names):
                return True
    return False


def _is_none(n):
    return isinstance(n, ast.Constant) and n.value is None


def _is_type_ish(n):
    """A node that reads as a TYPE in a union: None, a builtin type name, a subscripted generic of
    a type (list[int]), or a nested type-union. Deliberately excludes bare custom/uppercase names
    and Attributes (Enum.MEMBER) so IntFlag `A | B` is not mistaken for a type union."""
    if _is_none(n):
        return True
    if isinstance(n, ast.Name) and n.id in _BUILTIN_TYPE_NAMES:
        return True
    if isinstance(n, ast.Subscript):
        return _is_type_ish(n.value)
    if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr):
        return _is_union(n)
    return False


def _is_union(binop):
    """True if a `|` BinOp is a PEP-604 TYPE union (vs integer/set/flag bitwise-or): one operand is
    `None`, or both operands are type-ish."""
    left, right = binop.left, binop.right
    if _is_none(left) or _is_none(right):
        return True
    return _is_type_ish(left) and _is_type_ish(right)


def _top_unions(node):
    """Outermost type-union BinOps within `node` (does not descend into a union's own operands, so
    `str | int | None` reports once)."""
    out = []

    def rec(n):
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr) and _is_union(n):
            out.append(n)
            return
        for c in ast.iter_child_nodes(n):
            rec(c)

    rec(node)
    return out


def _all_args(a):
    return (list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
            + ([a.vararg] if a.vararg else []) + ([a.kwarg] if a.kwarg else []))


def _is_type_checking_guard(test):
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    return False


def scan_source(src, filename="<string>"):
    # type: (str, str) -> List[Finding]
    """Return sorted [(lineno, message)] for every 3.9-breaking construct in `src`."""
    try:
        tree = ast.parse(src, filename=filename)
    except SyntaxError as e:
        return [(e.lineno or 0, "parse error: %s" % e.msg)]

    has_future = _has_future_annotations(tree)
    findings = []  # type: List[Finding]

    # ── syntax-level 3.10+/3.12+ ──
    for n in ast.walk(tree):
        if _MATCH_NODE and isinstance(n, _MATCH_NODE):
            findings.append((n.lineno, "match-statement (3.10+ syntax -> SyntaxError on 3.9)"))
        if _TYPEALIAS_NODE and isinstance(n, _TYPEALIAS_NODE):
            findings.append((n.lineno, "PEP-695 'type' alias statement (3.12+ syntax -> SyntaxError on 3.9)"))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if getattr(n, "type_params", None):
                findings.append((n.lineno, "PEP-695 type parameters (3.12+ syntax -> SyntaxError on 3.9)"))

    # ── PEP-604 unions in evaluated positions ──
    all_unions = _top_unions(tree)
    exempt = set()          # id(binop) that are provably NOT evaluated on 3.9
    label = {}              # id(binop) -> human position label

    class _Ctx(ast.NodeVisitor):
        def __init__(self):
            self.scope = ["module"]

        def visit_If(self, node):
            if _is_type_checking_guard(node.test):
                for stmt in node.body:                 # dead at runtime -> exempt everything inside
                    for bo in _top_unions(stmt):
                        exempt.add(id(bo))
                for stmt in node.orelse:               # the else branch DOES run on 3.9
                    self.visit(stmt)
                return
            self.generic_visit(node)

        def _mark_ann(self, ann, lbl, local):
            if ann is None:
                return
            for bo in _top_unions(ann):
                label.setdefault(id(bo), lbl)
                if has_future or local:                # stringified, or never-evaluated local
                    exempt.add(id(bo))

        def _label_value(self, expr, lbl):
            if expr is None:
                return
            for bo in _top_unions(expr):
                label.setdefault(id(bo), lbl)

        def _func(self, node):
            for arg in _all_args(node.args):
                if arg and arg.annotation:
                    self._mark_ann(arg.annotation, "arg annotation", local=False)
            self._mark_ann(node.returns, "return annotation", local=False)
            a = node.args
            for d in list(a.defaults) + [k for k in a.kw_defaults if k is not None]:
                self._label_value(d, "default value")
            for dec in node.decorator_list:
                self._label_value(dec, "decorator")
            self.scope.append("func")
            for stmt in node.body:
                self.visit(stmt)
            self.scope.pop()

        visit_FunctionDef = _func
        visit_AsyncFunctionDef = _func

        def visit_ClassDef(self, node):
            for base in node.bases:
                self._label_value(base, "class base")
            for kw in node.keywords:
                self._label_value(kw.value, "class keyword")
            for dec in node.decorator_list:
                self._label_value(dec, "decorator")
            self.scope.append("class")
            for stmt in node.body:
                self.visit(stmt)
            self.scope.pop()

        def visit_AnnAssign(self, node):
            self._mark_ann(node.annotation, "variable annotation", local=(self.scope[-1] == "func"))
            self._label_value(node.value, "assigned value")
            self.generic_visit(node)

    _Ctx().visit(tree)

    for bo in all_unions:
        if id(bo) in exempt:
            continue
        seg = ast.get_source_segment(src, bo) or "<union>"
        findings.append((bo.lineno, "PEP-604 union in %s: %s" % (label.get(id(bo), "evaluated expression"), seg)))

    return sorted(set(findings))


def classify(rel_path):
    # type: (str) -> str
    """`deprecated` | `test` | `runtime` — only `runtime` is on a cron/import path. `test` requires
    a `tests/` DIRECTORY segment (not a bare test_*.py basename — a bin/test_*.py helper that
    runtime code imports must NOT be exempted)."""
    parts = rel_path.replace("\\", "/").split("/")
    if "deprecated" in parts:
        return "deprecated"
    if "tests" in parts:
        return "test"
    return "runtime"


def find_violations(root="."):
    # type: (str) -> List[dict]
    """Walk `root`, return [{path, category, findings:[(lineno,msg)]}] for every offending file,
    sorted by path (relative to `root`)."""
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, root)
            try:
                src = open(full, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError) as e:
                out.append({"path": rel, "category": classify(rel),
                            "findings": [(0, "unreadable: %s" % e)]})
                continue
            findings = scan_source(src, filename=rel)
            if findings:
                out.append({"path": rel, "category": classify(rel), "findings": findings})
    return sorted(out, key=lambda v: v["path"])


def format_report(violations, only=None):
    # type: (List[dict], Optional[str]) -> str
    lines = []
    for v in violations:
        if only is not None and v["category"] != only:
            continue
        lines.append("%s  [%s]" % (v["path"], v["category"]))
        for ln, msg in v["findings"]:
            lines.append("  L%d: %s" % (ln, msg))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Guard against Python-3.9-incompatible constructs.")
    default_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("root", nargs="?", default=default_root,
                    help="directory to scan (default: the kalshi-weather project root)")
    ap.add_argument("--all", action="store_true",
                    help="fail on ANY category, not just cron/import-path (`runtime`) files")
    args = ap.parse_args(argv)

    violations = find_violations(args.root)
    by_cat = {}
    for v in violations:
        by_cat.setdefault(v["category"], []).append(v)

    runtime = by_cat.get("runtime", [])
    print("Scanned %s" % os.path.abspath(args.root))
    print("  runtime/cron-path violations: %d" % len(runtime))
    print("  test violations (3.14-only, informational): %d" % len(by_cat.get("test", [])))
    print("  deprecated violations (off every cron path): %d" % len(by_cat.get("deprecated", [])))

    gate = violations if args.all else runtime
    if gate:
        print("\n" + format_report(violations, only=(None if args.all else "runtime")))
        print("\nFAIL: %d file(s) use constructs that crash under cron's python3.9." % len(gate))
        return 1
    print("\nOK: no 3.9-breaking constructs on the %s path." % ("full" if args.all else "cron/import"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
