"""Build step: download sqlglot at the pinned commit (allowed source) and unpack it to argv[1]."""
import io
import os
import sys
import tarfile
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://github.com/tobymao/sqlglot/archive/{COMMIT}.tar.gz"

dest = sys.argv[1]
data = urllib.request.urlopen(URL, timeout=120).read()
with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
    members = []
    for m in tar.getmembers():
        parts = m.name.split("/", 1)
        if len(parts) < 2 or not parts[1]:
            continue
        m.name = parts[1]
        members.append(m)
    tar.extractall(dest, members=members, filter="data")
assert os.path.exists(os.path.join(dest, "sqlglot", "parser.py")), "sqlglot not unpacked"
print(f"sqlglot {COMMIT} unpacked to {dest}")
