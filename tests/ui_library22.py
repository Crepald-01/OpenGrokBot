"""Drives the skill library dialog offscreen with fake API data and saves a screenshot.

    python tests/ui_library22.py [screenshot-folder]
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
os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-lib22-")
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("C:/gbt/shots22")
OUT.mkdir(parents=True, exist_ok=True)

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import library as L  # noqa: E402
from ui import theme  # noqa: E402
from ui.api import Api, Connection  # noqa: E402
from ui.library_dialog import LibraryDialog  # noqa: E402

app = QApplication(sys.argv)
app.setStyleSheet(theme.qss())
api = Api(Connection("local", "http://127.0.0.1:1", "t"))
posts: list[str] = []
folder = ROOT / "skills" / "library"
rows = [{**e, "origin": "bundled", "state": "installed" if i == 1 else "available", "installed_status": ""} for i, e in enumerate(L.build_catalog(folder))]


def fake_get(path, ok=None, err=None, **kw):
    if path == "/api/library":
        ok(rows)
    else:
        name = path.rsplit("/", 1)[1]
        e = next(r for r in rows if r["name"] == name)
        raw = (folder / e["file"]).read_text(encoding="utf-8")
        ok({**e, "raw": raw, "flags": L.flags_for(raw)})


api.get = fake_get  # type: ignore[assignment]
api.post = lambda path, body=None, ok=None, err=None, **kw: posts.append(path)  # type: ignore[assignment]
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


d = LibraryDialog(api)
d.show()
pump()
check(d.list.count() == len(rows), f"all {len(rows)} skills listed")
check(d.view.toPlainText().startswith("---"), "first skill previewed")
check(d.btn_install.isEnabled(), "install enabled")
d.search.setText("price")
pump()
check(d.list.count() == 1, "search narrows the list")
d.search.setText("")
d.tag.setCurrentIndex(d.tag.findData("dev"))
pump()
check(0 < d.list.count() < len(rows), "topic filter works")
d.tag.setCurrentIndex(0)
pump()
d.search.setText("zzzz-nothing")
pump()
check(d.list.count() == 0 and not d.btn_install.isEnabled(), "empty search disables install")
d.search.setText("")
pump()
d.list.setCurrentRow(0)
pump()
d.install()
check(posts and posts[-1].endswith("/install"), "install posts to the API")
d.grab().save(str(OUT / "library.png"))
print("ALL OK" if not fails else "FAILED")
sys.stdout.flush()
os._exit(1 if fails else 0)
