"""Skills (reviewable markdown) and what the Bots learned by watching you (recordings)."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog, QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QSplitter, QVBoxLayout, QWidget)

from . import theme
from .api import Api
from .pages_inbox import fmt_time
from .store import Store
from .widgets import button, chip, label, PageHeader, page_layout

NEW_SKILL = """---
name: {name}
description: One line that tells a Bot when to use this skill
status: draft
bot:
tags:
---
# {title}

## When to use

## Inputs

## Steps
1.

## Checks

## Needs approval
Steps that send, submit, purchase, delete or log in.
"""


class TestDialog(QDialog):
    def __init__(self, store: Store, skill: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Test skill: {skill}")
        v = QVBoxLayout(self)
        v.addWidget(label("Run the skill once as a test in a new thread. In a dry run the Bot may look and read, but consequential actions are refused automatically.", muted=True))
        self.bot = QComboBox()
        for b in store.bots:
            self.bot.addItem(f"{b['emoji']} {b['name']}", b["id"])
        v.addWidget(self.bot)
        self.dry = QCheckBox("Dry run (recommended)")
        self.dry.setChecked(True)
        v.addWidget(self.dry)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)


class SkillsPage(QWidget):
    openThread = Signal(str, str)
    followAlong = Signal(str, bool)

    def __init__(self, api: Api, store: Store):
        super().__init__()
        self.api, self.store = api, store
        self.current = ""
        self.skills: list[dict] = []
        self.recs: list[dict] = []
        root = page_layout(self, PageHeader("Skills", "Skills are plain markdown files your Bots follow. Review them, correct them, test them, then activate. Draft skills are not offered to Bots."))
        split = QSplitter()
        root.addWidget(split, 1)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 8, 0)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._pick)
        lv.addWidget(self.list, 2)
        row = QHBoxLayout()
        row.addWidget(button("New skill", on=self.new))
        row.addWidget(button("Delete", danger=True, on=self.delete))
        lv.addLayout(row)
        lv.addWidget(label("Learned by demonstration", h2=True))
        self.rec_list = QListWidget()
        self.rec_list.setMaximumHeight(160)
        lv.addWidget(self.rec_list, 1)
        r2 = QHBoxLayout()
        r2.addWidget(button("Follow along…", on=self.follow))
        r2.addWidget(button("Draft skill", on=self.draft))
        lv.addLayout(r2)
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(8, 0, 0, 0)
        top = QHBoxLayout()
        self.name_label = label("", h2=True, wrap=False)
        self.status_chip = chip("")
        top.addWidget(self.name_label, 1)
        top.addWidget(self.status_chip)
        rv.addLayout(top)
        self.editor = QPlainTextEdit()
        self.editor.setFont(theme.mono())
        self.editor.setPlaceholderText("Select a skill to review and edit it.")
        rv.addWidget(self.editor, 1)
        bar = QHBoxLayout()
        bar.addWidget(button("Save", primary=True, on=self.save))
        self.btn_toggle = button("Activate", on=self.toggle)
        bar.addWidget(self.btn_toggle)
        bar.addWidget(button("Test…", on=self.test))
        bar.addStretch(1)
        self.msg = label("", muted=True)
        bar.addWidget(self.msg)
        rv.addLayout(bar)
        split.addWidget(right)
        split.setSizes([320, 700])
        store.event.connect(lambda ev: ev.get("type") == "recording" and self.isVisible() and self.load())

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.load()

    def load(self, select: str | None = None) -> None:
        def ok(rows: list) -> None:
            self.skills = rows
            self.list.blockSignals(True)
            self.list.clear()
            for s in rows:
                it = QListWidgetItem(("● " if s["status"] == "active" else "○ ") + s["name"] + (f"   [{s['bot']}]" if s["bot"] else ""))
                it.setToolTip(s["description"])
                it.setData(Qt.ItemDataRole.UserRole, s["name"])
                self.list.addItem(it)
            self.list.blockSignals(False)
            target = select or self.current
            for i, s in enumerate(rows):
                if s["name"] == target:
                    self.list.setCurrentRow(i)
                    break
            else:
                if rows and not target:
                    self.list.setCurrentRow(0)
        self.api.get("/api/skills", ok)

        def recs(rows: list) -> None:
            self.recs = rows
            self.rec_list.clear()
            for r in rows:
                it = QListWidgetItem(f"{r['name']}  ·  {self.store.bot_name(r['bot_id'])}  ·  {len(r['steps'])} steps  ·  {r['status']}")
                it.setData(Qt.ItemDataRole.UserRole, r["id"])
                self.rec_list.addItem(it)
        self.api.get("/api/recordings", recs)

    def _pick(self, row: int) -> None:
        if row < 0:
            return
        name = self.list.item(row).data(Qt.ItemDataRole.UserRole)
        self.current = name

        def ok(s: dict) -> None:
            self.editor.setPlainText(s["raw"])
            self.name_label.setText(s["name"])
            self._status(s["status"])
            self.msg.setText("")
        self.api.get(f"/api/skills/{name}", ok)

    def _status(self, status: str) -> None:
        self.status_chip.setText("active" if status == "active" else "draft")
        self.status_chip.setProperty("chip", "ok" if status == "active" else "warn")
        self.status_chip.style().unpolish(self.status_chip)
        self.status_chip.style().polish(self.status_chip)
        self.btn_toggle.setText("Make draft" if status == "active" else "Activate")

    def save(self) -> None:
        if not self.current:
            return
        self.api.put(f"/api/skills/{self.current}", {"raw": self.editor.toPlainText()}, lambda s: (self.msg.setText("Saved."), self.load(s["name"])), lambda e: self.msg.setText(e))

    def toggle(self) -> None:
        if not self.current:
            return
        want = "draft" if self.status_chip.text() == "active" else "active"
        self.api.post(f"/api/skills/{self.current}/status", {"status": want}, lambda s: (self._status(s["status"]), self.editor.setPlainText(s["raw"]), self.load(s["name"])))

    def new(self) -> None:
        n, ok = QInputDialog.getText(self, "New skill", "Name (lowercase, dashes):")
        if ok and n.strip():
            slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in n.strip().lower())
            self.api.put(f"/api/skills/{slug}", {"raw": NEW_SKILL.format(name=slug, title=n.strip())}, lambda s: self.load(s["name"]))

    def delete(self) -> None:
        if self.current and QMessageBox.question(self, "Delete skill", f"Delete {self.current}?") == QMessageBox.StandardButton.Yes:
            self.api.delete(f"/api/skills/{self.current}", lambda _: (setattr(self, "current", ""), self.editor.clear(), self.load()))

    def test(self) -> None:
        if not self.current or not self.store.bots:
            return
        d = TestDialog(self.store, self.current, self)
        if d.exec():
            bid = d.bot.currentData()
            self.api.post(f"/api/skills/{self.current}/test", {"bot_id": bid, "dry_run": d.dry.isChecked()}, lambda th: self.openThread.emit(th["id"], bid),
                          lambda e: QMessageBox.warning(self, "Cannot test", e))

    def follow(self) -> None:
        if not self.store.bots:
            return
        names = [f"{b['emoji']} {b['name']}" for b in self.store.bots]
        n, ok = QInputDialog.getItem(self, "Follow along", "Which Bot should watch?", names, 0, False)
        if ok:
            self.followAlong.emit(self.store.bots[names.index(n)]["id"], True)

    def draft(self) -> None:
        it = self.rec_list.currentItem()
        if not it:
            QMessageBox.information(self, "Draft skill", "Pick a recording first.")
            return
        rid = it.data(Qt.ItemDataRole.UserRole)
        self.msg.setText("Drafting…")
        self.api.post(f"/api/recordings/{rid}/draft_skill", {}, lambda s: (self.msg.setText(""), self.load(s["name"])), lambda e: (self.msg.setText(""), QMessageBox.warning(self, "Draft failed", e)))
