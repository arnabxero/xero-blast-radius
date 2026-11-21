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
CHILD_LIMIT_S = float(os.environ.get("BLAST_CHILD_LIMIT", "75"))
WORKER_COST_MS = 2000


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------- maps

class Maps:
    def __init__(self, path: str):
        with gzip.open(path, "rt") as fh:
            d = json.load(fh)
        self.sets = d["line_sets"]
        self.lines = d["lines"]
        self.mod_run = d["mod_run"]
        self.mod_import = d["mod_import"]
        self.opened = d["opened"]
        self.class_tests = d.get("class_tests", {})
        self.tracked = set(d.get("tracked", []))

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
            return
        self.ok = True
        # statement spans: simple statements = whole span; compound statements = header only
        self.stmts: list[tuple[int, int]] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.body:
                    start = node.body[0].lineno
                    # a decorator/signature line shares the def line; body may start on the def line
                    self.funcs.append((start, node.end_lineno or start, node.lineno))
            elif isinstance(node, ast.Lambda):
                self.funcs.append((node.body.lineno, node.body.end_lineno or node.body.lineno, node.lineno))
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
    return plan


def _candidates_for_file(plan: Plan, maps: Maps, rel: str, fp: FilePatch, old_text, new_text):
    is_py = rel.endswith(".py")
    in_pkg = rel.startswith("sqlglot/") or rel.startswith("tests/")
    if not is_py or not in_pkg:
        hit = maps.opened_tests(rel)
        if fp.is_new or fp.is_deleted or fp.rename:
            d = os.path.dirname(rel)
            if d.startswith("tests/"):
                hit |= maps.opened_in_dir(d)
        if is_py and not in_pkg:
            hit |= maps.module_tests(rel)
        plan.candidates |= hit
        plan.notes.append(f"file {rel}: {len(hit)} cand")
        return

    # python module inside sqlglot/ or tests/
    if fp.is_new and old_text is None:
        # brand-new module: only matters if something imports it (other hunks) or a test lists the package
        d = os.path.dirname(rel)
        hit = set()
        for f in list(maps.mod_import.keys()) + list(maps.mod_run.keys()):
            if f.startswith(d + "/") and f.endswith("__init__.py"):
                hit |= maps.module_tests(f)
        plan.candidates |= hit
        plan.notes.append(f"new module {rel}: {len(hit)} cand")
        return
    if fp.is_deleted or new_text is None:
        hit = maps.module_tests(rel)
        plan.candidates |= hit
        plan.notes.append(f"deleted module {rel}: {len(hit)} cand")
        return
    if rel != fp.old_path:
        return  # rename target handled via old path
    if python_semantically_equal(old_text, new_text):
        plan.notes.append(f"{rel}: comments/whitespace only")
        return
    regions = Regions(old_text)
    new_ok = True
    try:
        ast.parse(new_text)
    except SyntaxError:
        new_ok = False
    touched, neighbors = changed_old_lines(fp)
    if not regions.ok or not new_ok:
        hit = maps.module_tests(rel)
        plan.candidates |= hit
        plan.notes.append(f"{rel}: syntax error -> module users {len(hit)}")
        return
    hit: set[int] = set()
    module_level = False
    class_quals: set[str] = set()
    modname = rel[:-3].replace("/", ".")
    if modname.endswith(".__init__"):
        modname = modname[: -len(".__init__")]
    added = "\n".join(l[1:] for h in fp.hunks for l in h.lines if l[:1] == "+")
    removed = "\n".join(l[1:] for h in fp.hunks for l in h.lines if l[:1] == "-")
    global_attr = bool(_GLOBAL_ATTR.search(added) or _GLOBAL_ATTR.search(removed))
    for ln in sorted(touched | neighbors):
        fn = regions.innermost(ln)
        if fn is not None:
            fs, fe, _ = fn
            # A deterministic test can only behave differently if it executes the changed
            # statement (or reaches the insertion point). Use the statement's full span so
            # continuation lines without their own line events are still covered; fall back
            # to the whole function when the line is not inside a statement within it.
            span = regions.stmt_span(ln)
            if span is None or span[0] < fs or span[1] > fe:
                span = (fs, fe)
            hit |= maps.lines_in_range(rel, span[0], span[1])
            continue
        # import-time line: class body (data/structure of one class) or module level
        where = regions.class_at(ln)
        if where is None or where[1] or global_attr:
            module_level = True
            continue
        qual = f"{modname}.{where[0]}"
        if qual in maps.tracked:
            class_quals.add(qual)
        else:
            module_level = True
    for q in class_quals:
        hit |= maps.instance_tests(q)
    if module_level:
        hit |= maps.module_tests(rel)
    plan.candidates |= hit
    kind = "module-level" if module_level else ("class " + ",".join(sorted(class_quals)) if class_quals else "runtime")
    plan.notes.append(f"{rel}: {kind} {len(hit)} cand")


# Class attributes that feed global registries (e.g. SQL function name -> class); a change to
# one of them can affect tests that never instantiate the class, so they get module-wide treatment.
import re as _re  # noqa: E402

_GLOBAL_ATTR = _re.compile(r"^\s*(_sql_names|key)\s*[:=]", _re.M)


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
    maps = Maps(MAPS)
    commit_dir = os.path.join(DATA, "commits")
    commits = sorted(f[:-6] for f in os.listdir(commit_dir) if f.endswith(".patch"))

    plans: list[Plan] = []
    for c in commits:
        with open(os.path.join(commit_dir, c + ".patch"), encoding="utf-8", errors="surrogateescape", newline="") as fh:
            text = fh.read()
        plans.append(plan_commit(c, text, maps, tests))
    log(f"planned {len(plans)} commits in {time.time()-t0:.2f}s")

    cpu_cost = _load_cpu_costs(ids, dur_ms)
    results: dict[str, set[int]] = {}
    jobs = []
    for p in plans:
        if p.fallback:
            p.listed = set(p.candidates)
            cheap = []
        else:
            verify, listed = choose_verify(p.candidates, dur_ms, cpu_cost)
            p.listed = listed
            cheap = sorted(verify)
        if cheap:
            jobs.append((p, cheap))
        else:
            results[p.commit] = set(p.listed)

    if jobs:
        _preload()
        _run_jobs(jobs, ids, dur_ms, results)

    for p in plans:
        sel = sorted(results.get(p.commit, set(p.listed)))
        print(json.dumps({"commit": p.commit, "workers": split_workers(sel, dur_ms)}), flush=True)
        log(f"{p.commit}: cand={len(p.candidates)} out={len(sel)} {'; '.join(p.notes)}")
    import resource
    me, ch = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = me.ru_utime + me.ru_stime + ch.ru_utime + ch.ru_stime
    log(f"total {time.time()-t0:.2f}s wall, cpu {cpu:.2f}s (self+children)")


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


def choose_verify(cands: set[int], dur_ms: list[int], cpu: list[float]):
    """Split candidates into (run them to find out, list them without running)."""
    if not cands:
        return set(), set()
    MAX_RUN_S = 8.0
    must_list = {n for n in cands if cpu[n] > MAX_RUN_S or cpu[n] > dur_ms[n] / 1000 + 2.0}
    rest = cands - must_list
    if must_list:
        # the 2 s worker fee is paid anyway: list whatever is cheaper to list than to run,
        # as long as it does not lengthen the busiest worker
        base = split_workers(sorted(must_list), dur_ms)
        base_cost = _pack_cost(base, dur_ms)
        listed = set(must_list)
        for n in sorted(rest, key=lambda n: -dur_ms[n]):
            if cpu[n] <= dur_ms[n] / 1000:
                continue
            trial = split_workers(sorted(listed | {n}), dur_ms)
            if (_pack_cost(trial, dur_ms) - base_cost) / 1000 <= cpu[n] * (1 - P_FAIL):
                listed.add(n)
                base_cost = _pack_cost(trial, dur_ms)
        return rest - listed, listed
    # nothing must be listed: either run everything, or list everything without running
    run_cost = sum(cpu[n] for n in rest)
    list_cost = _pack_cost(split_workers(sorted(rest), dur_ms), dur_ms) / 1000
    if list_cost <= run_cost + P_FAIL * 2.0 and len(rest) <= 3:
        return set(), set(rest)
    return set(rest), set()


def _pack_cost(bins, dur_ms) -> int:
    if not bins:
        return 0
    return max(sum(dur_ms[n] for n in b) for b in bins) + WORKER_COST_MS * len(bins)


def _preload():
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


def _run_jobs(jobs, ids, dur_ms, results):
    import select
    import signal

    pending = list(jobs)
    running = {}  # pid -> (plan, cheap, rfd, start, chunks)
    while pending or running:
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
                    failed = run_tests(SRC, [ids[n] for n in cheap], plan.patched, timeouts)
                    payload = json.dumps(sorted(failed)).encode()
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
            if isinstance(data, list):
                index = {t: i for i, t in enumerate(ids)}
                failed = {index[t] for t in data if t in index}
                results[plan.commit] = set(plan.listed) | failed
                plan.notes.append(f"verified {len(cheap)} in {time.time()-start:.1f}s -> {len(failed)} fail")
            else:
                results[plan.commit] = set(plan.listed) | set(cheap)
                plan.notes.append(f"child error {data!r}: listing all {len(cheap)}")
        # enforce time limits
        now = time.time()
        for pid, (plan, cheap, fd, start, chunks) in list(running.items()):
            if now - start > CHILD_LIMIT_S:
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
