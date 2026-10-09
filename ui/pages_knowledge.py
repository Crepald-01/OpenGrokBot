"""Knowledge: documents your Bots can search. Add notes, text and Word files or whole workspace folders."""
from __future__ import annotations

import base64
import html
import os
import re

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit, QSplitter, QStackedWidget, QVBoxLayout, QWidget)

from . import theme
from .api import Api
from .pages_files import ago, human_size
from .pages_inbox import fill_row, make_table
from .pages_routines import EmptyState
from .store import Store
from .widgets import PageHeader, button, card, label, page_layout

MAX_UPLOAD = 20_000_000
FILE_FILTER = "Documents (*.txt *.md *.markdown *.csv *.tsv *.json *.log *.html *.htm *.xml *.yml *.yaml *.docx *.rst *.py *.js);;All files (*)"


class NoteDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add a note")
        self.resize(560, 420)
        v = QVBoxLayout(self)
        v.setSpacing(theme.dp(12))
        v.addWidget(label("Paste or write anything you want your Bots to be able to look up: a policy, a checklist, how you like things done.", muted=True))
        f = QFormLayout()
        f.setVerticalSpacing(theme.dp(10))
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


def highlight(text: str, query: str) -> str:
    """Escape a passage and mark every word of the query in it (one pass, so marks never nest)."""
    terms = sorted({t for t in query.split() if t}, key=len, reverse=True)
    esc = html.escape(text)
    if not terms:
        return esc
    pat = re.compile("|".join(re.escape(html.escape(t)) for t in terms), re.IGNORECASE)
    mark = theme.palette()["accent_dim"]
    return pat.sub(lambda m: f'<span style="background-color: {mark}; font-weight: 600;">{m.group(0)}</span>', esc)


class HitCard(QFrame):
    """One search result: the source name as a heading, where it sits, and the passage with the match highlighted."""

    def __init__(self, hit: dict, query: str):
        super().__init__()
        self.setObjectName("hitcard")
        v = QVBoxLayout(self)
        v.setContentsMargins(theme.dp(16), theme.dp(12), theme.dp(16), theme.dp(14))
        v.setSpacing(theme.dp(4))
        v.addWidget(label(hit["name"], h2=True, wrap=False))
        v.addWidget(label(f"Passage {hit['seq']}", muted=True, wrap=False))
        snippet = " ".join(hit["snippet"].split())[:240]
        body = QLabel(highlight(snippet, query))
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        body.setContentsMargins(0, theme.dp(4), 0, 0)
        v.addWidget(body)
        for child in self.findChildren(QWidget):   # clicks fall through to the list row underneath
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.set_selected(False)

    def set_selected(self, on: bool) -> None:
        p = theme.palette()
        bg, bd = (p["select"], p["accent"]) if on else (p["panel"], p["line"])
        self.setStyleSheet(f"QFrame#hitcard {{ background: {bg}; border: 1px solid {bd}; border-radius: {theme.dp(12)}px; }}")


class HitList(QListWidget):
    """Result list whose rows are cards: keeps each card as wide as the list, so its text wraps to the right height."""

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.refit()

    def __init__(self):
        super().__init__()
        # the cards draw their own frame and selection, so the row padding and margins of the default item style are dropped here
        self.setStyleSheet("QListWidget::item { padding: 0px; margin: 0px; border: none; background: transparent; }")

    def refit(self) -> None:
        for i in range(self.count()):
            it = self.item(i)
            c = self.itemWidget(it)
            if isinstance(c, HitCard):
                w = max(1, self.viewport().width() - theme.dp(16))   # leave the same inset on both sides of a card
                it.setSizeHint(QSize(w, c.heightForWidth(w) or c.sizeHint().height()))
        self.doItemsLayout()

    def mark_selected(self, row: int) -> None:
        for i in range(self.count()):
            c = self.itemWidget(self.item(i))
            if isinstance(c, HitCard):
                c.set_selected(i == row)


class KnowledgePage(QWidget):
    def __init__(self, api: Api, store: Store):
        super().__init__()
        self.api, self.store = api, store
        self.sources: list[dict] = []
        self.hits: list[dict] = []
        self.current: dict | None = None
        self.scope = QComboBox()
        self.scope.setToolTip("Who can find what you add next")
        self.add_files_btn = button("Add files…", primary=True, icon="upload", on=self.add_files)
        self.add_note_btn = button("Add note…", icon="plus", on=self.add_note)
        self.add_folder_btn = button("Add workspace folder…", icon="folder", on=self.add_folder)
        v = page_layout(self, PageHeader("Knowledge", "Documents your Bots can search and quote. Nothing leaves your PC: it is indexed locally, with no model call.",
                                         [self.add_files_btn, self.add_note_btn, self.add_folder_btn]))
        v.setSpacing(theme.dp(20))
        bar = QHBoxLayout()
        bar.setSpacing(theme.dp(10))
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
        lv.setContentsMargins(0, 0, theme.dp(8), 0)
        lv.setSpacing(theme.dp(8))
        self.stack = QStackedWidget()
        self.table = make_table(["Name", "Shared with", "Passages", "Size", "Updated"], 0)
        self.table.itemSelectionChanged.connect(self._source_selected)
        self.results = HitList()
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results.setSpacing(theme.dp(8))
        self.results.currentRowChanged.connect(self._hit_selected)
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.results)
        lv.addWidget(self.stack, 1)
        self.empty_none = EmptyState("book", "Add a note or a file to get started",
                                     "Your Bots can search and quote anything you add here: a policy, a checklist, a handbook. Everything is indexed on this PC.",
                                     [button("Add files…", primary=True, icon="upload", on=self.add_files), button("Add note…", icon="plus", on=self.add_note)])
        self.empty_search = EmptyState("search", "No matches",
                                       "Nothing in your knowledge base matches that search. Try other words, or add the document that should contain it.",
                                       [button("Add files…", icon="upload", on=self.add_files)])
        lv.addWidget(self.empty_none, 1)
        lv.addWidget(self.empty_search, 1)
        self.summary = label("", muted=True, wrap=False)
        lv.addWidget(self.summary)
        split.addWidget(left)

        right = card("true")
        self.preview = right
        rv = QVBoxLayout(right)
        rv.setContentsMargins(theme.dp(20), theme.dp(18), theme.dp(20), theme.dp(16))
        rv.setSpacing(theme.dp(8))
        self.p_title = label("", h2=True, wrap=False)
        self.p_meta = label("", muted=True, wrap=False)
        rv.addWidget(self.p_title)
        rv.addWidget(self.p_meta)
        rv.addSpacing(theme.dp(4))
        self.p_text = QPlainTextEdit()
        self.p_text.setReadOnly(True)
        self.p_text.setPlaceholderText("Select a document to read it, or search for something.")
        rv.addWidget(self.p_text, 1)
        actions = QHBoxLayout()
        actions.setSpacing(theme.dp(8))
        self.refresh_btn = button("Refresh from workspace", icon="refresh", on=self.refresh_files, tip="Re-read workspace files that changed")
        self.del_btn = button("Remove", danger=True, icon="trash", on=self.remove_current)
        actions.addWidget(self.refresh_btn)
        actions.addStretch(1)
        actions.addWidget(self.del_btn)
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
        self.summary.setText(f"{st['sources']} document{'s' if st['sources'] != 1 else ''} · {st['passages']:,} passages" if st["sources"] else "")
        if keep:
            for r in range(self.table.rowCount()):
                if self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)["id"] == keep:
                    self.table.selectRow(r)
                    self._sync_views()
                    return
        if not self.search.text().strip():
            self._clear_preview()
        self._sync_views()

    def _sync_views(self) -> None:
        """Pick what the left side shows: the list, the 'add something' state, or the 'no match' state. The preview only appears when there is something to preview."""
        if self.search.text().strip():
            none = not self.hits
            self.stack.setVisible(not none)
            self.empty_search.setVisible(none)
            self.empty_none.setVisible(False)
            self.preview.setVisible(not none)
        else:
            none = not self.sources
            self.stack.setVisible(not none)
            self.empty_none.setVisible(none)
            self.empty_search.setVisible(False)
            self.preview.setVisible(not none)

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
        q = self.search.text().strip()
        self.results.blockSignals(True)
        self.results.clear()
        for h in self.hits:
            snippet = " ".join(h["snippet"].split())[:240]
            it = QListWidgetItem(f"{h['name']}  ·  passage {h['seq']}\n{snippet}")
            self.results.addItem(it)
            self.results.setItemWidget(it, HitCard(h, q))
        self.results.refit()
        self.results.blockSignals(False)
        self.stack.setCurrentWidget(self.results)
        self.summary.setText(f"{len(self.hits)} matching passage{'s' if len(self.hits) != 1 else ''}")
        self._sync_views()

    def _hit_selected(self, row: int) -> None:
        self.results.mark_selected(row)
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
