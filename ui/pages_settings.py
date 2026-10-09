"""Settings: providers and models, approvals, network policy, notifications, the computer (local or remote), mobile access, app."""
from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import QSize, Qt, QTime, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QTimeEdit, QHBoxLayout as _H, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QProgressBar, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from core import paths
from . import theme
from .api import Api, Connection, load_ui_config, save_ui_config
from .model_picker import ModelPicker
from .store import Store
from .settings_extra import ApiAccessPanel, BackupModelPanel, ChannelsPanel, DiagnosticsPanel, StatusLine, form
from .widgets import PageHeader, Section, SideTabs, button, card, chip, label, page_layout, repolish


def lines(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines() if l.strip()]


def _mb(n: float) -> str:
    return f"{n / 1024 / 1024:.1f}"


def qr_pixmap(text: str, size: int = 220) -> QPixmap | None:
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    m = qr.get_matrix()
    n = len(m)
    pm = QPixmap(size, size)
    pm.fill(QColor("#ffffff"))
    p = QPainter(pm)
    cell = size / n
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#000000"))
    for y, row in enumerate(m):
        for x, v in enumerate(row):
            if v:
                p.drawRect(int(x * cell), int(y * cell), int(cell) + 1, int(cell) + 1)
    p.end()
    return pm


class AccentPicker(QWidget):
    """A row of colour dots; the picked one gets a ring."""

    def __init__(self, current: str):
        super().__init__()
        self.value = current if current in theme.ACCENTS else "indigo"
        h = _H(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        self.dots: dict[str, QFrame] = {}
        light = theme.theme_name() == "light"
        for key, (name, dark_c, light_c) in theme.ACCENTS.items():
            d = QFrame()
            d.setFixedSize(26, 26)
            d.setProperty("swatch", True)
            d.setStyleSheet(f"QFrame[swatch=\"true\"] {{ background: {light_c if light else dark_c}; }}")
            d.setToolTip(name)
            d.setCursor(Qt.CursorShape.PointingHandCursor)
            d.mouseReleaseEvent = lambda e, k=key: self.pick(k)  # type: ignore[assignment]
            self.dots[key] = d
            h.addWidget(d)
        h.addStretch(1)
        self.pick(self.value)

    def pick(self, key: str) -> None:
        self.value = key
        for k, d in self.dots.items():
            d.setProperty("on", k == key)
            repolish(d)


def form_tab() -> tuple[QScrollArea, QVBoxLayout]:
    """A scrolling settings page: one readable column (max 700px) of section cards, left aligned, natural-width buttons."""
    w = QWidget()
    outer = QHBoxLayout(w)
    outer.setContentsMargins(0, 0, 16, 16)
    col = QWidget()
    col.setMaximumWidth(700)
    v = QVBoxLayout(col)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(16)
    outer.addWidget(col, 1)
    outer.addStretch(0)
    sc = QScrollArea()
    sc.setWidgetResizable(True)
    sc.setWidget(w)
    sc.setFrameShape(QScrollArea.Shape.NoFrame)
    return sc, v


def with_hint(check, text: str) -> QWidget:
    """A checkbox with a quiet, wrapping explanation under it. A checkbox's own text cannot wrap, so a long one gets cut off."""
    box = QWidget()
    l = QVBoxLayout(box)
    l.setContentsMargins(0, 0, 0, 0)
    l.setSpacing(2)
    l.addWidget(check)
    h = label(text, faint=True)
    h.setContentsMargins(26, 0, 0, 0)
    l.addWidget(h)
    return box


def buttons(*items: QWidget, end: list[QWidget] | None = None) -> QHBoxLayout:
    """A row of buttons from the left; `end` buttons sit at the right, away from the primary action (used for destructive ones)."""
    row = QHBoxLayout()
    row.setSpacing(8)
    for w in items:
        row.addWidget(w)
    row.addStretch(1)
    for w in end or []:
        row.addWidget(w)
    return row


class SettingsPage(QWidget):
    themeChanged = Signal(str)
    appPrefsChanged = Signal()
    switchConnection = Signal(object)
    toast = Signal(str, str)
    restartService = Signal()
    quitForUpdate = Signal()   # an update is installing: the main window quits so the installer can replace the app

    # nav rows that open a new group of related pages (Approvals, Notifications, Computer, API access)
    GROUP_STARTS = {1, 3, 4, 6}

    def __init__(self, api: Api, store: Store):
        super().__init__()
        self.api, self.store = api, store
        v = page_layout(self, PageHeader("Settings", ""))
        self.tabs = SideTabs(210)
        v.addWidget(self.tabs, 1)
        self.admin_banner = label("")
        self.admin_banner.hide()
        v.insertWidget(1, self.admin_banner)
        self._build_providers()
        self._build_safety()
        self._build_network()
        self._build_notifications()
        self._build_computer()
        self._build_mobile()
        self._build_api_access()
        self._build_diagnostics()
        self._build_app()
        self._apply_local_styles()
        store.settingsChanged.connect(self.load)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self._apply_local_styles()
        self.store.refresh_settings()
        self.store.refresh_profiles()
        self.load()
        self.load_mobile()
        self.refresh_update_card()

    def _apply_local_styles(self) -> None:
        """Colours and nav styling that depend on the theme. Re-applied on show and after a theme change."""
        p = theme.palette()
        self.admin_banner.setStyleSheet(f"color: {p['warn']};")
        nav = self.tabs.nav
        nav.setStyleSheet(
            "QListWidget#sidetabs { background: transparent; border: none; outline: none; }"
            f"QListWidget#sidetabs::item {{ padding: 8px 12px 8px 13px; margin: 1px 0; border-left: 3px solid transparent; border-radius: 6px; color: {p['muted']}; }}"
            f"QListWidget#sidetabs::item:hover {{ color: {p['text']}; background: {p['hover']}; }}"
            f"QListWidget#sidetabs::item:selected {{ color: {p['text']}; font-weight: 600; background: {p['select']}; border-left: 3px solid {p['accent']}; }}")
        base, gap = theme.dp(38), theme.dp(14)
        for i in range(nav.count()):
            it = nav.item(i)
            start = i in self.GROUP_STARTS
            it.setSizeHint(QSize(nav.width(), base + (gap if start else 0)))
            it.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for t in (self.q_start, self.q_end, self.d_time):
            t.setStyleSheet(f"QTimeEdit {{ background: {p['panel']}; border: 1px solid {p['line2']}; border-radius: 8px; padding: 6px 10px; }}")

    # =========================================================== providers
    def _build_providers(self) -> None:
        sc, v = form_tab()
        lst = Section("Providers", "Anthropic or any OpenAI-compatible endpoint. ● is ready, ○ still needs a key. New Bots use the default.")
        self.prov_list = QListWidget()
        self.prov_list.setMaximumHeight(theme.dp(150))
        self.prov_list.currentRowChanged.connect(self._prov_pick)
        lst.add(self.prov_list)
        lst.add(layout=buttons(button("Add endpoint…", icon="plus", on=self.add_provider, tip="Add any OpenAI-compatible endpoint")))
        v.addWidget(lst)

        sec = Section("Selected provider", "Each Bot can use its own provider and model. API keys go to the Windows Credential Manager.")
        f = form()
        self.p_label = QLineEdit()
        self.p_kind = QComboBox()
        self.p_kind.addItem("Anthropic API", "anthropic")
        self.p_kind.addItem("OpenAI-compatible", "openai")
        self.p_url = QLineEdit()
        self.p_url.setPlaceholderText("https://api.openai.com/v1")
        self.p_picker = ModelPicker(self.api, "Choose a detected model or type one")
        self.p_vision = QCheckBox("This model can see images (screenshots)")
        self.p_needs_key = QCheckBox("Requires an API key")
        self.p_key = QLineEdit()
        self.p_key.setEchoMode(QLineEdit.EchoMode.Password)
        f.addRow("Name", self.p_label)
        f.addRow("Type", self.p_kind)
        f.addRow("Base URL", self.p_url)
        f.addRow("Default model", self.p_picker)
        f.addRow("", self.p_vision)
        f.addRow("", self.p_needs_key)
        f.addRow("API key", self.p_key)
        sec.add(layout=f)
        self.p_delete = button("Delete", danger=True, on=self.p_del, tip="Delete this endpoint and its saved key")
        sec.add(layout=buttons(
            button("Save", primary=True, on=self.p_save),
            button("Test", on=self.p_test, tip="Check the key and detect the available models"),
            button("Make default", on=lambda: self.p_save(make_default=True)),
            end=[button("Remove key", danger=True, on=self.p_remove_key), self.p_delete]))
        self.p_status = StatusLine()
        sec.add(self.p_status)
        v.addWidget(sec)
        self.backup_panel = BackupModelPanel(self.api, self.store)
        v.addWidget(self.backup_panel)
        v.addStretch(1)
        self.tabs.addTab(sc, "Models", "sparkle")
        self.cur_prov = ""

    def _prov_refresh_list(self) -> None:
        sel = self.cur_prov
        self.prov_list.blockSignals(True)
        self.prov_list.clear()
        default = self.store.settings.get("default_profile", "anthropic")
        for p in self.store.profiles:
            ok = p.get("key_set") or not p.get("needs_key", True)
            it = QListWidgetItem(("● " if ok else "○ ") + p["label"] + ("   (default)" if p["id"] == default else ""))
            it.setData(Qt.ItemDataRole.UserRole, p["id"])
            self.prov_list.addItem(it)
        self.prov_list.blockSignals(False)
        # as tall as its rows (at most about five), not a big empty box
        self.prov_list.setFixedHeight(max(theme.dp(64), min(theme.dp(190), theme.dp(16) + self.prov_list.count() * theme.dp(38))))
        for i in range(self.prov_list.count()):
            if self.prov_list.item(i).data(Qt.ItemDataRole.UserRole) == sel:
                self.prov_list.setCurrentRow(i)
                return
        if self.prov_list.count():
            self.prov_list.setCurrentRow(0)

    def _prov_pick(self, row: int) -> None:
        if row < 0:
            return
        pid = self.prov_list.item(row).data(Qt.ItemDataRole.UserRole)
        p = self.store.provider(pid)
        if not p:
            return
        self.cur_prov = pid
        self.p_label.setText(p["label"])
        self.p_kind.setCurrentIndex(max(0, self.p_kind.findData(p["kind"])))
        self.p_url.setText(p.get("base_url", ""))
        self.p_picker.set_provider(pid, bool(p.get("key_set") or not p.get("needs_key", True)), p.get("model", ""))
        self.p_vision.setChecked(bool(p.get("vision", True)))
        self.p_needs_key.setChecked(bool(p.get("needs_key", True)))
        self.p_key.clear()
        self.p_key.setPlaceholderText("•••••••• saved (type to replace)" if p.get("key_set") else ("No key needed" if not p.get("needs_key", True) else "Paste your API key"))
        self.p_delete.setEnabled(not p.get("preset"))
        self.p_status.setText("")

    def p_save(self, make_default: bool = False, then=None) -> None:
        body = {"label": self.p_label.text().strip(), "kind": self.p_kind.currentData(), "base_url": self.p_url.text().strip(), "model": self.p_picker.text(),
                "vision": self.p_vision.isChecked(), "needs_key": self.p_needs_key.isChecked(), "make_default": make_default}
        new_key = self.p_key.text().strip()
        if new_key:
            body["api_key"] = new_key
        changed_endpoint = new_key or body["base_url"] != (self.store.provider(self.cur_prov) or {}).get("base_url", "")

        def ok(_p: dict) -> None:
            self.p_key.clear()
            self.p_status.ok("Saved." + (" Set as default." if make_default else ""))
            if changed_endpoint:
                self.p_picker.invalidate()   # new key or URL: the model list may differ
            if then:
                then()
            self.store.refresh_all()
        self.api.put(f"/api/providers/{self.cur_prov}", body, ok, lambda e: self.p_status.err("Error: " + e))

    def p_test(self) -> None:
        """Save any typed key first, then detect the models: success proves the key works."""
        self.p_status.setText("Testing connection…")

        def run() -> None:
            self.p_picker.set_provider(self.cur_prov, True, self.p_picker.text())
            self.p_picker.detect()
            self.p_status.setText("")
        if self.p_key.text().strip() or self.p_url.text().strip():
            self.p_save(then=run)
        else:
            run()

    def p_remove_key(self) -> None:
        self.api.put(f"/api/secrets/provider:{self.cur_prov}", {"value": ""}, lambda _: (self.p_status.ok("Key removed."), self.store.refresh_all()),
                     lambda e: self.p_status.err(e))

    def p_del(self) -> None:
        if QMessageBox.question(self, "Delete", "Delete this provider and its saved key?") == QMessageBox.StandardButton.Yes:
            self.api.delete(f"/api/providers/{self.cur_prov}", lambda _: self.store.refresh_all())

    def add_provider(self) -> None:
        name, ok = QInputDialog.getText(self, "Add endpoint", "Name (e.g. Together, vLLM, my-proxy):")
        if not ok or not name.strip():
            return
        pid = "".join(c if c.isalnum() else "-" for c in name.strip().lower()).strip("-") or "custom"
        body = {"label": name.strip(), "kind": "openai", "base_url": "http://localhost:8000/v1", "model": "", "vision": False, "needs_key": False}
        self.cur_prov = pid
        self.api.put(f"/api/providers/{pid}", body, lambda _: self.store.refresh_all(), lambda e: QMessageBox.warning(self, "Error", e))

    # ======================================================= safety / approvals
    def _build_safety(self) -> None:
        sc, v = form_tab()
        a = Section("Approvals", "Who decides on consequential actions, and how Bots may run commands.")
        f = form()
        self.s_mode = QComboBox()
        self.s_mode.addItem("Ask me for consequential actions", "ask")
        self.s_mode.addItem("Auto Review: a reviewer model decides", "auto_review")
        f.addRow("Default for new Bots", self.s_mode)
        self.s_rprov = QComboBox()
        self.s_rmodel = QLineEdit()
        self.s_rmodel.setPlaceholderText("Same as the Bot's model")
        f.addRow("Reviewer provider", self.s_rprov)
        f.addRow("Reviewer model", self.s_rmodel)
        self.s_cmd = QComboBox()
        self.s_cmd.addItem("Workspace commands run freely; others ask", "workspace")
        self.s_cmd.addItem("Ask for every command", "ask_all")
        f.addRow("Terminal", self.s_cmd)
        a.add(layout=f)
        a.add(label("Always requires approval, even in Auto Review: purchases, logging in to new services, granting access, and anything outside the workspace. "
                    "Content from web pages, emails and files is treated as data, never as instructions; if it contains instruction-like text, automatic approvals are switched off for that task.", faint=True))
        v.addWidget(a)

        lim = Section("Timeouts and limits", "How long a Bot waits for you, and how much work it may do in one task.")
        f2 = form()
        self.s_timeout = QSpinBox()
        self.s_timeout.setRange(1, 10080)
        self.s_timeout.setSuffix(" min")
        self.s_rtimeout = QSpinBox()
        self.s_rtimeout.setRange(1, 10080)
        self.s_rtimeout.setSuffix(" min")
        self.s_steps = QSpinBox()
        self.s_steps.setRange(1, 500)
        f2.addRow("Wait for an answer", self.s_timeout)
        f2.addRow("Wait during routines", self.s_rtimeout)
        f2.addRow("Default step limit", self.s_steps)
        self.s_reflect = QCheckBox("Let Bots curate their own memory after a task")
        f2.addRow("", with_hint(self.s_reflect, "Preferences, voice and work summaries."))
        lim.add(layout=f2)
        v.addWidget(lim)
        self.s_msg = StatusLine()
        v.addWidget(button("Save", primary=True, on=self.save_safety))
        v.addWidget(self.s_msg)
        v.addStretch(1)
        self.tabs.addTab(sc, "Approvals & safety", "shield")

    def save_safety(self) -> None:
        body = {"approval.default_mode": self.s_mode.currentData(), "reviewer.profile": self.s_rprov.currentData() or "", "reviewer.model": self.s_rmodel.text().strip(),
                "approval.timeout_min": self.s_timeout.value(), "approval.routine_timeout_min": self.s_rtimeout.value(), "computer.command_policy": self.s_cmd.currentData(),
                "memory.auto_reflect": self.s_reflect.isChecked(), "defaults.step_limit": self.s_steps.value()}
        self.api.put("/api/settings", body, lambda s: (setattr(self.store, "settings", s), self.s_msg.ok("Saved.")), lambda e: self.s_msg.err(e))

    # ========================================================= network policy
    def _build_network(self) -> None:
        sc, v = form_tab()
        sec = Section("Network policy", "Applies to every Bot's browser and web requests. Each Bot can add its own lists. Deny always wins.")
        self.n_mode = QComboBox()
        self.n_mode.addItem("Open: block only the deny list", "open")
        self.n_mode.addItem("Allow list only: block everything else", "allowlist")
        sec.add(self.n_mode)
        row = QHBoxLayout()
        row.setSpacing(16)
        col1, col2 = QVBoxLayout(), QVBoxLayout()
        col1.addWidget(label("Allowed domains (one per line, *.example.com works)", faint=True))
        self.n_allow = QPlainTextEdit()
        col1.addWidget(self.n_allow)
        col2.addWidget(label("Blocked domains (one per line)", faint=True))
        self.n_deny = QPlainTextEdit()
        col2.addWidget(self.n_deny)
        row.addLayout(col1, 1)
        row.addLayout(col2, 1)
        sec.add(layout=row)
        self.n_private = QCheckBox("Block local and private network addresses")
        sec.add(with_hint(self.n_private, "Such as localhost and 192.168.x.x."))
        v.addWidget(sec)
        self.n_msg = StatusLine()
        v.addWidget(button("Save", primary=True, on=self.save_network))
        v.addWidget(self.n_msg)
        v.addStretch(1)
        self.tabs.addTab(sc, "Network", "globe")

    def save_network(self) -> None:
        body = {"network.mode": self.n_mode.currentData(), "network.allow": lines(self.n_allow.toPlainText()), "network.deny": lines(self.n_deny.toPlainText()),
                "network.block_private": self.n_private.isChecked()}
        self.api.put("/api/settings", body, lambda s: (setattr(self.store, "settings", s), self.n_msg.ok("Saved.")), lambda e: self.n_msg.err(e))

    # ========================================================== notifications
    def _build_notifications(self) -> None:
        sc, v = form_tab()
        alerts = Section("Alerts on this PC", "Pop-ups, sounds and tray alerts when a Bot needs you or finishes.")
        self.no_toast = QCheckBox("Windows toast notifications and tray alerts")
        self.no_sound = QCheckBox("Play a sound when a Bot needs you")
        self.no_finish = QCheckBox("Notify when a Bot finishes a task")
        self.no_appr = QCheckBox("Notify when a Bot needs you (approval, question or browser)")
        for c in (self.no_toast, self.no_sound, self.no_finish, self.no_appr):
            alerts.add(c)
        f = form()
        self.no_min = QSpinBox()
        self.no_min.setRange(0, 3600)
        self.no_min.setSuffix(" s")
        f.addRow("Only for tasks longer than", self.no_min)
        alerts.add(layout=f)
        v.addWidget(alerts)

        quiet = Section("Quiet hours and digest", "Stay silent on a schedule, and get one summary a day.")
        self.q_on = QCheckBox("Quiet hours")
        quiet.add(with_hint(self.q_on, "No pop-ups, sounds or phone pushes on a schedule. The Inbox still collects everything."))
        self.q_start, self.q_end = QTimeEdit(), QTimeEdit()
        for t in (self.q_start, self.q_end):
            t.setDisplayFormat("HH:mm")
        fq = form()
        fq.addRow("Quiet from", self.q_start)
        fq.addRow("Quiet until", self.q_end)
        quiet.add(layout=fq)
        self.d_on = QCheckBox("Daily digest")
        quiet.add(with_hint(self.d_on, "One notification a day with what your Bots did."))
        self.d_time = QTimeEdit()
        self.d_time.setDisplayFormat("HH:mm")
        fd = form()
        fd.addRow("Digest at", self.d_time)
        quiet.add(layout=fd)
        v.addWidget(quiet)

        push = Section("Phone push", "For alerts while your phone is locked. Use the free ntfy app and your own server for privacy.")
        self.no_url = QLineEdit()
        self.no_url.setPlaceholderText("https://ntfy.sh")
        self.no_topic = QLineEdit()
        self.no_topic.setPlaceholderText("a-long-random-topic-name")
        fp = form()
        fp.addRow("ntfy server", self.no_url)
        fp.addRow("Topic", self.no_topic)
        push.add(layout=fp)
        push.add(label("Subscribe to the same topic in the ntfy app. The mobile web app also shows alerts while it is open.", faint=True))
        v.addWidget(push)

        self.channels_panel = ChannelsPanel(self.api, self.store)
        v.addWidget(self.channels_panel)

        dnd = Section("Do not disturb", "Silence all alerts for a while. The Inbox still collects everything.")
        dnd.add(layout=buttons(
            button("1 hour", on=lambda: self.set_dnd(3600)),
            button("4 hours", on=lambda: self.set_dnd(4 * 3600)),
            button("Until tomorrow", on=lambda: self.set_dnd(0)),
            end=[button("End it", flat=True, on=lambda: self.set_dnd(-1))]))
        v.addWidget(dnd)

        self.no_msg = StatusLine()
        v.addLayout(buttons(button("Save", primary=True, on=self.save_notifications), button("Send test", on=lambda: self.api.post("/api/notifications/test", {}))))
        v.addWidget(self.no_msg)
        v.addStretch(1)
        self.tabs.addTab(sc, "Notifications", "alert")

    def save_notifications(self) -> None:
        body = {"notifications.toast": self.no_toast.isChecked(), "notifications.sound": self.no_sound.isChecked(), "notifications.on_finish": self.no_finish.isChecked(),
                "notifications.on_approval": self.no_appr.isChecked(), "notifications.min_turn_seconds": self.no_min.value(), "notifications.ntfy_url": self.no_url.text().strip(),
                "notifications.ntfy_topic": self.no_topic.text().strip(), "notifications.quiet_enabled": self.q_on.isChecked(),
                "notifications.quiet_start": self.q_start.time().toString("HH:mm"), "notifications.quiet_end": self.q_end.time().toString("HH:mm"),
                "digest.enabled": self.d_on.isChecked(), "digest.time": self.d_time.time().toString("HH:mm")}
        self.api.put("/api/settings", body, lambda s: (setattr(self.store, "settings", s), self.no_msg.ok("Saved.")), lambda e: self.no_msg.err(e))

    def set_dnd(self, secs: int) -> None:
        """secs > 0: that long from now; 0: until 07:00 tomorrow-ish (next local midnight + 7h); -1: end Do Not Disturb."""
        if secs == 0:
            from datetime import datetime, timedelta
            tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
            until = tomorrow.timestamp()
        else:
            until = 0 if secs < 0 else time.time() + secs
        self.api.put("/api/settings", {"notifications.dnd_until": until},
                     lambda s: (setattr(self.store, "settings", s), self.store.settingsChanged.emit(),
                                self.no_msg.ok("Do Not Disturb is off." if not until else "Do Not Disturb is on.")),
                     lambda e: self.no_msg.err(e))

    # ============================================================== computer
    def _build_computer(self) -> None:
        sc, v = form_tab()
        top = Section("The computer", "All your Bots share one persistent computer: a browser, a workspace folder and a terminal. By default it runs on this PC.")
        self.c_headless = QCheckBox("Run the browser without a visible window")
        top.add(with_hint(self.c_headless, "Use Take over to watch or help."))
        self.c_msg = StatusLine()
        top.add(layout=buttons(button("Save", primary=True, on=self.save_computer)))
        top.add(self.c_msg)
        v.addWidget(top)

        sec = Section("Where the computer runs", "Remote mode runs the same computer, Bots, routines and background turns on another machine, so work continues while this laptop is closed.")
        self.c_mode_local = QComboBox()
        self.c_mode_local.addItem("This PC (local service, starts automatically)", "local")
        self.c_mode_local.addItem("Remote: a Windows VM or Docker host I run", "remote")
        self.c_url = QLineEdit()
        self.c_url.setPlaceholderText("http://my-vm:8765  (use https or a VPN/SSH tunnel across the internet)")
        self.c_token = QLineEdit()
        self.c_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.c_token.setPlaceholderText("Access token printed by the remote service")
        f = form()
        f.addRow("Runs on", self.c_mode_local)
        f.addRow("Remote URL", self.c_url)
        f.addRow("Access token", self.c_token)
        sec.add(layout=f)
        sec.add(label("See deploy/ and the README for a one-command remote setup.", faint=True))
        sec.add(layout=buttons(
            button("Test and switch", primary=True, on=self.switch_computer),
            end=[button("Restart local service", on=self.restartService.emit)]))
        self.c_status = StatusLine()
        sec.add(self.c_status)
        v.addWidget(sec)
        v.addStretch(1)
        cfg = load_ui_config()
        self.c_mode_local.setCurrentIndex(1 if cfg.get("mode") == "remote" else 0)
        self.c_url.setText(cfg.get("url", ""))
        self.tabs.addTab(sc, "Computer", "computer")

    def save_computer(self) -> None:
        self.api.put("/api/settings", {"computer.headless": self.c_headless.isChecked()},
                     lambda s: (setattr(self.store, "settings", s), self.c_msg.ok("Saved. The browser restarts on next use.")),
                     lambda e: self.c_msg.err(e))

    def switch_computer(self) -> None:
        from core import secrets as sec
        mode = self.c_mode_local.currentData()
        adm = (self.store.status.get("admin") or {}).get("policy", {})
        if mode == "remote" and adm.get("allow_remote_mode") is False:
            self.c_status.err("Remote mode is disabled by your administrator.")
            return
        cfg = {"mode": mode, "url": self.c_url.text().strip()}
        if mode == "remote":
            tok = self.c_token.text().strip() or sec.get_secret("ui:remote_token") or ""
            if not cfg["url"] or not tok:
                self.c_status.err("Enter the remote URL and access token.")
                return
            try:
                import httpx
                r = httpx.get(cfg["url"].rstrip("/") + "/api/status", headers={"Authorization": f"Bearer {tok}"}, timeout=10)
                if r.status_code == 401:
                    raise RuntimeError("The token was rejected.")
                r.raise_for_status()
            except Exception as e:  # noqa: BLE001
                self.c_status.err(f"Could not connect: {e}")
                return
            try:
                sec.set_secret("ui:remote_token", tok)
            except Exception as e:  # noqa: BLE001
                self.c_status.err(str(e))
                return
            self.c_token.clear()
            self.switchConnection.emit(Connection("remote", cfg["url"], tok))
        else:
            self.switchConnection.emit(Connection("local"))
        save_ui_config({**load_ui_config(), **cfg})
        self.c_status.ok("Switched. Reconnecting…")

    # ============================================================ api access
    def _build_api_access(self) -> None:
        sc, v = form_tab()
        v.addWidget(ApiAccessPanel(self.api, self.store))
        v.addStretch(1)
        self.tabs.addTab(sc, "API access", "key")

    # ============================================================ diagnostics
    def _build_diagnostics(self) -> None:
        sc, v = form_tab()
        v.addWidget(DiagnosticsPanel(self.api, self.store))
        v.addStretch(1)
        self.tabs.addTab(sc, "Diagnostics", "heart")

    # ================================================================ mobile
    def _build_mobile(self) -> None:
        sc, v = form_tab()
        sec = Section("Mobile web app", "Message your Bots from your phone: the same threads, approvals and a take-over view.")
        self.m_lan = QCheckBox("Allow phones on my network (listen on all addresses)")
        sec.add(self.m_lan)
        self.m_port = QSpinBox()
        self.m_port.setRange(1024, 65535)
        f = form()
        f.addRow("Port", self.m_port)
        sec.add(layout=f)
        sec.add(layout=buttons(button("Apply (restarts the service)", primary=True, on=self.apply_mobile)))
        self.m_qr = QLabel()
        self.m_qr.setFixedSize(220, 220)
        sec.add(self.m_qr)
        self.m_urls = QPlainTextEdit()
        self.m_urls.setReadOnly(True)
        self.m_urls.setMaximumHeight(theme.dp(110))
        sec.add(self.m_urls)
        sec.add(layout=buttons(button("Copy link", on=self.copy_link), button("Show access token", on=self.show_token)))
        sec.add(label("Open the link on your phone (same Wi-Fi), then “Add to Home screen”. The link contains your access token: treat it like a password. "
                      "Across the internet use a VPN such as Tailscale, an SSH tunnel, or HTTPS. Reset the token with:  python main.py --reset-token", faint=True))
        self.m_msg = StatusLine()
        sec.add(self.m_msg)
        v.addWidget(sec)
        v.addStretch(1)
        self.mobile_info: dict = {}
        self.tabs.addTab(sc, "Mobile", "users")

    def load_mobile(self) -> None:
        def ok(d: dict) -> None:
            self.mobile_info = d
            self.m_lan.setChecked(d["lan"])
            self.m_port.setValue(d["port"])
            urls = d["urls"] if d["lan"] and d["urls"] else [d["local_url"]]
            self.m_urls.setPlainText("\n".join(urls))
            pm = qr_pixmap(urls[0])
            if pm:
                self.m_qr.setPixmap(pm)
            else:
                self.m_qr.setText("Install the 'qrcode' package to show a QR code")
        self.api.get("/api/mobile", ok)

    def copy_link(self) -> None:
        t = self.m_urls.toPlainText().splitlines()
        if t:
            QGuiApplication.clipboard().setText(t[0])
            self.m_msg.ok("Link copied.")

    def show_token(self) -> None:
        tok = self.mobile_info.get("token", "")
        QMessageBox.information(self, "Access token", f"{tok}\n\nEnter this on the phone's sign-in screen if you do not use the link.")

    def apply_mobile(self) -> None:
        body = {"mobile.host": "0.0.0.0" if self.m_lan.isChecked() else "127.0.0.1", "mobile.port": self.m_port.value()}
        want_lan = self.m_lan.isChecked()

        def saved(_s: object) -> None:
            self.m_msg.setText("Restarting the service…")
            self.restartService.emit()
            QTimer.singleShot(1500, lambda: self._mobile_wait(want_lan, 0))
        self.api.put("/api/settings", body, saved, lambda e: self.m_msg.err(e))

    def _mobile_wait(self, want_lan: bool, tries: int) -> None:
        """Poll until the restarted service answers with the new settings, then refresh the link and QR code."""
        def ok(d: dict) -> None:
            if bool(d.get("lan")) != want_lan:   # still the old process
                retry()
                return
            self.load_mobile()
            if want_lan:
                self.m_msg.ok("Ready. On your phone (same Wi-Fi) open the link above. If it does not load: check the phone is not on mobile data or a guest network, and allow OpenGrokBot through Windows Firewall for Private and Public networks.")
            else:
                self.m_msg.ok("Phone access is off. The service now listens on this PC only.")

        def retry(_e: str = "") -> None:
            if tries < 25:
                QTimer.singleShot(1500, lambda: self._mobile_wait(want_lan, tries + 1))
            else:
                self.m_msg.err("The service did not come back. Use Settings > Computer > Restart local service.")
        self.api.get("/api/mobile", ok, retry)

    # ================================================================== app
    def _build_app(self) -> None:
        sc, v = form_tab()
        look = Section("Appearance", "How OpenGrokBot looks on this PC.")
        f = form()
        self.a_theme = QComboBox()
        self.a_theme.addItem("Dark (calm, default)", "dark")
        self.a_theme.addItem("Light", "light")
        self.a_theme.addItem("Match Windows (changes with your system)", "auto")
        f.addRow("Theme", self.a_theme)
        cfg0 = load_ui_config()
        self.a_accent = AccentPicker(cfg0.get("accent", "indigo"))
        f.addRow("Accent colour", self.a_accent)
        self.a_density = QComboBox()
        for k, (name, _) in theme.DENSITIES.items():
            self.a_density.addItem(name, k)
        self.a_density.setCurrentIndex(max(0, self.a_density.findData(cfg0.get("density", "comfortable"))))
        f.addRow("Density", self.a_density)
        self.a_text = QComboBox()
        for k, (name, px) in theme.TEXT_SIZES.items():
            self.a_text.addItem(f"{name} ({px}px)", k)
        self.a_text.setCurrentIndex(max(0, self.a_text.findData(cfg0.get("text", "default"))))
        f.addRow("Text size", self.a_text)
        look.add(layout=f)
        v.addWidget(look)

        startup = Section("Startup and tray", "What happens when you close the window, and when you sign in to Windows.")
        self.a_tray = QCheckBox("Closing the window keeps the app in the system tray")
        startup.add(with_hint(self.a_tray, "Bots always keep running in the background service."))
        self.a_start = QCheckBox("Start with Windows (minimised to the tray)")
        startup.add(self.a_start)
        self.a_hotkey = QCheckBox("Global shortcut: Ctrl+Alt+Space opens Quick Ask from anywhere")
        self.a_hotkey.setChecked(bool(cfg0.get("quick_hotkey", True)))
        startup.add(with_hint(self.a_hotkey, "Ctrl+J inside the app always works."))
        self.a_updates = QCheckBox("Tell me when a new version is out")
        startup.add(with_hint(self.a_updates, "One request to GitHub a day. Nothing about you is sent."))
        v.addWidget(startup)

        self.a_info = StatusLine()
        v.addLayout(buttons(button("Save", primary=True, on=self.save_app)))
        v.addWidget(self.a_info)

        self._build_update_card(v)

        data = Section("Data and logs", "Your Bots, chats and settings live in the data folder.")
        data.add(layout=buttons(
            button("Open data folder", on=lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.data_dir())))),
            button("Open logs", on=lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.logs_dir()))))))
        v.addWidget(data)

        backup = Section("Backup and restore", "A backup holds your Bots, chats, memory, routines, settings and skills in one zip. API keys and tokens stay in the Windows Credential Manager and are not included.")
        self.a_ws = QCheckBox("Also include the shared workspace files")
        backup.add(self.a_ws)
        backup.add(layout=buttons(button("Back up…", icon="download", on=self.backup_now), button("Restore…", icon="upload", on=self.restore_backup)))
        self.a_backup_msg = StatusLine()
        backup.add(self.a_backup_msg)
        v.addWidget(backup)
        self.a_admin = label("", faint=True)
        v.addWidget(self.a_admin)
        v.addStretch(1)
        cfg = load_ui_config()
        self.a_tray.setChecked(cfg.get("close_to_tray", True))
        self.a_start.setChecked(self._autostart_enabled())
        self.tabs.addTab(sc, "App", "settings")

    @staticmethod
    def _autostart_enabled() -> bool:
        if os.name != "nt":
            return False
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, "OpenGrokBot")
                return True
        except OSError:
            return False

    @staticmethod
    def _set_autostart(on: bool) -> None:
        if os.name != "nt":
            return
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as k:
            if on:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}" --tray'
                else:
                    pyw = sys.executable.replace("python.exe", "pythonw.exe")
                    cmd = f'"{pyw}" "{os.path.abspath(sys.argv[0])}" --tray'
                winreg.SetValueEx(k, "OpenGrokBot", 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, "OpenGrokBot")
                except OSError:
                    pass

    def save_app(self) -> None:
        cfg = load_ui_config()
        cfg["close_to_tray"] = self.a_tray.isChecked()
        cfg.update(accent=self.a_accent.value, density=self.a_density.currentData(), text=self.a_text.currentData())
        save_ui_config(cfg)
        try:
            self._set_autostart(self.a_start.isChecked())
        except OSError as e:
            self.a_info.err(f"Could not change autostart: {e}")
        cfg = load_ui_config()
        cfg["quick_hotkey"] = self.a_hotkey.isChecked()
        save_ui_config(cfg)
        t = self.a_theme.currentData()

        def saved(s: dict) -> None:
            setattr(self.store, "settings", s)
            self.appPrefsChanged.emit()
            self.themeChanged.emit(t)
            self._apply_local_styles()
            self.a_info.ok("Saved.")
        self.api.put("/api/settings", {"theme": t, "updates.check": self.a_updates.isChecked()}, saved, lambda e: self.a_info.err(e))

    def backup_now(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Back up OpenGrokBot", "opengrokbot-backup-" + time.strftime("%Y%m%d-%H%M") + ".zip", "Backup (*.zip)")
        if not path:
            return
        self.a_backup_msg.setText("Backing up…")

        def ok(data: bytes) -> None:
            with open(path, "wb") as f:
                f.write(data)
            self.a_backup_msg.ok(f"Saved {path} ({len(data) / 1024 / 1024:.1f} MB).")
        self.api.request("GET", "/api/backup", ok, lambda e: self.a_backup_msg.err(e), params={"workspace": str(self.a_ws.isChecked()).lower()}, raw=True)

    def restore_backup(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Restore from a backup", "", "Backup (*.zip)")
        if not path:
            return
        if QMessageBox.question(self, "Restore backup", "Replace your Bots, chats, memory and settings with the ones in this backup?\n\nYour current data is kept as a before-restore file in the data folder, and the background service restarts.") != QMessageBox.StandardButton.Yes:
            return
        with open(path, "rb") as f:
            data = f.read()

        def ok(r: dict) -> None:
            self.a_backup_msg.ok(f"Backup accepted: {r['bots']} Bots, {r['threads']} chats. Restarting the service to apply it…")
            QTimer.singleShot(800, self.restartService.emit)
        self.api.request("POST", "/api/backup/restore", ok, lambda e: self.a_backup_msg.err(e), content=data, timeout=300.0)

    # ============================================================ updates card
    def _build_update_card(self, v: QVBoxLayout) -> None:
        box = self.u_card = Section("Updates", "The installed version, and the newest release on GitHub.")
        self.u_title = label("", muted=True)
        self.u_status = label("Press Check for updates to look for a new version.")
        box.add(self.u_title)
        box.add(self.u_status)
        self.u_bar = QProgressBar()
        self.u_bar.setTextVisible(False)
        self.u_bar.setRange(0, 1)
        self.u_prog = label("", muted=True)
        self.u_error = label("")
        self.u_error.setStyleSheet(f"color: {theme.palette()['bad']};")
        for w in (self.u_bar, self.u_prog, self.u_error):
            box.add(w)
            w.hide()
        self.u_check = button("Check for updates", on=self.check_updates)
        self.u_update = button("Update now", primary=True, on=self.start_update, tip="Download the new version and install it. OpenGrokBot restarts itself.")
        self.u_release = button("Open release page", on=self.open_release)
        box.add(layout=buttons(self.u_check, self.u_update, self.u_release))
        v.addWidget(box)
        self.u_update.hide()
        self.u_release.hide()
        self._upd_state: dict = {}
        self._upd_poll_busy = False
        self._want_install = False     # set by "Update now": install as soon as the download is ready
        self._installing = False
        self._action_error = ""        # a failed download or install, shown until the next try
        self._upd_poll = QTimer(self)
        self._upd_poll.setInterval(700)
        self._upd_poll.timeout.connect(self._poll_update)

    def _status(self, text: str, warn: bool = False) -> None:
        self.u_status.setText(text)
        self.u_status.setStyleSheet(f"color: {theme.palette()['warn']};" if warn else "")

    def check_updates(self) -> None:
        """A fresh check against GitHub (not the cached result)."""
        self._action_error = ""
        self._status("Checking…")
        self.api.get("/api/updates", self.show_update_state, self._update_error, params={"refresh": "true"})

    def refresh_update_card(self) -> None:
        """Show the last known state; resumes a download that is still running."""
        self.api.get("/api/updates", self.show_update_state, self._update_error)

    def _update_error(self, msg: str) -> None:
        self._upd_poll_busy = False
        self._status("Could not check: " + msg, warn=True)

    def _poll_update(self) -> None:
        if self._upd_poll_busy:
            return
        self._upd_poll_busy = True
        self.api.get("/api/updates", self.show_update_state, lambda _e: setattr(self, "_upd_poll_busy", False))

    def show_update_state(self, d: dict) -> None:
        """Render the state from GET /api/updates or a download reply. Starts the install once a wanted download is ready."""
        self._upd_poll_busy = False
        self._upd_state = d = d or {}
        dl = d.get("download") or {}
        st = dl.get("status") or "idle"
        cur, latest = d.get("current") or "", d.get("latest") or ""
        newer, can = bool(d.get("newer")), bool(d.get("can_install"))
        if st == "error":
            self._want_install = False
        if st == "ready" and self._want_install and not self._installing:
            self._want_install, self._installing = False, True
            self.api.post("/api/updates/install", None, self._install_ok, self._install_fail)
        busy = st == "downloading" or self._installing

        self.u_title.setText(f"Installed: OpenGrokBot {cur}" if cur else "Installed: OpenGrokBot")
        if self._installing:
            self._status(f"Installing {latest or 'the update'}…")
        elif d.get("error") and not latest:
            self._status("Could not check: " + str(d["error"]), warn=True)
        elif newer:
            size = int(d.get("size") or 0)
            self._status(f"{latest} is available (you have {cur})." + (f"  Download: {_mb(size)} MB." if size else ""))
        elif latest or cur:
            self._status(f"You are up to date ({cur}).")
        else:
            self._status("Press Check for updates to look for a new version.")

        if self._installing:
            self.u_bar.setRange(0, 0)
            self.u_prog.setText("Starting the installer. OpenGrokBot will close and open again by itself.")
        elif st == "downloading":
            got, total = int(dl.get("got") or 0), int(dl.get("total") or 0)
            self.u_bar.setRange(0, total)
            self.u_bar.setValue(min(got, total) if total else 0)
            self.u_prog.setText(f"Downloading… {_mb(got)} MB of {_mb(total)} MB" if total else f"Downloading… {_mb(got)} MB")
        elif st == "ready":
            self.u_prog.setText(f"Downloaded {dl.get('version') or latest}. Click Update now to install it.")
        else:
            self.u_prog.setText("")
        show_progress = busy or st == "ready"
        self.u_bar.setVisible(busy)
        self.u_prog.setVisible(show_progress and bool(self.u_prog.text()))

        if st == "error":
            err = "Download failed: " + (dl.get("error") or "unknown error")
        else:
            err = self._action_error
        self.u_error.setText(err)
        self.u_error.setVisible(bool(err))

        self.u_check.setEnabled(not busy)
        self.u_update.setVisible(newer and can)
        self.u_update.setEnabled(not busy)
        self.u_update.setText("Try again" if (st == "error" or self._action_error) else "Update now")
        self.u_release.setVisible(newer and not can)
        self.u_release.setEnabled(bool(d.get("url")))

        if st == "downloading":
            if not self._upd_poll.isActive():
                self._upd_poll.start()
        else:
            self._upd_poll.stop()

    def start_update(self) -> None:
        self._want_install = True
        self._action_error = ""
        self.u_update.setEnabled(False)
        self.api.post("/api/updates/download", None, self.show_update_state, self._download_fail)

    def _download_fail(self, msg: str) -> None:
        self._want_install = False
        self._action_error = "Could not download the update: " + msg
        self.show_update_state(self._upd_state)

    def _install_ok(self, d: dict) -> None:
        if (d or {}).get("ok") is False:
            self._install_fail("the installer did not start")
            return
        self.quitForUpdate.emit()   # the main window quits; the installer replaces the app and relaunches it
        self.show_update_state(self._upd_state)

    def _install_fail(self, msg: str) -> None:
        self._installing = False
        self._action_error = "Could not install the update: " + msg
        self.show_update_state(self._upd_state)

    def open_release(self) -> None:
        url = self._upd_state.get("url") or ""
        if url:
            QDesktopServices.openUrl(QUrl(url))

    # ============================================================ load values
    def load(self) -> None:
        s = self.store.settings
        if not s:
            return
        self._prov_refresh_list()
        self.backup_panel.load()
        self.s_mode.setCurrentIndex(max(0, self.s_mode.findData(s.get("approval", {}).get("default_mode", "ask"))))
        self.s_rprov.clear()
        self.s_rprov.addItem("Same as the Bot's provider", "")
        for p in self.store.profiles:
            self.s_rprov.addItem(p["label"], p["id"])
        self.s_rprov.setCurrentIndex(max(0, self.s_rprov.findData(s.get("reviewer", {}).get("profile", ""))))
        self.s_rmodel.setText(s.get("reviewer", {}).get("model", ""))
        self.s_timeout.setValue(int(s.get("approval", {}).get("timeout_min", 1440)))
        self.s_rtimeout.setValue(int(s.get("approval", {}).get("routine_timeout_min", 120)))
        self.s_cmd.setCurrentIndex(max(0, self.s_cmd.findData(s.get("computer", {}).get("command_policy", "workspace"))))
        self.s_reflect.setChecked(bool(s.get("memory", {}).get("auto_reflect", True)))
        self.s_steps.setValue(int(s.get("defaults", {}).get("step_limit", 40)))
        n = s.get("network", {})
        self.n_mode.setCurrentIndex(max(0, self.n_mode.findData(n.get("mode", "open"))))
        self.n_allow.setPlainText("\n".join(n.get("allow", [])))
        self.n_deny.setPlainText("\n".join(n.get("deny", [])))
        self.n_private.setChecked(bool(n.get("block_private", True)))
        no = s.get("notifications", {})
        self.no_toast.setChecked(bool(no.get("toast", True)))
        self.no_sound.setChecked(bool(no.get("sound", True)))
        self.no_finish.setChecked(bool(no.get("on_finish", True)))
        self.no_appr.setChecked(bool(no.get("on_approval", True)))
        self.no_min.setValue(int(no.get("min_turn_seconds", 20)))
        self.no_url.setText(no.get("ntfy_url", ""))
        self.no_topic.setText(no.get("ntfy_topic", ""))
        self.q_on.setChecked(bool(no.get("quiet_enabled", False)))
        self.q_start.setTime(QTime.fromString(no.get("quiet_start", "22:00"), "HH:mm"))
        self.q_end.setTime(QTime.fromString(no.get("quiet_end", "07:00"), "HH:mm"))
        dg = s.get("digest", {})
        self.d_on.setChecked(bool(dg.get("enabled", False)))
        self.d_time.setTime(QTime.fromString(dg.get("time", "18:00"), "HH:mm"))
        self.c_headless.setChecked(bool(s.get("computer", {}).get("headless", True)))
        self.a_theme.setCurrentIndex(max(0, self.a_theme.findData(s.get("theme", "dark"))))
        self.a_updates.setChecked(bool(s.get("updates", {}).get("check", True)))
        st = self.store.status
        if st:
            self.a_info.setText(f"OpenGrokBot {st.get('version', '')}  ·  data folder: {st.get('data_dir', '')}  ·  secrets backend: {'Windows Credential Manager / keyring' if s.get('secrets_backend') else 'NOT AVAILABLE (use environment variables)'}")
            adm = st.get("admin", {})
            if adm.get("active"):
                self.a_admin.setText(f"Managed by your organization ({adm.get('path')}). Network policy, approval defaults, allowed plugins and limits may be locked.")
                self.admin_banner.setText("Some settings are managed by your organization.")
                self.admin_banner.show()
            else:
                self.a_admin.setText("No administrator policy file is active. See admin-settings.example.json to deploy presets.")
                self.admin_banner.hide()
