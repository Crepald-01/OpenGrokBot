"""Dialogs: new Bot (templates), Bot editor, group chat."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from . import theme
from .api import Api
from .model_picker import ModelPicker
from .store import Store
from .widgets import button, chip, label

MEMORY_KINDS = ["preference", "role", "voice", "edge_case", "fact", "work_summary"]


def lines(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines() if l.strip()]


class NewBotDialog(QDialog):
    created = Signal(dict, str)      # bot, suggested first message

    def __init__(self, api: Api, store: Store, parent=None):
        super().__init__(parent)
        self.api, self.store = api, store
        self.setWindowTitle("New Bot")
        self.resize(820, 560)
        root = QHBoxLayout(self)
        left = QVBoxLayout()
        left.addWidget(label("Start from a role", h2=True))
        self.list = QListWidget()
        self.list.addItem(QListWidgetItem("✨  Blank Bot"))
        self.list.item(0).setData(Qt.ItemDataRole.UserRole, None)
        for t in store.templates:
            it = QListWidgetItem(f"{t['emoji']}  {t['name']}")
            it.setData(Qt.ItemDataRole.UserRole, t)
            self.list.addItem(it)
        self.list.currentRowChanged.connect(self._picked)
        left.addWidget(self.list, 1)
        left.addWidget(button("Create a starter team (Chief of Staff + specialists)", on=self.create_team))
        root.addLayout(left, 2)

        right = QVBoxLayout()
        right.addWidget(label("Setup is a message. Name the Bot, say what its job is, and grant access when it asks.", muted=True))
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Inbox")
        self.emoji = QLineEdit("🤖")
        self.emoji.setMaximumWidth(60)
        row = QHBoxLayout()
        row.addWidget(self.emoji)
        row.addWidget(self.name, 1)
        form.addRow("Name", row)
        self.job = QPlainTextEdit()
        self.job.setPlaceholderText("Describe the job in a sentence or two. You can refine it later in chat.")
        self.job.setFixedHeight(90)
        form.addRow("Job", self.job)
        right.addLayout(form)
        self.example_label = label("", muted=True)
        right.addWidget(self.example_label)
        right.addStretch(1)
        self.err = label("")
        self.err.setStyleSheet(f"color: {theme.palette()['bad']};")
        right.addWidget(self.err)
        bb = QHBoxLayout()
        bb.addStretch(1)
        bb.addWidget(button("Cancel", on=self.reject))
        self.btn = button("Create Bot", primary=True, on=self.create)
        bb.addWidget(self.btn)
        right.addLayout(bb)
        root.addLayout(right, 3)
        self.list.setCurrentRow(0)
        self.example = ""

    def _picked(self, row: int) -> None:
        t = self.list.item(row).data(Qt.ItemDataRole.UserRole) if row >= 0 else None
        if t:
            self.name.setText(t["name"])
            self.emoji.setText(t["emoji"])
            self.job.setPlainText(t["job"])
            self.example = t["example"]
            self.example_label.setText("A good first handoff:  “" + t["example"] + "”")
        else:
            self.name.clear()
            self.emoji.setText("🤖")
            self.job.clear()
            self.example = ""
            self.example_label.setText("")

    def create(self) -> None:
        name = self.name.text().strip()
        if not name:
            self.err.setText("Give the Bot a name.")
            return
        t = self.list.currentItem().data(Qt.ItemDataRole.UserRole)
        self.btn.setEnabled(False)

        def ok(bot: dict) -> None:
            patch = {"job": self.job.toPlainText().strip(), "emoji": self.emoji.text().strip() or "🤖"}
            self.api.put(f"/api/bots/{bot['id']}", patch, lambda b: self._done(b))

        def fail(e: str) -> None:
            self.err.setText(e)
            self.btn.setEnabled(True)

        if t:
            self.api.post("/api/bots/from_template", {"template": t["id"], "name": name}, ok, fail)
        else:
            self.api.post("/api/bots", {"name": name, "job": self.job.toPlainText().strip(), "emoji": self.emoji.text().strip() or "🤖"}, ok, fail)

    def _done(self, bot: dict) -> None:
        self.store.refresh_all()
        self.created.emit(bot, self.example)
        self.accept()

    def create_team(self) -> None:
        self.btn.setEnabled(False)

        def ok(d: dict) -> None:
            self.store.refresh_all()
            first = d["bots"][0] if d.get("bots") else None
            if first:
                self.created.emit(first, "Set up the team for this week: triage my inbox daily, track receipts, and keep a running list of open recruiting candidates. Tell me what you need access to.")
            self.accept()
        self.api.post("/api/teams", {}, ok, lambda e: (self.err.setText(e), self.btn.setEnabled(True)))


class GroupDialog(QDialog):
    def __init__(self, api: Api, store: Store, group: dict | None = None, parent=None):
        super().__init__(parent)
        self.api, self.store, self.group = api, store, group
        self.setWindowTitle("Group chat" if not group else f"Edit {group['name']}")
        self.resize(460, 520)
        v = QVBoxLayout(self)
        v.addWidget(label("Bots in a group coordinate on their own: they pass work, assign ownership and only pull you in for judgment calls.", muted=True))
        form = QFormLayout()
        self.name = QLineEdit(group["name"] if group else "")
        self.goal = QPlainTextEdit(group["goal"] if group else "")
        self.goal.setFixedHeight(70)
        form.addRow("Name", self.name)
        form.addRow("Goal", self.goal)
        v.addLayout(form)
        v.addWidget(label("Members"))
        self.members = QListWidget()
        for b in store.bots:
            it = QListWidgetItem(f"{b['emoji']}  {b['name']}")
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if group and b["id"] in group["members"] else Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, b["id"])
            self.members.addItem(it)
        v.addWidget(self.members, 1)
        self.lead = QComboBox()
        self.lead.addItem("No lead (messages go to everyone)", "")
        for b in store.bots:
            self.lead.addItem(f"Lead: {b['name']} (chief of staff pattern)", b["id"])
        if group and group.get("lead_bot"):
            self.lead.setCurrentIndex(max(0, self.lead.findData(group["lead_bot"])))
        v.addWidget(self.lead)
        self.err = label("")
        v.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def save(self) -> None:
        ids = [self.members.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.members.count()) if self.members.item(i).checkState() == Qt.CheckState.Checked]
        if not self.name.text().strip() or not ids:
            self.err.setText("Name the group and pick at least one Bot.")
            return
        body = {"name": self.name.text().strip(), "goal": self.goal.toPlainText().strip(), "members": ids, "lead": self.lead.currentData(), "lead_bot": self.lead.currentData()}
        done = lambda g: (self.store.refresh_groups(), self.accept())
        fail = lambda e: self.err.setText(e)
        if self.group:
            self.api.put(f"/api/groups/{self.group['id']}", body, done, fail)
        else:
            self.api.post("/api/groups", body, done, fail)


class BotEditor(QDialog):
    deleted = Signal(str)

    def __init__(self, api: Api, store: Store, bot_id: str, parent=None):
        super().__init__(parent)
        self.api, self.store, self.bot_id = api, store, bot_id
        self.bot = store.bot(bot_id) or {}
        self.setWindowTitle(f"Edit {self.bot.get('name', 'Bot')}")
        self.resize(760, 640)
        v = QVBoxLayout(self)
        tabs = QTabWidget()
        v.addWidget(tabs, 1)

        # profile
        p = QWidget()
        pf = QFormLayout(p)
        self.name = QLineEdit(self.bot.get("name", ""))
        self.emoji = QLineEdit(self.bot.get("emoji", ""))
        self.emoji.setMaximumWidth(60)
        r = QHBoxLayout()
        r.addWidget(self.emoji)
        r.addWidget(self.name, 1)
        pf.addRow("Name", r)
        self.job = QPlainTextEdit(self.bot.get("job", ""))
        self.job.setFixedHeight(80)
        pf.addRow("Job", self.job)
        self.instr = QPlainTextEdit(self.bot.get("instructions", ""))
        pf.addRow("Standing instructions", self.instr)
        tabs.addTab(p, "Profile")

        # model
        m = QWidget()
        mf = QFormLayout(m)
        self.prov = QComboBox()
        self.prov.addItem("Default provider", "")
        for pr in store.profiles:
            self.prov.addItem(pr["label"] + ("" if pr.get("key_set") or not pr.get("needs_key", True) else "  (no key yet)"), pr["id"])
        self.prov.setCurrentIndex(max(0, self.prov.findData(self.bot.get("profile", ""))))
        mf.addRow("Model provider", self.prov)
        self.picker = ModelPicker(api, "Provider default")
        self.prov.currentIndexChanged.connect(lambda _i: self._provider_changed())
        mf.addRow("Model", self.picker)
        self.steps = QSpinBox()
        self.steps.setRange(1, 500)
        self.steps.setValue(int(self.bot.get("step_limit", 40)))
        mf.addRow("Step limit per task", self.steps)
        self.budget = QSpinBox()
        self.budget.setRange(0, 1_000_000_000)
        self.budget.setSingleStep(10_000)
        self.budget.setSpecialValueText("No limit")
        self.budget.setSuffix(" tokens a day")
        self.budget.setValue(int(self.bot.get("daily_token_limit", 0) or 0))
        self.budget.setToolTip("When this Bot has used this many tokens since midnight it stops, and works again the next day. Also: /budget 50k in its chat.")
        mf.addRow("Daily budget", self.budget)
        self.fb_prov = QComboBox()
        self.fb_prov.addItem("Use the app-wide backup (Settings > Models)", "")
        for pr in store.profiles:
            self.fb_prov.addItem(pr["label"], pr["id"])
        self.fb_prov.setCurrentIndex(max(0, self.fb_prov.findData(self.bot.get("fallback_profile", ""))))
        self.fb_model = QLineEdit(self.bot.get("fallback_model", ""))
        self.fb_model.setPlaceholderText("Provider default")
        self.fb_prov.setToolTip("If this Bot's model is rate limited, down, unreachable or rejects its key, it switches to this one for the rest of the task.")
        mf.addRow("Backup provider", self.fb_prov)
        mf.addRow("Backup model", self.fb_model)
        self.proactive = QComboBox()
        self.proactive.addItem("Off: only works when asked", "off")
        self.proactive.addItem("Suggest: notices work and proposes it", "suggest")
        self.proactive.addItem("Act: picks up low-risk work itself", "act")
        self.proactive.setCurrentIndex(max(0, self.proactive.findData(self.bot.get("proactive", "off"))))
        mf.addRow("Proactive", self.proactive)
        tabs.addTab(m, "Model")
        self._provider_changed(self.bot.get("model", ""))

        # access and safety
        a = QWidget()
        av = QVBoxLayout(a)
        af = QFormLayout()
        self.approval = QComboBox()
        self.approval.addItem("Ask me for consequential actions", "ask")
        self.approval.addItem("Auto Review: a reviewer model approves low-risk actions", "auto_review")
        self.approval.setCurrentIndex(max(0, self.approval.findData(self.bot.get("approval_mode", "ask"))))
        af.addRow("Approvals", self.approval)
        adm = (store.status.get("admin") or {}).get("policy", {}).get("approval_defaults", {})
        if adm.get("locked"):
            self.approval.setEnabled(False)
            af.addRow("", label("Locked by your administrator.", muted=True))
        self.net = QComboBox()
        self.net.addItem("Use the global network policy", "inherit")
        self.net.addItem("Open: block only the deny list", "open")
        self.net.addItem("Allow list only", "allowlist")
        self.net.setCurrentIndex(max(0, self.net.findData(self.bot.get("net_mode", "inherit"))))
        af.addRow("Network", self.net)
        av.addLayout(af)
        nl = QHBoxLayout()
        self.allow = QPlainTextEdit("\n".join(self.bot.get("net_allow", [])))
        self.allow.setPlaceholderText("Allowed domains, one per line\n*.example.com")
        self.deny = QPlainTextEdit("\n".join(self.bot.get("net_deny", [])))
        self.deny.setPlaceholderText("Blocked domains, one per line")
        nl.addWidget(self.allow)
        nl.addWidget(self.deny)
        av.addLayout(nl)
        av.addWidget(label("Connectors this Bot may use (Bots also request access themselves when they need it):"))
        self.grants = QListWidget()
        self.grants.setMaximumHeight(130)
        av.addWidget(self.grants)
        av.addWidget(label("Standing approval rules created with “Approve & always allow”:"))
        self.rules = QListWidget()
        self.rules.setMaximumHeight(80)
        av.addWidget(self.rules)
        av.addWidget(button("Remove selected rule", on=self.remove_rule))
        tabs.addTab(a, "Access && safety")

        # memory
        mem = QWidget()
        mv = QVBoxLayout(mem)
        mv.addWidget(label("What this Bot remembers across sessions. It is separate from every other Bot's memory. Edit or delete anything that is wrong.", muted=True))
        self.mem = QTableWidget(0, 4)
        self.mem.setHorizontalHeaderLabels(["Kind", "Memory", "Pinned", "Re-check"])
        self.mem.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.mem.verticalHeader().hide()
        self.mem.setWordWrap(True)
        mv.addWidget(self.mem, 1)
        mb = QHBoxLayout()
        self.mem_kind = QComboBox()
        self.mem_kind.addItems(MEMORY_KINDS)
        self.mem_text = QLineEdit()
        self.mem_text.setPlaceholderText("Add something the Bot should always remember…")
        mb.addWidget(self.mem_kind)
        mb.addWidget(self.mem_text, 1)
        mb.addWidget(button("Add", on=self.add_memory))
        mb.addWidget(button("Delete selected", danger=True, on=self.delete_memory))
        mv.addLayout(mb)
        tabs.addTab(mem, "Memory")

        # share
        s = QWidget()
        sv = QVBoxLayout(s)
        sv.addWidget(label("Share this Bot as a package: role, skills and routines. Never secrets, credentials or conversations. A teammate can import it and run a copy.", muted=True))
        self.inc_mem = QCheckBox("Include stable preferences and role memory")
        sv.addWidget(self.inc_mem)
        sv.addWidget(button("Export package (.gbbot)…", on=self.export))
        sv.addStretch(1)
        sv.addWidget(label("Danger zone", h2=True))
        row = QHBoxLayout()
        row.addWidget(button("Archive Bot", on=self.archive))
        row.addWidget(button("Delete Bot and its threads…", danger=True, on=self.delete))
        row.addStretch(1)
        sv.addLayout(row)
        tabs.addTab(s, "Share && manage")

        self.err = label("")
        self.err.setStyleSheet(f"color: {theme.palette()['bad']};")
        v.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.load_extras()

    def load_extras(self) -> None:
        def plugins(d: dict) -> None:
            for pl in d["plugins"]:
                it = QListWidgetItem(f"{pl['name']}" + ("" if pl["configured"] else "  (not connected yet)"))
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setData(Qt.ItemDataRole.UserRole, f"plugin:{pl['id']}")
                it.setCheckState(Qt.CheckState.Checked if f"plugin:{pl['id']}" in self.bot.get("grants", []) else Qt.CheckState.Unchecked)
                self.grants.addItem(it)
            self.api.get("/api/mcp", mcp)

        def mcp(servers: list) -> None:
            for sv in servers:
                it = QListWidgetItem(f"MCP: {sv['name']}  ({sv['status']})")
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setData(Qt.ItemDataRole.UserRole, f"mcp:{sv['name']}")
                it.setCheckState(Qt.CheckState.Checked if f"mcp:{sv['name']}" in self.bot.get("grants", []) else Qt.CheckState.Unchecked)
                self.grants.addItem(it)
        self.api.get("/api/plugins", plugins)
        self.api.get(f"/api/bots/{self.bot_id}", lambda b: self._rules(b.get("rules", [])))
        self.reload_memory()

    def _rules(self, rules: list) -> None:
        self.rules.clear()
        for r in rules:
            it = QListWidgetItem(f"{r['category']}  ·  {r['pattern']}")
            it.setData(Qt.ItemDataRole.UserRole, r["id"])
            self.rules.addItem(it)

    def remove_rule(self) -> None:
        it = self.rules.currentItem()
        if it:
            self.api.delete(f"/api/rules/{it.data(Qt.ItemDataRole.UserRole)}", lambda _: self.rules.takeItem(self.rules.row(it)))

    def reload_memory(self) -> None:
        def ok(rows: list) -> None:
            self.mem.blockSignals(True)
            self.mem.setRowCount(0)
            for r in rows:
                i = self.mem.rowCount()
                self.mem.insertRow(i)
                k = QTableWidgetItem(r["kind"])
                k.setData(Qt.ItemDataRole.UserRole, r["id"])
                k.setFlags(k.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.mem.setItem(i, 0, k)
                self.mem.setItem(i, 1, QTableWidgetItem(r["text"]))
                pin = QTableWidgetItem("★" if r["pinned"] else "")
                pin.setFlags(pin.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.mem.setItem(i, 2, pin)
                rc = QTableWidgetItem("✓" if r["verify"] else "")
                rc.setFlags(rc.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.mem.setItem(i, 3, rc)
            self.mem.resizeRowsToContents()
            self.mem.blockSignals(False)
        self.api.get(f"/api/bots/{self.bot_id}/memory", ok)
        if not getattr(self, "_mem_hooked", False):   # connect once; disconnect() on an unconnected signal logs a warning
            self.mem.cellChanged.connect(self._mem_edited)
            self._mem_hooked = True

    def _mem_edited(self, row: int, col: int) -> None:
        if col != 1:
            return
        mid = self.mem.item(row, 0).data(Qt.ItemDataRole.UserRole)
        self.api.put(f"/api/bots/{self.bot_id}/memory/{mid}", {"text": self.mem.item(row, 1).text()})

    def add_memory(self) -> None:
        t = self.mem_text.text().strip()
        if t:
            self.api.post(f"/api/bots/{self.bot_id}/memory", {"kind": self.mem_kind.currentText(), "text": t, "pinned": True}, lambda _: (self.mem_text.clear(), self.reload_memory()))

    def delete_memory(self) -> None:
        r = self.mem.currentRow()
        if r >= 0:
            self.api.delete(f"/api/bots/{self.bot_id}/memory/{self.mem.item(r, 0).data(Qt.ItemDataRole.UserRole)}", lambda _: self.reload_memory())

    def _provider_changed(self, current: str | None = None) -> None:
        pid = self.prov.currentData() or self.store.settings.get("default_profile", "anthropic")
        p = self.store.provider(pid) or {}
        self.picker.set_provider(pid, bool(p.get("key_set") or not p.get("needs_key", True)), current if current is not None else "")

    def export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export Bot package", f"{self.bot['name']}.gbbot", "Bot package (*.gbbot)")
        if not path:
            return

        def ok(data: bytes) -> None:
            with open(path, "wb") as f:
                f.write(data)
            QMessageBox.information(self, "Exported", f"Saved {path}\n\nIt contains the role, skills and routines. No secrets or conversations.")
        self.api.request("GET", f"/api/bots/{self.bot_id}/export", ok, lambda e: QMessageBox.warning(self, "Export failed", e), params={"memory": self.inc_mem.isChecked()}, raw=True)

    def archive(self) -> None:
        self.api.put(f"/api/bots/{self.bot_id}", {"archived": True}, lambda _: (self.store.refresh_all(), self.deleted.emit(self.bot_id), self.accept()))

    def delete(self) -> None:
        if QMessageBox.question(self, "Delete Bot", f"Delete {self.bot['name']}, its threads, memory and routines? This cannot be undone.") != QMessageBox.StandardButton.Yes:
            return
        self.api.delete(f"/api/bots/{self.bot_id}", lambda _: (self.store.refresh_all(), self.deleted.emit(self.bot_id), self.accept()))

    def save(self) -> None:
        grants = [self.grants.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.grants.count()) if self.grants.item(i).checkState() == Qt.CheckState.Checked]
        body = {"name": self.name.text().strip(), "emoji": self.emoji.text().strip() or "🤖", "job": self.job.toPlainText().strip(),
                "instructions": self.instr.toPlainText().strip(), "profile": self.prov.currentData(), "model": self.picker.text(),
                "step_limit": self.steps.value(), "daily_token_limit": self.budget.value(), "fallback_profile": self.fb_prov.currentData() or "",
                "fallback_model": self.fb_model.text().strip(), "proactive": self.proactive.currentData(), "net_mode": self.net.currentData(),
                "net_allow": lines(self.allow.toPlainText()), "net_deny": lines(self.deny.toPlainText()), "grants": grants}
        if self.approval.isEnabled():
            body["approval_mode"] = self.approval.currentData()
        self.api.put(f"/api/bots/{self.bot_id}", body, lambda _: (self.store.refresh_bots(), self.accept()), lambda e: self.err.setText(e))
