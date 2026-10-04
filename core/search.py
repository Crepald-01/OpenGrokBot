"""Search across everything the user can see: every conversation (Bots and groups) and every Bot's memory."""
from __future__ import annotations

import re
from typing import Callable

from .db import Database, jload
from .threads import blocks_text

MAX_TERMS = 5


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def snippet(text: str, terms: list[str], width: int = 70) -> str:
    flat = " ".join(text.split())
    low = flat.lower()
    pos = min((low.find(t) for t in terms if t in low), default=0)
    start = max(0, pos - width)
    end = min(len(flat), pos + width * 2)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


class Search:
    def __init__(self, db: Database, names: Callable[[], dict[str, dict]]):
        self.db, self._names = db, names

    def run(self, query: str, limit: int = 30) -> dict:
        terms = [t for t in re.split(r"\s+", (query or "").lower().strip()) if t][:MAX_TERMS]
        if not terms or sum(len(t) for t in terms) < 2:
            return {"query": query, "messages": [], "memories": []}
        names = self._names()
        where = " AND ".join("m.content LIKE ? ESCAPE '\\'" for _ in terms)
        rows = self.db.query(
            "SELECT m.id, m.thread_id, m.author, m.role, m.content, m.created_at, t.title, t.bot_id, t.group_id, t.kind "
            f"FROM messages m JOIN threads t ON t.id=m.thread_id WHERE m.kind='llm' AND {where} ORDER BY m.id DESC LIMIT ?",
            [*(_like(t) for t in terms), limit * 4])
        groups: dict[str, str] = {}
        messages = []
        for r in rows:
            text = blocks_text(jload(r["content"], []))
            low = text.lower()
            if not text or not all(t in low for t in terms):
                continue
            if r["group_id"] and r["group_id"] not in groups:
                g = self.db.one("SELECT name FROM groups WHERE id=?", (r["group_id"],))
                groups[r["group_id"]] = g["name"] if g else "Group chat"
            bot = names.get(r["bot_id"], {})
            who = "You" if r["author"] == "user" else names.get(r["author"], {}).get("name", r["author"])
            messages.append({"type": "message", "thread_id": r["thread_id"], "bot_id": r["bot_id"], "ts": r["created_at"],
                             "title": r["title"] or "Chat", "where": groups.get(r["group_id"]) or bot.get("name", "Bot"),
                             "emoji": bot.get("emoji", ""), "who": who, "snippet": snippet(text, terms), "group_id": r["group_id"]})
            if len(messages) >= limit:
                break
        mrows = self.db.query("SELECT id, bot_id, text, updated_at FROM memories WHERE " + " AND ".join("text LIKE ? ESCAPE '\\'" for _ in terms)
                              + " ORDER BY updated_at DESC LIMIT 10", [_like(t) for t in terms])
        memories = [{"type": "memory", "bot_id": m["bot_id"], "ts": m["updated_at"], "where": names.get(m["bot_id"], {}).get("name", "Bot"),
                     "emoji": names.get(m["bot_id"], {}).get("emoji", ""), "snippet": snippet(m["text"], terms), "memory_id": m["id"]} for m in mrows]
        return {"query": query, "messages": messages, "memories": memories}
