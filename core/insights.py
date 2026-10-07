"""Insights: how your Bots are doing over time, worked out from the turns, actions and usage the app already records.
No model call is made and nothing is sent anywhere."""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

RANGES = (7, 30, 90)
FINISHED = ("done", "error", "limit", "stopped")


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


class Insights:
    def __init__(self, engine: "Engine"):
        self.eng = engine

    def build(self, days: int = 7, bot_id: str | None = None) -> dict:
        days = days if days in RANGES else 7
        db = self.eng.db
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start = (today - timedelta(days=days - 1)).timestamp()
        names = {b["id"]: b for b in self.eng.bots.list(include_archived=True)}
        bw, bargs = (" AND bot_id=?", [bot_id]) if bot_id else ("", [])

        turns = db.query(f"SELECT bot_id, status, started_at, ended_at, steps, trigger FROM turns WHERE started_at>=? AND status IN ('done','error','limit','stopped'){bw}", [start, *bargs])
        per_day = {}
        for i in range(days):
            key = (today - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
            per_day[key] = {"day": key, "tasks": 0, "ok": 0, "problems": 0, "tokens": 0}
        hours = [0] * 24
        per_bot: dict[str, dict] = {}
        durations: list[float] = []
        triggers: dict[str, int] = {}
        for t in turns:
            d = per_day.get(_day(t["started_at"]))
            ok = t["status"] == "done"
            if d is not None:
                d["tasks"] += 1
                d["ok" if ok else "problems"] += 1
            hours[datetime.fromtimestamp(t["started_at"]).hour] += 1
            secs = max(0.0, (t["ended_at"] or t["started_at"]) - t["started_at"])
            durations.append(secs)
            triggers[t["trigger"]] = triggers.get(t["trigger"], 0) + 1
            b = per_bot.setdefault(t["bot_id"], {"bot_id": t["bot_id"], "tasks": 0, "ok": 0, "problems": 0, "seconds": 0.0, "steps": 0, "tokens": 0})
            b["tasks"] += 1
            b["ok" if ok else "problems"] += 1
            b["seconds"] += secs
            b["steps"] += t["steps"] or 0

        for r in db.query(f"SELECT ts, bot_id, input_tokens+output_tokens AS n FROM usage WHERE ts>=?{bw}", [start, *bargs]):
            d = per_day.get(_day(r["ts"]))
            if d is not None:
                d["tokens"] += r["n"] or 0
            if r["bot_id"] in per_bot:
                per_bot[r["bot_id"]]["tokens"] += r["n"] or 0
            elif r["bot_id"]:
                per_bot[r["bot_id"]] = {"bot_id": r["bot_id"], "tasks": 0, "ok": 0, "problems": 0, "seconds": 0.0, "steps": 0, "tokens": r["n"] or 0}

        tools = db.query(f"SELECT tool, COUNT(*) AS n, SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors, SUM(CASE WHEN status='denied' THEN 1 ELSE 0 END) AS denied, "
                         f"CAST(AVG(duration_ms) AS INTEGER) AS avg_ms FROM actions WHERE ts>=?{bw} GROUP BY tool ORDER BY n DESC LIMIT 12", [start, *bargs])
        problems = db.query(f"SELECT ts, bot_id, tool, substr(result,1,160) AS text FROM actions WHERE ts>=? AND status='error'{bw} ORDER BY id DESC LIMIT 8", [start, *bargs])
        approvals = {r["status"]: r["n"] for r in db.query(f"SELECT status, COUNT(*) AS n FROM approvals WHERE created_at>=?{bw} GROUP BY status", [start, *bargs])}

        total = len(turns)
        ok_n = sum(1 for t in turns if t["status"] == "done")
        durations.sort()
        bots = []
        for bid, b in per_bot.items():
            info = names.get(bid, {})
            bots.append({**b, "name": info.get("name", "(deleted)"), "emoji": info.get("emoji", ""), "success": (b["ok"] / b["tasks"]) if b["tasks"] else None,
                         "avg_seconds": (b["seconds"] / b["tasks"]) if b["tasks"] else 0.0, "seconds": round(b["seconds"], 1)})
        bots.sort(key=lambda b: (-b["tasks"], -b["tokens"]))
        for p in problems:
            p["bot_name"] = names.get(p["bot_id"], {}).get("name", "")
        busiest = max(range(24), key=lambda h: hours[h]) if any(hours) else None
        return {
            "days": days, "start": start, "bot_id": bot_id or "",
            "totals": {"tasks": total, "ok": ok_n, "problems": total - ok_n, "success": (ok_n / total) if total else None, "tokens": sum(d["tokens"] for d in per_day.values()),
                       "avg_seconds": (sum(durations) / total) if total else 0.0, "median_seconds": durations[len(durations) // 2] if durations else 0.0,
                       "actions": int(db.scalar(f"SELECT COUNT(*) FROM actions WHERE ts>=?{bw}", [start, *bargs], 0)), "approvals": approvals, "busiest_hour": busiest},
            "per_day": list(per_day.values()), "hours": hours, "tools": tools, "bots": bots, "problems": problems, "triggers": triggers,
        }
