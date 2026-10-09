"""Routines: skills saved as scheduled jobs, per Bot, with run history and results."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QPushButton, QSplitter, QTableWidget, QTextBrowser, QVBoxLayout, QWidget)

from . import icons, theme
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


# ------------------------------------------------------------------------------------------------ shared page pieces
class EmptyState(QWidget):
    """What a list shows when it has nothing yet: an icon, a short sentence and the one or two actions that start it."""

    def __init__(self, icon: str, title: str, text: str, actions: list[QPushButton] | None = None):
        super().__init__()
        p = theme.palette()
        v = QVBoxLayout(self)
        v.setContentsMargins(theme.dp(24), theme.dp(32), theme.dp(24), theme.dp(32))
        v.setSpacing(theme.dp(8))
        badge = QLabel()
        badge.setFixedSize(theme.dp(56), theme.dp(56))
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setPixmap(icons.pixmap(icon, p["accent"], theme.dp(26)))
        badge.setStyleSheet(f"background: {p['panel2']}; border: 1px solid {p['line']}; border-radius: {theme.dp(28)}px;")
        v.addWidget(badge, 0, Qt.AlignmentFlag.AlignHCenter)
        v.addSpacing(theme.dp(4))
        t = label(title, h2=True, wrap=False)
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(t)
        d = label(text, muted=True)
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        d.setFixedWidth(theme.dp(420))   # a fixed measure, so the wrapped text gets its full height
        text_row = QHBoxLayout()   # a row (not a centred column) so the wrapped text gets its full height
        text_row.addStretch(1)
        text_row.addWidget(d)
        text_row.addStretch(1)
        v.addLayout(text_row)
        if actions:
            v.addSpacing(theme.dp(8))
            row = QHBoxLayout()
            row.setSpacing(theme.dp(8))
            row.addStretch(1)
            for a in actions:
                row.addWidget(a)
            row.addStretch(1)
            v.addLayout(row)
        v.addStretch(1)


def set_empty(table: QTableWidget, empty: QWidget) -> None:
    """Show the table when it has rows, the empty state when it does not."""
    has_rows = table.rowCount() > 0
    table.setVisible(has_rows)
    empty.setVisible(not has_rows)


def table_with_empty(table: QTableWidget, empty: QWidget) -> QWidget:
    box = QWidget()
    v = QVBoxLayout(box)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(0)
    v.addWidget(table, 1)
    v.addWidget(empty, 1)
    return box


def heading(title: str, caption: str = "") -> QHBoxLayout:
    h = QHBoxLayout()
    h.setSpacing(theme.dp(12))
    h.addWidget(label(title, h2=True, wrap=False))
    if caption:
        h.addWidget(label(caption, muted=True, wrap=False))
    h.addStretch(1)
    return h


def toolbar(primary: list[QPushButton], secondary: list[QPushButton], danger: list[QPushButton] | None = None) -> QHBoxLayout:
    """One row of actions: the primary action first, a gap, the secondary ones, and destructive actions pushed to the right."""
    bar = QHBoxLayout()
    bar.setSpacing(theme.dp(8))
    for b in primary:
        bar.addWidget(b)
    if primary and secondary:
        bar.addSpacing(theme.dp(12))
    for b in secondary:
        bar.addWidget(b)
    bar.addStretch(1)
    for b in danger or []:
        bar.addWidget(b)
    return bar


# ------------------------------------------------------------------------------------------------ dialog
class RoutineDialog(QDialog):
    def __init__(self, api: Api, store: Store, routine: dict | None = None, parent=None):
        super().__init__(parent)
        self.api, self.store, self.routine = api, store, routine
        self.setWindowTitle("Edit routine" if routine else "New routine")
        self.resize(560, 520)
        v = QVBoxLayout(self)
        v.setSpacing(theme.dp(12))
        v.addWidget(label("A routine runs on its own on a schedule, even when the app window is closed (the background service keeps running). Example: “generate pipeline overnight”.", muted=True))
        f = QFormLayout()
        f.setVerticalSpacing(theme.dp(10))
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


# ------------------------------------------------------------------------------------------------ page
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
        new_btn = button("New routine…", primary=True, on=self.new)
        v.addLayout(toolbar([new_btn],
                            [button("Edit…", on=self.edit), button("Run now", on=self.run_now), button("Enable / disable", on=self.toggle)],
                            [button("Delete", danger=True, on=self.delete)]))
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = make_table(["Routine", "Bot", "Schedule", "Next run", "Last result", "On"], 0)
        self.table.itemSelectionChanged.connect(self.load_runs)
        self.table_empty = EmptyState("routines", "No routines yet",
                                      "A routine is a skill that runs on a schedule, for example “generate the pipeline report every weekday at 8:00”. It keeps running when the window is closed.",
                                      [button("New routine…", primary=True, on=self.new)])
        split.addWidget(table_with_empty(self.table, self.table_empty))
        lower = QWidget()
        lv = QVBoxLayout(lower)
        lv.setContentsMargins(0, theme.dp(16), 0, 0)
        lv.setSpacing(theme.dp(10))
        lv.addLayout(heading("Run history", "Newest first. Select a run to read its result, double-click to open its thread."))
        self.runs = make_table(["Started", "Routine", "Status", "Took", "Result"], 4)
        self.runs.itemSelectionChanged.connect(self.show_result)
        self.runs.cellDoubleClicked.connect(lambda r, c: self.open_run(r))
        self.runs_empty = EmptyState("history", "No runs yet", "Each run is listed here with its result. Select a routine and press Run now to try it.",
                                     [button("Run now", on=self.run_now)])
        lv.addWidget(table_with_empty(self.runs, self.runs_empty), 2)
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
            for r in rows:
                nxt = fmt_time(r["next_run_at"]) if r["enabled"] and r["next_run_at"] else "—"
                last = ("running…" if r["running"] else r["last_status"] or "never")
                row = fill_row(self.table, [r["name"], r["bot_name"], describe_cron(r["cron"]), nxt, last, "✓" if r["enabled"] else ""], r)
                if r["id"] == sel:
                    self.table.selectRow(row)
            self.table.resizeRowsToContents()
            set_empty(self.table, self.table_empty)
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
            set_empty(self.runs, self.runs_empty)
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
