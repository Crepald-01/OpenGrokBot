"""Files: browse, search and preview everything your Bots have saved in the shared workspace."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QRect, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import (QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QScrollArea, QSplitter, QStackedWidget, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from . import icons, theme
from .api import Api
from .store import Store
from .widgets import PageHeader, button, label, page_layout

ROW_ROLE = Qt.ItemDataRole.UserRole + 1      # display dict for RowDelegate: title, sub, badge (text, kind), icon, icon_color


def human_size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def ago(ts: float) -> str:
    d = max(0, time.time() - ts)
    return "just now" if d < 90 else f"{int(d // 60)} min ago" if d < 3600 else f"{int(d // 3600)} h ago" if d < 86400 else time.strftime("%b %d", time.localtime(ts))


ICON_FOR = {"dir": "folder", "text": "file", "image": "image", "other": "file"}
CODE_EXT = (".py", ".js", ".ts", ".json", ".sh", ".ps1", ".bat", ".html", ".css", ".yaml", ".yml", ".toml", ".csv")


def shrink(font: QFont, delta: float) -> QFont:
    """A copy of font, smaller by delta px (QSS sizes are pixel sizes, so pointSizeF() is not usable here)."""
    f = QFont(font)
    if font.pixelSize() > 0:
        f.setPixelSize(max(9, font.pixelSize() - round(delta)))
    else:
        f.setPointSizeF(max(7.0, font.pointSizeF() - delta))
    return f


def file_icon(e: dict) -> tuple[str, str]:
    """(icon name, colour role) for one entry: folders in the accent colour, code and images in a quiet tone."""
    if e.get("dir"):
        return "folder", "accent"
    if e.get("kind") == "image":
        return "image", "muted"
    if e.get("name", "").lower().endswith(CODE_EXT):
        return "terminal", "muted"
    return "file", "muted"


class RowDelegate(QStyledItemDelegate):
    """Two-line rows with an optional icon and a status badge, on a hover and selected state (used by Files and Skills)."""
    HEIGHT = 58

    def sizeHint(self, option, index) -> QSize:
        return QSize(option.rect.width(), theme.dp(self.HEIGHT))

    def paint(self, painter: QPainter, option, index) -> None:
        row = index.data(ROW_ROLE)
        if not row:
            return super().paint(painter, option, index)
        p = theme.palette()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(4, 3, -4, -3)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p["select"] if selected else p["hover"]))
            painter.drawRoundedRect(r, 10, 10)
        x = r.left() + 14
        if row.get("icon"):
            col = p["accent"] if row.get("icon_color") == "accent" else p["muted"]
            icons.icon(row["icon"], col, 20).paint(painter, QRect(x, r.center().y() - 10, 20, 20))
            x += 32
        badge = row.get("badge")
        bw = 0
        if badge:
            bfont = shrink(option.font, 2)
            bfont.setWeight(QFont.Weight.DemiBold)
            painter.setFont(bfont)
            text, kind = badge
            fg, bg = {"ok": (p["ok"], p["ok_bg"]), "warn": (p["warn"], p["warn_bg"]), "bad": (p["bad"], p["bad_bg"])}.get(kind, (p["muted"], p["panel2"]))
            bw = QFontMetrics(bfont).horizontalAdvance(text) + 18
            br = QRect(r.right() - 14 - bw, r.center().y() - 11, bw, 22)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(bg))
            painter.drawRoundedRect(br, 11, 11)
            painter.setPen(QColor(fg))
            painter.drawText(br, Qt.AlignmentFlag.AlignCenter, text)
        avail = r.right() - 14 - (bw + 12 if bw else 0) - x
        tfont = QFont(option.font)
        tfont.setWeight(QFont.Weight.DemiBold)
        sfont = shrink(option.font, 1)
        top, bottom = r.top() + 10, r.bottom() - 10
        painter.setFont(tfont)
        painter.setPen(QColor(p["text"]))
        title = QFontMetrics(tfont).elidedText(row.get("title", ""), Qt.TextElideMode.ElideRight, max(40, avail))
        painter.drawText(QRect(x, top, max(40, avail), (bottom - top) // 2 + 2), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, title)
        painter.setFont(sfont)
        painter.setPen(QColor(p["muted"]))
        sub = QFontMetrics(sfont).elidedText(row.get("sub", ""), Qt.TextElideMode.ElideRight, max(40, avail))
        painter.drawText(QRect(x, top + (bottom - top) // 2, max(40, avail), (bottom - top) // 2), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, sub)
        painter.restore()


def styled_list(lst: QListWidget) -> QListWidget:
    """Give a list the card rows: a delegate, no per-item sizing work and no focus frame."""
    lst.setItemDelegate(RowDelegate(lst))
    lst.setUniformItemSizes(True)
    lst.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    lst.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    lst.setTextElideMode(Qt.TextElideMode.ElideRight)
    return lst


def set_row(item: QListWidgetItem, title: str, sub: str, icon: str = "", icon_color: str = "muted", badge: tuple[str, str] | None = None) -> None:
    """Fill a row: the item text stays a plain two-line summary for screen readers, the delegate paints the card."""
    item.setText(f"{title}\n{sub}")
    item.setData(ROW_ROLE, {"title": title, "sub": sub, "icon": icon, "icon_color": icon_color, "badge": badge})


class EmptyPanel(QWidget):
    """What a list shows when there is nothing in it: an icon, one short sentence and at most one action."""

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.setSpacing(8)
        self.icon = QLabel()
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title = label("", h2=True, wrap=False)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sub = label("", muted=True)
        self.sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sub.setMaximumWidth(340)
        self.action = button("", primary=True, on=self._run)
        self.action.hide()
        self._on = None
        v.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignHCenter)
        v.addSpacing(6)
        v.addWidget(self.title, 0, Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(self.sub, 0, Qt.AlignmentFlag.AlignHCenter)
        v.addSpacing(10)
        v.addWidget(self.action, 0, Qt.AlignmentFlag.AlignHCenter)

    def set_content(self, icon: str, title: str, sub: str, action: str = "", on=None) -> None:
        self.icon.setPixmap(icons.pixmap(icon, theme.palette()["muted"], 40))
        self.title.setText(title)
        self.sub.setText(sub)
        self._on = on
        self.action.setText(action)
        self.action.setVisible(bool(action and on))

    def _run(self) -> None:
        if self._on:
            self._on()


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
        v.setSpacing(12)
        v.addWidget(label("Before a Bot overwrites, appends to, deletes or replaces this file (or you delete it here), the old contents are kept. Pick a version to read it, and restore it if you want it back. "
                          "Restoring keeps today's version too, so it can be undone.", muted=True))
        split = QSplitter(Qt.Orientation.Horizontal)
        self.list = styled_list(QListWidget())
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
            it = QListWidgetItem()
            set_row(it, time.strftime("%b %d %H:%M", time.localtime(r["ts"])), f"{r['reason']}{who} · {human_size(r['size'])}", "history")
            self.list.addItem(it)
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
        self.mode = "recent"          # recent | browse | search | deleted
        self.cwd = ""
        self.current: dict | None = None
        self.entries: list[dict] = []
        self.stats: dict = {}
        self.refresh_btn = button("Refresh", icon="refresh", on=self.reload)
        self.folder_btn = button("Open folder", icon="folder", on=self.open_folder)
        v = page_layout(self, PageHeader("Files", "Everything your Bots have saved in the shared workspace. Preview it, keep a copy or delete it.", [self.refresh_btn, self.folder_btn]))

        # one toolbar: the three views as a segmented group, then the search field
        bar = QHBoxLayout()
        bar.setSpacing(12)
        seg = QFrame()
        seg.setObjectName("toolgroup")
        sl = QHBoxLayout(seg)
        sl.setContentsMargins(4, 4, 4, 4)
        sl.setSpacing(4)
        self.b_recent = button("Recent", on=lambda: self.set_mode("recent"))
        self.b_browse = button("Folders", on=lambda: self.set_mode("browse"))
        self.b_deleted = button("Deleted", on=lambda: self.set_mode("deleted"))
        for b in (self.b_recent, self.b_browse, self.b_deleted):
            sl.addWidget(b)
        bar.addWidget(seg)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search file names…")
        self.search.setProperty("search", True)
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._search_changed)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._run_search)
        bar.addWidget(self.search, 1)
        v.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(8)
        crumb = QHBoxLayout()
        self.up_btn = button("Up", flat=True, icon="arrow-left", on=self.go_up)
        self.crumb = label("", muted=True, wrap=False)
        crumb.addWidget(self.up_btn)
        crumb.addWidget(self.crumb, 1)
        lv.addLayout(crumb)
        self.list = styled_list(QListWidget())
        self.list.currentRowChanged.connect(self._row_changed)
        self.list.itemActivated.connect(self._activated)
        self.list.itemDoubleClicked.connect(self._activated)
        self.empty = EmptyPanel()
        self.list_stack = QStackedWidget()
        self.list_stack.addWidget(self.list)
        self.list_stack.addWidget(self.empty)
        lv.addWidget(self.list_stack, 1)
        self.summary = label("", muted=True, wrap=False)
        lv.addWidget(self.summary)
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(16, 0, 0, 0)
        rv.setSpacing(8)
        self.p_title = label("", h2=True, wrap=False)
        self.p_meta = label("", muted=True, wrap=False)
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
        actions.setSpacing(0)
        act_box = QFrame()
        act_box.setObjectName("toolgroup")
        al = QHBoxLayout(act_box)
        al.setContentsMargins(4, 4, 4, 4)
        al.setSpacing(4)
        self.save_btn = button("Save a copy…", icon="download", on=self.save_copy)
        self.hist_btn = button("History…", icon="history", on=self.show_history, tip="Earlier versions of this file")
        self.del_btn = button("Delete", danger=True, icon="trash", on=self.delete_current)
        self.restore_btn = button("Restore", primary=True, icon="history", on=self.restore_deleted, tip="Put this deleted file back in the workspace")
        for b in (self.save_btn, self.hist_btn, self.del_btn, self.restore_btn):
            al.addWidget(b)
        actions.addWidget(act_box)
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
        for e in rows:
            who = f" by {e['bot_name']}" if e.get("bot_name") else ""
            where = (e["path"].rsplit("/", 1)[0] + " · ") if "/" in e["path"] else ""
            it = QListWidgetItem()
            set_row(it, e["name"], f"{where}deleted{who} · {ago(e['ts'])} · {human_size(e['size'])}", "file", "muted", ("deleted", "bad"))
            it.setData(Qt.ItemDataRole.UserRole, e)
            self.list.addItem(it)
        self.list.blockSignals(False)
        self.crumb.setText("Deleted files you can bring back")
        self.up_btn.hide()
        n = len(rows)
        self.summary.setText(f"{n} deleted file{'s' if n != 1 else ''} can be restored" if n else "")
        if not rows:
            self._show_empty()
            self._clear_preview()
            return
        self.list_stack.setCurrentWidget(self.list)
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
        for e in entries:
            where = e["path"].rsplit("/", 1)[0] if "/" in e["path"] and self.mode != "browse" else ""
            sub = " · ".join(x for x in (where, "Folder" if e["dir"] else f"{human_size(e['size'])} · {ago(e['mtime'])}") if x)
            icon, tone = file_icon(e)
            it = QListWidgetItem()
            set_row(it, e["name"], sub, icon, tone)
            it.setData(Qt.ItemDataRole.UserRole, e)
            self.list.addItem(it)
        self.list.blockSignals(False)
        self.crumb.setText(("workspace / " + self.cwd.replace("/", " / ")).strip(" /") if self.mode == "browse" else {"recent": "Recently changed", "search": "Search results"}[self.mode])
        self.up_btn.setVisible(self.mode == "browse" and bool(self.cwd))
        self.summary.setText(summary)
        if not entries:
            self._show_empty()
            self._clear_preview()
            return
        self.list_stack.setCurrentWidget(self.list)
        for i, e in enumerate(entries):
            if e["path"] == keep:
                self.list.setCurrentRow(i)
                return
        self._clear_preview()

    def _show_empty(self) -> None:
        """A friendly sentence and one action in place of an empty table."""
        if self.mode == "deleted":
            self.empty.set_content("file", "Nothing deleted lately", "When a Bot (or you) deletes a file, a copy waits here so you can bring it back.")
        elif self.mode == "search":
            self.empty.set_content("search", "No matches", f"Nothing is called “{self.search.text().strip()}”. Try part of the name.", "Clear search", self.search.clear)
        elif self.mode == "browse" and self.cwd:
            self.empty.set_content("folder", "This folder is empty", "Files your Bots save into it show up here.", "Go up", self.go_up)
        elif self.mode == "browse":
            self.empty.set_content("folder", "The workspace is empty", "Once a Bot saves a file, it shows up here.", "Refresh", self.reload)
        else:
            self.empty.set_content("file", "No files yet", "When a Bot saves something to the shared workspace, it appears here.", "Refresh", self.reload)
        self.list_stack.setCurrentWidget(self.empty)

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
                self.p_meta.setText("Folder  ·  double-click to open")
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
