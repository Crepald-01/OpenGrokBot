"""Build a Windows .exe with PyInstaller.

    python build_exe.py            # folder build: dist/OpenGrokBot/OpenGrokBot.exe  (recommended, starts fastest)
    python build_exe.py --onefile  # single dist/OpenGrokBot.exe

The browser engine (Chromium, ~150 MB) is not bundled. The app offers to download it on first run, or run:
    OpenGrokBot.exe --install-browsers
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEP = ";" if os.name == "nt" else ":"


def make_icon() -> Path:
    """Render the app icon to assets/icon.ico (Qt can write .ico)."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(ROOT))
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication.instance() or QGuiApplication([])
    from ui.theme import app_icon
    out = ROOT / "assets" / "icon.ico"
    out.parent.mkdir(exist_ok=True)
    ok = app_icon(256).pixmap(256, 256).save(str(out), "ICO")
    if not ok:
        print("warning: could not write icon.ico; building without a custom icon")
    del app
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--onefile", action="store_true", help="build a single self-extracting exe (slower to start)")
    ap.add_argument("--console", action="store_true", help="keep a console window (useful for debugging)")
    ap.add_argument("--clean", action="store_true", help="remove build/ and dist/ first")
    args = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed. Run: pip install pyinstaller")
        return 1
    if args.clean:
        shutil.rmtree(ROOT / "build", ignore_errors=True)
        shutil.rmtree(ROOT / "dist", ignore_errors=True)

    icon = make_icon()
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--name", "OpenGrokBot", "--onefile" if args.onefile else "--onedir"]
    if not args.console:
        cmd.append("--windowed")
    if icon.exists():
        cmd += ["--icon", str(icon)]
    for folder in ("web", "skills", "plugins", "assets"):
        if (ROOT / folder).is_dir():
            cmd += ["--add-data", f"{ROOT / folder}{SEP}{folder}"]
    # (mcp is NOT collected wholesale: mcp.cli needs typer. Its client modules are found by import analysis + the hints below.)
    for mod in ("mcp", "mcp.client.stdio", "mcp.client.sse", "mcp.client.streamable_http", "mcp.client.session"):
        cmd += ["--hidden-import", mod]
    cmd += ["--exclude-module", "mcp.cli"]
    for pkg in ("playwright", "certifi", "anthropic", "openai", "fastapi", "starlette", "pydantic", "pydantic_core", "keyring", "apscheduler", "tzlocal", "qrcode"):
        cmd += ["--collect-all", pkg]
    for mod in ("uvicorn", "uvicorn.logging", "uvicorn.loops", "uvicorn.protocols", "uvicorn.lifespan", "keyring.backends", "keyring.backends.Windows", "multipart",
                "httpx", "anyio", "h11", "sqlite3"):
        cmd += ["--collect-submodules", mod]
    cmd += ["--hidden-import", "win32ctypes.pywin32.pywintypes", "--hidden-import", "win32ctypes.pywin32.win32cred"]
    for meta in ("keyring", "mcp", "fastapi", "uvicorn", "anthropic", "openai", "httpx"):
        cmd += ["--copy-metadata", meta]
    cmd += ["--exclude-module", "tkinter", "--exclude-module", "pytest"]
    cmd.append(str(ROOT / "main.py"))
    print(" ".join(f'"{c}"' if " " in c else c for c in cmd))
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc == 0:
        exe = ROOT / "dist" / ("OpenGrokBot.exe" if args.onefile else "OpenGrokBot/OpenGrokBot.exe")
        print(f"\nBuilt: {exe}\nFirst run: it offers to install the Chromium engine. Or run:  \"{exe}\" --install-browsers")
    return rc


if __name__ == "__main__":
    sys.exit(main())
