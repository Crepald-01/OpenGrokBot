"""Settings panels added in 2.0: notification channels, API tokens, diagnostics, and the backup model.

Also the shared pieces of every settings form: the field form, the status line, dialog headers and the button row.
"""
from __future__ import annotations

import html
import time
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox, QTimeEdit, QVBoxLayout, QWidget)

from . import icons, theme
from .api import Api
from .pages_inbox import fill_row, fmt_time, make_table
from .store import Store
from .widgets import Section, button, chip, clear_layout, label, repolish, set_chip

EVENT_LABELS = {"approval": "Needs approval", "question": "Bot has a question", "takeover": "Needs you at the browser", "login": "Needs a login", "finished": "Task finished",
                "error": "Something went wrong", "routine": "Routines, workflows, triggers", "digest": "Daily digest", "bot_message": "Bot notifications"}

FIELD_HEIGHT = 34   # every single-line field in a settings form is this tall, so a column of them lines up


# ===================================================================================================== shared form pieces
def fit(field: QWidget) -> QWidget:
    """A field fills its column and has the shared height. A combo box must not demand the width of its longest item."""
    field.setMinimumHeight(theme.dp(FIELD_HEIGHT))
    field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    if isinstance(field, QComboBox):
        field.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        field.setMinimumContentsLength(12)
    return field


LABEL_WIDTH = 184   # every form's label column is this wide, so the fields of all sections start at the same x


class Form(QFormLayout):
    """Labels in one left column, fields full width and equally tall."""

    def __init__(self) -> None:
        super().__init__()
        self.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.setHorizontalSpacing(theme.dp(20))
        self.setVerticalSpacing(theme.dp(12))

    def addRow(self, *args) -> None:  # type: ignore[override]
        if len(args) == 2:
            text, field = args
            lb = label(text, wrap=False) if isinstance(text, str) else text
            lb.setMinimumWidth(theme.dp(LABEL_WIDTH))
            if isinstance(field, (QLineEdit, QComboBox, QSpinBox, QTimeEdit)):
                fit(field)
            super().addRow(lb, field)
            return
        super().addRow(*args)


def form() -> QFormLayout:
    return Form()


class StatusLine(QLabel):
    """The one line under a form or button row. Muted by default; green for success, red for an error, amber for a warning.

    setText() keeps the muted style; use ok(), err() or warn() for a coloured message.
    """

    def __init__(self, text: str = "") -> None:
        super().__init__()
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._show(text, "muted")

    def _show(self, text: str, kind: str) -> None:
        p = theme.palette()
        color = {"ok": p["ok"], "err": p["bad"], "warn": p["warn"]}.get(kind, "")
        self.setProperty("muted", kind == "muted")
        QLabel.setText(self, text)
        self.setStyleSheet(f"color: {color};" if color else "")
        self.setVisible(bool(text))   # an empty status line takes no room
        repolish(self)

    def setText(self, text: str) -> None:  # type: ignore[override]
        self._show(text, "muted")

    def ok(self, text: str) -> None:
        self._show(text, "ok")

    def err(self, text: str) -> None:
        self._show(text, "err")

    def warn(self, text: str) -> None:
        self._show(text, "warn")


def dialog_body(dlg: QDialog, title: str, desc: str = "") -> QVBoxLayout:
    """The layout of a settings dialog: the same margins everywhere, a title and one line of explanation."""
    v = QVBoxLayout(dlg)
    v.setContentsMargins(24, 22, 24, 20)
    v.setSpacing(14)
    v.addWidget(label(title, h1=True, wrap=False))
    if desc:
        v.addWidget(label(desc, muted=True))
    return v


def action_row(cancel: Callable[[], None], ok_text: str, ok: Callable[[], None]) -> tuple[QHBoxLayout, QPushButton]:
    """Cancel, then the primary action on the far right. Enter runs the primary action, Escape cancels."""
    row = QHBoxLayout()
    row.setSpacing(8)
    row.setContentsMargins(0, 6, 0, 0)
    row.addStretch(1)
    c = button("Cancel", on=cancel)
    c.setAutoDefault(False)
    o = button(ok_text, primary=True, on=ok)
    o.setDefault(True)
    row.addWidget(c)
    row.addWidget(o)
    return row, o


# ===================================================================================================== channels
class ChannelDialog(QDialog):
    def __init__(self, api: Api, meta: dict, channel: dict | None = None, parent=None):
        super().__init__(parent)
        self.api, self.meta, self.channel = api, meta, channel
        title = "Edit channel" if channel else "Add a channel"
        self.setWindowTitle(title)
        self.resize(600, 600)
        v = dialog_body(self, title, "Send the alerts that pop up on your PC to another place too. Quiet hours and Do Not Disturb apply here as well.")
        f = form()
        self.kind = QComboBox()
        for k, text in meta["kinds"].items():
            self.kind.addItem(text, k)
        if channel:
            self.kind.setCurrentIndex(max(0, self.kind.findData(channel["kind"])))
            self.kind.setEnabled(False)
        self.name = QLineEdit(channel["name"] if channel else "")
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.chat = QLineEdit()
        self.host, self.port, self.user = QLineEdit(), QLineEdit("587"), QLineEdit()
        self.frm, self.to = QLineEdit(), QLineEdit()
        self.sec = QComboBox()
        for text, k in (("STARTTLS (port 587)", "starttls"), ("SSL/TLS (port 465)", "ssl"), ("None (not recommended)", "none")):
            self.sec.addItem(text, k)
        cfg = (channel or {}).get("config", {})
        self.chat.setText(cfg.get("chat_id", ""))
        self.host.setText(cfg.get("host", ""))
        self.port.setText(str(cfg.get("port", 587)))
        self.user.setText(cfg.get("user", ""))
        self.frm.setText(cfg.get("from", ""))
        self.to.setText(cfg.get("to", ""))
        self.sec.setCurrentIndex(max(0, self.sec.findData(cfg.get("security", "starttls"))))
        self.row_secret = (label("", wrap=False), self.secret)
        f.addRow("Type", self.kind)
        f.addRow("Name", self.name)
        f.addRow(self.row_secret[0], self.secret)
        self.rows = {"chat": ("Chat id", self.chat), "host": ("Mail server", self.host), "port": ("Port", self.port), "user": ("User name", self.user), "from": ("From address", self.frm),
                     "to": ("To address", self.to), "sec": ("Security", self.sec)}
        self.row_labels = {}
        for key, (text, w) in self.rows.items():
            lb = label(text, wrap=False)
            self.row_labels[key] = (lb, w)
            f.addRow(lb, w)
        v.addLayout(f)
        v.addWidget(label("Send me", h2=True))
        self.events = {}
        have = set(channel["events"]) if channel else set(meta["default_events"])
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(8)
        for i, e in enumerate(meta["events"]):
            cb = QCheckBox(EVENT_LABELS.get(e, e))
            cb.setChecked(e in have)
            self.events[e] = cb
            grid.addWidget(cb, i // 2, i % 2)
        v.addLayout(grid)
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(channel["enabled"] if channel else True)
        v.addWidget(self.enabled)
        v.addWidget(label("Addresses, tokens and passwords are kept in the Windows Credential Manager and are never shown again.", faint=True))
        v.addStretch(1)
        self.err = StatusLine()
        v.addWidget(self.err)
        row, _ = action_row(self.reject, "Save", self.save)
        v.addLayout(row)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self._kind_changed()

    def _kind_changed(self) -> None:
        k = self.kind.currentData()
        need = {"slack": ("Webhook address", []), "discord": ("Webhook address", []), "webhook": ("Address (URL)", []), "telegram": ("Bot token", ["chat"]),
                "email": ("Password", ["host", "port", "user", "from", "to", "sec"])}[k]
        have = bool(self.channel and self.channel.get("secret_set"))
        self.row_secret[0].setText(need[0] + (" (saved: type to replace)" if have else ""))
        self.secret.setPlaceholderText("Leave empty for no password" if k == "email" else ("•••• saved" if have else "Paste it here"))
        for key, (lb, w) in self.row_labels.items():
            on = key in need[1]
            lb.setVisible(on)
            w.setVisible(on)

    def save(self) -> None:
        k = self.kind.currentData()
        config = {}
        if k == "telegram":
            config = {"chat_id": self.chat.text().strip()}
        elif k == "email":
            config = {"host": self.host.text().strip(), "port": self.port.text().strip(), "user": self.user.text().strip(), "from": self.frm.text().strip(), "to": self.to.text().strip(),
                      "security": self.sec.currentData()}
        body = {"name": self.name.text().strip(), "config": config, "events": [e for e, cb in self.events.items() if cb.isChecked()], "enabled": self.enabled.isChecked()}
        if self.secret.text().strip():
            body["secret"] = self.secret.text().strip()
        fail = lambda e: self.err.err(e)  # noqa: E731
        if self.channel:
            self.api.put(f"/api/channels/{self.channel['id']}", body, lambda _r: self.accept(), fail)
        else:
            self.api.post("/api/channels", {**body, "kind": k}, lambda _r: self.accept(), fail)


class ChannelsPanel(Section):
    """Where else alerts go: Slack, Discord, Telegram, email, a webhook."""

    def __init__(self, api: Api, store: Store):
        super().__init__("Other places to get alerts", "Slack, Discord, Telegram, email or any webhook can get the same alerts as your PC, for the kinds you choose.")
        self.api, self.store = api, store
        self.meta: dict = {"kinds": {}, "events": [], "default_events": []}
        self.channels: list[dict] = []
        self.list = QListWidget()
        self.list.setMinimumHeight(110)
        self.list.setMaximumHeight(170)
        self.list.itemDoubleClicked.connect(lambda _i: self.edit())
        self.add(self.list)
        row = QHBoxLayout()
        row.addWidget(button("Add channel…", icon="plus", on=self.new_channel))
        row.addWidget(button("Edit…", on=self.edit))
        row.addWidget(button("Send test", on=self.test))
        row.addStretch(1)
        row.addWidget(button("Delete", danger=True, on=self.delete))
        self.add(layout=row)
        self.msg = StatusLine()
        self.add(self.msg)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.load()

    def load(self) -> None:
        def ok(d: dict) -> None:
            self.meta, self.channels = d, d["channels"]
            sel = self.selected()
            self.list.clear()
            for c in self.channels:
                state = "off" if not c["enabled"] else ("last send failed" if c.get("last_status") == "error" else "on")
                it = QListWidgetItem(f"{c['name']}  ·  {c['kind_label']}\n{state} · {len(c['events'])} kinds of alert")
                it.setData(Qt.ItemDataRole.UserRole, c)
                self.list.addItem(it)
                if sel and sel["id"] == c["id"]:
                    self.list.setCurrentItem(it)
            if not self.channels:
                it = QListWidgetItem("No channels yet. Add one to get alerts there too.")
                it.setFlags(Qt.ItemFlag.NoItemFlags)
                self.list.addItem(it)
        self.api.get("/api/channels", ok, lambda m: self.msg.err(m))

    def selected(self) -> dict | None:
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def new_channel(self) -> None:
        if not self.meta["kinds"]:
            return
        if ChannelDialog(self.api, self.meta, None, self).exec():
            self.load()

    def edit(self) -> None:
        c = self.selected()
        if c and ChannelDialog(self.api, self.meta, c, self).exec():
            self.load()

    def test(self) -> None:
        c = self.selected()
        if not c:
            return
        self.msg.setText("Sending a test…")

        def done(r: dict) -> None:
            if r["ok"]:
                self.msg.ok("Sent. Check the channel.")
            else:
                self.msg.err(f"That did not work: {r['error']}")
            self.load()
        self.api.post(f"/api/channels/{c['id']}/test", {}, done, lambda m: self.msg.err(m))

    def delete(self) -> None:
        c = self.selected()
        if c and QMessageBox.question(self, "Delete channel", f"Delete “{c['name']}”? Its saved address or password is removed too.") == QMessageBox.StandardButton.Yes:
            self.api.delete(f"/api/channels/{c['id']}", lambda _r: self.load())


# ===================================================================================================== API tokens
SCOPE_TEXT = {"read": "Read only: look at Bots, chats, files and so on. Cannot change anything.",
              "chat": "Read and chat: also send messages to Bots.",
              "full": "Full: read and change things (create Bots, run workflows, edit files). Still cannot touch tokens, backups, channels, settings, keys or approvals."}


class TokenDialog(QDialog):
    def __init__(self, api: Api, parent=None):
        super().__init__(parent)
        self.api = api
        self.created: dict | None = None
        self.setWindowTitle("New API token")
        self.resize(560, 420)
        v = dialog_body(self, "New API token", "An API token lets a script or another program use OpenGrokBot without your main access token. You can revoke it at any time.")
        f = form()
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Home dashboard")
        self.scope = QComboBox()
        for k, text in (("read", "Read only"), ("chat", "Read and chat"), ("full", "Full")):
            self.scope.addItem(text, k)
        self.hint = label(SCOPE_TEXT["read"], faint=True, wrap=True)
        self.hint.setMinimumHeight(self.hint.fontMetrics().height() * 3)
        self.scope.currentIndexChanged.connect(lambda _i: self.hint.setText(SCOPE_TEXT[self.scope.currentData()]))
        self.expires = QComboBox()
        for text, d in (("Never", 0), ("In 30 days", 30), ("In 90 days", 90), ("In 1 year", 365)):
            self.expires.addItem(text, d)
        f.addRow("Name", self.name)
        f.addRow("Can", self.scope)
        f.addRow("", self.hint)
        f.addRow("Expires", self.expires)
        v.addLayout(f)
        v.addStretch(1)
        self.err = StatusLine()
        v.addWidget(self.err)
        row, _ = action_row(self.reject, "Create", self.save)
        v.addLayout(row)

    def save(self) -> None:
        self.api.post("/api/tokens", {"name": self.name.text().strip(), "scope": self.scope.currentData(), "days": self.expires.currentData()},
                      lambda r: (setattr(self, "created", r), self.accept()), lambda e: self.err.err(e))


class TokenShown(QDialog):
    def __init__(self, token: str, name: str, base: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Your new token")
        self.resize(620, 400)
        v = dialog_body(self, "Your new token", f"“{name}” is ready. Copy it now: it is not shown again, and only a fingerprint is kept here.")
        self.box = QPlainTextEdit(token)
        self.box.setReadOnly(True)
        self.box.setFont(theme.mono())
        self.box.setFixedHeight(54)
        v.addWidget(self.box)
        self.copy_btn = button("Copy token", icon="copy", on=self._copy)
        v.addWidget(self.copy_btn, 0, Qt.AlignmentFlag.AlignLeft)
        v.addWidget(label("Use it like this:", h2=True))
        ex = QPlainTextEdit(f"curl -H \"Authorization: Bearer {token}\" {base}/api/bots")
        ex.setReadOnly(True)
        ex.setFont(theme.mono())
        ex.setFixedHeight(70)
        v.addWidget(ex)
        v.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        done = button("Done", primary=True, on=self.accept)
        done.setDefault(True)
        row.addWidget(done)
        v.addLayout(row)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.box.toPlainText())
        self.copy_btn.setText("Copied")


class ApiAccessPanel(Section):
    def __init__(self, api: Api, store: Store):
        super().__init__("API access", "Let scripts and tools read your Bots and chats, send messages or run workflows. Give each tool its own token.")
        self.api, self.store = api, store
        self.table = make_table(["Name", "Can", "Created", "Last used", "Expires", "Status"], 0)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self.add(self.table)
        # shown instead of the table while there are no tokens: a sentence in place of a blank box
        self.empty = QFrame()
        ev = QVBoxLayout(self.empty)
        ev.setContentsMargins(0, 8, 0, 8)
        ev.setSpacing(6)
        ic = QLabel()
        ic.setPixmap(icons.pixmap("key", theme.palette()["faint"], 28))
        ic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ev.addWidget(ic)
        t = label("No tokens yet", h2=True, wrap=False)
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ev.addWidget(t)
        d = label("Create one for each script or tool you connect. Each token can be revoked on its own.", muted=True)
        d.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ev.addWidget(d)
        self.add(self.empty)
        row = QHBoxLayout()
        self.new_btn = button("New token…", primary=True, icon="key", on=self.new)
        self.revoke_btn = button("Revoke", danger=True, on=self.revoke, tip="Anything using this token stops working at once.")
        self.remove_btn = button("Remove from list", danger=True, on=self.remove)
        row.addWidget(self.new_btn)
        row.addStretch(1)
        row.addWidget(self.revoke_btn)
        row.addWidget(self.remove_btn)
        self.add(layout=row)
        self.msg = StatusLine()
        self.add(self.msg)
        self.add(label("Endpoints are listed in docs/API.md in the project. Your main access token (Settings > Mobile) always works too, but it can do everything: prefer a token.", faint=True))
        self._fit()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.load()

    def load(self) -> None:
        def ok(d: dict) -> None:
            self.table.setRowCount(0)
            for t in d["tokens"]:
                status = "revoked" if t["revoked"] else ("expired" if t["expired"] else "active")
                fill_row(self.table, [f"{t['name']}  ({t['prefix']}…)", {"read": "Read", "chat": "Read + chat", "full": "Full"}.get(t["scope"], t["scope"]), fmt_time(t["created_at"]),
                                      fmt_time(t["last_used_at"]) if t["last_used_at"] else "never", fmt_time(t["expires_at"]) if t["expires_at"] else "never", status], t)
            self.table.resizeRowsToContents()
            self._fit()
        self.api.get("/api/tokens", ok, lambda m: self.msg.err(m))

    def _fit(self) -> None:
        """The table is only as tall as its rows; with no tokens the empty state is shown instead."""
        n = self.table.rowCount()
        self.table.setVisible(n > 0)
        self.empty.setVisible(n == 0)
        if n:
            h = self.table.horizontalHeader().sizeHint().height() + sum(self.table.rowHeight(r) for r in range(n)) + 4
            self.table.setFixedHeight(h)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        t = self.selected()
        self.revoke_btn.setEnabled(bool(t and not t["revoked"]))
        self.remove_btn.setEnabled(bool(t))

    def selected(self) -> dict | None:
        r = self.table.currentRow()
        return self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) if r >= 0 and self.table.item(r, 0) else None

    def new(self) -> None:
        d = TokenDialog(self.api, self)
        if d.exec() and d.created:
            self.load()
            TokenShown(d.created["token"], d.created["name"], self.api.conn.base, self).exec()

    def revoke(self) -> None:
        t = self.selected()
        if t and not t["revoked"] and QMessageBox.question(self, "Revoke token", f"Revoke “{t['name']}”? Anything using it stops working immediately.") == QMessageBox.StandardButton.Yes:
            self.api.delete(f"/api/tokens/{t['id']}", lambda _r: (self.msg.ok("Revoked."), self.load()), lambda m: self.msg.err(m))

    def remove(self) -> None:
        t = self.selected()
        if t and QMessageBox.question(self, "Remove token", f"Remove “{t['name']}” from the list? It is revoked first.") == QMessageBox.StandardButton.Yes:
            self.api.delete(f"/api/tokens/{t['id']}", lambda _r: self.api.delete(f"/api/tokens/{t['id']}", lambda _r2: self.load(), params={"delete": "true"}))


# ===================================================================================================== diagnostics
class DiagnosticsPanel(QWidget):
    """Health checks as rows: status dot, title, a status chip, the detail and, when something is wrong, what to do."""

    def __init__(self, api: Api, store: Store):
        super().__init__()
        self.api, self.store = api, store
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)
        v.addWidget(label("Diagnostics", h2=True))
        v.addWidget(label("A quick health check of the whole app, in plain words, with what to do about anything that is wrong.", muted=True))
        row = QHBoxLayout()
        self.run_btn = button("Run checks", primary=True, icon="refresh", on=self.run)
        self.bundle_btn = button("Save support bundle…", icon="download", on=self.bundle, tip="A zip with the check results, versions, settings without secrets and the log. No chats, memories, files or keys.")
        row.addWidget(self.run_btn)
        row.addWidget(self.bundle_btn)
        row.addStretch(1)
        self.summary = chip("", "true")
        self.summary.hide()
        row.addWidget(self.summary)
        v.addLayout(row)
        self.box = QVBoxLayout()
        self.box.setSpacing(10)
        v.addLayout(self.box)
        self.msg = StatusLine()
        v.addWidget(self.msg)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if self.box.count() == 0:
            self.run()

    def _row(self, c: dict) -> QFrame:
        p = theme.palette()
        status = c["status"]
        color = {"ok": p["ok"], "warn": p["warn"]}.get(status, p["bad"])
        word, kind = {"ok": ("OK", "ok"), "warn": ("Check", "warn")}.get(status, ("Problem", "bad"))
        fr = QFrame()
        fr.setProperty("card", "true")
        h = QHBoxLayout(fr)
        h.setContentsMargins(16, 12, 16, 12)
        h.setSpacing(12)
        dot = QLabel()
        dot.setPixmap(theme.status_dot(color, 10).pixmap(10, 10))
        dot.setContentsMargins(0, 6, 0, 0)     # lines the dot up with the title
        dot.setFixedWidth(12)
        dot.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        h.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(label(c["title"], h2=True, wrap=False))
        head.addStretch(1)
        head.addWidget(chip(word, kind))
        col.addLayout(head)
        col.addWidget(label(c["detail"], muted=True))
        if c["fix"] and status != "ok":
            fx = label(f"<b>What to do:</b> {html.escape(c['fix'])}")
            fx.setStyleSheet(f"color: {color};")
            col.addWidget(fx)
        h.addLayout(col, 1)
        return fr

    def run(self) -> None:
        self.run_btn.setEnabled(False)
        self.msg.setText("Checking…")

        def ok(d: dict) -> None:
            self.run_btn.setEnabled(True)
            self.msg.setText("")
            clear_layout(self.box)
            for c in d["checks"]:
                self.box.addWidget(self._row(c))
            s = d["summary"]
            self.summary.show()
            if s["fail"]:
                set_chip(self.summary, f"{s['fail']} problem{'s' if s['fail'] != 1 else ''}", "bad")
            elif s["warn"]:
                set_chip(self.summary, f"{s['warn']} to look at", "warn")
            else:
                set_chip(self.summary, "All good", "ok")
        self.api.get("/api/diagnostics", ok, lambda m: (self.run_btn.setEnabled(True), self.msg.err(m)))

    def bundle(self) -> None:
        name = "opengrokbot-support-" + time.strftime("%Y%m%d-%H%M") + ".zip"
        path, _ = QFileDialog.getSaveFileName(self, "Save support bundle", name, "Zip (*.zip)")
        if not path:
            return

        def ok(data: bytes) -> None:
            with open(path, "wb") as f:
                f.write(data)
            self.msg.ok(f"Saved {path}. Open service.log before sharing it, to check you are happy with what it shows.")
        self.api.request("GET", "/api/diagnostics/bundle", ok, lambda m: self.msg.err(m), raw=True)


# ===================================================================================================== backup model
class BackupModelPanel(Section):
    """The app-wide backup model: used when a Bot's own model is rate limited, down, unreachable or rejects its key."""

    def __init__(self, api: Api, store: Store):
        super().__init__("Backup model", "Used when a Bot's model is rate limited, down or rejects its key. The Bot says so in its chat. A Bot can have its own backup in its settings.")
        self.api, self.store = api, store
        f = form()
        self.prov = QComboBox()
        self.model = QLineEdit()
        self.model.setPlaceholderText("Provider default")
        f.addRow("Provider", self.prov)
        f.addRow("Model", self.model)
        self.add(layout=f)
        row = QHBoxLayout()
        row.addWidget(button("Save backup model", on=self.save))
        row.addStretch(1)
        self.add(layout=row)
        self.msg = StatusLine()
        self.add(self.msg)

    def load(self) -> None:
        s = self.store.settings.get("fallback", {}) or {}
        self.prov.blockSignals(True)
        self.prov.clear()
        self.prov.addItem("No backup", "")
        for p in self.store.profiles:
            self.prov.addItem(p["label"], p["id"])
        idx = self.prov.findData(s.get("profile", ""))
        self.prov.setCurrentIndex(max(0, idx))
        self.prov.blockSignals(False)
        self.model.setText(s.get("model", ""))

    def save(self) -> None:
        prof = self.prov.currentData() or ""
        body = {"fallback.profile": prof, "fallback.model": self.model.text().strip() if prof else ""}
        self.api.put("/api/settings", body, lambda s: (setattr(self.store, "settings", s), self.msg.ok("Saved." if prof else "Backup model removed.")), lambda e: self.msg.err(e))
