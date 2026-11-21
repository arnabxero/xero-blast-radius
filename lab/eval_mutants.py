"""Run the built selector image on labeled mutants (batches like the final) and report misses/cost.

usage (server): python3 eval_mutants.py <mutant_dir> <label_dir> <work_dir> [batch=60] [image=blast-entry]
"""

import json
import os
import shutil
import subprocess
import sys

MUT, LAB, WORK = sys.argv[1], sys.argv[2], sys.argv[3]
BATCH = int(sys.argv[4]) if len(sys.argv) > 4 else 60
IMAGE = sys.argv[5] if len(sys.argv) > 5 else "blast-entry"
REPO = "/data/eftakhar/blast/repo"
tests = json.load(open(f"{REPO}/lab/sample/tests.json"))["tests"]
index = {t["id"]: t["number"] for t in tests}
ms = [round(t["seconds"] * 1000) for t in tests]
kinds = json.load(open(os.path.join(MUT, "kinds.json")))


def oracle(f):
    if not f:
        return 0
    best = None
    for k in range(1, 9):
        loads = [0] * k
        for n in sorted(f, key=lambda n: -ms[n]):
            i = loads.index(min(loads))
            loads[i] += ms[n]
        c = max(loads) + 2000 * k
        best = c if best is None else min(best, c)
    return best


labels = {}
for cid in kinds:
    p = os.path.join(LAB, cid + ".json")
    if not os.path.exists(p):
        continue
    d = json.load(open(p))
    if "results" not in d:
        continue
    failing = {index[k] for k, v in d["results"].items() if v == "failed" and k in index}
    for nid in d.get("collect_errors", []):  # a test file that cannot be imported fails all its tests
        failing |= {t["number"] for t in tests if t["file"] == nid or t["id"].startswith(nid + "::")}
    labels[cid] = failing

cids = sorted(labels)
os.makedirs(WORK, exist_ok=True)
outputs = {}
cpu_total = 0.0
for b in range(0, len(cids), BATCH):
    bd = os.path.join(WORK, f"batch{b // BATCH}")
    shutil.rmtree(bd, ignore_errors=True)
    os.makedirs(os.path.join(bd, "commits"))
    for cid in cids[b:b + BATCH]:
        shutil.copy(os.path.join(MUT, cid + ".patch"), os.path.join(bd, "commits"))
    shutil.copy(f"{REPO}/lab/sample/tests.json", os.path.join(bd, "tests.json"))
    cmd = (f"docker run --rm --network none --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 "
           f"--read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges "
           f"--user 65534:65534 --env HOME=/tmp -v {bd}/commits:/data/commits:ro -v {bd}/tests.json:/data/tests.json:ro {IMAGE}")
    r = subprocess.run(["sg", "docker", "-c", cmd], capture_output=True, text=True)
    open(os.path.join(bd, "stderr.log"), "w").write(r.stderr)
    for line in r.stdout.splitlines():
        d = json.loads(line)
        outputs[d["commit"]] = d["workers"]
    for line in r.stderr.splitlines():
        if line.startswith("total "):
            cpu_total += float(line.split("cpu ")[1].split("s")[0])

miss, sim, orc = [], 0, 0
by_kind = {}
for cid in cids:
    sel = {n for w in outputs.get(cid, []) for n in w}
    f = labels[cid]
    workers = outputs.get(cid)
    if workers is None:
        print("NO OUTPUT", cid)
        continue
    cost = (max([sum(ms[n] for n in w) for w in workers] or [0]) + 2000 * len([w for w in workers if w]))
    m = bool(f - sel)
    if m:
        miss.append(cid)
    sim += cost + (300000 if m else 0)
    orc += oracle(f)
    k = kinds[cid].split(":")[0]
    e = by_kind.setdefault(k, [0, 0, 0, 0])
    e[0] += 1
    e[1] += m
    e[2] += cost
    e[3] += oracle(f)
print(f"mutants={len(cids)} misses={len(miss)} simulated={sim/1000:.1f}s oracle={orc/1000:.1f}s cpu={cpu_total:.1f}s")
for k, (n, mm, c, o) in sorted(by_kind.items()):
    print(f"  {k:10s} n={n:3d} miss={mm} cost={c/1000:7.1f}s oracle={o/1000:7.1f}s waste={(c-o)/1000:6.1f}s")
for cid in miss:
    f = labels[cid]
    sel = {n for w in outputs[cid] for n in w}
    print("MISS", cid, kinds[cid], "missed:", [tests[n]["id"] for n in sorted(f - sel)][:5])
json.dump({"misses": miss}, open(os.path.join(WORK, "summary.json"), "w"))
