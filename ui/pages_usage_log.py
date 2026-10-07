"""Usage (weekly, per Bot) and the full action log per Bot."""
from __future__ import annotations

import json
import time

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLineEdit, QMessageBox, QProgressBar, QPushButton,
                               QScrollArea, QSpinBox, QSplitter, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget)

from . import theme
from .api import Api
from .pages_inbox import fill_row, fmt_time, make_table
from core.pricing import fmt_money
from .store import Store
from .widgets import button, card, label, PageHeader, page_layout


def fmt_tokens(n: int) -> str:
    return f"{n / 1_000_000:.2f}M" if n >= 1_000_000 else (f"{n / 1000:.1f}k" if n >= 1000 else str(n))


def fit_height(t, min_rows: int = 2, max_rows: int = 8) -> None:
    """Make a table exactly as tall as its rows (between min_rows and max_rows), so it neither hides rows nor leaves a blank gap."""
    rows = min(max_rows, max(min_rows, t.rowCount()))
    t.setFixedHeight(t.horizontalHeader().height() + t.verticalHeader().defaultSectionSize() * rows + 6)


class DailyBars(QWidget):
    def __init__(self):
        super().__init__()
        self.data: list[dict] = []
        self.setMinimumHeight(120)

    def set_data(self, d: list[dict]) -> None:
        self.data = d
        self.update()

    def paintEvent(self, e) -> None:
        if not self.data:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = theme.palette()
        mx = max([d["tokens"] for d in self.data] + [1])
        w = self.width() / len(self.data)
        for i, d in enumerate(self.data):
            h = (self.height() - 28) * d["tokens"] / mx
            p.setBrush(QColor(pal["accent"]))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(i * w + w * 0.2, self.height() - 20 - h, w * 0.6, max(h, 2)), 4, 4)
            p.setPen(QColor(pal["muted"]))
            p.drawText(QRectF(i * w, self.height() - 18, w, 16), Qt.AlignmentFlag.AlignCenter, d["day"])


class UsagePage(QWidget):
    def __init__(self, api: Api, store: Store):
        super().__init__()
        self.api, self.store = api, store
        outer = page_layout(self, PageHeader("Usage", "Tracked locally from the token counts your providers report. It resets weekly."))
        # the page is taller than a small window, so it scrolls instead of squeezing the tables to a row or two
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 10, 0)
        v.setSpacing(16)
        sc.setWidget(body)
        outer.addWidget(sc, 1)
        top = QHBoxLayout()
        self.c_total = self._stat("This week", "0")
        self.c_in = self._stat("Input tokens", "0")
        self.c_out = self._stat("Output tokens", "0")
        self.c_reset = self._stat("Resets in", "—")
        self.c_cost = self._stat("Est. cost", "—")
        for c in (self.c_total, self.c_in, self.c_out, self.c_cost, self.c_reset):
            top.addWidget(c[0])
        v.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.limit_label = label("", muted=True)
        v.addWidget(self.bar)
        v.addWidget(self.limit_label)
        v.addWidget(label("TOKENS BY DAY", eyebrow=True))
        self.daily = DailyBars()
        v.addWidget(self.daily)
        v.addWidget(label("BY BOT", eyebrow=True))
        self.table = make_table(["Bot", "Tasks", "Input", "Output", "Total", "Est. cost"], 0)
        v.addWidget(self.table)
        v.addWidget(label("BY MODEL AND PRICES (per million tokens). Enter what your provider charges; free and local models count as free.", eyebrow=True))
        self.prices = make_table(["Model", "Tokens in", "Tokens out", "Input price", "Output price", "Est. cost"], 0)
        self.prices.setEditTriggers(self.prices.EditTrigger.DoubleClicked | self.prices.EditTrigger.EditKeyPressed | self.prices.EditTrigger.AnyKeyPressed)
        self.prices.itemChanged.connect(lambda _it: setattr(self, "_price_dirty", True))
        self._price_dirty = False
        v.addWidget(self.prices)
        prow = QHBoxLayout()
        prow.addWidget(label("Currency", muted=True, wrap=False))
        self.currency = QLineEdit("$")
        self.currency.setMaximumWidth(60)
        self.currency.textChanged.connect(lambda _t: setattr(self, "_price_dirty", True))
        prow.addWidget(self.currency)
        prow.addWidget(button("Save prices", on=self.save_prices))
        self.price_msg = label("", faint=True, wrap=False)
        prow.addWidget(self.price_msg, 1)
        v.addLayout(prow)
        row = QHBoxLayout()
        row.addWidget(label("Weekly limit (tokens, 0 = none)", muted=True, wrap=False))
        self.limit = QSpinBox()
        self.limit.setRange(0, 2_000_000_000)
        self.limit.setSingleStep(100_000)
        row.addWidget(self.limit)
        self.day = QComboBox()
        for i, d in enumerate(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]):
            self.day.addItem("Resets on " + d, i)
        row.addWidget(self.day)
        row.addWidget(button("Save", primary=True, on=self.save))
        row.addStretch(1)
        v.addLayout(row)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.load)

    def _stat(self, title: str, value: str):
        c = card()
        l = QVBoxLayout(c)
        l.setContentsMargins(14, 10, 14, 10)
        l.addWidget(label(title, muted=True))
        val = label(value, h1=True)
        l.addWidget(val)
        return c, val

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.load()
        self.timer.start(8000)

    def hideEvent(self, e) -> None:
        super().hideEvent(e)
        self.timer.stop()

    def load(self) -> None:
        def ok(d: dict) -> None:
            self.c_total[1].setText(fmt_tokens(d["total"]))
            self.c_in[1].setText(fmt_tokens(d["input_tokens"]))
            self.c_out[1].setText(fmt_tokens(d["output_tokens"]))
            left = max(0, d["resets_at"] - time.time())
            self.c_reset[1].setText(f"{int(left // 86400)}d {int(left % 86400 // 3600)}h")
            if d["limit"]:
                self.bar.setRange(0, d["limit"])
                self.bar.setValue(min(d["total"], d["limit"]))
                self.limit_label.setText(f"{fmt_tokens(d['total'])} of {fmt_tokens(d['limit'])} ({100 * d['total'] // d['limit']}%). Bots pause at the limit.")
            else:
                self.bar.setRange(0, 1)
                self.bar.setValue(0)
                self.limit_label.setText("No weekly limit set.")
            self.daily.set_data(d["daily"])
            cost = d.get("cost", {})
            cur = cost.get("currency", "$")
            self.c_cost[1].setText(fmt_money(cost.get("total", 0), cur) + ("+" if cost.get("unpriced") else ""))
            self.c_cost[0].setToolTip("Based on the prices below. A + means some models have no price yet." if cost.get("unpriced") else "Based on the prices below.")
            self.table.setRowCount(0)
            for r in d["per_bot"]:
                bc = cost.get("per_bot", {}).get(r["bot_id"])
                fill_row(self.table, [f"{r['emoji']} {r['name']}", r["turns"], fmt_tokens(r["input_tokens"] or 0), fmt_tokens(r["output_tokens"] or 0), fmt_tokens((r["input_tokens"] or 0) + (r["output_tokens"] or 0)),
                                      fmt_money(bc, cur) if bc is not None else "—"])
            fit_height(self.table)
            if not self._price_dirty:
                self._fill_prices(cost)
            if not self.limit.hasFocus():
                self.limit.setValue(d["limit"] if not (self.store.status.get("admin", {}).get("policy", {}).get("weekly_token_limit")) else self.store.settings.get("usage", {}).get("weekly_token_limit", 0))
            self.day.setCurrentIndex(int(self.store.settings.get("usage", {}).get("reset_weekday", 0)))
        self.api.get("/api/usage", ok)

    def _fill_prices(self, cost: dict) -> None:
        cur = cost.get("currency", "$")
        self.prices.blockSignals(True)
        self.prices.setRowCount(0)
        rows = {m["key"]: m for m in cost.get("per_model", [])}
        for k in cost.get("prices", {}):
            rows.setdefault(k, {"key": k, "cost": 0.0, "priced": True})
        table = cost.get("prices", {})
        for k, m in sorted(rows.items()):
            p = table.get(k)
            free = m.get("priced") and p is None
            r = fill_row(self.prices, [k, fmt_tokens(m.get("input_tokens", 0)), fmt_tokens(m.get("output_tokens", 0)), "" if p is None else f"{p['in']:g}",
                                       "" if p is None else f"{p['out']:g}", fmt_money(m.get("cost"), cur) if m.get("priced") else "no price"], k)
            if free:
                self.prices.item(r, 3).setText("free")
                self.prices.item(r, 4).setText("free")
            for c in (0, 1, 2, 5):
                it = self.prices.item(r, c)
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
        fit_height(self.prices)
        self.currency.blockSignals(True)
        self.currency.setText(cur)
        self.currency.blockSignals(False)
        self.prices.blockSignals(False)

    def save_prices(self) -> None:
        models = {}
        for r in range(self.prices.rowCount()):
            key = self.prices.item(r, 0).data(Qt.ItemDataRole.UserRole)
            a, b = self.prices.item(r, 3).text().strip(), self.prices.item(r, 4).text().strip()
            if a.lower() == "free" and b.lower() == "free" or (not a and not b):
                continue
            # a row that shows "free" in one box and a number in the other: "free" means 0 there
            a, b = ("0" if a.lower() == "free" else a), ("0" if b.lower() == "free" else b)
            try:
                models[key] = {"in": float(a or 0), "out": float(b or 0)}
            except ValueError:
                self.price_msg.setText(f"“{a}” / “{b}” is not a number (row {r + 1}).")
                return

        def ok(_c: dict) -> None:
            self._price_dirty = False
            self.price_msg.setText("Saved.")
            self.load()
        self.api.put("/api/pricing", {"models": models, "currency": self.currency.text().strip() or "$"}, ok, lambda e: self.price_msg.setText(e))

    def save(self) -> None:
        self.api.put("/api/settings", {"usage.weekly_token_limit": self.limit.value(), "usage.reset_weekday": self.day.currentData()}, lambda s: (setattr(self.store, "settings", s), self.load()))


class LogPage(QWidget):
    def __init__(self, api: Api, store: Store):
        super().__init__()
        self.api, self.store = api, store
        v = page_layout(self, PageHeader("Action log", "Every tool call, page visited and file touched, per Bot. Secrets are scrubbed before they are written."))
        bar = QHBoxLayout()
        self.bot = QComboBox()
        self.status = QComboBox()
        for t, k in (("All results", ""), ("OK", "ok"), ("Errors", "error"), ("Denied", "denied"), ("Blocked by policy", "blocked")):
            self.status.addItem(t, k)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by tool name…")
        bar.addWidget(self.bot)
        bar.addWidget(self.status)
        bar.addWidget(self.search, 1)
        bar.addWidget(button("⟳", on=self.load))
        bar.addWidget(button("Export JSON", on=lambda: self.export("json")))
        bar.addWidget(button("Export CSV", on=lambda: self.export("csv")))
        v.addLayout(bar)
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = make_table(["Time", "Bot", "Tool", "Result", "Page / file", "ms"], 4)
        self.table.itemSelectionChanged.connect(self.detail)
        split.addWidget(self.table)
        self.detail_view = QTextBrowser()
        self.detail_view.setMaximumHeight(200)
        split.addWidget(self.detail_view)
        split.setSizes([480, 160])
        v.addWidget(split, 1)
        self.bot.currentIndexChanged.connect(lambda _: self.load())
        self.status.currentIndexChanged.connect(lambda _: self.load())
        self.search.returnPressed.connect(self.load)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.load)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        cur = self.bot.currentData()
        self.bot.blockSignals(True)
        self.bot.clear()
        self.bot.addItem("All Bots", "")
        for b in self.store.bots:
            self.bot.addItem(f"{b['emoji']} {b['name']}", b["id"])
        self.bot.setCurrentIndex(max(0, self.bot.findData(cur or "")))
        self.bot.blockSignals(False)
        self.load()
        self.timer.start(5000)

    def hideEvent(self, e) -> None:
        super().hideEvent(e)
        self.timer.stop()

    def load(self) -> None:
        params = {"limit": 400}
        if self.bot.currentData():
            params["bot_id"] = self.bot.currentData()
        if self.status.currentData():
            params["status"] = self.status.currentData()
        if self.search.text().strip():
            params["tool"] = self.search.text().strip()

        def ok(rows: list) -> None:
            self.table.setRowCount(0)
            for a in rows:
                fill_row(self.table, [fmt_time(a["ts"]), self.store.bot_name(a["bot_id"]), a["tool"], a["status"], a["url"] or a["path"] or "", a["duration_ms"]], a)
        self.api.get("/api/actions", ok, params=params)

    def detail(self) -> None:
        r = self.table.currentRow()
        if r < 0:
            return
        a = self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
        self.detail_view.setPlainText(f"{a['tool']}  [{a['status']}]  {a['category'] or ''}\n\nARGS\n{a['args']}\n\nRESULT\n{a['result']}")

    def export(self, fmt: str) -> None:
        name = f"action-log.{fmt}"
        path, _ = QFileDialog.getSaveFileName(self, "Export action log", name, "JSON (*.json)" if fmt == "json" else "CSV (*.csv)")
        if not path:
            return
        params = {"format": fmt}
        if self.bot.currentData():
            params["bot_id"] = self.bot.currentData()

        def ok(data: bytes) -> None:
            with open(path, "wb") as f:
                f.write(data)
            QMessageBox.information(self, "Exported", f"Saved {path}")
        self.api.request("GET", "/api/actions/export", ok, lambda e: QMessageBox.warning(self, "Export failed", e), params=params, raw=True)
