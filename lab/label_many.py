"""Label many patches with real pytest (inside the blast-lab container), in parallel.

usage: python label_many.py <patch_dir> <out_dir> <parallel> [first_cpu]
Each patch: copy the pristine tree (/src), git apply, run the full suite serially with
SKIP_INTEGRATION=0 like the organizers, record failing node ids via collect_plugin.
Each worker is pinned to 2 CPUs so spawn-based pools in the tests stay small.
"""

import json
import multiprocessing as mp
import os
import shutil
import subprocess
import sys
import tempfile

PATCH_DIR, OUT, PAR = sys.argv[1], sys.argv[2], int(sys.argv[3])
FIRST_CPU = int(sys.argv[4]) if len(sys.argv) > 4 else 0


def work(cid):
    out = os.path.join(OUT, cid + ".json")
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return cid, "cached"
    slot = mp.current_process()._identity[0] - 1
    cpus = {FIRST_CPU + 2 * slot, FIRST_CPU + 2 * slot + 1}
    d = tempfile.mkdtemp(prefix="lab_", dir="/tmp")
    try:
        shutil.copytree("/src", d, dirs_exist_ok=True)
        r = subprocess.run(["git", "apply", "--whitespace=nowarn", os.path.join(PATCH_DIR, cid + ".patch")],
                           cwd=d, capture_output=True, text=True)
        if r.returncode != 0:
            with open(out, "w") as fh:
                json.dump({"apply_failed": r.stderr[-500:]}, fh)
            return cid, "apply_failed"
        env = dict(os.environ, PYTHONPATH=f"{d}:/repo/lab", LABEL_OUT=out, SKIP_INTEGRATION="0",
                   OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
        try:
            subprocess.run([sys.executable, "-m", "pytest", "tests", "-p", "collect_plugin", "-q",
                            "-p", "no:cacheprovider", "--timeout=300"],
                           cwd=d, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           preexec_fn=lambda: os.sched_setaffinity(0, cpus), timeout=2400)
        except subprocess.TimeoutExpired:
            with open(out, "w") as fh:
                json.dump({"timeout": True}, fh)
        return cid, "ok"
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    cids = sorted(f[:-6] for f in os.listdir(PATCH_DIR) if f.endswith(".patch"))
    done = 0
    with mp.Pool(PAR) as pool:
        for cid, status in pool.imap_unordered(work, cids):
            done += 1
            if done % 25 == 0 or status != "ok":
                print(done, cid, status, flush=True)
    print("finished", done, flush=True)
