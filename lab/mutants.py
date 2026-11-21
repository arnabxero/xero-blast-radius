"""Generate synthetic patches against the pinned sqlglot tree, for evaluating the selector.

Used only to find blind spots (misses) and measure cost locally; nothing produced here ships
with the selector. Usage: python3 mutants.py <sqlglot_src> <out_dir> <count> <seed>
"""

from __future__ import annotations

import ast
import difflib
import json
import os
import random
import re
import sys

SRC, OUT, COUNT, SEED = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(SEED)


def read(rel):
    with open(os.path.join(SRC, rel), encoding="utf-8", newline="") as fh:
        return fh.read()


def diff(rel_old, old, rel_new, new):
    a = [] if old is None else old.splitlines(True)
    b = [] if new is None else new.splitlines(True)
    head = f"diff --git a/{rel_old or rel_new} b/{rel_new or rel_old}\n"
    if old is None:
        head += "new file mode 100644\n"
    if new is None:
        head += "deleted file mode 100644\n"
    body = "".join(difflib.unified_diff(a, b, f"a/{rel_old}" if old is not None else "/dev/null",
                                        f"b/{rel_new}" if new is not None else "/dev/null", n=3))
    if not body:
        return None
    # difflib omits the "\ No newline" markers; our files end with newlines so that is fine
    return head + "index 0000000..1111111 100644\n" * 0 + body


PY = []
for root, _, files in os.walk(os.path.join(SRC, "sqlglot")):
    for f in files:
        if f.endswith(".py"):
            PY.append(os.path.relpath(os.path.join(root, f), SRC).replace(os.sep, "/"))
PY.sort()
TESTPY = sorted(
    os.path.relpath(os.path.join(r, f), SRC).replace(os.sep, "/")
    for r, _, fs in os.walk(os.path.join(SRC, "tests")) for f in fs if f.endswith(".py")
)
FIX = sorted(
    os.path.relpath(os.path.join(r, f), SRC).replace(os.sep, "/")
    for r, _, fs in os.walk(os.path.join(SRC, "tests", "fixtures")) for f in fs if f.endswith(".sql")
)
SIZES = {p: len(read(p)) for p in PY}

SWAPS = [
    (" == ", " != "), (" != ", " == "), (" is not ", " is "), (" is None", " == None"),
    (" is ", " == "), (" < ", " <= "), (" <= ", " < "), (" > ", " >= "), (" >= ", " > "),
    (" and ", " or "), (" or ", " and "), ("True", "False"), ("False", "True"),
    (" not in ", " in "), (" in ", " not in "), ("+ 1", "- 1"), ("- 1", "+ 1"),
    ("seq_get(args, 0)", "seq_get(args, 1)"), ("seq_get(args, 1)", "seq_get(args, 0)"),
    ("seq_get(args, 1)", "seq_get(args, 2)"), (".upper()", ".lower()"), (".lower()", ".upper()"),
    ("return True", "return False"), ("return False", "return True"), ("if not ", "if "),
]
COMMENTS = ["# Cheap check first", "# Intentionally not cached", "# NOTE: keep in sync", "# fast path"]


def pick_py():
    files = [p for p in PY if SIZES[p] > 200]
    w = [SIZES[p] ** 0.5 for p in files]
    return rng.choices(files, w)[0]


def mut_code():
    for _ in range(50):
        rel = pick_py()
        text = read(rel)
        lines = text.split("\n")
        cands = []
        for i, l in enumerate(lines):
            s = l.strip()
            if not s or s.startswith("#") or s.startswith('"""') or s.startswith("from ") or s.startswith("import "):
                continue
            for a, b in SWAPS:
                if a in l:
                    cands.append((i, a, b))
            if re.search(r"\b\d+\b", l) and "=" in l:
                cands.append((i, "INT", None))
            if re.match(r'\s*"[A-Z_]+":\s', l):
                cands.append((i, "DEL", None))
        if not cands:
            continue
        i, a, b = rng.choice(cands)
        l = lines[i]
        if a == "INT":
            m = list(re.finditer(r"\b(\d+)\b", l))
            mm = rng.choice(m)
            l2 = l[: mm.start()] + str(int(mm.group(1)) + rng.choice([1, -1, 2])) + l[mm.end():]
            lines[i] = l2
        elif a == "DEL":
            if l.rstrip().endswith(","):
                del lines[i]
            else:
                continue
        else:
            idx = [m.start() for m in re.finditer(re.escape(a), l)]
            j = rng.choice(idx)
            lines[i] = l[:j] + b + l[j + len(a):]
        new = "\n".join(lines)
        if new == text:
            continue
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        return "code", diff(rel, text, rel, new)
    return None


def mut_comment():
    rel = pick_py()
    text = read(rel)
    lines = text.split("\n")
    for _ in range(50):
        i = rng.randrange(1, len(lines) - 1)
        if lines[i].strip() and not lines[i].rstrip().endswith("\\"):
            indent = re.match(r"\s*", lines[i]).group(0)
            lines.insert(i, indent + rng.choice(COMMENTS))
            new = "\n".join(lines)
            try:
                ast.parse(new)
            except SyntaxError:
                lines.pop(i)
                continue
            return "comment", diff(rel, text, rel, new)
    return None


def mut_fixture():
    rel = rng.choice([f for f in FIX if "tpc" not in f or rng.random() < 0.2])
    text = read(rel)
    lines = text.split("\n")
    idx = [i for i, l in enumerate(lines) if l.strip() and not l.startswith("--") and not l.startswith("#")]
    if not idx:
        return None
    i = rng.choice(idx)
    l = lines[i]
    op = rng.random()
    if op < 0.45:
        toks = list(re.finditer(r"[A-Za-z_][A-Za-z0-9_]*", l))
        if not toks:
            return None
        t = rng.choice(toks)
        l = l[: t.start()] + t.group(0) + "x" + l[t.end():]
    elif op < 0.65:
        j = rng.randrange(len(l))
        l = l[:j] + " " + l[j:]
    elif op < 0.8:
        lines.insert(i, "-- " + rng.choice(["note", "regression", "see issue"]))
        return "fixture", diff(rel, text, rel, "\n".join(lines))
    else:
        l = l.replace("SELECT", "SELECT DISTINCT", 1) if "SELECT" in l else l + " "
    lines[i] = l
    new = "\n".join(lines)
    return ("fixture", diff(rel, text, rel, new)) if new != text else None


def mut_testfile():
    rel = rng.choice([t for t in TESTPY if t.startswith("tests/") and "fixtures" not in t])
    text = read(rel)
    lines = text.split("\n")
    idx = [i for i, l in enumerate(lines) if ('"' in l or "'" in l) and ("validate" in l or "assert" in l or "SELECT" in l)]
    if not idx:
        return None
    i = rng.choice(idx)
    l = lines[i]
    q = [m.start() for m in re.finditer(r"[A-Za-z]", l)]
    if not q:
        return None
    j = rng.choice(q)
    lines[i] = l[:j] + l[j].swapcase() + l[j + 1:]
    new = "\n".join(lines)
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return "testfile", diff(rel, text, rel, new)


def mut_new_file():
    if rng.random() < 0.5:
        rel = f"sqlglot/xero_extra_{rng.randrange(1000)}.py"
        return "newfile", diff(None, None, rel, "def helper(x):\n    return x\n")
    rel = f"tests/fixtures/optimizer/xero_extra_{rng.randrange(1000)}.sql"
    return "newfile", diff(None, None, rel, "SELECT 1;\nSELECT 1 AS \"1\";\n")


def mut_delete_or_syntax():
    rel = pick_py()
    text = read(rel)
    if rng.random() < 0.5:
        lines = text.split("\n")
        i = rng.randrange(len(lines))
        lines.insert(i, "    )" if rng.random() < 0.5 else "def broken(:")
        return "syntax", diff(rel, text, rel, "\n".join(lines))
    # rename a module-level function (breaks its callers)
    names = re.findall(r"^def ([a-z_][a-z0-9_]*)\(", text, re.M)
    if not names:
        return None
    n = rng.choice(names)
    new = re.sub(rf"^def {n}\(", f"def {n}_renamed(", text, count=1, flags=re.M)
    return "rename_def", diff(rel, text, rel, new)


KINDS = [(mut_code, 50), (mut_fixture, 20), (mut_comment, 8), (mut_testfile, 8), (mut_new_file, 3),
         (mut_delete_or_syntax, 4)]
os.makedirs(OUT, exist_ok=True)
meta = {}
n = 0
while n < COUNT:
    if rng.random() < 0.1:  # multi-file commit
        parts = []
        kinds = []
        for _ in range(rng.choice([2, 3])):
            fn = rng.choices([k for k, _ in KINDS], [w for _, w in KINDS])[0]
            r = fn()
            if r and r[1]:
                kinds.append(r[0])
                parts.append(r[1])
        files = [re.search(r"^diff --git a/(\S+)", p).group(1) for p in parts]
        if len(parts) < 2 or len(set(files)) != len(files):
            continue
        kind, patch = "multi:" + "+".join(kinds), "".join(parts)
    else:
        fn = rng.choices([k for k, _ in KINDS], [w for _, w in KINDS])[0]
        r = fn()
        if not r or not r[1]:
            continue
        kind, patch = r
    cid = f"m{SEED}_{n:04d}"
    with open(os.path.join(OUT, cid + ".patch"), "w", encoding="utf-8", newline="") as fh:
        fh.write(patch)
    meta[cid] = kind
    n += 1
with open(os.path.join(OUT, "kinds.json"), "w") as fh:
    json.dump(meta, fh, indent=0)
print(f"wrote {n} mutants to {OUT}")
