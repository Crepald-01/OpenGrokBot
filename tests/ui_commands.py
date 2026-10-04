"""Drives the desktop chat with slash commands (offscreen): the popup, completion, command output and /new.

    python tests/ui_commands.py [screenshot-folder]
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
os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-cmdui-")
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else None

import uvicorn  # noqa: E402
from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.engine import Engine  # noqa: E402
from service.server import create_app  # noqa: E402

eng = Engine()
eng.settings.set("notifications.toast", False)
eng.start()
PORT, TOKEN = 18768, "t"
threading.Thread(target=uvicorn.Server(uvicorn.Config(create_app(eng, TOKEN), host="127.0.0.1", port=PORT, log_level="warning")).run, daemon=True).start()
time.sleep(1.5)
bot = eng.bots.create("Inbox", job="Triage email", emoji="📥")
eng.threads.main_thread(bot["id"])
eng.memory.add(bot["id"], "preference", "Keep summaries to one screen", pinned=True)

from ui import theme  # noqa: E402
from ui.api import Api, Connection, EventThread  # noqa: E402
from ui.chat_view import ChatPage  # noqa: E402
from ui.store import Store  # noqa: E402
from ui.widgets import ImageCache  # noqa: E402

app = QApplication(sys.argv)
app.setStyleSheet(theme.qss())
api = Api(Connection("local", f"http://127.0.0.1:{PORT}", TOKEN))
store = Store(api)
store.apply_bootstrap(api.call("GET", "/api/bootstrap"))
ev = EventThread(api)
ev.received.connect(store.on_event)
ev.start()
page = ChatPage(api, store, ImageCache(api))
page.resize(1100, 760)
page.show()


def pump(sec: float) -> None:
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def key(k, text=""):
    for t in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
        app.sendEvent(page.input, QKeyEvent(t, k, Qt.KeyboardModifier.NoModifier, text))
    pump(0.15)


def typ(s: str):
    for ch in s:
        key(Qt.Key(ord(ch.upper())) if ch.isalnum() else Qt.Key.Key_Slash, ch)


def notices() -> list[str]:
    from ui.widgets import AutoMarkdown
    return [w.text() if hasattr(w, "text") else "" for w in page.list.box.findChildren(AutoMarkdown)]


fails = []
page.show_bot(bot["id"])
pump(2.0)
page.input.setPlainText("/")
pump(0.3)
n_all = page.popup.count()
print("popup on '/':", page.popup.isVisible(), n_all, "commands shown")
if not page.popup.isVisible() or n_all < 5:
    fails.append("popup did not open on /")
if len(page.commands) < 15:
    fails.append("commands were not loaded from the service")

page.input.setPlainText("/mem")
pump(0.3)
top = page.popup.item(0).data(Qt.ItemDataRole.UserRole) if page.popup.count() else ""
print("popup on '/mem':", [page.popup.item(i).data(Qt.ItemDataRole.UserRole) for i in range(page.popup.count())])
if top != "memory":
    fails.append("filtering did not put /memory first")
if OUT:
    OUT.mkdir(parents=True, exist_ok=True)
    page.grab().save(str(OUT / "commands-popup.png"))

key(Qt.Key.Key_Tab)   # complete
print("after Tab:", repr(page.input.toPlainText()), "| popup hidden:", not page.popup.isVisible())
if page.input.toPlainText() != "/memory ":
    fails.append("Tab did not complete the command")

page.input.setPlainText("/status")
pump(0.2)
key(Qt.Key.Key_Return)   # exact command + Enter sends
pump(1.5)
page.input.setPlainText("/memory")
pump(0.2)
key(Qt.Key.Key_Return)
pump(1.5)
page.input.setPlainText("/nonsense")
pump(0.2)
page.popup.hide()
page.send()
pump(1.5)
msgs = " || ".join(notices())
print("command output in chat:", msgs[:200].replace("\n", " "))
raw = [i for i in eng.threads.display(page.thread_id) if i["type"] == "notice"]
print("notices stored:", len(raw))
if len(raw) < 3:
    fails.append("command outputs were not posted")
if not any("Keep summaries" in i["text"] for i in raw):
    fails.append("/memory output missing")
if not any("Unknown command" in i["text"] for i in raw):
    fails.append("unknown command message missing")
if any(i["type"] == "user" and i["text"].startswith("/") for i in eng.threads.display(page.thread_id)):
    fails.append("a command was stored as a chat message")

before = page.thread_id
page.input.setPlainText("/new Quarterly planning")
page.send()
pump(2.5)
print("thread switched:", page.thread_id != before, "|", page.thread_btn.text())
if page.thread_id == before:
    fails.append("/new did not switch to the new thread")
if OUT:
    page.input.setPlainText("/model")
    pump(0.3)
    page.popup.hide()
    page.send()
    pump(1.5)
    page.grab().save(str(OUT / "commands-output.png"))
print("FAIL: " + "; ".join(fails) if fails else "ALL OK")
sys.stdout.flush()
ev.stop()
eng.stop()
os._exit(1 if fails else 0)
