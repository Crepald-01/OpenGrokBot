"""Bot-to-bot messaging, group chats, handoffs, follow-ups and the proactive heartbeat.

Bots coordinate in threads: a group chat, or a private "bot pair" thread. They pass work with handoffs
(one owner per task), mention each other with @Name, and only pull the user in for judgment calls.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .db import jdump, jload, new_id, now
from .threads import blocks_text

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

MAX_HOPS = 10
STALL_SECONDS = 4 * 3600
OPEN_STATES = ("open", "accepted", "in_progress", "blocked")


class Messaging:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.db = engine.db

    # ------------------------------------------------------------------ groups
    def _group_out(self, g: dict | None) -> dict | None:
        if not g:
            return None
        g = dict(g)
        g["members"] = jload(g["members"], [])
        names = self.engine.bots.names()
        g["member_names"] = [names.get(m, {}).get("name", m) for m in g["members"]]
        return g

    def create_group(self, name: str, members: list[str], lead: str = "", goal: str = "", kind: str = "group") -> dict:
        members = list(dict.fromkeys(m for m in members if self.engine.bots.get(m)))
        if len(members) < 1:
            raise ValueError("A group needs at least one Bot.")
        gid = new_id()
        th = self.engine.threads.create(None, name, kind="group", group_id=gid)
        self.db.insert("groups", {"id": gid, "name": name.strip() or "Group", "goal": goal, "lead_bot": lead if lead in members else "",
                                  "members": jdump(members), "thread_id": th["id"], "kind": kind, "created_at": now()})
        self.engine.events.publish("groups", change="created", group_id=gid)
        return self.get_group(gid)  # type: ignore

    def get_group(self, gid: str) -> dict | None:
        return self._group_out(self.db.one("SELECT * FROM groups WHERE id=?", (gid,)))

    def group_by_thread(self, thread_id: str) -> dict | None:
        return self._group_out(self.db.one("SELECT * FROM groups WHERE thread_id=?", (thread_id,)))

    def list_groups(self, include_pairs: bool = False) -> list[dict]:
        rows = self.db.query("SELECT * FROM groups" + ("" if include_pairs else " WHERE kind='group'") + " ORDER BY created_at")
        return [self._group_out(r) for r in rows]  # type: ignore

    def update_group(self, gid: str, **f) -> dict:
        patch = {}
        if f.get("name"):
            patch["name"] = f["name"]
        if "goal" in f and f["goal"] is not None:
            patch["goal"] = f["goal"]
        if f.get("members") is not None:
            patch["members"] = jdump(list(dict.fromkeys(f["members"])))
        if "lead_bot" in f and f["lead_bot"] is not None:
            patch["lead_bot"] = f["lead_bot"]
        self.db.update("groups", gid, patch)
        g = self.get_group(gid)
        if g and f.get("name"):
            self.engine.threads.rename(g["thread_id"], f["name"])
        self.engine.events.publish("groups", change="updated", group_id=gid)
        return g  # type: ignore

    def delete_group(self, gid: str) -> None:
        g = self.get_group(gid)
        if g:
            self.engine.threads.delete(g["thread_id"])
            self.db.execute("DELETE FROM groups WHERE id=?", (gid,))
            self.engine.events.publish("groups", change="deleted", group_id=gid)

    def pair_thread(self, a: str, b: str) -> dict:
        key = sorted([a, b])
        for g in self.list_groups(include_pairs=True):
            if g["kind"] == "botpair" and sorted(g["members"]) == key:
                return self.engine.threads.get(g["thread_id"])  # type: ignore
        names = self.engine.bots.names()
        title = f"{names.get(a, {}).get('name', a)} ↔ {names.get(b, {}).get('name', b)}"
        g = self.create_group(title, [a, b], kind="botpair")
        return self.engine.threads.get(g["thread_id"])  # type: ignore

    # ----------------------------------------------------------------- routing
    def _mentions(self, members: list[str], text: str, exclude: str | None = None) -> list[str]:
        names = self.engine.bots.names()
        low = text.lower()
        return [m for m in members if m != exclude and f"@{names.get(m, {}).get('name', '').lower()}" in low]

    def on_user_message(self, thread_id: str, text: str) -> list[str]:
        """Start turns for whichever Bots the user's message is addressed to. Returns bot ids."""
        th = self.engine.threads.get(thread_id)
        if not th:
            return []
        self.db.update("threads", thread_id, {"hops": 0})
        if th["kind"] != "group":
            return [th["bot_id"]] if th["bot_id"] else []
        g = self.group_by_thread(thread_id)
        if not g:
            return []
        targets = self._mentions(g["members"], text) or ([g["lead_bot"]] if g["lead_bot"] else g["members"])
        return [t for t in targets if self.engine.bots.get(t)]

    def after_bot_message(self, thread_id: str, bot: dict, text: str) -> None:
        """Route a Bot's message in a group/pair thread to the Bots it addresses."""
        th = self.engine.threads.get(thread_id)
        g = self.group_by_thread(thread_id) if th and th["kind"] == "group" else None
        if not g or not text.strip():
            return
        targets = self._mentions(g["members"], text, exclude=bot["id"])
        if not targets and g["kind"] == "botpair":
            targets = [m for m in g["members"] if m != bot["id"]]
        if not targets:
            return
        hops = int(th.get("hops") or 0) + 1  # type: ignore[union-attr]
        if hops > MAX_HOPS:
            if hops == MAX_HOPS + 1:
                self.db.update("threads", thread_id, {"hops": hops})
                self.engine.threads.notice(thread_id, f"Paused: the Bots exchanged {MAX_HOPS} messages without you. Reply here to continue.", "warn")
                self.engine.notify("hops", bot, thread_id, "Bots paused for your input", g["name"])
            return
        self.db.update("threads", thread_id, {"hops": hops})
        for t in targets:
            self.engine.turns.start(t, thread_id, trigger="bot_message")

    # -------------------------------------------------------------- bot -> bot
    def send_to_bot(self, from_bot: dict, to_ref: str, text: str, thread_id: str | None = None) -> tuple[dict, str]:
        to = self.engine.bots.find(to_ref)
        if not to:
            raise ValueError(f"No Bot named '{to_ref}'. Call bots_list to see teammates.")
        if to["id"] == from_bot["id"]:
            raise ValueError("You cannot message yourself.")
        th = None
        if thread_id:
            cur = self.engine.threads.get(thread_id)
            g = self.group_by_thread(thread_id) if cur and cur["kind"] == "group" else None
            if g and to["id"] in g["members"]:
                th = cur
        th = th or self.pair_thread(from_bot["id"], to["id"])
        body = text if f"@{to['name']}".lower() in text.lower() else f"@{to['name']} {text}"
        self.engine.threads.add(th["id"], from_bot["id"], "assistant", body)
        hops = int(th.get("hops") or 0) + 1
        self.db.update("threads", th["id"], {"hops": hops})
        if hops <= MAX_HOPS:
            self.engine.turns.start(to["id"], th["id"], trigger="bot_message")
        return to, th["id"]

    # ---------------------------------------------------------------- handoffs
    def _handoff_out(self, h: dict | None) -> dict | None:
        if not h:
            return None
        names = self.engine.bots.names()
        h = dict(h)
        h["from_name"] = "You" if h["from_bot"] == "user" else names.get(h["from_bot"], {}).get("name", h["from_bot"])
        h["to_name"] = names.get(h["to_bot"], {}).get("name", h["to_bot"])
        return h

    def create_handoff(self, from_bot: str, to_ref: str, title: str, brief: str, thread_id: str | None = None) -> dict:
        to = self.engine.bots.find(to_ref)
        if not to:
            raise ValueError(f"No Bot named '{to_ref}'.")
        if to["id"] == from_bot:
            raise ValueError("You cannot hand a task to yourself.")
        frm = self.engine.bots.get(from_bot) if from_bot != "user" else None
        hid = new_id(8)
        if frm:
            th = None
            if thread_id:
                cur = self.engine.threads.get(thread_id)
                g = self.group_by_thread(thread_id) if cur and cur["kind"] == "group" else None
                if g and to["id"] in g["members"]:
                    th = cur
            th = th or self.pair_thread(frm["id"], to["id"])
        else:
            th = self.engine.threads.main_thread(to["id"])
        self.db.insert("handoffs", {"id": hid, "from_bot": from_bot, "to_bot": to["id"], "title": title[:120], "brief": brief, "status": "open",
                                    "thread_id": th["id"], "created_at": now(), "updated_at": now()})
        msg = f"@{to['name']} **Handoff `{hid}`: {title}**\n\n{brief}\n\nYou own this now. Use handoff_update when you accept, get blocked, or finish."
        if frm:
            self.engine.threads.add(th["id"], frm["id"], "assistant", msg)
        else:
            self.engine.threads.add(th["id"], "user", "user", msg.replace(f"@{to['name']} ", "", 1))
        self.db.update("threads", th["id"], {"hops": 0 if not frm else int(th.get("hops") or 0) + 1})
        self.engine.turns.start(to["id"], th["id"], trigger="handoff")
        self.engine.events.publish("handoffs", change="created", handoff_id=hid)
        return self._handoff_out(self.db.one("SELECT * FROM handoffs WHERE id=?", (hid,)))  # type: ignore

    def get_handoff(self, hid: str) -> dict | None:
        return self._handoff_out(self.db.one("SELECT * FROM handoffs WHERE id=?", (hid,)))

    def list_handoffs(self, bot_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
        where, params = [], []
        if bot_id:
            where.append("(from_bot=? OR to_bot=?)")
            params += [bot_id, bot_id]
        if status == "open":
            where.append("status IN ('open','accepted','in_progress','blocked','stalled')")
        elif status:
            where.append("status=?")
            params.append(status)
        sql = "SELECT * FROM handoffs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY updated_at DESC LIMIT ?"
        return [self._handoff_out(r) for r in self.db.query(sql, [*params, limit])]  # type: ignore

    def update_handoff(self, hid: str, by_bot: str, status: str, note: str = "") -> dict:
        h = self.db.one("SELECT * FROM handoffs WHERE id=?", (hid,))
        if not h:
            raise ValueError(f"No handoff {hid}.")
        if status not in ("accepted", "in_progress", "blocked", "done", "cancelled", "open"):
            raise ValueError("status must be accepted, in_progress, blocked, done, cancelled or open.")
        patch = {"status": status, "updated_at": now()}
        if status in ("done", "blocked", "cancelled") and note:
            patch["result"] = note[:4000]
        self.db.update("handoffs", hid, patch)
        who = self.engine.bots.get(by_bot)
        label = {"accepted": "accepted", "in_progress": "is working on", "blocked": "is BLOCKED on", "done": "finished", "cancelled": "cancelled", "open": "re-opened"}[status]
        text = f"Handoff `{hid}` ({h['title']}): {who['name'] if who else by_bot} {label} it." + (f"\n\n{note}" if note else "")
        if h["thread_id"] and who:
            if h["from_bot"] != "user":
                frm = self.engine.bots.get(h["from_bot"])
                if frm and status in ("done", "blocked") and who["id"] != frm["id"]:
                    text = f"@{frm['name']} " + text
            self.engine.threads.add(h["thread_id"], by_bot, "assistant", text)
            if status in ("done", "blocked") and h["from_bot"] not in ("user", by_bot):
                self.engine.turns.start(h["from_bot"], h["thread_id"], trigger="bot_message")
        if status in ("done", "blocked") and who:
            self.engine.notify("handoff", who, h["thread_id"], f"{who['name']} {label}: {h['title']}", note[:160], urgent=status == "blocked")
        self.engine.events.publish("handoffs", change="updated", handoff_id=hid)
        return self.get_handoff(hid)  # type: ignore

    def nudge_stalled(self) -> None:
        cutoff = time.time() - STALL_SECONDS
        rows = self.db.query("SELECT * FROM handoffs WHERE status IN ('open','accepted','in_progress') AND updated_at<? AND last_nudge_at<?", (cutoff, cutoff))
        for h in rows:
            bot = self.engine.bots.get(h["to_bot"])
            if not bot or bot["paused"] or not h["thread_id"]:
                continue
            n = h["nudges"] + 1
            self.db.update("handoffs", h["id"], {"nudges": n, "last_nudge_at": now()})
            if n > 3:
                self.db.update("handoffs", h["id"], {"status": "stalled"})
                self.engine.notify("handoff", bot, h["thread_id"], f"Stalled handoff: {h['title']}", f"{bot['name']} has not updated it after 3 nudges.", urgent=True)
                continue
            self.engine.threads.add(h["thread_id"], "system", "user",
                                    f"Nudge {n}/3: handoff `{h['id']}` ({h['title']}) has had no update for a while. Continue it, or call handoff_update "
                                    f"(blocked/done) so the owner knows where it stands.")
            self.engine.turns.start(bot["id"], h["thread_id"], trigger="nudge")

    # --------------------------------------------------------------- follow-ups
    def schedule_followup(self, bot_id: str, thread_id: str, note: str, minutes: float | None = None, at_iso: str | None = None) -> dict:
        if at_iso:
            try:
                due = datetime.fromisoformat(at_iso).timestamp()
            except ValueError as e:
                raise ValueError("when must be an ISO 8601 date/time like 2026-10-06T09:00") from e
        else:
            due = time.time() + float(minutes or 60) * 60
        fid = new_id(8)
        self.db.insert("followups", {"id": fid, "bot_id": bot_id, "thread_id": thread_id, "note": note[:1000], "due_at": due, "status": "open", "created_at": now()})
        return {"id": fid, "due_at": due}

    def list_followups(self, bot_id: str | None = None, status: str = "open") -> list[dict]:
        return self.db.query("SELECT * FROM followups WHERE status=?" + (" AND bot_id=?" if bot_id else "") + " ORDER BY due_at",
                             (status, bot_id) if bot_id else (status,))

    def cancel_followup(self, fid: str) -> None:
        self.db.update("followups", fid, {"status": "cancelled"})

    def run_due_followups(self) -> None:
        for f in self.db.query("SELECT * FROM followups WHERE status='open' AND due_at<=?", (time.time(),)):
            bot = self.engine.bots.get(f["bot_id"])
            self.db.update("followups", f["id"], {"status": "done"})
            if not bot or bot["paused"]:
                continue
            tid = f["thread_id"] if self.engine.threads.get(f["thread_id"]) else self.engine.threads.main_thread(bot["id"])["id"]
            self.engine.threads.add(tid, "system", "user", f"Follow-up you scheduled is due: {f['note']}\nCheck the current state at the source, then continue or report.")
            self.engine.turns.start(bot["id"], tid, trigger="followup")

    # ----------------------------------------------------------------- proactive
    def proactive_tick(self) -> None:
        interval = float(self.engine.settings.get("proactive.interval_min", 180)) * 60
        for bot in self.engine.bots.list():
            if bot["proactive"] == "off" or bot["paused"] or self.engine.usage.over_limit() or self.engine.usage.bot_over_budget(bot):
                continue
            last = float(self.engine.settings.get(f"proactive_last.{bot['id']}", 0) or 0)
            if time.time() - last < interval or self.engine.turns.is_busy(bot["id"]):
                continue
            self.engine.settings.set(f"proactive_last.{bot['id']}", time.time())
            threading_target = self._proactive_run
            import threading
            threading.Thread(target=threading_target, args=(bot,), daemon=True, name=f"proactive-{bot['name']}").start()

    def _proactive_run(self, bot: dict) -> None:
        eng = self.engine
        th = eng.db.one("SELECT * FROM threads WHERE bot_id=? AND kind='system' AND title='Proactive'", (bot["id"],)) or eng.threads.create(bot["id"], "Proactive", kind="system")
        mode = ("Propose it to the user in 2-3 sentences and wait; do not act yet." if bot["proactive"] == "suggest" else
                "If it is low-risk and clearly within your job, do it now (approvals still apply), then summarise briefly.")
        task = ("Heartbeat: look at your memory, open handoffs, scheduled follow-ups, and recent work. Decide whether there is useful work you should pick up or "
                "suggest, such as a dropped thread, a stalled handoff, something recurring worth turning into a skill or routine, or a follow-up that is due. "
                f"If there is, {mode} If there is nothing worthwhile, reply with exactly NOOP.")
        res = eng.turns.run_sync(bot["id"], th["id"], task, trigger="proactive")
        text = (res.final_text or "").strip()
        if res.status == "done" and text and not text.upper().startswith("NOOP"):
            main = eng.threads.main_thread(bot["id"])
            eng.threads.add(main["id"], bot["id"], "assistant", f"**Proactive note**\n\n{text}")
            eng.notify("suggestion", bot, main["id"], f"{bot['name']} has a suggestion", text[:160])
