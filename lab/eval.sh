#!/bin/bash
# Build the entry and score it on the sample set exactly like `make sample` (scorer flags), plus CPU.
set -e
cd "$(dirname "$0")/.."
IMAGE=blast-entry
docker build --platform linux/amd64 -q -t $IMAGE . >/dev/null
S="$(cd lab/sample && pwd -W)"
MSYS_NO_PATHCONV=1 docker run --rm --platform linux/amd64 --network none --cpus 2 \
  --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  --volume "$S/commits:/data/commits:ro" --volume "$S/tests.json:/data/tests.json:ro" \
  $IMAGE > lab/out/output.jsonl 2> lab/out/stderr.log
python ../kits/blast/kit/score.py lab/sample lab/sample/answers.json lab/out/output.jsonl
grep -E "^total|planned" lab/out/stderr.log
