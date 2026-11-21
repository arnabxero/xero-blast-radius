#!/bin/sh
# usage (inside a throwaway blast-lab container): label.sh <patch-or-empty> <out.json> [workers]
P="$1"; OUT="$2"; N="${3:-8}"
D=$(mktemp -d /tmp/s.XXXX)
cp -r /src/. "$D/" && cd "$D" || exit 1
if [ -n "$P" ]; then git apply --whitespace=nowarn "$P" 2>/dev/null || patch -p1 -s < "$P"; fi
export PYTHONPATH="$D:/lab" LABEL_OUT="$OUT"
python -m pytest tests -p collect_plugin -n "$N" -q -p no:cacheprovider --timeout=600 >/dev/null 2>&1
exit 0
