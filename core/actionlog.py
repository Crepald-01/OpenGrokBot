"""Full per-Bot action log: every tool call, page visited and file touched."""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from . import secrets
from .db import Database, jdump, now


def _clip(v: Any, n: int = 2000) -> str:
    s = v if isinstance(v, str) else jdump(v)
    s = secrets.redact(s)
    return s if len(s) <= n else s[:n] + f"... [{len(s) - n} more chars]"


class ActionLog:
    def __init__(self, db: Database):
        self.db = db

    def record(self, *, bot_id: str, thread_id: str = "", turn_id: str = "", tool: str, args: Any, result: str = "",
               status: str = "ok", category: str = "", url: str = "", path: str = "", duration_ms: int = 0) -> int:
        return self.db.insert("actions", {
            "ts": now(), "bot_id": bot_id, "thread_id": thread_id, "turn_id": turn_id, "tool": tool,
            "args": _clip(args), "result": _clip(result), "status": status, "category": category,
            "url": url[:500], "path": path[:500], "duration_ms": duration_ms})

    def list(self, bot_id: str | None = None, limit: int = 200, offset: int = 0, tool: str | None = None,
             status: str | None = None) -> list[dict]:
        where, params = [], []
        if bot_id:
            where.append("bot_id=?")
            params.append(bot_id)
        if tool:
            where.append("tool LIKE ?")
            params.append(f"%{tool}%")
        if status:
            where.append("status=?")
            params.append(status)
        sql = "SELECT * FROM actions" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ? OFFSET ?"
        return self.db.query(sql, [*params, limit, offset])

    def export(self, bot_id: str | None, fmt: str = "json") -> tuple[bytes, str, str]:
        rows = list(reversed(self.list(bot_id, limit=100000)))
        names = {b["id"]: b["name"] for b in self.db.query("SELECT id, name FROM bots")}
        for r in rows:
            r["bot"] = names.get(r["bot_id"], r["bot_id"])
        stem = f"action-log-{names.get(bot_id, 'all-bots') if bot_id else 'all-bots'}"
        if fmt == "csv":
            buf = io.StringIO()
            cols = ["id", "ts", "bot", "thread_id", "turn_id", "tool", "status", "category", "url", "path",
                    "duration_ms", "args", "result"]
            w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
            return buf.getvalue().encode("utf-8-sig"), "text/csv", stem + ".csv"
        return json.dumps(rows, indent=2, ensure_ascii=False).encode("utf-8"), "application/json", stem + ".json"
