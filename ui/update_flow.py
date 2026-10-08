"""One-click updates for the desktop UI: polls GET /api/updates, runs the download, starts the installer and asks the window to quit.

The service does the work (core/updates.py). This controller only decides when to ask and what the screens should show.
Every screen (Home card, sidebar row, toast) renders from the dict emitted on `changed`, so a test can drive them with a fake state.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from .api import Api, load_ui_config, save_ui_config

FIRST_MS = 4_000        # first look shortly after start
FAST_MS = 700           # while a download runs
SLOW_MS = 600_000       # every 10 minutes (the service checks GitHub itself every six hours)
RETRY_MS = 60_000       # after a poll that could not reach the service


class UpdateController(QObject):
    changed = Signal(dict)       # the service state plus: installing, skipped, action_error
    quitForUpdate = Signal()     # the installer is running: the main window quits so it can replace the app

    def __init__(self, api: Api, parent: QObject | None = None):
        super().__init__(parent)
        self.api = api
        self.state: dict = {}
        self.installing = False
        self.last_error = ""
        self._want_install = False     # set by update_now(): install as soon as this download is ready
        self._busy = False             # one GET in flight at a time
        self._halted = False           # stop(): no more polling, callbacks still work (tests)
        self._dead = False             # dispose(): the window is gone, ignore late answers
        self._refresh_next = False
        self._dismissed = load_ui_config().get("dismissed_update", "")
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._tick)
        self._timer.start(FIRST_MS)

    # ------------------------------------------------------------------ polling
    def stop(self) -> None:
        self._halted = True
        self._timer.stop()

    def dispose(self) -> None:
        self.stop()
        self._dead = True

    def check_now(self) -> None:
        """A fresh check against GitHub (the Settings card and Home may ask for one)."""
        self._poll(refresh=True)

    def poll(self) -> None:
        """Re-read what the service already knows (no call to GitHub)."""
        self._poll(refresh=False)

    def _arm(self, ms: int, refresh: bool) -> None:
        if self._halted or self._dead:
            return
        self._timer.start(ms)
        self._refresh_next = refresh

    def _tick(self) -> None:
        self._poll(refresh=self._refresh_next)

    def _poll(self, refresh: bool) -> None:
        if self._halted or self._dead or self._busy:
            return
        self._busy = True
        # a poll error is silent on purpose: a service that is down must not toast every 700 ms
        self.api.get("/api/updates", self._on_poll, self._on_poll_error, params={"refresh": "true"} if refresh else None)

    def _on_poll(self, d: dict) -> None:
        self._busy = False
        if self._dead:
            return
        self._apply(d)
        downloading = self._downloading()
        self._arm(FAST_MS if downloading else SLOW_MS, refresh=False)   # the service itself checks GitHub every six hours

    def _on_poll_error(self, _msg: str = "") -> None:
        self._busy = False
        if self._dead:
            return
        self._arm(FAST_MS if self._downloading() else RETRY_MS, refresh=False)

    def _downloading(self) -> bool:
        return (self.state.get("download") or {}).get("status") == "downloading"

    # ------------------------------------------------------------------ actions
    def update_now(self) -> None:
        """Download (if needed), then install and let the window quit. Without in-app install, open the release page."""
        st = self.state
        if not st.get("newer") or self.installing:
            return
        if not st.get("can_install"):
            if st.get("url"):
                QDesktopServices.openUrl(QUrl(st["url"]))
            return
        self.last_error = ""
        self._want_install = True
        dl = st.get("download") or {}
        if dl.get("status") == "downloading":
            self._emit()                       # it installs by itself when the running download is ready
            return
        if dl.get("status") == "ready" and dl.get("version") == st.get("latest"):
            self._apply(st)                    # already downloaded: straight to the installer
            return
        self.api.post("/api/updates/download", None, self._on_download, self._download_failed)
        self._emit()

    def skip(self, version: str | None = None) -> None:
        """Hide the sidebar item, the Home card and the toast for this version. The Settings card still shows it."""
        v = version or self.state.get("latest") or ""
        if not v:
            return
        self._dismissed = v
        cfg = load_ui_config()
        cfg["dismissed_update"] = v
        save_ui_config(cfg)
        self._emit()

    def is_skipped(self, state: dict | None = None) -> bool:
        latest = (state or self.state).get("latest") or ""
        return bool(latest) and latest == self._dismissed

    # ------------------------------------------------------------------ callbacks
    def _on_download(self, d: dict) -> None:
        if self._dead:
            return
        self._apply(d)
        if self._downloading():
            self._arm(FAST_MS, refresh=False)

    def _download_failed(self, msg: str) -> None:
        if self._dead:
            return
        self._want_install = False
        self.last_error = "Could not download the update: " + msg
        self._emit()

    def _install(self) -> None:
        self.installing = True
        self.api.post("/api/updates/install", None, self._on_installed, self._install_failed)

    def _on_installed(self, d: dict) -> None:
        if self._dead:
            return
        if (d or {}).get("ok") is False:
            self._install_failed("the installer did not start")
            return
        self.quitForUpdate.emit()          # the installer closes and replaces the app, then relaunches it
        self._emit()

    def _install_failed(self, msg: str) -> None:
        if self._dead:
            return
        self.installing = False
        self.last_error = "Could not install the update: " + msg
        self._emit()

    # ------------------------------------------------------------------ state out
    def _apply(self, d: dict) -> None:
        self.state = dict(d or {})
        dl = self.state.get("download") or {}
        if self._want_install and not self.installing and dl.get("status") == "ready" and dl.get("version") == self.state.get("latest"):
            self._want_install = False
            self._install()
        elif dl.get("status") == "error":
            self._want_install = False
        self._emit()

    def _emit(self) -> None:
        if self._dead:
            return
        dl = self.state.get("download") or {}
        err = self.last_error
        if not err and dl.get("status") == "error":
            err = "Download failed: " + (dl.get("error") or "unknown error")
        self.changed.emit({**self.state, "installing": self.installing, "skipped": self.is_skipped(), "action_error": err})
