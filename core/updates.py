"""Tells you when a newer release is out, and can fetch and run its installer for you. One plain GET to the public
GitHub releases API every few hours, nothing about you is sent, and it can be switched off in Settings > App.
Nothing is downloaded until you press Update, and the installer is only run if its SHA-256 matches the one GitHub lists."""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import VERSION

REPO = "Crepald-01/OpenGrokBot"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
INTERVAL = 6 * 3600
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"


def parse(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", (v or "").split("-")[0])[:4]) or (0,)


def is_newer(latest: str, current: str) -> bool:
    a, b = parse(latest), parse(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))   # "1.6" and "1.6.0" are the same version


def _fetch() -> dict:
    import httpx
    r = httpx.get(API, headers={"Accept": "application/vnd.github+json", "User-Agent": "opengrokbot-update-check"}, timeout=8.0, follow_redirects=True)
    r.raise_for_status()
    return r.json()


class UpdateError(Exception):
    pass


def _stream(url: str, dest: Path, progress: Callable[[int, int], None]) -> str:
    """Download `url` into `dest`, reporting (bytes so far, total); returns the SHA-256 of what was written."""
    import httpx
    h = hashlib.sha256()
    with httpx.stream("GET", url, headers={"User-Agent": "opengrokbot-updater"}, timeout=30.0, follow_redirects=True) as r:
        r.raise_for_status()
        total, got = int(r.headers.get("content-length") or 0), 0
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(256 * 1024):
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                progress(got, total)
    return h.hexdigest()


def _launch(installer: Path) -> None:
    """Start the installer so that it outlives this process. `cmd /c start` makes it an orphan: the installer stops the
    app and the service with `taskkill /T`, which would otherwise take down its own parent."""
    args = f'"{installer}" /SILENT /NORESTART /SUPPRESSMSGBOXES /CLOSEAPPLICATIONS /RELAUNCH'
    subprocess.Popen(f'cmd /c start "" {args}', shell=True, close_fds=True, cwd=str(installer.parent),
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class Updates:
    def __init__(self, settings: Any, current: str = VERSION, fetch: Callable[[], dict] | None = None, folder: Path | None = None,
                 download: Callable[[str, Path, Callable[[int, int], None]], str] | None = None, launch: Callable[[Path], None] | None = None):
        self.settings, self.current, self._fetch = settings, current, fetch or _fetch
        self.folder, self._download, self._launch = folder, download or _stream, launch or _launch
        self._lock = threading.Lock()
        self._dl: dict = {"status": "idle", "got": 0, "total": 0, "error": "", "version": "", "path": ""}

    def enabled(self) -> bool:
        return bool((self.settings.get("updates", {}) or {}).get("check", True))

    def state(self) -> dict:
        st = self.settings.get("updates_state", {}) or {}
        latest = st.get("latest", "")
        asset = st.get("asset") or {}
        newer = bool(latest) and is_newer(latest, self.current)
        return {"enabled": self.enabled(), "current": self.current, "latest": latest, "newer": newer,
                "url": st.get("url", ""), "name": st.get("name", ""), "checked_at": st.get("checked_at", 0), "error": st.get("error", ""),
                "notes": st.get("notes", ""), "size": int(asset.get("size") or 0),
                "can_install": newer and sys.platform == "win32" and bool(asset.get("url") and asset.get("sha256")),
                "download": dict(self._dl)}

    def check(self, force: bool = False) -> dict:
        st = self.settings.get("updates_state", {}) or {}
        if not force and (not self.enabled() or time.time() - float(st.get("checked_at", 0) or 0) < INTERVAL):
            return self.state()
        try:
            rel = self._fetch()
            tag = str(rel.get("tag_name", "")).lstrip("v")
            self.settings.set("updates_state", {"checked_at": time.time(), "latest": tag, "url": rel.get("html_url", ""), "name": rel.get("name", ""), "error": "",
                                                "notes": str(rel.get("body") or "")[:1500], "asset": _pick_asset(rel)})
        except Exception as e:  # noqa: BLE001  (offline, rate-limited...: remember the time so we do not retry in a loop)
            self.settings.set("updates_state", {**st, "checked_at": time.time(), "error": str(e)[:200]})
        return self.state()

    # ---------------------------------------------------------------- download and install
    def start_download(self) -> dict:
        """Fetch the installer for the newest release in the background; `state()["download"]` reports progress."""
        st = self.state()
        asset = (self.settings.get("updates_state", {}) or {}).get("asset") or {}
        if not st["newer"]:
            raise UpdateError("You already have the newest version.")
        if not st["can_install"]:
            raise UpdateError("This release has no installer that can be checked automatically. Use View release instead.")
        if not str(asset["url"]).startswith(DOWNLOAD_PREFIX):
            raise UpdateError("The installer is not hosted where it is expected, so it will not be downloaded.")
        with self._lock:
            if self._dl["status"] == "downloading":
                return self.state()
            if self._dl["status"] == "ready" and self._dl["version"] == st["latest"] and Path(self._dl["path"]).exists():
                return self.state()
            self._dl = {"status": "downloading", "got": 0, "total": int(asset.get("size") or 0), "error": "", "version": st["latest"], "path": ""}
        threading.Thread(target=self._run_download, args=(dict(asset), st["latest"]), daemon=True, name="update-download").start()
        return self.state()

    def _run_download(self, asset: dict, version: str) -> None:
        def progress(got: int, total: int) -> None:
            self._dl["got"], self._dl["total"] = got, total or self._dl["total"]
        try:
            folder = self.folder or Path.cwd()
            folder.mkdir(parents=True, exist_ok=True)
            for old in folder.glob("OpenGrokBot-Setup-*.exe*"):      # one installer at a time
                try:
                    old.unlink()
                except OSError:
                    pass
            name = re.sub(r"[^A-Za-z0-9._-]", "_", asset["name"]) or f"OpenGrokBot-Setup-{version}.exe"
            part, dest = folder / (name + ".part"), folder / name
            got = self._download(asset["url"], part, progress)
            if got.lower() != str(asset["sha256"]).lower():
                part.unlink(missing_ok=True)
                raise UpdateError("The downloaded file does not match the checksum GitHub lists for it, so it was thrown away.")
            part.replace(dest)
            self._dl.update({"status": "ready", "path": str(dest), "error": "", "got": dest.stat().st_size})
        except Exception as e:  # noqa: BLE001
            self._dl.update({"status": "error", "error": str(e)[:300] or e.__class__.__name__})

    def install(self) -> dict:
        """Run the downloaded, verified installer. The installer closes the app and the service, replaces the files and starts the app again."""
        with self._lock:
            d = dict(self._dl)
            if d["status"] != "ready" or not d["path"] or not Path(d["path"]).exists():
                raise UpdateError("Download the update first.")
            if not is_newer(d["version"], self.current):
                raise UpdateError("That update is not newer than this version.")
        self._launch(Path(d["path"]))
        return {"ok": True, "version": d["version"]}


def _pick_asset(rel: dict) -> dict:
    for a in rel.get("assets") or []:
        n = str(a.get("name", ""))
        if re.fullmatch(r"OpenGrokBot-Setup-[\d.]+\.exe", n):
            dg = str(a.get("digest") or "")
            return {"name": n, "url": a.get("browser_download_url", ""), "size": int(a.get("size") or 0), "sha256": dg.split(":", 1)[1] if dg.startswith("sha256:") else ""}
    return {}
