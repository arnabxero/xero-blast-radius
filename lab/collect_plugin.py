"""pytest plugin: record pass/fail per test node id (subtest failures and errors count as failures)."""
import json
import os

results: dict[str, str] = {}
collect_errors: list[str] = []


def pytest_runtest_logreport(report):
    nid = report.nodeid
    if report.failed:
        results[nid] = "failed"
    elif nid not in results:
        results[nid] = "passed" if report.passed else "skipped"


def pytest_collectreport(report):
    if report.failed:
        collect_errors.append(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("LABEL_OUT")
    if out:
        with open(out, "w") as f:
            json.dump({"results": results, "collect_errors": collect_errors, "exit": int(exitstatus)}, f)
