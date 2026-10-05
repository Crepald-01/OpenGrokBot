"""Application settings (DB-backed) and the optional admin policy file."""
from __future__ import annotations

import copy
import json
import threading
from typing import Any

from . import paths
from .db import Database, jdump, jload

PROVIDER_PRESETS: dict[str, dict] = {
    "anthropic": {"label": "Anthropic", "kind": "anthropic", "base_url": "", "model": "claude-sonnet-5-5",
                  "vision": True, "needs_key": True},
    "openai": {"label": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4.1",
               "vision": True, "needs_key": True},
    "openrouter": {"label": "OpenRouter", "kind": "openai", "base_url": "https://openrouter.ai/api/v1",
                   "model": "anthropic/claude-sonnet-4.5", "vision": True, "needs_key": True},
    "groq": {"label": "Groq", "kind": "openai", "base_url": "https://api.groq.com/openai/v1",
             "model": "llama-3.3-70b-versatile", "vision": False, "needs_key": True},
    "ollama": {"label": "Ollama (local)", "kind": "openai", "base_url": "http://localhost:11434/v1",
               "model": "llama3.1", "vision": False, "needs_key": False},
    "lmstudio": {"label": "LM Studio (local)", "kind": "openai", "base_url": "http://localhost:1234/v1",
                 "model": "local-model", "vision": False, "needs_key": False},
}

DEFAULTS: dict[str, Any] = {
    "theme": "dark",
    "providers": PROVIDER_PRESETS,
    "default_profile": "anthropic",
    "reviewer": {"profile": "", "model": ""},
    "approval": {"default_mode": "ask", "timeout_min": 1440, "routine_timeout_min": 120},
    "network": {"mode": "open", "allow": [], "deny": [], "block_private": True},
    "notifications": {"toast": True, "sound": True, "on_finish": True, "on_approval": True,
                      "min_turn_seconds": 20, "ntfy_url": "", "ntfy_topic": "",
                      "quiet_enabled": False, "quiet_start": "22:00", "quiet_end": "07:00", "dnd_until": 0},
    "usage": {"weekly_token_limit": 0, "reset_weekday": 0},
    "computer": {"headless": True, "viewport_w": 1280, "viewport_h": 800},
    "mobile": {"enabled": True, "host": "127.0.0.1", "port": 8765},
    "proactive": {"interval_min": 180},
    "digest": {"enabled": False, "time": "18:00", "last_sent": ""},
    "pricing": {"currency": "$", "models": {}},
    "updates": {"check": True},
    "defaults": {"step_limit": 40},
    "rest_connectors": [],
    "catalog_urls": [],
}


class Settings:
    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.RLock()
        self._cache: dict[str, Any] = {}
        for r in db.query("SELECT key, value FROM settings"):
            self._cache[r["key"]] = jload(r["value"])

    def get(self, key: str, default: Any = None) -> Any:
        """Dotted lookup, e.g. get('network.mode'). Falls back to built-in defaults."""
        head, _, rest = key.partition(".")
        with self._lock:
            if head in self._cache:
                val = copy.deepcopy(self._cache[head])
            elif head in DEFAULTS:
                val = copy.deepcopy(DEFAULTS[head])
            else:
                return default
        if isinstance(val, dict) and head in DEFAULTS and isinstance(DEFAULTS[head], dict) and head != "providers":
            merged = copy.deepcopy(DEFAULTS[head])
            merged.update(val)
            val = merged
        if not rest:
            return val
        for part in rest.split("."):
            if isinstance(val, dict) and part in val:
                val = val[part]
            else:
                return default
        return val

    def set(self, key: str, value: Any) -> None:
        head, _, rest = key.partition(".")
        with self._lock:
            if rest:
                cur = self.get(head, {})
                if not isinstance(cur, dict):
                    cur = {}
                node = cur
                parts = rest.split(".")
                for p in parts[:-1]:
                    node = node.setdefault(p, {})
                node[parts[-1]] = value
                value, key = cur, head
            self._cache[key] = value
            self.db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, jdump(value)))

    def update(self, values: dict[str, Any]) -> None:
        for k, v in values.items():
            self.set(k, v)

    def all(self) -> dict[str, Any]:
        keys = set(DEFAULTS) | set(self._cache)
        return {k: self.get(k) for k in sorted(keys)}

    # -- provider profiles -------------------------------------------------
    def profiles(self) -> dict[str, dict]:
        profs = self.get("providers", {}) or {}
        out = {}
        for pid, p in {**PROVIDER_PRESETS, **profs}.items():
            base = copy.deepcopy(PROVIDER_PRESETS.get(pid, {}))
            base.update(p or {})
            out[pid] = base
        return out

    def profile(self, pid: str | None) -> dict:
        profs = self.profiles()
        pid = pid or self.get("default_profile", "anthropic")
        p = profs.get(pid) or profs.get(self.get("default_profile", "anthropic")) or next(iter(profs.values()))
        p = dict(p)
        p["id"] = pid if pid in profs else self.get("default_profile", "anthropic")
        return p


class Admin:
    """Deployment presets read from admin.json. Absent file means no restrictions."""

    def __init__(self) -> None:
        self.path = None
        self.data: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        self.path, self.data = None, {}
        for p in paths.admin_policy_paths():
            try:
                if p.is_file():
                    self.data = json.loads(p.read_text(encoding="utf-8"))
                    self.path = str(p)
                    return
            except (OSError, ValueError):
                continue

    @property
    def active(self) -> bool:
        return bool(self.data)

    def network(self) -> dict | None:
        return self.data.get("network_policy")

    def approval(self) -> dict:
        return self.data.get("approval_defaults") or {}

    def always_ask(self) -> set[str]:
        return set(self.approval().get("always_ask_categories") or [])

    def allowed_plugins(self) -> list[str] | None:
        v = self.data.get("allowed_plugins")
        return list(v) if isinstance(v, list) else None

    def allowed_mcp(self) -> list[str] | None:
        v = self.data.get("allowed_mcp_servers")
        return list(v) if isinstance(v, list) else None

    def allowed_providers(self) -> list[str] | None:
        v = self.data.get("allowed_providers")
        return list(v) if isinstance(v, list) else None

    def plugin_install_allowed(self) -> bool:
        return bool(self.data.get("allow_plugin_install", True))

    def remote_mode_allowed(self) -> bool:
        return bool(self.data.get("allow_remote_mode", True))

    def max_steps(self) -> int | None:
        v = self.data.get("max_steps")
        return int(v) if v else None

    def weekly_token_limit(self) -> int | None:
        v = self.data.get("weekly_token_limit")
        return int(v) if v else None

    def summary(self) -> dict:
        return {"active": self.active, "path": self.path, "policy": self.data}


def plugin_allowed(admin: Admin, plugin_id: str) -> bool:
    allowed = admin.allowed_plugins()
    return allowed is None or plugin_id in allowed
