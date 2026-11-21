"""Unified-diff parsing and in-memory application against the pristine sqlglot tree."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[str] = field(default_factory=list)  # each starts with ' ', '-', '+' (or '\\')


@dataclass
class FilePatch:
    old_path: str | None  # None for new files
    new_path: str | None  # None for deleted files
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False
    rename: bool = False
    mode_change: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def is_new(self) -> bool:
        return self.old_path is None

    @property
    def is_deleted(self) -> bool:
        return self.new_path is None


def _strip_prefix(p: str) -> str | None:
    p = p.strip()
    if p == "/dev/null":
        return None
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p[:2] in ("a/", "b/"):
        p = p[2:]
    return p


def parse_patch(text: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    hunk: Hunk | None = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r"^diff --git (\S+) (\S+)$", line)
            a, b = (m.group(1), m.group(2)) if m else (None, None)
            cur = FilePatch(old_path=_strip_prefix(a) if a else None, new_path=_strip_prefix(b) if b else None)
            files.append(cur)
            hunk = None
        elif cur is not None and hunk is None and line.startswith("new file mode"):
            cur.old_path = None
        elif cur is not None and hunk is None and line.startswith("deleted file mode"):
            cur.new_path = None
        elif cur is not None and hunk is None and line.startswith("rename from "):
            cur.old_path = line[len("rename from "):].strip()
            cur.rename = True
        elif cur is not None and hunk is None and line.startswith("rename to "):
            cur.new_path = line[len("rename to "):].strip()
            cur.rename = True
        elif cur is not None and hunk is None and (line.startswith("old mode") or line.startswith("new mode")):
            cur.mode_change = True
        elif cur is not None and (line.startswith("Binary files") or line.startswith("GIT binary patch")):
            cur.binary = True
        elif cur is not None and line.startswith("--- ") and (hunk is None or _hunk_done(hunk)):
            p = _strip_prefix(line[4:].split("\t")[0])
            cur.old_path = p
            hunk = None
        elif cur is not None and line.startswith("+++ ") and (hunk is None or _hunk_done(hunk)):
            p = _strip_prefix(line[4:].split("\t")[0])
            cur.new_path = p
            hunk = None
        elif cur is not None and line.startswith("@@"):
            m = _HUNK.match(line)
            if m:
                hunk = Hunk(
                    int(m.group(1)),
                    int(m.group(2)) if m.group(2) is not None else 1,
                    int(m.group(3)),
                    int(m.group(4)) if m.group(4) is not None else 1,
                )
                cur.hunks.append(hunk)
        elif hunk is not None and line[:1] in (" ", "-", "+", "\\") and not _hunk_done(hunk):
            hunk.lines.append(line)
        elif hunk is not None and line == "" and not _hunk_done(hunk):
            # a context line that lost its leading space (rare); treat as empty context
            hunk.lines.append(" ")
        i += 1
    return files


def _hunk_done(h: Hunk) -> bool:
    old = sum(1 for l in h.lines if l[:1] in (" ", "-"))
    new = sum(1 for l in h.lines if l[:1] in (" ", "+"))
    return old >= h.old_len and new >= h.new_len


class PatchError(Exception):
    pass


def apply_file(old_text: str | None, fp: FilePatch) -> str | None:
    """Return the new file text (None if deleted)."""
    if fp.is_deleted:
        return None
    old_lines = [] if old_text is None else old_text.split("\n")
    # keep track of trailing newline: split gives '' as the last element when text ends with \n
    out: list[str] = []
    pos = 0  # index into old_lines
    no_newline_new = False
    for h in fp.hunks:
        old_seg = [l[1:] for l in h.lines if l[:1] in (" ", "-")]
        new_seg = [l[1:] for l in h.lines if l[:1] in (" ", "+")]
        start = h.old_start - 1 if h.old_len > 0 else h.old_start  # 0-based index of first old line
        idx = _find(old_lines, old_seg, start, pos)
        if idx is None:
            raise PatchError(f"hunk does not apply to {fp.path} at {h.old_start}")
        out.extend(old_lines[pos:idx])
        out.extend(new_seg)
        pos = idx + len(old_seg)
        # "\ No newline at end of file" after a '+' line means new file lacks trailing newline
        for j, l in enumerate(h.lines):
            if l.startswith("\\") and j > 0 and h.lines[j - 1][:1] == "+":
                no_newline_new = True
    out.extend(old_lines[pos:])
    text = "\n".join(out)
    if no_newline_new and text.endswith("\n"):
        text = text[:-1]
    return text


def _find(lines: list[str], seg: list[str], start: int, lo: int) -> int | None:
    if not seg:
        return max(start, lo)
    n = len(seg)
    for off in range(0, 200):
        for cand in ((start + off, start - off) if off else (start,)):
            if cand < lo or cand + n > len(lines):
                continue
            if lines[cand : cand + n] == seg:
                return cand
    return None


def changed_old_lines(fp: FilePatch) -> tuple[set[int], set[int]]:
    """Old-file line numbers that were removed/replaced, and old-file 'gap' lines around pure insertions.

    Returns (touched, neighbors): touched = old lines deleted or modified; neighbors = for each
    insertion point, the old line just before and just after it.
    """
    touched: set[int] = set()
    neighbors: set[int] = set()
    for h in fp.hunks:
        o = h.old_start if h.old_len > 0 else h.old_start + 1
        prev_old = o - 1
        pending_insert = False
        for l in h.lines:
            tag = l[:1]
            if tag == " ":
                if pending_insert:
                    neighbors.add(o)
                    pending_insert = False
                prev_old = o
                o += 1
            elif tag == "-":
                touched.add(o)
                prev_old = o
                o += 1
            elif tag == "+":
                if not pending_insert:
                    neighbors.add(prev_old)
                pending_insert = True
        if pending_insert:
            neighbors.add(o)
    neighbors -= touched
    return touched, {n for n in neighbors if n > 0}


def old_to_new_line_map(fp: FilePatch):
    """Function mapping an old line number (unchanged line) to its new line number."""
    shifts = []  # (old_line_threshold, delta) applied in order
    delta = 0
    marks = []
    for h in fp.hunks:
        o, n = h.old_start, h.new_start
        if h.old_len == 0:
            o += 1
        if h.new_len == 0:
            n += 1
        for l in h.lines:
            tag = l[:1]
            if tag == " ":
                marks.append((o, n))
                o += 1
                n += 1
            elif tag == "-":
                o += 1
            elif tag == "+":
                n += 1
        marks.append((o, n))  # first old line after the hunk maps here
    marks.sort()

    def f(old_line: int) -> int:
        best = None
        for mo, mn in marks:
            if mo <= old_line:
                best = (mo, mn)
            else:
                break
        if best is None:
            return old_line
        return best[1] + (old_line - best[0])

    return f
