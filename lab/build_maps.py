"""Turn per-test traces (lab/out/trace_lf/<n>.json) into the compact maps the selector ships with.

Output: maps/coverage.json.gz with
  tests:        list of test ids (index = test number)
  line_sets:    list of unique sorted test-number lists (deduplicated)
  lines:        {file: {line: set_index}}           run-phase line coverage
  mod_run:      {file: set_index}                   tests that ran any line of the file (run phase)
  mod_import:   {file: set_index}                   tests whose process executed the file during import/collection
  opened:       {path: set_index}                   non-.py/.pyc files opened by the test (run or import phase)
  outcome_ok:   bool per test that passed in the trace run (sanity)
"""

from __future__ import annotations

import gzip
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main(trace_dir: str, tests_json: str, out_path: str) -> None:
    tests = json.load(open(tests_json))["tests"]
    n = len(tests)
    set_index: dict[tuple, int] = {}
    sets: list[list[int]] = []

    def intern(nums) -> int:
        key = tuple(sorted(nums))
        idx = set_index.get(key)
        if idx is None:
            idx = len(sets)
            set_index[key] = idx
            sets.append(list(key))
        return idx

    lines: dict[str, dict[int, set[int]]] = {}
    mod_run: dict[str, set[int]] = {}
    mod_import: dict[str, set[int]] = {}
    opened: dict[str, set[int]] = {}
    class_tests: dict[str, set[int]] = {}
    tracked: set[str] = set()
    ok = []
    missing = []
    for t in tests:
        num = t["number"]
        p = os.path.join(trace_dir, f"{num}.json")
        if not os.path.exists(p) or os.path.getsize(p) == 0:
            missing.append(num)
            ok.append(False)
            continue
        try:
            d = json.load(open(p))
        except ValueError:
            missing.append(num)
            ok.append(False)
            continue
        ok.append(all(v == "passed" for v in d["outcome"].values()) and bool(d["outcome"]))
        for f, lns in d["run"].items():
            mod_run.setdefault(f, set()).add(num)
            fl = lines.setdefault(f, {})
            for ln in lns:
                fl.setdefault(ln, set()).add(num)
        for f in d["import"]:
            mod_import.setdefault(f, set()).add(num)
        for c in d.get("types", []):
            class_tests.setdefault(c, set()).add(num)
        tracked.update(d.get("tracked", []))
        for f in d["opened_run"] + d["opened_import"]:
            if f.endswith(".py") or f.endswith(".pyc") or "__pycache__" in f:
                continue
            opened.setdefault(f, set()).add(num)

    out = {
        "tests": [t["id"] for t in tests],
        "lines": {f: {str(ln): intern(s) for ln, s in sorted(fl.items())} for f, fl in sorted(lines.items())},
        "mod_run": {f: intern(s) for f, s in sorted(mod_run.items())},
        "mod_import": {f: intern(s) for f, s in sorted(mod_import.items())},
        "opened": {f: intern(s) for f, s in sorted(opened.items())},
        "outcome_ok": ok,
        "class_tests": {c: intern(s) for c, s in sorted(class_tests.items())},
        "tracked": sorted(tracked),
    }
    out["line_sets"] = sets
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with gzip.open(out_path, "wt", compresslevel=9) as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"tests={n} missing={len(missing)} not_ok={sum(1 for x in ok if not x)} files={len(lines)} "
          f"unique_sets={len(sets)} size={os.path.getsize(out_path)/1e6:.2f}MB", file=sys.stderr)
    if missing:
        print("missing:", missing[:30], file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
