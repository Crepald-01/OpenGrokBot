"""Home: one glance at the whole team. What needs you, who is working, how much of the week is used, what just happened."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QProgressBar, QScrollArea, QVBoxLayout, QWidget

from . import theme
from .api import Api
from .pages_inbox import fmt_time
from .pages_usage_log import fmt_tokens
from .store import Store
from .widgets import Avatar, button, card, clear_layout, label, repolish

STATE_TEXT = {"idle": "Idle", "work": "Working", "wait": "Needs you", "takeover": "You're driving"}


def greeting() -> str:
    h = datetime.now().hour
    return "Good morning" if 5 <= h < 12 else "Good afternoon" if 12 <= h < 18 else "Good evening"


class Tile(QFrame):
    """A big number with a caption. Clickable; turns amber when `hot` (something is waiting on you)."""
    clicked = Signal()

    def __init__(self, caption: str):
        super().__init__()
        self.setProperty("card", "tile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        v = QVBoxLayout(self)
        v.setContentsMargins(18, theme.dp(14), 18, theme.dp(14))
        v.setSpacing(2)
        self.caption = QLabel(caption.upper())
        self.caption.setProperty("eyebrow", True)
        self.value = QLabel("0")
        self.value.setStyleSheet(f"font-size: {theme.base_size() + 15}px; font-weight: 600; letter-spacing: -0.5px;")
        self.sub = QLabel("")
        self.sub.setProperty("faint", True)
        self.sub.setWordWrap(True)
        for w in (self.caption, self.value, self.sub):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            v.addWidget(w)
        self.bar: QProgressBar | None = None

    def set(self, value: str, sub: str = "", hot: bool = False) -> None:
        self.value.setText(value)
        self.sub.setText(sub)
        self.setProperty("hot", hot)
        repolish(self)

    def add_bar(self) -> QProgressBar:
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.layout().addWidget(self.bar)  # type: ignore[union-attr]
        self.bar.hide()
        return self.bar

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()


class HomePage(QWidget):
    openBot = Signal(str)
    openThread = Signal(str, str)
    openPage = Signal(str)
    newBot = Signal()

    def __init__(self, api: Api, store: Store):
        super().__init__()
        self.api, self.store = api, store
        self.actions: list[dict] = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(sc)
        body = QWidget()
        sc.setWidget(body)
        v = QVBoxLayout(body)
        v.setContentsMargins(36, 30, 36, 28)
        v.setSpacing(theme.dp(20))

        head = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(4)
        self.hello = label("", h1=True, wrap=False)
        self.summary = label("", muted=True)
        col.addWidget(self.hello)
        col.addWidget(self.summary)
        head.addLayout(col, 1)
        head.addWidget(button("New Bot", primary=True, icon="plus", on=self.newBot.emit), 0, Qt.AlignmentFlag.AlignTop)
        v.addLayout(head)

        tiles = QHBoxLayout()
        tiles.setSpacing(14)
        self.t_need, self.t_work, self.t_tokens = Tile("Needs you"), Tile("Working now"), Tile("Tokens this week")
        self.t_need.clicked.connect(lambda: self.openPage.emit("inbox"))
        self.t_work.clicked.connect(lambda: self.openPage.emit("computer"))
        self.t_tokens.clicked.connect(lambda: self.openPage.emit("usage"))
        self.token_bar = self.t_tokens.add_bar()
        for t in (self.t_need, self.t_work, self.t_tokens):
            tiles.addWidget(t, 1)
        v.addLayout(tiles)

        cols = QHBoxLayout()
        cols.setSpacing(24)
        left = QVBoxLayout()
        left.setSpacing(10)
        left.addWidget(label("YOUR TEAM", eyebrow=True))
        self.team = QGridLayout()
        self.team.setSpacing(12)
        self.team.setColumnStretch(0, 1)
        self.team.setColumnStretch(1, 1)
        left.addLayout(self.team)
        left.addStretch(1)
        cols.addLayout(left, 3)
        right = QVBoxLayout()
        right.setSpacing(10)
        self.need_head = label("NEEDS YOU", eyebrow=True)
        right.addWidget(self.need_head)
        self.need_box = QVBoxLayout()
        self.need_box.setSpacing(8)
        right.addLayout(self.need_box)
        right.addSpacing(8)
        right.addWidget(label("RECENT ACTIVITY", eyebrow=True))
        self.feed_card = card()
        self.feed = QVBoxLayout(self.feed_card)
        self.feed.setContentsMargins(14, 8, 14, 8)
        self.feed.setSpacing(0)
        right.addWidget(self.feed_card)
        right.addStretch(1)
        cols.addLayout(right, 2)
        v.addLayout(cols, 1)

        for sig in (store.botsChanged, store.busyChanged, store.approvalsChanged):
            sig.connect(self.render)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.load)
        self.render()

    # ------------------------------------------------------------------ data
    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.load()
        self.timer.start(10000)

    def hideEvent(self, e) -> None:
        super().hideEvent(e)
        self.timer.stop()

    def load(self) -> None:
        def usage(d: dict) -> None:
            lim, tot = d.get("limit", 0), d.get("total", 0)
            if lim:
                self.t_tokens.set(fmt_tokens(tot), f"of {fmt_tokens(lim)} ({100 * tot // lim}%)")
                self.token_bar.setRange(0, lim)
                self.token_bar.setValue(min(tot, lim))
                self.token_bar.show()
            else:
                self.t_tokens.set(fmt_tokens(tot), "no weekly limit set")
                self.token_bar.hide()

        def feed(rows: list) -> None:
            self.actions = rows
            self.render_feed()
        self.api.get("/api/usage", usage)
        self.api.get("/api/actions", feed, params={"limit": 8})
        self.render()

    # ---------------------------------------------------------------- render
    def render(self) -> None:
        s, p = self.store, theme.palette()
        working = [b for b in s.bots if s.state_of(b["id"])[0] in ("work", "takeover")]
        n_need = len(s.approvals)
        self.hello.setText(greeting())
        if not s.bots:
            self.summary.setText("No Bots yet. Create one to get started.")
        elif n_need:
            self.summary.setText(f"{n_need} thing{'s' if n_need != 1 else ''} waiting for your approval. {len(working)} working right now.")
        elif working:
            self.summary.setText(f"{len(working)} of {len(s.bots)} Bots {'is' if len(working) == 1 else 'are'} working. Nothing needs you.")
        else:
            self.summary.setText(f"All {len(s.bots)} Bots are idle. Message one to hand it a task.")
        self.t_need.set(str(n_need), "open the inbox" if n_need else "all clear", hot=n_need > 0)
        self.t_work.set(str(len(working)), ", ".join(b["name"] for b in working)[:60] or "nobody is busy")

        clear_layout(self.team)
        for i, b in enumerate(s.bots):
            kind, text = s.state_of(b["id"])
            c = card("hover")
            c.setCursor(Qt.CursorShape.PointingHandCursor)
            h = QHBoxLayout(c)
            h.setContentsMargins(14, theme.dp(12), 14, theme.dp(12))
            h.setSpacing(12)
            h.addWidget(Avatar(b.get("emoji") or "🤖", 40), 0, Qt.AlignmentFlag.AlignTop)
            col = QVBoxLayout()
            col.setSpacing(1)
            name = QLabel(b["name"] + ("  ⏸" if b.get("paused") else ""))
            name.setStyleSheet("font-weight: 600;")
            st = QLabel(("● " if kind != "idle" else "") + STATE_TEXT[kind] + (f" · {text}" if kind == "work" and text and text != "working" else ""))
            colour = {"idle": p["faint"], "work": p["accent"], "wait": p["warn"], "takeover": p["warn"]}[kind]
            st.setStyleSheet(f"color: {colour}; font-size: {theme.base_size() - 1}px;")
            text_job = b.get("job", "")
            job = label(text_job if len(text_job) <= 64 else text_job[:61].rstrip() + "…", muted=True)
            job.setMinimumHeight(job.fontMetrics().lineSpacing() * 2 + 4)   # same height for every card; two lines never clip
            for w in (name, st):
                w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                col.addWidget(w)
            job.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            col.addWidget(job)
            h.addLayout(col, 1)
            pend = len(s.pending_for_bot(b["id"]))
            if pend:
                badge = QLabel(str(pend))
                badge.setProperty("badge", True)
                badge.setFixedHeight(18)
                badge.setMinimumWidth(18)
                badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
                h.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
            c.mouseReleaseEvent = lambda e, bid=b["id"]: self.openBot.emit(bid)  # type: ignore[assignment]
            self.team.addWidget(c, i // 2, i % 2)

        clear_layout(self.need_box)
        self.need_head.setVisible(bool(s.approvals))
        for a in s.approvals[:4]:
            c = card("approval")
            h = QHBoxLayout(c)
            h.setContentsMargins(14, 10, 12, 10)
            h.setSpacing(10)
            col = QVBoxLayout()
            col.setSpacing(1)
            col.addWidget(label(a.get("summary", "")[:110], wrap=True))
            col.addWidget(label(f"{s.bot_name(a['bot_id'])}  ·  {a.get('category', '')}", faint=True))
            h.addLayout(col, 1)
            h.addWidget(button("Review", on=lambda a=a: self.openThread.emit(a.get("thread_id", ""), a["bot_id"])), 0, Qt.AlignmentFlag.AlignVCenter)
            self.need_box.addWidget(c)
        if len(s.approvals) > 4:
            self.need_box.addWidget(button(f"See all {len(s.approvals)}", flat=True, on=lambda: self.openPage.emit("inbox")))
        self.render_feed()

    def render_feed(self) -> None:
        p = theme.palette()
        clear_layout(self.feed)
        if not self.actions:
            self.feed.addWidget(label("Nothing yet. Bot actions show up here as they happen.", faint=True))
            return
        dot = {"ok": p["ok"], "error": p["bad"], "denied": p["warn"], "blocked": p["warn"]}
        for a in self.actions:
            row = QHBoxLayout()
            row.setContentsMargins(0, theme.dp(7), 0, theme.dp(7))
            row.setSpacing(10)
            d = QLabel()
            d.setFixedSize(8, 8)
            d.setStyleSheet(f"background: {dot.get(a.get('status', ''), p['faint'])}; border-radius: 4px;")
            row.addWidget(d, 0, Qt.AlignmentFlag.AlignTop)
            col = QVBoxLayout()
            col.setSpacing(0)
            what = a.get("url") or a.get("path") or ""
            col.addWidget(label(f"{self.store.bot_name(a['bot_id'])}  ·  {a['tool']}", wrap=False))
            col.addWidget(label(f"{fmt_time(a['ts'])}" + (f"  ·  {what[:46]}" if what else ""), faint=True, wrap=False))
            row.addLayout(col, 1)
            self.feed.addLayout(row)
