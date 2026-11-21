#!/bin/bash
# Label every sample commit with real pytest (serial inside each container), 6 containers at once.
cd "$(dirname "$0")"
SRC="$(cd ../work/sqlglot_lf && pwd -W)"; LAB="$(pwd -W)"
ls sample/commits/*.patch | xargs -P 6 -I{} bash -c 'c=$(basename {} .patch); MSYS_NO_PATHCONV=1 docker run --rm --platform linux/amd64 --cpus 2 -v "'"$SRC"':/src:ro" -v "'"$LAB"':/lab" blast-lab sh /lab/label.sh /lab/sample/commits/$c.patch /lab/out/samples_lf/$c.json 0 && echo done $c'
