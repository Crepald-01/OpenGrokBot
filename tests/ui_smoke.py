"""Renders every page of the desktop UI offscreen against a live in-process service and saves screenshots.

    set QT_QPA_PLATFORM=offscreen
    python tests/ui_smoke.py <output-folder>
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
os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-ui-")
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="ui-shots-"))
OUT.mkdir(parents=True, exist_ok=True)

import uvicorn  # noqa: E402
from PySide6.QtCore import QTimer, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core import agent as agent_mod  # noqa: E402
from core.engine import Engine  # noqa: E402
from core.providers import LLMResult, ToolCall  # noqa: E402
from service.server import create_app  # noqa: E402
from tests.test_engine import FakeProvider  # noqa: E402

PORT, TOKEN = 18765, "ui-smoke-token"
eng = Engine()
eng.settings.set("notifications.toast", False)
eng.settings.set("network", {"mode": "open", "allow": [], "deny": [], "block_private": False})
eng.start()
server = uvicorn.Server(uvicorn.Config(create_app(eng, TOKEN), host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
time.sleep(1.5)

# data to look at -----------------------------------------------------------------
team = eng.create_team(["chief_of_staff", "inbox", "researcher"])
inbox = next(b for b in team["bots"] if b["name"] == "Inbox")
chief = next(b for b in team["bots"] if b["name"] == "Chief of Staff")
th = eng.threads.main_thread(inbox["id"])
(eng.computer.workspace / "shared" / "summary.md").write_text("# Inbox summary\n- 3 urgent\n")
fake = FakeProvider([
    LLMResult(text="Let me check your inbox and write a summary.\n\n**Plan**\n1. Search unread mail\n2. Summarise\n3. Save to the workspace",
              tool_calls=[ToolCall("a", "fs_list", {"path": "shared"}), ToolCall("b", "fs_write", {"path": "shared/summary.md", "content": "# Inbox summary\n- 4 urgent\n"})]),
    LLMResult(text="Done. I found **4 urgent** emails and saved the summary to `shared/summary.md`.\n\n| Sender | Subject |\n|---|---|\n| Ana | Contract |\n"),
])
agent_mod.make_provider = lambda *a, **k: fake
eng.send_user_message(th["id"], "Go through my unread email and summarise what's urgent.")
t0 = time.time()
while time.time() - t0 < 15 and (eng.turns.is_busy(inbox["id"]) or eng.approvals.pending_count() == 0):
    time.sleep(0.2)
time.sleep(0.5)  # the overwrite of summary.md is waiting for approval
eng.routines.create(inbox["id"], "Morning triage", "0 8 * * 1-5", skill="inbox-triage")
eng.memory.add(inbox["id"], "preference", "Keep summaries to one screen", pinned=True)
eng.memory.add(inbox["id"], "voice", "Direct, friendly, no exclamation marks")
eng.messaging.create_handoff(chief["id"], "Inbox", "Triage this week's mail", "Summarise urgent mail daily")

# UI ---------------------------------------------------------------------------------
from ui import theme  # noqa: E402
from ui.api import Api, Connection, EventThread  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
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


def pump(sec: float) -> None:
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def shot(name: str) -> None:
    pump(0.9)
    win.grab().save(str(OUT / f"{name}.png"))
    print("saved", name)


pump(1.0)
win.start_page()
shot("00-home")
from ui.main_window import QuickSwitcher, ShortcutsDialog  # noqa: E402
qs = QuickSwitcher(win.palette_entries(), ["page:usage", f"bot:{inbox['id']}"], win)
qs.show()
pump(0.6)
qs.grab().save(str(OUT / "00-palette.png"))
qs.input.setText("inb")
pump(0.4)
qs.grab().save(str(OUT / "00-palette-search.png"))
assert qs.list.currentItem() is not None and qs.list.currentItem().data(Qt.ItemDataRole.UserRole) == f"bot:{inbox['id']}", 'search should select the Inbox Bot first'
qs.close()
sd = ShortcutsDialog(win)
sd.show()
pump(0.4)
sd.grab().save(str(OUT / "00-shortcuts.png"))
sd.close()
# navigation: Ctrl+N jumps to the Nth Bot, Home cards and the "Needs you" Review button open the right chat
win.jump_to_bot(1)
assert win.current_key == f"bot:{store.bots[1]['id']}", win.current_key
win.select("page:home")
opened: list = []
win.pages["home"].openBot.connect(opened.append)
win.pages["home"].openThread.connect(lambda t, b: opened.append(("thread", b)))
from PySide6.QtWidgets import QPushButton  # noqa: E402
review = [b for b in win.pages["home"].findChildren(QPushButton) if b.text() == "Review"]
assert review, "the pending approval should show a Review button on Home"
review[0].click()
assert opened and opened[-1] == ("thread", inbox["id"]), opened
win.show_bot(inbox["id"])
pump(1.5)
shot("01-chat-with-approval")
win.show_group(team["group"]["id"])
shot("02-group")
for key in ("inbox", "computer", "skills", "routines", "plugins", "usage", "log", "settings"):
    win.show_page(key)
    shot(f"03-{key}")
win.pages["settings"].tabs.setCurrentIndex(1)
shot("04-settings-safety")
win.pages["plugins"].tabs.setCurrentIndex(1)
shot("05-marketplace")
win.edit_bot  # noqa: B018
from ui.dialogs import BotEditor, NewBotDialog  # noqa: E402
d = BotEditor(api, store, inbox["id"], win)
d.show()
d.activateWindow()
pump(1.0)
d.grab().save(str(OUT / "06-bot-editor.png"))
d.close()
nd = NewBotDialog(api, store, win)
nd.show()
nd.activateWindow()
pump(0.5)
nd.list.setCurrentRow(2)
pump(0.3)
nd.grab().save(str(OUT / "07-new-bot.png"))
nd.close()
from ui.takeover import TakeoverView  # noqa: E402
tv = TakeoverView(api, store, inbox["id"], win)
tv.resize(1100, 700)
tv.show()
pump(2.0)
tv.grab().save(str(OUT / "08-takeover.png"))
tv.finished_handback = True
tv.close()
win.show_welcome()
shot("09-welcome")
fresh = eng.bots.create("Researcher 2", job="Deep web research with cited briefs.", emoji="🔎", template="researcher")
eng.threads.main_thread(fresh["id"])
pump(1.0)
win.select(f"bot:{fresh['id']}")
shot("10-empty-chat")
win.select("page:settings")
win.pages["settings"].tabs.setCurrentIndex(0)
shot("11-settings-models")
win.pages["settings"].tabs.setCurrentIndex(5)
shot("12-settings-mobile")
# appearance options: another accent, compact density, large text
theme.configure("violet", "compact", "large")
app.setStyleSheet(theme.qss())
win3 = MainWindow(api, store, tray_available=False)
win3.banner_shown = True
win3.resize(1320, 840)
win3.show()
pump(1.0)
win3.select("page:home")
pump(1.2)
win3.grab().save(str(OUT / "15-violet-compact-large-home.png"))
win3.select("page:settings")
win3.pages["settings"].tabs.setCurrentIndex(6)
pump(0.8)
win3.grab().save(str(OUT / "16-settings-app.png"))
win3.hide()
theme.configure("indigo", "comfortable", "default")
# light theme
theme.set_theme("light")
app.setStyleSheet(theme.qss())
win2 = MainWindow(api, store, tray_available=False)
win2.banner_shown = True
win2.resize(1320, 840)
win2.show()
pump(1.0)
win2.select("page:home")
pump(1.2)
win2.grab().save(str(OUT / "17-light-home.png"))
win2.select(f"bot:{inbox['id']}")
pump(1.5)
win2.grab().save(str(OUT / "13-light-chat.png"))
win2.select("page:plugins")
pump(1.2)
win2.grab().save(str(OUT / "14-light-plugins.png"))
print("OK", OUT)
ev.stop()
eng.stop()
os._exit(0)
