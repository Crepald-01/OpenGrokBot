"""Quick Ask: a small box to hand any Bot a message without opening its chat. Ctrl+J inside the app, and an optional
system-wide shortcut (Ctrl+Alt+Space) that works even when the window is closed to the tray."""
from __future__ import annotations

import os

from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout

from . import theme
from .api import Api, load_ui_config, save_ui_config
from .store import Store
from .widgets import button, label

WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
VK_SPACE = 0x20
HOTKEY_ID = 0x4F47   # "OG"


class GlobalHotkey(QObject, QAbstractNativeEventFilter):
    """Ctrl+Alt+Space for the whole desktop (Windows). register() returns False when another app already owns the keys."""
    triggered = Signal()

    def __init__(self) -> None:
        QObject.__init__(self)
        QAbstractNativeEventFilter.__init__(self)
        self.active = False

    def register(self) -> bool:
        if os.name != "nt":
            return False
        if self.active:
            return True
        import ctypes
        ok = bool(ctypes.windll.user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_SPACE))
        if ok:
            QApplication.instance().installNativeEventFilter(self)
            self.active = True
        return ok

    def unregister(self) -> None:
        if os.name != "nt" or not self.active:
            return
        import ctypes
        ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
        QApplication.instance().removeNativeEventFilter(self)
        self.active = False

    def nativeEventFilter(self, event_type, message):  # noqa: N802 (Qt API name)
        if self.active and bytes(event_type) == b"windows_generic_MSG":
            import ctypes
            from ctypes import wintypes
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self.triggered.emit()
                return True, 0
        return False, 0


class QuickAsk(QDialog):
    sent = Signal(str)   # a short confirmation line

    def __init__(self, api: Api, store: Store, parent=None):
        super().__init__(parent)
        self.api, self.store = api, store
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.resize(620, 230)
        p = theme.palette()
        self.setStyleSheet(f"QDialog {{ background: {p['panel']}; border: 1px solid {p['line2']}; border-radius: 16px; }}")
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 14)
        v.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(label("Quick ask", h2=True, wrap=False))
        top.addStretch(1)
        self.bots = QComboBox()
        self.bots.setMinimumWidth(220)
        top.addWidget(self.bots)
        v.addLayout(top)
        self.text = QPlainTextEdit()
        self.text.setFixedHeight(96)
        self.text.installEventFilter(self)
        v.addWidget(self.text)
        foot = QHBoxLayout()
        self.hint = label("Enter to send  ·  Shift+Enter for a new line  ·  Esc to close  ·  starts with / for a command", faint=True, wrap=False)
        foot.addWidget(self.hint, 1)
        self.send_btn = button("Send", primary=True, on=self.send)
        foot.addWidget(self.send_btn)
        v.addLayout(foot)
        self.err = QLabel("")
        self.err.setStyleSheet(f"color: {p['bad']};")
        self.err.hide()
        v.addWidget(self.err)
        self.bots.currentIndexChanged.connect(self._bot_changed)

    def open(self, preferred_bot: str = "") -> None:
        cfg = load_ui_config()
        want = preferred_bot or cfg.get("quick_bot", "")
        self.bots.blockSignals(True)
        self.bots.clear()
        for b in self.store.bots:
            self.bots.addItem(f"{b.get('emoji') or '🤖'}  {b['name']}" + ("  (paused)" if b["paused"] else ""), b["id"])
        self.bots.setCurrentIndex(max(0, self.bots.findData(want)))
        self.bots.blockSignals(False)
        self._bot_changed()
        self.err.hide()
        self.text.clear()
        self.send_btn.setEnabled(bool(self.store.bots))
        if not self.store.bots:
            self._fail("Create a Bot first.")
        self.show()
        self.raise_()
        self.activateWindow()
        self.text.setFocus()

    def _bot_changed(self) -> None:
        self.text.setPlaceholderText(f"Ask {self.bots.currentText().split('  ', 1)[-1].replace('  (paused)', '') or 'a Bot'} something…")

    def eventFilter(self, obj, e) -> bool:
        if obj is self.text and e.type() == QEvent.Type.KeyPress:
            if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                self.send()
                return True
            if e.key() == Qt.Key.Key_Escape:
                self.close()
                return True
        return super().eventFilter(obj, e)

    def _fail(self, msg: str) -> None:
        self.err.setText(msg)
        self.err.show()
        self.send_btn.setEnabled(bool(self.store.bots))

    def send(self) -> None:
        text = self.text.toPlainText().strip()
        bid = self.bots.currentData()
        if not text or not bid:
            return
        self.send_btn.setEnabled(False)
        self.err.hide()
        name = self.bots.currentText().split("  ", 1)[-1].replace("  (paused)", "")

        def got_threads(rows: list) -> None:
            main = next((t for t in rows if t.get("main")), rows[0] if rows else None)
            if not main:
                self._fail("That Bot has no chat yet. Open it once first.")
                return
            self.api.post(f"/api/threads/{main['id']}/messages", {"text": text}, lambda _r: self._done(bid, name), self._fail)
        self.api.get(f"/api/bots/{bid}/threads", got_threads, self._fail)

    def _done(self, bot_id: str, name: str) -> None:
        cfg = load_ui_config()
        cfg["quick_bot"] = bot_id
        save_ui_config(cfg)
        self.sent.emit(f"Sent to {name}.")
        self.close()
