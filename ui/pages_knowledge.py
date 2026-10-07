"""Knowledge: documents your Bots can search. Add notes, text and Word files or whole workspace folders."""
from __future__ import annotations

import base64
import os
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QSplitter, QStackedWidget, QVBoxLayout, QWidget)

from . import theme
from .api import Api
from .pages_files import ago, human_size
from .pages_inbox import fill_row, make_table
from .store import Store
from .widgets import PageHeader, button, label, page_layout

MAX_UPLOAD = 20_000_000
FILE_FILTER = "Documents (*.txt *.md *.markdown *.csv *.tsv *.json *.log *.html *.htm *.xml *.yml *.yaml *.docx *.rst *.py *.js);;All files (*)"


class NoteDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add a note")
        self.resize(560, 420)
        v = QVBoxLayout(self)
        v.addWidget(label("Paste or write anything you want your Bots to be able to look up: a policy, a checklist, how you like things done.", muted=True))
        f = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Travel policy")
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("The text…")
        f.addRow("Name", self.name)
        f.addRow("Text", self.text)
        v.addLayout(f, 1)
        self.err = label("")
        v.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._go)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _go(self) -> None:
        if not self.name.text().strip() or not self.text.toPlainText().strip():
            self.err.setText("Give the note a name and some text.")
            return
        self.accept()


class KnowledgePage(QWidget):
    def __init__(self, api: Api, store: Store):
        super().__init__()
        self.api, self.store = api, store
        self.sources: list[dict] = []
        self.hits: list[dict] = []
        self.current: dict | None = None
        self.scope = QComboBox()
        self.scope.setToolTip("Who can find what you add next")
        self.add_note_btn = button("Add note…", icon="plus", on=self.add_note)
        self.add_files_btn = button("Add files…", icon="upload", on=self.add_files)
        self.add_folder_btn = button("Add workspace folder…", icon="folder", on=self.add_folder)
        v = page_layout(self, PageHeader("Knowledge", "Documents your Bots can search and quote. Nothing leaves your PC: it is indexed locally, with no model call.",
                                         [self.add_note_btn, self.add_files_btn, self.add_folder_btn]))
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search everything you added…")
        self.search.setProperty("search", True)
        self.search.textChanged.connect(lambda _t: self._timer.start(250))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._run_search)
        bar.addWidget(self.search, 1)
        bar.addWidget(label("Add to", muted=True, wrap=False))
        bar.addWidget(self.scope)
        v.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(8)
        self.stack = QStackedWidget()
        self.table = make_table(["Name", "Shared with", "Passages", "Size", "Updated"], 0)
        self.table.itemSelectionChanged.connect(self._source_selected)
        self.results = QListWidget()
        self.results.setWordWrap(True)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.results.currentRowChanged.connect(self._hit_selected)
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.results)
        lv.addWidget(self.stack, 1)
        self.summary = label("", faint=True, wrap=False)
        lv.addWidget(self.summary)
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(12, 0, 0, 0)
        rv.setSpacing(8)
        self.p_title = label("", h2=True, wrap=False)
        self.p_meta = label("", faint=True, wrap=False)
        rv.addWidget(self.p_title)
        rv.addWidget(self.p_meta)
        self.p_text = QPlainTextEdit()
        self.p_text.setReadOnly(True)
        self.p_text.setPlaceholderText("Select a document to read it, or search for something.")
        rv.addWidget(self.p_text, 1)
        actions = QHBoxLayout()
        self.refresh_btn = button("Refresh from workspace", icon="refresh", on=self.refresh_files, tip="Re-read workspace files that changed")
        self.del_btn = button("Remove", danger=True, icon="trash", on=self.remove_current)
        actions.addWidget(self.refresh_btn)
        actions.addWidget(self.del_btn)
        actions.addStretch(1)
        rv.addLayout(actions)
        split.addWidget(right)
        split.setSizes([560, 520])
        v.addWidget(split, 1)
        self.del_btn.setEnabled(False)

    # ------------------------------------------------------------------ data
    def showEvent(self, e) -> None:
        super().showEvent(e)
        cur = self.scope.currentData() or ""
        self.scope.blockSignals(True)
        self.scope.clear()
        self.scope.addItem("All Bots", "")
        for b in self.store.bots:
            self.scope.addItem(f"{b.get('emoji') or '🤖'}  {b['name']} only", b["id"])
        self.scope.setCurrentIndex(max(0, self.scope.findData(cur)))
        self.scope.blockSignals(False)
        self.reload()

    def reload(self) -> None:
        self.api.get("/api/knowledge", self._got, self._error)

    def _error(self, msg: str) -> None:
        self.summary.setText(msg)

    def _got(self, d: dict) -> None:
        self.sources = d["sources"]
        st = d["stats"]
        keep = self.current["id"] if self.current else ""
        self.table.setRowCount(0)
        for s in self.sources:
            fill_row(self.table, [s["name"], s["bot_name"] and f"{s['bot_name']} only" or "All Bots", s["chunks"], human_size(s["size"]), ago(s["updated_at"])], s)
        self.summary.setText(f"{st['sources']} document{'s' if st['sources'] != 1 else ''} · {st['passages']:,} passages" if st["sources"]
                             else "Nothing added yet. Add a note, a few files or a workspace folder, and your Bots can look things up in it.")
        if keep:
            for r in range(self.table.rowCount()):
                if self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)["id"] == keep:
                    self.table.selectRow(r)
                    return
        if not self.search.text().strip():
            self._clear_preview()

    # ------------------------------------------------------------------ preview
    def _source_selected(self) -> None:
        r = self.table.currentRow()
        if r < 0:
            return
        s = self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
        self.current = s
        self.del_btn.setEnabled(True)
        self.p_title.setText(s["name"])
        kind = {"note": "Note", "upload": "Uploaded file", "file": f"Workspace file: {s['path']}"}.get(s["kind"], s["kind"])
        self.p_meta.setText(f"{kind} · {s['chunks']} passage{'s' if s['chunks'] != 1 else ''} · {s['bot_name'] + ' only' if s['bot_name'] else 'All Bots'}")
        self.api.get(f"/api/knowledge/{s['id']}", lambda d, sid=s["id"]: self._got_text(sid, d), self._error)

    def _got_text(self, sid: str, d: dict) -> None:
        if self.current and self.current["id"] == sid:
            more = f"\n\n… (the first passages of {d['passages']}; Bots can read the rest)" if d["passages"] > 6 else ""
            self.p_text.setPlainText(d["text"] + more)

    def _clear_preview(self) -> None:
        self.current = None
        self.p_title.setText("")
        self.p_meta.setText("")
        self.p_text.clear()
        self.del_btn.setEnabled(False)

    # ------------------------------------------------------------------ search
    def _run_search(self) -> None:
        q = self.search.text().strip()
        if not q:
            self.stack.setCurrentWidget(self.table)
            self.reload()
            return
        self.api.get("/api/knowledge/search", self._got_hits, self._error, params={"q": q, "limit": 20})

    def _got_hits(self, d: dict) -> None:
        self.hits = d["hits"]
        self.results.blockSignals(True)
        self.results.clear()
        for h in self.hits:
            it = QListWidgetItem(f"{h['name']}  ·  passage {h['seq']}\n{' '.join(h['snippet'].split())[:200]}")
            self.results.addItem(it)
        if not self.hits:
            it = QListWidgetItem("Nothing in your knowledge base matches that.")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.results.addItem(it)
        self.results.blockSignals(False)
        self.stack.setCurrentWidget(self.results)
        self.summary.setText(f"{len(self.hits)} matching passage{'s' if len(self.hits) != 1 else ''}")

    def _hit_selected(self, row: int) -> None:
        if 0 <= row < len(self.hits):
            h = self.hits[row]
            self.current = None
            self.del_btn.setEnabled(False)
            self.p_title.setText(h["name"])
            self.p_meta.setText(f"Passage {h['seq']}")
            self.p_text.setPlainText(h["text"])

    # ------------------------------------------------------------------ adding
    def _bot(self) -> str:
        return self.scope.currentData() or ""

    def add_note(self) -> None:
        d = NoteDialog(self)
        if d.exec():
            self.api.post("/api/knowledge/note", {"name": d.name.text().strip(), "text": d.text.toPlainText(), "bot_id": self._bot()},
                          lambda _r: self.reload(), lambda e: QMessageBox.warning(self, "Could not add the note", e))

    def add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add documents", "", FILE_FILTER)
        if not paths:
            return
        self._upload(list(paths), [])

    def _upload(self, todo: list[str], failed: list[str]) -> None:
        if not todo:
            self.reload()
            if failed:
                QMessageBox.warning(self, "Some files were not added", "\n".join(failed))
            return
        path = todo.pop(0)
        name = os.path.basename(path)
        try:
            if os.path.getsize(path) > MAX_UPLOAD:
                failed.append(f"{name}: too large (the limit is 20 MB).")
                return self._upload(todo, failed)
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            failed.append(f"{name}: {e}")
            return self._upload(todo, failed)
        self.summary.setText(f"Adding {name}…")
        self.api.post("/api/knowledge/upload", {"filename": name, "data_b64": base64.b64encode(data).decode(), "bot_id": self._bot()},
                      lambda _r: self._upload(todo, failed), lambda e, n=name: (failed.append(f"{n}: {e}"), self._upload(todo, failed)))

    def add_folder(self) -> None:
        text, ok = QInputDialog.getText(self, "Add a workspace folder", "Folder or file inside the workspace (for example docs or shared/reports):")
        if ok and text.strip():
            self.api.post("/api/knowledge/workspace", {"path": text.strip(), "bot_id": self._bot()}, lambda r: (self.reload(), self.summary.setText(f"Added {len(r['added'])} document(s).")),
                          lambda e: QMessageBox.warning(self, "Could not add that", e))

    def refresh_files(self) -> None:
        self.api.post("/api/knowledge/refresh", {}, lambda r: (self.reload(), self.summary.setText(f"{r['changed']} document(s) re-read." if r["changed"] else "Everything is up to date.")), self._error)

    def remove_current(self) -> None:
        if not self.current:
            return
        s = self.current
        if QMessageBox.question(self, "Remove document", f"Remove {s['name']} from the knowledge base? The original file is not touched.") != QMessageBox.StandardButton.Yes:
            return
        self.api.delete(f"/api/knowledge/{s['id']}", lambda _r: (self._clear_preview(), self.reload()), self._error)
