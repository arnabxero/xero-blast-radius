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


def run_tests(src_root: str, test_ids: list[str], patched: dict[str, str | None], timeouts: dict[str, float]) -> set[str]:
    """Return the subset of test_ids that fail on the patched tree."""
    py_patched = {p: t for p, t in patched.items() if p.endswith(".py")}
    other_patched = {p: t for p, t in patched.items() if not p.endswith(".py")}
    deleted_py = {p for p, t in py_patched.items() if t is None}

    _install_file_overlay(other_patched)
    if py_patched:
        _purge_modules()
        sys.meta_path.insert(0, _PatchedFinder(src_root, {p: t for p, t in py_patched.items() if t is not None}))
        if deleted_py:
            sys.meta_path.insert(0, _DeletedBlocker(src_root, deleted_py))
        importlib.invalidate_caches()

    failed: set[str] = set()
    by_module: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for tid in test_ids:
        file, cls, meth = tid.split("::")
        mod = file[:-3].replace("/", ".")
        by_module.setdefault(mod, {}).setdefault(cls, []).append((meth, tid))

    signal.signal(signal.SIGALRM, _alarm)
    for mod, classes in by_module.items():
        try:
            module = importlib.import_module(mod)
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
    return failed
