"""2.4 UI, offscreen: new trigger kinds and templates, trigger list errors and pause banner, meaning-search dialog, Home 'Coming up'.

    python tests/ui_features24.py [screenshot-folder]
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
os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-ui24-")
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("C:/gbt/shots24")
OUT.mkdir(parents=True, exist_ok=True)

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox  # noqa: E402

from ui import theme  # noqa: E402
from ui.api import Api, Connection  # noqa: E402
from ui.pages_automations import TRIGGER_TEMPLATES, TriggerDialog, TriggersTab, describe_trigger  # noqa: E402
from ui.pages_home import HomePage  # noqa: E402
from ui.pages_knowledge import HitCard, MeaningDialog  # noqa: E402
from ui.store import Store  # noqa: E402

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
posts: list[tuple[str, dict]] = []
puts: list[tuple[str, dict]] = []
now = time.time()
TRIGGERS = [
    {"id": "t1", "name": "Competitor pricing", "kind": "page", "url": "https://example.com/pricing", "query": "Price", "interval_min": 30, "bot_id": "b1", "bot_name": "Researcher",
     "workflow_id": "", "fired": 2, "running": False, "last_status": "ok", "last_error": "", "enabled": True, "dry_run": False, "prompt": "p", "folder": "", "pattern": "*", "minutes": 0, "max_per_day": 20},
    {"id": "t2", "name": "Invoices by email", "kind": "email", "url": "", "query": "from:billing@x.com is:unread", "interval_min": 5, "bot_id": "b1", "bot_name": "Researcher",
     "workflow_id": "", "fired": 0, "running": False, "last_status": "", "last_error": "Researcher has not been given the gmail connector.", "enabled": True, "dry_run": False,
     "prompt": "p", "folder": "", "pattern": "*", "minutes": 0, "max_per_day": 20},
    {"id": "t3", "name": "Meeting briefs", "kind": "calendar", "url": "", "query": "", "interval_min": 5, "bot_id": "b1", "bot_name": "Researcher", "workflow_id": "", "fired": 1,
     "running": False, "last_status": "ok", "last_error": "", "enabled": True, "dry_run": False, "prompt": "p", "folder": "", "pattern": "*", "minutes": 15, "max_per_day": 20},
]
state = {"paused": False, "embed": {"enabled": False, "profile": "", "model": "text-embedding-3-small", "usable": False, "why": "Meaning-based search is switched off.", "local": False,
                                     "last_error": "", "allowed": True, "vectors": 0, "passages": 12,
                                     "providers": [{"id": "openai", "label": "OpenAI", "local": False}, {"id": "ollama", "label": "Ollama (local)", "local": True}]}}


def fake_get(path, ok=None, err=None, **kw):
    if path == "/api/triggers":
        ok({"triggers": TRIGGERS, "runs": [], "paused": state["paused"]})
    elif path.startswith("/api/triggers/") and path.endswith("/runs"):
        ok({"runs": []})
    elif path == "/api/workflows":
        ok({"workflows": []})
    elif path == "/api/knowledge/embeddings":
        ok(state["embed"])
    elif path == "/api/upcoming":
        ok({"items": [{"kind": "routine", "name": "Morning report", "bot": "Researcher", "at": now + 1800}, {"kind": "workflow", "name": "Weekly pipeline", "bot": "", "at": now + 7200}],
            "more": 2, "triggers": {"watching": 3, "total": 3, "paused": False, "problems": 1}})
    elif path == "/api/digest":
        ok({"headline": "5 actions today"})
    elif path == "/api/usage":
        ok({"today": {}, "limit": 0, "total": 0})
    elif ok:
        ok([])


def fake_post(path, body=None, ok=None, err=None, **kw):
    posts.append((path, body or {}))
    if path == "/api/triggers/pause":
        state["paused"] = bool((body or {}).get("paused"))
        ok({"paused": state["paused"]})
    elif path == "/api/triggers/t1/check":
        ok({"started": 0, "error": "", "last_checked_at": now})
    elif path == "/api/knowledge/embeddings":
        if body.get("enabled") and not body.get("confirm"):
            err("Confirm that you understand your documents' text will be sent to the embedding service.")
        else:
            state["embed"] = {**state["embed"], "enabled": body["enabled"], "profile": body["profile"], "model": body["model"], "usable": body["enabled"], "why": ""}
            ok(state["embed"])
    elif ok:
        ok({"id": "new", "name": (body or {}).get("name", "")})


def fake_put(path, body=None, ok=None, err=None, **kw):
    puts.append((path, body or {}))
    if ok:
        ok({})


api.get, api.post, api.put = fake_get, fake_post, fake_put  # type: ignore[assignment]
store = Store(api)
store.bots = [{"id": "b1", "name": "Researcher", "emoji": "R", "paused": False}]

# -- every kind opens with the right fields and posts the right body
for kind, fields in (("page", ("url", "query", "every", "per_day")), ("feed", ("url", "every", "per_day")), ("email", ("query", "every", "per_day")),
                     ("calendar", ("minutes", "every", "per_day")), ("webhook", ()), ("folder", ())):
    d = TriggerDialog(api, store, kind, None, [{"id": "w1", "name": "Pipe"}])
    d.show()
    pump(0.05)
    visible = {n for n in ("url", "query", "minutes", "every", "per_day") if getattr(d, n).isVisibleTo(d)}
    check(visible == set(fields), f"{kind}: shows {sorted(fields)} (got {sorted(visible)})")
    wf_items = [d.target.itemText(i) for i in range(d.target.count()) if "Workflow" in d.target.itemText(i)]
    check(bool(wf_items) == (kind not in ("email", "calendar")), f"{kind}: workflow targets {'offered' if wf_items else 'hidden'}")
    d.close()

d = TriggerDialog(api, store, "page", None, [])
d.name.setText("Prices")
d.url.setText("https://example.com/p")
d.query.setText("Price")
d.every.setValue(15)
d.prompt.setPlainText("Changed: {{payload}}")
posts.clear()
d.save()
body = posts[-1][1]
check(posts[-1][0] == "/api/triggers" and body["kind"] == "page" and body["url"] == "https://example.com/p" and body["interval_min"] == 15 and body["query"] == "Price",
      "page trigger posts url, focus and interval")
check(d.every.minimum() == 5, "page checks no more often than every 5 minutes")

# -- templates
check(len(TRIGGER_TEMPLATES) >= 5 and all({"title", "kind", "prompt"} <= set(t) for t in TRIGGER_TEMPLATES), "templates are well formed")
d = TriggerDialog(api, store, "email", None, [], preset=TRIGGER_TEMPLATES[0])
check("invoices.csv" in d.prompt.toPlainText() and d.query.text().startswith("has:attachment"), "template fills the dialog")

# -- trigger list: descriptions, errors, pause
tab = TriggersTab(api, store)
tab.resize(1000, 640)
tab.show()
pump()
check(tab.table.rowCount() == 3, "three triggers listed")
check("Page: example.com" in tab.table.item(0, 1).text() and "every 30 min" in tab.table.item(0, 1).text(), "page row describes what it watches")
check(tab.table.item(1, 4).text().startswith("⚠ "), "a trigger that cannot check shows its problem")
check("15 min before" in describe_trigger(TRIGGERS[2]), "calendar row describes the lead time")
check(not tab.paused_banner.isVisible(), "no pause banner while running")
tab.toggle_pause()
pump()
check(tab.paused_banner.isVisible() and tab.btn_pause.text() == "Resume all", "pausing shows the banner and offers Resume")
tab.toggle_pause()
pump()
check(not tab.paused_banner.isVisible(), "resuming hides the banner")
QMessageBox.information = staticmethod(lambda *a, **k: None)  # type: ignore[assignment]
tab.table.selectRow(0)
posts.clear()
tab.check_now()
check(posts and posts[-1][0] == "/api/triggers/t1/check", "Check now posts for the selected trigger")
QInputDialog.getItem = staticmethod(lambda *a, **k: (TRIGGER_TEMPLATES[3]["title"], True))  # type: ignore[assignment]
opened = {}
tab.new = lambda kind, preset=None: opened.update(kind=kind, preset=preset)  # type: ignore[assignment]
tab.from_template()
check(opened.get("kind") == "feed" and opened["preset"]["name"] == "Reading list", "From a template opens the chosen kind prefilled")
tab.grab().save(str(OUT / "triggers.png"))

# -- meaning-search dialog: must confirm before switching on
m = MeaningDialog(api)
m.show()
pump()
check(m.provider.count() == 2 and not m.on_btn.isHidden(), "dialog lists OpenAI-compatible services")
from PySide6.QtWidgets import QLabel as _L  # noqa: E402
check(any("sent to the embedding service" in lb.text() for lb in m.findChildren(_L)), "dialog shows the privacy warning")
m.provider.setCurrentIndex(m.provider.findData("ollama"))
m._set(True)
pump()
check("Confirm" in m.err.text(), "switching on without confirming is refused")
m.confirm.setChecked(True)
m._set(True)
pump()
check(state["embed"]["enabled"] and m.off_btn.isEnabled() and m.on_btn.text() == "Apply", "confirmed: on, switch-off button enabled")
m.grab().save(str(OUT / "meaning-search.png"))
m._set(False)
pump()
check(not state["embed"]["enabled"], "switching off")

# -- hit card says why
c = HitCard({"name": "Pets", "seq": 2, "snippet": "A kitten.", "why": "meaning"}, "pet")
hit_texts = [lb.text() for lb in c.findChildren(_L)]
check(any("matched the meaning" in t for t in hit_texts), "a result says it matched on meaning")

# -- Home: coming up
home = HomePage(api, store)
home.resize(1100, 800)
home.show()
pump(0.4)
from PySide6.QtWidgets import QLabel  # noqa: E402
texts = [lb.text() for lb in home.coming_card.findChildren(QLabel)]
check("Morning report" in texts and "Weekly pipeline" in texts, f"Home lists what is coming up ({texts})")
check(any("2 more" in t for t in texts), "Home says how many more are due")
check(any("3 triggers watching, 1 with a problem" in t for t in texts), "Home says how many triggers are watching")
home.grab().save(str(OUT / "home.png"))

print("ALL OK" if not fails else "FAILED")
sys.stdout.flush()
os._exit(1 if fails else 0)
