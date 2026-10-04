"""Model dropdown that detects the models a provider offers (using the saved key) and lets you pick one or type your own."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QCompleter, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from . import theme
from .api import Api
from .widgets import icon_button

_CACHE: dict[tuple[str, str], list[str]] = {}   # (service url, provider id) -> detected model ids


class ModelPicker(QWidget):
    changed = Signal()

    def __init__(self, api: Api, placeholder: str = "Choose or type a model"):
        super().__init__()
        self.api = api
        self.pid = ""
        self._gen = 0
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo.setMaxVisibleItems(14)
        self.combo.lineEdit().setPlaceholderText(placeholder)
        comp = self.combo.completer()
        comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.combo.currentTextChanged.connect(lambda _t: self.changed.emit())
        row.addWidget(self.combo, 1)
        self.refresh_btn = icon_button("refresh", "Detect available models", lambda: self.detect(), size=16)
        row.addWidget(self.refresh_btn)
        v.addLayout(row)
        self.status = QLabel("")
        self.status.setProperty("faint", True)
        self.status.setWordWrap(True)
        v.addWidget(self.status)

    # -- public --------------------------------------------------------------------
    def text(self) -> str:
        return self.combo.currentText().strip()

    def set_text(self, t: str) -> None:
        self.combo.setCurrentText(t)

    def set_provider(self, pid: str, ready: bool, current: str | None = None) -> None:
        """Point at a provider. Shows cached models, or detects them if a key (or no key) is available."""
        cur = self.text() if current is None else current
        self.pid = pid
        self._gen += 1
        cached = _CACHE.get((self.api.conn.base, pid))
        if cached is not None:
            self._fill(cached, cur)
            self._say(f"{len(cached)} models detected")
        else:
            self._fill([], cur)
            if ready:
                self.detect(silent=True)
            else:
                self._say("Save an API key to detect the available models, or type a model name.")

    def invalidate(self) -> None:
        _CACHE.pop((self.api.conn.base, self.pid), None)

    def detect(self, silent: bool = False) -> None:
        pid, gen = self.pid, self._gen
        if not pid:
            return
        self._say("Detecting models…")
        self.refresh_btn.setEnabled(False)

        def ok(d: dict) -> None:
            self.refresh_btn.setEnabled(True)
            models = d.get("models", [])
            _CACHE[(self.api.conn.base, pid)] = models
            if gen == self._gen:
                self._fill(models, self.text())
                self._say(f"{len(models)} models detected" if models else "Connected, but the provider returned no models. Type a model name.")

        def err(e: str) -> None:
            self.refresh_btn.setEnabled(True)
            if gen == self._gen:
                msg = e if len(e) < 140 else e[:137] + "…"
                self._say(("Could not detect models: " + msg) if not silent else "Could not detect models (" + msg + "). You can still type a name.", bad=True)

        self.api.post(f"/api/providers/{pid}/test", {}, ok, err, timeout=60)

    # -- internals -------------------------------------------------------------------
    def _fill(self, models: list[str], current: str) -> None:
        self.combo.blockSignals(True)
        self.combo.clear()
        items = list(models)
        if current and current not in items:
            items.insert(0, current)
        self.combo.addItems(items)
        self.combo.setCurrentText(current)
        self.combo.blockSignals(False)

    def _say(self, text: str, bad: bool = False) -> None:
        p = theme.palette()
        self.status.setStyleSheet(f"color: {p['bad'] if bad else p['faint']}; font-size: 12px;")
        self.status.setText(text)
