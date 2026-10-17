"""What the polling triggers look at: a web page, a feed, a Gmail search, the calendar.

Each source has a `check_*` that returns (events, new state). The state is a small JSON-able dict kept on the trigger. The first
check only records a baseline, so creating a trigger never fires on what is already there. Only events that are returned (and so
will start a task) are recorded as seen: if more arrive than `limit`, the rest are still unseen at the next check."""
from __future__ import annotations

import difflib
import hashlib
import html
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

PAGE_KEEP = 20_000          # characters of page text kept as the baseline
SEEN_KEEP = 400             # ids remembered per feed / mailbox
FEED_BYTES = 3_000_000
MAX_EVENTS = 5              # events started per check, so a flood does not start dozens of tasks at once
DIFF_LINES = 2000


class WatchError(ValueError):
    pass


def _remember(seen: list[str], fired: list[str]) -> list[str]:
    """Newest first, no duplicates, capped."""
    out = list(dict.fromkeys([*fired, *seen]))
    return out[:SEEN_KEEP]


# ----------------------------------------------------------------------------- web page
def page_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head|template)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|section|article|td|th)>", "\n", raw)
    raw = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def focus_text(text: str, focus: str) -> str:
    """The part of the page that starts at the first line containing `focus` (about 40 lines). Empty focus: the whole page."""
    if not focus.strip():
        return text
    lines = text.splitlines()
    want = focus.strip().lower()
    for i, ln in enumerate(lines):
        if want in ln.lower():
            return "\n".join(lines[i:i + 40])
    return ""


def describe_change(old: str, new: str, limit: int = 4000) -> str:
    a, b = old.splitlines()[:DIFF_LINES], new.splitlines()[:DIFF_LINES]
    removed, added = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(a[i1:i2])
        if tag in ("replace", "insert"):
            added.extend(b[j1:j2])
    out = []
    if removed:
        out.append("Removed:\n" + "\n".join("- " + x for x in removed[:40]))
    if added:
        out.append("Added:\n" + "\n".join("+ " + x for x in added[:40]))
    return ("\n\n".join(out) or "The text changed only in spacing.")[:limit]


def check_page(fetch: Callable[[str], str], url: str, focus: str, state: dict) -> tuple[list[dict], dict]:
    """A change fires only when the same new text is seen on two checks in a row, so a clock, a counter or a rotating ad does not
    fire on every poll."""
    text = focus_text(page_text(fetch(url)), focus)[:PAGE_KEEP]
    if focus.strip() and not text:
        raise WatchError(f"The text '{focus}' is not on the page any more, so there is nothing to compare.")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    now = time.time()
    if "hash" not in state:
        return [], {"hash": digest, "text": text, "checked": now}
    if digest == state["hash"]:
        return [], {"hash": digest, "text": state.get("text", text), "checked": now}          # back to the baseline: forget a one-off change
    if state.get("pending") != digest:
        return [], {**state, "pending": digest, "checked": now}                              # changed once: wait for it to hold
    change = describe_change(state.get("text", ""), text)
    ev = {"key": digest[:12], "summary": f"{url} changed", "payload": f"Page: {url}\n\n{change}", "detail": url}
    return [ev], {"hash": digest, "text": text, "checked": now}


# ----------------------------------------------------------------------------- RSS / Atom
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(raw: str) -> list[dict]:
    """Items of an RSS 2.0 or Atom feed as the feed lists them: [{id, title, link, date, summary}]."""
    if len(raw) > FEED_BYTES:
        raise WatchError("That feed is too large.")
    if re.search(r"<!(DOCTYPE|ENTITY)", raw, re.I):
        raise WatchError("That feed declares a DOCTYPE or entities, which is not accepted.")   # no entity-expansion tricks
    try:
        root = ET.fromstring(raw.lstrip("﻿").strip().encode("utf-8"))
    except ET.ParseError as e:
        raise WatchError(f"That is not a valid RSS or Atom feed ({e}).") from e
    items = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        d: dict[str, str] = {}
        for ch in el:
            t = _local(ch.tag)
            txt = (ch.text or "").strip()
            if t == "title":
                d["title"] = txt
            elif t == "link":
                d.setdefault("link", ch.get("href") or txt)
            elif t in ("guid", "id"):
                d["id"] = txt
            elif t in ("pubdate", "published", "updated", "date"):
                d.setdefault("date", txt)
            elif t in ("description", "summary", "content", "encoded"):
                d.setdefault("summary", re.sub(r"\s+", " ", page_text(txt))[:600])
        d["id"] = d.get("id") or d.get("link") or d.get("title", "")
        if d["id"]:
            items.append(d)
    if not items and _local(root.tag) not in ("rss", "feed", "rdf"):
        raise WatchError("That address is not an RSS or Atom feed.")
    return items


def check_feed(fetch: Callable[[str], str], url: str, state: dict, limit: int = MAX_EVENTS) -> tuple[list[dict], dict]:
    items = parse_feed(fetch(url))
    ids = [it["id"] for it in items]
    now = time.time()
    if "seen" not in state:
        return [], {"seen": _remember([], ids), "checked": now}
    seen = set(state["seen"])
    fresh = [it for it in reversed(items) if it["id"] not in seen][:max(0, min(limit, MAX_EVENTS))]       # oldest first
    events = [{"key": it["id"][:200], "summary": it.get("title") or it["id"],
               "payload": f"Title: {it.get('title', '')}\nLink: {it.get('link', '')}\nDate: {it.get('date', '')}\n\n{it.get('summary', '')}",
               "detail": it.get("title", "")[:200]} for it in fresh]
    return events, {"seen": _remember(state["seen"], [it["id"] for it in fresh]), "checked": now}


# ----------------------------------------------------------------------------- Gmail
def check_email(search: Callable[[str], list[dict]], query: str, state: dict, limit: int = MAX_EVENTS) -> tuple[list[dict], dict]:
    """`search(query)` returns message summaries [{id, from, subject, date, snippet}], newest first (the Gmail connector's format)."""
    msgs = search(query)
    now = time.time()
    if "seen" not in state:
        return [], {"seen": _remember([], [m["id"] for m in msgs]), "checked": now}
    seen = set(state["seen"])
    fresh = [m for m in reversed(msgs) if m["id"] not in seen][:max(0, min(limit, MAX_EVENTS))]
    events = [{"key": m["id"], "summary": m.get("subject") or "(no subject)",
               "payload": f"From: {m.get('from', '')}\nSubject: {m.get('subject', '')}\nDate: {m.get('date', '')}\nMessage id: {m['id']}\n\n{m.get('snippet', '')}",
               "detail": f"{m.get('from', '')}: {m.get('subject', '')}"[:200]} for m in fresh]
    return events, {"seen": _remember(state["seen"], [m["id"] for m in fresh]), "checked": now}


# ----------------------------------------------------------------------------- Google Calendar
def _start_of(ev: dict) -> datetime | None:
    v = (ev.get("start") or {}).get("dateTime") or ""
    if not v:
        return None          # all-day events have no start time to warn before
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def check_calendar(list_events: Callable[[str, str], list[dict]], minutes_before: int, state: dict, limit: int = MAX_EVENTS,
                   now: datetime | None = None) -> tuple[list[dict], dict]:
    """Events that start within `minutes_before` minutes. Each fires once (remembered by id and start time)."""
    now = now or datetime.now(timezone.utc)
    window_end = now + timedelta(minutes=max(1, minutes_before))
    evs = list_events(now.isoformat(), window_end.isoformat())
    fired = {k: v for k, v in dict(state.get("fired", {})).items() if v > time.time() - 2 * 86400}     # forget after two days
    due = []
    for ev in evs:
        start = _start_of(ev)
        if start and now <= start <= window_end and f"{ev.get('id', '')}@{start.isoformat()}" not in fired:
            due.append((start, ev))
    due.sort(key=lambda t: t[0])
    events = []
    for start, ev in due[:max(0, min(limit, MAX_EVENTS))]:
        key = f"{ev.get('id', '')}@{start.isoformat()}"
        fired[key] = time.time()
        mins = max(0, int((start - now).total_seconds() // 60))
        local = start.astimezone().strftime("%H:%M")
        events.append({"key": key[:200], "summary": f"{ev.get('summary') or 'Event'} at {local}",
                       "payload": f"Event: {ev.get('summary', '')}\nStarts: {start.astimezone().strftime('%Y-%m-%d %H:%M')} (in {mins} min)\n"
                                  f"Location: {ev.get('location') or ''}\nAttendees: {', '.join(x for x in (ev.get('attendees') or []) if x)}",
                       "detail": f"{ev.get('summary', '')} at {local}"[:200]})
    return events, {"fired": fired, "checked": time.time()}


# ----------------------------------------------------------------------------- connector access
def connector_reader(engine: Any, bot: dict, pid: str, tool: str, make_args: Callable[..., dict]) -> Callable[..., list[dict]]:
    """A function that calls a connector read tool as this Bot and returns its plain list. It goes through the real tool builder
    (so the Bot's grant, the admin presets and the network policy all apply) and through the tool's own `raw` function."""
    from .tooling import ToolContext

    def call(*a: Any) -> list[dict]:
        if f"plugin:{pid}" not in set(bot.get("grants") or []):
            raise WatchError(f"{bot['name']} has not been given the {pid.replace('_', ' ')} connector. Grant it in the Bot's settings.")
        spec = next((t for t in engine.plugins.tool_specs({f"plugin:{pid}"}) if t.name == tool), None)
        if spec is None or spec.raw is None:
            raise WatchError(f"The {pid.replace('_', ' ')} connector is not set up or not allowed (Plugins page).")
        out = spec.raw(ToolContext(engine, bot, "", "", None), make_args(*a))      # type: ignore[arg-type]
        if not isinstance(out, list):
            raise WatchError(f"The {pid.replace('_', ' ')} connector returned something unexpected.")
        return out
    return call


def gmail_reader(engine: Any, bot: dict) -> Callable[[str], list[dict]]:
    return connector_reader(engine, bot, "gmail", "gmail_search", lambda q: {"query": q, "max_results": 25})


def calendar_reader(engine: Any, bot: dict) -> Callable[[str, str], list[dict]]:
    return connector_reader(engine, bot, "google_calendar", "gcal_list_events", lambda a, b: {"time_min": a, "time_max": b})
