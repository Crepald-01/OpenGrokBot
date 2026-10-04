"""Drives the 1.3 desktop features offscreen: palette message search, Do Not Disturb / quiet hours, per-Bot budget, chat export.

    python tests/ui_features13.py [screenshot-folder]
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
os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-f13ui-")
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if OUT:
    OUT.mkdir(parents=True, exist_ok=True)

import uvicorn  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

from core.engine import Engine  # noqa: E402
from service.server import create_app  # noqa: E402

eng = Engine()
eng.settings.set("notifications.toast", False)
eng.start()
PORT, TOKEN = 18769, "t"
threading.Thread(target=uvicorn.Server(uvicorn.Config(create_app(eng, TOKEN), host="127.0.0.1", port=PORT, log_level="warning")).run, daemon=True).start()
time.sleep(1.5)
inbox = eng.bots.create("Inbox", job="Triage email", emoji="📥")
research = eng.bots.create("Researcher", job="Deep web research", emoji="🔎")
th = eng.threads.main_thread(inbox["id"])
th2 = eng.threads.main_thread(research["id"])
eng.threads.add(th["id"], "user", "user", "Please book the Lisbon flights for the offsite next month")
eng.threads.add(th["id"], inbox["id"], "assistant", "Lisbon flights are booked: TAP 123 on Monday, back Friday.")
eng.threads.add(th2["id"], "user", "user", "Find three articles about quokka habitats")
eng.memory.add(inbox["id"], "fact", "Prefers aisle seats on Lisbon trips")
eng.bots.update(research["id"], daily_token_limit=50000)
eng.usage.record(research["id"], "t", "p", "m", 30000, 5000)

from ui import theme  # noqa: E402
from ui.api import Api, Connection, EventThread  # noqa: E402
from ui.main_window import MainWindow, QuickSwitcher  # noqa: E402
from ui.store import Store  # noqa: E402

app = QApplication(sys.argv)
app.setStyleSheet(theme.qss())
api = Api(Connection("local", f"http://127.0.0.1:{PORT}", TOKEN))
store = Store(api)
store.apply_bootstrap(api.call("GET", "/api/bootstrap"))
ev = EventThread(api)
ev.received.connect(store.on_event)
ev.connection.connect(store.set_connected)
ev.start()
win = MainWindow(api, store, tray_available=False)
win.banner_shown = True
win.resize(1320, 840)
win.show()
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
    if OUT:
        pump(0.5)
        widget.grab().save(str(OUT / f"{name}.png"))


pump(1.0)

# ---- 1. palette finds messages and memories, and opens the chat -------------------------------------------------
win.select(f"bot:{research['id']}")
pump(0.5)
qs = QuickSwitcher(win.palette_entries(), [], win, win._search)
qs.show()
qs.input.setText("lisbon")
pump(1.6)
keys = [qs.list.item(i).data(0x100) for i in range(qs.list.count())]
hits = [k for k in keys if k and k.startswith(("thread:", "memory:"))]
check(any(k.startswith(f"thread:{th['id']}:") for k in hits), f"palette lists Lisbon messages ({len(hits)} hits)")
check(any(k == f"memory:{inbox['id']}" for k in hits), "palette lists the Lisbon memory")
check(qs.list.currentItem() is not None, "something is selected")
shot(qs, "f13-palette-search")
qs.input.setText("lisbon flights offsite")
pump(1.2)
n_thread = len([i for i in range(qs.list.count()) if (qs.list.item(i).data(0x100) or "").startswith("thread:")])
check(n_thread == 1, f"more words narrow the hits ({n_thread})")
qs.close()
win._quick_chosen(f"thread:{th['id']}:{inbox['id']}")
pump(0.8)
check(win.current_key == f"bot:{inbox['id']}" and win.chat.thread_id == th["id"], "choosing a hit opens that chat")

# ---- 2. chat export ---------------------------------------------------------------------------------------------
target = Path(tempfile.mkdtemp()) / "out.md"
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(target), ""))     # type: ignore[assignment]
win.chat.export_chat()
pump(1.2)
body = target.read_text(encoding="utf-8") if target.exists() else ""
check("Lisbon flights are booked" in body and "**You**" in body, "Export chat writes a Markdown transcript")

# ---- 3. Do Not Disturb ------------------------------------------------------------------------------------------
settings = win.pages["settings"]
win.select("page:settings")
settings.tabs.setCurrentIndex(3)
pump(0.6)
settings.set_dnd(3600)
pump(1.2)
check(float(store.settings["notifications"]["dnd_until"]) > time.time(), "Do Not Disturb 1 hour saved through the UI")
check("Do Not Disturb" in win.foot.text(), f"sidebar footer shows it: {win.foot.text()!r}")
entries = [e.key for e in win.palette_entries()]
check("action:dndoff" in entries and "action:dnd1" not in entries, "palette offers 'End Do Not Disturb' while it is on")
shown = []
win.toast = lambda text, kind="info": shown.append(text)      # type: ignore[assignment]
win.on_notification({"title": "muted one", "body": "", "muted": True, "thread_id": "", "urgent": True})
win.on_notification({"title": "loud one", "body": "", "muted": False, "thread_id": "", "urgent": False})
check(shown == ["loud one"], f"a muted notification pops nothing up ({shown})")
settings.q_on.setChecked(True)
settings.save_notifications()
pump(1.0)
check(eng.settings.get("notifications.quiet_enabled") is True and eng.settings.get("notifications.quiet_start") == "22:00", "quiet hours saved")
shot(win, "f13-settings-quiet")
settings.set_dnd(-1)
pump(1.0)
check(float(store.settings["notifications"]["dnd_until"]) == 0 and "Do Not Disturb" not in win.foot.text(), "End it turns Do Not Disturb off")

# ---- 4. daily budget in the Bot editor and on Home -----------------------------------------------------------------
from ui.dialogs import BotEditor  # noqa: E402
ed = BotEditor(api, store, inbox["id"], win)
ed.show()
pump(0.8)
check(ed.budget.value() == 0 and ed.budget.specialValueText() == "No limit", "editor shows 'No limit' for no budget")
ed.budget.setValue(120000)
shot(ed, "f13-bot-editor-budget")
ed.save()
pump(1.2)
check(eng.bots.get(inbox["id"])["daily_token_limit"] == 120000, "budget saved from the Bot editor")
ed.close()
win.select("page:home")
pump(1.5)
texts = " | ".join(lb.text() for lb in win.pages["home"].findChildren(__import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel) if lb.text().startswith("Today"))
check("Today 35.0k of 50.0k" in texts, f"Home shows the budget on the Bot card ({texts})")
shot(win, "f13-home-budget")

print("FAIL: " + "; ".join(fails) if fails else "ALL OK")
sys.stdout.flush()
ev.stop()
eng.stop()
os._exit(1 if fails else 0)
