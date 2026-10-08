"""Drives the Settings > App "Updates" card offscreen with fake /api/updates states and saves screenshots.

    python tests/ui_settings_update201.py [screenshot-folder]
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-upd201-")
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("C:/gbt/shots201s")
OUT.mkdir(parents=True, exist_ok=True)

from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

from ui import theme  # noqa: E402
from ui.api import Api, Connection  # noqa: E402
from ui.pages_settings import SettingsPage  # noqa: E402
from ui.store import Store  # noqa: E402

app = QApplication(sys.argv)
app.setStyleSheet(theme.qss())
api = Api(Connection("local", "http://127.0.0.1:1", "t"))   # nothing listens here: no real request is made
store = Store(api)
page = SettingsPage(api, store)

calls: list[tuple[str, str]] = []
api.post = lambda path, body=None, ok=None, err=None, **kw: calls.append(("POST", path))  # type: ignore[assignment]
api.get = lambda path, ok=None, err=None, **kw: calls.append(("GET", path))  # type: ignore[assignment]

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def pump(sec: float = 0.2) -> None:
    import time
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


host = QWidget()
host.setStyleSheet(theme.qss())
hl = QVBoxLayout(host)
hl.setContentsMargins(16, 16, 16, 16)
host.setFixedWidth(700)
hl.addWidget(page.u_card)   # the card on its own, so the screenshot shows just it
host.show()


def shot(name: str) -> None:
    pump(0.4)
    host.adjustSize()
    host.grab().save(str(OUT / f"{name}.png"))


MB = 1024 * 1024
BASE = {"enabled": True, "current": "2.0.1", "checked_at": 1, "error": "", "notes": "", "url": "https://github.com/x/y/releases/tag/v2.0.2"}
IDLE_NEWER = {**BASE, "latest": "2.0.2", "newer": True, "name": "OpenGrokBot 2.0.2", "size": 45 * MB, "can_install": True,
              "download": {"status": "idle", "got": 0, "total": 0, "error": "", "version": "", "path": ""}}
UP_TO_DATE = {**BASE, "latest": "2.0.1", "newer": False, "name": "", "size": 0, "can_install": True,
              "download": {"status": "idle", "got": 0, "total": 0, "error": "", "version": "", "path": ""}}
DOWNLOADING = {**IDLE_NEWER, "download": {"status": "downloading", "got": 40 * MB, "total": 100 * MB, "error": "", "version": "2.0.2", "path": ""}}
READY = {**IDLE_NEWER, "download": {"status": "ready", "got": 45 * MB, "total": 45 * MB, "error": "", "version": "2.0.2", "path": "x"}}
ERROR = {**IDLE_NEWER, "download": {"status": "error", "got": 45 * MB, "total": 45 * MB, "error": "checksum mismatch: the file is not the one published", "version": "2.0.2", "path": ""}}
NO_INSTALL = {**IDLE_NEWER, "can_install": False}
CHECK_FAILED = {**BASE, "latest": "", "newer": False, "error": "GitHub is unreachable", "can_install": False,
                "download": {"status": "idle", "got": 0, "total": 0, "error": "", "version": "", "path": ""}}


def texts() -> str:
    return " | ".join([page.u_title.text(), page.u_status.text(), page.u_prog.text(), page.u_error.text()])


# ---- 1. up to date
page.show_update_state(UP_TO_DATE)
check(page.u_status.text() == "You are up to date (2.0.1).", f"up to date: {page.u_status.text()!r}")
check(not (not page.u_update.isHidden()) and not (not page.u_release.isHidden()), "up to date: no update button")
check(page.u_check.isEnabled() and page.u_check.text() == "Check for updates", "the Check for updates button is there")
check(not (not page.u_bar.isHidden()) and not (not page.u_error.isHidden()), "up to date: no progress or error line")
check(page.u_title.text() == "Installed: OpenGrokBot 2.0.1", f"the current version is shown ({page.u_title.text()!r})")
shot("upd-1-uptodate")

# ---- 2. newer, can install
page.show_update_state(IDLE_NEWER)
check(page.u_status.text().startswith("2.0.2 is available (you have 2.0.1)"), f"newer: {page.u_status.text()!r}")
check("45.0 MB" in page.u_status.text(), "newer: the download size is shown")
check((not page.u_update.isHidden()) and page.u_update.text() == "Update now" and page.u_update.isEnabled(), "newer: primary Update now is shown")
check(not (not page.u_release.isHidden()), "newer and installable: no release button")
check(not page._upd_poll.isActive(), "idle: nothing is polled")
shot("upd-2-newer")

# ---- 3. downloading 40%
page.show_update_state(DOWNLOADING)
check((not page.u_bar.isHidden()) and page.u_bar.maximum() == 100 * MB and page.u_bar.value() == 40 * MB, f"downloading: bar at 40% ({page.u_bar.value()}/{page.u_bar.maximum()})")
check(page.u_prog.text() == "Downloading… 40.0 MB of 100.0 MB", f"downloading: text {page.u_prog.text()!r}")
check(not page.u_update.isEnabled() and not page.u_check.isEnabled(), "downloading: buttons are disabled")
check(page._upd_poll.isActive() and page._upd_poll.interval() == 700, "downloading: polled every 700 ms")
shot("upd-3-downloading")
page._upd_poll.stop()

# ---- 4. ready: the install starts by itself, once, and the app is asked to quit
quits: list[bool] = []
page.quitForUpdate.connect(lambda: quits.append(True))
calls.clear()
page._want_install = True           # what "Update now" sets before the download is posted
page.show_update_state(READY)
check(calls == [("POST", "/api/updates/install")], f"ready after Update now: the install is posted once ({calls})")
check(quits == [], "the app does not quit before the installer answers")
check(page.u_status.text() == "Installing 2.0.2…", f"installing: {page.u_status.text()!r}")
page.show_update_state(READY)       # a second poll must not post the install again
check(calls.count(("POST", "/api/updates/install")) == 1, "the install is not posted twice")
page._install_ok({"ok": True})
check(quits == [True], "after the install answers ok: quitForUpdate is emitted once")
shot("upd-4-installing")

# ---- 5. ready but not asked for (for example after a restart): no automatic install, offer the button
ready_again = {**READY}
page._installing = False
page._want_install = False
calls.clear()
page.show_update_state(ready_again)
check(calls == [], "ready without a request: nothing is installed by itself")
check("Click Update now to install it" in page.u_prog.text(), f"ready: asks the user ({page.u_prog.text()!r})")

# ---- 6. error from the download: red line, retry
page.show_update_state(ERROR)
check((not page.u_error.isHidden()) and "checksum mismatch" in page.u_error.text(), f"error: shown in the error line ({page.u_error.text()!r})")
check("color" in page.u_error.styleSheet(), "error: the line is coloured")
check(page.u_update.text() == "Try again" and page.u_update.isEnabled(), "error: the button becomes Try again")
shot("upd-5-error")

# retry sends a new download request
calls.clear()
page.start_update()
check(calls == [("POST", "/api/updates/download")], f"retry posts the download again ({calls})")

# ---- 7. newer but this build cannot install itself: release page button
page.show_update_state(NO_INSTALL)
check((not page.u_release.isHidden()) and page.u_release.text() == "Open release page", "can_install false: Open release page is shown")
check(not (not page.u_update.isHidden()), "can_install false: no Update now")
shot("upd-6-release")

# ---- 8. check failed
page.show_update_state(CHECK_FAILED)
check(page.u_status.text() == "Could not check: GitHub is unreachable", f"check failed: {page.u_status.text()!r}")
check("warn" in page.u_status.styleSheet() or "color" in page.u_status.styleSheet(), "check failed: the status is coloured as a warning")
shot("upd-7-check-failed")

# ---- 9. the install itself failed: error line and Try again
page._installing = True
page._install_fail("the installer did not start")
check((not page.u_error.isHidden()) and "Could not install the update: the installer did not start" in page.u_error.text(), "install failure: error line")
check(page.u_update.text() == "Try again", "install failure: Try again")

print(texts())
print("FAIL: " + "; ".join(fails) if fails else "ALL OK")
sys.stdout.flush()
os._exit(1 if fails else 0)
