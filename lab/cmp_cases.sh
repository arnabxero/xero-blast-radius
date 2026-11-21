#!/bin/sh
# Inside blast-lab: comparison-equivalence data for the two TPC-DS tests, one process per case.
mkdir -p /tmp/x /repo/lab/out/cmpcase && cp -r /src/. /tmp/x/ && cd /tmp/x || exit 1
export PYTHONPATH="/tmp/x:/repo/lab" TRACE_ROOT=/tmp/x SKIP_INTEGRATION=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
F=optimizer/tpc-ds/tpc-ds.sql
N=$(python -c "import sys; sys.path.insert(0,'/tmp/x'); from tests.helpers import load_sql_fixture_pairs as l; print(sum(1 for _ in l('$F')))")
{
  for t in tests/test_executor.py::TestExecutor::test_execute_tpcds tests/test_optimizer.py::TestOptimizer::test_tpcds; do
    echo "$t BASE"
    k=0; while [ $k -lt $N ]; do echo "$t $F|$k"; k=$((k+1)); done
  done
} > /tmp/jobs.txt
wc -l /tmp/jobs.txt
cat /tmp/jobs.txt | xargs -P 64 -L 1 sh -c 'CMP_CASE="$1" CMP_OUT=/repo/lab/out/cmpcase/cmp python -m pytest -q -p no:cacheprovider -p cmp_plugin "$0" 2>&1 | tail -1' | sort | uniq -c
echo finished
