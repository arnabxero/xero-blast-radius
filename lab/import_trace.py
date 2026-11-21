"""Which module's import executes each line, and which modules each test file imports at collection.

Run inside the lab image with the pristine tree at /src:
    python import_trace.py attr <out.json>       # line -> modules whose import (module body) ran it
    python import_trace.py collect <out.json>    # test file -> modules imported when it is collected

`attr`: every sqlglot module and every test module is imported once in a fresh interpreter while
sys.monitoring records LINE events; each event is credited to the innermost module body on the
stack (a function called from a class body at import time is credited to that module). A patch that
changes such a line changes what importing that module does, so the selector re-imports exactly
those modules under the patch to see whether collection or import now fails.

`collect`: one fresh interpreter per test file (pytest collection = importing the file).
"""

import json
import os
import pkgutil
import subprocess
import sys

SRC = os.environ.get("SRC", "/src")


def test_files():
    out = []
    for root, _dirs, files in os.walk(os.path.join(SRC, "tests")):
        for f in files:
            if f.startswith("test_") and f.endswith(".py"):
                out.append(os.path.relpath(os.path.join(root, f), SRC).replace(os.sep, "/"))
    return sorted(out)


def modname(rel):
    m = rel[:-3].replace("/", ".")
    return m[: -len(".__init__")] if m.endswith(".__init__") else m


def attr(out_path):
    sys.path.insert(0, SRC)
    os.chdir(SRC)
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    prefix = SRC.rstrip("/") + "/"
    mon = sys.monitoring
    tool = mon.PROFILER_ID
    mon.use_tool_id(tool, "xero_import_trace")
    seen = {}  # (file, line) -> set(module)
    get = sys._getframe

    def owner():
        f = get(2)
        while f is not None:
            c = f.f_code
            if c.co_name == "<module>" and c.co_filename.startswith(prefix):
                return modname(os.path.relpath(c.co_filename, SRC).replace(os.sep, "/"))
            f = f.f_back
        return None

    def on_line(code, line):
        fn = code.co_filename
        if not fn.startswith(prefix):
            return mon.DISABLE
        m = owner()
        if m is not None:
            seen.setdefault((fn[len(prefix):], line), set()).add(m)

    mon.register_callback(tool, mon.events.LINE, on_line)
    mon.set_events(tool, mon.events.LINE)
    import importlib

    failed = {}
    order = [modname(f) for f in test_files()]
    import sqlglot  # noqa: F401

    for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot.", onerror=lambda n: None):
        if not info.name.endswith("__main__"):
            order.append(info.name)
    for m in order:
        try:
            importlib.import_module(m)
        except BaseException as e:  # noqa: BLE001
            failed[m] = repr(e)[:200]
    mon.set_events(tool, 0)
    mon.free_tool_id(tool)
    mods = sorted({m for v in seen.values() for m in v})
    mid = {m: i for i, m in enumerate(mods)}
    lines = {}
    for (f, ln), ms in seen.items():
        lines.setdefault(f, {})[str(ln)] = sorted(mid[m] for m in ms)
    json.dump({"modules": mods, "lines": lines, "failed": failed}, open(out_path, "w"))
    print(f"modules {len(mods)} files {len(lines)} line-entries {len(seen)} failed {failed}")


def collect(out_path):
    res = {}
    code = ("import os, sys, json, importlib; sys.path.insert(0, %r); os.chdir(%r); "
            "os.environ.setdefault('SKIP_INTEGRATION','0'); importlib.import_module(sys.argv[1]); "
            "print(json.dumps(sorted(m for m in sys.modules if m=='sqlglot' or m.startswith(('sqlglot.','tests.')))))")
    for f in test_files():
        r = subprocess.run([sys.executable, "-c", code % (SRC, SRC), modname(f)], capture_output=True, text=True)
        if r.returncode != 0:
            res[f] = {"error": r.stderr[-300:]}
            continue
        res[f] = json.loads(r.stdout.strip().splitlines()[-1])
    json.dump(res, open(out_path, "w"))
    print(f"test files {len(res)}; errors {[f for f, v in res.items() if isinstance(v, dict)]}")


if __name__ == "__main__":
    {"attr": attr, "collect": collect}[sys.argv[1]](sys.argv[2])
