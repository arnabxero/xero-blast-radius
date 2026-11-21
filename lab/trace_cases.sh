#!/bin/sh
# Inside a throwaway blast-lab container: per-case traces of case-driven slow tests.
# usage: trace_cases.sh <case_targets.txt> <outdir> <parallel>
TARGETS="$1"; OUTDIR="$2"; PAR="${3:-64}"
mkdir -p /tmp/x "$OUTDIR"
cp -r /src/. /tmp/x/
cd /tmp/x || exit 1
export PYTHONPATH="/tmp/x:/repo/lab" TRACE_ROOT=/tmp/x SKIP_INTEGRATION=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
python - "$TARGETS" "$OUTDIR" > /tmp/jobs.txt <<'PY'
import json, sys
sys.path.insert(0, "/tmp/x")
from tests.helpers import load_sql_fixture_pairs
jobs, seen_base = [], set()
for line in open(sys.argv[1]):
    num, nid, f = line.split()
    if num not in seen_base:
        seen_base.add(num); jobs.append((num, nid, "BASE"))
    n = sum(1 for _ in load_sql_fixture_pairs(f))
    jobs.extend((num, nid, f"{f}|{k}") for k in range(n))
json.dump([list(j) for j in jobs], open(sys.argv[2] + "/jobs.json", "w"))
for i, (num, nid, case) in enumerate(jobs):
    print(i, nid, case)
PY
wc -l /tmp/jobs.txt
cat /tmp/jobs.txt | xargs -P "$PAR" -L 1 sh -c 'test -s "'"$OUTDIR"'/$0.json" || TRACE_CASE="$2" TRACE_OUT="'"$OUTDIR"'/$0.json" python -m pytest -q -p no:cacheprovider -p trace_plugin --timeout=900 "$1" >/dev/null 2>&1; echo $0' | awk 'NR%500==0{print "traced", NR; fflush()}'
echo finished
