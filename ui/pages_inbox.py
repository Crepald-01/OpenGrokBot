"""Inbox: what needs you (approvals, questions, hand-offs to you), the decision history, and handoffs between Bots."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QScrollArea, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from . import icons, theme
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

        nw = QWidget()
        nv = QVBoxLayout(nw)
        nbar = QHBoxLayout()
        self.n_bot = QComboBox()
        self.n_bot.addItem("All Bots", "")
        self.n_bot.currentIndexChanged.connect(lambda _: self.reload())
        nbar.addWidget(self.n_bot)
        nbar.addStretch(1)
        self.n_count = label("", muted=True)
        nbar.addWidget(self.n_count)
        nbar.addWidget(button("Mark all read", on=self._read_all))
        nv.addLayout(nbar)
        self.notes = make_table(["When", "Bot", "What", "Details"], 3)
        self.notes.cellDoubleClicked.connect(lambda r, c: self._open_note(r))
        nv.addWidget(self.notes)
        self.tabs.addTab(nw, "Notifications")

        store.approvalsChanged.connect(self.render_pending)
        store.event.connect(self._event)
        store.notification.connect(lambda _n: self.isVisible() and self.reload())
        self.tabs.currentChanged.connect(lambda _: self.reload())

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.render_pending()
        self.reload()
        self.api.get("/api/notifications/unread", lambda d: self.tabs.setTabText(3, f"Notifications ({d['count']})" if d.get("count") else "Notifications"))

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
            self.pending_l.insertWidget(0, self._empty())
        for a in pend:
            c = ApprovalCard(a, self.store.bot_name(a["bot_id"]))
            c.decided.connect(self._decide)
            c.openBrowser.connect(self.openBrowser.emit)
            self.pending_l.insertWidget(self.pending_l.count() - 1, c)
        self.tabs.setTabText(0, f"Needs you ({len(pend)})" if pend else "Needs you")

    def _empty(self) -> QWidget:
        """Nothing is waiting: say so calmly, with a check icon, instead of a bare line of text."""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, theme.dp(40), 0, theme.dp(40))
        v.setSpacing(8)
        ic = QLabel()
        ic.setPixmap(icons.pixmap("check", theme.palette()["ok"], 32))
        ic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(ic)
        head = label("All clear", h2=True, wrap=False)
        head.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(head)
        sub = label("Nothing needs your approval right now. Your Bots will ask here when they need you.", muted=True)
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(sub)
        return w

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
        elif i == 3:
            self._fill_bots()

            def ok3(rows: list) -> None:
                want = self.n_bot.currentData() or ""
                self.notes.setRowCount(0)
                unread = 0
                for n in rows:
                    if want and n.get("bot_id") != want:
                        continue
                    r = fill_row(self.notes, [fmt_time(n["ts"]), n.get("bot_name") or "", n["title"], (n.get("body") or "")[:200]], n)
                    if not n.get("read"):
                        unread += 1
                        for c in range(self.notes.columnCount()):
                            it = self.notes.item(r, c)
                            f = it.font()
                            f.setBold(True)
                            it.setFont(f)
                self.notes.resizeRowsToContents()
                self.n_count.setText(f"{unread} unread" if unread else "All read")
            self.api.get("/api/notifications", ok3, params={"limit": 200})
        elif i == 2:
            def ok2(rows: list) -> None:
                self.handoffs.setRowCount(0)
                for h in rows:
                    fill_row(self.handoffs, [h["title"], f"{h['from_name']} → {h['to_name']}", h["status"], fmt_time(h["updated_at"]), h["result"][:200]], h)
                self.handoffs.resizeRowsToContents()
            self.api.get("/api/handoffs", ok2, params={"status": "all"})

    def _fill_bots(self) -> None:
        have = {self.n_bot.itemData(i) for i in range(self.n_bot.count())}
        want = {b["id"] for b in self.store.bots}
        if have - {""} != want:
            cur = self.n_bot.currentData()
            self.n_bot.blockSignals(True)
            self.n_bot.clear()
            self.n_bot.addItem("All Bots", "")
            for b in self.store.bots:
                self.n_bot.addItem(f"{b['emoji']} {b['name']}", b["id"])
            self.n_bot.setCurrentIndex(max(self.n_bot.findData(cur), 0))
            self.n_bot.blockSignals(False)

    def _open_note(self, row: int) -> None:
        n = self.notes.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if n and n.get("thread_id"):
            self.openThread.emit(n["thread_id"], n.get("bot_id", ""))

    def _read_all(self) -> None:
        self.api.post("/api/notifications/read", {}, lambda _: self.reload())

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
