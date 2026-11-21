"""Blast Radius selector (Team-Xero).

For every patch in /data/commits:
  1. Parse and apply it in memory against the pristine sqlglot checkout (SQLGLOT_SRC).
  2. Find candidate tests from coverage maps built at the pinned commit:
       - change inside a function/lambda body  -> tests that executed that function
       - change at module/class level           -> tests that ran or imported that module
       - non-Python file (fixtures, ...)        -> tests that opened that file
     Changes that only touch comments/whitespace of a Python file are ignored.
  3. Cheap candidates are actually run on the patched code (forked child, in memory);
     expensive candidates are listed without running (running costs as much as listing).
  4. Print only failing (or listed) tests, split over the cheapest number of workers.
Anything unexpected falls back to listing every candidate, never to an empty answer.
"""

from __future__ import annotations

import ast
import gzip
import io
import json
import os
import sys
import time
import tokenize

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from patchutil import FilePatch, PatchError, apply_file, changed_old_lines, parse_patch  # noqa: E402

SRC = os.environ.get("SQLGLOT_SRC", "/opt/sqlglot")
DATA = os.environ.get("DATA_DIR", "/data")
MAPS = os.environ.get("BLAST_MAPS", os.path.join(HERE, "..", "maps", "coverage.json.gz"))
HEAVY_S = float(os.environ.get("BLAST_HEAVY_S", "1.0"))  # tests slower than this are listed, not run
PARALLEL = int(os.environ.get("BLAST_PAR", "2"))
CHILD_LIMIT_S = float(os.environ.get("BLAST_CHILD_LIMIT", "60"))
# The scorer allows 3 minutes for the whole batch (4.5 in the final). Stop verifying well before.
DEADLINE = time.monotonic() + float(os.environ.get("BLAST_BUDGET_S", "130"))
WORKER_COST_MS = 2000
MAX_NODES_N = None
PROBE_SWEEP: list[str] = []
MAPS_OBJ = None


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------- maps

class Maps:
    def __init__(self, path: str, runtime_ids: list[str] | None = None):
        with gzip.open(path, "rt") as fh:
            d = json.load(fh)
        self.sets = d["line_sets"]
        self.test_ids = d["tests"]
        # Maps are keyed by the test numbers of the tests.json they were built from. If the
        # scorer's tests.json numbers tests differently, translate by test id; tests the maps
        # do not know are treated as candidates for every code change (see plan_commit).
        self.unknown_tests: set[int] = set()
        if runtime_ids is not None and runtime_ids != self.test_ids:
            pos = {t: i for i, t in enumerate(runtime_ids)}
            remap = [pos.get(t) for t in self.test_ids]
            nmap = len(remap)  # (sets that hold case-unit ids may exceed it; those are dropped below)
            self.sets = [[remap[n] for n in st if n < nmap and remap[n] is not None] for st in self.sets]
            known = {r for r in remap if r is not None}
            self.unknown_tests = set(range(len(runtime_ids))) - known
            self._remap = remap
            self.test_ids = list(runtime_ids)
        else:
            self._remap = None
        self.lines = d["lines"]
        self.mod_run = d["mod_run"]
        self.mod_import = d["mod_import"]
        self.opened = d["opened"]
        self.class_tests = d.get("class_tests", {})
        self.tracked = set(d.get("tracked", []))
        self.import_exec = {f: set(v) for f, v in d.get("import_exec", {}).items()}
        # functions/lambdas that were actually called while some module was being imported
        self.started_in_import = {(f, ln) for f, ln, _q in d.get("started_in_import", [])}
        self.lambda_tests = d.get("lambda_tests", {})
        # import-time execution (lab/import_trace.py): line -> modules whose import ran it, and
        # test file -> modules imported when pytest collects it
        self.imp_mods: list[str] = []
        self.imp_lines: dict = {}
        self.collect: dict[str, set[str]] = {}
        self.imp_names: dict = {}
        self.cattr_reads: dict = {}
        self.sql_at_import: set[str] = set()
        try:
            with gzip.open(os.path.join(os.path.dirname(path), "imports.json.gz"), "rt") as fh:
                di = json.load(fh)
            self.imp_mods, self.imp_lines = di["modules"], di["lines"]
            self.collect = {f: set(v) for f, v in di["collect"].items()}
            self.imp_names = di.get("names", {})
            self.cattr_reads = di.get("cattr_reads", {})
            self.sql_at_import = set(di.get("sql_at_import", []))
        except (OSError, ValueError, KeyError):
            pass
        self.lambdas_traced = bool(d.get("lambdas_traced"))
        self.cmp_ok = set(d.get("cmp_ok", []))
        self.name_uses = d.get("name_uses", {})
        self.name_unsafe = set(d.get("name_unsafe", []))
        # case maps share line_sets but hold case-unit ids, not test numbers: unusable after a remap
        cases = (d.get("cases") or {}) if self._remap is None else {}
        self.case_units = cases.get("units", [])
        self.case_lines = cases.get("lines", {})
        # test number -> {"BASE": uid, (file, k): uid}
        self.case_index: dict[int, dict] = {}
        for uid, u in enumerate(self.case_units):
            num, f, k = u[0], u[1], u[2]
            if self._remap is not None:
                num = self._remap[num] if num < len(self._remap) else None
                if num is None:
                    continue
            missing = len(u) > 3
            ent = self.case_index.setdefault(num, {"files": {}, "missing": False})
            ent["missing"] |= missing
            if f == "BASE":
                ent["BASE"] = uid
            else:
                ent["files"].setdefault(f, {})[k] = uid

    def file_tests(self, f: str) -> list[int]:
        ft = getattr(self, "_file_tests", None)
        if ft is None:
            ft = self._file_tests = {}
            for n, t in enumerate(self.test_ids):
                ft.setdefault(t.split("::")[0], []).append(n)
        return ft.get(f, [])

    def import_modules_at(self, f: str, lines) -> set[str]:
        """Modules whose import executes any of these lines of file f."""
        fl = self.imp_lines.get(f)
        if not fl:
            return set()
        out = set()
        for ln in lines:
            for i in fl.get(str(ln), ()):
                out.add(self.imp_mods[i])
        return out

    def import_modules_using(self, names) -> set[str]:
        """Modules whose import executes a statement that mentions one of these names."""
        out = set()
        for n in names:
            for i in self.imp_names.get(n, ()):
                out.add(self.imp_mods[i])
        return out

    def collect_importers(self, mod: str) -> set[str]:
        """Test modules whose collection imports `mod`."""
        return {_modname(f) for f, ms in self.collect.items() if mod in ms}

    def broken_tests(self, mods) -> set[int]:
        """Tests that cannot pass when these modules fail to import: tests in every file whose
        collection imports one of them (pytest reports the whole file as an error), plus tests
        that ran or imported them at run time."""
        mods = set(mods)
        out: set[int] = set()
        for m in mods:
            rel = m.replace(".", "/")
            out |= self.module_tests(rel + ".py") | self.module_tests(rel + "/__init__.py")
        for f, ms in self.collect.items():
            if ms & mods:
                out.update(self.file_tests(f))
        return out

    def lambda_callers(self, f: str, first_line: int) -> set[int]:
        return set(self._s(self.lambda_tests.get(f"{f}:{first_line}")))

    def case_units_in_range(self, f: str, lo: int, hi: int) -> set[int]:
        out: set[int] = set()
        fl = self.case_lines.get(f)
        if not fl:
            return out
        for ln in range(lo, hi + 1):
            idx = fl.get(str(ln))
            if idx is not None:
                out.update(self.sets[idx])
        return out

    def instance_tests(self, qual: str) -> set[int]:
        return set(self._s(self.class_tests.get(qual)))

    def _s(self, idx):
        return self.sets[idx] if idx is not None else ()

    def line_tests(self, f: str, line: int):
        fl = self.lines.get(f)
        if not fl:
            return ()
        return self._s(fl.get(str(line)))

    def lines_in_range(self, f: str, lo: int, hi: int) -> set[int]:
        out: set[int] = set()
        fl = self.lines.get(f)
        if not fl:
            return out
        for ln in range(lo, hi + 1):
            idx = fl.get(str(ln))
            if idx is not None:
                out.update(self.sets[idx])
        return out

    def module_tests(self, f: str) -> set[int]:
        out = set(self._s(self.mod_run.get(f)))
        out.update(self._s(self.mod_import.get(f)))
        return out

    def opened_tests(self, f: str) -> set[int]:
        return set(self._s(self.opened.get(f)))

    def opened_in_dir(self, d: str) -> set[int]:
        out: set[int] = set()
        prefix = d.rstrip("/") + "/"
        for f, idx in self.opened.items():
            if f.startswith(prefix):
                out.update(self.sets[idx])
        return out


def _docstrings_only(old_text: str, new_text: str) -> bool:
    """True if the two sources differ only in docstrings (and comments/layout)."""
    def strip(text):
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                b = node.body
                if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant)                         and isinstance(b[0].value.value, str):
                    node.body = b[1:] or [ast.Pass()]
        return ast.dump(tree)
    try:
        return strip(old_text) == strip(new_text)
    except (SyntaxError, ValueError, RecursionError):
        return False


def _bindings(tree) -> set[str]:
    """Names bound at module level or in class bodies (imports, defs, classes, assignments)."""
    out: set[str] = set()

    def targets(t):
        if isinstance(t, ast.Name):
            out.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                targets(e)

    def walk(body):
        for node in body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    out.add(a.asname or a.name.split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.add(node.name)
            elif isinstance(node, ast.ClassDef):
                out.add(node.name)
                walk(node.body)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    targets(t)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets(node.target)
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for attr in ("body", "orelse", "finalbody"):
                    walk(getattr(node, attr, []) or [])
                for hnd in getattr(node, "handlers", []) or []:
                    walk(hnd.body)

    walk(tree.body)
    return out


def _class_attr_changes(old_tree, new_tree, safe) -> dict[str, set[str]]:
    """class qualname -> attribute names, for classes whose body (methods aside) differs between
    the two trees only in the values of `NAME = ...` assignments to attributes in `safe` (data
    attributes read only at run time). Classes with any other kind of body change are absent."""
    if old_tree is None or new_tree is None:
        return {}

    def classes(tree):
        out = {}

        def walk(body, chain):
            for node in body:
                if isinstance(node, ast.ClassDef):
                    q = ".".join(chain + [node.name])
                    out[q] = node
                    walk(node.body, chain + [node.name])
        walk(tree.body, [])
        return out

    def target(st):
        tg = st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, ast.AnnAssign) else []
        return tg[0].id if len(tg) == 1 and isinstance(tg[0], ast.Name) else None

    def header(c):
        return ast.dump(ast.ClassDef(name=c.name, bases=c.bases, keywords=c.keywords, body=[],
                                     decorator_list=c.decorator_list, type_params=[]))

    old_c, new_c = classes(old_tree), classes(new_tree)
    out: dict[str, set[str]] = {}
    for q, oc in old_c.items():
        nc = new_c.get(q)
        if nc is None or header(oc) != header(nc):
            continue
        ob = [st for st in oc.body if not isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        nb = [st for st in nc.body if not isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        if len(ob) != len(nb):
            continue
        names: set[str] = set()
        ok = True
        for a, b in zip(ob, nb):
            if ast.dump(a) == ast.dump(b):
                continue
            ta, tb = target(a), target(b)
            if ta is None or ta != tb or ta not in safe or type(a) is not type(b):
                ok = False
                break
            names.add(ta)
        if ok and names:
            out[q] = names
    return out


def _modname(rel: str) -> str:
    m = rel[:-3].replace("/", ".")
    return m[: -len(".__init__")] if m.endswith(".__init__") else m


# ----------------------------------------------------------------------------- python analysis

def _tokens(text: str):
    """Significant token stream (comments and non-logical newlines dropped)."""
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING):
                continue
            out.append((tok.type, tok.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return out


def _coding_cookie(text: str):
    head = text.split("\n", 2)[:2]
    import re
    for l in head:
        m = re.match(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)", l)
        if m:
            return m.group(1)
    return None


def python_semantically_equal(old: str, new: str) -> bool:
    if _coding_cookie(old) != _coding_cookie(new):
        return False
    a, b = _tokens(old), _tokens(new)
    return a is not None and b is not None and a == b


class Regions:
    """Function/lambda body regions of a Python source, to tell runtime lines from import-time lines."""

    def __init__(self, text: str):
        self.funcs: list[tuple[int, int, int]] = []  # (body_start, body_end, def_line)
        # class bodies: (header_start, body_start, body_end, qualname, body statements)
        self.classes: list[tuple[int, int, int, str, list]] = []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            self.ok = False
            self.tree = None
            return
        self.ok = True
        self.tree = tree
        # statement spans: simple statements = whole span; compound statements = header only
        self.stmts: list[tuple[int, int]] = []
        # code-region details keyed by (body_start, body_end, first): (kind, node)
        self.info: dict[tuple[int, int, int], tuple[str, ast.AST]] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.body:
                    start = node.body[0].lineno
                    # third field = the code object's co_firstlineno (first decorator line, if any)
                    first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    key = (start, node.end_lineno or start, first)
                    self.funcs.append(key)
                    self.info[key] = ("def", node)
            elif isinstance(node, ast.Lambda):
                key = (node.body.lineno, node.body.end_lineno or node.body.lineno, node.lineno)
                self.funcs.append(key)
                self.info[key] = ("lambda", node)
            if isinstance(node, ast.stmt):
                body = getattr(node, "body", None)
                if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
                    hdr_end = max(node.lineno, body[0].lineno - 1)
                    if body[0].lineno == node.lineno:  # one-line compound: `if x: y`
                        hdr_end = node.end_lineno or node.lineno
                    self.stmts.append((node.lineno, hdr_end))
                else:
                    self.stmts.append((node.lineno, node.end_lineno or node.lineno))
        self._walk_classes(tree.body, [], in_func=False)

    def outside_segments(self, fn, line: int, text_line: str):
        """Text of `line` that lies outside the body of code region `fn` (import-time context).

        Returns None when the whole line is inside the body. For lambdas the body is everything
        between `lambda` and the end of the expression; for defs, the statements after the colon.
        """
        kind, node = self.info.get(fn, (None, None))
        if node is None:
            return None
        if kind == "lambda":
            s_line, s_col = node.lineno, node.col_offset
            e_line, e_col = node.end_lineno, node.end_col_offset
        else:
            s_line, s_col = node.body[0].lineno, node.body[0].col_offset
            e_line, e_col = node.end_lineno, node.end_col_offset
        b = text_line.encode("utf-8")
        before = b[:s_col].decode("utf-8", "replace") if line == s_line else ""
        after = b[e_col:].decode("utf-8", "replace") if line == e_line else ""
        if not before and not after:
            return None
        return before, after

    def stmt_span(self, line: int):
        """Smallest statement span containing `line` (None if the line is not inside one)."""
        best = None
        for s, e in self.stmts:
            if s <= line <= e and (best is None or (e - s) < (best[1] - best[0])):
                best = (s, e)
        return best

    def _walk_classes(self, body, chain, in_func):
        for node in body:
            if isinstance(node, ast.ClassDef):
                header = min([node.lineno] + [d.lineno for d in node.decorator_list])
                q = chain + [node.name]
                if not in_func and node.body:
                    self.classes.append((header, node.body[0].lineno, node.end_lineno or node.lineno, ".".join(q), node.body))
                self._walk_classes(node.body, q, in_func)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._walk_classes(node.body, chain + [node.name, "<locals>"], True)
            else:
                for field in ("body", "orelse", "finalbody", "handlers"):
                    sub = getattr(node, field, None)
                    if isinstance(sub, list):
                        self._walk_classes([s for s in sub if isinstance(s, ast.AST)], chain, in_func)

    def innermost(self, line: int):
        best = None
        for s, e, d in self.funcs:
            if s <= line <= e and (best is None or (e - s) < (best[1] - best[0])):
                best = (s, e, d)
        return best

    def class_at(self, line: int):
        """(qualname, is_header, statement) of the innermost class whose header/body holds `line`."""
        best = None
        for h, s, e, q, body in self.classes:
            if h <= line <= e and (best is None or (e - h) < (best[2] - best[0])):
                best = (h, s, e, q, body)
        if best is None:
            return None
        h, s, e, q, body = best
        if line < s:
            return q, True, None
        stmt = None
        for st in body:
            if st.lineno <= line <= (st.end_lineno or st.lineno):
                stmt = st
                break
        return q, False, stmt


# ----------------------------------------------------------------------------- planning

class Plan:
    def __init__(self, commit: str):
        self.commit = commit
        self.patched: dict[str, str | None] = {}  # abs path -> text
        self.candidates: set[int] = set()
        self.notes: list[str] = []
        self.fallback = False  # if True: list all candidates without verifying
        # why tests are candidates (drives partial runs of case-driven tests)
        self.cand_spans: set[int] = set()  # executed a changed statement inside a function
        self.cand_other: set[int] = set()  # module/class-level, structural, other files
        self.cand_fixture: dict[int, set[str]] = {}  # test -> changed fixture files it reads
        self.spans: list[tuple[str, int, int]] = []
        self.fixture_changes: list[tuple[str, str | None, str | None]] = []
        self.hotswap = True  # every Python change is a runtime-only body change
        self.hotswap_files: set[str] = set()
        self.partial: dict[int, dict[str, set[int]]] = {}  # test -> fixture file -> case indices
        self.cost_override: dict[int, float] = {}
        # modules to re-import under the patch before running anything: if one now fails to
        # import, every test whose file imports it at collection fails (see Maps.broken_tests)
        self.probe: set[str] = set()


def read_src(rel: str) -> str | None:
    p = os.path.join(SRC, rel)
    try:
        with open(p, encoding="utf-8", newline="") as fh:
            return fh.read()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        return None


def plan_commit(commit: str, text: str, maps: Maps, tests: list[dict]) -> Plan:
    plan = Plan(commit)
    try:
        fps = parse_patch(text)
    except Exception as e:  # pragma: no cover
        plan.notes.append(f"parse error {e}")
        plan.fallback = True
        plan.candidates = set(range(len(tests)))
        return plan
    if not fps:
        plan.notes.append("empty patch")
        return plan

    for fp in fps:
        rels = [p for p in (fp.old_path, fp.new_path) if p]
        if fp.binary:
            plan.notes.append(f"binary {fp.path}")
            for r in rels:
                plan.candidates |= maps.opened_tests(r) | maps.module_tests(r)
            plan.fallback = True
            continue
        old_text = read_src(fp.old_path) if fp.old_path else None
        try:
            new_text = apply_file(old_text, fp)
        except PatchError as e:
            plan.notes.append(str(e))
            plan.fallback = True
            for r in rels:
                plan.candidates |= maps.opened_tests(r) | maps.module_tests(r)
            continue
        if fp.old_path and fp.new_path and fp.old_path != fp.new_path:
            # rename: old path disappears, new path appears
            plan.patched[os.path.join(SRC, fp.old_path)] = None
            plan.patched[os.path.join(SRC, fp.new_path)] = new_text
        else:
            plan.patched[os.path.join(SRC, fp.path)] = new_text

        for rel in dict.fromkeys(rels):
            _candidates_for_file(plan, maps, rel, fp, old_text, new_text)
    if plan.fallback:
        plan.hotswap = False
    if maps.unknown_tests and plan.patched:
        plan.candidates |= maps.unknown_tests
        plan.cand_other |= maps.unknown_tests
    return plan


def _fixture_pairs(text: str | None):
    """Mirror of tests/helpers.py load_sql_fixture_pairs (raw strings per case)."""
    if text is None:
        return None
    s = "\n".join(line for line in text.splitlines() if line and not line.startswith("--"))
    st = s.split(";")
    return [(st[i].strip(), st[i + 1].strip()) for i in range(0, len(st), 2) if i + 1 < len(st)]


def _changed_cases(old: str | None, new: str | None):
    a, b = _fixture_pairs(old), _fixture_pairs(new)
    if a is None or b is None or len(a) != len(b):
        return None
    return {i for i, (x, y) in enumerate(zip(a, b)) if x != y}


def plan_partials(plan: Plan, maps: Maps, cpu_cost: list[float]) -> None:
    """For slow case-driven tests, run only the fixture cases a change can reach.

    Valid because a test fails iff one of its cases (or its case-independent part) fails, and a
    deterministic case that never executes a changed statement behaves exactly as before.
    """
    fixture_text = {rel: (old, new) for rel, old, new in plan.fixture_changes}
    span_hits = None
    for n in sorted(plan.candidates):
        ent = maps.case_index.get(n)
        if not ent or ent["missing"] or n in plan.cand_other:
            continue
        sel: dict[str, set[int]] = {}
        full = False
        if n in plan.cand_spans:
            if span_hits is None:
                span_hits = set()
                for rel, lo, hi in plan.spans:
                    span_hits |= maps.case_units_in_range(rel, lo, hi)
            if ent.get("BASE") in span_hits:
                full = True
            else:
                found = False
                for f, ks in ent["files"].items():
                    for k, uid in ks.items():
                        if uid in span_hits:
                            sel.setdefault(f, set()).add(k)
                            found = True
                full = not found  # full trace says it runs the change but no case does: stay safe
        if not full and n in plan.cand_fixture:
            for rel in plan.cand_fixture[n]:
                f = rel[len("tests/fixtures/"):]
                changed = _changed_cases(*fixture_text.get(rel, (None, None)))
                if f not in ent["files"] or not changed:
                    full = True
                    break
                sel.setdefault(f, set()).update(changed)
        if full or not sel:
            continue
        plan.partial[n] = sel
        n_cases = sum(len(v) for v in ent["files"].values())
        k = sum(len(v) for v in sel.values())
        base = _BASE_COST.get(maps.test_ids[n], max(0.3, 0.15 * cpu_cost[n]))
        plan.cost_override[n] = base + max(0.0, cpu_cost[n] - base) * k / max(1, n_cases)
    if plan.partial:
        plan.notes.append("partial " + ", ".join(
            f"{n}:{sum(len(v) for v in s.values())}" for n, s in sorted(plan.partial.items())))


_PYTEST_CONFIG = {"pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", ".pytest.ini"}
# package directories that some test walks with pkgutil (so a new module there gets imported)
_DISCOVERY = {"sqlglot/typing/": "tests/test_typing.py::", "sqlglot/": "tests/test_docs.py::"}


def _pytest_config_change(rel: str, old_text, new_text) -> str | None:
    """Directory whose tests a pytest configuration change can affect ('' = all), else None.

    Our runner does not go through pytest, so these changes cannot be verified in memory.
    """
    base = os.path.basename(rel)
    d = os.path.dirname(rel)
    if base == "conftest.py":
        return d
    if base in _PYTEST_CONFIG and d == "":
        if base in ("pytest.ini", ".pytest.ini", "tox.ini"):
            return ""
        txt = (old_text or "") + (new_text or "")
        if "pytest" in txt:
            return ""
    return None


def _candidates_for_file(plan: Plan, maps: Maps, rel: str, fp: FilePatch, old_text, new_text):
    is_py = rel.endswith(".py")
    in_pkg = rel.startswith("sqlglot/") or rel.startswith("tests/")
    scope = _pytest_config_change(rel, old_text, new_text)
    if scope is not None:
        hit = {n for n, t in enumerate(maps.test_ids) if t.startswith(scope + "/" if scope else "")}
        plan.candidates |= hit
        plan.cand_other |= hit
        plan.fallback = True
        plan.hotswap = False
        plan.notes.append(f"pytest config {rel}: listing {len(hit)}")
        return
    if not is_py or not in_pkg:
        hit = maps.opened_tests(rel)
        structural = fp.is_new or fp.is_deleted or fp.rename
        # (a brand-new non-Python file is only read if some test names it or lists its directory;
        #  no test at the pinned commit lists a fixture directory, so `opened_tests` is complete)
        if is_py and not in_pkg:
            hit |= maps.module_tests(rel)
            plan.hotswap = False
        if not structural and not is_py and rel.startswith("tests/fixtures/"):
            plan.fixture_changes.append((rel, old_text, new_text))
            for n in hit:
                plan.cand_fixture.setdefault(n, set()).add(rel)
        else:
            plan.cand_other |= hit
        plan.candidates |= hit
        plan.notes.append(f"file {rel}: {len(hit)} cand")
        return
    plan.hotswap_files.add(os.path.join(SRC, rel))

    # python module inside sqlglot/ or tests/
    if fp.is_new and old_text is None:
        # A brand-new module only runs if something imports it: a changed file (analysed on its
        # own as a module-level change) or a test that discovers modules by listing a package
        # directory (pkgutil). At the pinned commit only tests/test_typing.py does that, for
        # sqlglot/typing/. Being importable by name, it also shadows nothing that exists.
        hit = set()
        for d, prefix in _DISCOVERY.items():
            if rel.startswith(d):
                hit |= {n for n, t in enumerate(maps.test_ids) if t.startswith(prefix)}
        plan.candidates |= hit
        plan.cand_other |= hit
        plan.hotswap = False
        plan.notes.append(f"new module {rel}: {len(hit)} cand")
        return
    moved_away = bool(fp.old_path and fp.new_path and fp.old_path != fp.new_path and rel == fp.old_path)
    if fp.is_deleted or new_text is None or moved_away:
        # the module disappears: its users fail unless the patch also updates them; test files
        # that import it at collection are re-imported under the patch to find out
        hit = maps.module_tests(rel)
        plan.candidates |= hit
        plan.cand_other |= hit
        plan.hotswap = False
        plan.probe |= maps.collect_importers(_modname(rel))
        plan.notes.append(f"{'moved' if moved_away else 'deleted'} module {rel}: {len(hit)} cand")
        return
    if rel != fp.old_path:
        plan.hotswap = False
        return  # rename target handled via old path
    if python_semantically_equal(old_text, new_text):
        plan.notes.append(f"{rel}: comments/whitespace only")
        plan.hotswap_files.discard(os.path.join(SRC, rel))
        plan.patched.pop(os.path.join(SRC, rel), None)  # nothing to run differently
        return
    if _docstrings_only(old_text, new_text):
        # tests.json holds no doctests (tests/test_docs.py builds them in load_tests), and nothing
        # else reads __doc__ beyond "is it None"; so a docstring edit cannot change any listed test
        plan.notes.append(f"{rel}: docstrings only")
        plan.hotswap_files.discard(os.path.join(SRC, rel))
        plan.patched.pop(os.path.join(SRC, rel), None)
        return
    if maps.cmp_ok and _only_equivalent_swaps(fp, old_text, new_text, rel, maps.cmp_ok):
        plan.notes.append(f"{rel}: is/== swaps at sites where both forms always agreed")
        plan.hotswap_files.discard(os.path.join(SRC, rel))
        plan.patched.pop(os.path.join(SRC, rel), None)
        return
    regions = Regions(old_text)
    new_ok = True
    try:
        new_tree = ast.parse(new_text)
    except SyntaxError:
        new_ok = False
    touched, neighbors = changed_old_lines(fp)
    if not regions.ok or not new_ok:
        hit = maps.module_tests(rel) | (maps.broken_tests({_modname(rel)}) if not new_ok else set())
        plan.candidates |= hit
        plan.cand_other |= hit
        plan.hotswap = False
        plan.notes.append(f"{rel}: syntax error -> module users {len(hit)}")
        return
    hit: set[int] = set()
    span_hit: set[int] = set()
    import_lines = maps.import_exec.get(rel, set())
    module_level = False
    class_quals: set[str] = set()
    modname = rel[:-3].replace("/", ".")
    if modname.endswith(".__init__"):
        modname = modname[: -len(".__init__")]
    added = "\n".join(l[1:] for h in fp.hunks for l in h.lines if l[:1] == "+")
    removed = "\n".join(l[1:] for h in fp.hunks for l in h.lines if l[:1] == "-")
    global_attr = bool(_GLOBAL_ATTR.search(added) or _GLOBAL_ATTR.search(removed))
    forced_import = _import_time_lines(fp, old_text, new_text, regions, touched, neighbors)
    attr_changes = _class_attr_changes(regions.tree, new_tree, maps.cattr_reads) if maps.cattr_reads else {}
    # changed code that runs while some module is imported (module/class level, or a function a
    # class body calls, e.g. a parser method behind a pre-parsed constant): re-import those
    # modules under the patch. Insertion points count when inside a body or at module level;
    # a def header next to an insertion at the top of a body does not.
    probe_lines = touched | (neighbors & forced_import) | \
        {ln for ln in neighbors if regions.innermost(ln) is not None}
    # whole statements: a changed continuation line may only have been skipped at import time
    # (`if a and b` with a falsy `a`) while the statement itself ran
    for ln in list(probe_lines):
        sp = regions.stmt_span(ln)
        if sp is not None:
            probe_lines.update(range(sp[0], sp[1] + 1))
    probe = maps.import_modules_at(rel, probe_lines)
    # a module/class-level name that no longer exists (removed import, renamed def, deleted
    # constant or class attribute) breaks every module whose import-time code mentions it
    gone = _bindings(regions.tree) - _bindings(new_tree) if regions.ok else set()
    if gone:
        probe |= maps.import_modules_using(gone)
        plan.notes.append(f"{rel}: unbound {sorted(gone)[:3]}")
    if probe:
        # data consumed by the SQL tokenizer/parser (expression arg_types, tokenizer slots,
        # keyword tables) changes what the modules that parse SQL at import time do
        plan.probe |= probe | maps.sql_at_import
        plan.hotswap = False
    for ln in sorted(touched | neighbors):
        fn = None if ln in forced_import else regions.innermost(ln)
        if fn is not None and regions.info.get(fn, ("",))[0] == "lambda" and maps.lambdas_traced:
            if (rel, fn[2]) in maps.started_in_import:
                fn = None  # the lambda itself ran at import time: its results are import-time state
            else:
                # tests that actually CALLED this lambda (its first line also runs when the
                # enclosing table is built, so line coverage would over-select)
                span_hit |= maps.lambda_callers(rel, fn[2])
                plan.spans.append((rel, fn[0], fn[1]))
                continue
        if fn is not None:
            fs, fe = fn[0], fn[1]
            # A deterministic test can only behave differently if it executes the changed
            # statement (or reaches the insertion point). Use the statement's full span so
            # continuation lines without their own line events are still covered; fall back
            # to the whole function when the line is not inside a statement within it.
            span = regions.stmt_span(ln)
            if span is None or span[0] < fs or span[1] > fe:
                span = (fs, fe)
            span_hit |= maps.lines_in_range(rel, span[0], span[1])
            plan.spans.append((rel, span[0], span[1]))
            # a changed statement that also ran while modules were being imported built
            # import-time state; swapping the function body in a pre-imported process would
            # not redo that, so such commits re-import instead
            if (rel, fn[2]) in maps.started_in_import and                     any(l in import_lines for l in range(span[0], span[1] + 1)):
                plan.hotswap = False
            continue
        # import-time line: class body (data/structure of one class) or module level
        plan.hotswap = False
        where = regions.class_at(ln)
        if where is None or where[1] or global_attr:
            module_level = True
            continue
        names = attr_changes.get(where[0])
        if names:
            # only values of data attributes that nothing reads at import time changed: the tests
            # affected are those that execute a statement reading one of them
            for nm in names:
                for f, spans in maps.cattr_reads[nm].items():
                    for a, b in spans:
                        span_hit |= maps.lines_in_range(f, a, b)
                        plan.spans.append((f, a, b))
            plan.notes.append(f"{rel}: class attr {where[0]}.{','.join(sorted(names))}")
            continue
        qual = f"{modname}.{where[0]}"
        if qual in maps.tracked:
            class_quals.add(qual)
        else:
            module_level = True
    for q in class_quals:
        hit |= maps.instance_tests(q)
    if module_level and rel.startswith("sqlglot/") and _IMPORT_LINE.search(added + "\n" + removed):
        hit |= {n for n, t in enumerate(maps.test_ids) if t in SUBPROCESS_TESTS}
    if module_level:
        narrowed = _module_constant_tests(fp, old_text, new_text, rel, maps)
        if narrowed is None:
            hit |= maps.module_tests(rel)
        else:
            hit |= narrowed
            plan.notes.append(f"{rel}: module constant(s), {len(narrowed)} readers")
    plan.cand_other |= hit
    plan.cand_spans |= span_hit
    hit |= span_hit
    plan.candidates |= hit
    kind = "module-level" if module_level else ("class " + ",".join(sorted(class_quals)) if class_quals else "runtime")
    plan.notes.append(f"{rel}: {kind} {len(hit)} cand")


_SWAPS = {("is", "=="): ("is", "eq"), ("==", "is"): ("eq", "is"),
          ("isnot", "!="): ("isnot", "noteq"), ("!=", "isnot"): ("noteq", "isnot")}


def _only_equivalent_swaps(fp: FilePatch, old_text: str, new_text: str, rel: str, cmp_ok: set[str]) -> bool:
    """True if every change in this file swaps `is`<->`==` / `is not`<->`!=` in one comparison,
    at a site where (over every execution in the whole test suite, measured by lab/cmp_plugin.py)
    the identity and equality forms always gave the same answer. Then no test can change."""
    o2n, n2o, plus = _pair_lines(fp)
    n_minus = sum(1 for h in fp.hunks for l in h.lines if l[:1] == "-")
    if not o2n or n_minus != len(o2n) or len(plus) != len(n2o):
        return False  # only 1:1 line replacements qualify
    try:
        tree = ast.parse(old_text)
    except SyntaxError:
        return False
    compares = [n for n in ast.walk(tree) if isinstance(n, ast.Compare) and len(n.ops) == 1]
    old_l, new_l = old_text.split("\n"), new_text.split("\n")
    for ol, nl in o2n.items():
        a, b = old_l[ol - 1], new_l[nl - 1]
        if not a.isascii() or not b.isascii():
            return False
        p = 0
        while p < min(len(a), len(b)) and a[p] == b[p]:
            p += 1
        s = 0
        while s < min(len(a), len(b)) - p and a[len(a) - 1 - s] == b[len(b) - 1 - s]:
            s += 1
        kinds = _SWAPS.get((a[p:len(a) - s].replace(" ", ""), b[p:len(b) - s].replace(" ", "")))
        if kinds is None:
            return False
        want = {"is": ast.Is, "eq": ast.Eq, "isnot": ast.IsNot, "noteq": ast.NotEq}[kinds[0]]
        pos = (ol, p)
        hits = [c for c in compares if isinstance(c.ops[0], want)
                and (c.lineno, c.col_offset) <= pos <= (c.end_lineno or c.lineno, c.end_col_offset or 0)]
        if len(hits) != 1:
            return False
        c = hits[0]
        if f"{rel}:{c.lineno}:{c.col_offset}:{kinds[0]}" not in cmp_ok:
            return False
    return True


def _module_constant_tests(fp: FilePatch, old_text: str, new_text: str, rel: str, maps: Maps):
    """Tests that read a module-level constant changed by this file patch, or None if the change
    is anything other than edits to module-level `NAME = ...` assignments whose values are only
    read at runtime (inside functions), never at import time, never via aliases or strings."""
    if not maps.name_uses:
        return None
    try:
        t_old, t_new = ast.parse(old_text), ast.parse(new_text)
    except SyntaxError:
        return None
    old_l, new_l = old_text.split("\n"), new_text.split("\n")

    def assign_names(tree, ln, text_lines):
        for node in tree.body:
            if node.lineno <= ln <= (node.end_lineno or node.lineno):
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    tg = node.targets if isinstance(node, ast.Assign) else [node.target]
                    if all(isinstance(t, ast.Name) for t in tg):
                        return {t.id for t in tg}, (node.lineno, node.end_lineno or node.lineno)
                return None, None  # some other kind of statement changed
        s = text_lines[ln - 1].strip() if 0 < ln <= len(text_lines) else ""
        return (set(), None) if (not s or s.startswith("#")) else (None, None)

    names: set[str] = set()
    own_spans = []
    o_line = n_line = 0
    for h in fp.hunks:
        o = h.old_start if h.old_len > 0 else h.old_start + 1
        n = h.new_start if h.new_len > 0 else h.new_start + 1
        for l in h.lines:
            t = l[:1]
            if t == " ":
                o += 1
                n += 1
            elif t == "-":
                got, span = assign_names(t_old, o, old_l)
                if got is None:
                    return None
                names |= got
                if span:
                    own_spans.append(span)
                o += 1
            elif t == "+":
                got, _span = assign_names(t_new, n, new_l)
                if got is None:
                    return None
                names |= got
                n += 1
    if not names:
        return None
    hit: set[int] = set()
    for name in names:
        if name in maps.name_unsafe or name not in maps.name_uses:
            return None
        u = maps.name_uses[name]
        for f, lines in u.get("it", {}).items():
            for ln in lines:
                if f == rel and any(a <= ln <= b for a, b in own_spans):
                    continue  # the changed assignment itself
                return None  # read at import time: something derives from it
        for f, spans in u.get("rt", {}).items():
            for a, b in spans:
                hit |= maps.lines_in_range(f, a, b)
    return hit


def _pair_lines(fp: FilePatch):
    """Pair replaced lines positionally inside each -/+ block: (old->new, new->old, all '+' lines)."""
    o2n: dict[int, int] = {}
    n2o: dict[int, int] = {}
    plus: set[int] = set()
    for h in fp.hunks:
        o = h.old_start if h.old_len > 0 else h.old_start + 1
        n = h.new_start if h.new_len > 0 else h.new_start + 1
        mb: list[int] = []
        pb: list[int] = []

        def flush():
            for a, b in zip(mb, pb):
                o2n[a] = b
                n2o[b] = a
            mb.clear()
            pb.clear()

        for l in h.lines:
            t = l[:1]
            if t == " ":
                flush()
                o += 1
                n += 1
            elif t == "-":
                if pb:
                    flush()
                mb.append(o)
                o += 1
            elif t == "+":
                pb.append(n)
                plus.add(n)
                n += 1
        flush()
    return o2n, n2o, plus


def _import_time_lines(fp: FilePatch, old_text: str, new_text: str, regions: "Regions",
                       touched: set[int], neighbors: set[int]) -> set[int]:
    """Old line numbers whose change must be treated as import-time even though they sit in a
    function/lambda region: text outside the body changed on a shared line (a dict key next to
    a lambda, a one-line def's signature), or an added line lies outside any body."""
    new_regions = Regions(new_text)
    if not new_regions.ok:
        return set(touched | neighbors)
    old_l = old_text.split("\n")
    new_l = new_text.split("\n")
    o2n, n2o, plus = _pair_lines(fp)
    forced: set[int] = set()
    for ln in touched:
        fn = regions.innermost(ln)
        if fn is None or ln > len(old_l):
            continue
        seg_old = regions.outside_segments(fn, ln, old_l[ln - 1])
        if seg_old is None:
            continue
        nl = o2n.get(ln)
        if nl is None or nl > len(new_l):
            forced.add(ln)
            continue
        fn1 = new_regions.innermost(nl)
        seg_new = new_regions.outside_segments(fn1, nl, new_l[nl - 1]) if fn1 else ("<none>",)
        if seg_new != seg_old:
            forced.add(ln)
    for nl in sorted(plus):
        if nl > len(new_l):
            continue
        fn1 = new_regions.innermost(nl)
        if fn1 is None:
            return set(touched | neighbors)  # a new statement at module/class level
        seg_new = new_regions.outside_segments(fn1, nl, new_l[nl - 1])
        if seg_new is None:
            continue
        ol = n2o.get(nl)
        if ol is None or ol > len(old_l):
            return set(touched | neighbors)  # e.g. a new `"KEY": lambda ...` table entry
        fn0 = regions.innermost(ol)
        seg_old = regions.outside_segments(fn0, ol, old_l[ol - 1]) if fn0 else ("<none>",)
        if seg_old != seg_new:
            forced.add(ol)
    return forced


# Class attributes that feed global registries (e.g. SQL function name -> class); a change to
# one of them can affect tests that never instantiate the class, so they get module-wide treatment.
import re as _re  # noqa: E402

_GLOBAL_ATTR = _re.compile(r"^\s*(_sql_names|key)\s*[:=]", _re.M)
_IMPORT_LINE = _re.compile(r"^\s*(import|from)\s+\S", _re.M)

# Tests that run sqlglot in a separate interpreter (subprocess): the in-memory patch does not
# reach it, so running them here proves nothing; they are listed whenever they are candidates.
SUBPROCESS_TESTS = {"tests/dialects/test_dialect.py::TestDialect::test_lazy_load"}


# ----------------------------------------------------------------------------- execution

def split_workers(nums: list[int], dur_ms: list[int]) -> list[list[int]]:
    if not nums:
        return []
    items = sorted(nums, key=lambda n: -dur_ms[n])
    best = None
    for k in range(1, 9):
        if k > len(items):
            break
        loads = [0] * k
        bins: list[list[int]] = [[] for _ in range(k)]
        for n in items:
            i = loads.index(min(loads))
            loads[i] += dur_ms[n]
            bins[i].append(n)
        cost = max(loads) + WORKER_COST_MS * k
        if best is None or cost < best[0]:
            best = (cost, bins)
    return best[1]


def main() -> None:
    t0 = time.time()
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    ids = [t["id"] for t in tests]
    dur_ms = [round(t["seconds"] * 1000) for t in tests]
    maps = Maps(MAPS, ids)
    if maps.unknown_tests:
        log(f"WARNING: {len(maps.unknown_tests)} tests not in maps; numbering remapped by id")
    commit_dir = os.path.join(DATA, "commits")
    commits = sorted(f[:-6] for f in os.listdir(commit_dir) if f.endswith(".patch"))

    plans: list[Plan] = []
    for c in commits:
        with open(os.path.join(commit_dir, c + ".patch"), encoding="utf-8", errors="surrogateescape", newline="") as fh:
            text = fh.read()
        plans.append(plan_commit(c, text, maps, tests))
    log(f"planned {len(plans)} commits in {time.time()-t0:.2f}s")

    global MAX_NODES_N
    MAX_NODES_N = ids.index("tests/test_parser.py::TestParser::test_max_nodes")         if "tests/test_parser.py::TestParser::test_max_nodes" in ids else None
    cpu_cost = _load_cpu_costs(ids, dur_ms)
    pfail = _load_pfail(ids)
    results: dict[str, set[int]] = {}
    jobs = []
    for p in plans:
        if p.fallback:
            p.listed = set(p.candidates)
            cheap = []
        else:
            plan_partials(p, maps, cpu_cost)
            # test_max_nodes can be verified at a smaller scale (runner._max_nodes_scaled) unless
            # the patch edits the test itself
            p.scaled = not any(k.endswith("/tests/test_parser.py") for k in p.patched)
            if p.scaled and MAX_NODES_N is not None:
                p.cost_override[MAX_NODES_N] = 1.2
            verify, listed = choose_verify(p.candidates, dur_ms, _Costs(cpu_cost, p.cost_override), pfail)
            never = {n for n in verify if ids[n] in SUBPROCESS_TESTS}
            verify -= never
            listed |= never
            p.listed = listed
            cheap = sorted(verify)
            p.case_filter = _case_filter(p, set(cheap), maps)
        if cheap or (p.probe and not p.fallback):
            jobs.append((p, cheap))
        else:
            results[p.commit] = set(p.listed)

    global PROBE_SWEEP, MAPS_OBJ
    PROBE_SWEEP = sorted(set(maps.imp_mods) | {m for ms in maps.collect.values() for m in ms})
    MAPS_OBJ = maps
    if jobs:
        _preload(sorted({ids[n].split("::")[0][:-3].replace("/", ".") for _, cheap in jobs for n in cheap}))
        _run_jobs(jobs, ids, dur_ms, results)

    for p in plans:
        sel = sorted(results.get(p.commit, set(p.listed)))
        print(json.dumps({"commit": p.commit, "workers": split_workers(sel, dur_ms)}), flush=True)
        log(f"{p.commit}: cand={len(p.candidates)} out={len(sel)} {'; '.join(p.notes)}")
    import resource
    me, ch = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = me.ru_utime + me.ru_stime + ch.ru_utime + ch.ru_stime
    log(f"total {time.time()-t0:.2f}s wall, cpu {cpu:.2f}s (self+children)")


def _load_pfail(ids):
    try:
        with open(os.path.join(os.path.dirname(MAPS), "pfail.json")) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    default, per = d.get("default", P_FAIL), d.get("tests", {})
    probs = [per.get(t, default) for t in ids]
    return lambda n: probs[n]


class _Costs:
    def __init__(self, base, override):
        self.base, self.override = base, override

    def __getitem__(self, n):
        return self.override.get(n, self.base[n])


def _case_filter(plan: Plan, run: set[int], maps: Maps) -> dict[str, list[int]]:
    """fixture file -> case indices to load, for the partial tests that will actually run.

    A file is filtered only if no other test that runs fully in the same child reads it.
    """
    filt: dict[str, set[int]] = {}
    for n, sel in plan.partial.items():
        if n in run:
            for f, ks in sel.items():
                filt.setdefault(f, set()).update(ks)
    for f in list(filt):
        readers = maps.opened_tests("tests/fixtures/" + f)
        for t in readers & run:
            if f in plan.partial.get(t, {}):
                continue
            # TestExecutor.setUpClass loads both TPC case lists for every executor test, but only
            # the case-driven ones (in case_index) iterate them; other executor tests are unaffected
            iterates = f in maps.case_index.get(t, {}).get("files", {}) or                 not maps.test_ids[t].startswith("tests/test_executor.py::")
            if iterates:
                del filt[f]
                break
    # a partial test whose file filter was dropped simply runs fully
    return {f: sorted(ks) for f, ks in filt.items()}


try:  # measured CPU of each case-driven test with no cases (lab/measure_base.py)
    with open(os.path.join(os.path.dirname(MAPS), "base_cost.json")) as _fh:
        _BASE_COST = json.load(_fh)
except (OSError, ValueError):
    _BASE_COST = {}

P_FAIL = 0.25  # rough chance that a candidate actually fails (only steers run-vs-list choices)


def _load_cpu_costs(ids, dur_ms) -> list[float]:
    """CPU seconds it costs to run each test in our runner.

    Measured for slow tests (maps/cpu_cost.json). Otherwise estimated: optimizer tests fan out
    to a process pool in the labelled run, so their recorded (wall) time understates the
    serial CPU cost by ~4x; other tests cost about their recorded time.
    """
    measured = {}
    try:
        with open(os.path.join(os.path.dirname(MAPS), "cpu_cost.json")) as fh:
            measured = json.load(fh)
    except FileNotFoundError:
        pass
    out = []
    for tid, d in zip(ids, dur_ms):
        if tid in measured:
            out.append(measured[tid])
        elif tid.startswith("tests/test_optimizer.py::") or tid.startswith("tests/test_executor.py::"):
            out.append(4.0 * d / 1000 + 0.01)
        else:
            out.append(1.2 * d / 1000 + 0.002)
    return out


def choose_verify(cands: set[int], dur_ms: list[int], cpu, pf=None):
    """Split candidates into (run them to find out, list them without running).

    Running a test costs its CPU time; if it then fails it is listed as well. Listing costs its
    recorded time on some worker (+2 s if it needs a worker of its own). pf(n) is a prior chance
    that candidate n actually fails (measured on synthetic patches); it only steers this choice:
    whatever runs is verified, so it can never cause a miss.
    """
    if not cands:
        return set(), set()
    pf = pf or (lambda n: P_FAIL)
    MAX_RUN_S = 25.0  # keep any single verification well inside the batch time limit
    d = [x / 1000 for x in dur_ms]
    # 1) tests that are never worth running (even listed on a worker of their own)
    must_list = {n for n in cands if cpu[n] > MAX_RUN_S or cpu[n] + pf(n) * d[n] > d[n] + 2.0}
    # 2) expensive-but-runnable tests: compare against the MARGINAL cost of listing them next to
    #    what is listed anyway (a 29 s test beside a 21 s one adds ~8 s, not 31 s); expensive runs
    #    also need a clear expected win since they are high-variance (run AND list if it fails)
    for n in sorted((n for n in cands - must_list if cpu[n] > 1.0), key=lambda n: -dur_ms[n]):
        base = _pack_cost(split_workers(sorted(must_list), dur_ms), dur_ms) / 1000 if must_list else 0.0
        with_n = _pack_cost(split_workers(sorted(must_list | {n}), dur_ms), dur_ms) / 1000
        if cpu[n] + pf(n) * d[n] > 0.8 * (with_n - base):
            must_list.add(n)
    rest = cands - must_list
    if must_list:
        # the 2 s worker fee is paid anyway: list whatever is cheaper to list than to run,
        # as long as it does not lengthen the busiest worker
        bins = split_workers(sorted(must_list), dur_ms)
        loads = [sum(dur_ms[n] for n in b) for b in bins]
        cur_max = max(loads)
        listed = set(must_list)
        for n in sorted(rest, key=lambda n: -dur_ms[n]):
            i = loads.index(min(loads))
            new_max = max(cur_max, loads[i] + dur_ms[n])
            marginal = (new_max - cur_max) / 1000
            # list it when that is free (fits under the busiest worker) or cheaper than running it
            if marginal <= 0 or marginal <= cpu[n] * (1 - pf(n)):
                loads[i] += dur_ms[n]
                cur_max = new_max
                listed.add(n)
        return rest - listed, listed
    # nothing must be listed: run everything, or list everything without running
    p_none = 1.0
    for n in rest:
        p_none *= 1 - pf(n)
    run_cost = sum(cpu[n] for n in rest) + (1 - p_none) * 2.5
    list_cost = _pack_cost(split_workers(sorted(rest), dur_ms), dur_ms) / 1000
    if list_cost <= run_cost:
        return set(), set(rest)
    return set(rest), set()


def _pack_cost(bins, dur_ms) -> int:
    if not bins:
        return 0
    return max(sum(dur_ms[n] for n in b) for b in bins) + WORKER_COST_MS * len(bins)


def _preload(test_modules=()):
    """Import shared modules once in the parent so every forked child starts warm."""
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    os.chdir(SRC)
    import unittest  # noqa: F401

    from runner import install_inline_executor

    install_inline_executor()
    try:
        import sqlglot  # noqa: F401
        import sqlglot.optimizer  # noqa: F401
    except Exception as e:
        log(f"preload failed: {e}")
    import importlib
    for m in test_modules:
        try:
            importlib.import_module(m)
        except Exception as e:  # the child re-imports and reports it properly
            log(f"preload {m} failed: {e!r}")
    # Move everything imported so far into the permanent GC generation: forked children then
    # never re-scan (or copy-on-write touch) the parent's objects during garbage collection.
    import gc

    gc.collect()
    gc.freeze()


def _run_jobs(jobs, ids, dur_ms, results):
    import select
    import signal

    pending = list(jobs)
    running = {}  # pid -> (plan, cheap, rfd, start, chunks)
    while pending or running:
        # global wall-clock budget: past it, stop verifying and list every remaining candidate
        if pending and time.monotonic() > DEADLINE:
            for plan, cheap in pending:
                results[plan.commit] = set(plan.listed) | set(cheap)
                plan.notes.append("deadline: listing all")
            pending = []
        while pending and len(running) < PARALLEL:
            plan, cheap = pending.pop(0)
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:  # child
                os.close(r)
                code = 0
                try:
                    from runner import run_tests
                    timeouts = {ids[n]: max(1.0, 5 * dur_ms[n] / 1000) for n in cheap}
                    if getattr(plan, "scaled", False) and MAX_NODES_N in cheap:
                        # ~0.7 s normally; a big slowdown would make the full test time out
                        timeouts[ids[MAX_NODES_N]] = 10.0
                    import resource as _r
                    stats = {}
                    failed = run_tests(SRC, [ids[n] for n in cheap], plan.patched, timeouts, stats,
                                       case_filter=getattr(plan, "case_filter", None),
                                       hotswap=plan.hotswap, scaled=getattr(plan, "scaled", False),
                                       probe=sorted(plan.probe), sweep=PROBE_SWEEP)
                    ru = _r.getrusage(_r.RUSAGE_SELF)
                    stats["cpu"] = round(ru.ru_utime + ru.ru_stime, 3)
                    payload = json.dumps({"failed": sorted(failed), "broken": stats.pop("broken", []),
                                          "stats": stats}).encode()
                except BaseException as e:  # noqa: BLE001
                    payload = json.dumps({"error": repr(e)}).encode()
                    code = 1
                try:
                    with os.fdopen(w, "wb") as fh:
                        fh.write(payload)
                finally:
                    os._exit(code)
            os.close(w)
            running[pid] = (plan, cheap, r, time.time(), [])
        # collect output
        fds = {v[2]: pid for pid, v in running.items()}
        ready, _, _ = select.select(list(fds), [], [], 0.5)
        for fd in ready:
            pid = fds[fd]
            chunk = os.read(fd, 1 << 16)
            if chunk:
                running[pid][4].append(chunk)
                continue
            os.close(fd)
            os.waitpid(pid, 0)
            plan, cheap, _, start, chunks = running.pop(pid)
            try:
                data = json.loads(b"".join(chunks).decode() or "null")
            except Exception:
                data = None
            if isinstance(data, dict) and "failed" in data:
                index = {t: i for i, t in enumerate(ids)}
                failed = {index[t] for t in data["failed"] if t in index}
                results[plan.commit] = set(plan.listed) | failed
                if data.get("broken"):
                    bt = MAPS_OBJ.broken_tests(data["broken"])
                    results[plan.commit] |= bt
                    more = "..." if len(data["broken"]) > 4 else ""
                    plan.notes.append(f"import broken {data['broken'][:4]}{more}: +{len(bt)} listed")
                st = data.get("stats", {})
                plan.notes.append(f"verified {len(cheap)} in {time.time()-start:.1f}s -> {len(failed)} fail "
                                  f"[cpu {st.get('cpu')} setup {st.get('setup_cpu')} import {st.get('import_cpu')} "
                                  f"mode {st.get('mode')} cases {st.get('case_filter')} "
                                  f"probe {len(plan.probe)}/{st.get('probe_cpu')}]")
            else:
                results[plan.commit] = set(plan.listed) | set(cheap)
                plan.notes.append(f"child error {data!r}: listing all {len(cheap)}")
        # enforce time limits
        now = time.time()
        for pid, (plan, cheap, fd, start, chunks) in list(running.items()):
            if now - start > CHILD_LIMIT_S or time.monotonic() > DEADLINE + 15:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)
                os.close(fd)
                running.pop(pid)
                results[plan.commit] = set(plan.listed) | set(cheap)
                plan.notes.append("child timeout: listing all")


if __name__ == "__main__":
    main()
