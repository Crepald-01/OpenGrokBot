"""Build the Windows installer (OpenGrokBot-Setup-<version>.exe).

    python build_installer.py              # build the app folder with PyInstaller, then compile the installer
    python build_installer.py --skip-build # reuse an existing dist/OpenGrokBot folder

Needs Inno Setup 6 (free):  winget install JRSoftware.InnoSetup
Output: dist/OpenGrokBot-Setup-<version>.exe

Tip: PyInstaller can fail on very long folder paths. If the build complains about paths, run this from a short
folder such as C:\\gbb.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def find_iscc() -> str | None:
    found = shutil.which("ISCC") or shutil.which("iscc")
    if found:
        return found
    roots = [os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")]
    for base in roots:
        if not base:
            continue
        for sub in ("Programs\\Inno Setup 6", "Inno Setup 6"):
            p = Path(base) / sub / "ISCC.exe"
            if p.exists():
                return str(p)
    return None


def version() -> str:
    m = re.search(r'VERSION\s*=\s*"([^"]+)"', (ROOT / "core" / "__init__.py").read_text(encoding="utf-8"))
    return m.group(1) if m else "1.0.0"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-build", action="store_true", help="reuse the existing dist/OpenGrokBot folder")
    args = ap.parse_args()

    iscc = find_iscc()
    if not iscc:
        print("Inno Setup 6 was not found. Install it with:  winget install JRSoftware.InnoSetup")
        return 1
    if not args.skip_build:
        r = subprocess.run([sys.executable, str(ROOT / "build_exe.py")], cwd=ROOT)
        if r.returncode:
            return r.returncode
    exe = ROOT / "dist" / "OpenGrokBot" / "OpenGrokBot.exe"
    if not exe.exists():
        print(f"Missing {exe}. Run without --skip-build first.")
        return 1
    v = version()
    r = subprocess.run([iscc, f"/DAppVersion={v}", str(ROOT / "installer" / "OpenGrokBot.iss")], cwd=ROOT)
    if r.returncode:
        return r.returncode
    out = ROOT / "dist" / f"OpenGrokBot-Setup-{v}.exe"
    print(f"\nInstaller: {out}  ({out.stat().st_size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
