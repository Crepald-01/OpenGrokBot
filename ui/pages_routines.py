"""Routines: skills saved as scheduled jobs, per Bot, with run history and results."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit, QMessageBox, QPlainTextEdit, QSplitter,
                               QTableWidget, QTextBrowser, QVBoxLayout, QWidget)

from .api import Api
from .pages_inbox import fill_row, fmt_time, make_table
from .store import Store
from .widgets import button, label, PageHeader, page_layout

PRESETS = [("Every weekday at 8:00", "0 8 * * 1-5"), ("Every day at 7:00", "0 7 * * *"), ("Overnight (2:00 every day)", "0 2 * * *"), ("Every hour", "0 * * * *"),
           ("Every Monday at 9:00", "0 9 * * 1"), ("First of the month at 9:00", "0 9 1 * *"), ("Custom cron…", "")]


def describe_cron(expr: str) -> str:
    for n, c in PRESETS:
        if c == expr:
            return n
    return expr


class RoutineDialog(QDialog):
    def __init__(self, api: Api, store: Store, routine: dict | None = None, parent=None):
        super().__init__(parent)
        self.api, self.store, self.routine = api, store, routine
        self.setWindowTitle("Edit routine" if routine else "New routine")
        self.resize(560, 520)
        v = QVBoxLayout(self)
        v.addWidget(label("A routine runs on its own on a schedule, even when the app window is closed (the background service keeps running). Example: “generate pipeline overnight”.", muted=True))
        f = QFormLayout()
        self.name = QLineEdit(routine["name"] if routine else "")
        self.bot = QComboBox()
        for b in store.bots:
            self.bot.addItem(f"{b['emoji']} {b['name']}", b["id"])
        if routine:
            self.bot.setCurrentIndex(max(0, self.bot.findData(routine["bot_id"])))
        self.skill = QComboBox()
        self.skill.addItem("(no skill, just the prompt)", "")
        self.prompt = QPlainTextEdit(routine["prompt"] if routine else "")
        self.prompt.setPlaceholderText("What should the Bot do on each run? E.g. “Generate tomorrow's pipeline report and post the summary to Slack #sales”")
        self.sched = QComboBox()
        for n, c in PRESETS:
            self.sched.addItem(n, c)
        self.cron = QLineEdit(routine["cron"] if routine else "0 8 * * 1-5")
        self.cron.setPlaceholderText("minute hour day month weekday")
        self.sched.currentIndexChanged.connect(lambda i: self.cron.setText(self.sched.currentData()) if self.sched.currentData() else None)
        if routine:
            idx = self.sched.findData(routine["cron"])
            self.sched.setCurrentIndex(idx if idx >= 0 else self.sched.count() - 1)
        else:
            self.sched.setCurrentIndex(0)
        self.notify = QComboBox()
        for t, k in (("Notify me when it finishes", "always"), ("Only notify on problems", "failures"), ("Never notify", "never")):
            self.notify.addItem(t, k)
        if routine:
            self.notify.setCurrentIndex(max(0, self.notify.findData(routine["notify"])))
        self.dry = QCheckBox("Dry run: refuse consequential actions (use while testing)")
        self.dry.setChecked(bool(routine and routine["dry_run"]))
        self.catch = QCheckBox("If the PC/service was off at the scheduled time, run once when it is back")
        self.catch.setChecked(bool(routine and routine["catch_up"]))
        f.addRow("Name", self.name)
        f.addRow("Bot", self.bot)
        f.addRow("Skill", self.skill)
        f.addRow("Prompt", self.prompt)
        f.addRow("Schedule", self.sched)
        f.addRow("Cron", self.cron)
        f.addRow("", self.notify)
        f.addRow("", self.dry)
        f.addRow("", self.catch)
        v.addLayout(f)
        self.err = label("")
        v.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        def skills(rows: list) -> None:
            for s in rows:
                if s["status"] == "active":
                    self.skill.addItem(s["name"], s["name"])
            if routine:
                self.skill.setCurrentIndex(max(0, self.skill.findData(routine["skill"])))
        api.get("/api/skills", skills)

    def save(self) -> None:
        body = {"bot_id": self.bot.currentData(), "name": self.name.text().strip() or "Routine", "cron": self.cron.text().strip(), "skill": self.skill.currentData() or "",
                "prompt": self.prompt.toPlainText().strip(), "dry_run": self.dry.isChecked(), "notify": self.notify.currentData(), "catch_up": self.catch.isChecked()}
        fail = lambda e: self.err.setText(e)
        if self.routine:
            self.api.put(f"/api/routines/{self.routine['id']}", body, lambda _: self.accept(), fail)
        else:
            self.api.post("/api/routines", body, lambda _: self.accept(), fail)


class RoutinesPage(QWidget):
    openThread = Signal(str, str)

    def __init__(self, api: Api, store: Store, embedded: bool = False):
        super().__init__()
        self.api, self.store = api, store
        self.routines: list[dict] = []
        v = page_layout(self, None if embedded else PageHeader("Routines", "Save a skill as a scheduled routine per Bot. Routines run unattended in the background service and ask you only when something needs approval."))
        if embedded:   # shown inside the Automations page, which has its own header
            v.setContentsMargins(0, 12, 0, 0)
            v.addWidget(label("Save a skill as a scheduled routine per Bot. Routines run unattended in the background service and ask you only when something needs approval.", muted=True))
        bar = QHBoxLayout()
        bar.addWidget(button("New routine…", primary=True, on=self.new))
        bar.addWidget(button("Edit…", on=self.edit))
        bar.addWidget(button("Run now", on=self.run_now))
        bar.addWidget(button("Enable / disable", on=self.toggle))
        bar.addWidget(button("Delete", danger=True, on=self.delete))
        bar.addStretch(1)
        v.addLayout(bar)
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = make_table(["Routine", "Bot", "Schedule", "Next run", "Last result", "On"], 0)
        self.table.itemSelectionChanged.connect(self.load_runs)
        split.addWidget(self.table)
        lower = QWidget()
        lv = QVBoxLayout(lower)
        lv.setContentsMargins(0, 8, 0, 0)
        lv.addWidget(label("Run history", h2=True))
        self.runs = make_table(["Started", "Routine", "Status", "Took", "Result"], 4)
        self.runs.itemSelectionChanged.connect(self.show_result)
        self.runs.cellDoubleClicked.connect(lambda r, c: self.open_run(r))
        lv.addWidget(self.runs, 2)
        self.result = QTextBrowser()
        self.result.setMaximumHeight(150)
        self.result.setPlaceholderText("Select a run to read its result. Double-click to open the full thread.")
        lv.addWidget(self.result)
        split.addWidget(lower)
        split.setSizes([280, 360])
        v.addWidget(split, 1)
        store.event.connect(lambda ev: ev.get("type") in ("routines", "routine_run") and self.isVisible() and self.load())

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.load()

    def load(self) -> None:
        def ok(rows: list) -> None:
            sel = self.selected_id()
            self.routines = rows
            self.table.setRowCount(0)
            import time
            for r in rows:
                nxt = fmt_time(r["next_run_at"]) if r["enabled"] and r["next_run_at"] else "—"
                last = ("running…" if r["running"] else r["last_status"] or "never")
                row = fill_row(self.table, [r["name"], r["bot_name"], describe_cron(r["cron"]), nxt, last, "✓" if r["enabled"] else ""], r)
                if r["id"] == sel:
                    self.table.selectRow(row)
            self.table.resizeRowsToContents()
            if self.table.currentRow() < 0 and rows:
                self.table.selectRow(0)
            self.load_runs()
        self.api.get("/api/routines", ok)

    def selected(self) -> dict | None:
        r = self.table.currentRow()
        return self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) if r >= 0 and self.table.item(r, 0) else None

    def selected_id(self) -> str:
        s = self.selected()
        return s["id"] if s else ""

    def load_runs(self) -> None:
        s = self.selected()

        def ok(rows: list) -> None:
            self.runs.setRowCount(0)
            for r in rows:
                took = f"{int(r['ended_at'] - r['started_at'])}s" if r.get("ended_at") else "…"
                fill_row(self.runs, [fmt_time(r["started_at"]), r["routine_name"] or "", r["status"], took, (r["result"] or r["error"] or "")[:200]], r)
            self.runs.resizeRowsToContents()
        self.api.get("/api/routine_runs", ok, params={"routine_id": s["id"]} if s else {})

    def show_result(self) -> None:
        r = self.runs.currentRow()
        if r >= 0:
            run = self.runs.item(r, 0).data(Qt.ItemDataRole.UserRole)
            self.result.setMarkdown(run["result"] or ("**Error:** " + run["error"] if run["error"] else "(no result)"))

    def open_run(self, row: int) -> None:
        run = self.runs.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if run.get("thread_id"):
            self.openThread.emit(run["thread_id"], run["bot_id"])

    def new(self) -> None:
        if not self.store.bots:
            QMessageBox.information(self, "Routines", "Create a Bot first.")
            return
        if RoutineDialog(self.api, self.store, None, self).exec():
            self.load()

    def edit(self) -> None:
        s = self.selected()
        if s and RoutineDialog(self.api, self.store, s, self).exec():
            self.load()

    def run_now(self) -> None:
        s = self.selected()
        if s:
            self.api.post(f"/api/routines/{s['id']}/run", {}, lambda _: self.load())

    def toggle(self) -> None:
        s = self.selected()
        if s:
            self.api.put(f"/api/routines/{s['id']}", {"enabled": not s["enabled"]}, lambda _: self.load())

    def delete(self) -> None:
        s = self.selected()
        if s and QMessageBox.question(self, "Delete", f"Delete routine “{s['name']}” and its history?") == QMessageBox.StandardButton.Yes:
            self.api.delete(f"/api/routines/{s['id']}", lambda _: self.load())
