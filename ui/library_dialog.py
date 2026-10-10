"""Browse, preview and install skills from the library; import and export skill files."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QSplitter, QVBoxLayout, QWidget)

from . import theme
from .api import Api
from .pages_files import set_row, styled_list
from .widgets import button, label

STATE = {"available": ("new", "muted"), "installed": ("installed", "ok"), "update": ("update", "warn")}


class LibraryDialog(QDialog):
    installed = Signal(str)

    def __init__(self, api: Api, parent=None):
        super().__init__(parent)
        self.api = api
        self.rows: list[dict] = []
        self.current = ""
        self.setWindowTitle("Skill library")
        self.resize(980, 640)
        v = QVBoxLayout(self)
        v.addWidget(label("Ready-made skills. Installing one adds it as a draft: read it, change it, then activate it. Nothing runs until you do.", muted=True))
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search skills")
        self.search.textChanged.connect(self.render)
        self.tag = QComboBox()
        self.tag.addItem("All topics", "")
        self.tag.currentIndexChanged.connect(self.render)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.tag)
        self.btn_refresh = button("Check for new skills", on=self.refresh)
        self.btn_refresh.setToolTip("Contacts the OpenGrokBot website once to look for new or updated skills.")
        bar.addWidget(self.btn_refresh)
        v.addLayout(bar)
        split = QSplitter()
        v.addWidget(split, 1)
        self.list = styled_list(QListWidget())
        self.list.currentRowChanged.connect(self._pick)
        split.addWidget(self.list)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(8, 0, 0, 0)
        self.title = label("", h2=True, wrap=False)
        rv.addWidget(self.title)
        self.flags = label("", muted=True)
        rv.addWidget(self.flags)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(theme.mono())
        self.view.setPlaceholderText("Pick a skill to read it before installing.")
        rv.addWidget(self.view, 1)
        split.addWidget(right)
        split.setSizes([360, 600])
        row = QHBoxLayout()
        self.btn_install = button("Install as draft", primary=True, on=self.install)
        self.btn_install.setEnabled(False)
        row.addWidget(self.btn_install)
        row.addWidget(button("Import file…", on=self.import_file))
        row.addWidget(button("Import link…", on=self.import_url))
        row.addWidget(button("Export mine…", on=self.export))
        row.addStretch(1)
        self.msg = label("", muted=True)
        row.addWidget(self.msg)
        row.addWidget(button("Close", on=self.accept))
        v.addLayout(row)
        self.load()

    def load(self) -> None:
        def ok(rows: list) -> None:
            self.rows = rows
            tags = sorted({t.strip() for r in rows for t in r.get("tags", "").split(",") if t.strip()})
            cur = self.tag.currentData()
            self.tag.blockSignals(True)
            self.tag.clear()
            self.tag.addItem("All topics", "")
            for t in tags:
                self.tag.addItem(t, t)
            i = self.tag.findData(cur)
            self.tag.setCurrentIndex(max(i, 0))
            self.tag.blockSignals(False)
            self.render()
        self.api.get("/api/library", ok)

    def render(self) -> None:
        q, tag = self.search.text().strip().lower(), self.tag.currentData() or ""
        self.list.blockSignals(True)
        self.list.clear()
        for r in self.rows:
            hay = f"{r['name']} {r['description']} {r['tags']}".lower()
            if q and q not in hay:
                continue
            if tag and tag not in [t.strip() for t in r["tags"].split(",")]:
                continue
            it = QListWidgetItem()
            st = STATE.get(r["state"], ("", "muted"))
            set_row(it, r["name"], r["description"] or "No description.", "skills", "accent" if r["state"] != "available" else "muted", st)
            it.setData(Qt.ItemDataRole.UserRole, r["name"])
            self.list.addItem(it)
        self.list.blockSignals(False)
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self._pick(-1)

    def _pick(self, row: int) -> None:
        if row < 0 or not self.list.item(row):
            self.current = ""
            self.view.clear()
            self.title.setText("")
            self.flags.setText("No skills match." if self.rows else "")
            self.btn_install.setEnabled(False)
            return
        name = self.list.item(row).data(Qt.ItemDataRole.UserRole)
        self.current = name

        def ok(s: dict) -> None:
            if name != self.current:
                return
            self.view.setPlainText(s["raw"])
            self.title.setText(f"{s['name']}  ·  v{s['version']}")
            self.flags.setText("Look closely before activating: " + "; ".join(s["flags"]) + "." if s["flags"] else "Nothing unusual found.")
            self.btn_install.setEnabled(True)
            self.btn_install.setText({"installed": "Reinstall as draft", "update": "Update (saves as draft)"}.get(s["state"], "Install as draft"))
        self.api.get(f"/api/library/{name}", ok, lambda e: (self.view.clear(), self.flags.setText(e), self.btn_install.setEnabled(False)))

    def install(self) -> None:
        if not self.current:
            return
        row = next((r for r in self.rows if r["name"] == self.current), {})
        if row.get("state") in ("installed", "update") and QMessageBox.question(
                self, "Replace your copy?", "You already have this skill. Installing replaces your copy (including your edits) with the library version, as a draft.") != QMessageBox.StandardButton.Yes:
            return
        self.api.post(f"/api/library/{self.current}/install", {}, lambda s: (self.msg.setText(f"Installed {s['name']} as a draft."), self.installed.emit(s["name"]), self.load()),
                      lambda e: self.msg.setText(e))

    def refresh(self) -> None:
        self.btn_refresh.setEnabled(False)
        self.msg.setText("Checking…")
        self.api.post("/api/library/refresh", {}, lambda r: (self.btn_refresh.setEnabled(True), self.msg.setText(f"{r['count']} skills available."), self.load()),
                      lambda e: (self.btn_refresh.setEnabled(True), self.msg.setText(e)))

    def _imported(self, rows: list) -> None:
        self.msg.setText(f"Imported {len(rows)} skill{'s' if len(rows) != 1 else ''} as draft{'s' if len(rows) != 1 else ''}.")
        if rows:
            self.installed.emit(rows[0]["name"])
        self.load()

    def import_file(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Import skill", "", "Skills (*.md *.zip)")
        if p:
            self.api.post("/api/skills-import", {"path": p}, self._imported, lambda e: QMessageBox.warning(self, "Import failed", e))

    def import_url(self) -> None:
        url, ok = QInputDialog.getText(self, "Import link", "Link to a skill (.md, https only).\nOnly import skills from people you trust: a skill is a set of instructions your Bot will follow.")
        if ok and url.strip():
            self.api.post("/api/skills-import", {"url": url.strip()}, self._imported, lambda e: QMessageBox.warning(self, "Import failed", e))

    def export(self) -> None:
        p, _ = QFileDialog.getSaveFileName(self, "Export skills", "opengrokbot-skills.zip", "Zip (*.zip)")
        if p:
            self.api.post("/api/skills-export", {"path": p}, lambda r: self.msg.setText(f"Exported {r['count']} skills."), lambda e: self.msg.setText(e))
