"""pytest plugin: for every `is` / `is not` / `==` / `!=` comparison in sqlglot, record whether the
identity form and the equality form ever disagree on the values the test suite actually feeds it.

A patch that only swaps `is` <-> `==` (or `is not` <-> `!=`) at a site where they never disagreed
for any test cannot change any test's outcome. Each sqlglot module is compiled from an AST in which
single-operator comparisons of those kinds become calls to a recorder that returns the original
result unchanged. Output: JSON {"file:line:col:op": [executions, disagreements]} to $CMP_OUT.<pid>.
"""

import ast
import builtins
import importlib.abc
import importlib.machinery
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "selector"))
from runner import install_inline_executor  # noqa: E402

install_inline_executor()

ROOT = os.environ.get("TRACE_ROOT", os.getcwd()).rstrip("/") + "/"
OUT = os.environ.get("CMP_OUT")
_stats: dict[str, list[int]] = {}
_SITES: list[str] = []


def _rec(site, a, b, op):
    if op == 0:
        orig = a is b
    elif op == 1:
        orig = a is not b
    elif op == 2:
        orig = a == b
    else:
        orig = a != b
    try:
        if op == 0:
            alt = bool(a == b)
            agree = alt == orig
        elif op == 1:
            alt = bool(a != b)
            agree = alt == orig
        elif op == 2:
            agree = (type(orig) is bool) and orig == (a is b)
        else:
            agree = (type(orig) is bool) and orig == (a is not b)
    except Exception:  # noqa: BLE001
        agree = False
    st = _stats.get(_SITES[site])
    if st is None:
        st = _stats[_SITES[site]] = [0, 0]
    st[0] += 1
    if not agree:
        st[1] += 1
    return orig


builtins._xero_cmp_ = _rec

_OPS = {ast.Is: 0, ast.IsNot: 1, ast.Eq: 2, ast.NotEq: 3}
_NAMES = {0: "is", 1: "isnot", 2: "eq", 3: "noteq"}


class _Rewriter(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in _OPS:
            return node
        op = _OPS[type(node.ops[0])]
        key = f"{self.rel}:{node.lineno}:{node.col_offset}:{_NAMES[op]}"
        _SITES.append(key)
        call = ast.Call(
            func=ast.Name(id="_xero_cmp_", ctx=ast.Load()),
            args=[ast.Constant(len(_SITES) - 1), node.left, node.comparators[0], ast.Constant(op)],
            keywords=[],
        )
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    def source_to_code(self, data, path, *, _optimize=-1):
        tree = ast.parse(data, filename=path)
        rel = path[len(ROOT):] if path.startswith(ROOT) else path
        tree = _Rewriter(rel).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)

    def get_code(self, fullname):  # never use cached bytecode
        source = self.get_data(self.get_filename(fullname))
        return self.source_to_code(source, self.get_filename(fullname))


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not (fullname == "sqlglot" or fullname.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            return spec
        spec.loader = _Loader(spec.loader.name, spec.loader.path)
        return spec


sys.meta_path.insert(0, _Finder())

# Optional per-case run (same convention as trace_plugin): CMP_CASE=BASE or "<fixture>|<k>"
_CASE = os.environ.get("CMP_CASE")


def _wrap_loader(orig):
    def loader(filename, *args, **kwargs):
        if _CASE == "BASE":
            return
        f, k = _CASE.rsplit("|", 1)
        if filename != f:
            return
        for i, item in enumerate(orig(filename, *args, **kwargs)):
            if i == int(k):
                yield item

    return loader


if _CASE:
    import tests.helpers as _helpers  # imported before the test modules bind the loaders

    _helpers.load_sql_fixture_pairs = _wrap_loader(_helpers.load_sql_fixture_pairs)


def pytest_sessionfinish(session, exitstatus):
    if OUT:
        with open(f"{OUT}.{os.getpid()}", "w") as fh:
            json.dump(_stats, fh)
