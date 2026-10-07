"""Named Bots: create, edit, archive. Conversation threads and memory stay separate per Bot."""
from __future__ import annotations

from typing import Any

from .db import Database, jdump, jload, new_id, now
from .events import EventBus
from .settings import Admin, Settings

JSON_FIELDS = {"net_allow": [], "net_deny": [], "grants": []}
EDITABLE = {"name", "emoji", "job", "instructions", "profile", "model", "approval_mode", "step_limit", "net_mode",
            "net_allow", "net_deny", "grants", "proactive", "paused", "archived", "template", "daily_token_limit"}
APPROVAL_MODES = ("ask", "auto_review")
PROACTIVE_LEVELS = ("off", "suggest", "act")


class BotError(ValueError):
    pass


class Bots:
    def __init__(self, db: Database, settings: Settings, admin: Admin, events: EventBus):
        self.db, self.settings, self.admin, self.events = db, settings, admin, events

    # -- helpers ---------------------------------------------------------------
    def _out(self, row: dict | None) -> dict | None:
        if not row:
            return None
        row = dict(row)
        for k, d in JSON_FIELDS.items():
            row[k] = jload(row.get(k), d)
        row["paused"] = bool(row["paused"])
        row["archived"] = bool(row["archived"])
        row["daily_token_limit"] = int(row.get("daily_token_limit") or 0)
        row["effective_approval_mode"] = self.effective_approval_mode(row)
        return row

    def effective_approval_mode(self, bot: dict) -> str:
        adm = self.admin.approval()
        if adm.get("locked") and adm.get("mode") in APPROVAL_MODES:
            return adm["mode"]
        return bot.get("approval_mode") or self.settings.get("approval.default_mode", "ask")

    def effective_step_limit(self, bot: dict) -> int:
        n = int(bot.get("step_limit") or self.settings.get("defaults.step_limit", 40))
        cap = self.admin.max_steps()
        return min(n, cap) if cap else max(1, n)

    # -- CRUD ------------------------------------------------------------------
    def get(self, bot_id: str) -> dict | None:
        return self._out(self.db.one("SELECT * FROM bots WHERE id=?", (bot_id,)))

    def get_by_name(self, name: str) -> dict | None:
        return self._out(self.db.one("SELECT * FROM bots WHERE lower(name)=lower(?) AND archived=0", (name.strip(),)))

    def find(self, ref: str) -> dict | None:
        """Look up by id or name (names are matched case-insensitively, @ prefix tolerated)."""
        ref = (ref or "").strip().lstrip("@")
        return self.get(ref) or self.get_by_name(ref)

    def list(self, include_archived: bool = False) -> list[dict]:
        rows = self.db.query("SELECT * FROM bots" + ("" if include_archived else " WHERE archived=0") + " ORDER BY created_at")
        return [self._out(r) for r in rows]  # type: ignore

    def create(self, name: str, job: str = "", instructions: str = "", emoji: str = "🤖", **opts: Any) -> dict:
        name = (name or "").strip()[:40]   # the stored name is cut to 40, so uniqueness is checked on what is stored
        if not name:
            raise BotError("A Bot needs a name.")
        if self.get_by_name(name):
            raise BotError(f"There is already a Bot named {name}.")
        mode = opts.get("approval_mode") or self.settings.get("approval.default_mode", "ask")
        row = {"id": new_id(), "name": name[:40], "emoji": emoji or "🤖", "job": job.strip(), "instructions": instructions.strip(),
               "profile": opts.get("profile", ""), "model": opts.get("model", ""),
               "approval_mode": mode if mode in APPROVAL_MODES else "ask",
               "step_limit": int(opts.get("step_limit") or self.settings.get("defaults.step_limit", 40)),
               "net_mode": opts.get("net_mode", "inherit"), "net_allow": jdump(opts.get("net_allow", [])),
               "net_deny": jdump(opts.get("net_deny", [])), "grants": jdump(opts.get("grants", [])),
               "proactive": opts.get("proactive", "off"), "template": opts.get("template", ""),
               "created_at": now(), "updated_at": now()}
        self.db.insert("bots", row)
        self.events.publish("bots", change="created", bot_id=row["id"])
        return self.get(row["id"])  # type: ignore

    def update(self, bot_id: str, **fields: Any) -> dict:
        if not self.get(bot_id):
            raise BotError("No such Bot.")
        patch: dict[str, Any] = {}
        for k, v in fields.items():
            if k not in EDITABLE or v is None:
                continue
            if k == "name":
                v = str(v).strip()[:40]
                other = self.get_by_name(v)
                if not v or (other and other["id"] != bot_id):
                    raise BotError("Name is empty or already used by another Bot.")
            if k in JSON_FIELDS:
                v = jdump(list(v))
            if k == "approval_mode" and v not in APPROVAL_MODES:
                raise BotError("approval_mode must be 'ask' or 'auto_review'.")
            if k == "proactive" and v not in PROACTIVE_LEVELS:
                raise BotError("proactive must be off, suggest or act.")
            if k in ("paused", "archived"):
                v = int(bool(v))
            if k == "step_limit":
                v = max(1, min(500, int(v)))
            if k == "daily_token_limit":
                v = max(0, min(1_000_000_000, int(v)))
            patch[k] = v
        patch["updated_at"] = now()
        self.db.update("bots", bot_id, patch)
        self.events.publish("bots", change="updated", bot_id=bot_id)
        return self.get(bot_id)  # type: ignore

    def duplicate(self, bot_id: str, name: str | None = None) -> dict:
        """A new Bot with the same job, instructions, model, approval mode and limits. It does not copy access grants,
        conversations or memory: the copy starts clean and asks for access itself."""
        src = self.get(bot_id)
        if not src:
            raise BotError("No such Bot.")
        base = (name or f"{src['name']} copy").strip()[:36]
        new_name, n = base, 1
        while self.get_by_name(new_name):
            n += 1
            new_name = f"{base} {n}"
        bot = self.create(new_name, job=src["job"], instructions=src["instructions"], emoji=src["emoji"], profile=src["profile"], model=src["model"],
                          approval_mode=src["approval_mode"], step_limit=src["step_limit"], net_mode=src["net_mode"], net_allow=src["net_allow"],
                          net_deny=src["net_deny"], proactive=src["proactive"], template=src["template"])
        if src["daily_token_limit"]:
            bot = self.update(bot["id"], daily_token_limit=src["daily_token_limit"])
        return bot

    def set_paused_all(self, paused: bool) -> int:
        """Pause or resume every active Bot. Returns how many changed."""
        n = 0
        for b in self.list():
            if b["paused"] != paused:
                self.update(b["id"], paused=paused)
                n += 1
        return n

    def delete(self, bot_id: str) -> None:
        for t in self.db.query("SELECT id FROM threads WHERE bot_id=?", (bot_id,)):
            self.db.execute("DELETE FROM messages WHERE thread_id=?", (t["id"],))
        for table in ("threads", "memories", "routines", "followups", "approval_rules", "recordings"):
            self.db.execute(f"DELETE FROM {table} WHERE bot_id=?", (bot_id,))
        self.db.execute("DELETE FROM bots WHERE id=?", (bot_id,))
        self.events.publish("bots", change="deleted", bot_id=bot_id)

    # -- access grants -----------------------------------------------------------
    def grant(self, bot_id: str, resource: str) -> None:
        bot = self.get(bot_id)
        if bot and resource not in bot["grants"]:
            self.update(bot_id, grants=bot["grants"] + [resource])

    def revoke(self, bot_id: str, resource: str) -> None:
        bot = self.get(bot_id)
        if bot and resource in bot["grants"]:
            self.update(bot_id, grants=[g for g in bot["grants"] if g != resource])

    def names(self) -> dict[str, dict]:
        return {b["id"]: b for b in self.list(include_archived=True)}
