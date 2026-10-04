"""The engine wires every subsystem together. It runs inside the background service process."""
from __future__ import annotations

import logging
import logging.handlers
import threading
import time
from typing import Any

from . import VERSION, commands, notify as notifier, paths, quiet
from .actionlog import ActionLog
from .agent import AgentRun, TurnManager, run_reflection
from .approvals import ApprovalManager
from .bots import Bots
from .computer import Computer
from .db import Database, new_id, now
from .events import EventBus
from .mcp import McpManager
from .memory import Memory
from .messaging import Messaging
from .netpolicy import NetworkPolicy
from .plugins import PluginManager
from .routines import Routines
from .search import Search
from .settings import Admin, Settings, plugin_allowed
from .skills import Skills
from .templates import TEAM_PRESET, template as get_template
from .threads import Threads
from .tooling import ToolSpec
from .tools_builtin import builtin_tools
from .usage import Usage


def setup_logging() -> logging.Logger:
    log = logging.getLogger("opengrokbot")
    if not log.handlers:
        log.setLevel(logging.INFO)
        h = logging.handlers.RotatingFileHandler(paths.logs_dir() / "service.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        log.addHandler(sh)
    return log


class Engine:
    def __init__(self) -> None:
        self.log = setup_logging()
        self.db = Database(paths.db_path())
        self.settings = Settings(self.db)
        self.admin = Admin()
        self.events = EventBus()
        self.net = NetworkPolicy(self.db, self.settings, self.admin)
        self.bots = Bots(self.db, self.settings, self.admin, self.events)
        self.memory = Memory(self.db)
        self.skills = Skills()
        self.actions = ActionLog(self.db)
        self.usage = Usage(self.db, self.settings, self.admin)
        self.approvals = ApprovalManager(self.db, self.events, self.settings, self.admin)
        self.approvals.engine = self
        self.computer = Computer(self.db, self.settings, self.net, self.events)
        self.threads = Threads(self.db, self.events, self.bots.names, self.describe_tool)
        self.search = Search(self.db, self.bots.names)
        self.mcp = McpManager(self.db, self.admin)
        self.plugins = PluginManager(self)
        self.messaging = Messaging(self)
        self.turns = TurnManager(self)
        self.routines = Routines(self)
        self.builtin: list[ToolSpec] = builtin_tools(self)
        self._labels = {t.name: t.label for t in self.builtin if t.label}
        self.base_url = "http://127.0.0.1:8765"
        self.started = time.time()
        self._notif_lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self.recover()
        self.mcp.start_all()
        self.routines.start()
        s = self.routines.sched
        s.add_job(self.messaging.run_due_followups, "interval", minutes=1, id="_followups", replace_existing=True)
        s.add_job(self.messaging.nudge_stalled, "interval", minutes=15, id="_nudge", replace_existing=True)
        s.add_job(self.messaging.proactive_tick, "interval", minutes=10, id="_proactive", replace_existing=True)
        self.log.info("Engine started (v%s). Data dir: %s", VERSION, paths.data_dir())

    def stop(self) -> None:
        self.turns.stop_all()
        self.routines.stop()
        self.mcp.shutdown()
        self.computer.shutdown()

    def recover(self) -> None:
        """After a restart: expire stale approvals and resume work that was interrupted."""
        self.approvals.expire_all_pending()
        rows = self.db.query("SELECT DISTINCT bot_id, thread_id, trigger FROM turns WHERE status IN ('running','queued','waiting_approval','retrying','waiting_screen')")
        self.db.execute("UPDATE turns SET status='interrupted', ended_at=? WHERE status IN ('running','queued','waiting_approval','retrying','waiting_screen')", (now(),))
        for r in rows:
            if r["trigger"] in ("proactive", "routine", "skill_test") or not self.threads.get(r["thread_id"]):
                continue
            bot = self.bots.get(r["bot_id"])
            if not bot or bot["paused"]:
                continue
            self.threads.add(r["thread_id"], "system", "user", "The service restarted while you were working on this. Check the current state of things at the source, "
                                                               "then resume where you left off.")
            self.turns.start(r["bot_id"], r["thread_id"], trigger="resume")

    # ----------------------------------------------------------------- tools
    def tools_for(self, run: AgentRun) -> list[ToolSpec]:
        granted = set(run.bot["grants"])
        specs: dict[str, ToolSpec] = {t.name: t for t in self.builtin}
        for t in self.plugins.tool_specs(granted):
            specs.setdefault(t.name, t)
        for t in self.mcp.tool_specs(granted):
            specs.setdefault(t.name, t)
        return list(specs.values())

    def describe_tool(self, name: str, args: dict) -> str:
        fn = self._labels.get(name)
        if fn:
            try:
                return fn(args)
            except Exception:
                pass
        first = next(iter(args.values()), "") if isinstance(args, dict) else ""
        return name.replace("_", " ") + (f": {str(first)[:50]}" if first else "")

    def connector_catalog(self, bot: dict) -> str:
        lines = []
        for pid, d in self.plugins.defs.items():
            if f"plugin:{pid}" in bot["grants"] or not plugin_allowed(self.admin, pid):
                continue
            ready = "ready" if self.plugins.configured(pid) else "user must connect it first"
            lines.append(f"  - {pid}: {d.name}, {d.description} ({ready})")
        for c in self.mcp.conns.values():
            if f"mcp:{c.cfg['name']}" not in bot["grants"] and c.status == "connected":
                lines.append(f"  - mcp:{c.cfg['name']}: MCP server with {len(c.tools)} tools")
        return "\n".join(lines) or "  (none)"

    # ---------------------------------------------------------- notifications
    def notify(self, kind: str, bot: dict | None, thread_id: str | None, title: str, body: str = "", urgent: bool = False) -> None:
        cfg = self.settings.get("notifications", {})
        if kind in ("approval", "question", "takeover", "login") and not cfg.get("on_approval", True):
            return
        if kind == "finished" and not cfg.get("on_finish", True):
            return
        muted = quiet.is_quiet(cfg)   # quiet hours / Do Not Disturb: recorded in the Inbox, but nothing pops up, beeps or is pushed
        with self._notif_lock:
            nid = self.db.insert("notifications", {"ts": now(), "kind": kind, "bot_id": (bot or {}).get("id", ""), "thread_id": thread_id or "",
                                                   "title": title[:200], "body": body[:500]})
        self.events.publish("notification", id=nid, kind=kind, bot_id=(bot or {}).get("id", ""), bot_name=(bot or {}).get("name", ""),
                            thread_id=thread_id or "", title=title, body=body, urgent=urgent, muted=muted)
        if muted:
            return
        if cfg.get("toast", True) and not self.events.has_subscriber("desktop"):
            notifier.toast(title, body)
        if cfg.get("ntfy_url") and cfg.get("ntfy_topic") and (urgent or kind in ("approval", "question", "takeover", "login", "finished", "error", "routine")):
            notifier.ntfy(cfg["ntfy_url"], cfg["ntfy_topic"], title, body, urgent)

    def on_turn_end(self, run: AgentRun) -> None:
        res = run.result
        bot = self.bots.get(run.bot_id)
        if not res or not bot:
            return
        th = self.threads.get(run.thread_id) or {}
        elapsed = time.time() - run.started
        min_s = float(self.settings.get("notifications.min_turn_seconds", 20))
        if res.status in ("done", "limit") and run.trigger != "routine" and run.trigger != "proactive" and th.get("kind") != "system" \
                and (elapsed >= min_s or run.steps >= 4) and res.final_text:
            self.notify("finished", bot, run.thread_id, f"{bot['name']} finished", res.final_text[:160])
        if res.status in ("done", "limit") and th.get("kind") == "group" and res.final_text:
            self.messaging.after_bot_message(run.thread_id, bot, res.final_text)
        if res.status == "done" and run.steps >= 2 and run.trigger != "proactive":
            threading.Thread(target=run_reflection, args=(self, run), daemon=True, name="reflect").start()
        if res.status == "done" and self.turns.has_unseen_user_messages(run):
            self.turns.start(run.bot_id, run.thread_id, trigger="user")

    # ------------------------------------------------------- high-level actions
    def send_user_message(self, thread_id: str, text: str, image_paths: list[str] | None = None) -> dict:
        th = self.threads.get(thread_id)
        if not th:
            raise ValueError("No such thread.")
        if text.lstrip().startswith("//"):
            text = text.lstrip()[1:]          # "//text" sends a message that really starts with a slash
        elif not image_paths:
            res = commands.run(self, thread_id, text)   # "/status", "/approve"...: handled here, not sent to the model
            if res is not None:
                if not res.get("send"):
                    return {"started": [], **{k: v for k, v in res.items() if k != "send"}}
                text = res["send"]
        blocks: list[dict] = [{"type": "text", "text": text}] if text.strip() else []
        for p in image_paths or []:
            blocks.append({"type": "image", "path": p, "media_type": "image/jpeg" if p.lower().endswith((".jpg", ".jpeg")) else "image/png"})
        if not blocks:
            raise ValueError("Empty message.")
        self.threads.add(thread_id, "user", "user", blocks)
        started = []
        for bid in self.messaging.on_user_message(thread_id, text):
            if self.turns.start(bid, thread_id, trigger="user"):
                started.append(bid)
        return {"started": started}

    def create_from_template(self, tid: str, name: str | None = None) -> dict:
        t = get_template(tid)
        if not t:
            raise ValueError("Unknown template.")
        bot = self.bots.create(name or t["name"], job=t["job"], instructions=t["instructions"], emoji=t["emoji"], approval_mode=t["approval_mode"], template=tid)
        for g in t["grants"]:
            pass   # access is requested by the Bot and approved by the user; nothing is pre-granted
        self.threads.main_thread(bot["id"])
        return bot

    def create_team(self, template_ids: list[str] | None = None, group_name: str = "Ops room") -> dict:
        created, bots = [], []
        for tid in template_ids or TEAM_PRESET:
            t = get_template(tid)
            if not t:
                continue
            existing = self.bots.get_by_name(t["name"])
            bot = existing or self.create_from_template(tid)
            if not existing:
                created.append(bot["name"])
            bots.append(bot)
        group = None
        if len(bots) > 1:
            lead = next((b for b in bots if b["template"] == "chief_of_staff"), bots[0])
            group = self.messaging.create_group(group_name, [b["id"] for b in bots], lead=lead["id"],
                                                goal="Coordinate the team. The lead breaks goals into handoffs; specialists own their tasks; only pull the user in for judgment calls.")
            self.threads.add(group["thread_id"], "user", "user", "Team created. Lead: coordinate, hand work to the right specialist, and keep me posted.", publish=True)
        return {"bots": bots, "created": created, "group": group}

    def test_skill(self, bot_id: str, skill_name: str, dry_run: bool = True) -> dict:
        bot = self.bots.get(bot_id)
        sk = self.skills.get(skill_name)
        if not bot or not sk:
            raise ValueError("Unknown Bot or skill.")
        th = self.threads.create(bot_id, f"Test: {sk['name']}", kind="dm")
        task = (f"[Skill test] Run this skill once, as a test.{' This is a DRY RUN: consequential actions will be refused, so describe what you would do there.' if dry_run else ''}\n"
                f"Report after each step what worked and what was unclear, and suggest edits to the skill.\n\n# Skill: {sk['name']}\n\n{sk['body']}")
        self.turns.start(bot_id, th["id"], trigger="skill_test", task=task, dry_run=dry_run)
        return th

    def status(self) -> dict:
        busy: dict[str, dict] = {}
        for r in self.turns.active():
            cur = busy.get(r["bot_id"])
            if not cur or r["status"] in ("waiting_approval", "waiting_screen"):
                busy[r["bot_id"]] = r
        return {"version": VERSION, "uptime": time.time() - self.started, "busy": busy, "pending_approvals": self.approvals.pending_count(),
                "computer": self.computer.status(), "usage_total": self.usage.week_total(), "admin": self.admin.summary(),
                "data_dir": str(paths.data_dir())}
