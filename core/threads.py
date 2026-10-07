"""Conversation threads and messages.

Messages are stored in provider-neutral block format. In group threads every Bot sees the other
speakers as prefixed user messages, and only the author sees its own tool traces.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from .db import Database, jdump, jload, new_id, now
from .events import EventBus

ORDER = "ORDER BY COALESCE(anchor, id), (anchor IS NOT NULL), id"


def text_blocks(text: str) -> list[dict]:
    return [{"type": "text", "text": text}]


def blocks_text(blocks: Any) -> str:
    if isinstance(blocks, str):
        return blocks
    return "\n".join(b.get("text", "") for b in blocks or [] if b.get("type") == "text").strip()


def _synthetic_results(pending: set[str]) -> list[dict]:
    return [{"type": "tool_result", "tool_use_id": p, "is_error": True,
             "content": [{"type": "text", "text": "(This action was interrupted and never completed.)"}]} for p in sorted(pending)]


def fix_pairs(msgs: list[dict], ids: list[int]) -> tuple[list[dict], list[int]]:
    """Make every tool_use followed by its tool_result (stopped or crashed turns can leave gaps)."""
    out: list[dict] = []
    oids: list[int] = []
    pending: set[str] = set()
    for m, mid in zip(msgs, ids):
        if m["role"] == "user":
            results = [b for b in m["content"] if b.get("type") == "tool_result" and b["tool_use_id"] in pending]
            rest = [b for b in m["content"] if b.get("type") != "tool_result"]
            have = {b["tool_use_id"] for b in results}
            results += _synthetic_results(pending - have)
            pending = set()
            content = results + rest
            if content:
                out.append({"role": "user", "content": content})
                oids.append(mid)
            continue
        if pending:
            out.append({"role": "user", "content": _synthetic_results(pending)})
            oids.append(0)
            pending = set()
        out.append(m)
        oids.append(mid)
        pending = {b["id"] for b in m["content"] if b.get("type") == "tool_use"}
    if pending:
        out.append({"role": "user", "content": _synthetic_results(pending)})
        oids.append(0)
    return out, oids


class Threads:
    def __init__(self, db: Database, events: EventBus, names: Callable[[], dict[str, dict]],
                 describe: Callable[[str, dict], str]):
        self.db, self.events, self._names, self._describe = db, events, names, describe

    # -- threads --------------------------------------------------------------
    def create(self, bot_id: str | None, title: str = "New thread", kind: str = "dm", group_id: str | None = None) -> dict:
        tid = new_id()
        self.db.insert("threads", {"id": tid, "bot_id": bot_id or "", "group_id": group_id or "", "kind": kind, "title": title,
                                   "created_at": now(), "updated_at": now()})
        self.events.publish("threads", change="created", thread_id=tid, bot_id=bot_id or "")
        return self.get(tid)  # type: ignore

    def get(self, tid: str) -> dict | None:
        return self.db.one("SELECT * FROM threads WHERE id=?", (tid,))

    def list_for_bot(self, bot_id: str, kinds: tuple[str, ...] = ("dm", "routine")) -> list[dict]:
        marks = ",".join("?" for _ in kinds)
        return self.db.query(f"SELECT * FROM threads WHERE bot_id=? AND kind IN ({marks}) AND archived=0 ORDER BY updated_at DESC",
                             (bot_id, *kinds))

    def main_thread(self, bot_id: str) -> dict:
        t = self.db.one("SELECT * FROM threads WHERE bot_id=? AND kind='dm' AND archived=0 ORDER BY created_at LIMIT 1", (bot_id,))
        return t or self.create(bot_id, "Main")

    def rename(self, tid: str, title: str) -> None:
        self.db.update("threads", tid, {"title": title[:80]})
        self.events.publish("threads", change="renamed", thread_id=tid)

    def delete(self, tid: str) -> None:
        self.db.execute("DELETE FROM messages WHERE thread_id=?", (tid,))
        self.db.execute("DELETE FROM threads WHERE id=?", (tid,))
        self.events.publish("threads", change="deleted", thread_id=tid)

    def set_summary(self, tid: str, summary: str, upto: int) -> None:
        self.db.update("threads", tid, {"summary": summary, "summary_upto": upto})

    # -- messages -------------------------------------------------------------
    def add(self, thread_id: str, author: str, role: str, content: list[dict] | str, kind: str = "llm",
            anchor: int | None = None, turn_id: str = "", publish: bool = True, **extra: Any) -> int:
        blocks = text_blocks(content) if isinstance(content, str) else content
        mid = self.db.insert("messages", {"thread_id": thread_id, "author": author, "role": role, "kind": kind,
                                          "content": jdump(blocks), "anchor": anchor, "turn_id": turn_id, "created_at": now()})
        self.db.update("threads", thread_id, {"updated_at": now()})
        th = self.get(thread_id)
        if th and th["title"] in ("New thread", "Main", "") and author == "user" and role == "user":
            t = blocks_text(blocks)[:60].strip()
            if t:
                self.db.update("threads", thread_id, {"title": t})
        if publish:
            row = self.db.one("SELECT * FROM messages WHERE id=?", (mid,))
            self.events.publish("message", thread_id=thread_id, bot_id=(th or {}).get("bot_id", ""),
                                items=self.display_items([row], self._names()), **extra)  # type: ignore[arg-type]
        return mid

    def notice(self, thread_id: str, text: str, level: str = "info") -> int:
        return self.add(thread_id, "system", "user", [{"type": "text", "text": text, "level": level}], kind="notice")

    def rows(self, thread_id: str, after: int = 0) -> list[dict]:
        return self.db.query(f"SELECT * FROM messages WHERE thread_id=? AND id>? {ORDER}", (thread_id, after))

    def last_id(self, thread_id: str) -> int:
        return int(self.db.scalar("SELECT COALESCE(MAX(id),0) FROM messages WHERE thread_id=?", (thread_id,), 0))

    # -- LLM view -------------------------------------------------------------
    def _convert(self, rows: list[dict], viewer: str, group: bool, names: dict[str, dict]) -> tuple[list[dict], list[int]]:
        out: list[dict] = []
        ids: list[int] = []
        for r in rows:
            if r["kind"] != "llm":
                continue
            blocks = jload(r["content"], [])
            if r["author"] == viewer:
                out.append({"role": r["role"], "content": blocks})
            elif r["role"] == "assistant":  # another Bot speaking
                text = blocks_text(blocks)
                if not text:
                    continue
                who = names.get(r["author"], {}).get("name", r["author"])
                out.append({"role": "user", "content": [{"type": "text", "text": f"[{who}]: {text}"}]})
            else:
                if r["author"] == "user" and group:
                    blocks = [dict(x, text=f"[User]: {x['text']}") if x.get("type") == "text" and i == 0 else x
                              for i, x in enumerate(blocks)]
                elif r["author"] == "system":
                    blocks = [dict(x, text=f"[System]: {x['text']}") if x.get("type") == "text" else x for x in blocks]
                out.append({"role": "user", "content": blocks})
            ids.append(r["id"])
        return out, ids

    def llm_history(self, thread_id: str, viewer: str) -> tuple[list[dict], int, list[int]]:
        th = self.get(thread_id) or {}
        names = self._names()
        group = th.get("kind") == "group"
        rows = self.rows(thread_id, th.get("summary_upto", 0) or 0)
        last = max((r["id"] for r in rows), default=th.get("summary_upto", 0) or 0)
        msgs, ids = self._convert(rows, viewer, group, names)
        msgs, ids = fix_pairs(msgs, ids)
        return msgs, last, ids

    def external_since(self, thread_id: str, viewer: str, after_id: int) -> tuple[list[dict], int, list[int]]:
        th = self.get(thread_id) or {}
        all_rows = self.rows(thread_id, after_id)
        rows = [r for r in all_rows if r["author"] != viewer]
        last = max((r["id"] for r in all_rows), default=after_id)
        msgs, ids = self._convert(rows, viewer, th.get("kind") == "group", self._names())
        return msgs, last, ids

    # -- UI view --------------------------------------------------------------
    def display(self, thread_id: str, limit: int = 400) -> list[dict]:
        rows = self.rows(thread_id)
        if len(rows) > limit * 3:
            rows = rows[-limit * 3:]
        items = self.display_items(rows, self._names())
        return items[-limit:]

    def display_items(self, rows: list[dict], names: dict[str, dict]) -> list[dict]:
        items: list[dict] = []
        tools: dict[str, dict] = {}
        for r in rows:
            if not r:
                continue
            blocks = jload(r["content"], [])
            author = r["author"]
            who = names.get(author, {})
            base = {"id": r["id"], "ts": r["created_at"], "author": author, "turn_id": r.get("turn_id", "")}
            if r["kind"] == "notice":
                items.append({**base, "type": "notice", "level": (blocks[0].get("level") if blocks else "info"),
                              "text": blocks_text(blocks)})
                continue
            if r["role"] == "assistant":
                text = blocks_text(blocks)
                if text:
                    items.append({**base, "type": "assistant", "name": who.get("name", author), "emoji": who.get("emoji", ""),
                                  "text": text, "stream_id": blocks[0].get("stream_id", "") if blocks else ""})
                for blk in blocks:
                    if blk.get("type") == "tool_use":
                        it = {**base, "type": "tool", "call_id": blk["id"], "tool": blk["name"], "args": blk.get("input", {}),
                              "label": blk.get("label") or self._describe(blk["name"], blk.get("input", {})), "status": "running",
                              "summary": "", "images": [], "name": who.get("name", author), "emoji": who.get("emoji", "")}
                        tools[blk["id"]] = it
                        items.append(it)
            else:
                texts, images = [], []
                for blk in blocks:
                    if blk.get("type") == "tool_result":
                        ui = blk.get("ui") or {}
                        it = tools.get(blk["tool_use_id"])
                        patch = {"status": ui.get("status", "error" if blk.get("is_error") else "ok"), "summary": ui.get("summary", ""),
                                 "images": [os.path.basename(p) for p in ui.get("images", [])], "duration_ms": ui.get("duration_ms", 0),
                                 "url": ui.get("url", ""), "result_id": r["id"]}
                        if it is not None:
                            it.update(patch)
                        else:
                            items.append({**base, "type": "tool", "call_id": blk["tool_use_id"], "partial": True,
                                          "tool": ui.get("tool", ""), "label": ui.get("label", ""), **patch})
                    elif blk.get("type") == "text":
                        texts.append(blk["text"])
                    elif blk.get("type") == "image":
                        images.append(os.path.basename(blk.get("path", "")))
                if texts or images:
                    t = "\n".join(texts)
                    if author == "user":
                        items.append({**base, "type": "user", "text": t, "images": images})
                    else:
                        items.append({**base, "type": "system_note", "text": t})
        return items

    def export_markdown(self, thread_id: str) -> str:
        """A readable transcript: who said what and when, with tool use as one-line notes. No tool arguments or results."""
        from datetime import datetime
        th = self.get(thread_id)
        if not th:
            return ""
        names = self._names()
        owner = names.get(th.get("bot_id") or "", {})
        g = self.db.one("SELECT name FROM groups WHERE id=?", (th.get("group_id"),)) if th.get("group_id") else None
        stamp = lambda ts: datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")   # noqa: E731
        lines = [f"# {th.get('title') or 'Conversation'}", "",
                 f"_{(g or {}).get('name') or owner.get('name', 'Bot')} · exported {stamp(now())} from OpenGrokBot_", ""]
        for it in self.display(thread_id, 100000):
            t = it["type"]
            if t == "user":
                lines += [f"**You** · {stamp(it['ts'])}", "", it["text"], ""]
            elif t == "assistant":
                lines += [f"**{it.get('name') or 'Bot'}** · {stamp(it['ts'])}", "", it["text"], ""]
            elif t == "tool" and not it.get("partial"):
                lines += [f"> {it.get('label') or it.get('tool')} ({it.get('status', 'ok')})", ""]
        return "\n".join(lines).rstrip() + "\n"

    def last_user_text(self, thread_id: str) -> str:
        for r in reversed(self.rows(thread_id)):
            if r["kind"] == "llm" and r["author"] == "user" and r["role"] == "user":
                t = blocks_text(jload(r["content"], []))
                if t:
                    return t
        return ""

    def search(self, bot_id: str, query: str, limit: int = 10) -> list[dict]:
        like = f"%{query}%"
        rows = self.db.query(
            "SELECT m.id, m.thread_id, m.author, m.role, m.content, m.created_at, t.title FROM messages m "
            "JOIN threads t ON t.id=m.thread_id WHERE (t.bot_id=? OR t.id IN (SELECT thread_id FROM groups WHERE members LIKE ?)) "
            "AND m.kind='llm' AND m.content LIKE ? ORDER BY m.id DESC LIMIT ?", (bot_id, f"%{bot_id}%", like, limit * 3))
        out = []
        for r in rows:
            txt = blocks_text(jload(r["content"], []))
            if txt and query.lower() in txt.lower():
                out.append({"thread": r["title"], "thread_id": r["thread_id"], "author": r["author"], "ts": r["created_at"], "text": txt[:400]})
            if len(out) >= limit:
                break
        return out


class ThreadError(ValueError):
    pass


def fork_thread(threads: "Threads", thread_id: str, message_id: int, text: str | None = None) -> dict:
    """Branch a conversation. A copy of the thread is made up to a chosen message and continues from there on its own; the original
    is untouched, so you can try a different question or a different instruction and compare.

    With text=None the copy includes the chosen message ("branch from here"). With text, the copy stops just before the chosen user
    message and ends with your new wording in its place ("edit and resend"). Returns the new thread (with last_message_id)."""
    th = threads.get(thread_id)
    if not th:
        raise ThreadError("No such conversation.")
    if th.get("kind") == "group":
        raise ThreadError("Group chats cannot be branched. Branch a Bot's own conversation instead.")
    rows = threads.rows(thread_id)
    target = next((r for r in rows if r["id"] == int(message_id) and r["kind"] == "llm"), None)
    if target is None:
        raise ThreadError("That message is not part of this conversation.")
    if text is not None and not (target["role"] == "user" and target["author"] == "user"):
        raise ThreadError("Only your own messages can be edited.")
    if text is not None and not text.strip():
        raise ThreadError("The new message is empty.")
    key = lambda r: r["anchor"] or r["id"]   # noqa: E731  (tool results are stored after, and sorted with, the assistant message they answer)
    cut = key(target)
    keep = [r for r in rows if (key(r) < cut if text is not None else key(r) <= cut)]
    title = (th.get("title") or "Conversation")
    new = threads.create(th.get("bot_id"), ("Branch: " + title)[:80], kind="dm")
    nid = new["id"]
    idmap: dict[int, int] = {}
    last = 0
    for r in keep:
        anchor = idmap.get(r["anchor"]) if r["anchor"] else None
        new_id_ = threads.db.insert("messages", {"thread_id": nid, "author": r["author"], "role": r["role"], "kind": r["kind"], "content": r["content"], "anchor": anchor,
                                                 "turn_id": r["turn_id"], "created_at": r["created_at"]})
        idmap[r["id"]] = new_id_
        last = new_id_
    upto = int(th.get("summary_upto") or 0)
    if upto and th.get("summary") and keep and upto <= max(r["id"] for r in keep):
        covered = [idmap[r["id"]] for r in keep if r["id"] <= upto]
        if covered:
            threads.set_summary(nid, th["summary"], max(covered))
    if text is not None:
        blocks = [{"type": "text", "text": text.strip()}] + [b for b in jload(target["content"], []) if b.get("type") == "image"]
        last = threads.add(nid, "user", "user", blocks, publish=False)
    threads.db.update("threads", nid, {"updated_at": now()})
    threads.events.publish("threads", change="created", thread_id=nid, bot_id=th.get("bot_id", ""))
    out = threads.get(nid) or {}
    out["last_message_id"] = last
    out["copied"] = len(keep)
    return out
