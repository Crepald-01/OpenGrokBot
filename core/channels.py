"""Notification channels: send the same alerts that pop up on your PC to Slack, Discord, Telegram, email or any webhook.

Each channel chooses which kinds of alert it receives (approvals, questions, finished tasks, errors, routines, the daily digest...).
Quiet hours and Do Not Disturb apply, exactly as for phone pushes. A channel's secret (a webhook address, a bot token or a mail password)
is kept in the Windows Credential Manager, never in settings or the database, and is never shown again after it is saved."""
from __future__ import annotations

import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlparse

from . import secrets
from .db import new_id
from .settings import Settings

KINDS = {
    "slack": "Slack (incoming webhook)",
    "discord": "Discord (webhook)",
    "telegram": "Telegram (bot)",
    "webhook": "Generic webhook (JSON)",
    "email": "Email (SMTP)",
}
EVENTS = ("approval", "question", "takeover", "login", "finished", "error", "routine", "digest", "bot_message")
DEFAULT_EVENTS = ["approval", "question", "takeover", "error"]
MAX_CHANNELS = 20
TELEGRAM_API = "https://api.telegram.org"     # overridden in tests


class ChannelError(ValueError):
    pass


def _secret_name(cid: str) -> str:
    return f"channel:{cid}"


def _check_url(url: str) -> str:
    url = (url or "").strip()
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ChannelError("That is not a web address (it should start with https://).")
    return url


class Channels:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()

    # ------------------------------------------------------------------------- storage
    def _load(self) -> list[dict]:
        return list(self.settings.get("channels", []) or [])

    def _save(self, rows: list[dict]) -> None:
        self.settings.set("channels", rows)

    def _public(self, c: dict) -> dict:
        out = {k: v for k, v in c.items()}
        out["secret_set"] = secrets.has_secret(_secret_name(c["id"]))
        out["kind_label"] = KINDS.get(c["kind"], c["kind"])
        return out

    def list(self) -> list[dict]:
        return [self._public(c) for c in self._load()]

    def get(self, cid: str) -> dict | None:
        return next((c for c in self._load() if c["id"] == cid), None)

    # ------------------------------------------------------------------------- CRUD
    def _validate(self, kind: str, config: dict, secret: str | None, require_secret: bool) -> dict:
        if kind not in KINDS:
            raise ChannelError("Choose a channel type.")
        config = {k: (str(v).strip() if v is not None else "") for k, v in (config or {}).items()}
        if kind in ("slack", "discord", "webhook"):
            if secret:
                _check_url(secret)
            elif require_secret:
                raise ChannelError("Paste the webhook address.")
            return {}
        if kind == "telegram":
            if not config.get("chat_id"):
                raise ChannelError("Enter the Telegram chat id to send to.")
            if require_secret and not secret:
                raise ChannelError("Paste the bot token from @BotFather.")
            return {"chat_id": config["chat_id"]}
        host = config.get("host", "")
        if not host or not config.get("to") or not config.get("from"):
            raise ChannelError("Email needs a server, a from address and a to address.")
        try:
            port = int(config.get("port") or 587)
        except ValueError as e:
            raise ChannelError("The mail server port must be a number.") from e
        sec = config.get("security", "starttls")
        if sec not in ("starttls", "ssl", "none"):
            sec = "starttls"
        return {"host": host, "port": port, "user": config.get("user", ""), "from": config["from"], "to": config["to"], "security": sec}

    def create(self, kind: str, name: str, config: dict | None = None, secret: str | None = None, events: list[str] | None = None, enabled: bool = True) -> dict:
        rows = self._load()
        if len(rows) >= MAX_CHANNELS:
            raise ChannelError(f"There are already {MAX_CHANNELS} channels.")
        clean = self._validate(kind, config or {}, secret, True if kind != "email" else False)
        cid = new_id()
        row = {"id": cid, "kind": kind, "name": (name or "").strip()[:40] or KINDS[kind].split(" (")[0], "enabled": bool(enabled), "config": clean,
               "events": [e for e in (events or DEFAULT_EVENTS) if e in EVENTS], "last_status": "", "last_error": "", "last_at": 0}
        if secret:
            secrets.set_secret(_secret_name(cid), secret.strip())
        with self._lock:
            self._save(rows + [row])
        return self._public(row)

    def update(self, cid: str, name: str | None = None, config: dict | None = None, secret: str | None = None, events: list[str] | None = None, enabled: bool | None = None) -> dict:
        with self._lock:
            rows = self._load()
            row = next((c for c in rows if c["id"] == cid), None)
            if not row:
                raise ChannelError("No such channel.")
            if config is not None or secret:
                merged = {**row["config"], **{k: v for k, v in (config or {}).items() if v is not None}}
                row["config"] = self._validate(row["kind"], merged, secret, False)
            if secret:
                secrets.set_secret(_secret_name(cid), secret.strip())
            if name is not None:
                row["name"] = name.strip()[:40] or row["name"]
            if events is not None:
                row["events"] = [e for e in events if e in EVENTS]
            if enabled is not None:
                row["enabled"] = bool(enabled)
            self._save(rows)
        return self._public(row)

    def delete(self, cid: str) -> None:
        with self._lock:
            rows = self._load()
            self._save([c for c in rows if c["id"] != cid])
        try:
            secrets.delete_secret(_secret_name(cid))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------------- sending
    def _send(self, c: dict, title: str, body: str, kind: str = "test", bot_name: str = "", urgent: bool = False) -> None:
        """Deliver one message. Raises on failure (callers turn that into a status). The secret never appears in an error text."""
        secret = secrets.get_secret(_secret_name(c["id"])) or ""
        cfg = c["config"]
        title, body = (title or "OpenGrokBot")[:200], (body or "")[:1500]
        try:
            if c["kind"] == "email":
                self._send_email(cfg, secret, title, body, bot_name)
                return
            import httpx
            if c["kind"] == "slack":
                r = httpx.post(secret, json={"text": f"*{title}*\n{body}"}, timeout=10)
            elif c["kind"] == "discord":
                r = httpx.post(secret, json={"content": f"**{title}**\n{body}"[:1900]}, timeout=10)
            elif c["kind"] == "telegram":
                r = httpx.post(f"{TELEGRAM_API}/bot{secret}/sendMessage", json={"chat_id": cfg["chat_id"], "text": f"{title}\n{body}"[:4000]}, timeout=10)
            else:
                r = httpx.post(secret, json={"app": "OpenGrokBot", "kind": kind, "title": title, "body": body, "bot": bot_name, "urgent": urgent, "time": time.time()}, timeout=10)
            if r.status_code >= 300:
                raise ChannelError(f"The service answered {r.status_code}.")
        except ChannelError:
            raise
        except Exception as e:  # noqa: BLE001
            msg = str(e).replace(secret, "…") if secret else str(e)
            raise ChannelError(f"{type(e).__name__}: {secrets.redact(msg)[:160]}") from None

    @staticmethod
    def _send_email(cfg: dict, password: str, title: str, body: str, bot_name: str) -> None:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, cfg["from"], cfg["to"]
        msg.set_content(body + (f"\n\n(from {bot_name} in OpenGrokBot)" if bot_name else "\n\n(sent by OpenGrokBot)"))
        if cfg["security"] == "ssl":
            smtp = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15, context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(cfg["host"], cfg["port"], timeout=15)
        try:
            if cfg["security"] == "starttls":
                smtp.starttls(context=ssl.create_default_context())
            if cfg.get("user"):
                smtp.login(cfg["user"], password)
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                pass

    def _record(self, cid: str, error: str) -> None:
        with self._lock:
            rows = self._load()
            for c in rows:
                if c["id"] == cid:
                    c["last_status"], c["last_error"], c["last_at"] = ("error" if error else "ok"), error, time.time()
            self._save(rows)

    def test(self, cid: str) -> dict:
        c = self.get(cid)
        if not c:
            raise ChannelError("No such channel.")
        try:
            self._send(c, "OpenGrokBot test", "If you can read this, the channel works.", "test")
        except ChannelError as e:
            self._record(cid, str(e))
            return {"ok": False, "error": str(e)}
        self._record(cid, "")
        return {"ok": True}

    def dispatch(self, kind: str, bot_name: str, title: str, body: str, urgent: bool = False) -> int:
        """Send an alert to every enabled channel that wants this kind. Runs in the background; returns how many channels were chosen."""
        targets = [c for c in self._load() if c["enabled"] and kind in c.get("events", [])]
        for c in targets:
            threading.Thread(target=self._deliver, args=(c, title, body, kind, bot_name, urgent), daemon=True, name=f"channel-{c['kind']}").start()
        return len(targets)

    def _deliver(self, c: dict, title: str, body: str, kind: str, bot_name: str, urgent: bool) -> None:
        try:
            self._send(c, title, body, kind, bot_name, urgent)
            self._record(c["id"], "")
        except ChannelError as e:
            self._record(c["id"], str(e))
        except Exception as e:  # noqa: BLE001
            self._record(c["id"], f"{type(e).__name__}")
