"""Checks the Settings > Mobile tab refreshes its link/QR and message after phone access is switched on.

    python tests/ui_mobile.py
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
os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-mobile-")

import uvicorn  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.engine import Engine  # noqa: E402
from service.server import create_app  # noqa: E402

eng = Engine()
eng.settings.set("notifications.toast", False)
eng.start()
PORT, TOKEN = 18767, "t"
threading.Thread(target=uvicorn.Server(uvicorn.Config(create_app(eng, TOKEN), host="127.0.0.1", port=PORT, log_level="warning")).run, daemon=True).start()
time.sleep(1.5)

from ui import theme  # noqa: E402
from ui.api import Api, Connection  # noqa: E402
from ui.pages_settings import SettingsPage  # noqa: E402
from ui.store import Store  # noqa: E402

app = QApplication(sys.argv)
app.setStyleSheet(theme.qss())
api = Api(Connection("local", f"http://127.0.0.1:{PORT}", TOKEN))
store = Store(api)
store.apply_bootstrap(api.call("GET", "/api/bootstrap"))


def pump(sec: float) -> None:
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


fails = []
page = SettingsPage(api, store)
page.resize(1000, 800)
page.show()
page.load()
pump(1.5)
before = page.m_urls.toPlainText()
print("before:", before.splitlines()[:1])

restarts = []
page.restartService.connect(lambda: restarts.append(1))   # the real app restarts the service here
page.m_lan.setChecked(True)
page.apply_mobile()
pump(6.0)
after = page.m_urls.toPlainText().splitlines()
print("after:", after, "| message:", page.m_msg.text()[:60])
if not restarts:
    fails.append("restart was not requested")
if not after or after[0].startswith("http://127.0.0.1"):
    fails.append("link was not refreshed to a network address")
if not page.m_msg.text().startswith("Ready"):
    fails.append("message did not change to Ready")
if page.m_qr.pixmap() is None or page.m_qr.pixmap().isNull():
    fails.append("QR code missing")

# turning it back off
page.m_lan.setChecked(False)
page.apply_mobile()
pump(5.0)
print("off:", page.m_urls.toPlainText().splitlines(), "|", page.m_msg.text()[:50])
if not page.m_msg.text().startswith("Phone access is off"):
    fails.append("off message wrong")
print("FAIL: " + "; ".join(fails) if fails else "ALL OK")
eng.stop()
sys.stdout.flush()
os._exit(1 if fails else 0)
