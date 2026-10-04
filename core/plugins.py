"""Plugins and connectors.

Built-in connectors: Gmail, Google Calendar, Slack, Notion, Linear, Jira, GitHub, and a generic REST connector.
Third-party plugins live in folders with a plugin.json manifest and can be installed from a local folder or git URL:

  * declarative  - tools described as HTTP requests in plugin.json (no code)
  * python       - plugin.json has "entry": "plugin.py" exposing register(api) (runs in-process: only install code you trust)

Bots only see a plugin's tools after the user grants the plugin to that Bot.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import inspect
import json
import re
import secrets as pysecrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import quote, urlencode

from . import paths, secrets
from .settings import plugin_allowed
from .tooling import Risk, ToolContext, ToolResult, ToolSpec, b, i, obj, s, safe_name, short

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine


class ConnectorError(RuntimeError):
    pass


@dataclass
class PluginDef:
    id: str
    name: str
    description: str
    kind: str = "builtin"          # builtin | declarative | python | rest
    version: str = "1.0.0"
    fields: list[dict] = field(default_factory=list)   # {key,label,secret,required,placeholder,default}
    oauth: dict | None = None      # {"provider":"google","scopes":[...]}
    build: Callable[["Conn"], list[ToolSpec]] | None = None
    check: Callable[["Conn"], str] | None = None
    path: Path | None = None
    source: str = "builtin"
    skills: list[str] = field(default_factory=list)
    help: str = ""


def _res(data: Any, source: str, limit: int = 20000) -> ToolResult:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=1)
    if len(text) > limit:
        text = text[:limit] + f"\n[... truncated {len(text) - limit} chars ...]"
    return ToolResult(text, untrusted=source)


def T(name: str, desc: str, props: dict, required: list[str], fn: Callable[[ToolContext, dict], Any], *, risk=None,
      label: Callable[[dict], str] | None = None, read: bool = False, untrusted: str = "") -> ToolSpec:
    def handler(ctx: ToolContext, args: dict) -> ToolResult:
        out = fn(ctx, args)
        if isinstance(out, ToolResult):
            return out
        return _res(out, untrusted) if untrusted else ToolResult(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)[:8000])
    return ToolSpec(name, desc, obj(props, required), handler, risk=risk, label=label or (lambda a, n=name: n), read_only=read)


def R(category: str, summary: Callable[[dict], str], pattern: Callable[[dict], str] | None = None, never_auto: bool = False,
      detail: Callable[[dict], dict] | None = None):
    def fn(ctx: ToolContext, args: dict) -> Risk:
        d = dict(detail(args)) if detail else {}
        d["pattern"] = pattern(args) if pattern else "*"
        return Risk(category, summary(args), d, never_auto=never_auto)
    return fn


# ----------------------------------------------------------------------------- Google OAuth
class GoogleOAuth:
    AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN = "https://oauth2.googleapis.com/token"

    def __init__(self) -> None:
        self.pending: dict[str, dict] = {}
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def auth_url(self, plugin_id: str, client_id: str, scopes: list[str], redirect_uri: str) -> str:
        state = pysecrets.token_urlsafe(24)
        verifier = pysecrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.pending[state] = {"plugin": plugin_id, "verifier": verifier, "redirect": redirect_uri, "ts": time.time()}
        q = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(scopes),
             "access_type": "offline", "prompt": "consent", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
        return f"{self.AUTH}?{urlencode(q)}"

    def exchange(self, state: str, code: str, client_id: str, client_secret: str) -> str:
        import httpx
        p = self.pending.pop(state, None)
        if not p or time.time() - p["ts"] > 900:
            raise ConnectorError("This sign-in link expired or was not started here. Start again from Plugins.")
        r = httpx.post(self.TOKEN, data={"code": code, "client_id": client_id, "client_secret": client_secret, "redirect_uri": p["redirect"],
                                         "grant_type": "authorization_code", "code_verifier": p["verifier"]}, timeout=30)
        if r.status_code >= 400:
            raise ConnectorError(f"Google rejected the sign-in: {short(r.text, 200)}")
        data = r.json()
        if not data.get("refresh_token"):
            raise ConnectorError("Google did not return a refresh token. Remove the app's access in your Google account and try again.")
        secrets.set_secret(f"plugin:{p['plugin']}:refresh_token", data["refresh_token"])
        with self._lock:
            self._cache[p["plugin"]] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)) - 60)
        return p["plugin"]

    def access_token(self, plugin_id: str, client_id: str, client_secret: str) -> str:
        import httpx
        with self._lock:
            tok = self._cache.get(plugin_id)
            if tok and tok[1] > time.time():
                return tok[0]
        refresh = secrets.get_secret(f"plugin:{plugin_id}:refresh_token")
        if not refresh:
            raise ConnectorError("Not signed in to Google. Open Plugins and press Connect.")
        r = httpx.post(self.TOKEN, data={"client_id": client_id, "client_secret": client_secret, "refresh_token": refresh,
                                         "grant_type": "refresh_token"}, timeout=30)
        if r.status_code >= 400:
            raise ConnectorError(f"Google sign-in expired ({short(r.text, 120)}). Open Plugins and press Connect again.")
        data = r.json()
        with self._lock:
            self._cache[plugin_id] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)) - 60)
        return data["access_token"]


# ----------------------------------------------------------------------------- per-plugin runtime
class Conn:
    """What a plugin's tool builder uses: config, secrets, and a policy-checked HTTP client."""

    def __init__(self, mgr: "PluginManager", pdef: PluginDef):
        self.mgr, self.pdef = mgr, pdef

    def cfg(self, key: str, default: str = "") -> str:
        return self.mgr.config_value(self.pdef.id, key) or default

    def http(self, ctx: ToolContext, method: str, url: str, *, headers: dict | None = None, params: dict | None = None,
             json_body: Any = None, data: Any = None, timeout: float = 30) -> Any:
        import httpx
        ok, why = self.mgr.engine.net.check(ctx.bot["id"], url)
        if not ok:
            raise ConnectorError(f"Blocked by network policy: {why}")
        try:
            r = httpx.request(method, url, headers=headers, params=params, json=json_body, data=data, timeout=timeout, follow_redirects=True)
        except httpx.HTTPError as e:
            raise ConnectorError(f"Could not reach {self.pdef.name}: {e}") from e
        if r.status_code in (401, 403):
            raise ConnectorError(f"{self.pdef.name} refused the request (HTTP {r.status_code}). Check the credentials/permissions in Plugins. {short(r.text, 200)}")
        if r.status_code == 429:
            raise ConnectorError(f"{self.pdef.name} rate-limited the request. Wait and retry.")
        if r.status_code >= 400:
            raise ConnectorError(f"{self.pdef.name} error {r.status_code}: {short(r.text, 400)}")
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return r.text

    def google_headers(self) -> dict:
        cid, csec = self.cfg("client_id"), self.cfg("client_secret")
        if self.pdef.id != "gmail":
            cid = cid or self.mgr.config_value("gmail", "client_id")
            csec = csec or self.mgr.config_value("gmail", "client_secret")
        return {"Authorization": "Bearer " + self.mgr.google.access_token(self.pdef.id, cid, csec)}


# ----------------------------------------------------------------------------- built-in connectors
def _b64(txt: str) -> str:
    return base64.urlsafe_b64encode(txt.encode("utf-8")).decode()


def _gmail_text(payload: dict) -> str:
    def walk(p: dict, want: str) -> str:
        if p.get("mimeType", "").startswith(want) and p.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(p["body"]["data"] + "===").decode("utf-8", "replace")
        for sub in p.get("parts", []) or []:
            t = walk(sub, want)
            if t:
                return t
        return ""
    text = walk(payload, "text/plain")
    if not text:
        h = walk(payload, "text/html")
        text = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", " ", h))
        text = re.sub(r"\s+", " ", text)
    return text


def build_gmail(c: Conn) -> list[ToolSpec]:
    base = "https://gmail.googleapis.com/gmail/v1/users/me"

    def H() -> dict:
        return c.google_headers()

    def search(ctx, a):
        res = c.http(ctx, "GET", f"{base}/messages", headers=H(), params={"q": a.get("query", ""), "maxResults": min(int(a.get("max_results", 10)), 25)})
        out = []
        for m in res.get("messages", []):
            d = c.http(ctx, "GET", f"{base}/messages/{m['id']}", headers=H(),
                       params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]})
            hd = {x["name"]: x["value"] for x in d.get("payload", {}).get("headers", [])}
            out.append({"id": d["id"], "thread_id": d["threadId"], "from": hd.get("From"), "to": hd.get("To"), "subject": hd.get("Subject"),
                        "date": hd.get("Date"), "snippet": d.get("snippet"), "labels": d.get("labelIds")})
        return out or "No messages matched."

    def read(ctx, a):
        d = c.http(ctx, "GET", f"{base}/messages/{a['message_id']}", headers=H(), params={"format": "full"})
        hd = {x["name"]: x["value"] for x in d.get("payload", {}).get("headers", [])}
        return {"id": d["id"], "thread_id": d["threadId"], "from": hd.get("From"), "to": hd.get("To"), "cc": hd.get("Cc"),
                "subject": hd.get("Subject"), "date": hd.get("Date"), "message_id_header": hd.get("Message-ID"),
                "body": _gmail_text(d.get("payload", {}))[:10000]}

    def mime(a) -> str:
        m = EmailMessage()
        m["To"], m["Subject"] = a["to"], a.get("subject", "")
        if a.get("cc"):
            m["Cc"] = a["cc"]
        if a.get("in_reply_to"):
            m["In-Reply-To"] = m["References"] = a["in_reply_to"]
        m.set_content(a.get("body", ""))
        return base64.urlsafe_b64encode(m.as_bytes()).decode()

    def draft(ctx, a):
        msg: dict[str, Any] = {"raw": mime(a)}
        if a.get("thread_id"):
            msg["threadId"] = a["thread_id"]
        d = c.http(ctx, "POST", f"{base}/drafts", headers=H(), json_body={"message": msg})
        return f"Draft created (id {d['id']}). It has NOT been sent."

    def send(ctx, a):
        msg: dict[str, Any] = {"raw": mime(a)}
        if a.get("thread_id"):
            msg["threadId"] = a["thread_id"]
        d = c.http(ctx, "POST", f"{base}/messages/send", headers=H(), json_body=msg)
        return f"Email sent (id {d.get('id')})."

    def send_draft(ctx, a):
        d = c.http(ctx, "POST", f"{base}/drafts/send", headers=H(), json_body={"id": a["draft_id"]})
        return f"Draft sent (message id {d.get('id')})."

    def label(ctx, a):
        c.http(ctx, "POST", f"{base}/messages/{a['message_id']}/modify", headers=H(),
               json_body={"addLabelIds": a.get("add", []), "removeLabelIds": a.get("remove", [])})
        return "Labels updated."

    mail_props = {"to": s("Recipient address(es), comma separated"), "subject": s("Subject"), "body": s("Plain-text body"),
                  "cc": s("Cc addresses"), "thread_id": s("Gmail thread id when replying"), "in_reply_to": s("Message-ID header being replied to")}
    send_risk = R("send", lambda a: f"Send email to {a.get('to')}: \"{short(a.get('subject', ''), 80)}\"", lambda a: str(a.get("to", "*")).lower(),
                  detail=lambda a: {"to": a.get("to"), "cc": a.get("cc"), "subject": a.get("subject"), "body": short(a.get("body", ""), 1500)})
    return [
        T("gmail_search", "Search Gmail with Gmail query syntax (e.g. 'is:unread newer_than:2d'). Returns message summaries.",
          {"query": s("Gmail search query"), "max_results": i("Max results (<=25)")}, ["query"], search, read=True, untrusted="Gmail",
          label=lambda a: f"Search Gmail: {a.get('query', '')}"),
        T("gmail_read", "Read one email by id.", {"message_id": s("Message id")}, ["message_id"], read, read=True, untrusted="an email",
          label=lambda a: "Read an email"),
        T("gmail_create_draft", "Create a draft email (not sent). Use this to propose replies.", mail_props, ["to", "subject", "body"], draft,
          label=lambda a: f"Draft email to {a.get('to')}"),
        T("gmail_send", "Send an email. Requires user approval.", mail_props, ["to", "subject", "body"], send, risk=send_risk,
          label=lambda a: f"Send email to {a.get('to')}"),
        T("gmail_send_draft", "Send an existing draft by id. Requires user approval.", {"draft_id": s("Draft id")}, ["draft_id"], send_draft,
          risk=R("send", lambda a: f"Send Gmail draft {a.get('draft_id')}", lambda a: "draft"), label=lambda a: "Send a draft"),
        T("gmail_label", "Add/remove labels on a message (e.g. archive = remove INBOX, mark read = remove UNREAD).",
          {"message_id": s("Message id"), "add": {"type": "array", "items": {"type": "string"}}, "remove": {"type": "array", "items": {"type": "string"}}},
          ["message_id"], label, label=lambda a: "Update Gmail labels"),
    ]


def build_calendar(c: Conn) -> list[ToolSpec]:
    base = "https://www.googleapis.com/calendar/v3"

    def H() -> dict:
        return c.google_headers()

    def events(ctx, a):
        res = c.http(ctx, "GET", f"{base}/calendars/{quote(a.get('calendar_id', 'primary'))}/events", headers=H(),
                     params={"timeMin": a["time_min"], "timeMax": a["time_max"], "singleEvents": "true", "orderBy": "startTime", "maxResults": 50})
        return [{"id": e["id"], "summary": e.get("summary"), "start": e.get("start"), "end": e.get("end"), "location": e.get("location"),
                 "attendees": [x.get("email") for x in e.get("attendees", [])]} for e in res.get("items", [])] or "No events."

    def free(ctx, a):
        res = c.http(ctx, "POST", f"{base}/freeBusy", headers=H(), json_body={"timeMin": a["time_min"], "timeMax": a["time_max"],
                                                                              "items": [{"id": x} for x in a.get("calendars", ["primary"])]})
        return res.get("calendars", {})

    def create(ctx, a):
        body = {"summary": a["summary"], "description": a.get("description", ""), "location": a.get("location", ""),
                "start": {"dateTime": a["start"]}, "end": {"dateTime": a["end"]}}
        if a.get("attendees"):
            body["attendees"] = [{"email": x} for x in a["attendees"]]
        e = c.http(ctx, "POST", f"{base}/calendars/primary/events", headers=H(), params={"sendUpdates": "all" if a.get("attendees") else "none"}, json_body=body)
        return f"Event created: {e.get('htmlLink')}"

    def delete(ctx, a):
        c.http(ctx, "DELETE", f"{base}/calendars/primary/events/{a['event_id']}", headers=H())
        return "Event deleted."

    def create_risk(ctx: ToolContext, a: dict) -> Risk | None:
        if a.get("attendees"):
            return Risk("send", f"Create event '{a.get('summary')}' and invite {', '.join(a['attendees'])}", {"pattern": "invite", "start": a.get("start")})
        return None

    return [
        T("gcal_list_events", "List calendar events in a time range (RFC3339 times with timezone).",
          {"time_min": s("RFC3339 start"), "time_max": s("RFC3339 end"), "calendar_id": s("Calendar id, default primary")}, ["time_min", "time_max"],
          events, read=True, untrusted="Google Calendar", label=lambda a: "List calendar events"),
        T("gcal_freebusy", "Find busy periods to propose meeting times.", {"time_min": s("RFC3339"), "time_max": s("RFC3339"),
                                                                            "calendars": {"type": "array", "items": {"type": "string"}}},
          ["time_min", "time_max"], free, read=True, untrusted="Google Calendar", label=lambda a: "Check free/busy"),
        T("gcal_create_event", "Create an event. Inviting attendees sends invitations and needs approval.",
          {"summary": s("Title"), "start": s("RFC3339 start"), "end": s("RFC3339 end"), "description": s("Description"), "location": s("Location"),
           "attendees": {"type": "array", "items": {"type": "string"}, "description": "Attendee emails"}}, ["summary", "start", "end"], create,
          risk=create_risk, label=lambda a: f"Create event: {a.get('summary')}"),
        T("gcal_delete_event", "Delete an event by id. Requires approval.", {"event_id": s("Event id")}, ["event_id"], delete,
          risk=R("delete", lambda a: f"Delete calendar event {a.get('event_id')}", lambda a: "event"), label=lambda a: "Delete calendar event"),
    ]


def build_slack(c: Conn) -> list[ToolSpec]:
    api = "https://slack.com/api/"

    def call(ctx, method: str, params: dict | None = None, user: bool = False, post: bool = False) -> dict:
        tok = c.cfg("user_token") if user else (c.cfg("bot_token") or c.cfg("user_token"))
        if not tok:
            raise ConnectorError("Slack token missing. Add it in Plugins.")
        h = {"Authorization": f"Bearer {tok}"}
        res = c.http(ctx, "POST" if post else "GET", api + method, headers=h, params=None if post else params,
                     json_body=params if post else None)
        if isinstance(res, dict) and not res.get("ok"):
            raise ConnectorError(f"Slack error: {res.get('error')}")
        return res

    def channel_id(ctx, name: str) -> str:
        if re.match(r"^[CGD][A-Z0-9]{6,}$", name):
            return name
        cursor = ""
        for _ in range(10):
            r = call(ctx, "conversations.list", {"limit": 200, "types": "public_channel,private_channel", "cursor": cursor})
            for ch in r.get("channels", []):
                if ch["name"] == name.lstrip("#"):
                    return ch["id"]
            cursor = r.get("response_metadata", {}).get("next_cursor", "")
            if not cursor:
                break
        raise ConnectorError(f"Channel {name} not found (or the app is not in it).")

    def list_ch(ctx, a):
        r = call(ctx, "conversations.list", {"limit": 200, "types": "public_channel,private_channel", "exclude_archived": "true"})
        return [{"id": x["id"], "name": x["name"], "members": x.get("num_members"), "topic": x.get("topic", {}).get("value", "")} for x in r.get("channels", [])]

    def read(ctx, a):
        r = call(ctx, "conversations.history", {"channel": channel_id(ctx, a["channel"]), "limit": min(int(a.get("limit", 20)), 100)})
        return [{"ts": m.get("ts"), "user": m.get("user") or m.get("bot_id"), "text": m.get("text"), "thread_ts": m.get("thread_ts"),
                 "replies": m.get("reply_count")} for m in r.get("messages", [])]

    def post(ctx, a):
        body = {"channel": channel_id(ctx, a["channel"]), "text": a["text"]}
        if a.get("thread_ts"):
            body["thread_ts"] = a["thread_ts"]
        r = call(ctx, "chat.postMessage", body, post=True)
        return f"Posted to {a['channel']} (ts {r.get('ts')})."

    def search(ctx, a):
        r = call(ctx, "search.messages", {"query": a["query"], "count": 10}, user=True)
        return [{"channel": m.get("channel", {}).get("name"), "user": m.get("username"), "text": m.get("text"), "ts": m.get("ts"),
                 "permalink": m.get("permalink")} for m in r.get("messages", {}).get("matches", [])]

    return [
        T("slack_list_channels", "List Slack channels.", {}, [], list_ch, read=True, untrusted="Slack", label=lambda a: "List Slack channels"),
        T("slack_read_channel", "Read recent messages from a channel (name or id).", {"channel": s("Channel name or id"), "limit": i("Number of messages")},
          ["channel"], read, read=True, untrusted="Slack messages", label=lambda a: f"Read Slack {a.get('channel')}"),
        T("slack_post_message", "Post a message to a channel. Requires approval.", {"channel": s("Channel name or id"), "text": s("Message text"),
                                                                                    "thread_ts": s("Reply in thread")}, ["channel", "text"], post,
          risk=R("send", lambda a: f"Post to Slack {a.get('channel')}: {short(a.get('text', ''), 160)}", lambda a: str(a.get("channel")),
                 detail=lambda a: {"channel": a.get("channel"), "text": a.get("text")}), label=lambda a: f"Post to Slack {a.get('channel')}"),
        T("slack_search", "Search Slack messages (needs a user token).", {"query": s("Search query")}, ["query"], search, read=True,
          untrusted="Slack search", label=lambda a: f"Search Slack: {a.get('query')}"),
    ]


def build_notion(c: Conn) -> list[ToolSpec]:
    base = "https://api.notion.com/v1"

    def H() -> dict:
        return {"Authorization": f"Bearer {c.cfg('token')}", "Notion-Version": "2022-06-28"}

    def plain(rt: list) -> str:
        return "".join(x.get("plain_text", "") for x in rt or [])

    def search(ctx, a):
        r = c.http(ctx, "POST", f"{base}/search", headers=H(), json_body={"query": a["query"], "page_size": 10})
        out = []
        for x in r.get("results", []):
            title = ""
            if x["object"] == "page":
                for p in x.get("properties", {}).values():
                    if p.get("type") == "title":
                        title = plain(p["title"])
            else:
                title = plain(x.get("title", []))
            out.append({"id": x["id"], "type": x["object"], "title": title, "url": x.get("url")})
        return out

    def read(ctx, a):
        r = c.http(ctx, "GET", f"{base}/blocks/{a['page_id']}/children", headers=H(), params={"page_size": 100})
        lines = []
        for blk in r.get("results", []):
            t = blk["type"]
            lines.append(plain(blk.get(t, {}).get("rich_text", [])) if isinstance(blk.get(t), dict) else "")
        return "\n".join(l for l in lines if l)

    def paras(text: str) -> list[dict]:
        return [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": p[:1900]}}]}}
                for p in text.split("\n\n") if p.strip()][:90]

    def create(ctx, a):
        body = {"parent": {"page_id": a["parent_page_id"]}, "properties": {"title": {"title": [{"text": {"content": a["title"]}}]}},
                "children": paras(a.get("content", ""))}
        r = c.http(ctx, "POST", f"{base}/pages", headers=H(), json_body=body)
        return f"Page created: {r.get('url')}"

    def append(ctx, a):
        c.http(ctx, "PATCH", f"{base}/blocks/{a['page_id']}/children", headers=H(), json_body={"children": paras(a["text"])})
        return "Appended."

    return [
        T("notion_search", "Search Notion pages and databases.", {"query": s("Query")}, ["query"], search, read=True, untrusted="Notion", label=lambda a: f"Search Notion: {a.get('query')}"),
        T("notion_read_page", "Read a Notion page's text content.", {"page_id": s("Page id")}, ["page_id"], read, read=True, untrusted="a Notion page",
          label=lambda a: "Read Notion page"),
        T("notion_create_page", "Create a page under a parent page. Requires approval.", {"parent_page_id": s("Parent page id"), "title": s("Title"),
                                                                                         "content": s("Body text; blank line separates paragraphs")},
          ["parent_page_id", "title"], create, risk=R("write", lambda a: f"Create Notion page '{a.get('title')}'", lambda a: "notion-create"),
          label=lambda a: f"Create Notion page {a.get('title')}"),
        T("notion_append", "Append text to a page. Requires approval.", {"page_id": s("Page id"), "text": s("Text")}, ["page_id", "text"], append,
          risk=R("write", lambda a: f"Append to Notion page {a.get('page_id')}: {short(a.get('text', ''), 120)}", lambda a: "notion-append"),
          label=lambda a: "Append to Notion page"),
    ]


def build_linear(c: Conn) -> list[ToolSpec]:
    def gql(ctx, query: str, variables: dict | None = None) -> dict:
        r = c.http(ctx, "POST", "https://api.linear.app/graphql", headers={"Authorization": c.cfg("api_key"), "Content-Type": "application/json"},
                   json_body={"query": query, "variables": variables or {}})
        if isinstance(r, dict) and r.get("errors"):
            raise ConnectorError("Linear error: " + short(r["errors"][0].get("message", ""), 200))
        return r["data"]

    ISSUE = "id identifier title description priority url state{name} assignee{name} team{key} comments(first:10){nodes{body user{name} createdAt}}"

    def search(ctx, a):
        d = gql(ctx, "query($q:String!){issues(first:15, filter:{or:[{title:{containsIgnoreCase:$q}},{description:{containsIgnoreCase:$q}}]}){nodes{identifier title url state{name} assignee{name} priority}}}", {"q": a["query"]})
        return d["issues"]["nodes"] or "No issues found."

    def get(ctx, a):
        return gql(ctx, "query($id:String!){issue(id:$id){" + ISSUE + "}}", {"id": a["issue"]})["issue"]

    def create(ctx, a):
        team = gql(ctx, "query($k:String!){teams(filter:{key:{eq:$k}}){nodes{id}}}", {"k": a["team_key"]})["teams"]["nodes"]
        if not team:
            raise ConnectorError(f"No Linear team with key {a['team_key']}.")
        inp = {"teamId": team[0]["id"], "title": a["title"], "description": a.get("description", "")}
        if a.get("priority") is not None:
            inp["priority"] = int(a["priority"])
        d = gql(ctx, "mutation($i:IssueCreateInput!){issueCreate(input:$i){issue{identifier url}}}", {"i": inp})
        return f"Created {d['issueCreate']['issue']['identifier']}: {d['issueCreate']['issue']['url']}"

    def comment(ctx, a):
        iid = gql(ctx, "query($id:String!){issue(id:$id){id}}", {"id": a["issue"]})["issue"]["id"]
        gql(ctx, "mutation($i:CommentCreateInput!){commentCreate(input:$i){success}}", {"i": {"issueId": iid, "body": a["body"]}})
        return "Comment added."

    return [
        T("linear_search_issues", "Search Linear issues by text.", {"query": s("Text")}, ["query"], search, read=True, untrusted="Linear", label=lambda a: f"Search Linear: {a.get('query')}"),
        T("linear_get_issue", "Get a Linear issue by identifier (e.g. ENG-123).", {"issue": s("Identifier or id")}, ["issue"], get, read=True, untrusted="a Linear issue",
          label=lambda a: f"Read Linear {a.get('issue')}"),
        T("linear_create_issue", "Create a Linear issue. Requires approval.", {"team_key": s("Team key, e.g. ENG"), "title": s("Title"), "description": s("Markdown description"),
                                                                              "priority": i("0 none, 1 urgent .. 4 low")}, ["team_key", "title"], create,
          risk=R("write", lambda a: f"Create Linear issue in {a.get('team_key')}: {a.get('title')}", lambda a: "linear-create"), label=lambda a: f"Create Linear issue"),
        T("linear_comment", "Comment on a Linear issue. Requires approval.", {"issue": s("Identifier"), "body": s("Comment markdown")}, ["issue", "body"], comment,
          risk=R("write", lambda a: f"Comment on Linear {a.get('issue')}: {short(a.get('body', ''), 140)}", lambda a: "linear-comment"), label=lambda a: "Comment on Linear issue"),
    ]


def build_jira(c: Conn) -> list[ToolSpec]:
    def base() -> str:
        return c.cfg("base_url").rstrip("/")

    def H() -> dict:
        tok = base64.b64encode(f"{c.cfg('email')}:{c.cfg('api_token')}".encode()).decode()
        return {"Authorization": f"Basic {tok}", "Accept": "application/json", "Content-Type": "application/json"}

    def adf(text: str) -> dict:
        return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in text.split("\n\n") if p.strip()] or
                [{"type": "paragraph", "content": []}]}

    def flat(adf_node: Any) -> str:
        if isinstance(adf_node, dict):
            if adf_node.get("type") == "text":
                return adf_node.get("text", "")
            return " ".join(flat(x) for x in adf_node.get("content", []))
        return ""

    def search(ctx, a):
        r = c.http(ctx, "GET", f"{base()}/rest/api/3/search/jql", headers=H(),
                   params={"jql": a["jql"], "maxResults": min(int(a.get("max_results", 15)), 50), "fields": "summary,status,assignee,priority,issuetype"})
        return [{"key": x["key"], "summary": x["fields"].get("summary"), "status": (x["fields"].get("status") or {}).get("name"),
                 "assignee": (x["fields"].get("assignee") or {}).get("displayName"), "priority": (x["fields"].get("priority") or {}).get("name")}
                for x in r.get("issues", [])] or "No issues."

    def get(ctx, a):
        x = c.http(ctx, "GET", f"{base()}/rest/api/3/issue/{a['key']}", headers=H(), params={"fields": "summary,description,status,assignee,comment"})
        f = x["fields"]
        return {"key": x["key"], "summary": f.get("summary"), "status": (f.get("status") or {}).get("name"), "description": flat(f.get("description"))[:6000],
                "comments": [{"author": (cm.get("author") or {}).get("displayName"), "body": flat(cm.get("body"))[:1000]} for cm in (f.get("comment") or {}).get("comments", [])[-10:]]}

    def create(ctx, a):
        r = c.http(ctx, "POST", f"{base()}/rest/api/3/issue", headers=H(),
                   json_body={"fields": {"project": {"key": a["project_key"]}, "summary": a["summary"], "issuetype": {"name": a.get("issue_type", "Task")},
                                         "description": adf(a.get("description", ""))}})
        return f"Created {r['key']}: {base()}/browse/{r['key']}"

    def comment(ctx, a):
        c.http(ctx, "POST", f"{base()}/rest/api/3/issue/{a['key']}/comment", headers=H(), json_body={"body": adf(a["body"])})
        return "Comment added."

    return [
        T("jira_search", "Search Jira issues with JQL.", {"jql": s("JQL query"), "max_results": i("Max results")}, ["jql"], search, read=True, untrusted="Jira", label=lambda a: f"Search Jira: {a.get('jql')}"),
        T("jira_get_issue", "Get a Jira issue by key.", {"key": s("Issue key, e.g. OPS-12")}, ["key"], get, read=True, untrusted="a Jira issue", label=lambda a: f"Read Jira {a.get('key')}"),
        T("jira_create_issue", "Create a Jira issue. Requires approval.", {"project_key": s("Project key"), "summary": s("Summary"), "description": s("Description"),
                                                                          "issue_type": s("Task, Bug, Story...")}, ["project_key", "summary"], create,
          risk=R("write", lambda a: f"Create Jira issue in {a.get('project_key')}: {a.get('summary')}", lambda a: "jira-create"), label=lambda a: "Create Jira issue"),
        T("jira_comment", "Comment on a Jira issue. Requires approval.", {"key": s("Issue key"), "body": s("Comment")}, ["key", "body"], comment,
          risk=R("write", lambda a: f"Comment on Jira {a.get('key')}: {short(a.get('body', ''), 140)}", lambda a: "jira-comment"), label=lambda a: "Comment on Jira issue"),
    ]


def build_github(c: Conn) -> list[ToolSpec]:
    def base() -> str:
        return (c.cfg("api_url") or "https://api.github.com").rstrip("/")

    def H() -> dict:
        return {"Authorization": f"Bearer {c.cfg('token')}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    def repos(ctx, a):
        r = c.http(ctx, "GET", f"{base()}/user/repos", headers=H(), params={"per_page": 50, "sort": "updated"})
        return [{"full_name": x["full_name"], "private": x["private"], "description": x.get("description"), "open_issues": x.get("open_issues_count")} for x in r]

    def issues(ctx, a):
        r = c.http(ctx, "GET", f"{base()}/repos/{a['repo']}/issues", headers=H(), params={"state": a.get("state", "open"), "per_page": 30, "labels": a.get("labels", "")})
        return [{"number": x["number"], "title": x["title"], "state": x["state"], "labels": [l["name"] for l in x["labels"]], "user": x["user"]["login"],
                 "is_pr": "pull_request" in x, "updated": x["updated_at"]} for x in r] or "No issues."

    def get_issue(ctx, a):
        x = c.http(ctx, "GET", f"{base()}/repos/{a['repo']}/issues/{a['number']}", headers=H())
        cm = c.http(ctx, "GET", f"{base()}/repos/{a['repo']}/issues/{a['number']}/comments", headers=H(), params={"per_page": 20})
        return {"number": x["number"], "title": x["title"], "state": x["state"], "body": (x.get("body") or "")[:6000], "labels": [l["name"] for l in x["labels"]],
                "comments": [{"user": y["user"]["login"], "body": y["body"][:1200]} for y in cm]}

    def prs(ctx, a):
        r = c.http(ctx, "GET", f"{base()}/repos/{a['repo']}/pulls", headers=H(), params={"state": a.get("state", "open"), "per_page": 30})
        return [{"number": x["number"], "title": x["title"], "user": x["user"]["login"], "head": x["head"]["ref"], "base": x["base"]["ref"], "draft": x.get("draft")} for x in r] or "No pull requests."

    def get_file(ctx, a):
        r = c.http(ctx, "GET", f"{base()}/repos/{a['repo']}/contents/{a['path'].lstrip('/')}", headers=H(), params={"ref": a.get("ref", "")} if a.get("ref") else None)
        if isinstance(r, list):
            return [{"name": x["name"], "type": x["type"]} for x in r]
        return base64.b64decode(r.get("content", "")).decode("utf-8", "replace")[:20000]

    def search_code(ctx, a):
        r = c.http(ctx, "GET", f"{base()}/search/code", headers=H(), params={"q": a["query"], "per_page": 10})
        return [{"repo": x["repository"]["full_name"], "path": x["path"], "url": x["html_url"]} for x in r.get("items", [])]

    def create_issue(ctx, a):
        r = c.http(ctx, "POST", f"{base()}/repos/{a['repo']}/issues", headers=H(), json_body={"title": a["title"], "body": a.get("body", ""), "labels": a.get("labels", [])})
        return f"Created issue #{r['number']}: {r['html_url']}"

    def comment(ctx, a):
        r = c.http(ctx, "POST", f"{base()}/repos/{a['repo']}/issues/{a['number']}/comments", headers=H(), json_body={"body": a["body"]})
        return f"Comment posted: {r['html_url']}"

    def create_pr(ctx, a):
        r = c.http(ctx, "POST", f"{base()}/repos/{a['repo']}/pulls", headers=H(), json_body={"title": a["title"], "head": a["head"], "base": a["base"], "body": a.get("body", "")})
        return f"Pull request #{r['number']} opened: {r['html_url']}"

    rp = {"repo": s("owner/name")}
    return [
        T("github_list_repos", "List repositories you can access.", {}, [], repos, read=True, untrusted="GitHub", label=lambda a: "List GitHub repos"),
        T("github_list_issues", "List issues in a repo.", {**rp, "state": s("open|closed|all"), "labels": s("Comma-separated labels")}, ["repo"], issues, read=True,
          untrusted="GitHub issues", label=lambda a: f"List issues in {a.get('repo')}"),
        T("github_get_issue", "Read an issue or PR conversation.", {**rp, "number": i("Issue number")}, ["repo", "number"], get_issue, read=True, untrusted="a GitHub issue",
          label=lambda a: f"Read {a.get('repo')}#{a.get('number')}"),
        T("github_list_prs", "List pull requests.", {**rp, "state": s("open|closed|all")}, ["repo"], prs, read=True, untrusted="GitHub PRs", label=lambda a: f"List PRs in {a.get('repo')}"),
        T("github_get_file", "Read a file or list a folder from a repo.", {**rp, "path": s("Path"), "ref": s("Branch/tag/sha")}, ["repo", "path"], get_file, read=True,
          untrusted="a repository file", label=lambda a: f"Read {a.get('repo')}:{a.get('path')}"),
        T("github_search_code", "Search code across GitHub.", {"query": s("GitHub code search query")}, ["query"], search_code, read=True, untrusted="GitHub code search",
          label=lambda a: f"Search code: {a.get('query')}"),
        T("github_create_issue", "Open an issue. Requires approval.", {**rp, "title": s("Title"), "body": s("Body"), "labels": {"type": "array", "items": {"type": "string"}}},
          ["repo", "title"], create_issue, risk=R("write", lambda a: f"Open GitHub issue in {a.get('repo')}: {a.get('title')}", lambda a: str(a.get("repo"))),
          label=lambda a: "Open GitHub issue"),
        T("github_comment", "Comment on an issue/PR. Requires approval.", {**rp, "number": i("Number"), "body": s("Comment")}, ["repo", "number", "body"], comment,
          risk=R("write", lambda a: f"Comment on {a.get('repo')}#{a.get('number')}: {short(a.get('body', ''), 140)}", lambda a: str(a.get("repo"))), label=lambda a: "Comment on GitHub"),
        T("github_create_pr", "Open a pull request from an already-pushed branch. Requires approval.",
          {**rp, "title": s("Title"), "head": s("Head branch"), "base": s("Base branch"), "body": s("Description")}, ["repo", "title", "head", "base"], create_pr,
          risk=R("write", lambda a: f"Open PR in {a.get('repo')}: {a.get('title')} ({a.get('head')} -> {a.get('base')})", lambda a: str(a.get("repo"))), label=lambda a: "Open pull request"),
    ]


def check_http(url_fn: Callable[[Conn], tuple[str, dict]]) -> Callable[[Conn], str]:
    def check(c: Conn) -> str:
        import httpx
        url, headers = url_fn(c)
        r = httpx.get(url, headers=headers, timeout=20)
        if r.status_code >= 400:
            raise ConnectorError(f"HTTP {r.status_code}: {short(r.text, 160)}")
        return "Connection OK."
    return check


def _google_check(path: str) -> Callable[[Conn], str]:
    def check(c: Conn) -> str:
        import httpx
        r = httpx.get(path, headers=c.google_headers(), timeout=20)
        if r.status_code >= 400:
            raise ConnectorError(f"HTTP {r.status_code}: {short(r.text, 160)}")
        return "Signed in and reachable."
    return check


GOOGLE_FIELDS = [
    {"key": "client_id", "label": "Google OAuth client ID", "secret": False, "required": True, "placeholder": "xxxx.apps.googleusercontent.com"},
    {"key": "client_secret", "label": "Google OAuth client secret", "secret": True, "required": True},
]


def builtin_defs() -> list[PluginDef]:
    return [
        PluginDef("gmail", "Gmail", "Read, search, draft and (with approval) send email.", fields=GOOGLE_FIELDS,
                  oauth={"provider": "google", "scopes": ["https://www.googleapis.com/auth/gmail.modify"]}, build=build_gmail,
                  check=_google_check("https://gmail.googleapis.com/gmail/v1/users/me/profile"),
                  help="Create an OAuth client (type: Desktop app) in Google Cloud Console, enable the Gmail API, paste the client id/secret, then press Connect."),
        PluginDef("google_calendar", "Google Calendar", "Read your calendar, find free time, create events.", fields=GOOGLE_FIELDS,
                  oauth={"provider": "google", "scopes": ["https://www.googleapis.com/auth/calendar"]}, build=build_calendar,
                  check=_google_check("https://www.googleapis.com/calendar/v3/users/me/calendarList?maxResults=1"),
                  help="Uses the same Google OAuth client as Gmail if you leave the fields blank. Enable the Calendar API."),
        PluginDef("slack", "Slack", "Read channels and post messages (with approval).",
                  fields=[{"key": "bot_token", "label": "Bot token (xoxb-...)", "secret": True, "required": True},
                          {"key": "user_token", "label": "User token (xoxp-..., optional, for search)", "secret": True, "required": False}],
                  build=build_slack, check=check_http(lambda c: ("https://slack.com/api/auth.test", {"Authorization": f"Bearer {c.cfg('bot_token') or c.cfg('user_token')}"})),
                  help="Create a Slack app, add scopes channels:read, channels:history, groups:read, chat:write, and install it to your workspace."),
        PluginDef("notion", "Notion", "Search, read and write Notion pages.", fields=[{"key": "token", "label": "Internal integration token", "secret": True, "required": True}],
                  build=build_notion, check=check_http(lambda c: ("https://api.notion.com/v1/users/me", {"Authorization": f"Bearer {c.cfg('token')}", "Notion-Version": "2022-06-28"})),
                  help="Create an internal integration at notion.so/my-integrations and share your pages with it."),
        PluginDef("linear", "Linear", "Search, read, create and comment on issues.", fields=[{"key": "api_key", "label": "Personal API key", "secret": True, "required": True}],
                  build=build_linear, help="Create a personal API key in Linear > Settings > API."),
        PluginDef("jira", "Jira", "Search, read, create and comment on Jira Cloud issues.",
                  fields=[{"key": "base_url", "label": "Site URL", "secret": False, "required": True, "placeholder": "https://yourteam.atlassian.net"},
                          {"key": "email", "label": "Account email", "secret": False, "required": True},
                          {"key": "api_token", "label": "API token", "secret": True, "required": True}], build=build_jira,
                  check=check_http(lambda c: (c.cfg("base_url").rstrip("/") + "/rest/api/3/myself",
                                               {"Authorization": "Basic " + base64.b64encode(f"{c.cfg('email')}:{c.cfg('api_token')}".encode()).decode()})),
                  help="Create an API token at id.atlassian.com > Security > API tokens."),
        PluginDef("github", "GitHub", "Issues, pull requests, files and code search.",
                  fields=[{"key": "token", "label": "Personal access token", "secret": True, "required": True},
                          {"key": "api_url", "label": "API URL (GitHub Enterprise only)", "secret": False, "required": False, "placeholder": "https://api.github.com"}],
                  build=build_github, check=check_http(lambda c: ((c.cfg("api_url") or "https://api.github.com").rstrip("/") + "/user", {"Authorization": f"Bearer {c.cfg('token')}"})),
                  help="Create a fine-grained token with Issues, Pull requests and Contents access to the repos you want."),
    ]


# ----------------------------------------------------------------------------- generic REST + declarative plugins
_TOKEN_RE = re.compile(r"\{\{\s*(secret|config)\.(\w+)\s*\}\}|\{(\w+)\}")


def _subst(template: Any, args: dict, conn: Conn, in_url: bool = False) -> Any:
    """Fill {param} from the model's arguments and {{secret.x}}/{{config.x}} from stored config.

    One regex pass, so text supplied by the model can never expand into a secret reference.
    """
    if isinstance(template, dict):
        return {k: _subst(v, args, conn, in_url) for k, v in template.items()}
    if isinstance(template, list):
        return [_subst(v, args, conn, in_url) for v in template]
    if not isinstance(template, str):
        return template

    def repl(m: re.Match) -> str:
        if m.group(1):
            return conn.cfg(m.group(2))
        key = m.group(3)
        if key not in args:
            return m.group(0)
        v = str(args[key])
        return quote(v, safe="") if in_url else v
    return _TOKEN_RE.sub(repl, template)


def build_declarative(manifest: dict) -> Callable[[Conn], list[ToolSpec]]:
    def build(c: Conn) -> list[ToolSpec]:
        specs = []
        base_url = manifest.get("base_url", "")
        for t in manifest.get("tools", []):
            req = t.get("request", {})
            risk_kind = t.get("risk", "safe")

            def handler(ctx: ToolContext, args: dict, _req=req, _t=t) -> ToolResult:
                url = _subst(_req.get("url") or (base_url.rstrip("/") + "/" + _req.get("path", "").lstrip("/")), args, c, in_url=True)
                method = _req.get("method", "GET").upper()
                data = c.http(ctx, method, url, headers=_subst(_req.get("headers", {}), args, c), params=_subst(_req.get("query", {}), args, c) or None,
                              json_body=_subst(_req.get("body"), args, c))
                path = _t.get("response_path")
                if path and isinstance(data, (dict, list)):
                    for part in path.split("."):
                        data = data[int(part)] if isinstance(data, list) else data.get(part, data)
                return _res(data, manifest.get("name", manifest["id"]))

            risk = None
            if risk_kind != "safe":
                risk = R(risk_kind if risk_kind in ("send", "write", "delete", "submit", "purchase") else "write",
                         lambda a, _n=t["name"], _p=manifest["name"]: f"{_p}: {_n} {short(json.dumps(a), 200)}", lambda a, _n=t["name"]: _n)
            specs.append(ToolSpec(safe_name(f"{manifest['id']}_{t['name']}"), t.get("description", t["name"]), t.get("input_schema") or obj(), handler, risk=risk,
                                  label=lambda a, _n=t["name"], _p=manifest["name"]: f"{_p}: {_n}", read_only=risk_kind == "safe"))
        return specs
    return build


class PluginAPI:
    """Passed to python plugins' register(api)."""

    def __init__(self, conn: Conn):
        self._c = conn
        self.config = lambda key, default="": conn.cfg(key, default)

    def http(self, ctx: ToolContext, method: str, url: str, **kw: Any) -> Any:
        return self._c.http(ctx, method, url, **kw)


def build_python(path: Path, manifest: dict) -> Callable[[Conn], list[ToolSpec]]:
    def build(c: Conn) -> list[ToolSpec]:
        spec = importlib.util.spec_from_file_location(f"gbplugin_{manifest['id']}", path / manifest.get("entry", "plugin.py"))
        if not spec or not spec.loader:
            raise ConnectorError("Cannot load plugin entry point.")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        tools = mod.register(PluginAPI(c))
        out = []
        for t in tools:
            fn = t["handler"]
            risk = None
            if t.get("risk") and t["risk"] != "safe":
                risk = R(t["risk"] if t["risk"] in ("send", "write", "delete", "submit", "purchase", "command") else "write",
                         lambda a, _n=t["name"]: f"{manifest['name']}: {_n} {short(json.dumps(a, default=str), 200)}", lambda a, _n=t["name"]: _n)

            def handler(ctx: ToolContext, args: dict, _fn=fn) -> ToolResult:
                out_ = _fn(ctx, args) if len(inspect.signature(_fn).parameters) >= 2 else _fn(args)
                return out_ if isinstance(out_, ToolResult) else _res(out_, manifest["name"])
            out.append(ToolSpec(safe_name(f"{manifest['id']}_{t['name']}"), t.get("description", t["name"]), t.get("input_schema") or obj(), handler, risk=risk,
                                label=lambda a, _n=t["name"], _p=manifest["name"]: f"{_p}: {_n}", read_only=not risk))
        return out
    return build


def rest_def(cfg: dict) -> PluginDef:
    pid = "rest-" + cfg["id"]
    fields = []
    if cfg.get("auth") in ("bearer", "header", "basic"):
        fields.append({"key": "token", "label": {"bearer": "Bearer token", "header": "Header value", "basic": "Password"}[cfg["auth"]], "secret": True, "required": True})

    def build(c: Conn) -> list[ToolSpec]:
        base = cfg["base_url"].rstrip("/")

        def req(ctx: ToolContext, a: dict) -> Any:
            method = a.get("method", "GET").upper()
            if method not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
                raise ConnectorError("Unsupported method.")
            path = "/" + str(a.get("path", "")).lstrip("/")
            if path.startswith("//") or "://" in path:
                raise ConnectorError("Give a path on the configured API, not a full URL.")
            headers = dict(a.get("headers") or {})
            tok = c.cfg("token")
            if cfg.get("auth") == "bearer":
                headers["Authorization"] = f"Bearer {tok}"
            elif cfg.get("auth") == "header":
                headers[cfg.get("header_name") or "X-API-Key"] = tok
            elif cfg.get("auth") == "basic":
                headers["Authorization"] = "Basic " + base64.b64encode(f"{cfg.get('username', '')}:{tok}".encode()).decode()
            return _res(c.http(ctx, method, base + path, headers=headers, params=a.get("query"), json_body=a.get("body")), cfg["name"])

        def risk(ctx: ToolContext, a: dict) -> Risk | None:
            m = a.get("method", "GET").upper()
            if m in ("GET", "HEAD"):
                return None
            if not cfg.get("allow_writes", False):
                return Risk("write", f"{m} {cfg['name']}{a.get('path')}", {"pattern": f"{m} {a.get('path')}", "body": short(json.dumps(a.get("body")), 800)})
            return None

        return [ToolSpec(safe_name(f"rest_{cfg['id']}_request"), f"Call the {cfg['name']} REST API ({base}). {cfg.get('description', '')}",
                         obj({"method": s("GET, POST, PUT, PATCH or DELETE"), "path": s("Path starting with /"), "query": {"type": "object"}, "body": {"type": "object"},
                              "headers": {"type": "object"}}, ["method", "path"]), lambda ctx, a: req(ctx, a), risk=risk,
                         label=lambda a: f"{cfg['name']}: {a.get('method', 'GET')} {a.get('path', '')}")]
    return PluginDef(pid, f"REST: {cfg['name']}", cfg.get("description") or f"Generic REST connector for {cfg['base_url']}", kind="rest", fields=fields, build=build,
                     help="Calls a JSON REST API. Only GET is free; other methods ask for approval unless you enabled writes.")


# ----------------------------------------------------------------------------- manager
class PluginManager:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.db, self.settings, self.admin = engine.db, engine.settings, engine.admin
        self.google = GoogleOAuth()
        self.defs: dict[str, PluginDef] = {}
        self.reload()

    # -- registry -------------------------------------------------------------
    def reload(self) -> None:
        defs = {d.id: d for d in builtin_defs()}
        for cfg in self.settings.get("rest_connectors", []) or []:
            d = rest_def(cfg)
            defs[d.id] = d
        for folder in sorted(paths.plugins_dir().iterdir()) if paths.plugins_dir().exists() else []:
            mf = folder / "plugin.json"
            if not mf.is_file():
                continue
            try:
                m = json.loads(mf.read_text(encoding="utf-8"))
                d = self._def_from_manifest(m, folder)
                defs[d.id] = d
            except Exception as e:  # noqa: BLE001
                self.engine.log.warning("Bad plugin in %s: %s", folder, e)
        self.defs = defs

    def _def_from_manifest(self, m: dict, folder: Path) -> PluginDef:
        pid = re.sub(r"[^a-z0-9_-]", "-", str(m["id"]).lower())
        kind = "python" if m.get("entry") else "declarative"
        build = build_python(folder, {**m, "id": pid}) if kind == "python" else build_declarative({**m, "id": pid})
        return PluginDef(pid, m.get("name", pid), m.get("description", ""), kind=kind, version=m.get("version", "1.0.0"), fields=m.get("fields", []),
                         build=build, path=folder, source=(self.db.scalar("SELECT source FROM plugins WHERE id=?", (pid,), "local") or "local"),
                         skills=m.get("skills", []), help=m.get("help", ""))

    # -- config ----------------------------------------------------------------
    def config_value(self, pid: str, key: str) -> str:
        d = self.defs.get(pid)
        field_ = next((f for f in (d.fields if d else []) if f["key"] == key), None)
        if (field_ and field_.get("secret")) or key == "refresh_token":
            return secrets.get_secret(f"plugin:{pid}:{key}") or ""
        return str((self.settings.get(f"plugin_cfg.{pid}", {}) or {}).get(key, "") or (field_ or {}).get("default", ""))

    def set_config(self, pid: str, values: dict[str, str]) -> None:
        d = self.defs.get(pid)
        if not d:
            raise ValueError("Unknown plugin.")
        cfg = dict(self.settings.get(f"plugin_cfg.{pid}", {}) or {})
        for f in d.fields:
            k = f["key"]
            if k not in values:
                continue
            v = str(values[k]).strip()
            if f.get("secret"):
                if v:
                    secrets.set_secret(f"plugin:{pid}:{k}", v)
            else:
                cfg[k] = v
        self.settings.set(f"plugin_cfg.{pid}", cfg)

    def clear_secrets(self, pid: str) -> None:
        d = self.defs.get(pid)
        for f in (d.fields if d else []):
            if f.get("secret"):
                secrets.delete_secret(f"plugin:{pid}:{f['key']}")
        secrets.delete_secret(f"plugin:{pid}:refresh_token")

    def configured(self, pid: str) -> bool:
        d = self.defs.get(pid)
        if not d:
            return False
        for f in d.fields:
            if f.get("required") and not self.config_value(pid, f["key"]):
                if d.id == "google_calendar" and f["key"] in ("client_id", "client_secret") and self.config_value("gmail", f["key"]):
                    continue
                return False
        if d.oauth and not secrets.get_secret(f"plugin:{pid}:refresh_token"):
            return False
        return True

    def info(self, pid: str) -> dict:
        d = self.defs[pid]
        specs = []
        try:
            specs = d.build(Conn(self, d)) if d.build else []
        except Exception:
            specs = []
        return {"id": d.id, "name": d.name, "description": d.description, "kind": d.kind, "version": d.version, "help": d.help,
                "fields": [{**f, "set": bool(self.config_value(d.id, f["key"])), "value": ("" if f.get("secret") else self.config_value(d.id, f["key"]))} for f in d.fields],
                "oauth": bool(d.oauth), "connected": bool(not d.oauth or secrets.get_secret(f"plugin:{d.id}:refresh_token")),
                "configured": self.configured(d.id), "allowed": plugin_allowed(self.admin, d.id), "installed": d.kind != "builtin" and d.kind != "rest",
                "source": d.source, "tools": [{"name": t.name, "description": t.description, "read_only": t.read_only or t.risk is None} for t in specs],
                "removable": d.kind in ("declarative", "python", "rest")}

    def list(self) -> list[dict]:
        return [self.info(pid) for pid in self.defs]

    # -- tools for a Bot ---------------------------------------------------------
    def tool_specs(self, granted: set[str]) -> list[ToolSpec]:
        out: list[ToolSpec] = []
        for pid, d in self.defs.items():
            if f"plugin:{pid}" not in granted or not plugin_allowed(self.admin, pid) or not d.build or not self.configured(pid):
                continue
            try:
                for t in d.build(Conn(self, d)):
                    t.group = f"plugin:{pid}"
                    out.append(t)
            except Exception as e:  # noqa: BLE001
                self.engine.log.warning("Plugin %s failed to build tools: %s", pid, e)
        return out

    def test(self, pid: str) -> str:
        d = self.defs.get(pid)
        if not d:
            raise ValueError("Unknown plugin.")
        if not self.configured(pid):
            raise ConnectorError("Not fully configured yet." + (" Press Connect to sign in." if d.oauth else ""))
        if not d.check:
            return "Configured (this plugin has no connection test)."
        return d.check(Conn(self, d))

    # -- OAuth ----------------------------------------------------------------------
    def begin_oauth(self, pid: str, redirect_uri: str) -> str:
        d = self.defs.get(pid)
        if not d or not d.oauth:
            raise ValueError("This plugin does not use sign-in.")
        c = Conn(self, d)
        cid = c.cfg("client_id") or (self.config_value("gmail", "client_id") if pid != "gmail" else "")
        if not cid:
            raise ConnectorError("Enter the OAuth client ID and secret first.")
        return self.google.auth_url(pid, cid, d.oauth["scopes"], redirect_uri)

    def finish_oauth(self, state: str, code: str) -> str:
        p = self.google.pending.get(state)
        if not p:
            raise ConnectorError("Unknown or expired sign-in request.")
        c = Conn(self, self.defs[p["plugin"]])
        cid = c.cfg("client_id") or self.config_value("gmail", "client_id")
        csec = c.cfg("client_secret") or self.config_value("gmail", "client_secret")
        return self.google.exchange(state, code, cid, csec)

    # -- install / uninstall -----------------------------------------------------------
    def catalog(self) -> list[dict]:
        entries: list[dict] = []
        cat = paths.resource_root() / "plugins" / "catalog.json"
        if cat.is_file():
            try:
                entries += json.loads(cat.read_text(encoding="utf-8")).get("plugins", [])
            except ValueError:
                pass
        import httpx
        for url in self.settings.get("catalog_urls", []) or []:
            try:
                ok, _ = self.engine.net.check(None, url)
                if ok:
                    entries += httpx.get(url, timeout=10).json().get("plugins", [])
            except Exception:
                continue
        installed = set(self.defs)
        for e in entries:
            e["installed"] = e.get("id") in installed
        return entries

    def install(self, source: str) -> dict:
        if not self.admin.plugin_install_allowed():
            raise ValueError("Your administrator has disabled plugin installation.")
        source = source.strip()
        tmp = paths.data_dir() / "tmp-plugin-install"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            if source.startswith("bundled:"):
                src = (paths.resource_root() / source.split(":", 1)[1]).resolve()
                if not src.is_relative_to((paths.resource_root() / "plugins").resolve()):
                    raise ValueError("Invalid bundled source.")
                shutil.copytree(src, tmp)
            elif re.match(r"^(https?://|git@|ssh://)", source):
                if not shutil.which("git"):
                    raise ValueError("git is not installed or not on PATH.")
                r = subprocess.run(["git", "clone", "--depth", "1", source, str(tmp)], capture_output=True, text=True, timeout=180,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if r.returncode != 0:
                    raise ValueError("git clone failed: " + short(r.stderr, 300))
                shutil.rmtree(tmp / ".git", ignore_errors=True)
            else:
                src = Path(source).expanduser()
                if not src.is_dir():
                    raise ValueError(f"{source} is not a folder, bundled source or git URL.")
                shutil.copytree(src, tmp)
            mf = tmp / "plugin.json"
            if not mf.is_file():
                raise ValueError("plugin.json not found at the top of the plugin folder.")
            m = json.loads(mf.read_text(encoding="utf-8"))
            pid = re.sub(r"[^a-z0-9_-]", "-", str(m.get("id", "")).lower())
            if not pid or pid in {d.id for d in builtin_defs()}:
                raise ValueError("Plugin needs a unique 'id' that does not clash with a built-in connector.")
            if not plugin_allowed(self.admin, pid):
                raise ValueError(f"Your administrator has not allowed the plugin '{pid}'.")
            dest = paths.plugins_dir() / pid
            shutil.rmtree(dest, ignore_errors=True)
            shutil.move(str(tmp), str(dest))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.db.execute("INSERT INTO plugins(id,source,path,enabled,installed_at,version) VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET source=excluded.source, path=excluded.path, version=excluded.version",
                        (pid, source, str(dest), 1, time.time(), m.get("version", "")))
        for sk in m.get("skills", []):  # ship skills along with the plugin (as drafts to review)
            f = dest / sk
            if f.is_file():
                name = f"{pid}-{f.stem}"
                self.engine.skills.save(name, f.read_text(encoding="utf-8"), status="draft")
        self.reload()
        self.engine.events.publish("plugins", change="installed", plugin_id=pid)
        return self.info(pid)

    def uninstall(self, pid: str) -> None:
        d = self.defs.get(pid)
        if not d:
            return
        if d.kind == "rest":
            self.settings.set("rest_connectors", [c for c in self.settings.get("rest_connectors", []) if "rest-" + c["id"] != pid])
        elif d.path and d.path.is_relative_to(paths.plugins_dir()):
            shutil.rmtree(d.path, ignore_errors=True)
            self.db.execute("DELETE FROM plugins WHERE id=?", (pid,))
        self.clear_secrets(pid)
        self.reload()
        self.engine.events.publish("plugins", change="removed", plugin_id=pid)

    def add_rest(self, cfg: dict) -> dict:
        if not cfg.get("name") or not cfg.get("base_url", "").startswith(("http://", "https://")):
            raise ValueError("A REST connector needs a name and an http(s) base URL.")
        cfg = dict(cfg)
        cfg["id"] = cfg.get("id") or re.sub(r"[^a-z0-9]+", "-", cfg["name"].lower()).strip("-") or pysecrets.token_hex(3)
        lst = [c for c in self.settings.get("rest_connectors", []) if c["id"] != cfg["id"]] + [cfg]
        self.settings.set("rest_connectors", lst)
        self.reload()
        return self.info("rest-" + cfg["id"])
