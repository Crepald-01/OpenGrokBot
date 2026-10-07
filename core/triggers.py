"""Triggers: start a Bot's work when something happens, instead of on a clock.

  webhook      an address that anything can POST to (a form, a script, a service such as GitHub or Zapier). The Bot gets your
               instructions plus what was sent.
  folder watch a workspace folder is checked every few seconds; when a new or changed file matching a pattern appears, the Bot
               is told which file.

A webhook's address contains a secret. Only a hash of the secret is stored, so it is shown once (and can be regenerated).
Everything that arrives in a webhook is treated as untrusted data: it is wrapped like web content, and a payload that looks like
an attack marks the run so nothing is approved automatically."""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
import re
import secrets as pysecrets
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from . import injection
from .db import new_id, now

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

KINDS = ("webhook", "folder")
MAX_PAYLOAD = 64 * 1024
MAX_TRIGGERS = 100
MIN_GAP = 2.0                 # seconds between two firings of the same trigger
FOLDER_MAX_FILES = 2000
FOLDER_BATCH = 5              # files started per check, so a big drop does not start dozens of tasks at once
SETTLE_SECONDS = 3.0          # a file must be this old (unchanged) before it counts, so half-written files are not picked up


class TriggerError(ValueError):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status   # the HTTP status a webhook caller gets


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _dig(data, path: str):
    """a.b.0.c lookup in parsed JSON; None when it is not there."""
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def render(prompt: str, ctx: dict) -> tuple[str, list[str]]:
    """Fill {{placeholders}} in a trigger's instructions. Returns (task text, suspicious fragments found in outside data).

    {{payload}} the raw body, {{json.a.b}} a field of a JSON body, {{file}} {{filename}} {{folder}}, {{trigger}}, {{time}}.
    Outside data (payload and its fields) is wrapped as untrusted content; if the prompt never mentions it, it is added at the end."""
    hits: list[str] = []
    payload = ctx.get("payload", "")
    parsed = None
    if payload:
        try:
            parsed = json.loads(payload)
        except ValueError:
            parsed = None
    used_payload = False

    def sub(m: re.Match) -> str:
        nonlocal used_payload
        key = m.group(1).strip()
        if key == "payload":
            used_payload = True
            wrapped, h = injection.wrap(payload, "a webhook")
            hits.extend(h)
            return wrapped
        if key.startswith("json."):
            used_payload = True
            v = _dig(parsed, key[5:]) if parsed is not None else None
            text = "" if v is None else (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
            hits.extend(injection.scan(text))
            return text[:2000]
        if key in ("file", "filename", "folder"):
            return str(ctx.get(key, ""))
        if key == "trigger":
            return str(ctx.get("trigger", ""))
        if key == "time":
            return datetime.now().strftime("%Y-%m-%d %H:%M")
        return m.group(0)

    text = re.sub(r"\{\{\s*([\w.\-]+)\s*\}\}", sub, prompt or "")
    if payload and not used_payload:
        wrapped, h = injection.wrap(payload, "a webhook")
        hits.extend(h)
        text += "\n\nThe request that triggered this:\n" + wrapped
    return text.strip(), hits


class Triggers:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.db = engine.db
        self.running: set[str] = set()
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------------- CRUD
    def _out(self, r: dict) -> dict:
        r = {k: v for k, v in r.items() if k != "secret_hash"}
        r["enabled"], r["dry_run"] = bool(r["enabled"]), bool(r["dry_run"])
        if r.get("workflow_id"):
            wf = self.engine.workflows.get(r["workflow_id"])
            r["bot_name"] = ("Workflow: " + wf["name"]) if wf else "(deleted workflow)"
        else:
            bot = self.engine.bots.get(r["bot_id"])
            r["bot_name"] = bot["name"] if bot else "(deleted)"
        r["running"] = r["id"] in self.running
        r["path"] = f"/hooks/{r['id']}/…{r['secret_hint']}" if r["kind"] == "webhook" else ""
        last = self.db.one("SELECT status, detail FROM trigger_runs WHERE trigger_id=? ORDER BY started_at DESC LIMIT 1", (r["id"],))
        r["last_status"], r["last_detail"] = (last["status"], last["detail"]) if last else ("", "")
        return r

    def list(self) -> list[dict]:
        return [self._out(r) for r in self.db.query("SELECT * FROM triggers ORDER BY created_at")]

    def get(self, tid: str) -> dict | None:
        r = self.db.one("SELECT * FROM triggers WHERE id=?", (tid,))
        return self._out(r) if r else None

    def _check(self, kind: str, bot_id: str, prompt: str, folder: str, pattern: str, workflow_id: str = "") -> str:
        if kind not in KINDS:
            raise TriggerError("A trigger is a webhook or a folder watch.")
        if workflow_id:
            if not self.engine.workflows.get(workflow_id):
                raise TriggerError("Choose an existing workflow for the trigger to run.")
        elif not self.engine.bots.get(bot_id):
            raise TriggerError("Choose a Bot (or a workflow) for the trigger to run.")
        elif not (prompt or "").strip():
            raise TriggerError("Write what the Bot should do when this fires.")
        if kind == "folder":
            try:
                p = self.engine.files.resolve(folder)
            except Exception as e:  # noqa: BLE001
                raise TriggerError(str(e)) from e
            if not p.is_dir():
                raise TriggerError("Choose an existing folder in the workspace to watch.")
            return (pattern or "*").strip() or "*"
        return "*"

    def create(self, kind: str, name: str, bot_id: str, prompt: str, folder: str = "", pattern: str = "*", dry_run: bool = False, workflow_id: str = "") -> dict:
        if int(self.db.scalar("SELECT COUNT(*) FROM triggers", (), 0)) >= MAX_TRIGGERS:
            raise TriggerError(f"There are already {MAX_TRIGGERS} triggers.")
        pattern = self._check(kind, bot_id, prompt, folder, pattern, workflow_id)
        tid = new_id()
        secret = pysecrets.token_urlsafe(24) if kind == "webhook" else ""
        folder_rel = self.engine.files.rel(self.engine.files.resolve(folder)) if kind == "folder" else ""
        self.db.insert("triggers", {"id": tid, "name": (name or "").strip()[:60] or ("Webhook" if kind == "webhook" else "Folder watch"), "kind": kind, "bot_id": "" if workflow_id else bot_id, "workflow_id": workflow_id or "",
                                    "prompt": (prompt or "").strip(), "secret_hash": _hash(secret) if secret else "", "secret_hint": secret[-4:] if secret else "",
                                    "folder": folder_rel, "pattern": pattern, "dry_run": int(dry_run), "created_at": now()})
        if kind == "folder":
            self._seed(tid)   # files already there do not fire: only what appears from now on
        self.engine.events.publish("triggers", change="created", trigger_id=tid)
        out = self.get(tid)
        if secret:
            out["secret"] = secret
            out["url_path"] = f"/hooks/{tid}/{secret}"
        return out  # type: ignore

    def update(self, tid: str, **f) -> dict:
        cur = self.db.one("SELECT * FROM triggers WHERE id=?", (tid,))
        if not cur:
            raise TriggerError("No such trigger.")
        patch = {}
        for k in ("name", "bot_id", "prompt", "pattern", "enabled", "dry_run"):
            if k in f and f[k] is not None:
                patch[k] = int(f[k]) if k in ("enabled", "dry_run") else f[k]
        self._check(cur["kind"], patch.get("bot_id", cur["bot_id"]), patch.get("prompt", cur["prompt"]), cur["folder"], patch.get("pattern", cur["pattern"]), cur.get("workflow_id", ""))
        if "name" in patch:
            patch["name"] = str(patch["name"]).strip()[:60] or cur["name"]
        if patch:
            self.db.update("triggers", tid, patch)
        self.engine.events.publish("triggers", change="updated", trigger_id=tid)
        return self.get(tid)  # type: ignore

    def regenerate(self, tid: str) -> dict:
        cur = self.db.one("SELECT * FROM triggers WHERE id=?", (tid,))
        if not cur or cur["kind"] != "webhook":
            raise TriggerError("Only webhooks have an address to regenerate.")
        secret = pysecrets.token_urlsafe(24)
        self.db.update("triggers", tid, {"secret_hash": _hash(secret), "secret_hint": secret[-4:]})
        out = self.get(tid)
        out["secret"], out["url_path"] = secret, f"/hooks/{tid}/{secret}"  # type: ignore
        return out  # type: ignore

    def delete(self, tid: str) -> None:
        self.db.execute("DELETE FROM triggers WHERE id=?", (tid,))
        self.db.execute("DELETE FROM trigger_seen WHERE trigger_id=?", (tid,))
        self.engine.events.publish("triggers", change="deleted", trigger_id=tid)

    def runs(self, tid: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT r.*, t.name AS trigger_name FROM trigger_runs r LEFT JOIN triggers t ON t.id=r.trigger_id"
        rows = self.db.query(sql + (" WHERE r.trigger_id=?" if tid else "") + " ORDER BY r.started_at DESC LIMIT ?", ([tid, limit] if tid else [limit]))
        return rows

    # ------------------------------------------------------------------------- webhooks
    def fire_webhook(self, tid: str, secret: str, body: bytes, content_type: str = "") -> dict:
        row = self.db.one("SELECT * FROM triggers WHERE id=? AND kind='webhook'", (tid,))
        # the same answer for "no such trigger" and "wrong secret", so addresses cannot be probed
        if not row or not hmac.compare_digest(_hash(secret or ""), row["secret_hash"]):
            raise PermissionError("Not found.")
        if not row["enabled"]:
            raise TriggerError("This trigger is switched off.", 403)
        if len(body) > MAX_PAYLOAD:
            raise TriggerError("That request is too large (the limit is 64 KB).", 413)
        payload = body.decode("utf-8", errors="replace")
        return self._start(row, {"payload": payload, "content_type": content_type})

    def fire_test(self, tid: str, payload: str = "") -> dict:
        """Run a trigger by hand from the app (a webhook gets the given sample payload; a folder watch gets a placeholder file name)."""
        row = self.db.one("SELECT * FROM triggers WHERE id=?", (tid,))
        if not row:
            raise TriggerError("No such trigger.")
        ctx = {"payload": payload} if row["kind"] == "webhook" else {"file": f"{row['folder']}/example.txt", "filename": "example.txt", "folder": row["folder"]}
        return self._start(row, ctx, manual=True)

    # ------------------------------------------------------------------------- folder watching
    def _scan(self, row: dict) -> dict[str, tuple[float, int]]:
        root = self.engine.files.resolve(row["folder"])
        out: dict[str, tuple[float, int]] = {}
        n = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith(".") or not fnmatch.fnmatch(fn.lower(), row["pattern"].lower()):
                    continue
                n += 1
                if n > FOLDER_MAX_FILES:
                    return out
                f = Path(dirpath) / fn
                try:
                    st = f.stat()
                except OSError:
                    continue
                out[self.engine.files.rel(f)] = (st.st_mtime, st.st_size)
        return out

    def _seed(self, tid: str) -> None:
        row = self.db.one("SELECT * FROM triggers WHERE id=?", (tid,))
        if not row:
            return
        for path, (m, sz) in self._scan(row).items():
            self.db.execute("INSERT OR REPLACE INTO trigger_seen(trigger_id, path, mtime, size) VALUES(?,?,?,?)", (tid, path, m, sz))

    def tick(self) -> int:
        """Check every enabled folder watch once. Returns how many tasks were started."""
        started = 0
        for row in self.db.query("SELECT * FROM triggers WHERE kind='folder' AND enabled=1"):
            try:
                seen = {r["path"]: (r["mtime"], r["size"]) for r in self.db.query("SELECT * FROM trigger_seen WHERE trigger_id=?", (row["id"],))}
                now_files = self._scan(row)
            except Exception:  # noqa: BLE001  (the folder was deleted or is unreadable: skip this round)
                continue
            for gone in set(seen) - set(now_files):
                self.db.execute("DELETE FROM trigger_seen WHERE trigger_id=? AND path=?", (row["id"], gone))
            fresh = 0
            for path, (mtime, size) in sorted(now_files.items(), key=lambda kv: kv[1][0]):
                if seen.get(path) == (mtime, size):
                    continue
                if time.time() - mtime < SETTLE_SECONDS:
                    continue                      # still being written: look again next time
                if fresh >= FOLDER_BATCH:
                    break
                fresh += 1
                self.db.execute("INSERT OR REPLACE INTO trigger_seen(trigger_id, path, mtime, size) VALUES(?,?,?,?)", (row["id"], path, mtime, size))
                name = path.rsplit("/", 1)[-1]
                try:
                    self._start(row, {"file": path, "filename": name, "folder": row["folder"]}, detail=path, force=True)
                    started += 1
                except TriggerError:
                    pass
        return started

    # ------------------------------------------------------------------------- running
    def _start(self, row: dict, ctx: dict, manual: bool = False, detail: str = "", force: bool = False) -> dict:
        tid = row["id"]
        with self._lock:
            if tid in self.running and not force:
                raise TriggerError("This trigger is already running. Try again when it has finished.", 429)
            gap = time.time() - self._last.get(tid, 0)
            if gap < MIN_GAP and not manual and not force:
                raise TriggerError("Too many requests: wait a moment between calls.", 429)
            self._last[tid] = time.time()
            self.running.add(tid)
        run_id = new_id()
        self.db.insert("trigger_runs", {"id": run_id, "trigger_id": tid, "started_at": now(), "status": "running", "detail": (detail or ctx.get("content_type", "") or ("test" if manual else ""))[:200]})
        self.db.execute("UPDATE triggers SET fired=fired+1, last_fired_at=? WHERE id=?", (now(), tid))
        threading.Thread(target=self._execute, args=(row, run_id, ctx), daemon=True, name=f"trigger-{tid[:4]}").start()
        return {"run_id": run_id, "status": "accepted"}

    def _execute(self, row: dict, run_id: str, ctx: dict) -> None:
        eng = self.engine
        status, result, thread_id, detail = "ok", "", "", None
        try:
            bot = eng.bots.get(row["bot_id"]) if row["bot_id"] else None
            if row.get("workflow_id"):
                prompt = row["prompt"] or ("{{payload}}" if row["kind"] == "webhook" else "A new file arrived: {{file}}")
                text, hits = render(prompt, {**ctx, "trigger": row["name"]})
                run = eng.workflows.run(row["workflow_id"], text, dry_run=bool(row["dry_run"]), tainted=bool(hits), wait=True)
                status = {"ok": "ok", "stopped": "stopped"}.get(run["status"], "error")
                result = run["result"] or run["error"]
                detail = f"workflow run {run['id']}"
            elif not bot or bot["archived"]:
                status, result = "skipped", "The Bot for this trigger no longer exists."
            elif bot["paused"]:
                status, result = "skipped", "The Bot is paused."
            elif eng.usage.over_limit() or eng.usage.bot_over_budget(bot):
                status, result = "skipped", "Usage limit reached."
            else:
                task_text, hits = render(row["prompt"], {**ctx, "trigger": row["name"]})
                th = eng.threads.create(bot["id"], f"{row['name']} · {datetime.now().strftime('%b %d %H:%M')}", kind="routine")
                thread_id = th["id"]
                head = (f"[Trigger: {row['name']}] A {'webhook request arrived' if row['kind'] == 'webhook' else 'file appeared in ' + row['folder']}. "
                        "Nobody is watching right now, so work end to end and keep the final report short and concrete.\n\n")
                if hits:
                    eng.threads.notice(thread_id, "The data that came with this trigger contains text that looks like instructions. I treat it as data only, and automatic approvals are off for this task.", "warn")
                res = eng.turns.run_sync(bot["id"], thread_id, head + task_text, trigger="routine", dry_run=bool(row["dry_run"]), tainted=bool(hits))
                result = res.final_text or res.error
                status = {"done": "ok", "stopped": "stopped"}.get(res.status, "error")
                detail = res.error[:200] if res.error else None
        except Exception as e:  # noqa: BLE001
            status, result = "error", f"{type(e).__name__}: {e}"
        finally:
            with self._lock:
                self.running.discard(row["id"])
        upd = {"ended_at": now(), "status": status, "result": result[:6000], "thread_id": thread_id}
        if detail:
            upd["detail"] = detail
        self.db.update("trigger_runs", run_id, upd)
        eng.events.publish("trigger_run", trigger_id=row["id"], run_id=run_id, status=status)
        bot = eng.bots.get(row["bot_id"]) if row["bot_id"] else None
        if (bot or row.get("workflow_id")) and status != "skipped":
            eng.notify("routine", bot, thread_id, f"Trigger {'finished' if status == 'ok' else status}: {row['name']}", result[:160], urgent=status != "ok")
