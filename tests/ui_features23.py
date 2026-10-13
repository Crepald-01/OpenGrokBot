"""2.3 UI, offscreen: library install-all and placeholder, notification history tab, sidebar hover bar.

    python tests/ui_features23.py [screenshot-folder]
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-ui23-")
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("C:/gbt/shots23")
OUT.mkdir(parents=True, exist_ok=True)

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core import library as L  # noqa: E402
from ui import motion, theme  # noqa: E402
from ui.api import Api, Connection  # noqa: E402
from ui.library_dialog import LibraryDialog  # noqa: E402
from ui.main_window import SideRow  # noqa: E402
from ui.pages_inbox import InboxPage  # noqa: E402
from ui.store import Store  # noqa: E402
from ui.widgets import Skeleton  # noqa: E402

app = QApplication(sys.argv)
app.setStyleSheet(theme.qss())
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def pump(sec: float = 0.2) -> None:
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


api = Api(Connection("local", "http://127.0.0.1:1", "t"))
folder = ROOT / "skills" / "library"
rows = [{**e, "origin": "bundled", "state": "installed" if i < 2 else "available", "installed_status": ""} for i, e in enumerate(L.build_catalog(folder))]
posts: list[str] = []


def fake_get(path, ok=None, err=None, **kw):
    if path == "/api/library":
        ok(rows)
    elif path == "/api/notifications":
        ok([{"id": 3, "ts": time.time(), "kind": "done", "bot_id": "b1", "bot_name": "Researcher", "thread_id": "t1", "title": "Report ready", "body": "report.md saved", "read": 0},
            {"id": 2, "ts": time.time() - 90, "kind": "done", "bot_id": "b2", "bot_name": "Writer", "thread_id": "t2", "title": "Draft done", "body": "", "read": 1}])
    elif path == "/api/notifications/unread":
        ok({"count": 1})
    elif path.startswith("/api/library/"):
        e = next(r for r in rows if r["name"] == path.rsplit("/", 1)[1])
        raw = (folder / e["file"]).read_text(encoding="utf-8")
        ok({**e, "raw": raw, "flags": L.flags_for(raw)})
    elif ok:
        ok([])


def fake_post(path, body=None, ok=None, err=None, **kw):
    posts.append(path)
    if ok:
        ok({"name": path.split("/")[-2], "ok": True})


api.get = fake_get  # type: ignore[assignment]
api.post = fake_post  # type: ignore[assignment]

# -- library: placeholder and install-all
d = LibraryDialog(api)
d.show()
pump()
n_new = sum(1 for r in rows if r["state"] == "available")
check(d.list.count() == len(rows), "library lists all skills")
check(d.btn_all.isEnabled() and str(n_new) in d.btn_all.text(), f"install-all counts the {n_new} new skills")
check(isinstance(d.left.widget(0), Skeleton) and d.left.currentWidget() is d.list, "placeholder exists, list shown once loaded")
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)  # type: ignore[assignment]
posts.clear()
d.install_all()
pump()
inst = [p for p in posts if p.endswith("/install")]
check(len(inst) == n_new, f"install-all installed {n_new} skills one by one ({len(inst)})")
d.close()

# -- inbox: notification history
store = Store(api)
store.bots = [{"id": "b1", "name": "Researcher", "emoji": "R"}, {"id": "b2", "name": "Writer", "emoji": "W"}]
inbox = InboxPage(api, store)
inbox.resize(900, 600)
inbox.show()
pump()
inbox.tabs.setCurrentIndex(3)
pump()
check(inbox.notes.rowCount() == 2, "notification history lists 2 rows")
check(inbox.n_count.text() == "1 unread", f"unread counted ({inbox.n_count.text()})")
check(inbox.notes.item(0, 2).font().bold() and not inbox.notes.item(1, 2).font().bold(), "unread row bold, read row not")
inbox.n_bot.setCurrentIndex(inbox.n_bot.findData("b2"))
pump()
check(inbox.notes.rowCount() == 1, "filter by Bot")
posts.clear()
inbox._read_all()
check("/api/notifications/read" in posts, "mark all read posts")
inbox.n_bot.setCurrentIndex(0)
pump()
inbox.setGraphicsEffect(None)
pump(0.6)
inbox.grab().save(str(OUT / "inbox-notifications.png"))

# -- sidebar hover bar
motion.set_enabled(True)
row = SideRow("Home", "", None, tall=False)
row.resize(200, 36)
row.show()
pump(0.5)   # a synthetic Enter at (0,0) may start the grow animation; let it finish
check(row.bar.maximumHeight() == 16, f"bar starts at full height ({row.bar.maximumHeight()})")
app.sendEvent(row, QEvent(QEvent.Type.Enter))
check(row.bar.property("hover") is True, "hover marks the bar")
app.sendEvent(row, QEvent(QEvent.Type.Leave))
pump(0.3)
check(row.bar.property("hover") is False and row.bar.maximumHeight() == 16, "leaving resets the bar")
motion.set_enabled(None)

print("ALL OK" if not fails else "FAILED")
sys.stdout.flush()
os._exit(1 if fails else 0)
