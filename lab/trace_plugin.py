"""pytest plugin: per-test line coverage + opened files, using Python 3.13 sys.monitoring.

Run one test per process:  pytest -p trace_plugin <nodeid>
Phases: "import" = everything up to the test starting (pytest start-up, collection,
module imports); "run" = setup + call + teardown of the test (incl. setUpClass,
lazily imported dialect modules, fixture reads).
Writes JSON to $TRACE_OUT.
"""

import json
import os
import sys

import pytest

ROOT = os.environ.get("TRACE_ROOT", os.getcwd()).rstrip("/") + "/"
OUT = os.environ.get("TRACE_OUT")

mon = sys.monitoring
TOOL = 4
_phase = "import"
_lines = {"import": set(), "run": set(), "import_exec": set()}
_opened = {"import": set(), "run": set()}
_importing = 0  # >0 while a sqlglot/tests module body is executing (module import)


def _on_line(code, line):
    f = code.co_filename
    if f.startswith(ROOT):
        key = (f[len(ROOT):], line)
        bucket = _lines.get(_phase)
        if bucket is not None:
            bucket.add(key)
        if _importing:
            _lines["import_exec"].add(key)
    return mon.DISABLE


# Optional case filter for case-driven tests (fixture loaders in tests/helpers.py):
#   TRACE_CASE unset         -> no filtering
#   TRACE_CASE=BASE          -> every loader yields nothing (the test's case-independent part)
#   TRACE_CASE=<file>|<k>    -> only case k of fixture <file>; other fixture files yield nothing
_CASE = os.environ.get("TRACE_CASE")


def _wrap_loader(orig):
    def loader(filename, *args, **kwargs):
        if _CASE is None:
            yield from orig(filename, *args, **kwargs)
            return
        if _CASE == "BASE":
            return
        f, k = _CASE.rsplit("|", 1)
        if filename != f:
            return
        for i, item in enumerate(orig(filename, *args, **kwargs)):
            if i == int(k):
                yield item

    loader.__name__ = orig.__name__
    return loader


def _audit(event, args):
    if event == "open" and _phase in _opened:
        p = args[0]
        if isinstance(p, bytes):
            p = os.fsdecode(p)
        if isinstance(p, str):
            p = os.path.abspath(p)
            if p.startswith(ROOT):
                _opened[_phase].add(p[len(ROOT):])


_started_in_import: set = set()  # (file, first line, qualname) of code that RAN during an import


def _on_start(code, offset):
    # module bodies run during collection ("import" phase) or lazily (inside _importing)
    if _importing or _phase == "import":
        f = code.co_filename
        if f.startswith(ROOT):
            _started_in_import.add((f[len(ROOT):], code.co_firstlineno, code.co_qualname))
        return None
    return mon.DISABLE


mon.use_tool_id(TOOL, "blast-trace")
mon.register_callback(TOOL, mon.events.LINE, _on_line)
mon.register_callback(TOOL, mon.events.PY_START, _on_start)
mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)
sys.addaudithook(_audit)

_outcome = {}

# --- run process pools inline, so work done in "pool workers" is traced in this process -------
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "selector"))
from runner import install_inline_executor  # noqa: E402

install_inline_executor()

# --- record the classes each test instantiates (for class-level data changes) --------------------
import importlib.abc  # noqa: E402
import importlib.machinery  # noqa: E402

_types_seen: set = set()
_wrapped_classes: set = set()


def _wrap_init(cls):
    init = cls.__dict__.get("__init__")
    if init is None or not callable(init) or getattr(init, "_xero_wrapped", False):
        return

    def __init__(self, *args, __orig=init, **kwargs):
        if _phase == "run":
            _types_seen.add(type(self))
        return __orig(self, *args, **kwargs)

    __init__._xero_wrapped = True
    __init__.__wrapped__ = init
    try:
        cls.__init__ = __init__
        _wrapped_classes.add(cls)
    except (TypeError, AttributeError):
        pass


def _wrap_module(module):
    name = module.__name__
    stack = [v for v in list(vars(module).values()) if isinstance(v, type) and v.__module__ == name]
    seen = set()
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        _wrap_init(cls)
        for v in list(vars(cls).values()):
            if isinstance(v, type) and v.__module__ == name:
                stack.append(v)


class _WrapFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        is_sqlglot = fullname == "sqlglot" or fullname.startswith("sqlglot.")
        is_tests = fullname == "tests" or fullname.startswith("tests.")
        if not (is_sqlglot or is_tests):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None or not hasattr(spec.loader, "exec_module"):
            return spec
        orig = spec.loader.exec_module

        def exec_module(module, _orig=orig, _sq=is_sqlglot, _name=fullname):
            global _importing
            _importing += 1
            mon.restart_events()  # so lines already seen elsewhere are reported again inside imports
            try:
                _orig(module)
            finally:
                _importing -= 1
            try:
                if _sq:
                    _wrap_module(module)
                elif _name == "tests.helpers":
                    for fn in ("load_sql_fixture_pairs", "load_sql_fixtures"):
                        if hasattr(module, fn):
                            setattr(module, fn, _wrap_loader(getattr(module, fn)))
            except Exception:  # noqa: BLE001
                pass

        spec.loader.exec_module = exec_module
        return spec


sys.meta_path.insert(0, _WrapFinder())


def _qual(c):
    return f"{c.__module__}.{c.__qualname__}"


def _tracked_classes():
    """Every loaded sqlglot class whose instances would pass through a wrapped __init__."""
    out = set()
    for mname, mod in list(sys.modules.items()):
        if not (mname == "sqlglot" or mname.startswith("sqlglot.")) or mod is None:
            continue
        stack = [v for v in list(vars(mod).values()) if isinstance(v, type)]
        seen = set()
        while stack:
            cls = stack.pop()
            if cls in seen:
                continue
            seen.add(cls)
            if not cls.__module__.startswith("sqlglot"):
                continue
            if any(b in _wrapped_classes for b in cls.__mro__):
                out.add(_qual(cls))
            for v in list(vars(cls).values()):
                if isinstance(v, type):
                    stack.append(v)
    return sorted(out)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global _phase
    _phase = "run"
    mon.restart_events()
    yield
    _phase = "done"


def pytest_runtest_logreport(report):
    if report.failed:
        _outcome[report.nodeid] = "failed"
    else:
        _outcome.setdefault(report.nodeid, "passed" if report.passed else "skipped")


def pytest_sessionfinish(session, exitstatus):
    global _phase
    _phase = "done"
    mon.set_events(TOOL, 0)
    if not OUT:
        return

    def pack(s):
        by_file = {}
        for f, ln in s:
            by_file.setdefault(f, []).append(ln)
        return {f: sorted(v) for f, v in sorted(by_file.items())}

    with open(OUT, "w") as fh:
        json.dump(
            {
                "outcome": _outcome,
                "run": pack(_lines["run"]),
                "import": pack(_lines["import"]),
                "opened_run": sorted(_opened["run"]),
                "opened_import": sorted(_opened["import"]),
                "import_exec": pack(_lines["import_exec"]),
                "started_in_import": sorted([list(x) for x in _started_in_import]),
                "case": _CASE,
                "types": sorted({_qual(c) for t in _types_seen for c in t.__mro__ if c.__module__.startswith("sqlglot")}),
                "tracked": _tracked_classes(),
            },
            fh,
        )
