"""The account's shared computer: browser, filesystem workspace and terminal.

It belongs to the account, not to a Bot. Every Bot sees the same files, browser sessions and logins.
"""
from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from . import paths
from .browser import BrowserHost
from .db import Database, jdump, jload, new_id, now
from .events import EventBus
from .netpolicy import NetworkPolicy, host_of
from .settings import Settings
from .tooling import Risk

IS_WIN = os.name == "nt"

SAFE_PROGRAMS = {
    "dir", "type", "echo", "cd", "where", "findstr", "find", "sort", "more", "tree", "hostname", "date", "ver", "cls", "set",
    "python", "python3", "py", "node", "pytest", "ls", "cat", "pwd", "head", "tail", "grep", "wc", "mkdir", "md", "copy", "cp",
    "move", "mv", "ren", "rename", "touch", "tar", "git", "npm", "get-childitem", "get-content", "select-string", "write-output",
    "new-item", "set-content", "add-content", "copy-item", "move-item", "get-location", "test-path", "sleep", "timeout", "true",
}
DELETE_VERBS = {"del", "erase", "rd", "rmdir", "rm", "remove-item", "ri", "unlink", "shred"}
DANGEROUS = {"format", "diskpart", "reg", "regedit", "sc", "schtasks", "taskkill", "shutdown", "netsh", "net", "icacls", "takeown",
             "cipher", "bcdedit", "wmic", "vssadmin", "attrib", "mklink", "runas", "msiexec", "start-process", "invoke-expression",
             "iex", "sudo", "chmod", "chown", "kill", "pkill", "dd", "mkfs"}
INSTALLERS = {"winget", "choco", "scoop", "apt", "apt-get", "brew", "yum", "dnf", "pacman"}
FETCHERS = {"curl", "wget", "iwr", "invoke-webrequest", "invoke-restmethod", "irm", "certutil", "bitsadmin", "scp", "ftp"}
SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)


class ComputerError(RuntimeError):
    pass


class Computer:
    def __init__(self, db: Database, settings: Settings, net: NetworkPolicy, events: EventBus):
        self.db, self.settings, self.net, self.events = db, settings, net, events
        self.browser = BrowserHost(settings, net, events)
        self.browser.on_event = lambda t, d: self.events.publish(t, **d)
        self.workspace = paths.workspace_dir()
        self.terminal_log: deque[dict] = deque(maxlen=200)
        self._takeovers: dict[str, threading.Event] = {}
        self._tk_lock = threading.Lock()

    # ------------------------------------------------------------------ files
    def resolve(self, p: str | os.PathLike) -> tuple[Path, bool]:
        """Resolve a user/bot path against the workspace. Returns (path, inside_workspace)."""
        raw = Path(os.path.expandvars(os.path.expanduser(str(p or "."))))
        full = raw if raw.is_absolute() else self.workspace / raw
        full = full.resolve()
        try:
            inside = full.is_relative_to(self.workspace.resolve())
        except ValueError:
            inside = False
        return full, inside

    def rel(self, p: Path) -> str:
        try:
            return str(p.resolve().relative_to(self.workspace.resolve())).replace("\\", "/") or "."
        except ValueError:
            return str(p)

    def list_dir(self, path: str = ".", limit: int = 300) -> list[dict]:
        d, _ = self.resolve(path)
        if not d.is_dir():
            raise ComputerError(f"{path} is not a folder.")
        out = []
        for e in sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:limit]:
            try:
                st = e.stat()
                out.append({"name": e.name, "dir": e.is_dir(), "size": 0 if e.is_dir() else st.st_size, "mtime": st.st_mtime})
            except OSError:
                continue
        return out

    def read_file(self, path: str, max_bytes: int = 200_000) -> tuple[str, bool]:
        f, _ = self.resolve(path)
        if not f.is_file():
            raise ComputerError(f"{path} is not a file.")
        data = f.read_bytes()
        trunc = len(data) > max_bytes
        data = data[:max_bytes]
        if b"\x00" in data[:4096]:
            raise ComputerError(f"{path} looks like a binary file ({f.stat().st_size} bytes).")
        return data.decode("utf-8", errors="replace"), trunc

    def write_file(self, path: str, content: str, append: bool = False) -> Path:
        f, _ = self.resolve(path)
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a" if append else "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        return f

    def delete(self, path: str) -> None:
        f, inside = self.resolve(path)
        if f == self.workspace.resolve():
            raise ComputerError("Refusing to delete the workspace root.")
        if f.is_dir():
            shutil.rmtree(f)
        elif f.exists():
            f.unlink()
        else:
            raise ComputerError(f"{path} does not exist.")

    def move(self, src: str, dst: str) -> Path:
        a, _ = self.resolve(src)
        b, _ = self.resolve(dst)
        if not a.exists():
            raise ComputerError(f"{src} does not exist.")
        b.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(a), str(b))
        return b

    # --------------------------------------------------------------- terminal
    def command_risk(self, command: str, cwd: str | None = None) -> Risk | None:
        """Heuristic gate. Commands that stay inside the workspace and use common tools run freely."""
        policy = self.settings.get("computer.command_policy", "workspace")
        if policy == "ask_all":
            return Risk("command", f"Run: {command[:300]}", {"command": command, "pattern": "*"})
        d, inside = self.resolve(cwd or ".")
        if not inside:
            return Risk("outside_workspace", f"Run a command in {d}: {command[:300]}", {"command": command, "cwd": str(d), "pattern": str(d)}, never_auto=True)
        segments = [s.strip() for s in re.split(r"&&|\|\||[;|&\n]", command) if s.strip()]
        for seg in segments:
            try:
                toks = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', seg)
            except re.error:
                toks = seg.split()
            if not toks:
                continue
            prog = toks[0].strip("\"'").replace("\\", "/").split("/")[-1].lower()
            prog = re.sub(r"\.(exe|cmd|bat|ps1)$", "", prog)
            args = " ".join(toks[1:]).lower()
            if prog == "git" and re.match(r"(push|remote|credential|config)\b", args):
                return Risk("send", f"Publish or reconfigure git: {seg[:300]}", {"command": command, "pattern": "git " + args.split()[0]})
            if prog in DELETE_VERBS or re.search(r"\bgit\s+(clean|reset\s+--hard)\b", seg, re.I):
                return Risk("delete", f"Delete via command: {seg[:300]}", {"command": command, "pattern": "*"})
            if prog in DANGEROUS:
                return Risk("command", f"System-level command: {seg[:300]}", {"command": command, "pattern": prog}, never_auto=True)
            if prog in INSTALLERS or (prog in ("pip", "pip3") and "install" in args) or (prog == "npm" and re.search(r"\b(i|install|add)\b", args)) \
                    or (prog in ("python", "py", "python3") and "-m pip" in args and "install" in args):
                return Risk("install", f"Install software: {seg[:300]}", {"command": command, "pattern": prog})
            if prog in FETCHERS:
                return Risk("download", f"Network download/upload via command: {seg[:300]}", {"command": command, "pattern": prog})
            if prog in ("python", "py", "python3", "node", "powershell", "pwsh", "cmd") and re.search(r"(^|\s)(-c|-e|-command|-encodedcommand|-enc|/c)\b", args):
                return Risk("command", f"Run inline code: {seg[:300]}", {"command": command, "pattern": prog})
            if prog not in SAFE_PROGRAMS:
                return Risk("command", f"Run program '{prog}': {seg[:300]}", {"command": command, "pattern": prog})
            for tok in toks[1:]:
                t = tok.strip("\"'")
                looks_abs = bool(re.match(r"^[A-Za-z]:[\\/]", t)) or t.startswith(("\\\\", "~", "..", "$env:")) \
                    or (t.startswith("/") and not IS_WIN) or bool(re.search(r"%\w+%", t))
                if looks_abs:
                    full, ins = self.resolve(t) if not t.startswith("$env:") and "%" not in t else (Path(t), False)
                    if not ins:
                        return Risk("outside_workspace", f"Touches a path outside the workspace: {t}", {"command": command, "path": t, "pattern": t}, never_auto=True)
            m = re.search(r">{1,2}\s*([^\s|&<>]+)", seg)
            if m:
                full, ins = self.resolve(m.group(1).strip("\"'"))
                if not ins:
                    return Risk("outside_workspace", f"Writes outside the workspace: {m.group(1)}", {"command": command, "pattern": m.group(1)}, never_auto=True)
                if full.exists():
                    return Risk("overwrite", f"Redirect overwrites {self.rel(full)}", {"command": command, "pattern": "*"})
        return None

    def _clean_env(self) -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}

    def run_command(self, command: str, cwd: str | None = None, timeout: int = 60, shell: str = "default",
                    stop: threading.Event | None = None, who: str = "") -> dict:
        d, _ = self.resolve(cwd or ".")
        d.mkdir(parents=True, exist_ok=True)
        timeout = max(1, min(int(timeout or 60), 900))
        if IS_WIN and shell == "powershell":
            argv: Any = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
            use_shell = False
        else:
            argv, use_shell = command, True
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if IS_WIN else 0
        t0 = time.time()
        try:
            proc = subprocess.Popen(argv, shell=use_shell, cwd=str(d), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=self._clean_env(), creationflags=flags,
                                    text=True, encoding="utf-8", errors="replace")
        except OSError as e:
            return {"exit": -1, "output": f"Could not start command: {e}", "timed_out": False, "stopped": False, "seconds": 0}
        chunks: list[str] = []

        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                chunks.append(line)

        th = threading.Thread(target=pump, daemon=True)
        th.start()
        timed_out = stopped = False
        while proc.poll() is None:
            if stop is not None and stop.is_set():
                stopped = True
            if time.time() - t0 > timeout:
                timed_out = True
            if stopped or timed_out:
                self._kill(proc)
                break
            time.sleep(0.1)
        th.join(timeout=2)
        out = "".join(chunks)
        if len(out) > 20000:
            out = out[:10000] + f"\n[... {len(out) - 20000} chars omitted ...]\n" + out[-10000:]
        res = {"exit": proc.returncode if proc.returncode is not None else -1, "output": out, "timed_out": timed_out,
               "stopped": stopped, "seconds": round(time.time() - t0, 1)}
        self.terminal_log.append({"ts": now(), "cwd": self.rel(d), "command": command, "who": who, **res})
        return res

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            if IS_WIN:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=10,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # -------------------------------------------------------------------- web
    def web_fetch(self, bot_id: str | None, url: str, max_chars: int = 20000) -> dict:
        import httpx
        if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
            url = "https://" + url
        headers = {"User-Agent": "Mozilla/5.0 (compatible; OpenGrokBot/1.0)", "Accept": "text/html,text/plain,application/json;q=0.9,*/*;q=0.5"}
        cur = url
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=False, headers=headers) as client:
            for _ in range(6):
                ok, why = self.net.check(bot_id, cur, resolve=True)
                if not ok:
                    raise ComputerError(f"Blocked by network policy: {why}")
                try:
                    r = client.get(cur)
                except httpx.HTTPError as e:
                    raise ComputerError(f"Request failed: {e}") from e
                if r.is_redirect and r.headers.get("location"):
                    cur = urljoin(cur, r.headers["location"])
                    continue
                break
            else:
                raise ComputerError("Too many redirects.")
        ctype = r.headers.get("content-type", "")
        body = r.content[:2_000_000]
        text = body.decode(r.encoding or "utf-8", errors="replace")
        title = ""
        if "html" in ctype or text.lstrip().startswith("<"):
            m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
            title = html.unescape(m.group(1)).strip() if m else ""
            text = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", text)
            text = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6])>", "\n", text)
            text = html.unescape(re.sub(r"<[^>]+>", " ", text))
            text = re.sub(r"[ \t\r\f\v]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        return {"status": r.status_code, "url": cur, "title": title, "content_type": ctype, "text": text[:max_chars],
                "truncated": len(text) > max_chars}

    # ----------------------------------------------------------- takeover etc.
    def takeover_begin(self, bot_id: str, reason: str) -> threading.Event:
        scr = self.browser.screen(bot_id)
        ev = threading.Event()
        with self._tk_lock:
            self._takeovers[bot_id] = ev
        scr.takeover, scr.takeover_reason = True, reason
        self.events.publish("takeover", bot_id=bot_id, active=True, reason=reason)
        return ev

    def takeover_end(self, bot_id: str) -> None:
        scr = self.browser.screen(bot_id)
        scr.takeover, scr.takeover_reason = False, ""
        with self._tk_lock:
            ev = self._takeovers.pop(bot_id, None)
        if ev:
            ev.set()
        self.events.publish("takeover", bot_id=bot_id, active=False, reason="")

    def takeover_active(self, bot_id: str) -> bool:
        s = self.browser.screens.get(bot_id)
        return bool(s and s.takeover)

    def known_login(self, host: str) -> bool:
        return bool(self.db.one("SELECT domain FROM known_logins WHERE domain=?", (host,)))

    def remember_login(self, host: str) -> None:
        if host:
            self.db.execute("INSERT OR IGNORE INTO known_logins(domain, added_at) VALUES(?,?)", (host, now()))

    # --------------------------------------------------------------- recording
    def rec_start(self, bot_id: str, name: str) -> dict:
        scr = self.browser.screen(bot_id)
        rid = new_id()
        scr.recording = {"id": rid, "name": name or "Untitled demo", "steps": [], "paused": False, "started": time.time()}
        self.db.insert("recordings", {"id": rid, "bot_id": bot_id, "name": scr.recording["name"], "steps": "[]",
                                      "status": "recording", "created_at": now()})
        try:
            url = self.browser.call(self._current_url, bot_id, timeout=20)
            if url and url != "about:blank":
                scr.recording["steps"].append({"action": "navigate", "url": url, "ts": time.time(), "note": "start page"})
        except Exception:
            pass
        self.events.publish("recording", bot_id=bot_id, active=True, recording_id=rid)
        return {"id": rid, "name": scr.recording["name"]}

    async def _current_url(self, bot_id: str) -> str:
        page = await self.browser.screen(bot_id).ensure_page()
        return page.url

    def rec_note(self, bot_id: str, text: str) -> None:
        scr = self.browser.screen(bot_id)
        if scr.recording is not None:
            scr._record({"action": "note", "text": text[:1000]})

    def rec_stop(self, bot_id: str) -> dict | None:
        scr = self.browser.screen(bot_id)
        rec = scr.recording
        if rec is None:
            return None
        scr.recording = None
        self.db.update("recordings", rec["id"], {"steps": jdump(rec["steps"]), "status": "recorded"})
        self.events.publish("recording", bot_id=bot_id, active=False, recording_id=rec["id"])
        return self.recording(rec["id"])

    def recording(self, rid: str) -> dict | None:
        r = self.db.one("SELECT * FROM recordings WHERE id=?", (rid,))
        if r:
            r["steps"] = jload(r["steps"], [])
        return r

    def recordings(self, bot_id: str | None = None) -> list[dict]:
        rows = self.db.query("SELECT * FROM recordings" + (" WHERE bot_id=?" if bot_id else "") + " ORDER BY created_at DESC",
                             (bot_id,) if bot_id else ())
        for r in rows:
            r["steps"] = jload(r["steps"], [])
        return rows

    # ----------------------------------------------------------------- status
    def status(self) -> dict:
        return {"workspace": str(self.workspace), "browser": self.browser.status(), "browser_error": self.browser.last_error,
                "headless": bool(self.settings.get("computer.headless", True))}

    def shutdown(self) -> None:
        self.browser.shutdown()
