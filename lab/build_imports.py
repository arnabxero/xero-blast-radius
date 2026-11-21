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

# ---- class attributes read only at run time ---------------------------------------------------
# For every data attribute assigned in a sqlglot class body: the statements (any file under
# sqlglot/ or tests/) that read it, by name, attribute or string literal. Attributes that some
# import-time statement reads (metaclasses, __init_subclass__, subclass bodies such as
# `X = {**Base.X}`) or that are dispatched by name pattern (`*_sql`) are left out: a change to
# their value can reach tests through derived state, so the selector keeps the whole-class rule.
IDENT = __import__("re").compile(r"[A-Za-z_][A-Za-z0-9_]*$")
HEADER_FIELDS = ("test", "iter", "target", "items", "subject", "decorator_list", "bases", "keywords",
                 "args", "returns", "type_params")


def stmt_reads(node):
    """Names read by one statement (compound statements: their header only)."""
    parts = []
    if hasattr(node, "body") and isinstance(node.body, list):
        for f in HEADER_FIELDS:
            v = getattr(node, f, None)
            if v is None:
                continue
            parts.extend(v if isinstance(v, list) else [v])
    else:
        parts = [node]
    out = set()
    for p in parts:
        if not isinstance(p, ast.AST):
            continue
        for n in ast.walk(p):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                out.add(n.id)
            elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
                out.add(n.attr)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and IDENT.match(n.value):
                out.add(n.value)
    return out


def header_span(node):
    s = node.lineno
    body = getattr(node, "body", None)
    if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
        return s, max(s, body[0].lineno - 1)
    return s, node.end_lineno or s


py_files = []
for top in ("sqlglot", "tests"):
    for root, _d, fs in os.walk(os.path.join(SRC, top)):
        for f in fs:
            if f.endswith(".py"):
                py_files.append(os.path.relpath(os.path.join(root, f), SRC).replace(os.sep, "/"))
trees = {}
for rel in sorted(py_files):
    try:
        trees[rel] = ast.parse(open(os.path.join(SRC, rel), encoding="utf-8").read())
    except (SyntaxError, UnicodeDecodeError):
        pass

cattrs = set()
for rel, tree in trees.items():
    if not rel.startswith("sqlglot/"):
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for st in node.body:
                tg = st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, ast.AnnAssign) else []
                for t in tg:
                    if isinstance(t, ast.Name):
                        cattrs.add(t.id)

reads = {}       # attr -> {file: [[s, e], ...]}
imp_read = set()
for rel, tree in trees.items():
    imp_lines = {int(x) for x in a["lines"].get(rel, {})}
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        r = stmt_reads(node) & cattrs
        if not r:
            continue
        s, e = header_span(node)
        at_import = any(ln in imp_lines for ln in range(s, e + 1))
        for name in r:
            reads.setdefault(name, {}).setdefault(rel, []).append([s, e])
            if at_import:
                imp_read.add(name)
safe = {n for n in cattrs - imp_read if not n.endswith("_sql") and not n.startswith("__")}
cattr_reads = {n: reads.get(n, {}) for n in sorted(safe)}
print(f"class attrs {len(cattrs)}; read at import {len(imp_read & cattrs)}; runtime-only {len(safe)}")

# ---- modules that run the SQL tokenizer/parser while being imported ---------------------------
# (pre-parsed constants in generator class bodies, DataType.from_str in typing modules, parser
# helpers). Any change to data those code paths consume (expression arg_types, tokenizer slots,
# keyword tables, ...) can make these imports fail, so every probe re-imports them too.
def body_spans(tree):
    out = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((n.body[0].lineno, max(getattr(x, "end_lineno", x.lineno) for x in n.body)))
    return out


def own_mod(rel):
    m = rel[:-3].replace("/", ".")
    return m[: -len(".__init__")] if m.endswith(".__init__") else m


sql_at_import = set()
for f, fl in a["lines"].items():
    if not (f in ("sqlglot/parser.py", "sqlglot/tokenizer_core.py") or f.startswith("sqlglot/parsers/")):
        continue
    spans = body_spans(trees[f]) if f in trees else []
    for ln, ms in fl.items():
        if any(s0 <= int(ln) <= e0 for s0, e0 in spans):
            sql_at_import |= {a["modules"][i] for i in ms if a["modules"][i] != own_mod(f)}
print(f"modules running the parser at import: {sorted(sql_at_import)}")

out = {"modules": a["modules"], "lines": a["lines"], "collect": c,
       "names": {k: sorted(v) for k, v in names.items()}, "cattr_reads": cattr_reads,
       "sql_at_import": sorted(sql_at_import)}
with gzip.open(OUT, "wt") as fh:
    json.dump(out, fh, separators=(",", ":"))
print(f"names {len(names)}; size {os.path.getsize(OUT)}")
