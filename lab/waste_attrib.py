"""Attribute simulated waste on labeled mutants to the tests we list but that do not fail."""
import collections, json, os, sys
sys.path.insert(0, sys.argv[1] + "/selector")
os.environ["SQLGLOT_SRC"] = "/data/eftakhar/blast/sqlglot"
os.environ["BLAST_MAPS"] = sys.argv[1] + "/maps/coverage.json.gz"
import main as M
M.SRC = "/data/eftakhar/blast/sqlglot"
tests = json.load(open(sys.argv[1] + "/lab/sample/tests.json"))["tests"]
ids = [t["id"] for t in tests]; idx = {t: i for i, t in enumerate(ids)}
ms = [round(t["seconds"] * 1000) for t in tests]
maps = M.Maps(M.MAPS, ids)
cpu = M._load_cpu_costs(ids, ms); pf = M._load_pfail(ids)
def cost(sel):
    return M._pack_cost(M.split_workers(sorted(sel), ms), ms) / 1000 if sel else 0.0
waste_by_test = collections.Counter(); n_listed_nonfail = collections.Counter()
tot_ours = tot_orc = 0.0; per_kind = collections.Counter()
labdir, mutdir = sys.argv[2], sys.argv[3]
kinds = json.load(open(os.path.join(mutdir, "kinds.json")))
for f in sorted(os.listdir(labdir)):
    d = json.load(open(os.path.join(labdir, f)))
    if "results" not in d: continue
    cid = f[:-5]
    fail = {idx[k] for k, v in d["results"].items() if v == "failed" and k in idx}
    p = M.plan_commit(cid, open(os.path.join(mutdir, cid + ".patch")).read(), maps, tests)
    if p.fallback:
        listed, verify = set(p.candidates), set()
    else:
        M.plan_partials(p, maps, cpu)
        verify, listed = M.choose_verify(p.candidates, ms, M._Costs(cpu, p.cost_override), pf)
    out = listed | (verify & fail)
    c, o = cost(out), cost(fail)
    tot_ours += c; tot_orc += o; per_kind[kinds[cid].split(":")[0]] += c - o
    extra = out - fail
    if extra and c > o:
        # attribute the excess to the most expensive extra tests (simple: by duration share)
        s = sum(ms[n] for n in extra) or 1
        for n in extra:
            waste_by_test[n] += (c - o) * ms[n] / s
            n_listed_nonfail[n] += 1
print(f"selection-only: ours {tot_ours:.0f}s oracle {tot_orc:.0f}s waste {tot_ours-tot_orc:.0f}s")
print("by kind:", {k: round(v) for k, v in per_kind.items()})
for n, w in waste_by_test.most_common(15):
    print(f"{w:7.1f}s  listed-not-failing x{n_listed_nonfail[n]:3d}  {tests[n]['seconds']:6.2f}s  {ids[n]}")
