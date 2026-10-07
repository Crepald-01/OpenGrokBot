"""Settings panels added in 2.0: notification channels, API tokens, diagnostics, and the backup model."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QVBoxLayout, QWidget)

from . import theme
from .api import Api
from .pages_inbox import fill_row, fmt_time, make_table
from .store import Store
from .widgets import button, chip, clear_layout, label, set_chip

EVENT_LABELS = {"approval": "Needs approval", "question": "Bot has a question", "takeover": "Needs you at the browser", "login": "Needs a login", "finished": "Task finished",
                "error": "Something went wrong", "routine": "Routines, workflows, triggers", "digest": "Daily digest", "bot_message": "Bot notifications"}


def form() -> QFormLayout:
    f = QFormLayout()
    f.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
    f.setHorizontalSpacing(20)
    f.setVerticalSpacing(12)
    return f


# ===================================================================================================== channels
class ChannelDialog(QDialog):
    def __init__(self, api: Api, meta: dict, channel: dict | None = None, parent=None):
        super().__init__(parent)
        self.api, self.meta, self.channel = api, meta, channel
        self.setWindowTitle("Edit channel" if channel else "Add a channel")
        self.resize(600, 600)
        v = QVBoxLayout(self)
        v.addWidget(label("Send the alerts that pop up on your PC to another place too. Quiet hours and Do Not Disturb apply here as well. "
                          "Addresses, tokens and passwords are kept in the Windows Credential Manager and are never shown again.", muted=True))
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
        grid.setVerticalSpacing(6)
        for i, e in enumerate(meta["events"]):
            cb = QCheckBox(EVENT_LABELS.get(e, e))
            cb.setChecked(e in have)
            self.events[e] = cb
            grid.addWidget(cb, i // 2, i % 2)
        v.addLayout(grid)
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(channel["enabled"] if channel else True)
        v.addWidget(self.enabled)
        self.err = label("")
        v.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
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
        fail = lambda e: self.err.setText(e)
        if self.channel:
            self.api.put(f"/api/channels/{self.channel['id']}", body, lambda _r: self.accept(), fail)
        else:
            self.api.post("/api/channels", {**body, "kind": k}, lambda _r: self.accept(), fail)


class ChannelsPanel(QWidget):
    """Where else alerts go: Slack, Discord, Telegram, email, a webhook."""

    def __init__(self, api: Api, store: Store):
        super().__init__()
        self.api, self.store = api, store
        self.meta: dict = {"kinds": {}, "events": [], "default_events": []}
        self.channels: list[dict] = []
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        v.addWidget(label("Other places to get alerts", h2=True))
        v.addWidget(label("Slack, Discord, Telegram, email or any webhook can get the same alerts as your PC, for the kinds you choose.", muted=True))
        self.list = QListWidget()
        self.list.setMinimumHeight(110)
        self.list.setMaximumHeight(170)
        self.list.itemDoubleClicked.connect(lambda _i: self.edit())
        v.addWidget(self.list)
        row = QHBoxLayout()
        row.addWidget(button("Add channel…", icon="plus", on=self.add))
        row.addWidget(button("Edit…", on=self.edit))
        row.addWidget(button("Send test", on=self.test))
        row.addWidget(button("Delete", danger=True, on=self.delete))
        row.addStretch(1)
        v.addLayout(row)
        self.msg = label("", muted=True)
        v.addWidget(self.msg)

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
                it = QListWidgetItem("No channels yet.")
                it.setFlags(Qt.ItemFlag.NoItemFlags)
                self.list.addItem(it)
        self.api.get("/api/channels", ok, lambda m: self.msg.setText(m))

    def selected(self) -> dict | None:
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def add(self) -> None:
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
        self.api.post(f"/api/channels/{c['id']}/test", {}, lambda r: (self.msg.setText("Sent. Check the channel." if r["ok"] else f"That did not work: {r['error']}"), self.load()),
                      lambda m: self.msg.setText(m))

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
        self.resize(520, 330)
        v = QVBoxLayout(self)
        v.addWidget(label("An API token lets a script or another program use OpenGrokBot without your main access token. You can revoke it at any time.", muted=True))
        f = form()
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Home dashboard")
        self.scope = QComboBox()
        for k, text in (("read", "Read only"), ("chat", "Read and chat"), ("full", "Full")):
            self.scope.addItem(text, k)
        self.hint = label(SCOPE_TEXT["read"], faint=True)
        self.scope.currentIndexChanged.connect(lambda _i: self.hint.setText(SCOPE_TEXT[self.scope.currentData()]))
        self.expires = QComboBox()
        for text, d in (("Never", 0), ("In 30 days", 30), ("In 90 days", 90), ("In 1 year", 365)):
            self.expires.addItem(text, d)
        f.addRow("Name", self.name)
        f.addRow("Can", self.scope)
        f.addRow("", self.hint)
        f.addRow("Expires", self.expires)
        v.addLayout(f)
        self.err = label("")
        v.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Create")
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def save(self) -> None:
        self.api.post("/api/tokens", {"name": self.name.text().strip(), "scope": self.scope.currentData(), "days": self.expires.currentData()},
                      lambda r: (setattr(self, "created", r), self.accept()), lambda e: self.err.setText(e))


class TokenShown(QDialog):
    def __init__(self, token: str, name: str, base: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Your new token")
        self.resize(620, 360)
        v = QVBoxLayout(self)
        v.addWidget(label(f"“{name}” is ready. Copy it now: it is not shown again, and only a fingerprint is kept here.", muted=True))
        self.box = QPlainTextEdit(token)
        self.box.setReadOnly(True)
        self.box.setFont(theme.mono())
        self.box.setFixedHeight(54)
        v.addWidget(self.box)
        self.copy_btn = button("Copy token", primary=True, icon="copy", on=self._copy)
        row = QHBoxLayout()
        row.addWidget(self.copy_btn)
        row.addStretch(1)
        v.addLayout(row)
        v.addWidget(label("Use it like this:", faint=True))
        ex = QPlainTextEdit(f"curl -H \"Authorization: Bearer {token}\" {base}/api/bots")
        ex.setReadOnly(True)
        ex.setFont(theme.mono())
        ex.setFixedHeight(70)
        v.addWidget(ex)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        v.addWidget(bb)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.box.toPlainText())
        self.copy_btn.setText("Copied")


class ApiAccessPanel(QWidget):
    def __init__(self, api: Api, store: Store):
        super().__init__()
        self.api, self.store = api, store
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)
        v.addWidget(label("API access", h2=True))
        v.addWidget(label("Let scripts and other tools talk to OpenGrokBot: read your Bots and chats, send messages, run workflows. Give each tool its own token, "
                          "so you can revoke one without touching the others. Tokens never reveal or change your keys, approvals, settings or backups.", muted=True))
        self.table = make_table(["Name", "Can", "Created", "Last used", "Expires", "Status"], 0)
        self.table.setMinimumHeight(180)
        v.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addWidget(button("New token…", primary=True, icon="key", on=self.new))
        row.addWidget(button("Revoke", on=self.revoke))
        row.addWidget(button("Remove from list", danger=True, on=self.remove))
        row.addStretch(1)
        v.addLayout(row)
        self.msg = label("", muted=True)
        v.addWidget(self.msg)
        v.addWidget(label("Endpoints are listed in docs/API.md in the project. Your main access token (Settings > Mobile) always works too, but it can do everything: prefer a token.", faint=True))

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
        self.api.get("/api/tokens", ok, lambda m: self.msg.setText(m))

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
            self.api.delete(f"/api/tokens/{t['id']}", lambda _r: (self.msg.setText("Revoked."), self.load()))

    def remove(self) -> None:
        t = self.selected()
        if t and QMessageBox.question(self, "Remove token", f"Remove “{t['name']}” from the list? It is revoked first.") == QMessageBox.StandardButton.Yes:
            self.api.delete(f"/api/tokens/{t['id']}", lambda _r: self.api.delete(f"/api/tokens/{t['id']}", lambda _r2: self.load(), params={"delete": "true"}))


# ===================================================================================================== diagnostics
class DiagnosticsPanel(QWidget):
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
        row.addWidget(self.summary)
        v.addLayout(row)
        self.box = QVBoxLayout()
        self.box.setSpacing(8)
        v.addLayout(self.box)
        self.msg = label("", muted=True)
        v.addWidget(self.msg)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if self.box.count() == 0:
            self.run()

    def run(self) -> None:
        self.run_btn.setEnabled(False)
        self.msg.setText("Checking…")

        def ok(d: dict) -> None:
            self.run_btn.setEnabled(True)
            self.msg.setText("")
            clear_layout(self.box)
            p = theme.palette()
            for c in d["checks"]:
                fr = QFrame()
                fr.setProperty("card", "true")
                h = QHBoxLayout(fr)
                h.setContentsMargins(14, 10, 14, 10)
                h.setSpacing(12)
                dot = label({"ok": "●", "warn": "●", "fail": "●"}[c["status"]], wrap=False)
                dot.setStyleSheet(f"color: {p['ok'] if c['status'] == 'ok' else p['warn'] if c['status'] == 'warn' else p['bad']}; font-size: 16px;")
                h.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
                col = QVBoxLayout()
                col.setSpacing(2)
                col.addWidget(label(c["title"], wrap=False))
                col.addWidget(label(c["detail"], muted=True))
                if c["fix"] and c["status"] != "ok":
                    fx = label("What to do: " + c["fix"])
                    fx.setStyleSheet(f"color: {p['warn'] if c['status'] == 'warn' else p['bad']};")
                    col.addWidget(fx)
                h.addLayout(col, 1)
                self.box.addWidget(fr)
            s = d["summary"]
            if s["fail"]:
                set_chip(self.summary, f"{s['fail']} problem{'s' if s['fail'] != 1 else ''}", "bad")
            elif s["warn"]:
                set_chip(self.summary, f"{s['warn']} to look at", "warn")
            else:
                set_chip(self.summary, "All good", "ok")
        self.api.get("/api/diagnostics", ok, lambda m: (self.run_btn.setEnabled(True), self.msg.setText(m)))

    def bundle(self) -> None:
        name = "opengrokbot-support-" + time.strftime("%Y%m%d-%H%M") + ".zip"
        path, _ = QFileDialog.getSaveFileName(self, "Save support bundle", name, "Zip (*.zip)")
        if not path:
            return

        def ok(data: bytes) -> None:
            with open(path, "wb") as f:
                f.write(data)
            self.msg.setText(f"Saved {path}. Open service.log before sharing it, to check you are happy with what it shows.")
        self.api.request("GET", "/api/diagnostics/bundle", ok, lambda m: self.msg.setText(m), raw=True)


# ===================================================================================================== backup model
class BackupModelPanel(QWidget):
    """The app-wide backup model: used when a Bot's own model is rate limited, down, unreachable or rejects its key."""

    def __init__(self, api: Api, store: Store):
        super().__init__()
        self.api, self.store = api, store
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        v.addWidget(label("Backup model", h2=True))
        v.addWidget(label("If a Bot's model is rate limited, has an outage, cannot be reached or rejects its key, the Bot switches to this one for the rest of that task, and says so in its chat. "
                          "A Bot can have its own backup in its settings.", muted=True))
        f = form()
        self.prov = QComboBox()
        self.model = QLineEdit()
        self.model.setPlaceholderText("Provider default")
        for text, w in (("Provider", self.prov), ("Model", self.model)):
            lb = label(text, wrap=False)
            lb.setMinimumWidth(84)   # lines the fields up with the form above, which has a longer label ("Default model")
            f.addRow(lb, w)
        v.addLayout(f)
        row = QHBoxLayout()
        row.addWidget(button("Save backup model", on=self.save))
        self.msg = label("", muted=True, wrap=False)
        row.addWidget(self.msg, 1)
        v.addLayout(row)

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
        self.api.put("/api/settings", body, lambda s: (setattr(self.store, "settings", s), self.msg.setText("Saved." if prof else "Backup model removed.")), lambda e: self.msg.setText(e))
