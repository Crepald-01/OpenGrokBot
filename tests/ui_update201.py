"""Drives the one-click update UI offscreen: the Home card (idle, downloading, installing, error), the sidebar row, the once-per-version
toast and tray message, Later, the download-then-install path and the quit that follows. The service is a real in-process one; the
update calls that would download or launch an installer are stubbed, so nothing is downloaded and nothing is installed.

    python tests/ui_update201.py [screenshot-folder]      (default C:/gbt/shots201)
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-u201-")
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("C:/gbt/shots201")
OUT.mkdir(parents=True, exist_ok=True)

import uvicorn  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core import agent as agent_mod  # noqa: E402
from core import secrets  # noqa: E402
from core.engine import Engine  # noqa: E402
from core.providers import LLMResult  # noqa: E402
from service.server import create_app  # noqa: E402

_store: dict = {}
secrets.get_secret = lambda n: _store.get(n)            # type: ignore[assignment]
secrets.set_secret = lambda n, v: _store.__setitem__(n, v)   # type: ignore[assignment]
secrets.has_secret = lambda n: n in _store              # type: ignore[assignment]
secrets.delete_secret = lambda n: _store.pop(n, None)   # type: ignore[assignment]


class Fake:
    vision = False
    model = "fake"
    profile = {"id": "fake", "label": "Fake"}

    def stream(self, system, messages, tools, on_text, should_stop, max_tokens=8192):
        r = LLMResult(text="ok")
        on_text(r.text)
        r.input_tokens, r.output_tokens = 10, 5
        return r


agent_mod.make_provider = lambda *a, **k: Fake()

eng = Engine()
eng.settings.set("notifications.toast", False)
eng.settings.set("memory.auto_reflect", False)
eng.settings.set("updates.check", False)     # no GitHub request from the test
eng.start()
PORT, TOKEN = 18773, "t"
threading.Thread(target=uvicorn.Server(uvicorn.Config(create_app(eng, TOKEN), host="127.0.0.1", port=PORT, log_level="warning")).run, daemon=True).start()
time.sleep(1.5)

from ui import theme, update_flow  # noqa: E402
from ui.api import Api, Connection, load_ui_config  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.store import Store  # noqa: E402

app = QApplication(sys.argv)
app.setStyleSheet(theme.qss())
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)   # type: ignore[assignment]
api = Api(Connection("local", f"http://127.0.0.1:{PORT}", TOKEN))
store = Store(api)
store.apply_bootstrap(api.call("GET", "/api/bootstrap"))
win = MainWindow(api, store, tray_available=False)
win.banner_shown = True
ctl = win.updates
ctl.stop()                                   # no polling: the test injects every state itself

toasts: list[str] = []   # every toast; update toasts are the ones with "is available"
tray_msgs: list[tuple] = []


class FakeTray:
    def showMessage(self, *a) -> None:
        tray_msgs.append(a)


class FakeAction:
    def setText(self, _t: str) -> None:
        pass


win.toast = lambda text, kind="info": toasts.append(text)     # type: ignore[assignment]
win.tray = FakeTray()                         # type: ignore[assignment]
win.tray_needs = FakeAction()                 # type: ignore[assignment]
quits: list[bool] = []
win.quitRequested.connect(lambda stop: quits.append(stop))
opened: list[str] = []


class FakeDesktop:
    @staticmethod
    def openUrl(url) -> None:
        opened.append(url.toString())


update_flow.QDesktopServices = FakeDesktop   # type: ignore[assignment]
MB = 1024 * 1024
fails: list[str] = []


def pump(sec: float) -> None:
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def check(cond: bool, msg: str) -> None:
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def shot(widget, name: str) -> None:
    pump(0.5)
    widget.grab().save(str(OUT / f"{name}.png"))


def state(latest="2.0.2", newer=True, can=True, **extra) -> dict:
    d = {"enabled": True, "current": "2.0.1", "latest": latest, "newer": newer, "url": "https://example.invalid/release",
         "name": f"OpenGrokBot {latest}", "checked_at": time.time(), "error": "",
         "notes": "## What's new\n- Faster browsing for Bots\n- Fixes for Routines",
         "size": 83 * MB, "can_install": can,
         "download": {"status": "idle", "got": 0, "total": 0, "error": "", "version": "", "path": ""}}
    d.update(extra)
    return d


def dl(status: str, version: str = "2.0.2", **kw) -> dict:
    base = {"status": status, "got": 0, "total": 83 * MB, "error": "", "version": version, "path": ""}
    base.update(kw)
    return base


home = win.pages["home"]


def inject(d: dict) -> None:
    """Stands in for a poll: the controller state and the changed signal, as a real GET /api/updates would set them."""
    ctl.state = dict(d)
    ctl.changed.emit(d)


win.resize(1180, 760)
win.show()
win.select("page:home")
pump(0.3)

# ---- 1. a new version shows the card, the sidebar item, one toast and one tray message -----------------------------------------------
inject(state())
pump(0.2)
check(home.update_card.isVisibleTo(home), "the Home update card shows for a new version")
check(home.upd_title.text() == "OpenGrokBot 2.0.2 is available (you have 2.0.1)", f"card title ({home.upd_title.text()!r})")
check(home.upd_notes.text() == "What's new", f"notes show the first real line without markdown marks ({home.upd_notes.text()!r})")
check(home.upd_now.text() == "Update now" and home.upd_now.isVisibleTo(home) and home.upd_later.isVisibleTo(home), "Update now and Later are shown when idle")
check(home.upd_bar.isHidden() and home.upd_status.isHidden(), "no progress bar while idle")
check(win.update_row.isVisibleTo(win) and "2.0.2" in win.update_row.title.text(), f"sidebar item shows the version ({win.update_row.title.text()!r})")
check([t for t in toasts if "is available" in t] == ["OpenGrokBot 2.0.2 is available. Click Update now on the Home page."], f"one toast for 2.0.2 ({toasts})")
check(len(tray_msgs) == 1 and tray_msgs[0][0] == "OpenGrokBot 2.0.2 is available" and tray_msgs[0][1] == "Click Update now on the Home page.", "one tray message for 2.0.2")
shot(win, "01_home_idle")

# ---- 2. the same version again does not notify twice (a theme change rebuilds the window, so this is saved, not in memory) -----
inject(state())
pump(0.1)
check(len(toasts) == 1 and len(tray_msgs) == 1, "the same version does not notify again")
check(load_ui_config().get("notified_update") == "2.0.2", "the notified version is saved in the UI config")

# ---- 3. download in progress --------------------------------------------------------------------------------------------------------
inject(state(download=dl("downloading", got=42 * MB)))
pump(0.2)
check(home.upd_bar.isVisibleTo(home) and home.upd_bar.maximum() == 83 * MB, "the progress bar shows while downloading")
check(home.upd_status.text() == "Downloading… 42.0 MB of 83.0 MB", f"download line ({home.upd_status.text()!r})")
check(home.upd_now.isHidden() and home.upd_later.isHidden(), "Update now and Later are replaced by the progress while downloading")
shot(win, "02_home_downloading")

# ---- 4. installing: the app will restart -------------------------------------------------------------------------------------------
inject(state(download=dl("ready", got=83 * MB), installing=True))
pump(0.2)
check(home.upd_status.text() == "Installing… the app will restart", f"install line ({home.upd_status.text()!r})")
shot(win, "03_home_installing")

# ---- 5. an error: the card says why and offers a retry -----------------------------------------------------------------------------
inject(state(download=dl("error", error="connection reset")))
pump(0.2)
check("connection reset" in home.upd_status.text() and home.upd_now.text() == "Try again", f"error line and retry ({home.upd_status.text()!r})")
shot(win, "04_home_error")

# ---- 6. Later hides the sidebar item and the card, and it stays hidden for this version ------------------------------------------
inject(state())
pump(0.1)
home._update_later()
pump(0.2)
check(load_ui_config().get("dismissed_update") == "2.0.2", "Later remembers the version")
check(not win.update_row.isVisibleTo(win) and not home.update_card.isVisibleTo(home), "Later hides the sidebar item and the Home card")
check(len(toasts) == 1, "Later does not toast again")
inject(state(skipped=True))
pump(0.1)
check(not win.update_row.isVisibleTo(win), "a skipped version stays hidden on the next poll")

# ---- 7. a newer version shows again, at the 980x620 minimum window size -----------------------------------------------------------
win.resize(980, 620)
pump(0.3)
inject(state(latest="2.0.3", notes="Release 2.0.3\nSmaller fixes"))
pump(0.3)
check(win.update_row.isVisibleTo(win) and "2.0.3" in win.update_row.title.text(), "a newer version than the skipped one shows the sidebar item")
check(len(toasts) == 2 and "2.0.3" in toasts[-1], "the newer version toasts once")
check(home.upd_notes.text() == "Release 2.0.3", f"the first notes line is used when there is no markdown heading ({home.upd_notes.text()!r})")
shot(win, "05_window_980x620")

# ---- 8. the sidebar item opens Home ----------------------------------------------------------------------------------------------
win.select("page:knowledge")
win.update_row.clicked.emit()
pump(0.2)
check(win.stack.currentWidget() is home, "clicking the sidebar item opens Home")

# ---- 9. Update now: download, install, then the window asks to quit ----------------------------------------------------------------
calls: list[str] = []


def fake_post(path, body=None, ok=None, err=None, **kw):
    calls.append(path)
    if path.endswith("/download"):
        ok(state(latest="2.0.3", download=dl("ready", version="2.0.3", got=83 * MB, path="x")))
    elif path.endswith("/install"):
        ok({"ok": True})


ctl.api.post = fake_post                      # type: ignore[assignment]
ctl.update_now()
pump(0.2)
check(calls == ["/api/updates/download", "/api/updates/install"], f"Update now downloads, then installs ({calls})")
check(quits == [False], f"after the installer starts the window quits so it can replace the app ({quits})")
ctl.installing = False                        # the real window quits here; the test keeps going on the same controller

# ---- 10. no in-app install: the button opens the release page instead -------------------------------------------------------------
calls.clear()
quits.clear()
inject(state(latest="2.0.4", can=False))
pump(0.1)
check(home.upd_now.text() == "Open download page", "without in-app install the button says so")
ctl.update_now()
check(opened == ["https://example.invalid/release"] and not calls and not quits, "without in-app install Update now opens the release page only")

# ---- 11. a failed download shows the reason in the card and does not install ----------------------------------------------------
calls.clear()


def failing_post(path, body=None, ok=None, err=None, **kw):
    calls.append(path)
    err("the service rejected the download")


ctl.api.post = failing_post                   # type: ignore[assignment]
inject(state(latest="2.0.5"))
ctl.update_now()
pump(0.2)
check(calls == ["/api/updates/download"], "a failed download does not start the installer")
check("could not download" in home.upd_status.text().lower(), f"the card shows the failed download ({home.upd_status.text()!r})")

print("\n" + ("ALL OK" if not fails else f"{len(fails)} FAILED"))
sys.exit(1 if fails else 0)
