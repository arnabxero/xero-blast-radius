"""Measure the CPU each slow-ish test costs inside our runner (pristine code), in a forked child."""
import json, os, resource, sys, time
sys.path.insert(0, "/app/selector")
os.chdir("/opt/sqlglot"); sys.path.insert(0, "/opt/sqlglot")
from runner import install_inline_executor, run_tests
install_inline_executor()
import sqlglot  # noqa
tests = json.load(open("/data/tests.json"))["tests"]
sel = [t for t in tests if t["seconds"] >= 0.3]
for t in sorted(sel, key=lambda t: -t["seconds"]):
    r0 = resource.getrusage(resource.RUSAGE_CHILDREN); w0 = time.time()
    pid = os.fork()
    if pid == 0:
        failed = run_tests("/opt/sqlglot", [t["id"]], {}, {t["id"]: 600})
        os._exit(1 if failed else 0)
    _, st = os.waitpid(pid, 0)
    r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (r1.ru_utime + r1.ru_stime) - (r0.ru_utime + r0.ru_stime)
    print(f"{t['number']:5d} rec={t['seconds']:7.3f} cpu={cpu:7.3f} wall={time.time()-w0:6.2f} {'FAIL' if st else 'ok'} {t['id']}", flush=True)
