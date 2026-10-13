"""Run the unit tests (tests/test_*.py) in parallel, one process per file, then the UI scripts one at a time.

    python scripts/run_tests.py          # unit tests, 4 processes
    python scripts/run_tests.py --ui     # also run tests/ui_*.py (they use fixed ports, so they run one by one)
    python scripts/run_tests.py -j 8

Each file gets its own temp folder and process, so they cannot disturb each other. The plain
`python -m unittest discover -s tests -p "test_*.py"` still works and runs everything in one process (slower)."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(path: Path, unit: bool) -> tuple[str, bool, float, str]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", QT_QPA_PLATFORM="offscreen")
    t = time.time()
    cmd = [sys.executable, "-m", "unittest", f"tests.{path.stem}"] if unit else [sys.executable, str(path)]
    p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    ok = p.returncode == 0   # the UI scripts exit 1 on any failed check
    return path.name, ok, time.time() - t, (p.stdout + p.stderr)[-1500:]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ui", action="store_true")
    ap.add_argument("-j", type=int, default=4)
    a = ap.parse_args()
    os.environ.setdefault("TEMP", tempfile.gettempdir())
    t0 = time.time()
    bad = 0
    files = sorted((ROOT / "tests").glob("test_*.py"))
    with ThreadPoolExecutor(max_workers=a.j) as ex:
        for name, ok, secs, tail in ex.map(lambda f: run(f, True), files):
            print(f"{'ok  ' if ok else 'FAIL'} {name:32s} {secs:5.1f}s")
            if not ok:
                bad += 1
                print(tail)
    if a.ui:
        for f in sorted((ROOT / "tests").glob("ui_*.py")):
            if f.name == "ui_smoke.py":
                continue
            name, ok, secs, tail = run(f, False)
            print(f"{'ok  ' if ok else 'FAIL'} {name:32s} {secs:5.1f}s")
            if not ok:
                bad += 1
                print(tail)
    print(f"\n{'ALL PASSED' if not bad else str(bad) + ' FAILED'} in {time.time() - t0:.0f}s")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
