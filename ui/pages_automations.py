"""Automations: everything your Bots do without being asked. Routines (on a schedule), Workflows (a pipeline of Bots) and Triggers
(when a webhook is called or a file appears)."""
from __future__ import annotations

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QLineEdit, QMessageBox, QPlainTextEdit, QScrollArea, QSplitter,
                               QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from . import theme
from .api import Api
from .pages_inbox import fill_row, fmt_time, make_table
from .pages_routines import PRESETS, RoutinesPage, describe_cron
from .store import Store
from .widgets import PageHeader, button, clear_layout, label, page_layout

PLACEHOLDERS_WORKFLOW = "Use {{input}} for what you give the workflow when you run it, {{previous}} for the last step's result, and {{step1}}, {{step2}}… for any earlier step."
PLACEHOLDERS_WEBHOOK = "Use {{payload}} for what was sent, {{json.name}} for a field of a JSON body, {{time}} for the time. Whatever arrives is passed to the Bot as untrusted data."
PLACEHOLDERS_FOLDER = "Use {{file}} for the file's path in the workspace, {{filename}} for its name and {{folder}} for the watched folder."


def took(r: dict) -> str:
    return f"{int(r['ended_at'] - r['started_at'])}s" if r.get("ended_at") else "…"


def status_text(s: str) -> str:
    return {"ok": "✓ ok", "failed": "✗ failed", "error": "✗ error", "stopped": "stopped", "skipped": "skipped", "running": "running…"}.get(s, s)


# ===================================================================================================== workflows
class StepRow(QFrame):
    removeRequested = Signal(object)
    moveRequested = Signal(object, int)

    def __init__(self, store: Store, step: dict | None = None):
        super().__init__()
        self.setProperty("card", "true")
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(6)
        top = QHBoxLayout()
        self.num = label("Step 1", h2=True, wrap=False)
        self.bot = QComboBox()
        for b in store.bots:
            self.bot.addItem(f"{b.get('emoji') or '🤖'}  {b['name']}", b["id"])
        if step:
            self.bot.setCurrentIndex(max(0, self.bot.findData(step["bot_id"])))
        top.addWidget(self.num)
        top.addWidget(self.bot, 1)
        top.addWidget(button("↑", flat=True, on=lambda: self.moveRequested.emit(self, -1), tip="Move up"))
        top.addWidget(button("↓", flat=True, on=lambda: self.moveRequested.emit(self, 1), tip="Move down"))
        top.addWidget(button("✕", flat=True, on=lambda: self.removeRequested.emit(self), tip="Remove this step"))
        v.addLayout(top)
        self.text = QPlainTextEdit(step["instruction"] if step else "")
        self.text.setPlaceholderText("What should this Bot do? e.g. “Summarise {{previous}} in five bullet points.”")
        self.text.setFixedHeight(76)
        v.addWidget(self.text)

    def set_number(self, n: int) -> None:
        self.num.setText(f"Step {n}")

    def value(self) -> dict:
        return {"bot_id": self.bot.currentData(), "instruction": self.text.toPlainText().strip()}


class WorkflowDialog(QDialog):
    def __init__(self, api: Api, store: Store, workflow: dict | None = None, parent=None):
        super().__init__(parent)
        self.api, self.store, self.workflow = api, store, workflow
        self.setWindowTitle("Edit workflow" if workflow else "New workflow")
        self.resize(660, 700)
        v = QVBoxLayout(self)
        v.addWidget(label("A workflow passes work from Bot to Bot. Each step is an ordinary task, so approvals, budgets and memory all work as usual. If a step fails, the workflow stops there.", muted=True))
        f = QFormLayout()
        self.name = QLineEdit(workflow["name"] if workflow else "")
        self.name.setPlaceholderText("e.g. Weekly competitor brief")
        self.desc = QLineEdit(workflow["description"] if workflow else "")
        f.addRow("Name", self.name)
        f.addRow("About", self.desc)
        v.addLayout(f)
        v.addWidget(label("Steps", h2=True))
        v.addWidget(label(PLACEHOLDERS_WORKFLOW, faint=True))
        self.rows: list[StepRow] = []
        holder = QWidget()
        self.steps_l = QVBoxLayout(holder)
        self.steps_l.setContentsMargins(0, 0, 8, 0)
        self.steps_l.setSpacing(8)
        self.steps_l.addStretch(1)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        sc.setWidget(holder)
        v.addWidget(sc, 1)
        v.addWidget(button("Add a step", icon="plus", on=lambda: self.add_step()))
        g = QFormLayout()
        self.sched = QComboBox()
        self.sched.addItem("No schedule: run it by hand or from a trigger", "")
        for n, c in PRESETS:
            self.sched.addItem(n, c if c else "__custom")
        self.cron = QLineEdit(workflow["cron"] if workflow else "")
        self.cron.setPlaceholderText("minute hour day month weekday")
        self.sched.currentIndexChanged.connect(self._sched_changed)
        self.enabled = QCheckBox("Enabled (the schedule only runs while this is on)")
        self.enabled.setChecked(workflow["enabled"] if workflow else True)
        g.addRow("Schedule", self.sched)
        g.addRow("Cron", self.cron)
        g.addRow("", self.enabled)
        v.addLayout(g)
        self.err = label("")
        v.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        for st in (workflow["steps"] if workflow else [{}, {}]):
            self.add_step(st or None)
        cron = workflow["cron"] if workflow else ""
        idx = self.sched.findData(cron) if cron else 0
        self.sched.setCurrentIndex(idx if idx >= 0 else self.sched.count() - 1)
        self._sched_changed()

    def _sched_changed(self) -> None:
        d = self.sched.currentData()
        self.cron.setEnabled(d == "__custom")
        if d and d != "__custom":
            self.cron.setText(d)
        elif not d:
            self.cron.clear()

    def add_step(self, step: dict | None = None) -> None:
        row = StepRow(self.store, step)
        row.removeRequested.connect(self._remove)
        row.moveRequested.connect(self._move)
        self.rows.append(row)
        self.steps_l.insertWidget(self.steps_l.count() - 1, row)
        self._renumber()

    def _remove(self, row: StepRow) -> None:
        if len(self.rows) <= 1:
            self.err.setText("A workflow needs at least one step.")
            return
        self.rows.remove(row)
        row.setParent(None)
        row.deleteLater()
        self._renumber()

    def _move(self, row: StepRow, d: int) -> None:
        i = self.rows.index(row)
        j = i + d
        if 0 <= j < len(self.rows):
            self.rows[i], self.rows[j] = self.rows[j], self.rows[i]
            for r in self.rows:
                self.steps_l.removeWidget(r)
            for k, r in enumerate(self.rows):
                self.steps_l.insertWidget(k, r)
            self._renumber()

    def _renumber(self) -> None:
        for i, r in enumerate(self.rows, 1):
            r.set_number(i)

    def save(self) -> None:
        body = {"name": self.name.text().strip(), "description": self.desc.text().strip(), "steps": [r.value() for r in self.rows], "enabled": self.enabled.isChecked(),
                "cron": self.cron.text().strip() if self.sched.currentData() else ""}
        fail = lambda e: self.err.setText(e)
        if self.workflow:
            self.api.put(f"/api/workflows/{self.workflow['id']}", body, lambda _: self.accept(), fail)
        else:
            self.api.post("/api/workflows", body, lambda _: self.accept(), fail)


class RunDialog(QDialog):
    def __init__(self, workflow: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Run “{workflow['name']}”")
        self.resize(520, 300)
        v = QVBoxLayout(self)
        v.addWidget(label("What should the first step work on? It is available to every step as {{input}}. Leave it empty if the workflow needs nothing.", muted=True))
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("e.g. the topic, a link, or a few lines of notes")
        v.addWidget(self.input, 1)
        self.dry = QCheckBox("Dry run: refuse consequential actions (use while testing)")
        v.addWidget(self.dry)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Run")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)


class WorkflowsTab(QWidget):
    openThread = Signal(str, str)

    def __init__(self, api: Api, store: Store):
        super().__init__()
        self.api, self.store = api, store
        self.workflows: list[dict] = []
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 12, 0, 0)
        v.setSpacing(12)
        v.addWidget(label("A pipeline of Bots: the first does its part, its result goes to the next, and so on. Run one by hand, on a schedule, or from a trigger.", muted=True))
        bar = QHBoxLayout()
        bar.addWidget(button("New workflow…", primary=True, on=self.new))
        bar.addWidget(button("Edit…", on=self.edit))
        bar.addWidget(button("Run…", icon="play", on=self.run))
        bar.addWidget(button("Stop", on=self.stop))
        bar.addWidget(button("Enable / disable", on=self.toggle))
        bar.addWidget(button("Delete", danger=True, on=self.delete))
        bar.addStretch(1)
        v.addLayout(bar)
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = make_table(["Workflow", "Steps", "Schedule", "Next run", "Last run", "On"], 1)
        self.table.itemSelectionChanged.connect(self.load_runs)
        split.addWidget(self.table)
        lower = QWidget()
        lv = QVBoxLayout(lower)
        lv.setContentsMargins(0, 8, 0, 0)
        lv.addWidget(label("Run history", h2=True))
        self.runs = make_table(["Started", "Workflow", "Status", "Took", "Result"], 4)
        self.runs.itemSelectionChanged.connect(self.show_run)
        self.runs.cellDoubleClicked.connect(lambda r, c: self.open_run(r))
        lv.addWidget(self.runs, 2)
        self.detail = QTextBrowser()
        self.detail.setMaximumHeight(170)
        self.detail.setPlaceholderText("Select a run to read what every step produced. Double-click to open the last step's chat.")
        lv.addWidget(self.detail)
        split.addWidget(lower)
        split.setSizes([240, 360])
        v.addWidget(split, 1)
        store.event.connect(lambda ev: ev.get("type") in ("workflows", "workflow_run") and self.isVisible() and self.load())

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.load()

    def load(self) -> None:
        def ok(d: dict) -> None:
            sel = self.selected_id()
            self.workflows = d["workflows"]
            self.table.setRowCount(0)
            for w in self.workflows:
                chain = " → ".join(s["bot_name"] for s in w["steps"])
                nxt = fmt_time(w["next_run_at"]) if w["enabled"] and w["next_run_at"] else "—"
                last = "running…" if w["running"] else (status_text(w["last_status"]) if w["last_status"] else "never")
                row = fill_row(self.table, [w["name"], chain, describe_cron(w["cron"]) if w["cron"] else "By hand", nxt, last, "✓" if w["enabled"] else ""], w)
                if w["id"] == sel:
                    self.table.selectRow(row)
            self.table.resizeRowsToContents()
            if self.table.currentRow() < 0 and self.workflows:
                self.table.selectRow(0)
            self.load_runs()
        self.api.get("/api/workflows", ok)

    def selected(self) -> dict | None:
        r = self.table.currentRow()
        return self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) if r >= 0 and self.table.item(r, 0) else None

    def selected_id(self) -> str:
        s = self.selected()
        return s["id"] if s else ""

    def load_runs(self) -> None:
        s = self.selected()

        def ok(d: dict) -> None:
            self.runs.setRowCount(0)
            for r in d["runs"]:
                fill_row(self.runs, [fmt_time(r["started_at"]), r.get("workflow_name") or "", status_text(r["status"]), took(r), (r["result"] or r["error"] or "")[:160].replace("\n", " ")], r)
            self.runs.resizeRowsToContents()
        self.api.get(f"/api/workflows/{s['id']}/runs" if s else "/api/workflows", (lambda d: ok(d)) if s else (lambda d: ok({"runs": d["runs"]})))

    def show_run(self) -> None:
        r = self.runs.currentRow()
        if r < 0:
            return
        run = self.runs.item(r, 0).data(Qt.ItemDataRole.UserRole)
        parts = []
        if run.get("input"):
            parts.append(f"**Input:** {run['input'][:400]}")
        for st in run["steps"]:
            head = f"### Step {st['n']} · {st['bot_name']} · {status_text(st['status'])}"
            body = st.get("result") or (f"_{st['error']}_" if st.get("error") else "")
            parts.append(f"{head}\n\n{body}")
        if run.get("error"):
            parts.append(f"**Stopped:** {run['error']}")
        self.detail.setMarkdown("\n\n".join(parts) or "(no steps ran)")

    def open_run(self, row: int) -> None:
        run = self.runs.item(row, 0).data(Qt.ItemDataRole.UserRole)
        for st in reversed(run["steps"]):
            if st.get("thread_id"):
                self.openThread.emit(st["thread_id"], st["bot_id"])
                return

    def new(self) -> None:
        if not self.store.bots:
            QMessageBox.information(self, "Workflows", "Create a Bot first.")
            return
        if WorkflowDialog(self.api, self.store, None, self).exec():
            self.load()

    def edit(self) -> None:
        s = self.selected()
        if s and WorkflowDialog(self.api, self.store, s, self).exec():
            self.load()

    def run(self) -> None:
        s = self.selected()
        if not s:
            return
        d = RunDialog(s, self)
        if d.exec():
            self.api.post(f"/api/workflows/{s['id']}/run", {"input": d.input.toPlainText().strip(), "dry_run": d.dry.isChecked()}, lambda _r: self.load(),
                          lambda e: QMessageBox.warning(self, "Could not run", e))

    def stop(self) -> None:
        s = self.selected()
        if s:
            self.api.post(f"/api/workflows/{s['id']}/stop", {}, lambda _r: self.load())

    def toggle(self) -> None:
        s = self.selected()
        if s:
            self.api.put(f"/api/workflows/{s['id']}", {"enabled": not s["enabled"]}, lambda _r: self.load())

    def delete(self) -> None:
        s = self.selected()
        if s and QMessageBox.question(self, "Delete", f"Delete workflow “{s['name']}” and its history?") == QMessageBox.StandardButton.Yes:
            self.api.delete(f"/api/workflows/{s['id']}", lambda _r: self.load())


# ===================================================================================================== triggers
class WebhookShown(QDialog):
    """The address of a new (or regenerated) webhook. It is shown once: only a hash is kept."""

    def __init__(self, url: str, name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Your webhook address")
        self.resize(620, 380)
        v = QVBoxLayout(self)
        v.addWidget(label(f"“{name}” is ready. Anything that POSTs to this address starts the task. Copy it now: it contains a secret and is not shown again (you can make a new one later).", muted=True))
        self.box = QPlainTextEdit(url)
        self.box.setReadOnly(True)
        self.box.setFont(theme.mono())
        self.box.setFixedHeight(70)
        v.addWidget(self.box)
        row = QHBoxLayout()
        self.copy_btn = button("Copy address", primary=True, icon="copy", on=self._copy)
        row.addWidget(self.copy_btn)
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(label("Try it from PowerShell:", faint=True))
        ex = QPlainTextEdit(f"Invoke-RestMethod -Method Post -Uri \"{url}\" -ContentType application/json -Body '{{\"hello\": \"world\"}}'")
        ex.setReadOnly(True)
        ex.setFont(theme.mono())
        ex.setFixedHeight(70)
        v.addWidget(ex)
        v.addWidget(label("The address works on this PC. To call it from other devices, use this PC's network address instead of 127.0.0.1 (turn on phone access in Settings > Mobile) and keep it private.", faint=True))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        bb.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        v.addWidget(bb)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.box.toPlainText())
        self.copy_btn.setText("Copied")


class TriggerDialog(QDialog):
    def __init__(self, api: Api, store: Store, kind: str, trigger: dict | None = None, workflows: list[dict] | None = None, parent=None):
        super().__init__(parent)
        self.api, self.store, self.kind, self.trigger = api, store, kind, trigger
        self.created: dict | None = None
        self.setWindowTitle(("Edit " if trigger else "New ") + ("webhook" if kind == "webhook" else "folder watch"))
        self.resize(600, 520)
        v = QVBoxLayout(self)
        v.addWidget(label("A webhook gives you an address that anything can POST to." if kind == "webhook" else
                          "The workspace folder is checked every few seconds. When a new or changed file matches, the task starts.", muted=True))
        f = QFormLayout()
        self.name = QLineEdit(trigger["name"] if trigger else "")
        self.name.setPlaceholderText("e.g. New order form" if kind == "webhook" else "e.g. Invoices dropbox")
        self.target = QComboBox()
        wf_target = bool(trigger and trigger.get("workflow_id"))
        if not wf_target:
            for b in store.bots:
                self.target.addItem(f"{b.get('emoji') or '🤖'}  {b['name']}", ("bot", b["id"]))
        if not trigger or wf_target:
            for w in workflows or []:
                self.target.addItem(f"⛓  Workflow: {w['name']}", ("wf", w["id"]))
        if trigger:
            want = ("wf", trigger["workflow_id"]) if wf_target else ("bot", trigger["bot_id"])
            self.target.setCurrentIndex(max(0, self.target.findData(want)))
            self.target.setEnabled(not wf_target)
        self.prompt = QPlainTextEdit(trigger["prompt"] if trigger else "")
        self.prompt.setPlaceholderText("e.g. A customer submitted this form: {{payload}}. Add them to the tracker and reply with a short confirmation." if kind == "webhook" else
                                       "e.g. A new invoice arrived: {{file}}. Read it, extract the total and due date, and add a line to shared/invoices.csv.")
        f.addRow("Name", self.name)
        f.addRow("Runs", self.target)
        f.addRow("Instructions", self.prompt)
        self.folder = QLineEdit(trigger["folder"] if trigger else "")
        self.folder.setPlaceholderText("a folder inside the workspace, e.g. inbox")
        self.pattern = QLineEdit(trigger["pattern"] if trigger else "*")
        self.pattern.setPlaceholderText("*.pdf, *.csv, report-*")
        if kind == "folder":
            f.addRow("Watch folder", self.folder)
            f.addRow("File pattern", self.pattern)
            self.folder.setEnabled(not trigger)
        self.dry = QCheckBox("Dry run: refuse consequential actions (use while testing)")
        self.dry.setChecked(bool(trigger and trigger["dry_run"]))
        f.addRow("", self.dry)
        v.addLayout(f)
        v.addWidget(label(PLACEHOLDERS_WEBHOOK if kind == "webhook" else PLACEHOLDERS_FOLDER, faint=True))
        self.err = label("")
        v.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def save(self) -> None:
        tgt = self.target.currentData()
        if not tgt:
            self.err.setText("Create a Bot first.")
            return
        body = {"kind": self.kind, "name": self.name.text().strip(), "prompt": self.prompt.toPlainText().strip(), "dry_run": self.dry.isChecked(),
                "folder": self.folder.text().strip(), "pattern": self.pattern.text().strip() or "*"}
        if tgt[0] == "wf":
            body["workflow_id"] = tgt[1]
        else:
            body["bot_id"] = tgt[1]
        fail = lambda e: self.err.setText(e)
        if self.trigger:
            patch = {k: body[k] for k in ("name", "prompt", "dry_run", "pattern")}
            if tgt[0] == "bot":
                patch["bot_id"] = tgt[1]
            self.api.put(f"/api/triggers/{self.trigger['id']}", patch, lambda r: (setattr(self, "created", r), self.accept()), fail)
        else:
            self.api.post("/api/triggers", body, lambda r: (setattr(self, "created", r), self.accept()), fail)


class TriggersTab(QWidget):
    openThread = Signal(str, str)

    def __init__(self, api: Api, store: Store):
        super().__init__()
        self.api, self.store = api, store
        self.triggers: list[dict] = []
        self.workflows: list[dict] = []
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 12, 0, 0)
        v.setSpacing(12)
        v.addWidget(label("Start a Bot (or a workflow) when something happens: a webhook is called, or a file appears in a workspace folder.", muted=True))
        bar = QHBoxLayout()
        bar.addWidget(button("New webhook…", primary=True, icon="link", on=lambda: self.new("webhook")))
        bar.addWidget(button("New folder watch…", icon="folder", on=lambda: self.new("folder")))
        bar.addWidget(button("Edit…", on=self.edit))
        bar.addWidget(button("Test fire", icon="play", on=self.fire))
        bar.addWidget(button("New address…", on=self.regenerate))
        bar.addWidget(button("Enable / disable", on=self.toggle))
        bar.addWidget(button("Delete", danger=True, on=self.delete))
        bar.addStretch(1)
        v.addLayout(bar)
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = make_table(["Trigger", "Type", "Runs", "Fired", "Last result", "On"], 0)
        self.table.itemSelectionChanged.connect(self.load_runs)
        split.addWidget(self.table)
        lower = QWidget()
        lv = QVBoxLayout(lower)
        lv.setContentsMargins(0, 8, 0, 0)
        lv.addWidget(label("Recent firings", h2=True))
        self.runs = make_table(["Started", "Trigger", "Status", "Took", "Detail"], 4)
        self.runs.itemSelectionChanged.connect(self.show_run)
        self.runs.cellDoubleClicked.connect(lambda r, c: self.open_run(r))
        lv.addWidget(self.runs, 2)
        self.detail = QTextBrowser()
        self.detail.setMaximumHeight(140)
        self.detail.setPlaceholderText("Select a firing to read the Bot's result. Double-click to open its chat.")
        lv.addWidget(self.detail)
        split.addWidget(lower)
        split.setSizes([240, 360])
        v.addWidget(split, 1)
        store.event.connect(lambda ev: ev.get("type") in ("triggers", "trigger_run") and self.isVisible() and self.load())

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.api.get("/api/workflows", lambda d: setattr(self, "workflows", d["workflows"]))
        self.load()

    def load(self) -> None:
        def ok(d: dict) -> None:
            sel = self.selected_id()
            self.triggers = d["triggers"]
            self.table.setRowCount(0)
            for t in self.triggers:
                kind = "Webhook" if t["kind"] == "webhook" else f"Folder: {t['folder'] or '(workspace)'} · {t['pattern']}"
                last = "running…" if t["running"] else (status_text(t["last_status"]) if t["last_status"] else "never")
                row = fill_row(self.table, [t["name"], kind, t["bot_name"], t["fired"], last, "✓" if t["enabled"] else ""], t)
                if t["id"] == sel:
                    self.table.selectRow(row)
            self.table.resizeRowsToContents()
            if self.table.currentRow() < 0 and self.triggers:
                self.table.selectRow(0)
            self.load_runs()
        self.api.get("/api/triggers", ok)

    def selected(self) -> dict | None:
        r = self.table.currentRow()
        return self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) if r >= 0 and self.table.item(r, 0) else None

    def selected_id(self) -> str:
        s = self.selected()
        return s["id"] if s else ""

    def load_runs(self) -> None:
        s = self.selected()
        self.api.get(f"/api/triggers/{s['id']}/runs" if s else "/api/triggers", lambda d: self._fill_runs(d["runs"]))

    def _fill_runs(self, rows: list[dict]) -> None:
        self.runs.setRowCount(0)
        for r in rows:
            fill_row(self.runs, [fmt_time(r["started_at"]), r.get("trigger_name") or "", status_text(r["status"]), took(r), r["detail"] or ""], r)
        self.runs.resizeRowsToContents()

    def show_run(self) -> None:
        r = self.runs.currentRow()
        if r >= 0:
            run = self.runs.item(r, 0).data(Qt.ItemDataRole.UserRole)
            self.detail.setMarkdown(run["result"] or "(no result yet)")

    def open_run(self, row: int) -> None:
        run = self.runs.item(row, 0).data(Qt.ItemDataRole.UserRole)
        t = next((x for x in self.triggers if x["id"] == run["trigger_id"]), None)
        if run.get("thread_id") and t:
            self.openThread.emit(run["thread_id"], t["bot_id"])

    def new(self, kind: str) -> None:
        if not self.store.bots:
            QMessageBox.information(self, "Triggers", "Create a Bot first.")
            return
        d = TriggerDialog(self.api, self.store, kind, None, self.workflows, self)
        if d.exec():
            self.load()
            if d.created and d.created.get("url_path"):
                WebhookShown(self.api.conn.base + d.created["url_path"], d.created["name"], self).exec()

    def edit(self) -> None:
        s = self.selected()
        if s and TriggerDialog(self.api, self.store, s["kind"], s, self.workflows, self).exec():
            self.load()

    def fire(self) -> None:
        s = self.selected()
        if s:
            self.api.post(f"/api/triggers/{s['id']}/test", {"payload": json.dumps({"test": True, "from": "OpenGrokBot"}) if s["kind"] == "webhook" else ""}, lambda _r: self.load(),
                          lambda e: QMessageBox.warning(self, "Could not fire", e))

    def regenerate(self) -> None:
        s = self.selected()
        if not s or s["kind"] != "webhook":
            return
        if QMessageBox.question(self, "New address", "Make a new secret address? The old one stops working immediately.") != QMessageBox.StandardButton.Yes:
            return
        self.api.post(f"/api/triggers/{s['id']}/regenerate", {}, lambda r: (self.load(), WebhookShown(self.api.conn.base + r["url_path"], r["name"], self).exec()),
                      lambda e: QMessageBox.warning(self, "Could not change it", e))

    def toggle(self) -> None:
        s = self.selected()
        if s:
            self.api.put(f"/api/triggers/{s['id']}", {"enabled": not s["enabled"]}, lambda _r: self.load())

    def delete(self) -> None:
        s = self.selected()
        if s and QMessageBox.question(self, "Delete", f"Delete trigger “{s['name']}”?") == QMessageBox.StandardButton.Yes:
            self.api.delete(f"/api/triggers/{s['id']}", lambda _r: self.load())


# ===================================================================================================== the page
class AutomationsPage(QWidget):
    openThread = Signal(str, str)

    def __init__(self, api: Api, store: Store):
        super().__init__()
        v = page_layout(self, PageHeader("Automations", "Things your Bots do without being asked: on a schedule, as a pipeline of Bots, or when something happens."))
        self.tabs = QTabWidget()
        self.routines = RoutinesPage(api, store, embedded=True)
        self.workflows = WorkflowsTab(api, store)
        self.triggers = TriggersTab(api, store)
        self.tabs.addTab(self.routines, "Routines")
        self.tabs.addTab(self.workflows, "Workflows")
        self.tabs.addTab(self.triggers, "Triggers")
        v.addWidget(self.tabs, 1)
        for t in (self.routines, self.workflows, self.triggers):
            t.openThread.connect(self.openThread)
