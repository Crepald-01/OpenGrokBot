"""Client-side cache of service state, kept fresh by the event stream."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Signal

from .api import Api

TERMINAL = ("done", "stopped", "error", "limit", "skipped")


class Store(QObject):
    botsChanged = Signal()
    groupsChanged = Signal()
    busyChanged = Signal()
    approvalsChanged = Signal()
    settingsChanged = Signal()
    connectionChanged = Signal(bool)
    event = Signal(dict)
    notification = Signal(dict)

    def __init__(self, api: Api):
        super().__init__()
        self.api = api
        self.bots: list[dict] = []
        self.groups: list[dict] = []
        self.busy: dict[str, dict] = {}
        self.approvals: list[dict] = []
        self.profiles: list[dict] = []
        self.settings: dict[str, Any] = {}
        self.templates: list[dict] = []
        self.presets: list = []
        self.status: dict = {}
        self.connected = False
        self.takeovers: set[str] = set()

    # -- loading ------------------------------------------------------------------
    def apply_bootstrap(self, d: dict) -> None:
        self.bots, self.groups, self.approvals = d["bots"], d["groups"], d["approvals"]
        self.profiles, self.settings, self.templates, self.presets = d["profiles"], d["settings"], d["templates"], d["presets"]
        self.status = d["status"]
        self.busy = dict(self.status.get("busy", {}))
        self.takeovers = {bid for bid, s in (self.status.get("computer", {}).get("browser", {}) or {}).items() if s.get("takeover")}
        self.botsChanged.emit()
        self.groupsChanged.emit()
        self.busyChanged.emit()
        self.approvalsChanged.emit()
        self.settingsChanged.emit()

    def refresh_all(self, done=None) -> None:
        def ok(d: dict) -> None:
            self.apply_bootstrap(d)
            if done:
                done()
        self.api.get("/api/bootstrap", ok)

    def refresh_bots(self) -> None:
        def ok(b: list) -> None:
            self.bots = b
            self.botsChanged.emit()
        self.api.get("/api/bots", ok)

    def refresh_groups(self) -> None:
        def ok(g: list) -> None:
            self.groups = g
            self.groupsChanged.emit()
        self.api.get("/api/groups", ok)

    def refresh_approvals(self) -> None:
        def ok(a: list) -> None:
            self.approvals = a
            self.approvalsChanged.emit()
        self.api.get("/api/approvals", ok, params={"status": "pending"})

    def refresh_settings(self) -> None:
        def ok(s: dict) -> None:
            self.settings = s
            self.settingsChanged.emit()
        self.api.get("/api/settings", ok)

    def refresh_profiles(self) -> None:
        def ok(p: list) -> None:
            self.profiles = p
            self.settingsChanged.emit()
        self.api.get("/api/providers", ok)

    # -- lookups ---------------------------------------------------------------------
    def bot(self, bot_id: str) -> dict | None:
        return next((b for b in self.bots if b["id"] == bot_id), None)

    def bot_name(self, bot_id: str) -> str:
        b = self.bot(bot_id)
        return b["name"] if b else "Bot"

    def group(self, gid: str) -> dict | None:
        return next((g for g in self.groups if g["id"] == gid), None)

    def group_by_thread(self, tid: str) -> dict | None:
        return next((g for g in self.groups if g["thread_id"] == tid), None)

    def state_of(self, bot_id: str) -> tuple[str, str]:
        """(kind, label): kind is idle | work | wait | takeover."""
        if bot_id in self.takeovers:
            return "takeover", "you're driving"
        r = self.busy.get(bot_id)
        if not r:
            return "idle", ""
        s = r.get("status", "running")
        if s == "waiting_approval":
            return "wait", "needs you"
        if s == "waiting_screen":
            return "work", "waiting for screen"
        if s == "retrying":
            return "work", "retrying"
        return "work", "working"

    def pending_for_bot(self, bot_id: str) -> list[dict]:
        return [a for a in self.approvals if a["bot_id"] == bot_id]

    def provider(self, pid: str) -> dict | None:
        return next((p for p in self.profiles if p["id"] == pid), None)

    # -- events ------------------------------------------------------------------------
    def on_event(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "turn":
            if ev["status"] in TERMINAL:
                self.busy.pop(ev["bot_id"], None)
            else:
                self.busy[ev["bot_id"]] = {"thread_id": ev["thread_id"], "status": ev["status"], "steps": ev.get("steps", 0), "turn_id": ev["turn_id"]}
            self.busyChanged.emit()
        elif t == "bots":
            self.refresh_bots()
        elif t == "groups":
            self.refresh_groups()
        elif t == "approval":
            self.refresh_approvals()
        elif t == "takeover":
            (self.takeovers.add if ev.get("active") else self.takeovers.discard)(ev["bot_id"])
            self.busyChanged.emit()
        elif t == "notification":
            self.notification.emit(ev)
        self.event.emit(ev)

    def set_connected(self, ok: bool) -> None:
        was = self.connected
        self.connected = ok
        self.connectionChanged.emit(ok)
        if ok and not was:
            self.refresh_all()
