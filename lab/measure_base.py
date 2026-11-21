"""CPU of each case-driven slow test with NO cases (its fixed setup + case-independent part)."""
import gzip, json, os, resource, sys, time
sys.path.insert(0, "/app/selector")
os.chdir("/opt/sqlglot"); sys.path.insert(0, "/opt/sqlglot")
from runner import install_inline_executor, run_tests
install_inline_executor()
import sqlglot  # noqa
d = json.load(gzip.open("/app/maps/coverage.json.gz", "rt"))
tests = d["tests"]
files = {}
for u in d["cases"]["units"]:
    if u[1] != "BASE":
        files.setdefault(u[0], set()).add(u[1])
out = {}
for n, fs in sorted(files.items()):
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    pid = os.fork()
    if pid == 0:
        failed = run_tests("/opt/sqlglot", [tests[n]], {}, {tests[n]: 600}, case_filter={f: [] for f in fs})
        os._exit(1 if failed else 0)
    _, st = os.waitpid(pid, 0)
    r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (r1.ru_utime + r1.ru_stime) - (r0.ru_utime + r0.ru_stime)
    out[tests[n]] = round(cpu, 3)
    print(f"{n} base_cpu={cpu:.3f} {'FAIL' if st else 'ok'} {tests[n]}", flush=True)
json.dump(out, open("/out/base_cost.json", "w"), indent=0)
