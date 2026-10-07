"""Workflows: a pipeline of Bots. Step 1's Bot does its part, its result goes to step 2's Bot, and so on.

    Researcher: find three competitors and summarise them  ->  Writer: turn {{previous}} into a one-page brief  ->  Inbox: email it

Each step is an ordinary task in its own chat thread (so approvals, budgets, memory and the action log all work as usual).
A step's instruction can use {{input}} (what you gave the workflow), {{previous}} (the last step's result) and {{step1}}, {{step2}}...
Results from earlier steps are passed on as data, not as instructions. If a step fails, the workflow stops there.
A workflow can be run by hand, on a schedule, or started by a trigger."""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

from . import injection
from .db import jdump, jload, new_id, now
from .routines import RoutineError, validate_cron

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

MAX_STEPS = 12
MAX_WORKFLOWS = 100
RESULT_CAP = 8000


class WorkflowError(ValueError):
    pass


def render_step(instruction: str, input_text: str, results: list[str], names: list[str]) -> tuple[str, bool]:
    """Fill {{input}}, {{previous}}, {{stepN}} in a step's instruction. Returns (text, suspicious) where suspicious means an earlier
    result contained instruction-like text (the step then runs with automatic approvals off)."""
    suspicious = False

    def wrapped(i: int) -> str:
        nonlocal suspicious
        text, hits = injection.wrap(results[i], f"the result of step {i + 1} ({names[i]})", max_chars=RESULT_CAP)
        suspicious = suspicious or bool(hits)
        return text

    def sub(m: re.Match) -> str:
        key = m.group(1).lower()
        if key == "input":
            return input_text
        if key == "previous":
            return wrapped(len(results) - 1) if results else "(this is the first step: there is no previous result)"
        mm = re.fullmatch(r"step(\d+)", key)
        if mm:
            n = int(mm.group(1)) - 1
            return wrapped(n) if 0 <= n < len(results) else f"(step {n + 1} has not run)"
        return m.group(0)

    text = re.sub(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}", sub, instruction)
    # the previous result is always handed over, even if the instruction forgot to mention it
    if results and not re.search(r"\{\{\s*(previous|step\d+)\s*\}\}", instruction, re.I):
        text += "\n\nResult of the previous step:\n" + wrapped(len(results) - 1)
    return text.strip(), suspicious


class Workflows:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.db = engine.db
        self.running: dict[str, str] = {}      # workflow id -> run id
        self.cancel: set[str] = set()          # run ids asked to stop
        self.current_thread: dict[str, str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------------- scheduling
    def start(self) -> None:
        for w in self.db.query("SELECT * FROM workflows WHERE enabled=1 AND cron!=''"):
            self._schedule(w)

    def _schedule(self, w: dict) -> None:
        try:
            self.engine.routines.sched.add_job(self._scheduled, validate_cron(w["cron"]), args=[w["id"]], id="wf:" + w["id"], replace_existing=True)
        except (RoutineError, Exception):  # noqa: BLE001
            pass

    def _unschedule(self, wid: str) -> None:
        try:
            self.engine.routines.sched.remove_job("wf:" + wid)
        except Exception:  # noqa: BLE001
            pass

    def _scheduled(self, wid: str) -> None:
        try:
            self.run(wid, "", wait=True)
        except WorkflowError:
            pass

    # ------------------------------------------------------------------------- CRUD
    def _clean_steps(self, steps) -> list[dict]:
        if not isinstance(steps, list) or not steps:
            raise WorkflowError("A workflow needs at least one step.")
        if len(steps) > MAX_STEPS:
            raise WorkflowError(f"A workflow can have at most {MAX_STEPS} steps.")
        out = []
        for i, s in enumerate(steps, 1):
            if not isinstance(s, dict):
                raise WorkflowError(f"Step {i} is not valid.")
            bot = self.engine.bots.get(str(s.get("bot_id", "")))
            if not bot or bot["archived"]:
                raise WorkflowError(f"Step {i}: choose a Bot.")
            ins = str(s.get("instruction", "")).strip()
            if not ins:
                raise WorkflowError(f"Step {i}: write what {bot['name']} should do.")
            out.append({"bot_id": bot["id"], "instruction": ins[:4000], "name": str(s.get("name", "")).strip()[:60]})
        return out

    def _out(self, w: dict) -> dict:
        w = dict(w)
        w["steps"] = jload(w["steps"], [])
        names = {b["id"]: b["name"] for b in self.engine.bots.list(include_archived=True)}
        for s in w["steps"]:
            s["bot_name"] = names.get(s["bot_id"], "(deleted)")
        w["enabled"] = bool(w["enabled"])
        w["running"] = w["id"] in self.running
        job = self.engine.routines.sched.get_job("wf:" + w["id"]) if self.engine.routines.sched.running else None
        w["next_run_at"] = job.next_run_time.timestamp() if job and job.next_run_time else 0
        last = self.db.one("SELECT status, started_at, ended_at FROM workflow_runs WHERE workflow_id=? ORDER BY started_at DESC LIMIT 1", (w["id"],))
        w["last_status"] = last["status"] if last else ""
        w["last_run_at"] = last["started_at"] if last else 0
        return w

    def list(self) -> list[dict]:
        return [self._out(w) for w in self.db.query("SELECT * FROM workflows ORDER BY created_at")]

    def get(self, wid: str) -> dict | None:
        w = self.db.one("SELECT * FROM workflows WHERE id=?", (wid,))
        return self._out(w) if w else None

    def create(self, name: str, steps: list, description: str = "", cron: str = "", enabled: bool = True) -> dict:
        name = (name or "").strip()[:60]
        if not name:
            raise WorkflowError("Give the workflow a name.")
        if int(self.db.scalar("SELECT COUNT(*) FROM workflows", (), 0)) >= MAX_WORKFLOWS:
            raise WorkflowError(f"There are already {MAX_WORKFLOWS} workflows.")
        steps = self._clean_steps(steps)
        cron = (cron or "").strip()
        if cron:
            validate_cron(cron)
        wid = new_id()
        self.db.insert("workflows", {"id": wid, "name": name, "description": (description or "").strip()[:300], "steps": jdump(steps), "cron": cron, "enabled": int(enabled),
                                     "created_at": now(), "updated_at": now()})
        if enabled and cron:
            self._schedule(self.db.one("SELECT * FROM workflows WHERE id=?", (wid,)))  # type: ignore
        self.engine.events.publish("workflows", change="created", workflow_id=wid)
        return self.get(wid)  # type: ignore

    def update(self, wid: str, **f) -> dict:
        cur = self.db.one("SELECT * FROM workflows WHERE id=?", (wid,))
        if not cur:
            raise WorkflowError("No such workflow.")
        patch: dict = {}
        if f.get("name") is not None:
            patch["name"] = str(f["name"]).strip()[:60] or cur["name"]
        if f.get("description") is not None:
            patch["description"] = str(f["description"]).strip()[:300]
        if f.get("steps") is not None:
            patch["steps"] = jdump(self._clean_steps(f["steps"]))
        if f.get("cron") is not None:
            cron = str(f["cron"]).strip()
            if cron:
                validate_cron(cron)
            patch["cron"] = cron
        if f.get("enabled") is not None:
            patch["enabled"] = int(bool(f["enabled"]))
        patch["updated_at"] = now()
        self.db.update("workflows", wid, patch)
        self._unschedule(wid)
        row = self.db.one("SELECT * FROM workflows WHERE id=?", (wid,))
        if row and row["enabled"] and row["cron"]:
            self._schedule(row)
        self.engine.events.publish("workflows", change="updated", workflow_id=wid)
        return self.get(wid)  # type: ignore

    def delete(self, wid: str) -> None:
        self._unschedule(wid)
        self.db.execute("DELETE FROM workflows WHERE id=?", (wid,))
        self.engine.events.publish("workflows", change="deleted", workflow_id=wid)

    def runs(self, wid: str | None = None, limit: int = 30) -> list[dict]:
        sql = "SELECT r.*, w.name AS workflow_name FROM workflow_runs r LEFT JOIN workflows w ON w.id=r.workflow_id"
        rows = self.db.query(sql + (" WHERE r.workflow_id=?" if wid else "") + " ORDER BY r.started_at DESC LIMIT ?", [wid, limit] if wid else [limit])
        for r in rows:
            r["steps"] = jload(r["steps"], [])
        return rows

    # ------------------------------------------------------------------------- running
    def run(self, wid: str, input_text: str = "", dry_run: bool = False, tainted: bool = False, wait: bool = False) -> dict:
        """Start a run. With wait=True it finishes before returning (used by schedules and triggers); otherwise it runs in the background."""
        w = self.db.one("SELECT * FROM workflows WHERE id=?", (wid,))
        if not w:
            raise WorkflowError("No such workflow.")
        with self._lock:
            if wid in self.running:
                raise WorkflowError("This workflow is already running.")
            run_id = new_id()
            self.running[wid] = run_id
        self.db.insert("workflow_runs", {"id": run_id, "workflow_id": wid, "started_at": now(), "status": "running", "input": (input_text or "")[:RESULT_CAP], "steps": "[]"})
        self.db.update("workflows", wid, {"last_run_at": now()})
        self.engine.events.publish("workflow_run", workflow_id=wid, run_id=run_id, status="running")
        if wait:
            self._execute(w, run_id, input_text or "", dry_run, tainted)
            return self.db.one("SELECT * FROM workflow_runs WHERE id=?", (run_id,))  # type: ignore
        threading.Thread(target=self._execute, args=(w, run_id, input_text or "", dry_run, tainted), daemon=True, name=f"workflow-{wid[:4]}").start()
        return {"run_id": run_id, "status": "running"}

    def stop(self, wid: str) -> bool:
        run_id = self.running.get(wid)
        if not run_id:
            return False
        self.cancel.add(run_id)
        th = self.current_thread.get(run_id)
        if th:
            self.engine.turns.stop_thread(th)
        return True

    def _execute(self, w: dict, run_id: str, input_text: str, dry_run: bool, tainted: bool) -> None:
        eng = self.engine
        wid = w["id"]
        steps = jload(w["steps"], [])
        names = {b["id"]: b["name"] for b in eng.bots.list(include_archived=True)}
        results: list[str] = []
        labels: list[str] = []
        records: list[dict] = []
        status, error, last_thread, last_bot = "ok", "", "", None
        try:
            for i, st in enumerate(steps, 1):
                if run_id in self.cancel:
                    status, error = "stopped", "Stopped before step %d." % i
                    break
                bot = eng.bots.get(st["bot_id"])
                label = st.get("name") or (bot["name"] if bot else "?")
                rec = {"n": i, "bot_id": st["bot_id"], "bot_name": names.get(st["bot_id"], "(deleted)"), "name": label, "status": "running", "result": "", "thread_id": "",
                       "started_at": now(), "ended_at": 0, "error": ""}
                records.append(rec)
                self._save(run_id, records)
                if not bot or bot["archived"]:
                    rec.update(status="failed", error="This Bot no longer exists.", ended_at=now())
                    status, error = "failed", f"Step {i}: this Bot no longer exists."
                    break
                text, suspicious = render_step(st["instruction"], input_text, results, labels)
                th = eng.threads.create(bot["id"], f"{w['name']} · step {i} · {datetime.now().strftime('%b %d %H:%M')}", kind="routine")
                rec["thread_id"] = last_thread = th["id"]
                last_bot = bot
                self.current_thread[run_id] = th["id"]
                if suspicious:
                    eng.threads.notice(th["id"], "A result passed on from an earlier step contains text that looks like instructions. I treat it as data only, and automatic approvals are off for this step.", "warn")
                task = (f"[Workflow: {w['name']} · step {i} of {len(steps)}] This is one step of an automated pipeline. Nobody is watching right now, so work end to end "
                        f"and finish with a result the next step can use (the result itself, not a description of it).\n\n{text}")
                res = eng.turns.run_sync(bot["id"], th["id"], task, trigger="routine", dry_run=dry_run, tainted=tainted or suspicious)
                rec["ended_at"] = now()
                rec["result"] = (res.final_text or "")[:RESULT_CAP]
                if run_id in self.cancel or res.status == "stopped":
                    rec.update(status="stopped")
                    status, error = "stopped", f"Stopped during step {i}."
                    break
                if res.status != "done":
                    why = {"limit": "ran out of steps", "skipped": "could not start (paused, over its budget or archived)"}.get(res.status, res.error or "failed")
                    rec.update(status="failed", error=why)
                    status, error = "failed", f"Step {i} ({label}) {why}."
                    break
                if not (res.final_text or "").strip():
                    rec.update(status="failed", error="no result")
                    status, error = "failed", f"Step {i} ({label}) finished without a result to pass on."
                    break
                rec["status"] = "ok"
                results.append(res.final_text)
                labels.append(label)
                self._save(run_id, records)
        except Exception as e:  # noqa: BLE001
            status, error = "failed", f"{type(e).__name__}: {e}"
        finally:
            with self._lock:
                self.running.pop(wid, None)
            self.cancel.discard(run_id)
            self.current_thread.pop(run_id, None)
        final = results[-1] if status == "ok" and results else ""
        self.db.update("workflow_runs", run_id, {"ended_at": now(), "status": status, "steps": jdump(records), "result": final[:RESULT_CAP], "error": error[:500]})
        eng.events.publish("workflow_run", workflow_id=wid, run_id=run_id, status=status)
        if last_bot:
            eng.notify("routine", last_bot, last_thread, f"Workflow {'finished' if status == 'ok' else status}: {w['name']}", (final or error)[:160], urgent=status != "ok")

    def _save(self, run_id: str, records: list[dict]) -> None:
        self.db.update("workflow_runs", run_id, {"steps": jdump(records)})
        self.engine.events.publish("workflow_run", run_id=run_id, status="running")
