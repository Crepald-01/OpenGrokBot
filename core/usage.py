"""Local usage tracker with a weekly reset."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from .db import Database, now
from .pricing import Pricing, key_for
from .settings import Admin, Settings


class Usage:
    def __init__(self, db: Database, settings: Settings, admin: Admin):
        self.db, self.settings, self.admin = db, settings, admin
        self.pricing = Pricing(settings)

    def week_bounds(self, at: float | None = None) -> tuple[float, float]:
        at = at or time.time()
        wd = int(self.settings.get("usage.reset_weekday", 0)) % 7  # 0 = Monday
        local = datetime.fromtimestamp(at)
        start = (local - timedelta(days=(local.weekday() - wd) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp(), (start + timedelta(days=7)).timestamp()

    def day_start(self, at: float | None = None) -> float:
        return datetime.fromtimestamp(at or time.time()).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def bot_today(self, bot_id: str) -> int:
        """Tokens this Bot has used since local midnight."""
        return int(self.db.scalar("SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM usage WHERE bot_id=? AND ts>=?", (bot_id, self.day_start()), 0))

    def bot_over_budget(self, bot: dict | None) -> bool:
        lim = int((bot or {}).get("daily_token_limit") or 0)
        return bool(lim) and self.bot_today(bot["id"]) >= lim  # type: ignore[index]

    def record(self, bot_id: str, turn_id: str, profile: str, model: str, input_tokens: int, output_tokens: int) -> None:
        self.db.insert("usage", {"ts": now(), "bot_id": bot_id, "turn_id": turn_id, "profile": profile, "model": model,
                                 "input_tokens": int(input_tokens), "output_tokens": int(output_tokens)})

    def limit(self) -> int:
        lim = int(self.settings.get("usage.weekly_token_limit", 0) or 0)
        adm = self.admin.weekly_token_limit()
        if adm:
            lim = min(lim, adm) if lim else adm
        return lim

    def week_total(self) -> int:
        start, _ = self.week_bounds()
        return int(self.db.scalar("SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM usage WHERE ts>=?", (start,), 0))

    def over_limit(self) -> bool:
        lim = self.limit()
        return bool(lim) and self.week_total() >= lim

    def cost_summary(self, since: float | None = None) -> dict:
        """Estimated spend from the prices you entered. Models without a price are listed in `unpriced`, never guessed."""
        start = since if since is not None else self.week_bounds()[0]
        today_start = self.day_start()

        def fold(from_ts: float) -> tuple[float, dict, dict, dict]:
            rows = self.db.query("SELECT bot_id, profile, model, SUM(input_tokens) AS i, SUM(output_tokens) AS o FROM usage WHERE ts>=? GROUP BY bot_id, profile, model", (from_ts,))
            total, per_bot, per_model, unpriced = 0.0, {}, {}, {}
            for r in rows:
                k = key_for(r["profile"], r["model"])
                m = per_model.setdefault(k, {"key": k, "profile": r["profile"], "model": r["model"], "input_tokens": 0, "output_tokens": 0, "cost": 0.0, "priced": True})
                m["input_tokens"] += r["i"] or 0
                m["output_tokens"] += r["o"] or 0
                c = self.pricing.cost(r["i"] or 0, r["o"] or 0, r["profile"], r["model"])
                if c is None:
                    m["priced"] = False
                    unpriced[k] = True
                else:
                    m["cost"] += c
                    total += c
                    per_bot[r["bot_id"]] = per_bot.get(r["bot_id"], 0.0) + c
            return total, per_bot, per_model, unpriced

        total, per_bot, per_model, unpriced = fold(start)
        today_total = fold(today_start)[0]
        p = self.pricing
        return {"currency": p.currency(), "total": total, "today": today_total, "per_bot": per_bot, "per_model": sorted(per_model.values(), key=lambda m: -(m["input_tokens"] + m["output_tokens"])),
                "unpriced": sorted(unpriced), "prices": p.table()}

    def summary(self) -> dict:
        start, end = self.week_bounds()
        per_bot = self.db.query(
            "SELECT u.bot_id, COALESCE(b.name,'(deleted)') AS name, COALESCE(b.emoji,'') AS emoji, "
            "SUM(u.input_tokens) AS input_tokens, SUM(u.output_tokens) AS output_tokens, COUNT(DISTINCT u.turn_id) AS turns "
            "FROM usage u LEFT JOIN bots b ON b.id=u.bot_id WHERE u.ts>=? GROUP BY u.bot_id ORDER BY 4+5 DESC", (start,))
        per_model = self.db.query(
            "SELECT profile, model, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens "
            "FROM usage WHERE ts>=? GROUP BY profile, model", (start,))
        daily = []
        for i in range(7):
            d0 = start + i * 86400
            row = self.db.one("SELECT COALESCE(SUM(input_tokens+output_tokens),0) AS t FROM usage WHERE ts>=? AND ts<?",
                              (d0, d0 + 86400))
            daily.append({"day": datetime.fromtimestamp(d0).strftime("%a"), "tokens": row["t"] if row else 0})
        total_in = sum(r["input_tokens"] or 0 for r in per_bot)
        total_out = sum(r["output_tokens"] or 0 for r in per_bot)
        today = {r["bot_id"]: int(r["t"]) for r in self.db.query(
            "SELECT bot_id, SUM(input_tokens+output_tokens) AS t FROM usage WHERE ts>=? GROUP BY bot_id", (self.day_start(),))}
        return {"week_start": start, "resets_at": end, "today": today, "cost": self.cost_summary(start), "input_tokens": total_in, "output_tokens": total_out,
                "total": total_in + total_out, "limit": self.limit(), "per_bot": per_bot, "per_model": per_model,
                "daily": daily}
