"""Build maps/imports.json.gz from import_trace.py output.

usage: python3 build_imports.py <src> <attr.json> <collect.json> <out.json.gz>

modules/lines: line -> modules whose import executes it (import_trace.py attr)
collect:       test file -> modules imported when pytest collects it
names:         identifier -> modules whose import executes a statement that mentions it. A patch
               that removes or renames a module-level binding (an import, def, class or constant)
               breaks exactly the modules whose import-time code refers to that name.
"""

import ast
import gzip
import io
import json
import os
import sys
import tokenize

SRC, ATTR, COLLECT, OUT = sys.argv[1:5]
a = json.load(open(ATTR))
c = json.load(open(COLLECT))


def simple_spans(text):
    """line -> (start, end) of the innermost simple statement (compound statements: header only)."""
    tree = ast.parse(text)
    best = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        s = node.lineno
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
            e = max(s, body[0].lineno - 1)  # header only
        else:
            e = node.end_lineno or s
        for ln in range(s, e + 1):
            cur = best.get(ln)
            if cur is None or (e - s) < (cur[1] - cur[0]):
                best[ln] = (s, e)
    return best


names = {}
for f, fl in a["lines"].items():
    path = os.path.join(SRC, f)
    try:
        text = open(path, encoding="utf-8").read()
        spans = simple_spans(text)
    except (OSError, SyntaxError, UnicodeDecodeError):
        continue
    src_lines = text.split("\n")
    toks_by_line = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.NAME:
                for ln in range(tok.start[0], tok.end[0] + 1):
                    toks_by_line.setdefault(ln, set()).add(tok.string)
    except (tokenize.TokenError, IndentationError):
        continue
    for ln_s, mods in fl.items():
        ln = int(ln_s)
        s, e = spans.get(ln, (ln, ln))
        for l2 in range(s, min(e, len(src_lines)) + 1):
            for name in toks_by_line.get(l2, ()):
                names.setdefault(name, set()).update(mods)

out = {"modules": a["modules"], "lines": a["lines"], "collect": c,
       "names": {k: sorted(v) for k, v in names.items()}}
with gzip.open(OUT, "wt") as fh:
    json.dump(out, fh, separators=(",", ":"))
print(f"names {len(names)}; size {os.path.getsize(OUT)}")
