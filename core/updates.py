"""Tells you when a newer release is out. One plain GET to the public GitHub releases API at most once a day, nothing
about you is sent, and it can be switched off in Settings > App."""
from __future__ import annotations

import re
import time
from typing import Any, Callable

from . import VERSION

REPO = "Crepald-01/OpenGrokBot"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
INTERVAL = 24 * 3600


def parse(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", (v or "").split("-")[0])[:4]) or (0,)


def is_newer(latest: str, current: str) -> bool:
    a, b = parse(latest), parse(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))   # "1.6" and "1.6.0" are the same version


def _fetch() -> dict:
    import httpx
    r = httpx.get(API, headers={"Accept": "application/vnd.github+json", "User-Agent": "opengrokbot-update-check"}, timeout=8.0, follow_redirects=True)
    r.raise_for_status()
    return r.json()


class Updates:
    def __init__(self, settings: Any, current: str = VERSION, fetch: Callable[[], dict] | None = None):
        self.settings, self.current, self._fetch = settings, current, fetch or _fetch

    def enabled(self) -> bool:
        return bool((self.settings.get("updates", {}) or {}).get("check", True))

    def state(self) -> dict:
        st = self.settings.get("updates_state", {}) or {}
        latest = st.get("latest", "")
        return {"enabled": self.enabled(), "current": self.current, "latest": latest, "newer": bool(latest) and is_newer(latest, self.current),
                "url": st.get("url", ""), "name": st.get("name", ""), "checked_at": st.get("checked_at", 0), "error": st.get("error", "")}

    def check(self, force: bool = False) -> dict:
        st = self.settings.get("updates_state", {}) or {}
        if not force and (not self.enabled() or time.time() - float(st.get("checked_at", 0) or 0) < INTERVAL):
            return self.state()
        try:
            rel = self._fetch()
            tag = str(rel.get("tag_name", "")).lstrip("v")
            self.settings.set("updates_state", {"checked_at": time.time(), "latest": tag, "url": rel.get("html_url", ""), "name": rel.get("name", ""), "error": ""})
        except Exception as e:  # noqa: BLE001  (offline, rate-limited...: remember the time so we do not retry in a loop)
            self.settings.set("updates_state", {**st, "checked_at": time.time(), "error": str(e)[:200]})
        return self.state()
