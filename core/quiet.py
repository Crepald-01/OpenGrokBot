"""Quiet hours and Do Not Disturb: when they apply, notifications are still recorded (the Inbox keeps them) but nothing
pops up, beeps or is pushed to the phone."""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

HHMM = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)?\s*$", re.I)


def minutes_of(hhmm: str, default: int) -> int:
    m = HHMM.match(hhmm or "")
    if not m:
        return default
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 60 + mi if h < 24 and mi < 60 else default


def in_window(now_min: int, start: int, end: int) -> bool:
    """True when `now_min` is inside [start, end). A window that ends before it starts crosses midnight (22:00-07:00)."""
    if start == end:
        return False
    return start <= now_min < end if start < end else (now_min >= start or now_min < end)


def is_quiet(cfg: dict[str, Any], at: float | None = None) -> bool:
    at = at or time.time()
    if float(cfg.get("dnd_until", 0) or 0) > at:
        return True
    if not cfg.get("quiet_enabled"):
        return False
    d = datetime.fromtimestamp(at)
    return in_window(d.hour * 60 + d.minute, minutes_of(cfg.get("quiet_start", "22:00"), 22 * 60), minutes_of(cfg.get("quiet_end", "07:00"), 7 * 60))


def parse_duration(text: str) -> float | None:
    """'30m', '2h', '1.5 hours', '1d' or a bare number of minutes -> seconds. None when it is not a duration."""
    m = DURATION.match(text or "")
    if not m:
        return None
    n = float(m.group(1))
    unit = (m.group(2) or "m").lower()
    secs = n * (86400 if unit.startswith("d") else 3600 if unit.startswith("h") else 60)
    return secs if 0 < secs <= 14 * 86400 else None


def describe(cfg: dict[str, Any], at: float | None = None) -> str:
    at = at or time.time()
    until = float(cfg.get("dnd_until", 0) or 0)
    parts = []
    if until > at:
        parts.append(f"Do Not Disturb until {datetime.fromtimestamp(until).strftime('%H:%M' if until - at < 86400 else '%a %H:%M')}")
    if cfg.get("quiet_enabled"):
        parts.append(f"quiet hours {cfg.get('quiet_start', '22:00')}-{cfg.get('quiet_end', '07:00')}" + (" (active now)" if is_quiet({**cfg, "dnd_until": 0}, at) else ""))
    return "; ".join(parts) or "Notifications are on."
