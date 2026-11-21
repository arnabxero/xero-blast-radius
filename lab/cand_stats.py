"""For labeled mutants: how often is each test a candidate, and how often does it then fail?"""
import collections, json, os, sys
sys.path.insert(0, "/data/eftakhar/blast/repo/selector")
os.environ["SQLGLOT_SRC"] = "/data/eftakhar/blast/sqlglot"
os.environ["BLAST_MAPS"] = "/data/eftakhar/blast/repo/maps/coverage.json.gz"
import main as M
M.SRC = "/data/eftakhar/blast/sqlglot"
tests = json.load(open("/data/eftakhar/blast/repo/lab/sample/tests.json"))["tests"]
ids = [t["id"] for t in tests]
idx = {t: i for i, t in enumerate(ids)}
maps = M.Maps(M.MAPS, ids)
cand = collections.Counter(); fail = collections.Counter(); total = 0
for f in sorted(os.listdir("/data/eftakhar/blast/labels1")):
    cid = f[:-5]
    d = json.load(open(f"/data/eftakhar/blast/labels1/{f}"))
    if "results" not in d:
        continue
    failing = {idx[k] for k, v in d["results"].items() if v == "failed" and k in idx}
    text = open(f"/data/eftakhar/blast/mut1/{cid}.patch").read()
    p = M.plan_commit(cid, text, maps, tests)
    total += 1
    for n in p.candidates:
        cand[n] += 1
        fail[n] += n in failing
rows = sorted(((t["seconds"], t["number"]) for t in tests if t["seconds"] >= 0.3), reverse=True)
print("mutants", total)
for s, n in rows[:25]:
    c = cand[n]
    print(f"{n:5d} {s:7.2f}s cand={c:3d} fail={fail[n]:3d} p={fail[n]/c if c else 0:.2f} {ids[n].split('::')[-1]}")
allc = sum(cand.values()); allf = sum(fail.values())
print("overall candidate->fail rate", round(allf / max(1, allc), 3))

# smoothed per-test failure probability when selected as a candidate (a prior for run-vs-list)
pf = {}
for n, c in cand.items():
    pf[ids[n]] = round((fail[n] + 0.5) / (c + 5), 4)
json.dump({"default": round((allf + 1) / (allc + 20), 4), "tests": pf},
          open("/data/eftakhar/blast/repo/maps/pfail.json", "w"), indent=0, sort_keys=True)
print("pfail.json written", len(pf))
