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
_lines = {"import": set(), "run": set()}
_opened = {"import": set(), "run": set()}


def _on_line(code, line):
    f = code.co_filename
    if f.startswith(ROOT):
        bucket = _lines.get(_phase)
        if bucket is not None:
            bucket.add((f[len(ROOT):], line))
    return mon.DISABLE


def _audit(event, args):
    if event == "open" and _phase in _opened:
        p = args[0]
        if isinstance(p, bytes):
            p = os.fsdecode(p)
        if isinstance(p, str):
            p = os.path.abspath(p)
            if p.startswith(ROOT):
                _opened[_phase].add(p[len(ROOT):])


mon.use_tool_id(TOOL, "blast-trace")
mon.register_callback(TOOL, mon.events.LINE, _on_line)
mon.set_events(TOOL, mon.events.LINE)
sys.addaudithook(_audit)

_outcome = {}


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
            },
            fh,
        )
