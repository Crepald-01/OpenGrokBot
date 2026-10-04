"""The shared computer: each Bot's screen, the workspace files and a terminal."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QScrollArea, QSplitter, QTabWidget, QVBoxLayout, QWidget)

from . import theme
from .api import Api
from .store import Store
from .widgets import button, card, chip, clear_layout, label, PageHeader, page_layout


class ScreenCard(QFrame):
    def __init__(self, bot: dict, api: Api, take, follow):
        super().__init__()
        self.setProperty("card", "true")
        self.bot, self.api = bot, api
        self.setMinimumWidth(280)
        self.setMaximumWidth(460)
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        head = QHBoxLayout()
        head.addWidget(label(f"{bot['emoji']}  {bot['name']}", h2=True, wrap=False), 1)
        self.chip = chip("no screen yet")
        head.addWidget(self.chip)
        v.addLayout(head)
        self.thumb = QLabel("No browser tab open yet")
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setMinimumHeight(170)
        self.thumb.setStyleSheet(f"background: {theme.palette()['code']}; border-radius: 8px; color: {theme.palette()['muted']};")
        v.addWidget(self.thumb)
        self.url = label("", muted=True, wrap=False)
        v.addWidget(self.url)
        row = QHBoxLayout()
        row.addWidget(button("Take over", primary=True, on=lambda: take(bot["id"])))
        row.addWidget(button("Follow along", on=lambda: follow(bot["id"])))
        v.addLayout(row)
        self.fetching = False

    def update_state(self, st: dict | None, kind: str) -> None:
        if not st or not st.get("open"):
            self.chip.setText("no screen yet")
            self.url.setText("")
            return
        txt = {"takeover": "you're driving", "work": "working", "wait": "needs you"}.get(kind, "open")
        self.chip.setText(txt)
        self.chip.setProperty("chip", {"takeover": "warn", "work": "work", "wait": "warn"}.get(kind, "true"))
        self.chip.style().unpolish(self.chip)
        self.chip.style().polish(self.chip)
        self.url.setText(st.get("url", ""))
        if not self.fetching:
            self.fetching = True

            def ok(data: bytes) -> None:
                self.fetching = False
                pm = QPixmap()
                if pm.loadFromData(data):
                    self.thumb.setPixmap(pm.scaledToWidth(300, Qt.TransformationMode.SmoothTransformation))
            self.api.request("GET", f"/api/bots/{self.bot['id']}/screen.jpg", ok, lambda e: setattr(self, "fetching", False), params={"q": 40}, raw=True)


class FilesTab(QWidget):
    def __init__(self, api: Api):
        super().__init__()
        self.api = api
        self.path = "."
        self.current_file = ""
        h = QHBoxLayout(self)
        left = QVBoxLayout()
        bar = QHBoxLayout()
        bar.addWidget(button("↑", on=self.up))
        self.path_label = label(".", muted=True, wrap=False)
        bar.addWidget(self.path_label, 1)
        bar.addWidget(button("⟳", on=self.reload))
        left.addLayout(bar)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self.open_item)
        left.addWidget(self.list, 1)
        row = QHBoxLayout()
        row.addWidget(button("New file", on=self.new_file))
        row.addWidget(button("New folder", on=self.new_folder))
        row.addWidget(button("Upload", on=self.upload))
        left.addLayout(row)
        row2 = QHBoxLayout()
        row2.addWidget(button("Download", on=self.download))
        row2.addWidget(button("Delete", danger=True, on=self.delete))
        left.addLayout(row2)
        lw = QWidget()
        lw.setLayout(left)
        lw.setMaximumWidth(380)
        h.addWidget(lw)
        right = QVBoxLayout()
        self.file_label = label("Select a text file to view or edit it.", muted=True)
        right.addWidget(self.file_label)
        self.editor = QPlainTextEdit()
        self.editor.setFont(theme.mono())
        right.addWidget(self.editor, 1)
        right.addWidget(button("Save file", primary=True, on=self.save))
        h.addLayout(right, 1)

    def reload(self) -> None:
        def ok(d: dict) -> None:
            self.path = d["path"] or "."
            self.path_label.setText("workspace/" + ("" if self.path == "." else self.path))
            self.list.clear()
            for it in d["items"]:
                li = QListWidgetItem(("📁  " if it["dir"] else "📄  ") + it["name"] + ("" if it["dir"] else f"   ({it['size']:,} B)"))
                li.setData(Qt.ItemDataRole.UserRole, it)
                self.list.addItem(li)
        self.api.get("/api/computer/files", ok, params={"path": self.path})

    def _join(self, name: str) -> str:
        return name if self.path == "." else f"{self.path}/{name}"

    def up(self) -> None:
        self.path = "." if "/" not in self.path else self.path.rsplit("/", 1)[0]
        self.reload()

    def open_item(self, li: QListWidgetItem) -> None:
        it = li.data(Qt.ItemDataRole.UserRole)
        if it["dir"]:
            self.path = self._join(it["name"])
            self.reload()
            return
        p = self._join(it["name"])

        def ok(d: dict) -> None:
            self.current_file = p
            self.file_label.setText(p + ("  (truncated, saving is disabled)" if d["truncated"] else ""))
            self.editor.setPlainText(d["content"])
        self.api.get("/api/computer/file", ok, lambda e: QMessageBox.information(self, "Cannot open", e), params={"path": p})

    def save(self) -> None:
        if self.current_file:
            self.api.put("/api/computer/file", {"path": self.current_file, "content": self.editor.toPlainText()}, lambda _: self.reload())

    def new_file(self) -> None:
        n, ok = QInputDialog.getText(self, "New file", "File name:")
        if ok and n.strip():
            self.api.put("/api/computer/file", {"path": self._join(n.strip()), "content": ""}, lambda _: self.reload())

    def new_folder(self) -> None:
        n, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if ok and n.strip():
            self.api.post("/api/computer/mkdir", {"path": self._join(n.strip())}, lambda _: self.reload())

    def upload(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Upload a file to the shared workspace")
        if not f:
            return
        with open(f, "rb") as fh:
            data = fh.read()
        self.api.request("POST", "/api/computer/upload", lambda _: self.reload(), lambda e: QMessageBox.warning(self, "Upload failed", e),
                         content=data, params={"path": self._join(os.path.basename(f))})

    def download(self) -> None:
        li = self.list.currentItem()
        if not li or li.data(Qt.ItemDataRole.UserRole)["dir"]:
            return
        name = li.data(Qt.ItemDataRole.UserRole)["name"]
        dest, _ = QFileDialog.getSaveFileName(self, "Save file", name)
        if dest:
            def ok(data: bytes) -> None:
                with open(dest, "wb") as fh:
                    fh.write(data)
            self.api.request("GET", "/api/computer/file", ok, lambda e: QMessageBox.warning(self, "Download failed", e), params={"path": self._join(name), "download": True}, raw=True)

    def delete(self) -> None:
        li = self.list.currentItem()
        if not li:
            return
        name = li.data(Qt.ItemDataRole.UserRole)["name"]
        if QMessageBox.question(self, "Delete", f"Delete {name}? Bots share this workspace, so they will lose it too.") == QMessageBox.StandardButton.Yes:
            self.api.delete("/api/computer/file", lambda _: self.reload(), params={"path": self._join(name)})


class TerminalTab(QWidget):
    def __init__(self, api: Api):
        super().__init__()
        self.api = api
        v = QVBoxLayout(self)
        v.addWidget(label("A terminal in the shared workspace. Bots use the same one (their commands appear here too, and only commands inside the workspace run without approval).", muted=True))
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setFont(theme.mono())
        v.addWidget(self.out, 1)
        row = QHBoxLayout()
        self.cwd = QLineEdit(".")
        self.cwd.setMaximumWidth(180)
        self.cwd.setToolTip("Working folder inside the workspace")
        self.shell = QComboBox()
        self.shell.addItem("Default shell", "default")
        if os.name == "nt":
            self.shell.addItem("PowerShell", "powershell")
        self.cmd = QLineEdit()
        self.cmd.setPlaceholderText("Type a command and press Enter")
        self.cmd.setFont(theme.mono())
        self.cmd.returnPressed.connect(self.run)
        row.addWidget(self.cwd)
        row.addWidget(self.shell)
        row.addWidget(self.cmd, 1)
        row.addWidget(button("Run", primary=True, on=self.run))
        v.addLayout(row)
        self.seen = 0

    def load_history(self, log: list[dict]) -> None:
        fresh = [e for e in log if e["ts"] > self.seen]
        for e in fresh:
            if e.get("who") != "you":
                self._append(e)
            self.seen = max(self.seen, e["ts"])

    def _append(self, e: dict) -> None:
        who = e.get("who") or "bot"
        self.out.appendPlainText(f"[{who}] {e['cwd']}> {e['command']}\n{e['output'].rstrip()}\n(exit {e['exit']}, {e['seconds']}s)\n")

    def run(self) -> None:
        c = self.cmd.text().strip()
        if not c:
            return
        self.cmd.clear()
        self.out.appendPlainText(f"[you] {self.cwd.text() or '.'}> {c}")

        def ok(d: dict) -> None:
            self.out.appendPlainText(d["output"].rstrip() + f"\n(exit {d['exit']}, {d['seconds']}s)\n")
            self.seen = time.time()
        self.api.post("/api/computer/terminal", {"command": c, "cwd": self.cwd.text() or ".", "shell": self.shell.currentData()}, ok, lambda e: self.out.appendPlainText("Error: " + e))


class ComputerPage(QWidget):
    takeOver = Signal(str, bool)

    def __init__(self, api: Api, store: Store):
        super().__init__()
        self.api, self.store = api, store
        v = page_layout(self, PageHeader("Shared computer", ""))
        self.banner = label("One persistent computer belongs to your account, not to any single Bot. Every Bot sees the same files, browser sessions and app logins. "
                            "Treat everything here as available to every Bot, and never leave secrets in files.", muted=True)
        v.addWidget(self.banner)
        self.err = label("")
        self.err.setStyleSheet(f"color: {theme.palette()['warn']};")
        self.err.hide()
        v.addWidget(self.err)
        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)

        sc = QScrollArea()
        sc.setWidgetResizable(True)
        self.grid_w = QWidget()
        self.grid = QGridLayout(self.grid_w)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.grid.setSpacing(14)
        sc.setWidget(self.grid_w)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tabs.addTab(sc, "Screens")
        self.files = FilesTab(api)
        self.tabs.addTab(self.files, "Files")
        self.term = TerminalTab(api)
        self.tabs.addTab(self.term, "Terminal")
        self.cards: dict[str, ScreenCard] = {}
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        store.botsChanged.connect(self.build)
        self.tabs.currentChanged.connect(self._tab)

    def _tab(self, i: int) -> None:
        if i == 1:
            self.files.reload()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.build()
        self.poll()
        self.timer.start(3000)

    def hideEvent(self, e) -> None:
        super().hideEvent(e)
        self.timer.stop()

    def build(self) -> None:
        if not self.isVisible():
            return
        clear_layout(self.grid)
        self.cards.clear()
        for b in self.store.bots:
            self.cards[b["id"]] = ScreenCard(b, self.api, lambda bid: self.takeOver.emit(bid, False), lambda bid: self.takeOver.emit(bid, True))
        if not self.store.bots:
            self.grid.addWidget(label("Create a Bot to give it a screen on this computer."), 0, 0)
        self.relayout()

    def relayout(self) -> None:
        """Responsive grid: as many 300px+ columns as fit, cards share the width."""
        cols = max(1, (self.width() - 120) // 320)
        for i in range(self.grid.count()):
            self.grid.setColumnStretch(i, 0)
        for i, c in enumerate(self.cards.values()):
            self.grid.addWidget(c, i // cols, i % cols)
        for k in range(cols):
            self.grid.setColumnStretch(k, 1)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if self.cards:
            self.relayout()

    def poll(self) -> None:
        def ok(d: dict) -> None:
            br = d.get("browser", {})
            for bid, card_ in self.cards.items():
                card_.update_state(br.get(bid), self.store.state_of(bid)[0])
            if d.get("browser_error"):
                self.err.setText("Browser problem: " + d["browser_error"])
                self.err.show()
            else:
                self.err.hide()
            self.term.load_history(d.get("terminal", []))
            self.banner.setText(self.banner.text().split("  Browser mode:")[0] + f"  Browser mode: {'headless' if d.get('headless') else 'visible window'}.")
        self.api.get("/api/computer", ok)
