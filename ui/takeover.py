"""Take over a Bot's browser like a remote desktop, record a demonstration (Follow along), then hand it back."""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeyEvent, QPainter, QPixmap
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMessageBox, QVBoxLayout, QWidget)

from . import theme
from .api import Api
from .store import Store
from .widgets import button, label

KEYMAP = {
    Qt.Key.Key_Return: "Enter", Qt.Key.Key_Enter: "Enter", Qt.Key.Key_Backspace: "Backspace", Qt.Key.Key_Tab: "Tab", Qt.Key.Key_Escape: "Escape",
    Qt.Key.Key_Delete: "Delete", Qt.Key.Key_Left: "ArrowLeft", Qt.Key.Key_Right: "ArrowRight", Qt.Key.Key_Up: "ArrowUp", Qt.Key.Key_Down: "ArrowDown",
    Qt.Key.Key_Home: "Home", Qt.Key.Key_End: "End", Qt.Key.Key_PageUp: "PageUp", Qt.Key.Key_PageDown: "PageDown", Qt.Key.Key_Space: " ",
}


class ScreenWidget(QWidget):
    click = Signal(int, int, str, int)
    scroll = Signal(int, int, int, int)
    key = Signal(str)
    text = Signal(str)

    def __init__(self):
        super().__init__()
        self.pm = QPixmap()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(640, 400)
        self._rect = QRect()

    def set_pixmap(self, pm: QPixmap) -> None:
        self.pm = pm
        self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000"))
        if self.pm.isNull():
            p.setPen(QColor("#8b93a7"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Loading the Bot's screen…")
            return
        scaled = self.pm.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        x, y = (self.width() - scaled.width()) // 2, (self.height() - scaled.height()) // 2
        self._rect = QRect(x, y, scaled.width(), scaled.height())
        p.drawPixmap(x, y, scaled)
        if self.hasFocus():
            p.setPen(QColor(theme.palette()["accent"]))
            p.drawRect(self._rect.adjusted(0, 0, -1, -1))

    def _map(self, pos: QPoint) -> tuple[int, int] | None:
        if self.pm.isNull() or not self._rect.contains(pos):
            return None
        sx = self.pm.width() / self._rect.width()
        return int((pos.x() - self._rect.x()) * sx), int((pos.y() - self._rect.y()) * sx)

    def mousePressEvent(self, e) -> None:
        self.setFocus()
        pt = self._map(e.position().toPoint())
        if pt:
            self.click.emit(pt[0], pt[1], "right" if e.button() == Qt.MouseButton.RightButton else "left", 1)

    def mouseDoubleClickEvent(self, e) -> None:
        pt = self._map(e.position().toPoint())
        if pt:
            self.click.emit(pt[0], pt[1], "left", 2)

    def wheelEvent(self, e) -> None:
        pt = self._map(e.position().toPoint()) or (400, 300)
        d = e.angleDelta()
        self.scroll.emit(pt[0], pt[1], int(-d.x()), int(-d.y()))

    def keyPressEvent(self, e: QKeyEvent) -> None:
        mods = e.modifiers()
        k = e.key()
        if k in (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Meta):
            return
        combo = []
        if mods & Qt.KeyboardModifier.ControlModifier:
            combo.append("Control")
        if mods & Qt.KeyboardModifier.AltModifier:
            combo.append("Alt")
        if combo:
            name = KEYMAP.get(k) or (chr(k).lower() if 32 < k < 127 else "")
            if name:
                self.key.emit("+".join(combo + [name]))
            return
        if k in KEYMAP and k != Qt.Key.Key_Space:
            self.key.emit(KEYMAP[k])
        elif e.text() and e.text().isprintable():
            self.text.emit(e.text())


def describe_step(s: dict) -> str:
    a = s.get("action")
    lab = s.get("label") or s.get("name") or s.get("selector") or s.get("tag") or ""
    return {"navigate": f"Open {s.get('url', '')}", "click": f"Click {lab}", "type": f"Type “{s.get('value', '')}” in {lab}", "select": f"Select “{s.get('value', '')}”",
            "toggle": f"Toggle {lab}", "press": f"Press {s.get('key', '')}", "submit": "Submit form", "note": f"Note: {s.get('text', '')}"}.get(a, str(a))


class TakeoverView(QDialog):
    skillDrafted = Signal(str)
    closedView = Signal(str)

    def __init__(self, api: Api, store: Store, bot_id: str, parent=None):
        super().__init__(parent)
        self.api, self.store, self.bot_id = api, store, bot_id
        self.bot = store.bot(bot_id) or {"name": "Bot", "emoji": "🤖"}
        self.setWindowTitle(f"{self.bot['emoji']} {self.bot['name']}'s browser")
        self.resize(1180, 760)
        self.setModal(False)
        self.queue: list[dict] = []
        self.sending = False
        self.fetching = False
        self.recording = False
        self.rec_id = ""
        self.finished_handback = False

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)
        bar = QHBoxLayout()
        bar.addWidget(button("‹", on=lambda: self._send({"type": "back"})))
        bar.addWidget(button("⟳", on=lambda: self._send({"type": "reload"})))
        self.url = QLineEdit()
        self.url.setPlaceholderText("Go to a URL")
        self.url.returnPressed.connect(self._go)
        bar.addWidget(self.url, 1)
        bar.addWidget(button("Go", on=self._go))
        self.btn_rec = button("● Follow along", on=self.toggle_record)
        self.btn_rec.setToolTip("Record what you do so the Bot can learn it as a skill")
        bar.addWidget(self.btn_rec)
        v.addLayout(bar)

        mid = QHBoxLayout()
        self.screen = ScreenWidget()
        self.screen.click.connect(lambda x, y, b, c: self._send({"type": "click", "x": x, "y": y, "button": b, "count": c}))
        self.screen.scroll.connect(lambda x, y, dx, dy: self._send({"type": "scroll", "x": x, "y": y, "dx": dx, "dy": dy}))
        self.screen.key.connect(lambda k: self._send({"type": "key", "key": k}))
        self.screen.text.connect(lambda t: self._send({"type": "text", "text": t}))
        mid.addWidget(self.screen, 1)

        self.side = QWidget()
        self.side.setFixedWidth(270)
        sv = QVBoxLayout(self.side)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.addWidget(label("Recording", h2=True))
        sv.addWidget(label("Do the job once, normally. Passwords you type are never recorded. Add notes for anything the Bot should know.", muted=True))
        self.steps = QListWidget()
        self.steps.setWordWrap(True)
        sv.addWidget(self.steps, 1)
        self.note = QLineEdit()
        self.note.setPlaceholderText("Add a note, e.g. “always pick the 2nd option”")
        self.note.returnPressed.connect(self._add_note)
        sv.addWidget(self.note)
        sv.addWidget(button("Add note", on=self._add_note))
        self.side.hide()
        mid.addWidget(self.side)
        v.addLayout(mid, 1)

        foot = QHBoxLayout()
        self.info = label(f"You are controlling {self.bot['name']}'s browser. The Bot is paused until you hand it back.  Click the page, then type.", muted=True)
        foot.addWidget(self.info, 1)
        foot.addWidget(button(f"Hand back to {self.bot['name']}", primary=True, on=self.hand_back))
        v.addLayout(foot)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.url_timer = QTimer(self)
        self.url_timer.timeout.connect(self.refresh_url)
        store.event.connect(self.on_event)

    # --------------------------------------------------------------- lifecycle
    def begin(self) -> None:
        self.finished_handback = False
        self.api.post(f"/api/bots/{self.bot_id}/takeover", {"action": "start"}, lambda _: None, lambda e: QMessageBox.warning(self, "Browser", e))
        self.refresh()
        self.refresh_url()
        self.timer.start(450)
        self.url_timer.start(2000)
        self.show()
        self.raise_()

    def refresh(self) -> None:
        if self.fetching or not self.isVisible():
            return
        self.fetching = True

        def ok(data: bytes) -> None:
            self.fetching = False
            pm = QPixmap()
            if pm.loadFromData(data):
                self.screen.set_pixmap(pm)

        def err(e: str) -> None:
            self.fetching = False
            self.info.setText(e)

        self.api.request("GET", f"/api/bots/{self.bot_id}/screen.jpg", ok, err, params={"q": 62}, raw=True, timeout=30)

    def refresh_url(self) -> None:
        def ok(d: dict) -> None:
            u = (d.get("browser", {}).get(self.bot_id) or {}).get("url", "")
            if u and not self.url.hasFocus():
                self.url.setText(u)
            if d.get("browser_error"):
                self.info.setText(d["browser_error"])
        self.api.get("/api/computer", ok)

    # ---------------------------------------------------------------- input queue
    def _send(self, ev: dict) -> None:
        if self.queue and ev["type"] == "text" and self.queue[-1]["type"] == "text":
            self.queue[-1]["text"] += ev["text"]
        else:
            self.queue.append(ev)
        self._pump()

    def _pump(self) -> None:
        if self.sending or not self.queue:
            return
        self.sending = True
        ev = self.queue.pop(0)

        def done(*_a) -> None:
            self.sending = False
            QTimer.singleShot(60, self.refresh)
            self._pump()
        self.api.post(f"/api/bots/{self.bot_id}/screen/input", ev, done, done)

    def _go(self) -> None:
        u = self.url.text().strip()
        if u:
            self._send({"type": "navigate", "url": u})
            self.screen.setFocus()

    # -------------------------------------------------------------- follow along
    def toggle_record(self) -> None:
        if not self.recording:
            name, ok = QInputDialog.getText(self, "Follow along", "What job are you about to show the Bot?", text="")
            if not ok:
                return
            self.api.post(f"/api/bots/{self.bot_id}/record", {"action": "start", "name": name.strip() or "Untitled demo"}, self._rec_started,
                          lambda e: QMessageBox.warning(self, "Follow along", e))
        else:
            self.stop_record()

    def _rec_started(self, d: dict) -> None:
        self.recording, self.rec_id = True, d.get("id", "")
        self.steps.clear()
        self.side.show()
        self.btn_rec.setText("■ Stop recording")
        self.btn_rec.setProperty("danger", True)
        self.btn_rec.style().unpolish(self.btn_rec)
        self.btn_rec.style().polish(self.btn_rec)

    def _add_note(self) -> None:
        t = self.note.text().strip()
        if t and self.recording:
            self.api.post(f"/api/bots/{self.bot_id}/record", {"action": "note", "text": t})
            self.note.clear()

    def stop_record(self, then=None) -> None:
        def ok(rec: dict) -> None:
            self.recording = False
            self.btn_rec.setText("● Follow along")
            self.btn_rec.setProperty("danger", False)
            self.btn_rec.style().unpolish(self.btn_rec)
            self.btn_rec.style().polish(self.btn_rec)
            self.side.hide()
            n = len(rec.get("steps", []))
            if n and QMessageBox.question(self, "Draft a skill?", f"Recorded {n} steps. Ask {self.bot['name']} to draft a reviewable skill from them?") == QMessageBox.StandardButton.Yes:
                self.info.setText("Drafting the skill…")
                self.api.post(f"/api/recordings/{rec['id']}/draft_skill", {}, self._drafted, lambda e: (self.info.setText(""), QMessageBox.warning(self, "Draft failed", e)))
            if then:
                then()
        self.api.post(f"/api/bots/{self.bot_id}/record", {"action": "stop"}, ok, lambda e: QMessageBox.warning(self, "Follow along", e))

    def _drafted(self, skill: dict) -> None:
        self.info.setText("")
        QMessageBox.information(self, "Skill drafted", f"Saved “{skill['name']}” as a draft. Review, edit and test it under Skills before activating it.")
        self.skillDrafted.emit(skill["name"])

    def on_event(self, ev: dict) -> None:
        if ev.get("type") == "recording_step" and ev.get("bot_id") == self.bot_id and self.recording:
            idx = ev.get("index", self.steps.count())
            text = describe_step(ev["step"])
            if idx < self.steps.count():
                self.steps.item(idx).setText(text)
            else:
                self.steps.addItem(text)
                self.steps.scrollToBottom()
        elif ev.get("type") == "takeover" and ev.get("bot_id") == self.bot_id and not ev.get("active") and not self.finished_handback:
            self.info.setText("The browser was handed back to the Bot.")

    # ------------------------------------------------------------------ closing
    def hand_back(self) -> None:
        if self.recording:
            self.stop_record(then=self._do_handback)
        else:
            self._do_handback()

    def _do_handback(self) -> None:
        self.finished_handback = True
        self.timer.stop()
        self.url_timer.stop()
        self.api.post(f"/api/bots/{self.bot_id}/takeover", {"action": "handback"}, lambda _: self.store.refresh_approvals())
        self.closedView.emit(self.bot_id)
        self.hide()

    def closeEvent(self, e) -> None:
        if not self.finished_handback:
            self.hand_back()
        super().closeEvent(e)
