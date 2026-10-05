"""The daily digest: what every Bot did in a window of time, built from the action log, turns, approvals and usage.
No model call is involved, so it is instant, free and cannot make anything up."""
from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

WRITE_TOOLS = {"fs_write", "fs_move"}
SPECS = ("today", "yesterday", "24h", "week")


def tokens(n: int) -> str:
    n = int(n or 0)
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def domain(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().removeprefix("www.")
    except ValueError:
        return ""


class Digest:
    def __init__(self, eng: "Engine"):
        self.eng = eng

    def window(self, spec: str = "today") -> tuple[float, float, str]:
        now = time.time()
        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        if spec == "yesterday":
            return (midnight - timedelta(days=1)).timestamp(), midnight.timestamp(), "Yesterday"
        if spec == "24h":
            return now - 86400, now, "Last 24 hours"
        if spec == "week":
            return self.eng.usage.week_bounds()[0], now, "This week"
        return midnight.timestamp(), now, "Today"

    def build(self, spec: str = "today") -> dict:
        db = self.eng.db
        start, end, label = self.window(spec if spec in SPECS else "today")
        bots_out, quiet = [], []
        total = Counter()
        for b in self.eng.bots.list():
            bid = b["id"]
            turns = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) AS n FROM turns WHERE bot_id=? AND started_at>=? AND started_at<=? GROUP BY status", (bid, start, end))}
            acts = db.query("SELECT tool, status, path, url FROM actions WHERE bot_id=? AND ts>=? AND ts<=?", (bid, start, end))
            appr = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) AS n FROM approvals WHERE bot_id=? AND decided_at>=? AND decided_at<=? GROUP BY status", (bid, start, end))}
            used = int(db.scalar("SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM usage WHERE bot_id=? AND ts>=? AND ts<=?", (bid, start, end), 0))
            tools = Counter(a["tool"] for a in acts)
            files = sorted({a["path"] for a in acts if a["tool"] in WRITE_TOOLS and a["path"] and a["status"] == "ok"})
            pages = Counter(domain(a["url"]) for a in acts if a["url"])
            pages.pop("", None)
            errors = sum(1 for a in acts if a["status"] == "error")
            row = {"bot_id": bid, "name": b["name"], "emoji": b["emoji"], "tasks": sum(turns.values()), "finished": turns.get("done", 0),
                   "problems": turns.get("error", 0) + turns.get("limit", 0), "stopped": turns.get("stopped", 0), "actions": len(acts), "errors": errors,
                   "top_tools": tools.most_common(4), "files": files, "sites": [d for d, _ in pages.most_common(5)],
                   "approved": appr.get("approved", 0), "denied": appr.get("denied", 0), "tokens": used}
            if row["tasks"] or row["actions"] or used:
                bots_out.append(row)
                for k in ("tasks", "finished", "problems", "actions", "approved", "denied", "tokens"):
                    total[k] += row[k]
                total["files"] += len(files)
            else:
                quiet.append(b["name"])
        bots_out.sort(key=lambda r: -r["tokens"])
        waiting = len(self.eng.approvals.list("pending"))
        return {"spec": spec, "label": label, "start": start, "end": end, "bots": bots_out, "quiet": quiet, "totals": dict(total), "waiting": waiting}

    def headline(self, d: dict) -> str:
        t = d["totals"]
        if not t.get("tasks") and not t.get("actions"):
            return f"{d['label']}: nothing happened yet."
        bits = [f"{t.get('tasks', 0)} task{'s' if t.get('tasks', 0) != 1 else ''}", f"{t.get('files', 0)} file{'s' if t.get('files', 0) != 1 else ''} written", f"{tokens(t.get('tokens', 0))} tokens"]
        if t.get("problems"):
            bits.append(f"{t['problems']} problem{'s' if t['problems'] != 1 else ''}")
        if d["waiting"]:
            bits.append(f"{d['waiting']} waiting for you")
        return f"{d['label']}: " + ", ".join(bits) + "."

    def markdown(self, d: dict, title: bool = True) -> str:
        t = d["totals"]
        lines = ([f"# {d['label']}", ""] if title else []) + [self.headline(d), ""]
        for r in d["bots"]:
            lines.append(f"## {r['emoji']} {r['name']}")
            parts = [f"{r['tasks']} task{'s' if r['tasks'] != 1 else ''} ({r['finished']} finished" + (f", {r['problems']} with problems" if r["problems"] else "") + ")",
                     f"{r['actions']} action{'s' if r['actions'] != 1 else ''}", f"{tokens(r['tokens'])} tokens"]
            lines.append("- " + " · ".join(parts))
            if r["top_tools"]:
                lines.append("- Most used: " + ", ".join(f"{n} ×{c}" for n, c in r["top_tools"]))
            if r["files"]:
                lines.append("- Wrote: " + ", ".join(f"`{p}`" for p in r["files"][:6]) + (f" and {len(r['files']) - 6} more" if len(r["files"]) > 6 else ""))
            if r["sites"]:
                lines.append("- Visited: " + ", ".join(r["sites"]))
            if r["approved"] or r["denied"]:
                lines.append(f"- Approvals: {r['approved']} approved, {r['denied']} denied")
            lines.append("")
        if d["quiet"]:
            lines.append("**Quiet:** " + ", ".join(d["quiet"]))
        if d["waiting"]:
            lines.append(f"**Waiting for you:** {d['waiting']} approval{'s' if d['waiting'] != 1 else ''} in the Inbox.")
        if t.get("tokens") is not None and not d["bots"]:
            lines.append("No Bot did anything in this period.")
        return "\n".join(lines).strip() + "\n"
