"""Files: browse, search and preview everything your Bots have saved in the shared workspace."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QScrollArea,
                               QSplitter, QStackedWidget, QVBoxLayout, QWidget)

from . import icons, theme
from .api import Api
from .store import Store
from .widgets import PageHeader, button, label, page_layout


def human_size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def ago(ts: float) -> str:
    d = max(0, time.time() - ts)
    return "just now" if d < 90 else f"{int(d // 60)} min ago" if d < 3600 else f"{int(d // 3600)} h ago" if d < 86400 else time.strftime("%b %d", time.localtime(ts))


ICON_FOR = {"dir": "folder", "text": "file", "image": "image", "other": "file"}


class HistoryDialog(QDialog):
    """Earlier versions of one file: look at them and put one back."""

    def __init__(self, api: Api, path: str, parent=None):
        super().__init__(parent)
        self.api, self.path = api, path
        self.versions: list[dict] = []
        self.restored = False
        self.setWindowTitle(f"History of {path.rsplit('/', 1)[-1]}")
        self.resize(780, 520)
        v = QVBoxLayout(self)
        v.addWidget(label("Before a Bot overwrites, appends to, deletes or replaces this file (or you delete it here), the old contents are kept. Pick a version to read it, and restore it if you want it back. "
                          "Restoring keeps today's version too, so it can be undone.", muted=True))
        split = QSplitter(Qt.Orientation.Horizontal)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._pick)
        split.addWidget(self.list)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(theme.mono())
        split.addWidget(self.text)
        split.setSizes([280, 480])
        v.addWidget(split, 1)
        self.msg = label("", muted=True)
        v.addWidget(self.msg)
        row = QHBoxLayout()
        self.restore_btn = button("Restore this version", primary=True, icon="history", on=self._restore)
        self.restore_btn.setEnabled(False)
        row.addWidget(self.restore_btn)
        row.addStretch(1)
        row.addWidget(button("Close", on=self.accept))
        v.addLayout(row)
        api.get("/api/ws/history", self._got, lambda m: self.msg.setText(m), params={"path": path})

    def _got(self, d: dict) -> None:
        self.versions = d["versions"]
        self.list.clear()
        for r in self.versions:
            who = f" · {r['bot_name']}" if r.get("bot_name") else ""
            self.list.addItem(f"{time.strftime('%b %d %H:%M', time.localtime(r['ts']))}  ·  {r['reason']}{who}\n{human_size(r['size'])}")
        if not self.versions:
            it = QListWidgetItem("No earlier versions yet.")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(it)
        else:
            self.list.setCurrentRow(0)

    def _pick(self, row: int) -> None:
        self.restore_btn.setEnabled(0 <= row < len(self.versions))
        if 0 <= row < len(self.versions):
            vid = self.versions[row]["id"]
            self.api.get("/api/ws/version", lambda d, i=vid: self._got_text(i, d), lambda m: self.msg.setText(m), params={"id": vid})

    def _got_text(self, vid: int, d: dict) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self.versions) and self.versions[row]["id"] == vid:
            self.text.setPlainText("(This version is not text, so it cannot be shown. You can still restore it.)" if d.get("binary") else
                                   d["text"] + ("\n\n… (shown the first 200 KB)" if d.get("truncated") else ""))

    def _restore(self) -> None:
        row = self.list.currentRow()
        if not (0 <= row < len(self.versions)):
            return
        self.api.post("/api/ws/restore", {"path": self.path, "version_id": self.versions[row]["id"]}, lambda _r: (setattr(self, "restored", True), self.accept()),
                      lambda m: self.msg.setText(m))


class FilesPage(QWidget):
    def __init__(self, api: Api, store: Store):
        super().__init__()
        self.api, self.store = api, store
        self.mode = "recent"          # recent | browse | search
        self.cwd = ""
        self.current: dict | None = None
        self.entries: list[dict] = []
        self.stats: dict = {}
        self.refresh_btn = button("Refresh", icon="refresh", on=self.reload)
        self.folder_btn = button("Open folder", icon="folder", on=self.open_folder)
        v = page_layout(self, PageHeader("Files", "Everything your Bots have saved in the shared workspace. Preview it, keep a copy or delete it.", [self.refresh_btn, self.folder_btn]))

        bar = QHBoxLayout()
        self.b_recent = button("Recent", on=lambda: self.set_mode("recent"))
        self.b_browse = button("Folders", on=lambda: self.set_mode("browse"))
        self.b_deleted = button("Deleted", on=lambda: self.set_mode("deleted"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search file names…")
        self.search.setProperty("search", True)
        self.search.textChanged.connect(self._search_changed)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._run_search)
        bar.addWidget(self.b_recent)
        bar.addWidget(self.b_browse)
        bar.addWidget(self.b_deleted)
        bar.addWidget(self.search, 1)
        v.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(8)
        crumb = QHBoxLayout()
        self.up_btn = button("↑ Up", flat=True, on=self.go_up)
        self.crumb = label("", muted=True, wrap=False)
        crumb.addWidget(self.up_btn)
        crumb.addWidget(self.crumb, 1)
        lv.addLayout(crumb)
        self.list = QListWidget()
        self.list.setIconSize(QSize(18, 18))
        self.list.currentRowChanged.connect(self._row_changed)
        self.list.itemActivated.connect(self._activated)
        self.list.itemDoubleClicked.connect(self._activated)
        lv.addWidget(self.list, 1)
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
        self.stack = QStackedWidget()
        self.p_text = QPlainTextEdit()
        self.p_text.setReadOnly(True)
        self.p_text.setFont(theme.mono())
        self.p_img = QLabel()
        self.p_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(self.p_img)
        self.p_msg = label("Select a file to preview it.", muted=True)
        self.p_msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for w in (self.p_msg, self.p_text, sc):
            self.stack.addWidget(w)
        self._sc = sc
        rv.addWidget(self.stack, 1)
        actions = QHBoxLayout()
        self.save_btn = button("Save a copy…", icon="download", on=self.save_copy)
        self.hist_btn = button("History…", icon="history", on=self.show_history, tip="Earlier versions of this file")
        self.del_btn = button("Delete", danger=True, icon="trash", on=self.delete_current)
        self.restore_btn = button("Restore", primary=True, icon="history", on=self.restore_deleted, tip="Put this deleted file back in the workspace")
        actions.addWidget(self.save_btn)
        actions.addWidget(self.hist_btn)
        actions.addWidget(self.del_btn)
        actions.addWidget(self.restore_btn)
        actions.addStretch(1)
        rv.addLayout(actions)
        split.addWidget(right)
        split.setSizes([460, 620])
        v.addWidget(split, 1)
        self._set_preview_enabled(False)
        self._style_modes()

    # ------------------------------------------------------------------ state
    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.folder_btn.setVisible(self.api.conn.mode == "local")
        self.reload()

    def _style_modes(self) -> None:
        for b, on in ((self.b_recent, self.mode == "recent"), (self.b_browse, self.mode == "browse"), (self.b_deleted, self.mode == "deleted")):
            b.setProperty("primary", on)
            b.style().unpolish(b)
            b.style().polish(b)
        deleted = self.mode == "deleted"
        for b in (self.save_btn, self.hist_btn, self.del_btn):
            b.setVisible(not deleted)
        self.restore_btn.setVisible(deleted)

    def set_mode(self, mode: str) -> None:
        if mode != "search":
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
        self.mode = mode
        self._style_modes()
        self.reload()

    def reload(self) -> None:
        if self.mode == "deleted":
            self.api.get("/api/ws/deleted", self._got_deleted, self._error)
        elif self.mode == "browse":
            self.api.get("/api/ws/list", self._got_list, self._error, params={"path": self.cwd})
        elif self.mode == "search":
            self.api.get("/api/ws/search", lambda d: self._show(d["entries"], f"{len(d['entries'])} match{'es' if len(d['entries']) != 1 else ''}"), self._error,
                         params={"q": self.search.text().strip()})
        else:
            self.api.get("/api/ws/recent", self._got_recent, self._error, params={"limit": 60})

    def _error(self, msg: str) -> None:
        self.summary.setText(msg)

    def _got_recent(self, d: dict) -> None:
        self.stats = d.get("stats", {})
        self._show(d["entries"], f"{self.stats.get('files', 0)} files · {human_size(self.stats.get('bytes', 0))} in the workspace")

    def _got_deleted(self, d: dict) -> None:
        rows = [{**e, "deleted": True, "name": e["path"].rsplit("/", 1)[-1], "dir": False, "kind": "other", "mtime": e["ts"]} for e in d["entries"]]
        keep = self.current["version_id"] if self.current and self.current.get("deleted") else 0
        self.entries = rows
        self.list.blockSignals(True)
        self.list.clear()
        p = theme.palette()
        for e in rows:
            who = f" by {e['bot_name']}" if e.get("bot_name") else ""
            where = (e["path"].rsplit("/", 1)[0] + " · ") if "/" in e["path"] else ""
            it = QListWidgetItem(icons.icon("file", p["muted"], 18), f"{e['name']}\n{where}deleted{who} · {ago(e['ts'])} · {human_size(e['size'])}")
            it.setData(Qt.ItemDataRole.UserRole, e)
            self.list.addItem(it)
        self.list.blockSignals(False)
        self.crumb.setText("Deleted files you can bring back")
        self.up_btn.hide()
        n = len(rows)
        self.summary.setText(f"{n} deleted file{'s' if n != 1 else ''} can be restored" if n else "")
        if not rows:
            it = QListWidgetItem("Nothing deleted lately. When a Bot (or you) deletes a file, it shows up here so you can restore it.")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(it)
        for i, e in enumerate(rows):
            if e["version_id"] == keep:
                self.list.setCurrentRow(i)
                return
        self._clear_preview()

    def _got_list(self, d: dict) -> None:
        self.cwd = d["path"]
        self._show(d["entries"], f"{len(d['entries'])} item{'s' if len(d['entries']) != 1 else ''}")

    def _show(self, entries: list[dict], summary: str) -> None:
        keep = self.current["path"] if self.current else ""
        self.entries = entries
        self.list.blockSignals(True)
        self.list.clear()
        p = theme.palette()
        for e in entries:
            where = e["path"].rsplit("/", 1)[0] if "/" in e["path"] and self.mode != "browse" else ""
            sub = "Folder" if e["dir"] else f"{human_size(e['size'])} · {ago(e['mtime'])}"
            it = QListWidgetItem(icons.icon(ICON_FOR.get(e["kind"], "file"), p["accent"] if e["dir"] else p["muted"], 18), f"{e['name']}\n{(where + ' · ') if where else ''}{sub}")
            it.setData(Qt.ItemDataRole.UserRole, e)
            self.list.addItem(it)
        self.list.blockSignals(False)
        self.crumb.setText(("workspace / " + self.cwd.replace("/", " / ")).strip(" /") if self.mode == "browse" else {"recent": "Recently changed", "search": "Search results"}[self.mode])
        self.up_btn.setVisible(self.mode == "browse" and bool(self.cwd))
        self.summary.setText(summary)
        if not entries:
            self.list.addItem(QListWidgetItem("Nothing here yet. Files your Bots save show up here."))
            self.list.item(0).setFlags(Qt.ItemFlag.NoItemFlags)
        for i, e in enumerate(entries):
            if e["path"] == keep:
                self.list.setCurrentRow(i)
                return
        self._clear_preview()

    def go_up(self) -> None:
        self.cwd = self.cwd.rsplit("/", 1)[0] if "/" in self.cwd else ""
        self.reload()

    def _search_changed(self, _t: str) -> None:
        self._timer.start(250)

    def _run_search(self) -> None:
        q = self.search.text().strip()
        self.mode = "search" if q else "recent"
        self._style_modes()
        self.reload()

    # ----------------------------------------------------------------- preview
    def _entry(self) -> dict | None:
        it = self.list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _activated(self, it: QListWidgetItem) -> None:
        e = it.data(Qt.ItemDataRole.UserRole)
        if e and e["dir"]:
            self.mode = "browse"
            self.cwd = e["path"]
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
            self._style_modes()
            self.reload()

    def _row_changed(self, row: int) -> None:
        e = self._entry()
        if e and e.get("deleted"):
            self.current = e
            who = f" by {e['bot_name']}" if e.get("bot_name") else ""
            self.p_title.setText(e["name"])
            self.p_meta.setText(f"{e['path']}  ·  deleted{who}  ·  {ago(e['ts'])}  ·  {human_size(e['size'])}")
            self.restore_btn.setEnabled(True)
            self.api.get("/api/ws/version", lambda d, v=e["version_id"]: self._got_version(v, d), lambda m: self._msg(m), params={"id": e["version_id"]})
            self._msg("Loading…")
            return
        if not e or e["dir"]:
            self._clear_preview(keep_title=bool(e))
            if e:
                self.p_title.setText(e["name"])
                self.p_meta.setText("Folder · double-click to open")
            return
        self.current = e
        self.p_title.setText(e["name"])
        self.p_meta.setText(f"{e['path']}  ·  {human_size(e['size'])}  ·  {ago(e['mtime'])}")
        self._set_preview_enabled(True)
        self.api.get("/api/ws/preview", lambda d, p=e["path"]: self._got_preview(p, d), lambda m: self._msg(m), params={"path": e["path"]})

    def _got_preview(self, path: str, d: dict) -> None:
        if not self.current or self.current["path"] != path:
            return   # the selection moved on
        if d["kind"] == "text":
            self.p_text.setPlainText(d["text"] + ("\n\n… (shown the first 200 KB)" if d.get("truncated") else ""))
            self.stack.setCurrentWidget(self.p_text)
        elif d["kind"] == "image" and not d.get("too_big"):
            self.api.request("GET", "/api/ws/raw", lambda data, p=path: self._got_image(p, data), lambda m: self._msg(m), params={"path": path}, raw=True)
            self._msg("Loading…")
        else:
            self._msg("No preview for this kind of file. Use “Save a copy…” to open it elsewhere." if d["kind"] != "image" else "This image is too large to preview.")

    def _got_version(self, vid: int, d: dict) -> None:
        if not self.current or self.current.get("version_id") != vid:
            return
        if d.get("binary"):
            self._msg("No preview for this kind of file. Restore it to get it back.")
        else:
            self.p_text.setPlainText(d["text"] + ("\n\n… (shown the first 200 KB)" if d.get("truncated") else ""))
            self.stack.setCurrentWidget(self.p_text)

    def _got_image(self, path: str, data: bytes) -> None:
        if not self.current or self.current["path"] != path:
            return
        pm = QPixmap()
        if pm.loadFromData(data):
            self.p_img.setPixmap(pm.scaledToWidth(min(pm.width(), max(300, self._sc.viewport().width() - 20)), Qt.TransformationMode.SmoothTransformation))
            self.stack.setCurrentWidget(self._sc)
        else:
            self._msg("Could not read this image.")

    def _msg(self, text: str) -> None:
        self.p_msg.setText(text)
        self.stack.setCurrentWidget(self.p_msg)

    def _clear_preview(self, keep_title: bool = False) -> None:
        self.current = None
        if not keep_title:
            self.p_title.setText("")
            self.p_meta.setText("")
        self._msg("Select a file to preview it.")
        self._set_preview_enabled(False)

    def _set_preview_enabled(self, on: bool) -> None:
        self.save_btn.setEnabled(on)
        self.hist_btn.setEnabled(on)
        self.del_btn.setEnabled(on)
        self.restore_btn.setEnabled(on and self.mode == "deleted")

    # ------------------------------------------------------------------ actions
    def save_copy(self) -> None:
        if not self.current:
            return
        name = self.current["name"]
        path, _ = QFileDialog.getSaveFileName(self, "Save a copy", name)
        if not path:
            return

        def ok(data: bytes) -> None:
            with open(path, "wb") as f:
                f.write(data)
            self.summary.setText(f"Saved a copy to {path}")
        self.api.request("GET", "/api/ws/raw", ok, self._error, params={"path": self.current["path"]}, raw=True)

    def delete_current(self) -> None:
        if not self.current:
            return
        name, path = self.current["name"], self.current["path"]
        if QMessageBox.question(self, "Delete file", f"Delete {name} from the workspace? A copy is kept, so you can bring it back from the Deleted view.") != QMessageBox.StandardButton.Yes:
            return
        self.api.delete("/api/ws/file", lambda _r: (self._clear_preview(), self.reload()), self._error, params={"path": path})

    def show_history(self) -> None:
        if not self.current or self.current.get("dir"):
            return
        d = HistoryDialog(self.api, self.current["path"], self)
        d.exec()
        if d.restored:
            self.summary.setText(f"Restored an earlier version of {self.current['name']}.")
            self.reload()

    def restore_deleted(self) -> None:
        e = self.current
        if not e or not e.get("deleted"):
            return
        self.api.post("/api/ws/restore", {"path": e["path"], "version_id": e["version_id"]},
                      lambda _r: (self._clear_preview(), self.reload(), self.summary.setText(f"Restored {e['name']} to {e['path']}.")), self._error)

    def open_folder(self) -> None:
        base = os.path.join(str(self.store.status.get("data_dir", "")), "workspace")
        target = os.path.join(base, self.cwd.replace("/", os.sep)) if self.mode == "browse" and self.cwd else base
        if os.path.isdir(target):
            QDesktopServices.openUrl(QUrl.fromLocalFile(target))
