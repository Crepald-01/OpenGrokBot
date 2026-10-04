"""Per-Bot memory plus shared project context.

Memory is per Bot (preferences, role context, voice, edge cases, work summaries). Project
context is shared by all Bots so the user never has to paste notes between chats.
"""
from __future__ import annotations

import re
import time
from typing import Any

from . import secrets
from .db import Database, new_id, now

KINDS = ("preference", "role", "voice", "edge_case", "fact", "work_summary")
STABLE = ("role", "preference", "voice", "edge_case")


def _age(ts: float) -> str:
    d = (time.time() - ts) / 86400
    if d < 1:
        return "today"
    if d < 2:
        return "yesterday"
    return f"{int(d)}d ago"


class Memory:
    def __init__(self, db: Database):
        self.db = db

    # -- per Bot ------------------------------------------------------------
    def add(self, bot_id: str, kind: str, text: str, source: str = "", verify: bool = False, pinned: bool = False) -> int | None:
        text = secrets.redact(text.strip())
        if not text:
            return None
        kind = kind if kind in KINDS else "fact"
        dup = self.db.one("SELECT id FROM memories WHERE bot_id=? AND kind=? AND lower(text)=lower(?)", (bot_id, kind, text))
        if dup:
            self.db.update("memories", dup["id"], {"updated_at": now()})
            return dup["id"]
        return self.db.insert("memories", {"bot_id": bot_id, "kind": kind, "text": text[:1500], "source": source,
                                           "verify": int(verify), "pinned": int(pinned), "created_at": now(), "updated_at": now()})

    def update(self, bot_id: str, mem_id: int, text: str | None = None, kind: str | None = None, pinned: bool | None = None) -> bool:
        row = self.db.one("SELECT id FROM memories WHERE id=? AND bot_id=?", (mem_id, bot_id))
        if not row:
            return False
        patch: dict[str, Any] = {"updated_at": now()}
        if text is not None:
            patch["text"] = secrets.redact(text)[:1500]
        if kind in KINDS:
            patch["kind"] = kind
        if pinned is not None:
            patch["pinned"] = int(pinned)
        self.db.update("memories", mem_id, patch)
        return True

    def forget(self, bot_id: str, mem_id: int) -> bool:
        row = self.db.one("SELECT id FROM memories WHERE id=? AND bot_id=?", (mem_id, bot_id))
        if row:
            self.db.execute("DELETE FROM memories WHERE id=?", (mem_id,))
        return bool(row)

    def list(self, bot_id: str, kind: str | None = None) -> list[dict]:
        if kind:
            return self.db.query("SELECT * FROM memories WHERE bot_id=? AND kind=? ORDER BY pinned DESC, updated_at DESC", (bot_id, kind))
        return self.db.query("SELECT * FROM memories WHERE bot_id=? ORDER BY pinned DESC, kind, updated_at DESC", (bot_id,))

    def search(self, bot_id: str, query: str, limit: int = 15) -> list[dict]:
        words = [w for w in re.findall(r"\w{3,}", query.lower())][:6]
        rows = self.list(bot_id)
        scored = []
        for r in rows:
            t = r["text"].lower()
            score = sum(1 for w in words if w in t)
            if score or not words:
                scored.append((score, r["updated_at"], r))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [r for _, _, r in scored[:limit]]

    def prompt_block(self, bot_id: str, hint: str = "") -> str:
        rows = self.list(bot_id)
        if not rows:
            return "(No saved memory yet. Save stable preferences, role context and edge cases with memory_save as you learn them.)"
        out: list[str] = []
        stable = [r for r in rows if r["kind"] in STABLE or r["pinned"]][:40]
        if stable:
            out.append("Stable preferences, role context and edge cases:")
            out += [f"- [{r['kind']}#{r['id']}] {r['text']}" for r in stable]
        facts = [r for r in rows if r["kind"] == "fact" and not r["pinned"]][:15]
        if hint:
            hits = {r["id"] for r in self.search(bot_id, hint, 10)}
            facts = sorted(facts, key=lambda r: r["id"] not in hits)
        if facts:
            out.append("Facts (may be stale; saved dates shown):")
            out += [f"- [fact#{r['id']}, {_age(r['updated_at'])}{', VERIFY BEFORE USE' if r['verify'] else ''}] {r['text']}" for r in facts]
        work = [r for r in rows if r["kind"] == "work_summary"][:8]
        if work:
            out.append("Summaries of your recent work (newest first):")
            out += [f"- [{_age(r['updated_at'])}] {r['text']}" for r in work]
        return "\n".join(out)

    # -- shared project context --------------------------------------------
    def project_list(self) -> list[dict]:
        return self.db.query("SELECT id, name, updated_by, updated_at, length(content) AS size FROM projects ORDER BY updated_at DESC")

    def project_get(self, name: str) -> dict | None:
        return self.db.one("SELECT * FROM projects WHERE lower(name)=lower(?) OR id=?", (name, name))

    def project_write(self, name: str, content: str, by: str = "", append: bool = False) -> dict:
        name = name.strip()[:80]
        content = secrets.redact(content)
        cur = self.project_get(name)
        if cur:
            new = (cur["content"].rstrip() + "\n\n" + content) if append else content
            self.db.update("projects", cur["id"], {"content": new[:100000], "updated_by": by, "updated_at": now()})
            return self.project_get(name)  # type: ignore
        pid = new_id()
        self.db.insert("projects", {"id": pid, "name": name, "content": content[:100000], "updated_by": by, "updated_at": now()})
        return self.project_get(pid)  # type: ignore

    def project_delete(self, name: str) -> None:
        cur = self.project_get(name)
        if cur:
            self.db.execute("DELETE FROM projects WHERE id=?", (cur["id"],))

    def project_headlines(self) -> str:
        rows = self.project_list()
        if not rows:
            return "(none yet)"
        return "\n".join(f"- {r['name']} ({r['size']} chars, updated {_age(r['updated_at'])}"
                         f"{' by ' + r['updated_by'] if r['updated_by'] else ''})" for r in rows[:25])
