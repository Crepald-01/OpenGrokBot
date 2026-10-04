"""Reusable building blocks: labels, buttons, cards, page headers, side tabs, toasts, approval cards, thumbnails."""
from __future__ import annotations

import time
from typing import Callable

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QStackedWidget,
                               QVBoxLayout, QWidget)

from . import icons, theme

PAGE_MARGINS = (32, 26, 32, 20)


def repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)


def prop(w: QWidget, name: str, value) -> QWidget:
    w.setProperty(name, value)
    repolish(w)
    return w


def label(text: str = "", muted: bool = False, h1: bool = False, h2: bool = False, wrap: bool = True, faint: bool = False, eyebrow: bool = False) -> QLabel:
    lb = QLabel(text)
    lb.setWordWrap(wrap)
    for k, v in (("muted", muted), ("h1", h1), ("h2", h2), ("faint", faint), ("eyebrow", eyebrow)):
        if v:
            lb.setProperty(k, True)
    lb.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lb


def chip(text: str, kind: str = "true") -> QLabel:
    lb = QLabel(text)
    lb.setProperty("chip", kind)
    lb.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return lb


def set_chip(lb: QLabel, text: str, kind: str) -> None:
    lb.setText(text)
    lb.setProperty("chip", kind)
    repolish(lb)


def button(text: str = "", primary: bool = False, danger: bool = False, flat: bool = False, on: Callable | None = None, icon: str | None = None,
           tip: str = "") -> QPushButton:
    b = QPushButton(text)
    if primary:
        b.setProperty("primary", True)
    if danger:
        b.setProperty("danger", True)
    if flat:
        b.setProperty("flat", True)
    if icon:
        col = theme.palette()["accent_text"] if primary else (theme.palette()["bad"] if danger else theme.palette()["muted"])
        b.setIcon(icons.icon(icon, col, 16))
        b.setIconSize(QSize(16, 16))
    if tip:
        b.setToolTip(tip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    if on:
        b.clicked.connect(lambda _=False: on())
    return b


def icon_button(name: str, tip: str = "", on: Callable | None = None, kind: str = "true", color: str | None = None, size: int = 18) -> QPushButton:
    """Square 32px icon button. kind: true (ghost) | accent (filled round) | danger."""
    b = QPushButton()
    b.setProperty("iconbtn", kind)
    p = theme.palette()
    col = color or {"accent": p["accent_text"], "danger": p["bad"]}.get(kind, p["muted"])
    b.setIcon(icons.icon(name, col, size))
    b.setIconSize(QSize(size, size))
    b.setToolTip(tip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFixedSize(32, 32)
    if on:
        b.clicked.connect(lambda _=False: on())
    return b


def card(kind: str = "true") -> QFrame:
    f = QFrame()
    f.setProperty("card", kind)
    return f


def separator() -> QFrame:
    f = QFrame()
    f.setProperty("sep", True)
    return f


def clear_layout(layout) -> None:
    while layout.count():
        it = layout.takeAt(0)
        w = it.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        elif it.layout() is not None:
            clear_layout(it.layout())


class Avatar(QLabel):
    def __init__(self, emoji: str = "🤖", size: int = 36):
        super().__init__(emoji or "🤖")
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        p = theme.palette()
        self.setStyleSheet(f"background: {p['panel2']}; border-radius: {size // 3 + 2}px; font-size: {int(size * 0.5)}px;")

    def set_emoji(self, e: str) -> None:
        self.setText(e or "🤖")


class PageHeader(QWidget):
    """Title, one-line explanation and right-aligned actions: the same top of every page."""

    def __init__(self, title: str, subtitle: str = "", actions: list[QWidget] | None = None):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)
        col = QVBoxLayout()
        col.setSpacing(4)
        self.title = label(title, h1=True, wrap=False)
        col.addWidget(self.title)
        self.sub = label(subtitle, muted=True)
        if subtitle:
            col.addWidget(self.sub)
        h.addLayout(col, 1)
        for a in actions or []:
            h.addWidget(a, 0, Qt.AlignmentFlag.AlignTop)


def page_layout(w: QWidget, header: PageHeader | None = None) -> QVBoxLayout:
    v = QVBoxLayout(w)
    v.setContentsMargins(*PAGE_MARGINS)
    v.setSpacing(16)
    if header:
        v.addWidget(header)
    return v


class Section(QFrame):
    """A titled group of related fields on a quiet card."""

    def __init__(self, title: str = "", desc: str = ""):
        super().__init__()
        self.setProperty("card", "true")
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(20, 18, 20, 18)
        self.v.setSpacing(12)
        if title:
            self.v.addWidget(label(title, h2=True))
        if desc:
            self.v.addWidget(label(desc, muted=True))

    def add(self, w: QWidget | None = None, layout=None) -> None:
        if w is not None:
            self.v.addWidget(w)
        if layout is not None:
            self.v.addLayout(layout)


class SideTabs(QWidget):
    """Vertical section list + stacked pages. Drop-in for QTabWidget's addTab/setCurrentIndex/currentChanged."""
    currentChanged = Signal(int)

    def __init__(self, nav_width: int = 200):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(20)
        self.nav = QListWidget()
        self.nav.setObjectName("sidetabs")
        self.nav.setFixedWidth(nav_width)
        self.nav.setIconSize(QSize(16, 16))
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.nav.setStyleSheet("QListWidget#sidetabs { background: transparent; border: none; } "
                               "QListWidget#sidetabs::item { padding: 9px 12px; margin: 1px 0; }")
        self.stack = QStackedWidget()
        h.addWidget(self.nav)
        h.addWidget(self.stack, 1)
        self.nav.currentRowChanged.connect(self._row)

    def addTab(self, w: QWidget, text: str, icon: str | None = None) -> int:
        it = QListWidgetItem(text)
        if icon:
            it.setIcon(icons.icon(icon, theme.palette()["muted"], 16))
        self.nav.addItem(it)
        self.stack.addWidget(w)
        if self.nav.count() == 1:
            self.nav.setCurrentRow(0)
        return self.nav.count() - 1

    def _row(self, i: int) -> None:
        if i >= 0:
            self.stack.setCurrentIndex(i)
            self.currentChanged.emit(i)

    def setCurrentIndex(self, i: int) -> None:
        self.nav.setCurrentRow(i)

    def currentIndex(self) -> int:
        return self.nav.currentRow()

    def setTabText(self, i: int, text: str) -> None:
        self.nav.item(i).setText(text)


class Toasts(QObject):
    """Small non-blocking messages at the bottom of the window (replaces status bar text and most message boxes)."""

    def __init__(self, host: QWidget):
        super().__init__(host)
        self.host = host
        self.items: list[QLabel] = []

    def show(self, text: str, kind: str = "info", ms: int = 4200) -> None:
        p = theme.palette()
        col = {"ok": p["ok"], "error": p["bad"], "warn": p["warn"]}.get(kind, p["accent"])
        lb = QLabel(text, self.host)
        lb.setWordWrap(True)
        lb.setMaximumWidth(460)
        lb.setStyleSheet(f"background: {p['raised']}; color: {p['text']}; border: 1px solid {p['line2']}; border-left: 3px solid {col}; border-radius: 10px; padding: 10px 16px;")
        lb.adjustSize()
        lb.show()
        lb.raise_()
        self.items.append(lb)
        self.layout()
        QTimer.singleShot(ms, lambda: self._drop(lb))

    def _drop(self, lb: QLabel) -> None:
        if lb in self.items:
            self.items.remove(lb)
        lb.deleteLater()
        self.layout()

    def layout(self) -> None:
        y = self.host.height() - 24
        for lb in reversed(self.items):
            lb.adjustSize()
            y -= lb.height()
            lb.move(self.host.width() - lb.width() - 24, y)
            lb.raise_()
            y -= 10


class AutoMarkdown(QLabel):
    """Markdown text that wraps and sizes itself (a QLabel handles height-for-width natively, so it lives happily in a chat)."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.MarkdownText)
        self.setWordWrap(True)
        self.setOpenExternalLinks(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._text = ""
        self._pending: str | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush)
        if text:
            self.set_text(text, immediate=True)

    def set_text(self, text: str, immediate: bool = False) -> None:
        self._text = text
        if immediate:
            self._pending = None
            super().setText(text)
        else:
            self._pending = text
            if not self._timer.isActive():
                self._timer.start(90)   # throttle streaming repaints

    def append_text(self, chunk: str) -> None:
        self.set_text(self._text + chunk)

    def text(self) -> str:
        return self._text

    def _flush(self) -> None:
        if self._pending is not None:
            super().setText(self._pending)
            self._pending = None


class ImageViewer(QDialog):
    def __init__(self, pm: QPixmap, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Screenshot")
        lay = QVBoxLayout(self)
        lb = QLabel()
        lb.setPixmap(pm.scaled(QSize(1100, 760), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        lay.addWidget(lb)


class Thumb(QLabel):
    """Inline screenshot thumbnail; click to enlarge."""

    def __init__(self, pm: QPixmap, width: int = 240, parent=None):
        super().__init__(parent)
        self._pm = pm
        self.setPixmap(pm.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"border: 1px solid {theme.palette()['line2']}; border-radius: 8px;")
        self.setToolTip("Click to enlarge")

    def mousePressEvent(self, e) -> None:
        ImageViewer(self._pm, self.window()).exec()


class ImageCache:
    """Downloads screenshots from the service once and hands out pixmaps."""

    def __init__(self, api) -> None:
        self.api = api
        self.cache: dict[str, QPixmap] = {}
        self.waiting: dict[str, list[Callable[[QPixmap], None]]] = {}

    def get(self, name: str, cb: Callable[[QPixmap], None]) -> None:
        if name in self.cache:
            cb(self.cache[name])
            return
        if name in self.waiting:
            self.waiting[name].append(cb)
            return
        self.waiting[name] = [cb]

        def ok(data: bytes) -> None:
            pm = QPixmap()
            pm.loadFromData(data)
            self.cache[name] = pm
            for f in self.waiting.pop(name, []):
                f(pm)
        self.api.request("GET", f"/api/files/shots/{name}", ok, lambda e: self.waiting.pop(name, None), raw=True)


CATEGORY_VERB = {
    "send": "Send a message", "submit": "Submit a form", "purchase": "Make a purchase", "delete": "Delete something", "overwrite": "Overwrite a file",
    "command": "Run a command", "outside_workspace": "Access files outside the workspace", "login": "Log in", "access": "Grant access",
    "schedule": "Schedule unattended work", "write": "Change data in an app", "download": "Download or upload data", "install": "Install software",
}


class ApprovalCard(QFrame):
    """Approve / deny a consequential action, answer a question, or hand the browser back. Quiet but unmissable."""
    decided = Signal(str, dict)
    openBrowser = Signal(str)

    def __init__(self, a: dict, bot_name: str, parent=None):
        super().__init__(parent)
        self.a = a
        cat = a["category"]
        p = theme.palette()
        self.setProperty("card", "question" if cat == "question" else "approval")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(8)
        d = a.get("details") or {}
        icon_name, tint = {"question": ("users", p["accent"]), "takeover": ("pointer", p["warn"]), "login": ("pointer", p["warn"])}.get(cat, ("shield", p["warn"]))
        title = {"question": f"{bot_name} has a question", "takeover": f"{bot_name} needs you at the browser",
                 "login": f"{bot_name} needs you to log in" + (" to a new service" if d.get("new_service") else "")}.get(cat, CATEGORY_VERB.get(cat, a.get("title", cat)) + "?")
        head = QHBoxLayout()
        head.setSpacing(10)
        ic = QLabel()
        ic.setPixmap(icons.pixmap(icon_name, tint, 18))
        head.addWidget(ic)
        head.addWidget(label(title, h2=True, wrap=False))
        if cat not in ("question", "takeover", "login"):
            head.addWidget(label(f"· {bot_name}", muted=True, wrap=False))
        head.addStretch(1)
        head.addWidget(label(time.strftime("%H:%M", time.localtime(a["created_at"])), faint=True, wrap=False))
        lay.addLayout(head)
        lay.addWidget(label(a["summary"]))
        if a.get("tainted"):
            w = label("⚠ Content seen earlier in this task contained instruction-like text (possible prompt injection). Automatic approval is off; check this carefully.")
            w.setStyleSheet(f"color: {p['warn']};")
            lay.addWidget(w)
        extra = d.get("body") or d.get("command") or d.get("preview") or d.get("text") or d.get("prompt")
        if extra:
            box = QLabel(str(extra)[:600] + ("…" if len(str(extra)) > 600 else ""))
            box.setWordWrap(True)
            box.setFont(theme.mono())
            box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            box.setStyleSheet(f"background: {p['code']}; border-radius: 8px; padding: 10px 12px; color: {p['muted']};")
            lay.addWidget(box)
        meta = [f"{k}: {d[k]}" for k in ("to", "cc", "subject", "channel", "target", "path", "form_action", "page", "cron", "resource") if d.get(k)]
        if meta:
            lay.addWidget(label("   ·   ".join(str(m)[:90] for m in meta), faint=True))
        if d.get("auto_review"):
            lay.addWidget(label(d["auto_review"], faint=True))
        row = QHBoxLayout()
        row.setSpacing(8)
        row.setContentsMargins(0, 4, 0, 0)
        if cat == "question":
            for o in d.get("options") or []:
                row.addWidget(self._chipbtn(o, lambda o=o: self.decided.emit(a["id"], {"approve": True, "answer": o})))
            row.addStretch(1)
            lay.addLayout(row)
            r2 = QHBoxLayout()
            self.answer = QLineEdit()
            self.answer.setPlaceholderText("Type your answer…")
            self.answer.returnPressed.connect(self._send_answer)
            r2.addWidget(self.answer, 1)
            r2.addWidget(button("Send", primary=True, on=self._send_answer))
            lay.addLayout(r2)
        elif cat in ("takeover", "login"):
            row.addWidget(button("Open browser", primary=True, icon="pointer", on=lambda: self.openBrowser.emit(a["bot_id"])))
            row.addWidget(button("I'm done, hand back", on=lambda: self.decided.emit(a["id"], {"approve": True})))
            row.addWidget(button("Can't do it", flat=True, on=lambda: self.decided.emit(a["id"], {"approve": False})))
            row.addStretch(1)
            lay.addLayout(row)
        else:
            row.addWidget(button("Approve", primary=True, on=lambda: self.decided.emit(a["id"], {"approve": True})))
            row.addWidget(button("Deny", on=lambda: self.decided.emit(a["id"], {"approve": False})))
            row.addStretch(1)
            if cat != "access" and not a.get("tainted"):
                b2 = button("Always allow this", flat=True, on=lambda: self.decided.emit(a["id"], {"approve": True, "remember": True}))
                b2.setToolTip("Approve now and create a standing rule for this Bot, this kind of action and this target. Remove it any time in the Bot's settings.")
                row.addWidget(b2)
            lay.addLayout(row)

    @staticmethod
    def _chipbtn(text: str, on: Callable) -> QPushButton:
        b = QPushButton(text)
        b.setProperty("chipbtn", True)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda _=False: on())
        return b

    def _send_answer(self) -> None:
        t = self.answer.text().strip()
        if t:
            self.decided.emit(self.a["id"], {"approve": True, "answer": t})
