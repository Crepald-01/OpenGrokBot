"""Inbox: what needs you (approvals, questions, hand-offs to you), the decision history, and handoffs between Bots."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QLineEdit, QPlainTextEdit, QScrollArea, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from .api import Api
from .store import Store
from .widgets import ApprovalCard, button, clear_layout, label, PageHeader, page_layout


def fmt_time(ts: float) -> str:
    return time.strftime("%b %d %H:%M", time.localtime(ts)) if ts else ""


def make_table(headers: list[str], stretch: int = -1) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.setShowGrid(False)
    t.setWordWrap(True)
    hh = t.horizontalHeader()
    for c in range(len(headers)):
        hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Stretch if c == stretch else QHeaderView.ResizeMode.ResizeToContents)
    hh.setStretchLastSection(stretch < 0)
    hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    t.verticalHeader().setDefaultSectionSize(38)
    return t


def fill_row(t: QTableWidget, values: list, data=None) -> int:
    r = t.rowCount()
    t.insertRow(r)
    for c, v in enumerate(values):
        it = QTableWidgetItem(str(v))
        if c == 0 and data is not None:
            it.setData(Qt.ItemDataRole.UserRole, data)
        t.setItem(r, c, it)
    return r


class HandoffDialog(QDialog):
    def __init__(self, api: Api, store: Store, parent=None):
        super().__init__(parent)
        self.api, self.store = api, store
        self.setWindowTitle("Hand off a task")
        self.resize(520, 380)
        v = QVBoxLayout(self)
        v.addWidget(label("Give a Bot a task with its context. It owns the task from here and keeps you updated.", muted=True))
        f = QFormLayout()
        self.bot = QComboBox()
        for b in store.bots:
            self.bot.addItem(f"{b['emoji']} {b['name']}", b["id"])
        self.title = QLineEdit()
        self.brief = QPlainTextEdit()
        self.brief.setPlaceholderText("Goal, context (or the project notes to read), constraints, definition of done")
        f.addRow("To", self.bot)
        f.addRow("Task", self.title)
        f.addRow("Brief", self.brief)
        v.addLayout(f)
        self.err = label("")
        v.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.go)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def go(self) -> None:
        if not self.title.text().strip():
            self.err.setText("Give the task a title.")
            return
        self.api.post("/api/handoffs", {"to_bot": self.bot.currentData(), "title": self.title.text().strip(), "brief": self.brief.toPlainText()},
                      lambda h: (self.accept()), lambda e: self.err.setText(e))


class InboxPage(QWidget):
    openBrowser = Signal(str)
    openThread = Signal(str, str)

    def __init__(self, api: Api, store: Store):
        super().__init__()
        self.api, self.store = api, store
        v = page_layout(self, PageHeader("Inbox", "Your Bots work end to end and only come back here when something needs you."))
        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)

        self.pending_box = QWidget()
        self.pending_l = QVBoxLayout(self.pending_box)
        self.pending_l.setSpacing(10)
        self.pending_l.addStretch(1)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(self.pending_box)
        self.tabs.addTab(sc, "Needs you")

        self.history = make_table(["When", "Bot", "Action", "Decision", "Decided by", "Note"], 2)
        self.history.cellDoubleClicked.connect(lambda r, c: self._open_hist(r))
        self.tabs.addTab(self.history, "History")

        hw = QWidget()
        hv = QVBoxLayout(hw)
        bar = QHBoxLayout()
        bar.addWidget(button("New handoff…", primary=True, on=self.new_handoff))
        bar.addWidget(button("Mark done", on=lambda: self._set("done")))
        bar.addWidget(button("Cancel handoff", on=lambda: self._set("cancelled")))
        bar.addStretch(1)
        hv.addLayout(bar)
        self.handoffs = make_table(["Task", "From → To", "Status", "Updated", "Result"], 4)
        hv.addWidget(self.handoffs)
        self.tabs.addTab(hw, "Handoffs")

        store.approvalsChanged.connect(self.render_pending)
        store.event.connect(self._event)
        self.tabs.currentChanged.connect(lambda _: self.reload())

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.render_pending()
        self.reload()

    def _event(self, ev: dict) -> None:
        if ev.get("type") in ("approval", "handoffs") and self.isVisible():
            self.reload()

    def render_pending(self) -> None:
        while self.pending_l.count() > 1:
            it = self.pending_l.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        pend = self.store.approvals
        if not pend:
            self.pending_l.insertWidget(0, label("Nothing needs your attention. ✓"))
        for a in pend:
            c = ApprovalCard(a, self.store.bot_name(a["bot_id"]))
            c.decided.connect(self._decide)
            c.openBrowser.connect(self.openBrowser.emit)
            self.pending_l.insertWidget(self.pending_l.count() - 1, c)
        self.tabs.setTabText(0, f"Needs you ({len(pend)})" if pend else "Needs you")

    def _decide(self, aid: str, body: dict) -> None:
        self.api.post(f"/api/approvals/{aid}/decide", body, lambda _: self.store.refresh_approvals())

    def reload(self) -> None:
        i = self.tabs.currentIndex()
        if i == 1:
            def ok(rows: list) -> None:
                self.history.setRowCount(0)
                for a in rows:
                    fill_row(self.history, [fmt_time(a["created_at"]), self.store.bot_name(a["bot_id"]), f"{a['title']}: {a['summary'][:140]}",
                                            a["status"].replace("_", " "), a["decided_by"] or "", a["reason"] or ""], a)
                self.history.resizeRowsToContents()
            self.api.get("/api/approvals", ok, params={"status": "all", "limit": 150})
        elif i == 2:
            def ok2(rows: list) -> None:
                self.handoffs.setRowCount(0)
                for h in rows:
                    fill_row(self.handoffs, [h["title"], f"{h['from_name']} → {h['to_name']}", h["status"], fmt_time(h["updated_at"]), h["result"][:200]], h)
                self.handoffs.resizeRowsToContents()
            self.api.get("/api/handoffs", ok2, params={"status": "all"})

    def _open_hist(self, row: int) -> None:
        a = self.history.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if a and a.get("thread_id"):
            self.openThread.emit(a["thread_id"], a["bot_id"])

    def new_handoff(self) -> None:
        if not self.store.bots:
            return
        if HandoffDialog(self.api, self.store, self).exec():
            self.reload()

    def _set(self, status: str) -> None:
        r = self.handoffs.currentRow()
        if r >= 0:
            h = self.handoffs.item(r, 0).data(Qt.ItemDataRole.UserRole)
            self.api.put(f"/api/handoffs/{h['id']}", {"status": status}, lambda _: self.reload())
