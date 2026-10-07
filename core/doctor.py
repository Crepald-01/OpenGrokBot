"""Diagnostics: a health check of the whole app, in plain language, and a support bundle you can attach to a bug report.

Every check says what it found and, when something is wrong, what to do about it. The support bundle holds the check results, the
versions, your settings with anything secret removed, and the tail of the service log with secrets scrubbed. It contains no chats,
memories, files or keys, and it is only saved to disk where you choose; nothing is sent anywhere."""
from __future__ import annotations

import glob
import io
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import time
import zipfile
from typing import TYPE_CHECKING

from . import VERSION, paths, secrets

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

GB = 1024 ** 3
MB = 1024 ** 2


def scrub(text: str, extra: list[str] | None = None) -> str:
    """secrets.redact plus the secret shapes this app adds: webhook addresses, API tokens, Telegram bot tokens, URLs with credentials."""
    out = secrets.redact(text, extra)
    out = re.sub(r"/hooks/[A-Za-z0-9_\-]+/[A-Za-z0-9_\-]+", "/hooks/[REDACTED]/[REDACTED]", out)
    out = re.sub(r"gbt_[A-Za-z0-9_\-]{10,}", "[REDACTED]", out)
    out = re.sub(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b", "[REDACTED]", out)
    out = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[REDACTED]@", out)
    out = re.sub(r"(?i)(token|secret|password|api[_-]?key)=([^&\s\"']+)", r"\1=[REDACTED]", out)
    return out


def _chromium_installed() -> bool:
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or (os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ms-playwright") if os.name == "nt"
                                                         else os.path.expanduser("~/.cache/ms-playwright"))
    return bool(glob.glob(os.path.join(base, "chromium-*")))


def _dir_bytes(p, cap: int = 50_000) -> tuple[int, int]:
    n = total = 0
    for dirpath, _d, files in os.walk(p):
        for f in files:
            n += 1
            if n > cap:
                return n, total
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return n, total


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _check(cid: str, title: str, status: str, detail: str, fix: str = "") -> dict:
    return {"id": cid, "title": title, "status": status, "detail": detail, "fix": fix}


class Doctor:
    def __init__(self, engine: "Engine"):
        self.eng = engine

    # ------------------------------------------------------------------------- the checks
    def run(self) -> dict:
        checks: list[dict] = []
        steps = (("service", "Background service", self._service), ("database", "Database", self._database), ("disk", "Disk space", self._disk),
                 ("workspace", "Workspace folder", self._workspace), ("secrets", "Key storage", self._secrets), ("provider-default", "Model provider", self._providers),
                 ("browser", "Browser for Bots", self._browser), ("scheduler", "Scheduler", self._scheduler), ("mobile", "Phone access", self._mobile),
                 ("updates", "Updates", self._updates), ("logs", "Log files", self._logs), ("turns", "Tasks", self._turns), ("approvals", "Approvals", self._approvals),
                 ("search", "Knowledge search", self._search), ("channels", "Notification channels", self._channels), ("history", "File history", self._history))
        for cid, title, fn in steps:
            try:
                out = fn()
            except Exception as e:  # noqa: BLE001  (a check that crashes is itself worth reporting, and must not stop the others)
                out = _check(cid, title, "warn", f"This check could not run: {type(e).__name__}: {str(e)[:120]}", "Save a support bundle and include it if you report this.")
            checks.extend(out if isinstance(out, list) else [out])
        summary = {s: sum(1 for c in checks if c["status"] == s) for s in ("ok", "warn", "fail")}
        return {"checks": checks, "summary": summary, "at": time.time(), "version": VERSION}

    def _service(self):
        up = time.time() - self.eng.started
        return _check("service", "Background service", "ok", f"Running version {VERSION}, up for {up / 3600:.1f} hours." if up > 3600 else f"Running version {VERSION}, up for {up / 60:.0f} minutes.")

    def _database(self):
        db = self.eng.db
        res = db.scalar("PRAGMA quick_check", (), "?")
        size = os.path.getsize(paths.db_path()) if paths.db_path().exists() else 0
        if res != "ok":
            return _check("database", "Database", "fail", f"The database reported a problem: {str(res)[:120]}.", "Back up now (Settings > App > Back up), then restore from an earlier backup if things misbehave.")
        bots = int(db.scalar("SELECT COUNT(*) FROM bots", (), 0))
        msgs = int(db.scalar("SELECT COUNT(*) FROM messages", (), 0))
        return _check("database", "Database", "ok", f"Healthy. {human(size)} holding {bots} Bot(s) and {msgs:,} messages.")

    def _disk(self):
        free = shutil.disk_usage(paths.data_dir()).free
        if free < 0.5 * GB:
            return _check("disk", "Disk space", "fail", f"Only {human(free)} free where your data lives.", "Free some space: the app needs room for its database, browser profile and logs.")
        if free < 2 * GB:
            return _check("disk", "Disk space", "warn", f"{human(free)} free where your data lives.", "Getting low. Clear old downloads or the workspace.")
        return _check("disk", "Disk space", "ok", f"{human(free)} free.")

    def _workspace(self):
        ws = self.eng.computer.workspace
        try:
            fd, name = tempfile.mkstemp(prefix=".gb-check-", dir=ws)
            os.close(fd)
            os.unlink(name)
        except OSError as e:
            return _check("workspace", "Workspace folder", "fail", f"Bots cannot write to {ws}: {e}.", "Check the folder's permissions or pick another location.")
        n, total = _dir_bytes(ws)
        if total > 2 * GB:
            return _check("workspace", "Workspace folder", "warn", f"{n:,} files, {human(total)}.", "The workspace is large. Delete what you no longer need on the Files page.")
        return _check("workspace", "Workspace folder", "ok", f"Writable. {n:,} files, {human(total)}.")

    def _secrets(self):
        if secrets.backend_available():
            return _check("secrets", "Key storage", "ok", "Your keys are kept in the Windows Credential Manager.")
        return _check("secrets", "Key storage", "fail", "No credential store is available, so keys cannot be saved.", "On Windows this is the Credential Manager. Otherwise set keys as environment variables (see the README).")

    def _providers(self):
        eng = self.eng
        default = eng.settings.profile(None)
        out = []
        if default.get("needs_key", True) and not secrets.has_secret(f"provider:{default['id']}"):
            out.append(_check("provider-default", "Model provider", "warn", f"No API key for the default provider ({default.get('label', default['id'])}).", "Add it in Settings > Models. Bots on that provider cannot work without it."))
        elif not (default.get("model") or ""):
            out.append(_check("provider-default", "Model provider", "warn", f"The default provider ({default.get('label', default['id'])}) has no model chosen.", "Pick a model in Settings > Models."))
        else:
            out.append(_check("provider-default", "Model provider", "ok", f"Default: {default.get('label', default['id'])} · {default.get('model')}."))
        profs = eng.settings.profiles()
        bad = []
        for b in eng.bots.list():
            p = profs.get(b["profile"] or default["id"]) or default
            if p.get("needs_key", True) and not secrets.has_secret(f"provider:{p.get('id', b['profile'])}") and not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
                bad.append(b["name"])
        if bad:
            out.append(_check("provider-bots", "Bots and keys", "warn", "These Bots use a provider with no key: " + ", ".join(bad[:6]) + ".", "Add the key in Settings > Models, or give them another model."))
        fb = (eng.settings.get("fallback.profile", "") or "", eng.settings.get("fallback.model", "") or "")
        if any(fb):
            out.append(_check("fallback", "Backup model", "ok", f"Set: {fb[0] or 'default provider'} · {fb[1] or 'its default model'}."))
        return out

    def _browser(self):
        if _chromium_installed():
            return _check("browser", "Browser for Bots", "ok", "Chromium is installed.")
        return _check("browser", "Browser for Bots", "warn", "Chromium is not installed, so Bots cannot browse websites.", "Open the Computer page and use Install browser, or run: playwright install chromium")

    def _scheduler(self):
        sched = self.eng.routines.sched
        if not sched.running:
            return _check("scheduler", "Scheduler", "fail", "The scheduler is not running, so routines, workflows and triggers will not fire.", "Restart the app.")
        jobs = [j for j in sched.get_jobs() if not j.id.startswith("_")]
        nxt = min((j.next_run_time.timestamp() for j in jobs if j.next_run_time), default=0)
        when = time.strftime("%a %H:%M", time.localtime(nxt)) if nxt else "nothing scheduled"
        return _check("scheduler", "Scheduler", "ok", f"Running. {len(jobs)} scheduled item(s); next: {when}.")

    def _mobile(self):
        m = self.eng.settings.get("mobile", {}) or {}
        host = m.get("host", "127.0.0.1")
        if host in ("0.0.0.0", "::"):
            return _check("mobile", "Phone access", "warn", f"The service listens on every network address (port {m.get('port', 8765)}). Anyone on your network with the access token can use it.",
                          "Turn this off in Settings > Mobile unless you use the phone app, and use a VPN rather than exposing it to the internet.")
        return _check("mobile", "Phone access", "ok", "The service only listens on this PC.")

    def _updates(self):
        st = self.eng.updates.state()
        if not st["enabled"]:
            return _check("updates", "Updates", "ok", "Update checks are off.")
        if st["newer"]:
            return _check("updates", "Updates", "warn", f"Version {st['latest']} is available (you have {VERSION}).", "Download it from the project page.")
        if st["error"] and not st["latest"]:
            return _check("updates", "Updates", "ok", "Could not check for updates right now (offline?).")
        return _check("updates", "Updates", "ok", f"You have the latest version ({VERSION}).")

    def _logs(self):
        n, total = _dir_bytes(paths.logs_dir())
        if total > 200 * MB:
            return _check("logs", "Log files", "warn", f"{human(total)} of logs.", "Use Open logs in Settings > App and delete the old files.")
        return _check("logs", "Log files", "ok", f"{human(total)} in {n} file(s).")

    def _turns(self):
        marked = self.eng.db.query("SELECT bot_id FROM turns WHERE status IN ('running','queued','waiting_approval','retrying','waiting_screen') AND started_at<?", (time.time() - 6 * 3600,))
        active = self.eng.turns.active()
        stuck = [t for t in marked if not any(a["bot_id"] == t["bot_id"] for a in active)]   # marked running, but no live run behind it
        if stuck:
            return _check("turns", "Tasks", "warn", f"{len(stuck)} task(s) were left marked as running for over 6 hours, but nothing is running them.", "Restart the app: it marks interrupted tasks and offers to resume them.")
        return _check("turns", "Tasks", "ok", f"{len(active)} running right now." if active else "Nothing is stuck.")

    def _approvals(self):
        n = self.eng.db.scalar("SELECT COUNT(*) FROM approvals WHERE status='pending' AND created_at<?", (time.time() - 7 * 86400,), 0)
        if n:
            return _check("approvals", "Approvals", "warn", f"{n} approval(s) have been waiting more than a week.", "Open the Inbox: Bots are blocked until you answer.")
        return _check("approvals", "Approvals", "ok", f"{self.eng.approvals.pending_count()} waiting for you.")

    def _search(self):
        if self.eng.knowledge.fts:
            return _check("search", "Knowledge search", "ok", "Full-text search is available.")
        return _check("search", "Knowledge search", "warn", "This SQLite build has no full-text search, so the knowledge base uses slower plain matching.")

    def _channels(self):
        bad = [c for c in self.eng.channels.list() if c["enabled"] and c.get("last_status") == "error"]
        if bad:
            return _check("channels", "Notification channels", "warn", "These channels failed last time: " + ", ".join(f"{c['name']} ({c['last_error'][:60]})" for c in bad[:4]) + ".", "Open Settings > Notifications and use Test on them.")
        n = len(self.eng.channels.list())
        return _check("channels", "Notification channels", "ok", f"{n} channel(s), none failing." if n else "No channels set up.")

    def _history(self):
        st = self.eng.filehistory.stats()
        return _check("history", "File history", "ok", f"{st['versions']} saved version(s) of {st['files']} file(s), {human(st['bytes'])}.")

    # ------------------------------------------------------------------------- the support bundle
    def bundle(self) -> bytes:
        eng = self.eng
        report = self.run()
        settings = eng.settings.all()
        settings.pop("providers", None)
        chans = settings.pop("channels", []) or []
        settings["channels"] = [{"kind": c.get("kind"), "enabled": c.get("enabled"), "events": c.get("events"), "last_status": c.get("last_status")} for c in chans]
        info = {"app": "OpenGrokBot", "version": VERSION, "python": sys.version.split()[0], "platform": platform.platform(), "frozen": bool(getattr(sys, "frozen", False)),
                "created": time.strftime("%Y-%m-%d %H:%M:%S"), "bots": len(eng.bots.list()), "knowledge": eng.knowledge.stats(), "history": eng.filehistory.stats()}
        log = ""
        lf = paths.logs_dir() / "service.log"
        if lf.exists():
            lines = lf.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]
            log = scrub("\n".join(lines))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("README.txt", "OpenGrokBot support bundle. It has no chats, memories, files or keys. Secrets are scrubbed from the log and settings, "
                                     "but read service.log before sharing it.\n")
            z.writestr("info.json", json.dumps(info, indent=2))
            z.writestr("checks.json", json.dumps(report, indent=2))
            z.writestr("settings.json", scrub(json.dumps(settings, indent=2, default=str)))
            z.writestr("service.log", log)
        return buf.getvalue()
