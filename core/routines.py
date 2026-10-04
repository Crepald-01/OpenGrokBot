"""Routines: skills saved as cron-scheduled jobs, per Bot, with run history.

Routines run inside the background service so they keep running when the app window is closed.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .db import new_id, now

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

PRESETS = [
    ("Every weekday at 8:00", "0 8 * * 1-5"),
    ("Every day at 7:00", "0 7 * * *"),
    ("Overnight (2:00 every day)", "0 2 * * *"),
    ("Every hour", "0 * * * *"),
    ("Every Monday at 9:00", "0 9 * * 1"),
    ("First of the month at 9:00", "0 9 1 * *"),
]


class RoutineError(ValueError):
    pass


def validate_cron(expr: str) -> CronTrigger:
    try:
        return CronTrigger.from_crontab(expr.strip())
    except (ValueError, TypeError) as e:
        raise RoutineError(f"Invalid cron expression '{expr}': {e}. Use 5 fields, e.g. '0 8 * * 1-5'.") from e


class Routines:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.db = engine.db
        self.sched = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300})
        self.running: set[str] = set()

    # -- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        self.sched.start()
        for r in self.db.query("SELECT * FROM routines WHERE enabled=1"):
            self._schedule(r)
            if r["catch_up"]:
                self._catch_up(r)

    def stop(self) -> None:
        try:
            self.sched.shutdown(wait=False)
        except Exception:
            pass

    def _schedule(self, r: dict) -> None:
        try:
            trig = validate_cron(r["cron"])
        except RoutineError:
            return
        self.sched.add_job(self._fire, trig, args=[r["id"]], id=r["id"], replace_existing=True)

    def _catch_up(self, r: dict) -> None:
        try:
            trig = validate_cron(r["cron"])
            prev_ts = r["last_run_at"] or r["created_at"]
            prev = datetime.fromtimestamp(prev_ts).astimezone()
            nxt = trig.get_next_fire_time(prev, prev)
            if nxt and nxt.timestamp() <= time.time():
                threading.Thread(target=self._fire, args=[r["id"]], daemon=True, name="routine-catchup").start()
        except Exception:
            pass

    # -- CRUD ---------------------------------------------------------------------
    def _out(self, r: dict) -> dict:
        r = dict(r)
        job = self.sched.get_job(r["id"]) if self.sched.running else None
        r["next_run_at"] = job.next_run_time.timestamp() if job and job.next_run_time else 0
        r["running"] = r["id"] in self.running
        last = self.db.one("SELECT status, ended_at FROM routine_runs WHERE routine_id=? ORDER BY started_at DESC LIMIT 1", (r["id"],))
        r["last_status"] = last["status"] if last else ""
        bot = self.engine.bots.get(r["bot_id"])
        r["bot_name"] = bot["name"] if bot else "(deleted)"
        return r

    def list(self, bot_id: str | None = None) -> list[dict]:
        rows = self.db.query("SELECT * FROM routines" + (" WHERE bot_id=?" if bot_id else "") + " ORDER BY created_at", (bot_id,) if bot_id else ())
        return [self._out(r) for r in rows]

    def get(self, rid: str) -> dict | None:
        r = self.db.one("SELECT * FROM routines WHERE id=?", (rid,))
        return self._out(r) if r else None

    def create(self, bot_id: str, name: str, cron: str, skill: str = "", prompt: str = "", enabled: bool = True,
               dry_run: bool = False, notify: str = "always", catch_up: bool = False) -> dict:
        validate_cron(cron)
        if not self.engine.bots.get(bot_id):
            raise RoutineError("No such Bot.")
        if not (skill or prompt):
            raise RoutineError("A routine needs a skill, a prompt, or both.")
        if skill and not self.engine.skills.get(skill):
            raise RoutineError(f"No skill named '{skill}'.")
        rid = new_id()
        self.db.insert("routines", {"id": rid, "bot_id": bot_id, "name": name.strip() or "Routine", "skill": skill, "prompt": prompt,
                                    "cron": cron.strip(), "enabled": int(enabled), "dry_run": int(dry_run),
                                    "notify": notify if notify in ("always", "failures", "never") else "always",
                                    "catch_up": int(catch_up), "created_at": now()})
        if enabled:
            self._schedule(self.db.one("SELECT * FROM routines WHERE id=?", (rid,)))  # type: ignore
        self.engine.events.publish("routines", change="created", routine_id=rid)
        return self.get(rid)  # type: ignore

    def update(self, rid: str, **f) -> dict:
        cur = self.db.one("SELECT * FROM routines WHERE id=?", (rid,))
        if not cur:
            raise RoutineError("No such routine.")
        patch = {}
        for k in ("name", "skill", "prompt", "cron", "enabled", "dry_run", "notify", "catch_up", "bot_id"):
            if k in f and f[k] is not None:
                patch[k] = int(f[k]) if k in ("enabled", "dry_run", "catch_up") else f[k]
        if "cron" in patch:
            validate_cron(patch["cron"])
        if patch:
            self.db.update("routines", rid, patch)
        row = self.db.one("SELECT * FROM routines WHERE id=?", (rid,))
        try:
            self.sched.remove_job(rid)
        except Exception:
            pass
        if row and row["enabled"]:
            self._schedule(row)
        self.engine.events.publish("routines", change="updated", routine_id=rid)
        return self.get(rid)  # type: ignore

    def delete(self, rid: str) -> None:
        try:
            self.sched.remove_job(rid)
        except Exception:
            pass
        self.db.execute("DELETE FROM routines WHERE id=?", (rid,))
        self.engine.events.publish("routines", change="deleted", routine_id=rid)

    def runs(self, rid: str | None = None, bot_id: str | None = None, limit: int = 50) -> list[dict]:
        where, params = [], []
        if rid:
            where.append("rr.routine_id=?")
            params.append(rid)
        if bot_id:
            where.append("rr.bot_id=?")
            params.append(bot_id)
        sql = ("SELECT rr.*, r.name AS routine_name, b.name AS bot_name FROM routine_runs rr LEFT JOIN routines r ON r.id=rr.routine_id "
               "LEFT JOIN bots b ON b.id=rr.bot_id" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY rr.started_at DESC LIMIT ?")
        return self.db.query(sql, [*params, limit])

    # -- execution ----------------------------------------------------------------
    def run_now(self, rid: str) -> None:
        if not self.db.one("SELECT id FROM routines WHERE id=?", (rid,)):
            raise RoutineError("No such routine.")
        threading.Thread(target=self._fire, args=[rid, True], daemon=True, name=f"routine-{rid}").start()

    def _fire(self, rid: str, manual: bool = False) -> None:
        r = self.db.one("SELECT * FROM routines WHERE id=?", (rid,))
        if not r or rid in self.running:
            return
        self.running.add(rid)
        run_id = new_id()
        self.db.insert("routine_runs", {"id": run_id, "routine_id": rid, "bot_id": r["bot_id"], "started_at": now(), "status": "running"})
        self.engine.events.publish("routine_run", routine_id=rid, run_id=run_id, status="running")
        status, result, error, thread_id, turn_id = "ok", "", "", "", ""
        try:
            bot = self.engine.bots.get(r["bot_id"])
            if not bot or bot["paused"] or bot["archived"]:
                status, error = "skipped", "Bot is paused or archived."
            elif self.engine.usage.over_limit():
                status, error = "skipped", "Weekly usage limit reached."
            elif self.engine.usage.bot_over_budget(bot):
                status, error = "skipped", "This Bot's daily token budget is used up."
            else:
                th = self.engine.threads.create(bot["id"], f"{r['name']} · {datetime.now().strftime('%b %d %H:%M')}", kind="routine")
                thread_id = th["id"]
                skill_text = ""
                if r["skill"]:
                    sk = self.engine.skills.get(r["skill"])
                    if sk:
                        skill_text = f"Follow this skill:\n\n{sk['body']}\n\n"
                task = (f"[Routine: {r['name']}] This is a scheduled run. Nobody is watching right now, so work end to end "
                        f"and keep the final report short and concrete (what you did, results, anything needing the user).\n\n"
                        f"{skill_text}{r['prompt']}").strip()
                res = self.engine.turns.run_sync(bot["id"], thread_id, task, trigger="routine", dry_run=bool(r["dry_run"]))
                turn_id, result = res.turn_id, res.final_text
                status = {"done": "ok", "stopped": "stopped"}.get(res.status, "error")
                error = res.error
        except Exception as e:  # noqa: BLE001
            status, error = "error", f"{type(e).__name__}: {e}"
        finally:
            self.running.discard(rid)
        self.db.update("routine_runs", run_id, {"ended_at": now(), "status": status, "result": result[:8000], "error": error[:2000],
                                                "thread_id": thread_id, "turn_id": turn_id})
        self.db.update("routines", rid, {"last_run_at": now()})
        self.engine.events.publish("routine_run", routine_id=rid, run_id=run_id, status=status)
        bot = self.engine.bots.get(r["bot_id"])
        if bot and (r["notify"] == "always" or (r["notify"] == "failures" and status not in ("ok",))) and status != "skipped":
            self.engine.notify("routine", bot, thread_id, f"Routine {'finished' if status == 'ok' else status}: {r['name']}",
                               (result or error)[:160], urgent=status != "ok")
