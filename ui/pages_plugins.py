"""Plugins, connectors and MCP servers."""
from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QScrollArea, QTabWidget, QVBoxLayout, QWidget)

from . import theme
from .api import Api
from .pages_inbox import fill_row, make_table
from .store import Store
from .widgets import button, card, chip, clear_layout, label, PageHeader, page_layout


class ConfigureDialog(QDialog):
    def __init__(self, api: Api, info: dict, parent=None):
        super().__init__(parent)
        self.api, self.info = api, info
        self.setWindowTitle(f"Configure {info['name']}")
        self.resize(520, 360)
        v = QVBoxLayout(self)
        if info.get("help"):
            v.addWidget(label(info["help"], muted=True))
        f = QFormLayout()
        self.edits: dict[str, QLineEdit] = {}
        for fld in info["fields"]:
            e = QLineEdit(fld.get("value", ""))
            e.setPlaceholderText("•••••••• saved (type to replace)" if fld.get("secret") and fld.get("set") else fld.get("placeholder", ""))
            if fld.get("secret"):
                e.setEchoMode(QLineEdit.EchoMode.Password)
            f.addRow(fld["label"] + (" *" if fld.get("required") else ""), e)
            self.edits[fld["key"]] = e
        if not info["fields"]:
            f.addRow(label("This plugin needs no configuration."))
        v.addLayout(f)
        v.addWidget(label("Secrets are stored in the Windows Credential Manager, never in files. Everything on the shared computer, including connected accounts, is available to every Bot you grant it to.", muted=True))
        self.err = label("")
        self.err.setStyleSheet(f"color: {theme.palette()['bad']};")
        v.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def save(self) -> None:
        body = {k: e.text() for k, e in self.edits.items() if e.text() != "" or not any(f["key"] == k and f.get("secret") for f in self.info["fields"])}
        self.api.put(f"/api/plugins/{self.info['id']}/config", body, lambda _: self.accept(), lambda e: self.err.setText(e))


class McpDialog(QDialog):
    def __init__(self, api: Api, cfg: dict | None = None, parent=None):
        super().__init__(parent)
        self.api, self.cfg = api, cfg or {}
        self.setWindowTitle("MCP server")
        self.resize(560, 520)
        v = QVBoxLayout(self)
        v.addWidget(label("Add any Model Context Protocol server. Its tools become available to the Bots you grant it to. Use ${secret:NAME} in env or headers to pull a value from the credential store.", muted=True))
        f = QFormLayout()
        self.name = QLineEdit(self.cfg.get("name", ""))
        self.transport = QComboBox()
        for t, k in (("Local program (stdio)", "stdio"), ("Remote: streamable HTTP", "http"), ("Remote: SSE", "sse")):
            self.transport.addItem(t, k)
        self.transport.setCurrentIndex(max(0, self.transport.findData(self.cfg.get("transport", "stdio"))))
        self.command = QLineEdit(self.cfg.get("command", ""))
        self.command.setPlaceholderText("npx")
        self.args = QLineEdit(" ".join(self.cfg.get("args", [])))
        self.args.setPlaceholderText("-y @modelcontextprotocol/server-filesystem C:\\path")
        self.url = QLineEdit(self.cfg.get("url", ""))
        self.url.setPlaceholderText("https://example.com/mcp")
        self.env = QPlainTextEdit("\n".join(f"{k}={v_}" for k, v_ in (self.cfg.get("env") or {}).items()))
        self.env.setPlaceholderText("KEY=value  (one per line)")
        self.env.setFixedHeight(70)
        self.headers = QPlainTextEdit("\n".join(f"{k}: {v_}" for k, v_ in (self.cfg.get("headers") or {}).items()))
        self.headers.setPlaceholderText("Authorization: Bearer ${secret:mcp:myserver}")
        self.headers.setFixedHeight(70)
        self.trust = QComboBox()
        for t, k in (("Ask before each tool call", "ask"), ("Read-only tools run freely, others ask", "readonly_auto"), ("Trusted: never ask", "trusted")):
            self.trust.addItem(t, k)
        self.trust.setCurrentIndex(max(0, self.trust.findData(self.cfg.get("trust", "ask"))))
        for lab, w in (("Name", self.name), ("Transport", self.transport), ("Command", self.command), ("Arguments", self.args), ("URL", self.url), ("Environment", self.env),
                       ("Headers", self.headers), ("Approvals", self.trust)):
            f.addRow(lab, w)
        v.addLayout(f)
        self.err = label("")
        v.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def save(self) -> None:
        import shlex
        try:
            args = shlex.split(self.args.text(), posix=False)
        except ValueError:
            args = self.args.text().split()
        env = dict(l.split("=", 1) for l in self.env.toPlainText().splitlines() if "=" in l)
        headers = {k.strip(): v_.strip() for k, v_ in (l.split(":", 1) for l in self.headers.toPlainText().splitlines() if ":" in l)}
        body = {"id": self.cfg.get("id"), "name": self.name.text().strip(), "transport": self.transport.currentData(), "command": self.command.text().strip(),
                "args": args, "url": self.url.text().strip(), "env": env, "headers": headers, "trust": self.trust.currentData(), "enabled": True}
        self.api.post("/api/mcp", body, lambda _: self.accept(), lambda e: self.err.setText(e))


class RestDialog(QDialog):
    def __init__(self, api: Api, parent=None):
        super().__init__(parent)
        self.api = api
        self.setWindowTitle("Add a REST connector")
        self.resize(500, 340)
        v = QVBoxLayout(self)
        v.addWidget(label("A generic connector for any JSON API. Bots can GET freely; POST/PUT/PATCH/DELETE ask for approval unless you allow writes.", muted=True))
        f = QFormLayout()
        self.name = QLineEdit()
        self.base = QLineEdit()
        self.base.setPlaceholderText("https://api.example.com/v1")
        self.auth = QComboBox()
        for t, k in (("No authentication", "none"), ("Bearer token", "bearer"), ("API key in a header", "header"), ("Basic auth", "basic")):
            self.auth.addItem(t, k)
        self.header = QLineEdit("X-API-Key")
        self.user = QLineEdit()
        self.user.setPlaceholderText("Username (basic auth)")
        self.desc = QLineEdit()
        self.desc.setPlaceholderText("What this API is for (shown to the Bot)")
        for lab, w in (("Name", self.name), ("Base URL", self.base), ("Auth", self.auth), ("Header name", self.header), ("Username", self.user), ("Description", self.desc)):
            f.addRow(lab, w)
        v.addLayout(f)
        self.writes = None
        self.err = label("")
        v.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def save(self) -> None:
        body = {"name": self.name.text().strip(), "base_url": self.base.text().strip(), "auth": self.auth.currentData(), "header_name": self.header.text().strip(),
                "username": self.user.text().strip(), "description": self.desc.text().strip(), "allow_writes": False}
        self.api.post("/api/plugins/rest", body, lambda _: self.accept(), lambda e: self.err.setText(e))


class PluginsPage(QWidget):
    def __init__(self, api: Api, store: Store):
        super().__init__()
        self.api, self.store = api, store
        v = page_layout(self, PageHeader("Plugins & connectors", "Bots use connectors where they exist and the browser for everything else. A Bot only gets a connector after you grant it, usually when it asks."))
        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)

        self.conn_box = QWidget()
        self.conn_l = QVBoxLayout(self.conn_box)
        self.conn_l.setSpacing(10)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(self.conn_box)
        w = QWidget()
        wl = QVBoxLayout(w)
        bar = QHBoxLayout()
        bar.addWidget(button("Add REST connector…", on=self.add_rest))
        bar.addStretch(1)
        wl.addLayout(bar)
        wl.addWidget(sc, 1)
        self.tabs.addTab(w, "Connectors")

        self.market = QWidget()
        mv = QVBoxLayout(self.market)
        inst = QHBoxLayout()
        self.source = QLineEdit()
        self.source.setPlaceholderText("Install from a local folder or a git URL (https://github.com/you/your-plugin.git)")
        inst.addWidget(self.source, 1)
        inst.addWidget(button("Browse…", on=self.browse))
        inst.addWidget(button("Install", primary=True, on=lambda: self.install(self.source.text().strip())))
        mv.addLayout(inst)
        self.market_l = QVBoxLayout()
        self.market_l.setSpacing(10)
        msc = QScrollArea()
        msc.setWidgetResizable(True)
        mw = QWidget()
        mw.setLayout(self.market_l)
        msc.setWidget(mw)
        mv.addWidget(msc, 1)
        self.tabs.addTab(self.market, "Marketplace")

        mcp = QWidget()
        cv = QVBoxLayout(mcp)
        cb = QHBoxLayout()
        cb.addWidget(button("Add server…", primary=True, on=lambda: self.edit_mcp(None)))
        cb.addWidget(button("Paste JSON config…", on=self.paste_mcp))
        cb.addWidget(button("Edit…", on=self.edit_selected_mcp))
        cb.addWidget(button("Restart", on=self.restart_mcp))
        cb.addWidget(button("Remove", danger=True, on=self.remove_mcp))
        cb.addStretch(1)
        cv.addLayout(cb)
        self.mcp = make_table(["Name", "Transport", "Command / URL", "Status", "Tools", "Approvals"], 2)
        cv.addWidget(self.mcp)
        self.tabs.addTab(mcp, "MCP servers")
        store.event.connect(lambda ev: ev.get("type") == "plugins" and self.isVisible() and self.load())
        self.tabs.currentChanged.connect(lambda _: self.load())

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.load()

    def load(self) -> None:
        def ok(d: dict) -> None:
            clear_layout(self.conn_l)
            for p in d["plugins"]:
                self.conn_l.addWidget(self._plugin_card(p))
            self.conn_l.addStretch(1)
            clear_layout(self.market_l)
            if not d["install_allowed"]:
                self.market_l.addWidget(label("Plugin installation is disabled by your administrator."))
            for e in d["catalog"]:
                self.market_l.addWidget(self._catalog_card(e, d["install_allowed"]))
            self.market_l.addStretch(1)
        self.api.get("/api/plugins", ok)
        self.load_mcp()

    def _plugin_card(self, p: dict) -> QFrame:
        c = card()
        h = QVBoxLayout(c)
        h.setContentsMargins(14, 12, 14, 12)
        top = QHBoxLayout()
        top.addWidget(label(p["name"], h2=True, wrap=False))
        top.addWidget(chip({"builtin": "built-in", "declarative": "plugin", "python": "code plugin", "rest": "REST"}.get(p["kind"], p["kind"])))
        if not p["allowed"]:
            top.addWidget(chip("blocked by admin", "bad"))
        elif p["configured"]:
            top.addWidget(chip("connected", "ok"))
        else:
            top.addWidget(chip("needs setup", "warn"))
        top.addStretch(1)
        h.addLayout(top)
        h.addWidget(label(p["description"], muted=True))
        tools = ", ".join(t["name"] + ("" if t["read_only"] else "*") for t in p["tools"][:8])
        if tools:
            h.addWidget(label("Tools: " + tools + ("…" if len(p["tools"]) > 8 else "") + "   (* asks for approval)", muted=True))
        row = QHBoxLayout()
        if p["allowed"]:
            row.addWidget(button("Configure…", on=lambda p=p: self.configure(p)))
            if p["oauth"]:
                row.addWidget(button("Connect…" if not p["connected"] else "Reconnect…", primary=not p["connected"], on=lambda p=p: self.connect_oauth(p)))
            row.addWidget(button("Test", on=lambda p=p: self.test(p)))
        if p["removable"]:
            row.addWidget(button("Remove", danger=True, on=lambda p=p: self.remove(p)))
        row.addStretch(1)
        h.addLayout(row)
        return c

    def _catalog_card(self, e: dict, allowed: bool) -> QFrame:
        c = card()
        h = QHBoxLayout(c)
        h.setContentsMargins(14, 12, 14, 12)
        col = QVBoxLayout()
        top = QHBoxLayout()
        top.addWidget(label(e["name"], h2=True, wrap=False))
        top.addWidget(chip({"declarative": "no code", "python": "runs code"}.get(e.get("kind", ""), e.get("kind", ""))))
        top.addStretch(1)
        col.addLayout(top)
        col.addWidget(label(e.get("description", ""), muted=True))
        h.addLayout(col, 1)
        if e.get("installed"):
            h.addWidget(chip("installed", "ok"))
        else:
            b = button("Install", primary=True, on=lambda e=e: self.install(e["source"], e.get("kind")))
            b.setEnabled(allowed)
            h.addWidget(b)
        return c

    # -- actions ----------------------------------------------------------------------
    def configure(self, p: dict) -> None:
        if ConfigureDialog(self.api, p, self).exec():
            self.store.refresh_all()
            self.load()

    def connect_oauth(self, p: dict) -> None:
        def ok(d: dict) -> None:
            QDesktopServices.openUrl(QUrl(d["url"]))
            QMessageBox.information(self, "Sign in", "Finish signing in in your browser. This window updates when it is done.")
        self.api.post(f"/api/plugins/{p['id']}/connect", {}, ok, lambda e: QMessageBox.warning(self, "Cannot connect", e))

    def test(self, p: dict) -> None:
        self.api.post(f"/api/plugins/{p['id']}/test", {}, lambda d: QMessageBox.information(self, p["name"], d["message"]), lambda e: QMessageBox.warning(self, p["name"], e))

    def remove(self, p: dict) -> None:
        if QMessageBox.question(self, "Remove", f"Remove {p['name']} and delete its saved credentials?") == QMessageBox.StandardButton.Yes:
            self.api.delete(f"/api/plugins/{p['id']}", lambda _: self.load())

    def add_rest(self) -> None:
        if RestDialog(self.api, self).exec():
            self.load()

    def browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose a plugin folder (contains plugin.json)")
        if d:
            self.source.setText(d)

    def install(self, source: str, kind: str | None = None) -> None:
        if not source:
            return
        if kind != "declarative":
            if QMessageBox.question(self, "Install plugin", "Code plugins run inside the background service with your permissions. Only install plugins you trust.\n\nContinue?") != QMessageBox.StandardButton.Yes:
                return
        self.api.post("/api/plugins/install", {"source": source}, lambda p: (QMessageBox.information(self, "Installed", f"{p['name']} installed. Configure it, then grant it to a Bot."), self.load()),
                      lambda e: QMessageBox.warning(self, "Install failed", e), timeout=240)

    # -- MCP -----------------------------------------------------------------------------
    def load_mcp(self) -> None:
        def ok(rows: list) -> None:
            self.mcp.setRowCount(0)
            for r in rows:
                fill_row(self.mcp, [r["name"], r["transport"], r["command"] + " " + " ".join(r["args"]) if r["transport"] == "stdio" else r["url"],
                                    r["status"] + (f": {r['error'][:80]}" if r["error"] else ""), r["tool_count"], r["trust"]], r)
            self.mcp.resizeRowsToContents()
        self.api.get("/api/mcp", ok)

    def _sel(self) -> dict | None:
        r = self.mcp.currentRow()
        return self.mcp.item(r, 0).data(Qt.ItemDataRole.UserRole) if r >= 0 else None

    def edit_mcp(self, cfg: dict | None) -> None:
        if McpDialog(self.api, cfg, self).exec():
            self.load_mcp()

    def edit_selected_mcp(self) -> None:
        s = self._sel()
        if s:
            self.edit_mcp(s)

    def paste_mcp(self) -> None:
        d = QDialog(self)
        d.setWindowTitle("Paste MCP config")
        d.resize(540, 360)
        v = QVBoxLayout(d)
        v.addWidget(label('Paste the standard config, e.g. {"mcpServers": {"fs": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]}}}', muted=True))
        te = QPlainTextEdit()
        v.addWidget(te, 1)
        err = label("")
        v.addWidget(err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        v.addWidget(bb)
        bb.rejected.connect(d.reject)
        bb.accepted.connect(lambda: self.api.post("/api/mcp", {"import_json": te.toPlainText()}, lambda _: d.accept(), lambda e: err.setText(e)))
        if d.exec():
            self.load_mcp()

    def restart_mcp(self) -> None:
        s = self._sel()
        if s:
            self.api.post(f"/api/mcp/{s['id']}/restart", {}, lambda _: self.load_mcp())

    def remove_mcp(self) -> None:
        s = self._sel()
        if s and QMessageBox.question(self, "Remove", f"Remove MCP server {s['name']}?") == QMessageBox.StandardButton.Yes:
            self.api.delete(f"/api/mcp/{s['id']}", lambda _: self.load_mcp())
