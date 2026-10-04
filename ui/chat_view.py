"""Chat-style thread view: streaming replies, markdown, grouped activity with inline screenshots, approvals and a live view."""
from __future__ import annotations

import base64
import html
import os
import re

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent, QPixmap
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSizePolicy, QStackedLayout, QVBoxLayout, QWidget)

from . import icons, theme
from .api import Api
from .store import Store
from .widgets import (ApprovalCard, AutoMarkdown, Avatar, ImageCache, Thumb, button, chip, clear_layout, icon_button, label, repolish, set_chip)

COLUMN = 800


def status_color(status: str) -> str:
    p = theme.palette()
    return {"running": p["accent"], "ok": p["ok"], "error": p["bad"], "denied": p["bad"], "blocked": p["bad"]}.get(status, p["muted"])


class ToolLine(QWidget):
    """One action inside an activity group."""

    def __init__(self, item: dict):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 2, 0, 2)
        h.setSpacing(8)
        self.icon = QLabel()
        self.icon.setFixedWidth(16)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        h.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        h.addWidget(self.text, 1)
        self.update_item(item)

    def update_item(self, it: dict) -> None:
        p = theme.palette()
        status = it.get("status", "running")
        name = {"running": "play", "ok": "check", "error": "x", "denied": "shield", "blocked": "shield"}.get(status, "check")
        self.icon.setPixmap(icons.pixmap(name, status_color(status), 13, 2.2))
        lab = it.get("label") or it.get("tool") or ""
        dur = f"   {it['duration_ms'] / 1000:.1f}s" if it.get("duration_ms", 0) > 1500 else ""
        extra = ""
        if status in ("denied", "blocked"):
            extra = "  (not performed)"
        self.text.setText(lab + extra + dur)
        self.text.setStyleSheet(f"color: {p['bad'] if status in ('error', 'denied', 'blocked') else p['muted']}; font-size: 12px;")
        self.setToolTip((it.get("summary") or "")[:600])


class ToolGroup(QFrame):
    """Consecutive actions collapsed into one calm line; expands on click. Screenshots stay visible."""

    def __init__(self, images: ImageCache):
        super().__init__()
        self.setObjectName("toolgroup")
        self.images = images
        self.lines: dict[str, ToolLine] = {}
        self.status: dict[str, str] = {}
        self.labels: dict[str, str] = {}
        self.order: list[str] = []
        self.shown: set[str] = set()
        self.expanded = False
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 8, 12, 8)
        v.setSpacing(6)
        self.head = QWidget()
        self.head.setCursor(Qt.CursorShape.PointingHandCursor)
        hh = QHBoxLayout(self.head)
        hh.setContentsMargins(0, 0, 0, 0)
        hh.setSpacing(8)
        self.dot = QLabel()
        self.dot.setFixedWidth(16)
        hh.addWidget(self.dot)
        self.summary = QLabel()
        self.summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        hh.addWidget(self.summary, 1)
        self.count = QLabel()
        self.count.setProperty("faint", True)
        hh.addWidget(self.count)
        self.chev = QLabel()
        hh.addWidget(self.chev)
        self.head.mousePressEvent = lambda e: self.toggle()  # type: ignore[assignment]
        v.addWidget(self.head)
        self.body = QWidget()
        self.body_l = QVBoxLayout(self.body)
        self.body_l.setContentsMargins(24, 0, 0, 0)
        self.body_l.setSpacing(0)
        self.body.hide()
        v.addWidget(self.body)
        self.thumbs = QHBoxLayout()
        self.thumbs.setContentsMargins(24, 0, 0, 0)
        self.thumbs.setSpacing(8)
        self.thumb_box = QWidget()
        self.thumb_box.setLayout(self.thumbs)
        self.thumb_box.hide()
        v.addWidget(self.thumb_box)
        self.refresh_head()

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self.body.setVisible(self.expanded)
        self.refresh_head()

    def upsert(self, it: dict) -> None:
        cid = it["call_id"]
        if cid not in self.lines:
            if it.get("partial"):
                return
            ln = ToolLine(it)
            self.lines[cid] = ln
            self.order.append(cid)
            self.body_l.addWidget(ln)
        else:
            self.lines[cid].update_item({**it, "label": it.get("label") or self.labels.get(cid, "")} if it.get("partial") else it)
        if it.get("label"):
            self.labels[cid] = it["label"]
        self.status[cid] = it.get("status", self.status.get(cid, "running"))
        for name in it.get("images", []) or []:
            if name in self.shown:
                continue
            self.shown.add(name)
            self.images.get(name, self._add_thumb)
        self.refresh_head()

    def _add_thumb(self, pm: QPixmap) -> None:
        while self.thumbs.count() >= 3:
            w = self.thumbs.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.thumbs.addWidget(Thumb(pm, 250))
        self.thumb_box.show()

    def refresh_head(self) -> None:
        p = theme.palette()
        n = len(self.order)
        running = [c for c in self.order if self.status.get(c) == "running"]
        bad = [c for c in self.order if self.status.get(c) in ("error", "denied", "blocked")]
        if running:
            cid, st, color = running[-1], "running", p["accent"]
            text = self.labels.get(cid, "Working") + "…"
        else:
            cid = self.order[-1] if self.order else ""
            st = "error" if bad and self.status.get(cid) in ("error", "denied", "blocked") else "ok"
            color = p["bad"] if st == "error" else p["muted"]
            text = self.labels.get(cid, "") if n == 1 else f"{n} actions" + (f", last: {self.labels.get(cid, '')}" if cid else "")
        self.dot.setPixmap(icons.pixmap("play" if st == "running" else ("x" if st == "error" else "check"), p["accent"] if st == "running" else (p["bad"] if st == "error" else p["ok"]), 12, 2.2))
        self.summary.setText(text)
        self.summary.setStyleSheet(f"color: {color if running or st == 'error' else p['muted']}; font-size: 12px;")
        self.count.setText(f"{n}" if n > 1 else "")
        self.chev.setPixmap(icons.pixmap("chevron-down" if self.expanded else "chevron-right", p["faint"], 14))


class MessageList(QScrollArea):
    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.box = QWidget()
        outer = QHBoxLayout(self.box)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.addStretch(1)
        self.col = QWidget()
        self.col.setMaximumWidth(COLUMN)
        self.col.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.v = QVBoxLayout(self.col)
        self.v.setContentsMargins(0, 0, 0, 0)
        self.v.setSpacing(12)
        outer.addWidget(self.col, 100)
        outer.addStretch(1)
        self.tail = QWidget()
        self.tail_l = QVBoxLayout(self.tail)
        self.tail_l.setContentsMargins(0, 0, 0, 0)
        self.tail_l.setSpacing(12)
        self.v.addWidget(self.tail)
        self.v.addStretch(1)
        self.setWidget(self.box)
        self._stick = True
        bar = self.verticalScrollBar()
        bar.rangeChanged.connect(lambda lo, hi: self._stick and bar.setValue(hi))
        bar.valueChanged.connect(lambda val: setattr(self, "_stick", val >= bar.maximum() - 50))

    def add(self, w: QWidget) -> None:
        self.v.insertWidget(self.v.count() - 2, w)

    def clear(self) -> None:
        while self.v.count() > 2:
            it = self.v.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        clear_layout(self.tail_l)
        self._stick = True

    def to_bottom(self) -> None:
        self._stick = True
        QTimer.singleShot(30, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))


class CommandPopup(QListWidget):
    """The list of slash commands that appears above the composer while you type "/"."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.commands: list[dict] = []
        self.picked = None            # callback(name)
        self.itemClicked.connect(lambda it: self.picked and self.picked(it.data(Qt.ItemDataRole.UserRole)))
        self.hide()

    def update_for(self, text: str, in_group: bool) -> None:
        m = re.match(r"^/([\w-]*)$", text)
        if not m or not self.commands:
            self.hide()
            return
        q = m.group(1).lower()
        scored = []
        for c in self.commands:
            if in_group and c["bot_only"]:
                continue
            names = [c["name"], *c["aliases"]]
            if any(n.startswith(q) for n in names):
                scored.append((0, c))
            elif q and any(q in n for n in names):
                scored.append((1, c))
        rows = [c for _, c in sorted(scored, key=lambda t: t[0])]
        if not rows:
            self.hide()
            return
        p = theme.palette()
        self.setStyleSheet(f"QListWidget {{ background: {p['panel2']}; border: 1px solid {p['line2']}; border-radius: 12px; padding: 4px; outline: 0; }}"
                           f"QListWidget::item {{ border-radius: 8px; }} QListWidget::item:selected {{ background: {p['select']}; }}")
        self.clear()
        for c in rows[:8]:
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, c["name"])
            it.setSizeHint(QSize(0, 34))
            self.addItem(it)
            lb = QLabel(f"<b>{html.escape(c['usage'])}</b>  <span style='color:{p['muted']}'>{html.escape(c['summary'])}</span>")
            lb.setStyleSheet("background: transparent; padding: 0 8px;")
            lb.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self.setItemWidget(it, lb)
        self.setCurrentRow(0)
        self.setFixedHeight(min(len(rows), 8) * 34 + 14)
        self.show()
        self.raise_()

    def step(self, d: int) -> None:
        n = self.count()
        if n:
            self.setCurrentRow((self.currentRow() + d) % n)

    def accept_into(self, comp: QPlainTextEdit, enter: bool) -> bool:
        """Complete the highlighted command into the composer. False means "send it as typed"."""
        it = self.currentItem()
        if it is None:
            return False
        name = it.data(Qt.ItemDataRole.UserRole)
        if enter and comp.toPlainText().strip() == f"/{name}":
            return False
        comp.setPlainText(f"/{name} ")
        cur = comp.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        comp.setTextCursor(cur)
        self.hide()
        return True


class Composer(QPlainTextEdit):
    send = Signal()
    focusChanged = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setProperty("bare", True)
        self.setPlaceholderText("Message your Bot…   (type / for commands)")
        self.popup: CommandPopup | None = None
        self.setTabChangesFocus(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.document().contentsChanged.connect(self._grow)
        self._grow()

    def _grow(self) -> None:
        lines = max(1, min(6, self.document().blockCount() + self.toPlainText().count("\n") * 0))
        h = int(self.document().size().height()) + 14
        self.setFixedHeight(max(36, min(h, 150)))

    def keyPressEvent(self, e: QKeyEvent) -> None:
        pop = self.popup
        if pop is not None and pop.isVisible():
            k = e.key()
            if k == Qt.Key.Key_Down:
                pop.step(1)
                return
            if k == Qt.Key.Key_Up:
                pop.step(-1)
                return
            if k == Qt.Key.Key_Escape:
                pop.hide()
                return
            if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab) and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                if pop.accept_into(self, enter=k != Qt.Key.Key_Tab) or k == Qt.Key.Key_Tab:
                    return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.send.emit()
            return
        super().keyPressEvent(e)

    def focusNextPrevChild(self, nxt: bool) -> bool:
        # Tab would normally move focus; while the command list is open it completes the highlighted command instead
        pop = self.popup
        if pop is not None and pop.isVisible():
            pop.accept_into(self, enter=False)
            return True
        return super().focusNextPrevChild(nxt)

    def focusInEvent(self, e) -> None:
        super().focusInEvent(e)
        self.focusChanged.emit(True)

    def focusOutEvent(self, e) -> None:
        super().focusOutEvent(e)
        self.focusChanged.emit(False)


class ActivityPanel(QFrame):
    openBrowser = Signal()
    closed = Signal()

    def __init__(self, api: Api):
        super().__init__()
        self.setObjectName("activity")
        self.api = api
        self.setFixedWidth(320)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 16)
        v.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(label("Live view", h2=True))
        top.addStretch(1)
        top.addWidget(icon_button("x", "Hide", self.closed.emit, size=14))
        v.addLayout(top)
        self.shot = QLabel("No browser activity yet")
        self.shot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.shot.setMinimumHeight(150)
        self.shot.setStyleSheet(f"background: {theme.palette()['code']}; border-radius: 10px; color: {theme.palette()['faint']};")
        v.addWidget(self.shot)
        self.url = label("", faint=True, wrap=False)
        v.addWidget(self.url)
        v.addWidget(button("Open browser view", icon="pointer", on=self.openBrowser.emit))
        v.addSpacing(4)
        v.addWidget(label("RECENT ACTIVITY", eyebrow=True))
        self.feed = QListWidget()
        self.feed.setWordWrap(True)
        self.feed.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.feed.setStyleSheet("QListWidget { border: none; background: transparent; } QListWidget::item { padding: 5px 2px; margin: 0; color: " + theme.palette()["muted"] + "; font-size: 12px; }")
        v.addWidget(self.feed, 1)
        self.bot_id = ""
        self._busy = False
        self.has_browser = False
        self._fetching = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_shot)
        self.timer.start(2500)

    def reset(self, bot_id: str) -> None:
        self.bot_id = bot_id
        self.has_browser = False
        self.feed.clear()
        self.shot.setPixmap(QPixmap())
        self.shot.setText("No browser activity yet")
        self.url.setText("")

    def set_busy(self, busy: bool) -> None:
        self._busy = busy

    def log(self, text: str, status: str = "ok") -> None:
        sym = {"running": "●  ", "ok": "✓  ", "error": "✕  ", "denied": "⛔  ", "blocked": "⛔  "}.get(status, "•  ")
        self.feed.addItem(QListWidgetItem(sym + text))
        while self.feed.count() > 60:
            self.feed.takeItem(0)
        self.feed.scrollToBottom()

    def update_last(self, label_: str, status: str) -> None:
        for i in range(self.feed.count() - 1, -1, -1):
            it = self.feed.item(i)
            if it.text()[3:] == label_ and it.text().startswith("●"):
                it.setText({"ok": "✓  ", "error": "✕  ", "denied": "⛔  ", "blocked": "⛔  "}.get(status, "•  ") + label_)
                return

    def refresh_shot(self) -> None:
        if not (self._busy and self.has_browser) or not self.bot_id or self._fetching or not self.isVisible():
            return
        self._fetching = True

        def ok(data: bytes) -> None:
            self._fetching = False
            pm = QPixmap()
            if pm.loadFromData(data):
                self.shot.setPixmap(pm.scaledToWidth(self.shot.width() - 4, Qt.TransformationMode.SmoothTransformation))

        self.api.request("GET", f"/api/bots/{self.bot_id}/screen.jpg", ok, lambda e: setattr(self, "_fetching", False), params={"q": 35}, raw=True)


class EmptyState(QWidget):
    suggestion = Signal(str)

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.setSpacing(10)
        self.avatar = Avatar("🤖", 64)
        v.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title = label("", h1=True, wrap=False)
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(self.title)
        self.sub = label("", muted=True)
        self.sub.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.sub.setMaximumWidth(520)
        v.addWidget(self.sub, 0, Qt.AlignmentFlag.AlignHCenter)
        v.addSpacing(10)
        self.chips = QVBoxLayout()
        self.chips.setSpacing(8)
        v.addLayout(self.chips)

    def set_content(self, emoji: str, title: str, sub: str, suggestions: list[str]) -> None:
        self.avatar.set_emoji(emoji)
        self.title.setText(title)
        self.sub.setText(sub)
        clear_layout(self.chips)
        for s in suggestions:
            b = QPushButton(s if len(s) < 90 else s[:88] + "…")
            b.setProperty("chipbtn", True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(s)
            b.clicked.connect(lambda _=False, s=s: self.suggestion.emit(s))
            self.chips.addWidget(b, 0, Qt.AlignmentFlag.AlignHCenter)


class ChatPage(QWidget):
    openBrowser = Signal(str)
    editBot = Signal(str)
    editGroup = Signal(str)
    exportBot = Signal(str)
    toast = Signal(str, str)

    def __init__(self, api: Api, store: Store, images: ImageCache):
        super().__init__()
        self.api, self.store, self.images = api, store, images
        self.thread_id = ""
        self.bot_id = ""
        self.group_id = ""
        self.attachments: list[dict] = []
        self.tools: dict[str, ToolGroup] = {}
        self.cur_group: ToolGroup | None = None
        self.streams: dict[str, AutoMarkdown] = {}
        self.seen_ids: set[int] = set()
        self.cards: dict[str, ApprovalCard] = {}
        self.thread_list: list[dict] = []
        self.item_count = 0
        self.panel_pinned = False

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        main = QVBoxLayout()
        main.setSpacing(0)
        outer.addLayout(main, 1)

        # header
        head = QFrame()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(24, 14, 16, 14)
        hl.setSpacing(12)
        self.avatar = Avatar("🤖", 38)
        hl.addWidget(self.avatar)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.title = label("", h2=True, wrap=False)
        self.title.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.subtitle = label("", faint=True, wrap=False)
        self.subtitle.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(self.title)
        col.addWidget(self.subtitle)
        hl.addLayout(col, 1)
        self.status = chip("Idle")
        hl.addWidget(self.status)
        self.thread_btn = button("Main", flat=True)
        self.thread_btn.setIcon(icons.icon("chevron-down", theme.palette()["muted"], 14))
        self.thread_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.thread_btn.clicked.connect(self._thread_menu)
        hl.addWidget(self.thread_btn)
        self.btn_browser = icon_button("globe", "Open this Bot's browser (take over)", lambda: self.openBrowser.emit(self.bot_id))
        hl.addWidget(self.btn_browser)
        self.btn_panel = icon_button("panel-right", "Show or hide the live view", self.toggle_panel)
        hl.addWidget(self.btn_panel)
        self.btn_menu = icon_button("more", "More", self._menu)
        hl.addWidget(self.btn_menu)
        main.addWidget(head)
        sep = QFrame()
        sep.setProperty("sep", True)
        main.addWidget(sep)

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        p = theme.palette()
        self.banner.setStyleSheet(f"background: {p['warn_bg']}; color: {p['warn']}; padding: 9px 24px;")
        self.banner.hide()
        main.addWidget(self.banner)

        # message area (list or empty state)
        host = QWidget()
        self.stackl = QStackedLayout(host)
        self.empty = EmptyState()
        self.empty.suggestion.connect(self._use_suggestion)
        self.list = MessageList()
        self.stackl.addWidget(self.empty)
        self.stackl.addWidget(self.list)
        self.stackl.setCurrentWidget(self.empty)
        main.addWidget(host, 1)

        # composer
        comp_wrap = QWidget()
        cw = QHBoxLayout(comp_wrap)
        cw.setContentsMargins(24, 4, 24, 18)
        cw.addStretch(1)
        inner = QWidget()
        inner.setMaximumWidth(COLUMN)
        inner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(0, 0, 0, 0)
        iv.setSpacing(6)
        self.attach_row = QHBoxLayout()
        self.attach_row.setSpacing(6)
        iv.addLayout(self.attach_row)
        self.comp_frame = QFrame()
        self.comp_frame.setObjectName("composer")
        row = QHBoxLayout(self.comp_frame)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(6)
        row.addWidget(icon_button("paperclip", "Attach an image", self.attach), 0, Qt.AlignmentFlag.AlignBottom)
        self.input = Composer()
        self.input.send.connect(self.send)
        self.input.focusChanged.connect(self._focus_ring)
        self.input.textChanged.connect(lambda: self._sync_buttons())
        self.popup = CommandPopup(self)
        self.popup.picked = self._pick_command
        self.input.popup = self.popup
        self.input.textChanged.connect(self._slash_update)
        self.input.focusChanged.connect(lambda on: None if on else self.popup.hide())
        self.commands: list[dict] = []
        self.api.get("/api/commands", self._set_commands)
        row.addWidget(self.input, 1)
        self.btn_send = icon_button("send", "Send (Enter)", self.send, kind="accent")
        self.btn_stop = icon_button("stop", "Stop this Bot", self.stop, kind="danger")
        self.btn_stop.hide()
        row.addWidget(self.btn_send, 0, Qt.AlignmentFlag.AlignBottom)
        row.addWidget(self.btn_stop, 0, Qt.AlignmentFlag.AlignBottom)
        iv.addWidget(self.comp_frame)
        hint = label("Enter to send  ·  Shift+Enter for a new line  ·  / for commands  ·  Bots ask before anything consequential", faint=True, wrap=False)
        hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        iv.addWidget(hint)
        cw.addWidget(inner, 100)
        cw.addStretch(1)
        main.addWidget(comp_wrap)

        self.activity = ActivityPanel(api)
        self.activity.openBrowser.connect(lambda: self.openBrowser.emit(self.bot_id))
        self.activity.closed.connect(self.toggle_panel)
        self.activity.hide()
        outer.addWidget(self.activity)

        store.event.connect(self.on_event)
        store.busyChanged.connect(self.update_status)
        store.approvalsChanged.connect(self.render_approvals)
        store.botsChanged.connect(self._refresh_header)
        self._focus_ring(False)

    # ---------------------------------------------------------------- composer
    def _focus_ring(self, on: bool) -> None:
        p = theme.palette()
        self.comp_frame.setStyleSheet(f"QFrame#composer {{ background: {p['panel']}; border: 1px solid {p['accent'] if on else p['line2']}; border-radius: 18px; }}")

    def _set_commands(self, rows: list) -> None:
        self.commands = rows
        self.popup.commands = rows

    def _slash_update(self) -> None:
        self.popup.update_for(self.input.toPlainText(), bool(self.group_id))
        if self.popup.isVisible():
            top = self.comp_frame.mapTo(self, self.comp_frame.rect().topLeft())
            self.popup.setFixedWidth(self.comp_frame.width())
            self.popup.move(top.x(), top.y() - self.popup.height() - 6)

    def _pick_command(self, name: str) -> None:
        self.input.setPlainText(f"/{name} ")
        cur = self.input.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        self.input.setTextCursor(cur)
        self.input.setFocus()
        self.popup.hide()

    def _use_suggestion(self, text: str) -> None:
        self.input.setPlainText(text)
        self.input.setFocus()
        cur = self.input.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        self.input.setTextCursor(cur)

    # ----------------------------------------------------------------- loading
    def show_bot(self, bot_id: str, thread_id: str | None = None, draft: str | None = None) -> None:
        self.bot_id, self.group_id = bot_id, ""
        self.thread_btn.show()
        self.btn_browser.show()
        self.btn_panel.show()
        self.activity.reset(bot_id)
        self._refresh_header()
        self._load_threads(thread_id)
        self._pending_draft = draft

    def show_group(self, group_id: str) -> None:
        g = self.store.group(group_id)
        if not g:
            return
        self.bot_id, self.group_id = "", group_id
        self.thread_btn.hide()
        self.btn_browser.hide()
        self.btn_panel.hide()
        self.activity.hide()
        self._refresh_header()
        self.open_thread(g["thread_id"])

    def _load_threads(self, select: str | None = None) -> None:
        def ok(ths: list) -> None:
            self.thread_list = ths
            target = select or (ths[0]["id"] if ths else "")
            if target:
                self.open_thread(target)
        self.api.get(f"/api/bots/{self.bot_id}/threads", ok)

    def _thread_menu(self) -> None:
        m = QMenu(self)
        for t in self.thread_list:
            a = m.addAction(("★  " if t.get("main") else "") + (t["title"] or "Thread")[:48])
            a.setCheckable(True)
            a.setChecked(t["id"] == self.thread_id)
            a.triggered.connect(lambda _=False, tid=t["id"]: self.open_thread(tid))
        m.addSeparator()
        m.addAction("New thread", self._new_thread)
        m.exec(self.thread_btn.mapToGlobal(self.thread_btn.rect().bottomLeft()))

    def _new_thread(self) -> None:
        self.api.post(f"/api/bots/{self.bot_id}/threads", {"title": "New thread"}, lambda t: self._load_threads(t["id"]))

    def open_thread(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.list.clear()
        self.tools.clear()
        self.cur_group = None
        self.streams.clear()
        self.seen_ids.clear()
        self.cards.clear()
        self.item_count = 0
        self.activity.feed.clear()
        t = next((t for t in self.thread_list if t["id"] == thread_id), None)
        if t:
            self.thread_btn.setText(("Main" if t.get("main") else (t["title"] or "Thread")[:22]))

        def ok(d: dict) -> None:
            if d["thread"]["id"] != self.thread_id:
                return
            for it in d["items"]:
                self.apply_item(it, initial=True)
            self.list.to_bottom()
            self.store.approvals = [a for a in self.store.approvals if a["thread_id"] != self.thread_id] + d["approvals"]
            self.render_approvals()
            self.update_status()
            self._refresh_header()
            self._sync_empty()
            if getattr(self, "_pending_draft", None):
                self._use_suggestion(self._pending_draft)  # type: ignore[arg-type]
                self._pending_draft = None
            else:
                self.input.setFocus()
        self.api.get(f"/api/threads/{thread_id}", ok)

    def _sync_empty(self) -> None:
        has = self.item_count > 0 or bool(self.cards)
        if has:
            self.stackl.setCurrentWidget(self.list)
            return
        if self.group_id:
            g = self.store.group(self.group_id) or {}
            self.empty.set_content("👥", g.get("name", "Group"), (g.get("goal") or "Bots in this group coordinate on their own. Message the group to start; the lead Bot will take it from there."),
                                   ["Plan this week's priorities and assign owners", "Give me a status update on everything in flight"])
        else:
            b = self.store.bot(self.bot_id) or {}
            tpl = next((t for t in self.store.templates if t["id"] == b.get("template")), None)
            sug = ([tpl["example"]] if tpl else []) + ["What can you help me with, and what access will you need?", "Here's your job. Ask me anything you need to know first."]
            self.empty.set_content(b.get("emoji") or "🤖", f"Message {b.get('name', 'your Bot')}", b.get("job") or "Describe the job and give it the context it needs.", sug[:3])
        self.stackl.setCurrentWidget(self.empty)

    def _refresh_header(self) -> None:
        if self.group_id:
            g = self.store.group(self.group_id)
            if g:
                self.avatar.set_emoji("👥")
                self.title.setText(g["name"])
                lead = self.store.bot_name(g["lead_bot"]) if g.get("lead_bot") else ""
                self.subtitle.setText(", ".join(g["member_names"]) + (f"  ·  lead: {lead}" if lead else ""))
            return
        b = self.store.bot(self.bot_id)
        if b:
            self.avatar.set_emoji(b.get("emoji") or "🤖")
            self.title.setText(b["name"])
            self.subtitle.setText(b.get("job") or "No job described yet. Open ⋯ > Edit Bot.")

    # --------------------------------------------------------------- rendering
    def _plain(self, w: QWidget) -> None:
        self.cur_group = None
        self.list.add(w)
        self.item_count += 1
        self.stackl.setCurrentWidget(self.list)

    def apply_item(self, it: dict, initial: bool = False) -> None:
        t = it["type"]
        if t == "tool":
            cid = it["call_id"]
            g = self.tools.get(cid)
            if g is None:
                if it.get("partial"):
                    return
                if self.cur_group is None:
                    self.cur_group = ToolGroup(self.images)
                    self.list.add(self.cur_group)
                    self.item_count += 1
                    self.stackl.setCurrentWidget(self.list)
                g = self.cur_group
                self.tools[cid] = g
                self.activity.log(it.get("label") or it.get("tool", ""), it.get("status", "running"))
                if (it.get("tool") or "").startswith("browser_"):
                    self.activity.has_browser = True
                    if not self.panel_pinned and not initial:
                        self.show_panel()
            else:
                self.activity.update_last(it.get("label") or "", it.get("status", "ok"))
            g.upsert(it)
            return
        if it.get("id") in self.seen_ids and t != "assistant":
            return
        if it.get("id"):
            self.seen_ids.add(it["id"])
        p = theme.palette()
        if t == "user":
            f = QFrame()
            f.setStyleSheet(f"background: {p['user']}; border-radius: 16px;")
            f.setMaximumWidth(620)
            f.setMinimumWidth(max(110, min(620, 44 + len(it["text"] or "") * 7)))
            l = QVBoxLayout(f)
            l.setContentsMargins(16, 11, 16, 11)
            l.setSpacing(6)
            l.addWidget(AutoMarkdown(it["text"] or ""))
            for name in it.get("images", []) or []:
                self.images.get(name, lambda pm, l=l: l.addWidget(Thumb(pm, 260)))
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.addStretch(1)
            rl.addWidget(f)
            self._plain(row)
            self.list.to_bottom()
        elif t == "assistant":
            sid = it.get("stream_id", "")
            w = self.streams.pop(sid, None) if sid else None
            if w is not None:
                w.set_text(it["text"], immediate=True)
                w.setProperty("msg_id", it["id"])
                return
            if it["id"] in self._assistant_ids():
                return
            self._plain(self._assistant_bubble(it["text"], it.get("name", ""), it.get("emoji", ""), it["id"]))
        elif t == "notice" and it.get("level") == "cmd":
            card = QFrame()
            card.setStyleSheet(f"QFrame {{ background: {p['panel']}; border: 1px solid {p['line2']}; border-left: 3px solid {p['accent']}; border-radius: 10px; }}")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(14, 8, 14, 10)
            md = AutoMarkdown(it["text"])
            md.setStyleSheet("border: none; background: transparent;")
            cl.addWidget(md)
            self._plain(card)
        elif t == "notice":
            lb = QLabel(it["text"])
            lb.setWordWrap(True)
            lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col, bg = {"error": (p["bad"], p["bad_bg"]), "warn": (p["warn"], p["warn_bg"])}.get(it.get("level"), (p["muted"], p["panel2"]))
            lb.setStyleSheet(f"color: {col}; background: {bg}; border-radius: 10px; padding: 8px 14px; font-size: 12px;")
            self._plain(lb)
        elif t == "system_note":
            lb = QLabel("↻  " + it["text"][:240])
            lb.setWordWrap(True)
            lb.setStyleSheet(f"color: {p['faint']}; font-size: 11px; padding-left: 4px;")
            self._plain(lb)

    def _assistant_ids(self) -> set[int]:
        return {w.property("msg_id") for w in self.list.box.findChildren(AutoMarkdown) if w.property("msg_id")}

    def _assistant_bubble(self, text: str, name: str, emoji: str, mid: int | None) -> QWidget:
        w = AutoMarkdown(text)
        if mid:
            w.setProperty("msg_id", mid)
        if not self.group_id or not name:
            return w
        box = QWidget()
        l = QHBoxLayout(box)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(10)
        l.addWidget(Avatar(emoji, 28), 0, Qt.AlignmentFlag.AlignTop)
        c = QVBoxLayout()
        c.setSpacing(2)
        c.addWidget(label(name, faint=True, wrap=False))
        c.addWidget(w)
        l.addLayout(c, 1)
        return box

    def on_event(self, ev: dict) -> None:
        if ev.get("thread_id") != self.thread_id or not self.thread_id:
            return
        t = ev["type"]
        if t == "message":
            for it in ev["items"]:
                if it["type"] == "assistant" and ev.get("stream_id") and not it.get("stream_id"):
                    it["stream_id"] = ev["stream_id"]
                self.apply_item(it)
        elif t == "delta":
            sid = ev["stream_id"]
            w = self.streams.get(sid)
            if w is None:
                w = AutoMarkdown("")
                self.streams[sid] = w
                self.cur_group = None
                self.list.add(w)
                self.item_count += 1
                self.stackl.setCurrentWidget(self.list)
            w.append_text(ev["text"])
        elif t == "delta_reset":
            w = self.streams.pop(ev["stream_id"], None)
            if w:
                w.setParent(None)
                w.deleteLater()
        elif t == "activity":
            self.activity.log(ev["text"], "running")
        elif t == "takeover":
            self.update_status()

    def render_approvals(self) -> None:
        pend = [a for a in self.store.approvals if a["thread_id"] == self.thread_id and a["status"] == "pending"]
        ids = {a["id"] for a in pend}
        for aid in list(self.cards):
            if aid not in ids:
                c = self.cards.pop(aid)
                c.hide()
                c.setParent(None)
                c.deleteLater()
        for a in pend:
            if a["id"] in self.cards:
                continue
            c = ApprovalCard(a, self.store.bot_name(a["bot_id"]))
            c.decided.connect(self._decide)
            c.openBrowser.connect(self.openBrowser.emit)
            self.cards[a["id"]] = c
            self.list.tail_l.addWidget(c)
            self.stackl.setCurrentWidget(self.list)
            self.list.to_bottom()

    def _decide(self, aid: str, body: dict) -> None:
        self.api.post(f"/api/approvals/{aid}/decide", body, lambda _: self.store.refresh_approvals())

    def update_status(self) -> None:
        if self.group_id:
            running = [r for r in self.store.busy.values() if r.get("thread_id") == self.thread_id]
            self._set_running(bool(running))
            set_chip(self.status, f"{len(running)} working" if running else "Idle", "work" if running else "true")
            return
        if not self.bot_id:
            return
        kind, text = self.store.state_of(self.bot_id)
        run = self.store.busy.get(self.bot_id)
        here = bool(run and run.get("thread_id") == self.thread_id)
        self._set_running(here)
        self.activity.set_busy(here)
        label_ = (text or "idle").capitalize()
        if run and not here:
            label_ += " elsewhere"
        set_chip(self.status, label_, {"work": "work", "wait": "warn", "takeover": "warn"}.get(kind, "true"))
        if self.bot_id in self.store.takeovers:
            self.banner.setText("You are driving this Bot's browser. It is paused until you hand it back.")
            self.banner.show()
        elif self.store.bot(self.bot_id) and self.store.bot(self.bot_id).get("paused"):
            self.banner.setText("This Bot is paused: no routines, follow-ups or background work. It still answers your messages.")
            self.banner.show()
        else:
            self.banner.hide()

    def _set_running(self, running: bool) -> None:
        self.running = running
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        typing = bool(self.input.toPlainText().strip()) or bool(self.attachments)
        show_stop = getattr(self, "running", False) and not typing
        self.btn_stop.setVisible(show_stop)
        self.btn_send.setVisible(not show_stop)

    def toggle_panel(self) -> None:
        self.panel_pinned = True
        self.activity.setVisible(not self.activity.isVisible())

    def show_panel(self) -> None:
        if not self.group_id:
            self.activity.show()

    # ----------------------------------------------------------------- actions
    def send(self) -> None:
        text = self.input.toPlainText().strip()
        if (not text and not self.attachments) or not self.thread_id:
            return
        body = {"text": text, "images": self.attachments}
        self.input.clear()
        self.attachments = []
        clear_layout(self.attach_row)
        self.list.to_bottom()
        def sent(d: dict) -> None:
            if isinstance(d, dict) and d.get("switch_thread") and self.bot_id:
                self._load_threads(d["switch_thread"])
        self.api.post(f"/api/threads/{self.thread_id}/messages", body, sent, lambda e: self.toast.emit(e, "error"))

    def attach(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Attach an image", "", "Images (*.png *.jpg *.jpeg)")
        if not path:
            return
        with open(path, "rb") as f:
            data = f.read()
        if len(data) > 8 * 1024 * 1024:
            self.toast.emit("Images must be under 8 MB.", "warn")
            return
        att = {"name": os.path.basename(path), "data": base64.b64encode(data).decode()}
        self.attachments.append(att)
        b = button(f"{os.path.basename(path)}   ✕", flat=True)
        b.setStyleSheet(f"background: {theme.palette()['panel2']}; border-radius: 12px; padding: 3px 10px;")

        def drop(att=att, b=b) -> None:
            if att in self.attachments:
                self.attachments.remove(att)
            b.hide()
            b.deleteLater()
        b.clicked.connect(drop)
        self.attach_row.insertWidget(self.attach_row.count(), b)

    def stop(self) -> None:
        self.api.post(f"/api/threads/{self.thread_id}/stop", {})
        self.btn_stop.setEnabled(False)
        QTimer.singleShot(1500, lambda: self.btn_stop.setEnabled(True))

    def _menu(self) -> None:
        m = QMenu(self)
        if self.group_id:
            m.addAction("Edit group…", lambda: self.editGroup.emit(self.group_id))
            m.addAction("Export chat as Markdown…", self.export_chat)
            m.addAction("Delete group", self._delete_thread)
        else:
            m.addAction("Edit Bot…", lambda: self.editBot.emit(self.bot_id))
            b = self.store.bot(self.bot_id)
            m.addAction("Resume Bot" if b and b["paused"] else "Pause Bot", self._toggle_pause)
            m.addSeparator()
            m.addAction("Rename thread…", self._rename)
            m.addAction("Export chat as Markdown…", self.export_chat)
            m.addAction("Delete thread", self._delete_thread)
            m.addSeparator()
            m.addAction("Export Bot package…", lambda: self.exportBot.emit(self.bot_id))
        m.exec(self.btn_menu.mapToGlobal(self.btn_menu.rect().bottomLeft()))

    def export_chat(self) -> None:
        if not self.thread_id:
            return
        title = "".join(ch if ch.isalnum() or ch in "-_ " else "" for ch in (self.thread_btn.text() or "chat")).strip().replace(" ", "-")[:40] or "chat"
        path, _ = QFileDialog.getSaveFileName(self, "Export chat", f"{title}.md", "Markdown (*.md)")
        if not path:
            return

        def ok(data: bytes) -> None:
            with open(path, "wb") as f:
                f.write(data)
            self.toast.emit("Chat exported.", "ok")
        self.api.request("GET", f"/api/threads/{self.thread_id}/export", ok, lambda e: self.toast.emit(e, "error"), raw=True)

    def _toggle_pause(self) -> None:
        b = self.store.bot(self.bot_id)
        if b:
            self.api.put(f"/api/bots/{b['id']}", {"paused": not b["paused"]}, lambda _: self.store.refresh_bots())

    def _rename(self) -> None:
        cur = next((t["title"] for t in self.thread_list if t["id"] == self.thread_id), "")
        t, ok = QInputDialog.getText(self, "Rename thread", "Title:", text=cur)
        if ok and t.strip():
            self.api.put(f"/api/threads/{self.thread_id}", {"title": t.strip()}, lambda _: self._load_threads(self.thread_id))

    def _delete_thread(self) -> None:
        what = "this group chat" if self.group_id else "this thread"
        if QMessageBox.question(self, "Delete", f"Delete {what} and its messages? Memory and skills are kept.") != QMessageBox.StandardButton.Yes:
            return

        def done(_):
            if self.group_id:
                self.store.refresh_groups()
                self.list.clear()
                self.thread_id = ""
                self.stackl.setCurrentWidget(self.empty)
            else:
                self._load_threads()
        self.api.delete(f"/api/threads/{self.thread_id}", done)
