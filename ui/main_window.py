"""Main window: a calm sidebar (Bots, Groups, tools), the page area, tray icon, toasts and a Ctrl+K quick switcher."""
from __future__ import annotations

import glob
import os
import sys

from PySide6.QtCore import QEvent, QProcess, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                               QMenu, QMessageBox, QScrollArea, QSizePolicy, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget)

from . import icons, theme
from .api import Api, load_ui_config, save_ui_config
from .chat_view import ChatPage
from .dialogs import BotEditor, GroupDialog, NewBotDialog
from .pages_computer import ComputerPage
from .pages_inbox import InboxPage
from .pages_plugins import PluginsPage
from .pages_routines import RoutinesPage
from .pages_settings import SettingsPage
from .pages_skills import SkillsPage
from .pages_usage_log import LogPage, UsagePage
from .store import Store
from .takeover import TakeoverView
from .widgets import Avatar, ImageCache, Toasts, button, card, chip, icon_button, label, repolish

NAV = [("inbox", "Inbox", "inbox"), ("computer", "Computer", "computer"), ("skills", "Skills", "skills"), ("routines", "Routines", "routines"),
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
        h.setContentsMargins(8, 6 if tall else 5, 10, 6 if tall else 5)
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
        self.badge = QLabel("")
        self.badge.setProperty("badge", True)
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.badge.hide()
        h.addWidget(self.badge)

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


class QuickSwitcher(QDialog):
    """Ctrl+K: jump to any Bot, group, page or action."""
    chosen = Signal(str)

    def __init__(self, entries: list[tuple[str, str, str, str]], parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.resize(520, 420)
        p = theme.palette()
        self.setStyleSheet(f"QDialog {{ background: {p['panel']}; border: 1px solid {p['line2']}; border-radius: 14px; }}")
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        self.entries = entries
        self.input = QLineEdit()
        self.input.setPlaceholderText("Jump to a Bot, group or page…")
        self.input.setProperty("search", True)
        self.input.textChanged.connect(self.filter)
        self.input.returnPressed.connect(self.accept_current)
        v.addWidget(self.input)
        self.list = QListWidget()
        self.list.setStyleSheet("QListWidget { border: none; background: transparent; }")
        self.list.itemActivated.connect(lambda _: self.accept_current())
        self.list.itemClicked.connect(lambda _: self.accept_current())
        v.addWidget(self.list, 1)
        self.filter("")

    def filter(self, text: str) -> None:
        self.list.clear()
        t = text.lower().strip()
        for key, name, sub, icon_name in self.entries:
            if t and t not in name.lower() and t not in sub.lower():
                continue
            it = QListWidgetItem(icons.icon(icon_name, theme.palette()["muted"], 16), f"{name}" + (f"   {sub}" if sub else ""))
            it.setData(Qt.ItemDataRole.UserRole, key)
            self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            r = self.list.currentRow() + (1 if e.key() == Qt.Key.Key_Down else -1)
            self.list.setCurrentRow(max(0, min(self.list.count() - 1, r)))
            return
        super().keyPressEvent(e)

    def accept_current(self) -> None:
        it = self.list.currentItem()
        if it:
            self.chosen.emit(it.data(Qt.ItemDataRole.UserRole))
        self.accept()


class MainWindow(QMainWindow):
    quitRequested = Signal(bool)
    reconnect = Signal(object)
    themeRequested = Signal(str)

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
            "inbox": InboxPage(api, store), "computer": ComputerPage(api, store), "skills": SkillsPage(api, store), "routines": RoutinesPage(api, store),
            "plugins": PluginsPage(api, store), "usage": UsagePage(api, store), "log": LogPage(api, store), "settings": SettingsPage(api, store),
        }
        for w in (self.welcome, self.chat, *self.pages.values()):
            self.stack.addWidget(w)

        self.chat.openBrowser.connect(self.open_takeover)
        self.chat.editBot.connect(self.edit_bot)
        self.chat.editGroup.connect(self.edit_group)
        self.chat.exportBot.connect(self.export_bot)
        self.chat.toast.connect(self.toast)
        self.pages["inbox"].openBrowser.connect(self.open_takeover)  # type: ignore[attr-defined]
        self.pages["inbox"].openThread.connect(self.open_thread)  # type: ignore[attr-defined]
        self.pages["computer"].takeOver.connect(lambda bid, follow: self.open_takeover(bid, follow))  # type: ignore[attr-defined]
        self.pages["skills"].openThread.connect(self.open_thread)  # type: ignore[attr-defined]
        self.pages["skills"].followAlong.connect(lambda bid, f: self.open_takeover(bid, True))  # type: ignore[attr-defined]
        self.pages["routines"].openThread.connect(self.open_thread)  # type: ignore[attr-defined]
        st: SettingsPage = self.pages["settings"]  # type: ignore[assignment]
        st.themeChanged.connect(self.themeRequested.emit)
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

        QShortcut(QKeySequence("Ctrl+K"), self, activated=self.quick_switch)
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self.new_bot)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=lambda: self.select("page:settings"))

        self.tray: QSystemTrayIcon | None = None
        if tray_available and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(theme.app_icon(), self)
            self.tray.setToolTip("OpenGrokBot: your Bots keep working in the background")
            m = QMenu()
            m.addAction("Open OpenGrokBot", self.show_window)
            self.tray_needs = m.addAction("Needs you: 0", lambda: self.select("page:inbox"))
            m.addAction("Stop all running tasks", self.stop_all)
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
            r.title.setText(b["name"] + ("  ⏸" if b["paused"] else ""))
            if isinstance(r.leading, Avatar):
                r.leading.set_emoji(b.get("emoji") or "🤖")
            r.set_sub({"idle": "Idle", "work": text.capitalize(), "wait": "Needs you", "takeover": "You're driving"}[kind],
                      {"idle": None, "work": p["accent"], "wait": p["warn"], "takeover": p["warn"]}[kind])
            r.set_badge(len(self.store.pending_for_bot(b["id"])))
            r.setToolTip(b.get("job", ""))
        # keep bot rows in store order
        for i, b in enumerate(self.store.bots):
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
        self.foot.setText(("Connected" if ok else "Reconnecting…") + ("  ·  this PC" if mode == "local" else "  ·  remote computer"))

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
            self.show_bot(self.store.bots[0]["id"])

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

    def quick_switch(self) -> None:
        entries: list[tuple[str, str, str, str]] = []
        for b in self.store.bots:
            entries.append((f"bot:{b['id']}", f"{b['emoji']}  {b['name']}", "Bot", "bot"))
        for g in self.store.groups:
            entries.append((f"group:{g['id']}", g["name"], "Group chat", "users"))
        for key, text, ic in NAV:
            entries.append((f"page:{key}", text, "Page", ic))
        entries.append(("page:settings", "Settings", "Page", "settings"))
        entries.append(("action:new", "New Bot…", "Action", "plus"))
        entries.append(("action:group", "New group chat…", "Action", "users"))
        entries.append(("action:import", "Import a Bot package…", "Action", "download"))
        d = QuickSwitcher(entries, self)
        d.chosen.connect(self._quick_chosen)
        g = self.geometry()
        d.move(g.x() + (g.width() - d.width()) // 2, g.y() + 90)
        d.exec()

    def _quick_chosen(self, key: str) -> None:
        if key == "action:new":
            self.new_bot()
        elif key == "action:group":
            self.new_group()
        elif key == "action:import":
            self.import_bot()
        else:
            self.select(key)

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
