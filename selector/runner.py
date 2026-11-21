"""Run a set of sqlglot tests against a patched source tree, in memory, and report which fail.

Called in a forked child of the selector. The pristine checkout lives in SRC (read-only);
patched files are served from memory:
  * Python modules under sqlglot/ and tests/: a meta-path finder executes the patched source.
  * Any other file (SQL fixtures, ...): builtins.open/io.open are wrapped to return the
    patched text for those exact paths.
A test "fails" like pytest would count it: an error or failure in setUpClass/setUp/test/
tearDown, any failing subTest, a timeout, or its module failing to import.
"""

from __future__ import annotations

import builtins
import importlib
import importlib.abc
import importlib.util
import io
import os
import signal
import sys
import unittest


class _InlineExecutor:
    """Stand-in for ProcessPoolExecutor: runs tasks in this process.

    sqlglot's optimizer/executor tests create spawn-based process pools (one process per CPU).
    In the scorer's sandbox that costs ~1 CPU-second per spawned process and can exceed the
    256-process limit; the computations are pure, so running them inline gives the same results.
    """

    # Everything crossing the process boundary is pickled and unpickled exactly like a real
    # pool would (ForkingPickler), because sqlglot pickles expressions via sqlglot/serde.py and
    # a change there breaks pool-using tests only through that round trip.

    @staticmethod
    def _roundtrip(obj):
        import pickle
        from multiprocessing.reduction import ForkingPickler

        return pickle.loads(ForkingPickler.dumps(obj))

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer, initargs = self._roundtrip((initializer, initargs))
            initializer(*initargs)

    def submit(self, fn, /, *args, **kwargs):
        from concurrent.futures import Future

        f = Future()
        try:
            fn, args, kwargs = self._roundtrip((fn, args, kwargs))
            f.set_result(self._roundtrip(fn(*args, **kwargs)))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def map(self, fn, *iterables, timeout=None, chunksize=1, **kwargs):
        return [self.submit(fn, *a).result() for a in zip(*iterables)]

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install_inline_executor():
    import concurrent.futures as cf

    cf.ProcessPoolExecutor = _InlineExecutor
    try:
        import concurrent.futures.process as cfp

        cfp.ProcessPoolExecutor = _InlineExecutor
    except Exception:  # noqa: BLE001
        pass


class _Timeout(Exception):
    """Raised by SIGALRM inside a test; re-armed every 0.25 s in case code swallows it."""


def _alarm(signum, frame):
    raise _Timeout()


class _PatchedFinder(importlib.abc.MetaPathFinder):
    def __init__(self, src_root: str, patched: dict[str, str | None]):
        self.src_root = src_root
        self.patched = patched  # abs path -> new text (None = deleted)

    def _candidates(self, fullname: str):
        rel = fullname.replace(".", "/")
        base = os.path.join(self.src_root, rel)
        return (base + ".py", False), (os.path.join(base, "__init__.py"), True)

    def find_spec(self, fullname, path=None, target=None):
        if not (fullname == "sqlglot" or fullname.startswith("sqlglot.") or fullname == "tests" or fullname.startswith("tests.")):
            return None
        for cand, is_pkg in self._candidates(fullname):
            if cand in self.patched:
                text = self.patched[cand]
                if text is None:
                    return None  # deleted: fall through to normal finders, which will not find a .py...
                loader = _SourceLoader(cand, text)
                spec = importlib.util.spec_from_file_location(
                    fullname, cand, loader=loader,
                    submodule_search_locations=[os.path.dirname(cand)] if is_pkg else None,
                )
                return spec
        # deleted module whose file still exists on disk: block it
        for cand, _ in self._candidates(fullname):
            if cand in self.patched and self.patched[cand] is None:
                raise ModuleNotFoundError(fullname)
        return None


class _SourceLoader(importlib.abc.Loader):
    def __init__(self, path: str, text: str):
        self.path = path
        self.text = text

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        code = compile(self.text, self.path, "exec", dont_inherit=True)
        exec(code, module.__dict__)


class _DeletedBlocker(importlib.abc.MetaPathFinder):
    def __init__(self, src_root: str, deleted: set[str]):
        self.src_root = src_root
        self.deleted = deleted

    def find_spec(self, fullname, path=None, target=None):
        rel = fullname.replace(".", "/")
        for cand in (os.path.join(self.src_root, rel + ".py"), os.path.join(self.src_root, rel, "__init__.py")):
            if cand in self.deleted:
                raise ModuleNotFoundError(f"No module named {fullname!r}")
        return None


def _install_file_overlay(patched_files: dict[str, str | None]):
    if not patched_files:
        return
    real_open = builtins.open

    def _norm(p):
        try:
            return os.path.abspath(os.fsdecode(p)) if isinstance(p, (str, bytes, os.PathLike)) else None
        except Exception:
            return None

    def overlay_open(file, mode="r", *args, **kwargs):
        p = _norm(file)
        if p is not None and p in patched_files and not any(c in mode for c in "wax+"):
            text = patched_files[p]
            if text is None:
                raise FileNotFoundError(2, "No such file or directory", p)
            if "b" in mode:
                return io.BytesIO(text.encode("utf-8"))
            return io.StringIO(text)
        return real_open(file, mode, *args, **kwargs)

    builtins.open = overlay_open
    io.open = overlay_open
    # pathlib uses io.open, which we replaced; os.path.exists for deleted files is not overlaid.


def _purge_modules():
    for name in list(sys.modules):
        if name == "sqlglot" or name.startswith("sqlglot.") or name == "tests" or name.startswith("tests."):
            del sys.modules[name]


class _Result(unittest.TestResult):
    def __init__(self, timeouts: dict[str, float]):
        super().__init__()
        self.bad: set[str] = set()
        self.timeouts = timeouts

    def startTest(self, test):
        super().startTest(test)
        tid = self._id(test)
        signal.setitimer(signal.ITIMER_REAL, self.timeouts.get(tid, 5.0), 0.25)

    def stopTest(self, test):
        signal.setitimer(signal.ITIMER_REAL, 0)
        super().stopTest(test)

    def _id(self, test):
        return getattr(test, "_xero_id", None)

    def addError(self, test, err):
        tid = self._id(test)
        if tid is None:
            # class/module fixture failure: _ErrorHolder; mark the whole class via caller
            self.bad.add("__fixture__:" + str(test))
        else:
            self.bad.add(tid)

    def addFailure(self, test, err):
        tid = self._id(test)
        if tid:
            self.bad.add(tid)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            tid = self._id(test)
            if tid:
                self.bad.add(tid)

    def addUnexpectedSuccess(self, test):
        tid = self._id(test)
        if tid:
            self.bad.add(tid)


def _cpu():
    import resource
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def _module_name(src_root: str, path: str) -> str:
    rel = os.path.relpath(path, src_root)[:-3].replace(os.sep, "/").replace("/", ".")
    return rel[: -len(".__init__")] if rel.endswith(".__init__") else rel


def _code_objects(code, acc):
    import types

    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            acc.append(c)
            _code_objects(c, acc)
    return acc


def _keyed(codes):
    seen: dict[str, int] = {}
    out = {}
    for c in codes:
        i = seen.get(c.co_qualname, 0)
        seen[c.co_qualname] = i + 1
        out[(c.co_qualname, i)] = c
    return out, seen


def hot_swap(src_root: str, path: str, new_text: str) -> bool:
    """Swap the code of every live function defined in `path` for its patched version.

    Only used when every change is inside function bodies that never ran during an import
    (the planner checks this against the import-time trace), so module and class state built
    at import time is unaffected. Returns False (caller falls back to re-importing) whenever
    the function structure differs or a live function cannot be matched.
    """
    import gc
    import types

    mod = sys.modules.get(_module_name(src_root, path))
    if mod is None:
        return True  # not imported yet: the patched finder serves the new source on import
    if os.path.abspath(getattr(mod, "__file__", "") or "") != os.path.abspath(path):
        return False
    try:
        with builtins.open(path, encoding="utf-8", newline="") as fh:
            old_text = fh.read()
        old_root = compile(old_text, path, "exec", dont_inherit=True)
        new_root = compile(new_text, path, "exec", dont_inherit=True)
    except (OSError, SyntaxError, ValueError):
        return False
    old_k, old_n = _keyed(_code_objects(old_root, []))
    new_k, new_n = _keyed(_code_objects(new_root, []))
    if old_n != new_n:
        return False
    by_pos = {}
    for key, c in old_k.items():
        pos = (c.co_qualname, c.co_firstlineno)
        if pos in by_pos:
            by_pos[pos] = None  # ambiguous (e.g. two lambdas on one line)
        else:
            by_pos[pos] = key
    gc.unfreeze()  # the parent froze its heap; get_objects() does not see the frozen generation
    swaps = []
    for f in gc.get_objects():
        if type(f) is not types.FunctionType:
            continue
        code = f.__code__
        if code.co_filename != path:
            continue
        key = by_pos.get((code.co_qualname, code.co_firstlineno))
        if key is None:
            return False
        new = new_k[key]
        if new.co_freevars != code.co_freevars:
            return False
        swaps.append((f, new))
    if not swaps and any(k[0] != "<module>" and not k[0].endswith(">") or "<lambda>" in k[0] for k in old_k):
        return False  # a loaded module whose functions cannot be found: never verify stale code
    for f, new in swaps:
        f.__code__ = new
    return True


def _clear_caches():
    import functools
    import gc

    gc.unfreeze()
    for o in gc.get_objects():
        if isinstance(o, functools._lru_cache_wrapper):
            try:
                o.cache_clear()
            except Exception:  # noqa: BLE001
                pass


def _install_case_filter(case_filter):
    """Restrict tests/helpers.load_sql_fixture_pairs to the selected case indices per file."""
    if not case_filter:
        return None
    sel = {f: set(ks) for f, ks in case_filter.items()}
    import tests.helpers as helpers

    orig = helpers.load_sql_fixture_pairs

    def load_sql_fixture_pairs(filename, *args, **kwargs):
        keep = sel.get(filename)
        for i, item in enumerate(orig(filename, *args, **kwargs)):
            if keep is None or i in keep:
                yield item

    helpers.load_sql_fixture_pairs = load_sql_fixture_pairs
    return orig, load_sql_fixture_pairs


def _rebind_filter(module, pair):
    if pair and getattr(module, "load_sql_fixture_pairs", None) is pair[0]:
        module.load_sql_fixture_pairs = pair[1]


def run_tests(src_root: str, test_ids: list[str], patched: dict[str, str | None], timeouts: dict[str, float],
              stats=None, case_filter=None, hotswap=False) -> set[str]:
    """Return the subset of test_ids that fail on the patched tree."""
    stats = {} if stats is None else stats
    c0 = _cpu()
    py_patched = {p: t for p, t in patched.items() if p.endswith(".py")}
    other_patched = {p: t for p, t in patched.items() if not p.endswith(".py")}
    deleted_py = {p for p, t in py_patched.items() if t is None}

    install_inline_executor()
    _install_file_overlay(other_patched)
    mode = "none"
    if py_patched:
        swapped = False
        if hotswap and not deleted_py:
            swapped = all(hot_swap(src_root, p, t) for p, t in py_patched.items())
            if swapped:
                _clear_caches()
                mode = "hotswap"
        if not swapped:
            _purge_modules()
            mode = "reimport"
        sys.meta_path.insert(0, _PatchedFinder(src_root, {p: t for p, t in py_patched.items() if t is not None}))
        if deleted_py:
            sys.meta_path.insert(0, _DeletedBlocker(src_root, deleted_py))
        importlib.invalidate_caches()
    stats["mode"] = mode
    filter_pair = _install_case_filter(case_filter)
    stats["case_filter"] = {f: len(k) for f, k in (case_filter or {}).items()} or None

    failed: set[str] = set()
    by_module: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for tid in test_ids:
        file, cls, meth = tid.split("::")
        mod = file[:-3].replace("/", ".")
        by_module.setdefault(mod, {}).setdefault(cls, []).append((meth, tid))

    signal.signal(signal.SIGALRM, _alarm)
    stats["setup_cpu"] = round(_cpu() - c0, 3)
    imp = 0.0
    for mod, classes in by_module.items():
        ci = _cpu()
        try:
            module = importlib.import_module(mod)
            imp += _cpu() - ci
            _rebind_filter(module, filter_pair)
        except BaseException:
            for cls, items in classes.items():
                failed.update(tid for _, tid in items)
            continue
        for cls, items in classes.items():
            klass = getattr(module, cls, None)
            if klass is None:
                failed.update(tid for _, tid in items)
                continue
            suite = unittest.TestSuite()
            tests_in_suite = []
            for meth, tid in items:
                try:
                    t = klass(meth)
                except BaseException:
                    failed.add(tid)
                    continue
                t._xero_id = tid
                tests_in_suite.append(t)
                suite.addTest(t)
            res = _Result(timeouts)
            try:
                suite.run(res)
            except BaseException:
                failed.update(t._xero_id for t in tests_in_suite)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
            if any(b.startswith("__fixture__:") for b in res.bad):
                failed.update(t._xero_id for t in tests_in_suite)
            failed.update(b for b in res.bad if not b.startswith("__fixture__:"))
            unittest.suite._call_if_exists(res, "_restoreStdout")
    stats["import_cpu"] = round(imp, 3)
    return failed
