"""Drives the 1.5 desktop features offscreen: Files, Quick Ask, digest and update banner on Home, costs on Usage, backup/restore,
pinned and duplicated Bots, pause all, Match Windows theme.

    python tests/ui_features15.py [screenshot-folder]
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-f15ui-")
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if OUT:
    OUT.mkdir(parents=True, exist_ok=True)

import uvicorn  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QInputDialog, QLabel, QMessageBox  # noqa: E402

from core import backup, paths  # noqa: E402
from core.engine import Engine  # noqa: E402
from service.server import create_app  # noqa: E402

eng = Engine()
eng.settings.set("notifications.toast", False)
eng.start()
PORT, TOKEN = 18771, "t"
threading.Thread(target=uvicorn.Server(uvicorn.Config(create_app(eng, TOKEN), host="127.0.0.1", port=PORT, log_level="warning")).run, daemon=True).start()
time.sleep(1.5)


def tiny_png() -> bytes:
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x7c\x9c\xff" * 40 for _ in range(40))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 40, 40, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


inbox = eng.bots.create("Inbox", job="Triage email", emoji="📥")
research = eng.bots.create("Researcher", job="Deep web research", emoji="🔎")
writer = eng.bots.create("Writer", job="Drafts posts", emoji="✍️")
for b in (inbox, research, writer):
    eng.threads.main_thread(b["id"])
ws = paths.workspace_dir()
(ws / "shared" / "reports").mkdir(parents=True, exist_ok=True)
(ws / "shared" / "summary.md").write_text("# Inbox summary\n\n- 3 urgent\n- 9 can wait\n", encoding="utf-8")
(ws / "shared" / "reports" / "q3.csv").write_text("region,sales\nEU,120\nUS,340\n", encoding="utf-8")
(ws / "shared" / "chart.png").write_bytes(tiny_png())
(ws / "downloads" / "archive.bin").write_bytes(b"\x00\x01\x02" * 200)
eng.actions.record(bot_id=inbox["id"], tool="fs_write", args="{}", path="shared/summary.md")
eng.actions.record(bot_id=research["id"], tool="browser_open", args="{}", url="https://www.example.com/q3")
eng.db.insert("turns", {"id": "tt1", "bot_id": inbox["id"], "thread_id": "x", "trigger": "user", "status": "done", "started_at": time.time(), "ended_at": time.time()})
eng.usage.record(inbox["id"], "u1", "openai", "gpt-x", 800_000, 120_000)
eng.usage.record(research["id"], "u2", "mystery", "m1", 300_000, 50_000)
eng.usage.pricing.set_prices({"openai/gpt-x": {"in": 2.5, "out": 10}}, "$")
eng.settings.set("updates_state", {"checked_at": time.time(), "latest": "9.9.9", "url": "https://example.test/release", "name": "OpenGrokBot 9.9.9", "error": ""})

from ui import theme  # noqa: E402
from ui.api import Api, Connection, EventThread, load_ui_config  # noqa: E402
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
win.resize(1320, 860)
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


def labels(w) -> str:
    return " | ".join(lb.text() for lb in w.findChildren(QLabel))


pump(1.0)

# ---- 1. Files -----------------------------------------------------------------------------------------------------
win.select("page:files")
fp = win.pages["files"]
pump(1.5)
names = [fp.list.item(i).data(Qt.ItemDataRole.UserRole)["name"] for i in range(fp.list.count()) if fp.list.item(i).data(Qt.ItemDataRole.UserRole)]
check({"summary.md", "q3.csv", "chart.png", "archive.bin"} <= set(names), f"Files lists recent files ({len(names)})")
fp.list.setCurrentRow(names.index("summary.md"))
pump(1.2)
check("Inbox summary" in fp.p_text.toPlainText() and fp.stack.currentWidget() is fp.p_text, "a text file previews")
fp.list.setCurrentRow(names.index("chart.png"))
pump(1.8)
check(fp.stack.currentWidget() is fp._sc and fp.p_img.pixmap() is not None and not fp.p_img.pixmap().isNull(), "an image previews")
fp.list.setCurrentRow(names.index("archive.bin"))
pump(1.0)
check(fp.stack.currentWidget() is fp.p_msg and "No preview" in fp.p_msg.text(), "a binary file says there is no preview")
shot(win, "f15-files")
fp.search.setText("q3")
pump(1.6)
res = [fp.list.item(i).data(Qt.ItemDataRole.UserRole)["name"] for i in range(fp.list.count()) if fp.list.item(i).data(Qt.ItemDataRole.UserRole)]
check(res == ["q3.csv"], f"search finds q3.csv ({res})")
fp.search.setText("")
pump(0.8)
fp.set_mode("browse")
pump(1.0)
check(fp.cwd == "" and any(fp.list.item(i).data(Qt.ItemDataRole.UserRole) and fp.list.item(i).data(Qt.ItemDataRole.UserRole)["dir"] for i in range(fp.list.count())), "Folders shows the workspace root")
shared_row = next(i for i in range(fp.list.count()) if (fp.list.item(i).data(Qt.ItemDataRole.UserRole) or {}).get("name") == "shared")
fp._activated(fp.list.item(shared_row))
pump(1.0)
check(fp.cwd == "shared" and fp.up_btn.isVisibleTo(fp), "double-click opens a folder")
fp.go_up()
pump(0.8)
check(fp.cwd == "", "Up goes back")
fp.set_mode("recent")
pump(1.0)
names = [fp.list.item(i).data(Qt.ItemDataRole.UserRole)["name"] for i in range(fp.list.count()) if fp.list.item(i).data(Qt.ItemDataRole.UserRole)]
fp.list.setCurrentRow(names.index("summary.md"))
pump(0.8)
target = Path(tempfile.mkdtemp()) / "copy.md"
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(target), ""))     # type: ignore[assignment]
fp.save_copy()
pump(1.2)
check(target.exists() and "3 urgent" in target.read_text(encoding="utf-8"), "Save a copy writes the file")
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)   # type: ignore[assignment]
fp.delete_current()
pump(1.5)
check(not (ws / "shared" / "summary.md").exists(), "Delete removes the file from the workspace")

# ---- 2. Quick Ask ---------------------------------------------------------------------------------------------------
win.select("page:home")
win.quick_ask()
qa = win._qa
pump(0.5)
check(qa is not None and qa.isVisible() and qa.bots.count() == 3, "Quick Ask opens with all Bots")
qa.bots.setCurrentIndex(qa.bots.findData(research["id"]))
qa.text.setPlainText("what is the weather in Lisbon?")
shot(qa, "f15-quick-ask")
sent: list[str] = []
qa.sent.connect(sent.append)
qa.send()
pump(1.5)
th = eng.threads.main_thread(research["id"])
users = [i["text"] for i in eng.threads.display(th["id"]) if i["type"] == "user"]
check("what is the weather in Lisbon?" in users, "Quick Ask delivers the message to that Bot's chat")
check(sent and sent[0] == "Sent to Researcher." and not qa.isVisible(), f"it confirms and closes ({sent})")
check(load_ui_config().get("quick_bot") == research["id"], "it remembers the last Bot")
win.quick_ask()
pump(0.3)
check(win._qa.bots.currentData() == research["id"], "next time it starts on that Bot")
win._qa.text.setPlainText("/status")
win._qa.send()
pump(1.2)
notes = [i["text"] for i in eng.threads.display(th["id"]) if i["type"] == "notice"]
check(any("Researcher" in n for n in notes), "a slash command works from Quick Ask too")

# ---- 3. Home: digest and update banner ------------------------------------------------------------------------------------
win.select("page:home")
pump(2.0)
home = win.pages["home"]
check(home.update_card.isVisibleTo(home) and "9.9.9" in home.upd_title.text(), f"Home shows the update card ({home.upd_title.text()!r})")
check("Today" in home.digest_text.text() and "task" in home.digest_text.text(), f"Home shows today's digest line ({home.digest_text.text()!r})")
shot(win, "f15-home")
home._update_later()
check(not home.update_card.isVisibleTo(home) and load_ui_config().get("dismissed_update") == "9.9.9", "Dismiss hides the banner and remembers it")
home.load()
pump(1.5)
check(not home.update_card.isVisibleTo(home), "a dismissed update stays hidden")
from ui.pages_home import DigestDialog  # noqa: E402
dd = DigestDialog(api, win)
dd.show()
pump(1.3)
check("Inbox" in dd.view.toPlainText() and "summary.md" in dd.view.toPlainText(), "the digest dialog lists what Bots did")
shot(dd, "f15-digest")
dd.close()

# ---- 4. Usage: cost and prices ---------------------------------------------------------------------------------------------
win.select("page:usage")
pump(2.0)
us = win.pages["usage"]
check(us.c_cost[1].text().endswith("+") and us.c_cost[1].text().startswith("$"), f"Usage shows an estimated cost with + for unpriced models ({us.c_cost[1].text()})")
rows = {us.prices.item(r, 0).text(): (us.prices.item(r, 3).text(), us.prices.item(r, 5).text()) for r in range(us.prices.rowCount())}
check(rows.get("openai/gpt-x", ("", ""))[0] == "2.5" and rows.get("mystery/m1", ("x", ""))[1] == "no price", f"price table lists models ({rows})")
shot(win, "f15-usage-cost")
r_my = next(r for r in range(us.prices.rowCount()) if us.prices.item(r, 0).text() == "mystery/m1")
us.prices.item(r_my, 3).setText("1")
us.prices.item(r_my, 4).setText("4")
us.save_prices()
pump(1.8)
check("+" not in us.c_cost[1].text(), f"pricing the last model removes the + ({us.c_cost[1].text()})")
r_my = next(r for r in range(us.prices.rowCount()) if us.prices.item(r, 0).text() == "mystery/m1")
us.prices.item(r_my, 3).setText("abc")
us.save_prices()
check("not a number" in us.price_msg.text(), "a bad price is refused with a message")
us._price_dirty = False

# ---- 5. Settings: backup, restore, theme choice ---------------------------------------------------------------------------------
win.select("page:settings")
st = win.pages["settings"]
st.tabs.setCurrentIndex(6)
pump(0.8)
check(st.a_theme.findData("auto") >= 0, "theme choices include Match Windows")
check(st.a_hotkey.isChecked() and st.a_updates.isChecked(), "hotkey and update-check options are on by default")
zpath = Path(tempfile.mkdtemp()) / "b.zip"
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(zpath), ""))      # type: ignore[assignment]
st.backup_now()
pump(2.0)
check(zpath.exists() and zpath.stat().st_size > 1000 and "Saved" in st.a_backup_msg.text(), f"Back up… writes a zip ({st.a_backup_msg.text()[:50]})")
QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(zpath), ""))      # type: ignore[assignment]
restarts: list[int] = []
st.restartService.connect(lambda: restarts.append(1))
st.restore_backup()
pump(2.5)
check((backup.pending_dir() / backup.DB_NAME).exists() and restarts, "Restore… stages the backup and asks for a service restart")
import shutil  # noqa: E402
shutil.rmtree(backup.pending_dir(), ignore_errors=True)
shot(win, "f15-settings-app")
st.tabs.setCurrentIndex(3)
pump(0.5)
check(st.d_on.isVisibleTo(st) and st.d_time.time().toString("HH:mm") == "18:00", "Notifications tab has the daily digest option")

# ---- 6. pinned Bots, duplicate, pause all -----------------------------------------------------------------------------------------
win.toggle_pin(writer["id"])
pump(0.5)
order = [b["name"] for b in win.ordered_bots()]
check(order[0] == "Writer", f"a pinned Bot sorts first ({order})")
check(win.rows[f"bot:{writer['id']}"].title.text().startswith("★"), "the sidebar marks it")
entries = [e for e in win.palette_entries() if e.key.startswith("bot:")]
check(entries[0].name == "Writer" and entries[0].hint == "Ctrl+1", "Ctrl+1 follows the pinned order")
check(load_ui_config().get("pinned_bots") == [writer["id"]], "pins are remembered")
win.toggle_pin(writer["id"])
QInputDialog.getText = staticmethod(lambda *a, **k: ("Inbox Two", True))          # type: ignore[assignment]
win.duplicate_bot(inbox["id"])
pump(2.0)
dup = eng.bots.get_by_name("Inbox Two")
check(dup is not None and dup["job"] == "Triage email" and dup["grants"] == [], "Duplicate Bot creates a clean copy")
check(win.current_key == f"bot:{dup['id']}" if dup else False, "and opens it")
win.pause_all(True)
pump(1.0)
check(all(b["paused"] for b in eng.bots.list()), "Pause all pauses every Bot")
check(any(e.key == "action:resumeall" for e in win.palette_entries()), "the palette now offers Resume all")
win.pause_all(False)
pump(1.0)
check(not any(b["paused"] for b in eng.bots.list()), "Resume all resumes them")

# ---- 7. Match Windows theme ------------------------------------------------------------------------------------------------------
orig = theme._apps_use_light
theme._apps_use_light = lambda: True        # type: ignore[assignment]
theme.set_theme("auto")
check(theme.theme_name() == "light" and theme.preference() == "auto", "auto follows Windows: light")
theme._apps_use_light = lambda: False       # type: ignore[assignment]
theme.set_theme("auto")
check(theme.theme_name() == "dark", "auto follows Windows: dark")
theme._apps_use_light = lambda: None        # type: ignore[assignment]
theme.set_theme("auto")
check(theme.theme_name() == "dark", "auto falls back to dark when Windows cannot say")
theme._apps_use_light = orig                # type: ignore[assignment]
theme.set_theme("dark")

# ---- 8. 1.6 fixes ---------------------------------------------------------------------------------------------------------------
win.quick_ask()
pump(0.3)
win._qa.text.setPlainText("half-written thought")
win.quick_ask()                                         # pressing the shortcut again must not wipe the draft
pump(0.2)
check(win._qa.text.toPlainText() == "half-written thought", "Quick Ask keeps the draft when the shortcut is pressed again")
win._qa.close()
check(not hasattr(win._qa, "open") or type(win._qa).open is QDialog.open, "Quick Ask no longer shadows QDialog.open")
from ui.main_window import SideRow  # noqa: E402
row = next(r for r in win.rows.values() if isinstance(r, SideRow))
row.set_badge(3)
pump(0.3)
check(row.badge.height() == 18, f"the sidebar count badge is a pill, not a full-height slab (h={row.badge.height()})")
win.select("page:usage")
pump(1.5)
up = win.pages["usage"]
check(up.table.height() >= up.table.horizontalHeader().height() + 2 * up.table.verticalHeader().defaultSectionSize(), "Usage tables are sized to their rows")
from ui.pages_settings import SettingsPage  # noqa: E402
sp = win.pages["settings"]
sp.a_tray.parentWidget()
check(sp.a_tray.parentWidget() is not sp and any("background service" in lb.text() for lb in sp.a_tray.parentWidget().findChildren(QLabel)),
      "long Settings options keep their detail in a wrapping hint instead of a cut-off checkbox")

print("FAIL: " + "; ".join(fails) if fails else "ALL OK")
sys.stdout.flush()
ev.stop()
eng.stop()
os._exit(1 if fails else 0)
