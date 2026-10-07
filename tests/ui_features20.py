"""Drives the 2.0 desktop features offscreen: Knowledge, Automations (routines, workflows, triggers), Activity + Insights, file history,
channels, API tokens, diagnostics, backup model, and conversation branches.

    python tests/ui_features20.py [screenshot-folder]
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-f20ui-")
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if OUT:
    OUT.mkdir(parents=True, exist_ok=True)

import uvicorn  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox  # noqa: E402

from core import agent as agent_mod  # noqa: E402
from core import paths, secrets  # noqa: E402
from core.engine import Engine  # noqa: E402
from core.providers import LLMResult  # noqa: E402
from service.server import create_app  # noqa: E402

# no real model and no real Windows Credential Manager in a test
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
        r = LLMResult(text="Done: I looked into it and here is the result.")
        on_text(r.text)
        r.input_tokens, r.output_tokens = 120, 30
        return r


agent_mod.make_provider = lambda *a, **k: Fake()

eng = Engine()
eng.settings.set("notifications.toast", False)
eng.settings.set("memory.auto_reflect", False)
eng.start()
PORT, TOKEN = 18772, "t"
threading.Thread(target=uvicorn.Server(uvicorn.Config(create_app(eng, TOKEN), host="127.0.0.1", port=PORT, log_level="warning")).run, daemon=True).start()
time.sleep(1.5)

inbox = eng.bots.create("Inbox", job="Triage email", emoji="📥")
research = eng.bots.create("Researcher", job="Deep web research", emoji="🔎")
writer = eng.bots.create("Writer", job="Drafts posts", emoji="✍️")
for b in (inbox, research, writer):
    eng.threads.main_thread(b["id"])
ws = paths.workspace_dir()
(ws / "docs").mkdir(parents=True, exist_ok=True)
(ws / "docs" / "handbook.md").write_text("# Handbook\n\nVacation: 25 days a year.\n\nExpenses over 200 euros need a director's approval.", encoding="utf-8")
(ws / "shared").mkdir(parents=True, exist_ok=True)
eng.computer.write_file("shared/report.md", "# Weekly report\n\nDraft one.")
eng.computer.write_file("shared/report.md", "# Weekly report\n\nDraft two, with more detail.")
eng.computer.write_file("shared/old-notes.txt", "notes that will be deleted")
eng.computer.delete("shared/old-notes.txt", who=research["id"])
eng.knowledge.add_text("Travel policy", "Economy flights only. Business class needs a director's approval. Hotels up to 180 euros a night.")
eng.knowledge.add_text("Brand voice", "Friendly, short sentences, no jargon.", bot_id=writer["id"])
now = time.time()
for i, (b, st, hr) in enumerate(((inbox, "done", 9), (inbox, "done", 9), (research, "done", 14), (research, "error", 15), (writer, "done", 10), (inbox, "done", 18), (writer, "limit", 11))):
    t = now - 86400 * (i % 5) - 3600 * (i % 3)
    eng.db.insert("turns", {"id": f"seed{i}", "bot_id": b["id"], "thread_id": "x", "trigger": "user" if i % 2 else "routine", "status": st, "started_at": t, "ended_at": t + 40 + i * 9, "steps": 3 + i})
    eng.db.insert("usage", {"ts": t, "bot_id": b["id"], "turn_id": f"seed{i}", "profile": "p", "model": "m", "input_tokens": 3000 + i * 400, "output_tokens": 600})
for tool, st, ms in (("browser_open", "ok", 900), ("browser_open", "ok", 1100), ("fs_write", "ok", 12), ("fs_read", "ok", 8), ("fs_read", "error", 15), ("gmail_search", "ok", 420), ("fs_delete", "denied", 3)):
    eng.actions.record(bot_id=research["id"], tool=tool, args="{}", result=f"{tool} {st}: could not open the page" if st == "error" else f"{tool} {st}", status=st, duration_ms=ms)

from ui import theme  # noqa: E402
from ui.api import Api, Connection, EventThread  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.store import Store  # noqa: E402

app = QApplication(sys.argv)
app.setStyleSheet(theme.qss())
api = Api(Connection("local", f"http://127.0.0.1:{PORT}", TOKEN))
store = Store(api)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)   # type: ignore[assignment]
QMessageBox.warning = staticmethod(lambda *a, **k: print("WARNING BOX:", a[1:3]))      # type: ignore[assignment]
store.apply_bootstrap(api.call("GET", "/api/bootstrap"))
ev = EventThread(api)
ev.received.connect(store.on_event)
ev.connection.connect(store.set_connected)
ev.start()
win = MainWindow(api, store, tray_available=False)
win.banner_shown = True
win.resize(1320, 900)
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
        pump(0.6)
        widget.grab().save(str(OUT / f"{name}.png"))


def texts(w) -> str:
    return " | ".join(lb.text() for lb in w.findChildren(QLabel))


# ---- 0. navigation ------------------------------------------------------------------------------------------------------------------
names = [r.title.text() for k, r in win.rows.items() if k.startswith("page:") and hasattr(r, "title")]
check({"Knowledge", "Automations", "Activity"} <= set(names), f"the sidebar has Knowledge, Automations and Activity ({names})")
check(any(e.key == "page:knowledge" for e in win.palette_entries()), "the palette can jump to Knowledge")

# ---- 1. Knowledge -------------------------------------------------------------------------------------------------------------------
win.select("page:knowledge")
pump(1.5)
kp = win.pages["knowledge"]
check(kp.table.rowCount() == 2, f"Knowledge lists the documents ({kp.table.rowCount()})")
check("2 documents" in kp.summary.text(), f"it summarises them ({kp.summary.text()!r})")
r = api.call("POST", "/api/knowledge/workspace", json_body={"path": "docs"})
kp.reload()
pump(1.0)
check(kp.table.rowCount() == 3, "a workspace folder can be added")
kp.table.selectRow(0)
pump(1.0)
check(kp.p_title.text() != "" and kp.p_text.toPlainText() != "", f"selecting a document shows its text ({kp.p_title.text()!r})")
kp.search.setText("director approval")
pump(1.2)
check(kp.stack.currentWidget() is kp.results and kp.results.count() >= 1 and "approval" in kp.results.item(0).text().lower(), "searching shows matching passages")
kp._hit_selected(0)
check("director" in kp.p_text.toPlainText().lower(), "a match shows its passage")
shot(win, "f20-knowledge")
kp.search.setText("")
pump(0.8)
kp.table.selectRow(0)
pump(0.5)
kp.remove_current()
pump(1.0)
check(kp.table.rowCount() == 2, "removing a document works")
sid = api.call("POST", "/api/knowledge/upload", json_body={"filename": "faq.txt", "data_b64": base64.b64encode(b"Office hours: nine to five.").decode()})["id"]
check(eng.knowledge.search("office hours")[0]["name"] == "faq.txt", "uploaded text is searchable")
from ui.pages_knowledge import NoteDialog  # noqa: E402
nd = NoteDialog(win)
nd.name.setText("x")
check(nd.text.toPlainText() == "", "the note dialog starts empty")

# ---- 2. Automations ------------------------------------------------------------------------------------------------------------------
win.select("page:routines")
pump(1.0)
ap = win.pages["routines"]
check([ap.tabs.tabText(i) for i in range(ap.tabs.count())] == ["Routines", "Workflows", "Triggers"], "Automations has Routines, Workflows and Triggers")
from ui.pages_automations import RunDialog, TriggerDialog, WebhookShown, WorkflowDialog  # noqa: E402
wd = WorkflowDialog(api, store, None, win)
check(len(wd.rows) == 2, "a new workflow starts with two steps")
wd.name.setText("Weekly brief")
wd.desc.setText("Research a topic, then write it up")
wd.rows[0].bot.setCurrentIndex(wd.rows[0].bot.findData(research["id"]))
wd.rows[0].text.setPlainText("Research {{input}} and list three findings.")
wd.rows[1].bot.setCurrentIndex(wd.rows[1].bot.findData(writer["id"]))
wd.rows[1].text.setPlainText("Turn {{previous}} into a short brief.")
wd._move(wd.rows[1], -1)
check(wd.rows[0].bot.currentData() == writer["id"], "steps can be reordered")
wd._move(wd.rows[1], -1)
shot(wd, "f20-dialog-workflow")
wd.save()
pump(1.2)
wf = eng.workflows.list()
check(len(wf) == 1 and [s["bot_name"] for s in wf[0]["steps"]] == ["Researcher", "Writer"], f"saving the dialog creates the workflow ({[s['bot_name'] for s in wf[0]['steps']] if wf else None})")
ap.tabs.setCurrentWidget(ap.workflows)
pump(1.2)
wt = ap.workflows
check(wt.table.rowCount() == 1 and "Researcher → Writer" in wt.table.item(0, 1).text(), "the Workflows tab shows the chain")
wt.table.selectRow(0)
eng.workflows.run(wf[0]["id"], "whales")
pump(4.0)
runs = eng.workflows.runs(wf[0]["id"])
check(runs and runs[0]["status"] == "ok", f"the workflow ran ({runs[0]['status'] if runs else None})")
wt.load()
pump(1.2)
check(wt.runs.rowCount() == 1 and "ok" in wt.runs.item(0, 2).text(), "the run history lists it")
wt.runs.selectRow(0)
pump(0.5)
check("Step 2" in wt.detail.toPlainText() and "Done:" in wt.detail.toPlainText(), "selecting a run shows what each step produced")
shot(win, "f20-workflows")
rd = RunDialog(wf[0], win)
check(rd.dry.isChecked() is False, "the run dialog opens")
dups = len(eng.workflows.list())
ap.tabs.setCurrentWidget(ap.triggers)
pump(1.0)
td = TriggerDialog(api, store, "webhook", None, eng.workflows.list(), win)
td.name.setText("Order form")
td.prompt.setPlainText("A customer ordered: {{payload}}")
check(td.target.count() == 4, "a trigger can run any Bot or workflow")
td.save()
pump(1.2)
check(td.created is not None and td.created.get("url_path", "").startswith("/hooks/"), "creating a webhook returns its secret address once")
shown = WebhookShown(api.conn.base + td.created["url_path"], "Order form", win)
shot(shown, "f20-dialog-webhook")
check("/hooks/" in shown.box.toPlainText(), "the address is shown with a copy button")
fd = TriggerDialog(api, store, "folder", None, [], win)
fd.name.setText("Invoices")
fd.folder.setText("docs")
fd.pattern.setText("*.pdf")
fd.prompt.setPlainText("A new invoice arrived: {{file}}")
fd.save()
pump(1.2)
tt = ap.triggers
tt.load()
pump(1.2)
check(tt.table.rowCount() == 2, f"the Triggers tab lists both triggers ({tt.table.rowCount()})")
tt.table.selectRow(0)
tt.fire()
pump(3.5)
check(any(r["status"] == "ok" for r in eng.triggers.runs(limit=5)), "Test fire runs the trigger")
shot(win, "f20-triggers")
ap.tabs.setCurrentWidget(ap.routines)
pump(0.5)
shot(win, "f20-automations")

# ---- 3. Activity and Insights --------------------------------------------------------------------------------------------------------
win.select("page:log")
pump(1.0)
act = win.pages["log"]
check([act.tabs.tabText(i) for i in range(act.tabs.count())] == ["Action log", "Insights"], "Activity has the Action log and Insights")
act.tabs.setCurrentWidget(act.insights)
pump(1.8)
ins = act.insights
check(ins.c_tasks[1].text() not in ("0", "?"), f"Insights counts tasks ({ins.c_tasks[1].text()})")
check("%" in ins.c_ok[1].text(), f"it shows how often tasks finish well ({ins.c_ok[1].text()})")
check(len(ins.daily.data) == 7 and sum(ins.hours.hours) > 0, "the daily chart and the hours strip have data")
check(ins.bots.rowCount() >= 3 and ins.tools.rowCount() >= 3, "it ranks Bots and tools")
check(ins.problems.count() >= 1 and "fs_read" in ins.problems.item(0).text(), "it lists recent problems")
ins.set_days(30)
pump(1.5)
check(len(ins.daily.data) == 30, "the range can be changed to 30 days")
ins.set_days(7)
pump(1.2)
ins.bot.setCurrentIndex(ins.bot.findData(research["id"]))
pump(1.2)
check(ins.bots.rowCount() == 1, "it can focus on one Bot")
ins.bot.setCurrentIndex(0)
pump(1.0)
shot(win, "f20-insights")
act.tabs.setCurrentWidget(act.log)
pump(1.2)
check(act.log.table.rowCount() > 0, "the action log is still there")

# ---- 4. File history -----------------------------------------------------------------------------------------------------------------
win.select("page:files")
pump(1.5)
fp = win.pages["files"]
fp.search.setText("report")
pump(1.2)
check(fp.list.count() >= 1, "Files finds the report")
fp.list.setCurrentRow(0)
pump(1.0)
from ui.pages_files import HistoryDialog  # noqa: E402
hd = HistoryDialog(api, "shared/report.md", win)
pump(1.5)
check(len(hd.versions) == 1 and "Draft one" in hd.text.toPlainText(), "the history dialog lists the earlier version and shows it")
shot(hd, "f20-file-history")
hd.restore_btn.click()
pump(1.5)
check("Draft one" in (ws / "shared" / "report.md").read_text(encoding="utf-8"), "restoring puts the earlier version back")
check(len(eng.filehistory.versions("shared/report.md")) == 2, "and keeps the version it replaced")
fp.search.setText("")
pump(0.8)
fp.set_mode("deleted")
pump(1.5)
check(fp.list.count() == 1 and "old-notes" in fp.list.item(0).text() and "Researcher" in fp.list.item(0).text(), f"the Deleted view lists the deleted file and who deleted it ({fp.list.item(0).text()!r})")
fp.list.setCurrentRow(0)
pump(1.2)
check("notes that will be deleted" in fp.p_text.toPlainText() and fp.restore_btn.isEnabled(), "its last contents can be read before restoring")
shot(win, "f20-files-deleted")
fp.restore_deleted()
pump(1.5)
check((ws / "shared" / "old-notes.txt").exists(), "Restore brings the deleted file back")
fp.set_mode("recent")
pump(0.5)

# ---- 5. Settings: channels, API access, diagnostics, backup model -----------------------------------------------------------------------
win.select("page:settings")
pump(1.5)
sp = win.pages["settings"]
tabs = [sp.tabs.nav.item(i).text() for i in range(sp.tabs.nav.count())]
check(tabs[-3:] == ["API access", "Diagnostics", "App"] or ("API access" in tabs and "Diagnostics" in tabs), f"Settings has API access and Diagnostics ({tabs})")
api.call("POST", "/api/channels", json_body={"kind": "slack", "name": "Team Slack", "secret": "https://hooks.example.test/slack/abc"})
sp.tabs.setCurrentIndex(tabs.index("Notifications"))
sp.channels_panel.load()
pump(1.2)
check(sp.channels_panel.list.count() == 1 and "Team Slack" in sp.channels_panel.list.item(0).text(), "Notifications lists the channel")
shot(win, "f20-channels")
from ui.settings_extra import ChannelDialog, TokenDialog  # noqa: E402
cd = ChannelDialog(api, sp.channels_panel.meta, None, win)
cd.kind.setCurrentIndex(cd.kind.findData("email"))
check(cd.host.isVisibleTo(cd) and not cd.chat.isVisibleTo(cd), "the channel dialog shows the fields for the chosen type")
shot(cd, "f20-dialog-channel")
sp.tabs.setCurrentIndex(tabs.index("Models"))
pump(0.8)
sp.backup_panel.prov.setCurrentIndex(sp.backup_panel.prov.findData("openai"))
sp.backup_panel.model.setText("gpt-4.1-mini")
sp.backup_panel.save()
pump(1.2)
check(eng.settings.get("fallback.profile") == "openai" and eng.settings.get("fallback.model") == "gpt-4.1-mini", "the app-wide backup model is saved")
shot(win, "f20-models-backup")
sp.tabs.setCurrentIndex(tabs.index("API access"))
pump(1.0)
td_ = TokenDialog(api, win)
td_.name.setText("Home dashboard")
td_.scope.setCurrentIndex(td_.scope.findData("read"))
td_.save()
pump(1.2)
check(td_.created is not None and td_.created["token"].startswith("gbt_"), "creating a token returns it once")
from ui.settings_extra import TokenShown  # noqa: E402
shot(TokenShown(td_.created["token"], "Home dashboard", api.conn.base, win), "f20-dialog-token")
from ui.settings_extra import ApiAccessPanel  # noqa: E402
panel = sp.tabs.stack.currentWidget().findChild(ApiAccessPanel)
panel.load()
pump(1.2)
check(panel.table.rowCount() == 1 and "Home dashboard" in panel.table.item(0, 0).text(), "API access lists the token (without the secret)")
check(td_.created["token"] not in texts(sp) and td_.created["token"] not in panel.table.item(0, 0).text(), "the token itself is not shown in the list")
shot(win, "f20-api-access")
sp.tabs.setCurrentIndex(tabs.index("Diagnostics"))
pump(2.5)
from ui.settings_extra import DiagnosticsPanel  # noqa: E402
dp = sp.tabs.stack.currentWidget().findChild(DiagnosticsPanel)
check(dp.box.count() >= 14, f"Diagnostics shows its checks ({dp.box.count()})")
check(dp.summary.text() != "", f"and a summary ({dp.summary.text()!r})")
shot(win, "f20-diagnostics")

# ---- 6. The Bot editor: a backup model per Bot ----------------------------------------------------------------------------------------
from ui.dialogs import BotEditor  # noqa: E402
bd = BotEditor(api, store, writer["id"], win)
if bd is not None:
    bd.fb_prov.setCurrentIndex(bd.fb_prov.findData("openrouter"))
    bd.fb_model.setText("anthropic/claude-haiku")
    bd.save()
    pump(1.5)
    b = eng.bots.get(writer["id"])
    check((b["fallback_profile"], b["fallback_model"]) == ("openrouter", "anthropic/claude-haiku"), "a Bot's own backup model is saved from its editor")

# ---- 7. Conversation branches -------------------------------------------------------------------------------------------------------------
th = eng.threads.main_thread(inbox["id"])
q1 = eng.threads.add(th["id"], "user", "user", "What is on my calendar?")
a1 = eng.threads.add(th["id"], inbox["id"], "assistant", "Two meetings today.")
q2 = eng.threads.add(th["id"], "user", "user", "Move the first one to 4pm")
win.show_bot(inbox["id"], th["id"])
pump(1.5)
before = len(eng.threads.list_for_bot(inbox["id"]))
win.chat._branch(q2, "Move the first one to 5pm instead")
pump(3.0)
after = eng.threads.list_for_bot(inbox["id"])
check(len(after) == before + 1, f"branching creates a new conversation ({before} -> {len(after)})")
check(win.chat.thread_id != th["id"], "and opens it")
newest = after[0]
users = [i["text"] for i in eng.threads.display(newest["id"]) if i["type"] == "user"]
check(users == ["What is on my calendar?", "Move the first one to 5pm instead"], f"the branch has the earlier turns and the new wording ({users})")
check(any("Move the first one to 4pm" in i.get("text", "") for i in eng.threads.display(th["id"])), "the original conversation is unchanged")
shot(win, "f20-branch")

print("FAIL: " + "; ".join(fails) if fails else "ALL OK")
sys.stdout.flush()
ev.stop()
eng.stop()
os._exit(1 if fails else 0)
