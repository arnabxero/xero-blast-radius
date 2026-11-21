#!/bin/bash
# On the server: build the entry and score it on the sample set with the scorer's container flags.
cd /data/eftakhar/blast/repo
sg docker -c "docker build -q -t blast-entry ." >/dev/null || exit 1
sg docker -c "docker run --rm --network none --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
  --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
  -v $PWD/lab/sample/commits:/data/commits:ro -v $PWD/lab/sample/tests.json:/data/tests.json:ro blast-entry" > lab/out/output.jsonl 2> lab/out/stderr.log
python3 /data/eftakhar/blast/score.py lab/sample lab/sample/answers.json lab/out/output.jsonl | head -6
grep -E "^total" lab/out/stderr.log
