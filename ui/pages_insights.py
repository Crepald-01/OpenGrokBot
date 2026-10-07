"""Activity: the action log and Insights (how your Bots are doing over time)."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QListWidget, QListWidgetItem, QScrollArea, QTabWidget, QVBoxLayout, QWidget

from . import theme
from .api import Api
from .pages_inbox import fill_row, fmt_time, make_table
from .pages_usage_log import LogPage, fmt_tokens
from .store import Store
from .widgets import PageHeader, button, card, label, page_layout


def secs(n: float) -> str:
    n = float(n or 0)
    return f"{n:.0f}s" if n < 90 else f"{n / 60:.1f} min" if n < 5400 else f"{n / 3600:.1f} h"


def pct(x) -> str:
    return "—" if x is None else f"{round(x * 100)}%"


class StackedDays(QWidget):
    """Tasks per day: finished well (accent) and with problems (red), stacked."""

    def __init__(self):
        super().__init__()
        self.data: list[dict] = []
        self.setMinimumHeight(150)
        self.setMouseTracking(True)

    def set_data(self, d: list[dict]) -> None:
        self.data = d
        self.update()

    def _geom(self):
        n = max(1, len(self.data))
        return self.width() / n, self.height() - 26

    def paintEvent(self, _e) -> None:
        if not self.data:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = theme.palette()
        w, h = self._geom()
        mx = max([d["tasks"] for d in self.data] + [1])
        step = max(1, len(self.data) // 7)
        for i, d in enumerate(self.data):
            x = i * w + w * 0.18
            bw = w * 0.64
            ok_h = h * d["ok"] / mx
            bad_h = h * d["problems"] / mx
            p.setPen(Qt.PenStyle.NoPen)
            if d["tasks"] == 0:
                p.setBrush(QColor(pal["line2"]))
                p.drawRoundedRect(QRectF(x, h - 2, bw, 2), 1, 1)
            else:
                p.setBrush(QColor(pal["accent"]))
                p.drawRoundedRect(QRectF(x, h - ok_h, bw, max(ok_h, 2)), 3, 3)
                if bad_h:
                    p.setBrush(QColor(pal["bad"]))
                    p.drawRoundedRect(QRectF(x, h - ok_h - bad_h, bw, bad_h), 3, 3)
            if i % step == 0 or len(self.data) <= 8:
                p.setPen(QColor(pal["muted"]))
                p.drawText(QRectF(i * w - 6, h + 4, w + 12, 18), Qt.AlignmentFlag.AlignCenter, d["day"][5:].replace("-", "/") if len(self.data) > 8 else d["day"][5:])

    def mouseMoveEvent(self, e) -> None:
        if not self.data:
            return
        w, _h = self._geom()
        i = max(0, min(len(self.data) - 1, int(e.position().x() // w)))
        d = self.data[i]
        self.setToolTip(f"{d['day']}: {d['tasks']} task{'s' if d['tasks'] != 1 else ''} ({d['ok']} finished, {d['problems']} with problems) · {fmt_tokens(d['tokens'])} tokens")


class HourStrip(QWidget):
    """When your Bots are busiest: 24 small bars, midnight to midnight."""

    def __init__(self):
        super().__init__()
        self.hours = [0] * 24
        self.setFixedHeight(64)
        self.setMouseTracking(True)

    def set_data(self, hours: list[int]) -> None:
        self.hours = hours
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = theme.palette()
        w = self.width() / 24
        mx = max(self.hours + [1])
        h = self.height() - 18
        p.setPen(Qt.PenStyle.NoPen)
        for i, n in enumerate(self.hours):
            bh = max(2.0, h * n / mx) if n else 2.0
            col = QColor(pal["accent"] if n else pal["line2"])
            if n:
                col.setAlphaF(0.35 + 0.65 * n / mx)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(i * w + w * 0.15, h - bh, w * 0.7, bh), 2, 2)
        p.setPen(QColor(pal["muted"]))
        for hr in (0, 6, 12, 18, 23):
            p.drawText(QRectF(hr * w - 10, h + 2, w + 20, 16), Qt.AlignmentFlag.AlignCenter, f"{hr:02d}")

    def mouseMoveEvent(self, e) -> None:
        i = max(0, min(23, int(e.position().x() // (self.width() / 24))))
        self.setToolTip(f"{i:02d}:00 to {i:02d}:59: {self.hours[i]} task{'s' if self.hours[i] != 1 else ''}")


class InsightsTab(QWidget):
    def __init__(self, api: Api, store: Store):
        super().__init__()
        self.api, self.store = api, store
        self.days = 7
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 12, 0, 0)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QScrollArea.Shape.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 10, 0)
        v.setSpacing(16)
        sc.setWidget(body)
        outer.addWidget(sc)

        bar = QHBoxLayout()
        self.range_btns = {}
        for d, text in ((7, "7 days"), (30, "30 days"), (90, "90 days")):
            b = button(text, on=lambda d=d: self.set_days(d))
            self.range_btns[d] = b
            bar.addWidget(b)
        self.bot = QComboBox()
        self.bot.currentIndexChanged.connect(lambda _i: self.load())
        bar.addSpacing(12)
        bar.addWidget(self.bot)
        bar.addStretch(1)
        v.addLayout(bar)

        top = QHBoxLayout()
        self.c_tasks = self._stat("Tasks", "0")
        self.c_ok = self._stat("Finished well", "—")
        self.c_time = self._stat("Typical task", "—")
        self.c_tokens = self._stat("Tokens", "0")
        self.c_busy = self._stat("Busiest hour", "—")
        for c in (self.c_tasks, self.c_ok, self.c_time, self.c_tokens, self.c_busy):
            top.addWidget(c[0])
        v.addLayout(top)

        v.addWidget(label("TASKS PER DAY", eyebrow=True))
        self.daily = StackedDays()
        v.addWidget(self.daily)
        legend = QHBoxLayout()
        legend.addWidget(label("● finished", wrap=False))
        legend.itemAt(0).widget().setStyleSheet(f"color: {theme.palette()['accent']};")
        legend.addWidget(label("● with problems", wrap=False))
        legend.itemAt(1).widget().setStyleSheet(f"color: {theme.palette()['bad']};")
        legend.addStretch(1)
        v.addLayout(legend)
        v.addWidget(label("WHEN THEY WORK", eyebrow=True))
        self.hours = HourStrip()
        v.addWidget(self.hours)

        v.addWidget(label("BY BOT", eyebrow=True))
        self.bots = make_table(["Bot", "Tasks", "Finished well", "Typical task", "Tokens"], 0)
        v.addWidget(self.bots)
        v.addWidget(label("MOST USED TOOLS", eyebrow=True))
        self.tools = make_table(["Tool", "Uses", "Errors", "Denied", "Average"], 0)
        v.addWidget(self.tools)
        v.addWidget(label("RECENT PROBLEMS", eyebrow=True))
        self.problems = QListWidget()
        self.problems.setMinimumHeight(120)
        v.addWidget(self.problems)
        self._style()

    def _stat(self, title: str, value: str):
        c = card()
        l = QVBoxLayout(c)
        l.setContentsMargins(14, 10, 14, 10)
        l.addWidget(label(title, muted=True))
        val = label(value, h1=True)
        l.addWidget(val)
        return c, val

    def _style(self) -> None:
        for d, b in self.range_btns.items():
            b.setProperty("primary", d == self.days)
            b.style().unpolish(b)
            b.style().polish(b)

    def set_days(self, d: int) -> None:
        self.days = d
        self._style()
        self.load()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        cur = self.bot.currentData() or ""
        self.bot.blockSignals(True)
        self.bot.clear()
        self.bot.addItem("All Bots", "")
        for b in self.store.bots:
            self.bot.addItem(f"{b.get('emoji') or '🤖'}  {b['name']}", b["id"])
        self.bot.setCurrentIndex(max(0, self.bot.findData(cur)))
        self.bot.blockSignals(False)
        self.load()

    def load(self) -> None:
        params = {"days": self.days}
        if self.bot.currentData():
            params["bot_id"] = self.bot.currentData()
        self.api.get("/api/insights", self._got, lambda m: self.c_tasks[1].setText("?"), params=params)

    def _got(self, d: dict) -> None:
        t = d["totals"]
        self.c_tasks[1].setText(str(t["tasks"]))
        self.c_ok[1].setText(pct(t["success"]))
        self.c_time[1].setText(secs(t["median_seconds"]) if t["tasks"] else "—")
        self.c_tokens[1].setText(fmt_tokens(t["tokens"]))
        self.c_busy[1].setText("—" if t["busiest_hour"] is None else f"{t['busiest_hour']:02d}:00")
        self.c_ok[0].setToolTip(f"{t['ok']} of {t['tasks']} tasks finished without an error or running out of steps")
        self.daily.set_data(d["per_day"])
        self.hours.set_data(d["hours"])
        self.bots.setRowCount(0)
        for b in d["bots"]:
            fill_row(self.bots, [f"{b['emoji']} {b['name']}".strip(), b["tasks"], pct(b["success"]), secs(b["avg_seconds"]) if b["tasks"] else "—", fmt_tokens(b["tokens"])])
        self.tools.setRowCount(0)
        for x in d["tools"]:
            fill_row(self.tools, [x["tool"], x["n"], x["errors"] or "", x["denied"] or "", f"{x['avg_ms']} ms" if x["avg_ms"] is not None else ""])
        from .pages_files import ago
        self.problems.clear()
        for pr in d["problems"]:
            it = QListWidgetItem(f"{pr['bot_name'] or 'Bot'} · {pr['tool']} · {ago(pr['ts'])}\n{' '.join((pr['text'] or '').split())[:160]}")
            self.problems.addItem(it)
        if not d["problems"]:
            it = QListWidgetItem("No tool errors in this period.")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.problems.addItem(it)
        for tb in (self.bots, self.tools):
            rows = max(2, min(8, tb.rowCount()))
            tb.setFixedHeight(tb.horizontalHeader().height() + tb.verticalHeader().defaultSectionSize() * rows + 6)


class ActivityPage(QWidget):
    """The action log and Insights together."""

    def __init__(self, api: Api, store: Store):
        super().__init__()
        v = page_layout(self, PageHeader("Activity", "Every tool call, page visited and file touched, and how your Bots are doing over time. Secrets are scrubbed before anything is written."))
        self.tabs = QTabWidget()
        self.log = LogPage(api, store, embedded=True)
        self.insights = InsightsTab(api, store)
        self.tabs.addTab(self.log, "Action log")
        self.tabs.addTab(self.insights, "Insights")
        v.addWidget(self.tabs, 1)
