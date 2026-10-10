"""API tokens: named, scoped keys so scripts and other tools can use the service without your main access token.

Three scopes:
  read   look at things (GET requests only)
  chat   read, plus send messages to Bots
  full   read and change things (create Bots, run routines, edit files...) except the sensitive areas that stay with the main
         access token: tokens, backups, notification channels, settings, provider keys, approvals and plugin/MCP setup.
A token is shown once when it is created. Only its hash is stored, so a lost token cannot be recovered, only revoked."""
from __future__ import annotations

import hashlib
import secrets
import time

from .db import Database, new_id, now

SCOPES = ("read", "chat", "full")
PREFIX = "gbt_"
MAX_ACTIVE = 50


class TokenError(ValueError):
    pass


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ApiTokens:
    def __init__(self, db: Database):
        self.db = db
        self._touched: dict[str, float] = {}

    def create(self, name: str, scope: str = "read", days: int = 0) -> dict:
        name = (name or "").strip()[:60]
        if not name:
            raise TokenError("Give the token a name, so you can tell it apart later.")
        if scope not in SCOPES:
            raise TokenError("Scope must be read, chat or full.")
        if int(self.db.scalar("SELECT COUNT(*) FROM api_tokens WHERE revoked=0", (), 0)) >= MAX_ACTIVE:
            raise TokenError(f"There are already {MAX_ACTIVE} active tokens. Revoke some first.")
        days = max(0, min(3650, int(days or 0)))
        token = PREFIX + secrets.token_urlsafe(30)
        tid = new_id()
        self.db.insert("api_tokens", {"id": tid, "name": name, "prefix": token[:8], "hash": _hash(token), "scope": scope, "created_at": now(),
                                      "expires_at": (time.time() + days * 86400) if days else 0})
        return {**self._public(self.db.one("SELECT * FROM api_tokens WHERE id=?", (tid,))), "token": token}

    @staticmethod
    def _public(row: dict) -> dict:
        r = {k: v for k, v in row.items() if k != "hash"}
        r["revoked"] = bool(r.get("revoked"))
        r["expired"] = bool(r.get("expires_at")) and r["expires_at"] < time.time()
        return r

    def list(self) -> list[dict]:
        return [self._public(r) for r in self.db.query("SELECT * FROM api_tokens ORDER BY created_at DESC")]

    def revoke(self, tid: str) -> None:
        if not self.db.one("SELECT id FROM api_tokens WHERE id=?", (tid,)):
            raise TokenError("No such token.")
        self.db.update("api_tokens", tid, {"revoked": 1})

    def delete(self, tid: str) -> None:
        self.db.execute("DELETE FROM api_tokens WHERE id=?", (tid,))

    def verify(self, token: str) -> dict | None:
        """The token's row when it is valid (right hash, not revoked, not expired); None otherwise."""
        if not token or not token.startswith(PREFIX) or len(token) > 200:
            return None
        row = self.db.one("SELECT * FROM api_tokens WHERE hash=?", (_hash(token),))
        if not row or row["revoked"] or (row["expires_at"] and row["expires_at"] < time.time()):
            return None
        t = time.time()
        if t - self._touched.get(row["id"], 0) > 60:     # remember when it was last used, without a database write per request
            self._touched[row["id"]] = t
            self.db.update("api_tokens", row["id"], {"last_used_at": t})
        return self._public(row)


# which requests each scope may make (the main access token may make any)
ALWAYS_MAIN = ("/api/tokens", "/api/backup", "/api/channels", "/api/diagnostics/bundle")
WRITE_MAIN = ("/api/settings", "/api/approvals", "/api/rules", "/api/providers", "/api/mcp", "/api/plugins", "/api/computer/terminal", "/api/triggers", "/api/updates", "/api/library")


def allowed(scope: str, method: str, path: str) -> bool:
    """Whether a request is within an API token's scope."""
    if any(path == p or path.startswith(p + "/") for p in ALWAYS_MAIN):
        return False
    safe = method in ("GET", "HEAD", "OPTIONS")
    if safe:
        return True
    if any(path == p or path.startswith(p + "/") for p in WRITE_MAIN):
        return False
    if scope == "full":
        return True
    if scope == "chat":
        return method == "POST" and path.startswith("/api/threads/") and path.endswith("/messages")
    return False
