"""Main window: a calm sidebar (Bots, Groups, tools), the page area, tray icon, toasts and a Ctrl+K quick switcher."""
from __future__ import annotations

import glob
import os
import sys
import time
from typing import NamedTuple

from PySide6.QtCore import QEvent, QProcess, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                               QMenu, QMessageBox, QScrollArea, QSizePolicy, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget)

from core import quiet
from . import icons, theme
from .api import Api, load_ui_config, save_ui_config
from .chat_view import ChatPage
from .dialogs import BotEditor, GroupDialog, NewBotDialog
from .pages_computer import ComputerPage
from .pages_files import FilesPage
from .pages_home import DigestDialog, HomePage
from .pages_inbox import InboxPage
from .pages_plugins import PluginsPage
from .pages_routines import RoutinesPage
from .pages_settings import SettingsPage
from .pages_skills import SkillsPage
from .pages_usage_log import LogPage, UsagePage
from .quick_ask import QuickAsk
from .store import Store
from .takeover import TakeoverView
from .widgets import Avatar, ImageCache, Toasts, button, card, chip, icon_button, label, repolish

NAV = [("home", "Home", "home"), ("inbox", "Inbox", "inbox"), ("computer", "Computer", "computer"), ("files", "Files", "folder"), ("skills", "Skills", "skills"), ("routines", "Routines", "routines"),
       ("plugins", "Plugins", "plugins"), ("usage", "Usage", "usage"), ("log", "Action log", "log")]


def playwright_browser_installed() -> bool:
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or (os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ms-playwright") if os.name == "nt"
                                                         else os.path.expanduser("~/.cache/ms-playwright"))
    return bool(glob.glob(os.path.join(base, "chromium-*")))


class SideRow(QFrame):
    """A selectable sidebar row: leading visual, title, optional status line and badge."""
    clicked = Signal()

    def __init__(self, title: str, sub: str = "", leading: QWidget | None = None, tall: bool = True):
        super().__init__()
        self.setProperty("siderow", True)
        self.setProperty("checked", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        h = QHBoxLayout(self)
        h.setContentsMargins(8, theme.dp(6 if tall else 5), 10, theme.dp(6 if tall else 5))
        h.setSpacing(10)
        self.leading = leading
        if leading is not None:
            h.addWidget(leading, 0, Qt.AlignmentFlag.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.title = QLabel(title)
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        col.addWidget(self.title)
        self.sub = QLabel(sub)
        self.sub.setProperty("faint", True)
        self.sub.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.sub.setVisible(bool(sub))
        col.addWidget(self.sub)
        h.addLayout(col, 1)
        # long names give way (they are cut) before the count badge does: with a scrollbar showing, the row is narrower
        # and the badge used to be squeezed flat on its right edge
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.badge = QLabel("")
        self.badge.setProperty("badge", True)
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.badge.setFixedHeight(18)   # as tall as its 9px corner radius makes a pill; it used to stretch to the whole row
        self.badge.setMinimumWidth(18)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.hide()
        h.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_checked(self, on: bool) -> None:
        self.setProperty("checked", on)
        repolish(self)

    def set_sub(self, text: str, color: str | None = None) -> None:
        self.sub.setText(text)
        self.sub.setVisible(bool(text))
        self.sub.setStyleSheet(f"color: {color};" if color else "")

    def set_badge(self, n: int) -> None:
        self.badge.setText(str(n))
        self.badge.setVisible(n > 0)

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()


class NavRow(SideRow):
    def __init__(self, text: str, icon_name: str):
        lead = QLabel()
        lead.setFixedSize(20, 20)
        lead.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lead.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        super().__init__(text, "", lead, tall=False)
        self.icon_name = icon_name
        self.paint_icon(False)

    def paint_icon(self, active: bool) -> None:
        p = theme.palette()
        self.leading.setPixmap(icons.pixmap(self.icon_name, p["text"] if active else p["muted"], 18))  # type: ignore[union-attr]

    def set_checked(self, on: bool) -> None:
        super().set_checked(on)
        self.paint_icon(on)


class WelcomePage(QWidget):
    newBot = Signal()
    team = Signal()
    fromTemplate = Signal(str)
    openSettings = Signal()

    def __init__(self, store: Store):
        super().__init__()
        self.store = store
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        outer.addWidget(sc)
        w = QWidget()
        sc.setWidget(w)
        wrap = QHBoxLayout(w)
        wrap.setContentsMargins(40, 56, 40, 40)
        wrap.addStretch(1)
        col = QVBoxLayout()
        col.setSpacing(18)
        wrap.addLayout(col)
        wrap.addStretch(1)
        w.setMinimumWidth(0)
        inner = QWidget()
        inner.setMaximumWidth(860)
        inner.setMinimumWidth(620)
        col.addWidget(inner)
        v = QVBoxLayout(inner)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(18)
        logo = QLabel()
        logo.setPixmap(theme.app_icon(96).pixmap(56, 56))
        v.addWidget(logo)
        v.addWidget(label("Your AI teammates, on a computer of their own", h1=True))
        v.addWidget(label("Create named Bots with a job and a memory that grows. Message one with a task and the context it needs; it works across apps and websites, "
                          "keeps you posted, and comes back only when something needs your approval.", muted=True))
        self.steps = card()
        sl = QVBoxLayout(self.steps)
        sl.setContentsMargins(20, 16, 20, 16)
        sl.setSpacing(10)
        sl.addWidget(label("Get started", h2=True))
        self.step_key = self._step(sl, "Add a model key", "Anthropic, OpenAI, OpenRouter, Groq, Ollama, LM Studio…", "Open settings", self.openSettings.emit)
        self.step_bot = self._step(sl, "Create your first Bot", "Pick a role below, or describe your own.", "New Bot", self.newBot.emit)
        v.addWidget(self.steps)
        row = QHBoxLayout()
        row.addWidget(button("Create your first Bot", primary=True, icon="plus", on=self.newBot.emit))
        row.addWidget(button("Create a starter team", icon="users", on=self.team.emit))
        row.addStretch(1)
        v.addLayout(row)
        v.addSpacing(8)
        v.addWidget(label("START FROM A ROLE", eyebrow=True))
        g = QGridLayout()
        g.setSpacing(12)
        for i, t in enumerate(store.templates):
            c = card("hover")
            c.setCursor(Qt.CursorShape.PointingHandCursor)
            h = QHBoxLayout(c)
            h.setContentsMargins(14, 12, 14, 12)
            h.setSpacing(12)
            h.addWidget(Avatar(t["emoji"], 38), 0, Qt.AlignmentFlag.AlignTop)
            cc = QVBoxLayout()
            cc.setSpacing(2)
            cc.addWidget(label(t["name"], h2=True, wrap=False))
            job = label(t["job"], muted=True)
            job.setMaximumHeight(52)
            cc.addWidget(job)
            h.addLayout(cc, 1)
            c.mousePressEvent = lambda e, tid=t["id"]: self.fromTemplate.emit(tid)  # type: ignore[assignment]
            g.addWidget(c, i // 2, i % 2)
        v.addLayout(g)
        v.addStretch(1)
        store.settingsChanged.connect(self.update_steps)
        self.update_steps()

    def _step(self, lay: QVBoxLayout, title: str, desc: str, cta: str, on) -> dict:
        row = QHBoxLayout()
        row.setSpacing(12)
        tick = QLabel()
        tick.setFixedSize(22, 22)
        row.addWidget(tick)
        col = QVBoxLayout()
        col.setSpacing(0)
        t = QLabel(title)
        col.addWidget(t)
        col.addWidget(label(desc, faint=True))
        row.addLayout(col, 1)
        b = button(cta, on=on)
        row.addWidget(b)
        lay.addLayout(row)
        return {"tick": tick, "button": b, "title": t}

    def update_steps(self) -> None:
        p = theme.palette()
        have_key = any(pr.get("key_set") or not pr.get("needs_key", True) for pr in self.store.profiles)
        for st, done in ((self.step_key, have_key), (self.step_bot, bool(self.store.bots))):
            st["tick"].setPixmap(icons.pixmap("check" if done else "plus", p["ok"] if done else p["faint"], 16))
            st["button"].setVisible(not done)
            st["title"].setStyleSheet(f"color: {p['muted'] if done else p['text']};")


class Entry(NamedTuple):
    key: str
    name: str
    sub: str
    icon: str
    group: str
    hint: str = ""
    emoji: str = ""


def _score(q: str, text: str) -> int:
    """0 = no match; higher is better. Prefix beats word-start beats substring beats loose subsequence."""
    t = text.lower()
    if t.startswith(q):
        return 100
    if f" {q}" in t:
        return 80
    if q in t:
        return 60
    it = iter(t)
    return 30 if all(ch in it for ch in q) else 0


def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _ago(ts: float) -> str:
    d = max(0, time.time() - ts)
    return "just now" if d < 90 else f"{int(d // 60)} min ago" if d < 3600 else f"{int(d // 3600)} h ago" if d < 86400 else f"{int(d // 86400)} d ago"


def kbd(text: str) -> QLabel:
    k = QLabel(text)
    k.setProperty("kbd", True)
    return k


class PaletteRow(QWidget):
    def __init__(self, e: Entry):
        super().__init__()
        p = theme.palette()
        h = QHBoxLayout(self)
        h.setContentsMargins(10, theme.dp(6), 10, theme.dp(6))
        h.setSpacing(12)
        lead = QLabel(e.emoji) if e.emoji else QLabel()
        lead.setFixedSize(22, 22)
        lead.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if not e.emoji:
            lead.setPixmap(icons.pixmap(e.icon, p["muted"], 17))
        h.addWidget(lead)
        h.addWidget(QLabel(e.name))
        if e.sub:
            sub = QLabel(e.sub)
            sub.setProperty("faint", True)
            h.addWidget(sub)
        h.addStretch(1)
        for part in e.hint.split("+") if e.hint else []:
            h.addWidget(kbd(part))
        for w in self.findChildren(QLabel):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


class QuickSwitcher(QDialog):
    """Ctrl+K command palette: Bots, groups, pages and actions in sections, with shortcut hints, recents and loose matching."""
    chosen = Signal(str)

    def __init__(self, entries: list[Entry], recent: list[str] | None = None, parent=None, searcher=None):
        super().__init__(parent)
        self.searcher = searcher
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._run_search)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.resize(580, 470)
        p = theme.palette()
        self.setStyleSheet(f"QDialog {{ background: {p['panel']}; border: 1px solid {p['line2']}; border-radius: 16px; }}")
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 10)
        v.setSpacing(8)
        self.entries, self.recent = entries, recent or []
        self.input = QLineEdit()
        self.input.setPlaceholderText("Search Bots, pages and actions…")
        self.input.setProperty("search", True)
        self.input.textChanged.connect(self.filter)
        self.input.returnPressed.connect(self.accept_current)
        v.addWidget(self.input)
        self.list = QListWidget()
        self.list.setStyleSheet("QListWidget { border: none; background: transparent; } QListWidget::item { padding: 0px; margin: 1px 2px; }")
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.itemActivated.connect(lambda _: self.accept_current())
        self.list.itemClicked.connect(lambda _: self.accept_current())
        v.addWidget(self.list, 1)
        foot = QHBoxLayout()
        for key, text in (("↑↓", "move"), ("Enter", "open"), ("Esc", "close")):
            foot.addWidget(kbd(key))
            foot.addWidget(label(text, faint=True, wrap=False))
            foot.addSpacing(8)
        foot.addStretch(1)
        v.addLayout(foot)
        self.filter("")

    def _header(self, text: str) -> None:
        it = QListWidgetItem(text.upper())
        it.setFlags(Qt.ItemFlag.NoItemFlags)
        f = it.font()
        f.setPointSizeF(max(7.0, f.pointSizeF() - 2))
        f.setBold(True)
        it.setFont(f)
        it.setForeground(QColor(theme.palette()["faint"]))
        it.setSizeHint(QSize(0, theme.dp(30)))
        self.list.addItem(it)

    def _add(self, e: Entry) -> None:
        it = QListWidgetItem()
        it.setData(Qt.ItemDataRole.UserRole, e.key)
        it.setSizeHint(QSize(0, theme.dp(40)))
        self.list.addItem(it)
        self.list.setItemWidget(it, PaletteRow(e))

    def _run_search(self) -> None:
        q = self.input.text().strip()
        if self.searcher and len(q) >= 2:
            self.searcher(q, self._show_hits)

    def _show_hits(self, q: str, data: dict) -> None:
        """Message and memory hits from the service arrive a moment after the local matches; they are appended in their own section."""
        if q != self.input.text().strip():
            return   # the user kept typing
        for i in range(self.list.count() - 1, -1, -1):   # replace an earlier batch
            it = self.list.item(i)
            if it.data(Qt.ItemDataRole.UserRole + 1) == "hit":
                self.list.takeItem(i)
        hits = [Entry(f"thread:{m['thread_id']}:{m['bot_id']}", _clip(m["snippet"], 74), f"{m['where']}  ·  {_ago(m['ts'])}", "log", "Messages", "", m.get("emoji") or "")
                for m in data.get("messages", [])]
        hits += [Entry(f"memory:{m['bot_id']}", _clip(m["snippet"], 74), f"Memory  ·  {m['where']}", "bot", "Messages", "", "🧠") for m in data.get("memories", [])]
        if not hits:
            return
        had_current = self.list.currentItem() is not None and bool(self.list.currentItem().flags() & Qt.ItemFlag.ItemIsSelectable)
        self._header("Messages and memories")
        self.list.item(self.list.count() - 1).setData(Qt.ItemDataRole.UserRole + 1, "hit")
        for e in hits:
            self._add(e)
            self.list.item(self.list.count() - 1).setData(Qt.ItemDataRole.UserRole + 1, "hit")
        if not had_current:
            self._select(0, 1)

    def filter(self, text: str) -> None:
        self.list.clear()
        q = text.lower().strip()
        if len(q) >= 2 and self.searcher:
            self._search_timer.start(250)
        if q:
            scored = sorted(((max(_score(q, e.name), _score(q, e.sub) - 20), i, e) for i, e in enumerate(self.entries)), key=lambda t: (-t[0], t[1]))
            hits = [e for s, _, e in scored if s > 0]
            if hits:
                self._header("Best matches")
            for e in hits[:30]:
                self._add(e)
        else:
            by_key = {e.key: e for e in self.entries}
            rec = [by_key[k] for k in self.recent if k in by_key]
            if rec:
                self._header("Recent")
                for e in rec:
                    self._add(e)
            shown = {e.key for e in rec}
            last = ""
            for e in self.entries:
                if e.key in shown:
                    continue
                if e.group != last:
                    self._header(e.group)
                    last = e.group
                self._add(e)
        self._select(0, 1)

    def _select(self, start: int, step: int) -> None:
        i = start
        while 0 <= i < self.list.count():
            if self.list.item(i).flags() & Qt.ItemFlag.ItemIsSelectable:
                self.list.setCurrentRow(i)
                return
            i += step

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            step = 1 if e.key() == Qt.Key.Key_Down else -1
            self._select(self.list.currentRow() + step, step)
            return
        super().keyPressEvent(e)

    def accept_current(self) -> None:
        it = self.list.currentItem()
        if it and it.data(Qt.ItemDataRole.UserRole):
            self.chosen.emit(it.data(Qt.ItemDataRole.UserRole))
            self.accept()


SHORTCUTS = [
    ("Navigation", [("Ctrl+K", "Command palette: search Bots, pages and actions"), ("Ctrl+0", "Home"), ("Ctrl+1 … 9", "Jump to the 1st … 9th Bot"),
                    ("Ctrl+,", "Settings"), ("Ctrl+/", "This list")]),
    ("Create", [("Ctrl+N", "New Bot"), ("Ctrl+J", "Quick ask: message any Bot without opening its chat"), ("Ctrl+Alt+Space", "Quick ask from anywhere on your PC (can be turned off)")]),
    ("Find", [("Ctrl+K", "Then type: also searches every chat and memory")]),
    ("In a chat", [("Enter", "Send"), ("Shift+Enter", "New line"), ("/", "Slash commands (type / to see them)"), ("Tab", "Complete the highlighted command"),
                   ("Esc", "Close the command list")]),
]


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        self.setMinimumWidth(500)
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 22, 24, 20)
        v.setSpacing(8)
        v.addWidget(label("Keyboard shortcuts", h1=True))
        for title, rows in SHORTCUTS:
            head = label(title.upper(), eyebrow=True)
            head.setContentsMargins(0, 10, 0, 0)
            v.addWidget(head)
            for keys, what in rows:
                r = QHBoxLayout()
                r.addWidget(label(what, wrap=False), 1)
                for part in keys.split("+"):
                    r.addWidget(kbd(part.strip()))
                v.addLayout(r)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button("Close", primary=True, on=self.accept))
        v.addSpacing(8)
        v.addLayout(row)


class MainWindow(QMainWindow):
    quitRequested = Signal(bool)
    reconnect = Signal(object)
    themeRequested = Signal(str)
    appPrefsChanged = Signal()

    def __init__(self, api: Api, store: Store, tray_available: bool = True):
        super().__init__()
        self.api, self.store = api, store
        self.images = ImageCache(api)
        self.takeovers: dict[str, TakeoverView] = {}
        self.setWindowTitle("OpenGrokBot")
        self.setWindowIcon(theme.app_icon())
        self.resize(1360, 860)
        self.setMinimumSize(980, 620)
        self.really_quit = False
        self.last_notification: dict = {}
        self.banner_shown = False
        self.rows: dict[str, SideRow] = {}
        self.current_key = ""
        self._pins: set[str] = set(load_ui_config().get("pinned_bots", []))
        self._qa: QuickAsk | None = None
        api.on_unhandled_error = lambda m: self.toast(m, "error")

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(self._build_sidebar())

        self.stack = QStackedWidget()
        h.addWidget(self.stack, 1)
        self.welcome = WelcomePage(store)
        self.welcome.newBot.connect(self.new_bot)
        self.welcome.team.connect(self.create_team)
        self.welcome.fromTemplate.connect(self.new_bot_from_template)
        self.welcome.openSettings.connect(lambda: self.select("page:settings"))
        self.chat = ChatPage(api, store, self.images)
        self.pages: dict[str, QWidget] = {
            "home": HomePage(api, store), "inbox": InboxPage(api, store), "computer": ComputerPage(api, store), "files": FilesPage(api, store), "skills": SkillsPage(api, store), "routines": RoutinesPage(api, store),
            "plugins": PluginsPage(api, store), "usage": UsagePage(api, store), "log": LogPage(api, store), "settings": SettingsPage(api, store),
        }
        for w in (self.welcome, self.chat, *self.pages.values()):
            self.stack.addWidget(w)

        self.chat.openBrowser.connect(self.open_takeover)
        self.chat.editBot.connect(self.edit_bot)
        self.chat.editGroup.connect(self.edit_group)
        self.chat.exportBot.connect(self.export_bot)
        self.chat.pins = self._pins
        self.chat.duplicateBot.connect(self.duplicate_bot)
        self.chat.pinBot.connect(self.toggle_pin)
        self.chat.toast.connect(self.toast)
        home: HomePage = self.pages["home"]  # type: ignore[assignment]
        home.openBot.connect(self.show_bot)
        home.openThread.connect(self.open_thread)
        home.openPage.connect(self.show_page)
        home.newBot.connect(self.new_bot)
        self.pages["inbox"].openBrowser.connect(self.open_takeover)  # type: ignore[attr-defined]
        self.pages["inbox"].openThread.connect(self.open_thread)  # type: ignore[attr-defined]
        self.pages["computer"].takeOver.connect(lambda bid, follow: self.open_takeover(bid, follow))  # type: ignore[attr-defined]
        self.pages["skills"].openThread.connect(self.open_thread)  # type: ignore[attr-defined]
        self.pages["skills"].followAlong.connect(lambda bid, f: self.open_takeover(bid, True))  # type: ignore[attr-defined]
        self.pages["routines"].openThread.connect(self.open_thread)  # type: ignore[attr-defined]
        st: SettingsPage = self.pages["settings"]  # type: ignore[assignment]
        st.themeChanged.connect(self.themeRequested.emit)
        st.appPrefsChanged.connect(self.appPrefsChanged.emit)
        st.switchConnection.connect(self.reconnect.emit)
        st.restartService.connect(lambda: self.reconnect.emit("restart"))
        st.toast.connect(self.toast)

        self.toasts = Toasts(self)
        for page in self.pages.values():
            if hasattr(page, "toast"):
                page.toast.connect(self.toast)  # type: ignore[attr-defined]

        store.botsChanged.connect(self.refresh_lists)
        store.groupsChanged.connect(self.refresh_lists)
        store.busyChanged.connect(self.refresh_lists)
        store.approvalsChanged.connect(self.refresh_lists)
        store.connectionChanged.connect(self.set_connected)
        store.notification.connect(self.on_notification)
        store.settingsChanged.connect(lambda: self.set_connected(self.store.connected))
        self._quiet_timer = QTimer(self)
        self._quiet_timer.timeout.connect(lambda: self.set_connected(self.store.connected))
        self._quiet_timer.start(30_000)

        QShortcut(QKeySequence("Ctrl+K"), self, activated=self.quick_switch)
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self.new_bot)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=lambda: self.select("page:settings"))
        QShortcut(QKeySequence("Ctrl+/"), self, activated=self.show_shortcuts)
        QShortcut(QKeySequence("Ctrl+0"), self, activated=lambda: self.select("page:home"))
        QShortcut(QKeySequence("Ctrl+J"), self, activated=self.quick_ask)
        for n in range(1, 10):
            QShortcut(QKeySequence(f"Ctrl+{n}"), self, activated=lambda n=n: self.jump_to_bot(n - 1))

        self.tray: QSystemTrayIcon | None = None
        if tray_available and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(theme.app_icon(), self)
            self.tray.setToolTip("OpenGrokBot: your Bots keep working in the background")
            m = QMenu()
            m.addAction("Open OpenGrokBot", self.show_window)
            m.addAction("Quick ask…", lambda: self.quick_ask(True))
            self.tray_needs = m.addAction("Needs you: 0", lambda: self.select("page:inbox"))
            m.addAction("Stop all running tasks", self.stop_all)
            m.addAction("Pause all Bots", lambda: self.pause_all(True))
            m.addAction("Resume all Bots", lambda: self.pause_all(False))
            m.addSeparator()
            m.addAction("Quit app (Bots keep running)", lambda: self.quit_app(False))
            m.addAction("Quit and stop all Bots", lambda: self.quit_app(True))
            self.tray.setContextMenu(m)
            self.tray.activated.connect(lambda r: self.show_window() if r in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None)
            self.tray.messageClicked.connect(self.open_last_notification)
            self.tray.show()
        self.set_connected(store.connected)
        self.refresh_lists()
        QTimer.singleShot(1500, self.check_browser_engine)

    # ------------------------------------------------------------------ sidebar
    def _build_sidebar(self) -> QFrame:
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(272)
        v = QVBoxLayout(side)
        v.setContentsMargins(12, 16, 12, 12)
        v.setSpacing(10)
        head = QHBoxLayout()
        head.setContentsMargins(6, 0, 4, 0)
        logo = QLabel()
        logo.setPixmap(theme.app_icon(64).pixmap(26, 26))
        head.addWidget(logo)
        t = QLabel("OpenGrokBot")
        t.setStyleSheet("font-size: 15px; font-weight: 600;")
        head.addWidget(t)
        head.addStretch(1)
        self.conn_dot = QLabel()
        self.conn_dot.setFixedSize(10, 10)
        head.addWidget(self.conn_dot)
        v.addLayout(head)
        v.addWidget(button("New Bot", primary=True, icon="plus", on=self.new_bot, tip="New Bot (Ctrl+N)"))
        self.search_btn = button("Search…   Ctrl+K", flat=True, icon="search", on=self.quick_switch)
        self.search_btn.setStyleSheet(f"text-align: left; color: {theme.palette()['faint']};")
        self.search_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        v.addWidget(self.search_btn)

        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        self.lists = QVBoxLayout(inner)
        self.lists.setContentsMargins(0, 4, 0, 4)
        self.lists.setSpacing(2)
        self.bots_head = label("BOTS", eyebrow=True)
        self.bots_head.setContentsMargins(8, 4, 0, 2)
        self.lists.addWidget(self.bots_head)
        self.bots_box = QVBoxLayout()
        self.bots_box.setSpacing(2)
        self.lists.addLayout(self.bots_box)
        gh = QHBoxLayout()
        self.groups_head = label("GROUPS", eyebrow=True)
        self.groups_head.setContentsMargins(8, 12, 0, 2)
        gh.addWidget(self.groups_head)
        gh.addStretch(1)
        self.group_add = icon_button("plus", "New group chat", self.new_group, size=14)
        self.group_add.setFixedSize(24, 24)
        gh.addWidget(self.group_add)
        self.lists.addLayout(gh)
        self.groups_box = QVBoxLayout()
        self.groups_box.setSpacing(2)
        self.lists.addLayout(self.groups_box)
        self.lists.addStretch(1)
        sc.setWidget(inner)
        # leave room for the scrollbar only while it is showing, so it never sits on top of a badge or the + button
        sc.verticalScrollBar().rangeChanged.connect(lambda lo, hi: self.lists.setContentsMargins(0, 4, 8 if hi > lo else 0, 4))
        v.addWidget(sc, 1)

        sep = QFrame()
        sep.setProperty("sep", True)
        v.addWidget(sep)
        for key, text, ic in NAV:
            r = NavRow(text, ic)
            r.clicked.connect(lambda k=key: self.select(f"page:{k}"))
            self.rows[f"page:{key}"] = r
            v.addWidget(r)
        r = NavRow("Settings", "settings")
        r.clicked.connect(lambda: self.select("page:settings"))
        self.rows["page:settings"] = r
        v.addWidget(r)
        self.foot = label("", faint=True, wrap=False)
        self.foot.setContentsMargins(8, 2, 0, 0)
        v.addWidget(self.foot)
        return side

    def _bot_row(self, b: dict) -> SideRow:
        r = SideRow(b["name"], "Idle", Avatar(b.get("emoji") or "🤖", 32))
        r.clicked.connect(lambda bid=b["id"]: self.select(f"bot:{bid}"))
        return r

    def _group_row(self, g: dict) -> SideRow:
        lead = QLabel()
        lead.setPixmap(icons.pixmap("users", theme.palette()["muted"], 18))
        lead.setFixedSize(32, 32)
        lead.setAlignment(Qt.AlignmentFlag.AlignCenter)
        r = SideRow(g["name"], ", ".join(g["member_names"])[:40], lead)
        r.clicked.connect(lambda gid=g["id"]: self.select(f"group:{gid}"))
        return r

    def refresh_lists(self) -> None:
        p = theme.palette()
        want = {f"bot:{b['id']}": b for b in self.store.bots}
        for key in [k for k in self.rows if k.startswith("bot:") and k not in want]:
            self.rows.pop(key).deleteLater()
        for b in self.store.bots:
            key = f"bot:{b['id']}"
            if key not in self.rows:
                self.rows[key] = self._bot_row(b)
                self.bots_box.addWidget(self.rows[key])
            r = self.rows[key]
            kind, text = self.store.state_of(b["id"])
            r.title.setText(("★  " if b["id"] in self._pins else "") + b["name"] + ("  ⏸" if b["paused"] else ""))
            if isinstance(r.leading, Avatar):
                r.leading.set_emoji(b.get("emoji") or "🤖")
            r.set_sub({"idle": "Idle", "work": text.capitalize(), "wait": "Needs you", "takeover": "You're driving"}[kind],
                      {"idle": None, "work": p["accent"], "wait": p["warn"], "takeover": p["warn"]}[kind])
            r.set_badge(len(self.store.pending_for_bot(b["id"])))
            r.setToolTip(b.get("job", ""))
        # keep bot rows in store order
        for i, b in enumerate(self.ordered_bots()):
            self.bots_box.insertWidget(i, self.rows[f"bot:{b['id']}"])
        gwant = {f"group:{g['id']}": g for g in self.store.groups}
        for key in [k for k in self.rows if k.startswith("group:") and k not in gwant]:
            self.rows.pop(key).deleteLater()
        for g in self.store.groups:
            key = f"group:{g['id']}"
            if key not in self.rows:
                self.rows[key] = self._group_row(g)
                self.groups_box.addWidget(self.rows[key])
            self.rows[key].title.setText(g["name"])
            self.rows[key].set_sub(", ".join(g["member_names"])[:40])
        self.groups_head.setVisible(True)
        n = len(self.store.approvals)
        self.rows["page:inbox"].set_badge(n)
        self.setWindowTitle("OpenGrokBot" + (f"  ·  {n} need you" if n else ""))
        if self.tray:
            self.tray_needs.setText(f"Needs you: {n}")
        self.bots_head.setText(f"BOTS  ·  {len(self.store.bots)}" if self.store.bots else "BOTS")
        for k, r in self.rows.items():
            r.set_checked(k == self.current_key)
        if not self.store.bots and self.current_key.startswith("bot:"):
            self.show_welcome()

    def set_connected(self, ok: bool) -> None:
        p = theme.palette()
        self.conn_dot.setStyleSheet(f"background: {p['ok'] if ok else p['bad']}; border-radius: 5px;")
        mode = self.api.conn.mode
        self.conn_dot.setToolTip("Connected to the background service" if ok else "Reconnecting to the background service…")
        self.foot.setText(("Connected" if ok else "Reconnecting…") + ("  ·  this PC" if mode == "local" else "  ·  remote computer")
                          + ("  ·  Do Not Disturb" if quiet.is_quiet(self.store.settings.get("notifications", {})) else ""))

    # --------------------------------------------------------------- navigation
    def select(self, key: str) -> None:
        kind, _, ident = key.partition(":")
        self.current_key = key
        if kind == "bot":
            self.stack.setCurrentWidget(self.chat)
            self.chat.show_bot(ident)
        elif kind == "group":
            self.stack.setCurrentWidget(self.chat)
            self.chat.show_group(ident)
        else:
            self.stack.setCurrentWidget(self.pages[ident])
        for k, r in self.rows.items():
            r.set_checked(k == key)
        if self.stack.currentWidget() is not self.welcome:
            self.show_window()

    def show_welcome(self) -> None:
        self.current_key = ""
        self.stack.setCurrentWidget(self.welcome)
        for r in self.rows.values():
            r.set_checked(False)

    def show_page(self, key: str) -> None:
        self.select(f"page:{key}")

    def show_bot(self, bot_id: str, thread_id: str | None = None, draft: str | None = None) -> None:
        self.current_key = f"bot:{bot_id}"
        self.stack.setCurrentWidget(self.chat)
        self.chat.show_bot(bot_id, thread_id, draft)
        for k, r in self.rows.items():
            r.set_checked(k == self.current_key)

    def show_group(self, gid: str) -> None:
        self.select(f"group:{gid}")

    def start_page(self) -> None:
        if not self.store.bots:
            self.show_welcome()
        else:
            self.select("page:home")

    def open_thread(self, thread_id: str, bot_id: str = "") -> None:
        g = self.store.group_by_thread(thread_id)
        if g:
            self.show_group(g["id"])
        elif bot_id:
            self.show_bot(bot_id, thread_id)
        self.show_window()

    def show_window(self) -> None:
        self.showNormal() if self.isMinimized() else self.show()
        self.raise_()
        self.activateWindow()

    def ordered_bots(self) -> list[dict]:
        """Pinned Bots first (in their usual order), then the rest."""
        return sorted(self.store.bots, key=lambda b: b["id"] not in self._pins)

    def jump_to_bot(self, i: int) -> None:
        bots = self.ordered_bots()
        if i < len(bots):
            self.select(f"bot:{bots[i]['id']}")

    def toggle_pin(self, bot_id: str) -> None:
        (self._pins.discard if bot_id in self._pins else self._pins.add)(bot_id)
        cfg = load_ui_config()
        cfg["pinned_bots"] = sorted(self._pins)
        save_ui_config(cfg)
        self.refresh_lists()
        self.toast("Pinned to the top." if bot_id in self._pins else "Unpinned.")

    def duplicate_bot(self, bot_id: str) -> None:
        b = self.store.bot(bot_id)
        if not b:
            return
        name, ok = QInputDialog.getText(self, "Duplicate Bot", "Name for the copy. It keeps the job, instructions and model, but starts with no memory, chats or access.", text=f"{b['name']} copy")
        if not ok or not name.strip():
            return
        self.api.post(f"/api/bots/{bot_id}/duplicate", {"name": name.strip()},
                      lambda nb: self.store.refresh_all(lambda: (self.show_bot(nb["id"]), self.toast(f"Created {nb['name']}.", "ok"))), lambda e: self.toast(e, "error"))

    def pause_all(self, paused: bool) -> None:
        self.api.post("/api/bots/pause_all", {"paused": paused}, lambda r: (self.store.refresh_bots(), self.toast(
            (f"Paused {r['changed']} Bot{'s' if r['changed'] != 1 else ''}." if paused else f"Resumed {r['changed']} Bot{'s' if r['changed'] != 1 else ''}.") if r["changed"] else
            ("Every Bot was already paused." if paused else "No Bot was paused."))), lambda e: self.toast(e, "error"))

    def quick_ask(self, from_hotkey: bool = False) -> None:
        if self._qa is None:
            self._qa = QuickAsk(self.api, self.store)
            self._qa.sent.connect(self.toast)
        self._qa.present(self.chat.bot_id if self.current_key.startswith("bot:") else "")

    def palette_entries(self) -> list[Entry]:
        out: list[Entry] = []
        for i, b in enumerate(self.ordered_bots()):
            kind, text = self.store.state_of(b["id"])
            sub = {"idle": "Bot", "work": f"Bot  ·  {text}", "wait": "Bot  ·  needs you", "takeover": "Bot  ·  you are driving"}[kind]
            out.append(Entry(f"bot:{b['id']}", b["name"], sub, "bot", "Bots", f"Ctrl+{i + 1}" if i < 9 else "", b.get("emoji") or ""))
        for g in self.store.groups:
            out.append(Entry(f"group:{g['id']}", g["name"], "Group chat", "users", "Groups"))
        for key, text, ic in NAV:
            out.append(Entry(f"page:{key}", text, "Page", ic, "Pages", "Ctrl+0" if key == "home" else ""))
        out.append(Entry("page:settings", "Settings", "Page", "settings", "Pages", "Ctrl+,"))
        out.append(Entry("action:new", "New Bot…", "", "plus", "Actions", "Ctrl+N"))
        out.append(Entry("action:group", "New group chat…", "", "users", "Actions"))
        out.append(Entry("action:import", "Import a Bot package…", "", "download", "Actions"))
        out.append(Entry("action:theme", "Switch to light theme" if theme.theme_name() == "dark" else "Switch to dark theme", "", "moon", "Actions"))
        if self.store.busy:
            out.append(Entry("action:stopall", f"Stop all running tasks ({len(self.store.busy)})", "", "stop", "Actions"))
        if quiet.is_quiet({"dnd_until": self.store.settings.get("notifications", {}).get("dnd_until", 0)}):
            out.append(Entry("action:dndoff", "End Do Not Disturb", "", "alert", "Actions"))
        else:
            out.append(Entry("action:dnd1", "Do Not Disturb for 1 hour", "", "alert", "Actions"))
        if self.current_key.startswith(("bot:", "group:")):
            out.append(Entry("action:export", "Export this chat as Markdown…", "", "download", "Actions"))
        out.append(Entry("action:ask", "Quick ask…", "send a message to any Bot", "send", "Actions", "Ctrl+J"))
        out.append(Entry("action:digest", "Digest: what did my Bots do today?", "", "log", "Actions"))
        if any(not b["paused"] for b in self.store.bots):
            out.append(Entry("action:pauseall", "Pause all Bots", "", "pause", "Actions"))
        elif self.store.bots:
            out.append(Entry("action:resumeall", "Resume all Bots", "", "play", "Actions"))
        if self.current_key.startswith("bot:"):
            bid = self.current_key.split(":", 1)[1]
            out.append(Entry("action:pin", "Unpin this Bot" if bid in self._pins else "Pin this Bot to the top", "", "bot", "Actions"))
            out.append(Entry("action:dup", "Duplicate this Bot…", "", "plus", "Actions"))
        out.append(Entry("action:keys", "Keyboard shortcuts", "", "keyboard", "Actions", "Ctrl+/"))
        return out

    def quick_switch(self) -> None:
        d = QuickSwitcher(self.palette_entries(), load_ui_config().get("recent", []), self, self._search)
        d.chosen.connect(self._quick_chosen)
        g = self.geometry()
        d.move(g.x() + (g.width() - d.width()) // 2, g.y() + 90)
        d.exec()

    def _search(self, text: str, done) -> None:
        self.api.get("/api/search", lambda d: done(text, d), lambda _e: None, params={"q": text, "limit": 12})

    def _set_dnd(self, secs: int) -> None:
        until = time.time() + secs if secs > 0 else 0
        self.api.put("/api/settings", {"notifications.dnd_until": until}, lambda s: (setattr(self.store, "settings", s), self.store.settingsChanged.emit(),
                                                                                  self.toast("Do Not Disturb until " + time.strftime("%H:%M", time.localtime(until)) + "." if until else "Do Not Disturb is off.")))

    def _quick_chosen(self, key: str) -> None:
        cfg = load_ui_config()
        cfg["recent"] = ([key] + [k for k in cfg.get("recent", []) if k != key])[:5]
        save_ui_config(cfg)
        if key == "action:new":
            self.new_bot()
        elif key == "action:group":
            self.new_group()
        elif key == "action:import":
            self.import_bot()
        elif key == "action:theme":
            t = "light" if theme.theme_name() == "dark" else "dark"
            self.api.put("/api/settings", {"theme": t}, lambda s: (setattr(self.store, "settings", s), self.themeRequested.emit(t)))
        elif key == "action:stopall":
            self.stop_all()
            self.toast("Asked every running Bot to stop.")
        elif key == "action:keys":
            self.show_shortcuts()
        elif key == "action:ask":
            self.quick_ask()
        elif key == "action:digest":
            DigestDialog(self.api, self).exec()
        elif key == "action:pauseall":
            self.pause_all(True)
        elif key == "action:resumeall":
            self.pause_all(False)
        elif key == "action:pin":
            self.toggle_pin(self.current_key.split(":", 1)[1])
        elif key == "action:dup":
            self.duplicate_bot(self.current_key.split(":", 1)[1])
        elif key == "action:dnd1":
            self._set_dnd(3600)
        elif key == "action:dndoff":
            self._set_dnd(0)
        elif key == "action:export":
            self.chat.export_chat()
        elif key.startswith("thread:"):
            _, tid, bid = key.split(":", 2)
            self.open_thread(tid, bid)
        elif key.startswith("memory:"):
            self.select(f"bot:{key.split(':', 1)[1]}")
        else:
            self.select(key)

    def show_shortcuts(self) -> None:
        ShortcutsDialog(self).exec()

    # ------------------------------------------------------------------- actions
    def toast(self, text: str, kind: str = "info") -> None:
        self.toasts.show(text, kind)

    def new_bot(self) -> None:
        d = NewBotDialog(self.api, self.store, self)
        d.created.connect(lambda bot, example: self._after_create(bot, example))
        d.exec()

    def new_bot_from_template(self, tid: str) -> None:
        d = NewBotDialog(self.api, self.store, self)
        for i in range(d.list.count()):
            it = d.list.item(i).data(Qt.ItemDataRole.UserRole)
            if it and it["id"] == tid:
                d.list.setCurrentRow(i)
        d.created.connect(lambda bot, example: self._after_create(bot, example))
        d.exec()

    def _after_create(self, bot: dict, example: str) -> None:
        self.store.refresh_all(lambda: self.show_bot(bot["id"], None, example or None))

    def create_team(self) -> None:
        self.api.post("/api/teams", {}, lambda d: self.store.refresh_all(lambda: (self.show_group(d["group"]["id"]) if d.get("group") else self.start_page())),
                      lambda e: self.toast(e, "error"))

    def new_group(self) -> None:
        if not self.store.bots:
            self.toast("Create a Bot first.", "warn")
            return
        if GroupDialog(self.api, self.store, None, self).exec():
            self.store.refresh_groups()

    def edit_group(self, gid: str) -> None:
        g = self.store.group(gid)
        if g and GroupDialog(self.api, self.store, g, self).exec():
            self.store.refresh_groups()

    def edit_bot(self, bot_id: str) -> None:
        d = BotEditor(self.api, self.store, bot_id, self)
        d.deleted.connect(lambda _id: self.start_page())
        d.exec()
        self.chat._refresh_header()

    def export_bot(self, bot_id: str) -> None:
        b = self.store.bot(bot_id)
        path, _ = QFileDialog.getSaveFileName(self, "Export Bot package", f"{b['name'] if b else 'bot'}.gbbot", "Bot package (*.gbbot)")
        if path:
            def ok(data: bytes) -> None:
                with open(path, "wb") as f:
                    f.write(data)
                self.toast("Exported: role, skills and routines only. No secrets, no conversations.", "ok")
            self.api.request("GET", f"/api/bots/{bot_id}/export", ok, lambda e: self.toast(e, "error"), raw=True)

    def import_bot(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import a Bot package", "", "Bot package (*.gbbot *.zip)")
        if not path:
            return
        with open(path, "rb") as f:
            data = f.read()

        def ok(d: dict) -> None:
            need = ", ".join(d.get("connectors_needed", [])) or "none"
            QMessageBox.information(self, "Imported", f"Imported {d['bot']['name']}.\nSkills (as drafts to review): {len(d['skills'])}\nRoutines (disabled until you enable them): {len(d['routines'])}\n"
                                                      f"Connectors it used on the original: {need}. Grant them again on your account.")
            self.store.refresh_all(lambda: self.show_bot(d["bot"]["id"]))
        self.api.request("POST", "/api/bots/import", ok, lambda e: self.toast(e, "error"), content=data)

    def open_takeover(self, bot_id: str, follow: bool = False) -> None:
        v = self.takeovers.get(bot_id)
        if v is None:
            v = TakeoverView(self.api, self.store, bot_id, self)
            v.skillDrafted.connect(lambda name: self.select("page:skills"))
            self.takeovers[bot_id] = v
        v.begin()
        if follow:
            QTimer.singleShot(400, v.toggle_record)

    def stop_all(self) -> None:
        for bid in list(self.store.busy):
            self.api.post(f"/api/bots/{bid}/stop", {})

    # -------------------------------------------------------------- notifications
    def on_notification(self, n: dict) -> None:
        self.last_notification = n
        if n.get("muted"):   # quiet hours / Do Not Disturb: it is in the Inbox, nothing pops up or beeps
            return
        here = self.isActiveWindow() and self.stack.currentWidget() is self.chat and self.chat.thread_id == n.get("thread_id")
        cfg = self.store.settings.get("notifications", {})
        if here:
            return
        if self.isActiveWindow():
            self.toast(f"{n['title']}" + (f"\n{n['body']}" if n.get("body") else ""), "warn" if n.get("urgent") else "info")
        elif cfg.get("toast", True) and self.tray:
            self.tray.showMessage(n["title"], n.get("body", ""), QSystemTrayIcon.MessageIcon.Information, 9000)
        if cfg.get("sound", True) and n.get("urgent"):
            QApplication.beep()

    def open_last_notification(self) -> None:
        n = self.last_notification
        if n.get("thread_id"):
            self.open_thread(n["thread_id"], n.get("bot_id", ""))
        else:
            self.show_window()

    def check_browser_engine(self) -> None:
        if self.banner_shown or playwright_browser_installed():
            return
        self.banner_shown = True
        r = QMessageBox.question(self, "Install the browser engine",
                                 "Bots use a Chromium browser (Playwright) as their computer-use browser. It is not installed on this PC yet (about 150 MB).\n\nInstall it now?")
        if r != QMessageBox.StandardButton.Yes:
            return
        self.proc = QProcess(self)
        args = ["--install-browsers"] if getattr(sys, "frozen", False) else [os.path.abspath(sys.argv[0]), "--install-browsers"]
        self.toast("Installing the browser engine… this can take a few minutes.")
        self.proc.finished.connect(lambda code, _s: self.toast("Browser engine installed." if code == 0 else "Install failed. Run: python -m playwright install chromium",
                                                               "ok" if code == 0 else "error"))
        self.proc.start(sys.executable, args)

    # ------------------------------------------------------------------ lifecycle
    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if hasattr(self, "toasts"):
            self.toasts.layout()

    def quit_app(self, stop_service: bool) -> None:
        self.really_quit = True
        self.quitRequested.emit(stop_service)

    def closeEvent(self, e: QCloseEvent) -> None:
        cfg = load_ui_config()
        if not self.really_quit and self.tray and cfg.get("close_to_tray", True):
            e.ignore()
            self.hide()
            if not cfg.get("tray_hint_shown"):
                self.tray.showMessage("Still running", "Your Bots keep working in the background. Use the tray icon to open the app or quit.", QSystemTrayIcon.MessageIcon.Information, 6000)
                save_ui_config({**cfg, "tray_hint_shown": True})
            return
        self.really_quit = True
        self.quitRequested.emit(False)
        e.accept()
