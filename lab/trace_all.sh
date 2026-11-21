#!/bin/sh
# Inside a throwaway blast-lab container: trace every test in its own process.
# usage: trace_all.sh <tests.json> <outdir> <parallel>
TESTS="$1"; OUTDIR="$2"; PAR="${3:-16}"
mkdir -p /tmp/x "$OUTDIR"
cp -r /src/. /tmp/x/
cd /tmp/x || exit 1
export PYTHONPATH="/tmp/x:/lab" TRACE_ROOT=/tmp/x SKIP_INTEGRATION=0
python - "$TESTS" > /tmp/ids.txt <<'EOF'
import json, sys
for t in json.load(open(sys.argv[1]))["tests"]:
    print(t["number"], t["id"])
EOF
# each line: "<number> <nodeid>"
cat /tmp/ids.txt | xargs -P "$PAR" -L 1 sh -c 'test -s "'"$OUTDIR"'/$0.json" || TRACE_OUT="'"$OUTDIR"'/$0.json" python -m pytest -q -p no:cacheprovider -p trace_plugin --timeout=900 "$1" >/dev/null 2>&1; echo $0' | awk 'NR%100==0{print "traced", NR; fflush()}'
echo finished
