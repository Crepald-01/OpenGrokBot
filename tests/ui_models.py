"""Checks that the model dropdown detects models from a provider and lets you pick one (offscreen UI + fake provider server).

    python tests/ui_models.py [screenshot.png]
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-models-")

import uvicorn  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from core.engine import Engine  # noqa: E402
from service.server import create_app  # noqa: E402
from tests.test_providers import Handler  # noqa: E402

fake = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=fake.serve_forever, daemon=True).start()
eng = Engine()
eng.settings.set("notifications.toast", False)
eng.start()
PORT, TOKEN = 18766, "t"
threading.Thread(target=uvicorn.Server(uvicorn.Config(create_app(eng, TOKEN), host="127.0.0.1", port=PORT, log_level="warning")).run, daemon=True).start()
time.sleep(1.5)

from ui import theme  # noqa: E402
from ui.api import Api, Connection  # noqa: E402
from ui.dialogs import BotEditor  # noqa: E402
from ui.pages_settings import SettingsPage  # noqa: E402
from ui.store import Store  # noqa: E402

app = QApplication(sys.argv)
app.setStyleSheet(theme.qss())
api = Api(Connection("local", f"http://127.0.0.1:{PORT}", TOKEN))
api.call("PUT", "/api/providers/fakeprov", json_body={"label": "Fake endpoint", "kind": "openai", "base_url": f"http://127.0.0.1:{fake.server_address[1]}/oai",
                                                      "model": "gpt-a", "needs_key": False, "vision": False})
bot = api.call("POST", "/api/bots", json_body={"name": "Picker Bot", "job": "x", "profile": "fakeprov", "model": "gpt-b"})
store = Store(api)
store.apply_bootstrap(api.call("GET", "/api/bootstrap"))


def pump(sec: float) -> None:
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def items(combo) -> list[str]:
    return [combo.itemText(i) for i in range(combo.count())]


fails = []
page = SettingsPage(api, store)
page.resize(1000, 700)
page.show()
page.load()
for i in range(page.prov_list.count()):
    if page.prov_list.item(i).data(0x100) == "fakeprov":   # Qt.UserRole
        page.prov_list.setCurrentRow(i)
pump(2.0)
got = items(page.p_picker.combo)
print("settings dropdown:", got, "| status:", page.p_picker.status.text(), "| current:", page.p_picker.text())
if got != ["gpt-a", "gpt-b"]:
    fails.append("settings dropdown did not list detected models")
page.p_picker.combo.setCurrentIndex(1)
if page.p_picker.text() != "gpt-b":
    fails.append("could not select a detected model")
if len(sys.argv) > 1:
    page.grab().save(sys.argv[1])

# switching to a provider with no key must not fake a list and must say what to do
for i in range(page.prov_list.count()):
    if page.prov_list.item(i).data(0x100) == "openai":
        page.prov_list.setCurrentRow(i)
pump(0.5)
print("no-key provider status:", page.p_picker.status.text())
if "API key" not in page.p_picker.status.text():
    fails.append("missing-key hint not shown")

ed = BotEditor(api, store, bot["id"])
ed.show()
pump(2.0)
got = items(ed.picker.combo)
print("bot editor dropdown:", got, "| selected:", ed.picker.text())
if "gpt-a" not in got or ed.picker.text() != "gpt-b":
    fails.append("bot editor dropdown wrong")
print("FAIL: " + "; ".join(fails) if fails else "ALL OK")
eng.stop()
os._exit(1 if fails else 0)
