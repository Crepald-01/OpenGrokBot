"""Slash commands typed into a chat ("/status", "/model gpt-5", "/approve"...).

A message that starts with /<command> is handled by the service instead of being sent to the model, so it works
the same in the desktop app and on the phone. The answer is posted into the thread as a "notice" (shown in the
chat, never sent to the model). Type "//text" to send a message that really starts with a slash.

Safety: commands only run from a message the user typed in the chat. Output and Bot messages are never parsed.
"""
from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from . import VERSION

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

CMD_RE = re.compile(r"^/([A-Za-z][\w-]*)(?:[ \t]+(.*))?$", re.S)

# approvals that are never decided in bulk: the user must look at each one
NEVER_BULK = {"purchase", "login", "access", "outside_workspace", "takeover", "question"}


@dataclass
class Ctx:
    eng: "Engine"
    thread: dict
    bot: dict | None
    args: str


@dataclass
class Command:
    name: str
    summary: str
    usage: str
    handler: Callable[[Ctx], "dict | str"]
    bot_only: bool = False
    aliases: tuple[str, ...] = ()
    group: str = "Chat"


COMMANDS: dict[str, Command] = {}
ALIASES: dict[str, str] = {}


def command(name: str, summary: str, usage: str = "", bot_only: bool = False, aliases: tuple[str, ...] = (), group: str = "Chat"):
    def deco(fn: Callable[[Ctx], "dict | str"]):
        COMMANDS[name] = Command(name, summary, usage or f"/{name}", fn, bot_only, aliases, group)
        for a in aliases:
            ALIASES[a] = name
        return fn
    return deco


# ------------------------------------------------------------------------------------------------ helpers
def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _when(ts: float) -> str:
    if not ts:
        return "-"
    d = datetime.fromtimestamp(ts)
    return d.strftime("%a %H:%M") if abs(ts - time.time()) < 6 * 86400 else d.strftime("%d %b %H:%M")


def _tokens(n: int) -> str:
    n = int(n or 0)
    return f"{n/1_000_000:.1f}M" if n >= 1_000_000 else f"{n/1000:.1f}k" if n >= 1000 else str(n)


def _pending(c: Ctx) -> list[dict]:
    """Pending approvals for this Bot (or this group thread), oldest first, so numbers stay stable."""
    rows = c.eng.approvals.list("pending", c.bot["id"] if c.bot else None, 100)
    if not c.bot:
        rows = [a for a in rows if a["thread_id"] == c.thread["id"]]
    return sorted(rows, key=lambda a: a["created_at"])


def _need_bot(c: Ctx) -> dict | None:
    return None if c.bot else {"level": "warn", "text": "This command works in a Bot's own chat, not in a group chat."}


# ------------------------------------------------------------------------------------------------ commands
@command("help", "List the commands, or explain one", "/help [command]", aliases=("?", "commands"))
def _help(c: Ctx):
    arg = c.args.strip().lstrip("/").lower()
    if arg:
        cmd = COMMANDS.get(ALIASES.get(arg, arg))
        if not cmd:
            return {"level": "warn", "text": f"No command called /{arg}. Type /help for the list."}
        al = f"\n\nAlso: {', '.join('/' + a for a in cmd.aliases)}" if cmd.aliases else ""
        return f"`{cmd.usage}`\n\n{cmd.summary}{al}"
    groups: dict[str, list[Command]] = {}
    for cmd in COMMANDS.values():
        groups.setdefault(cmd.group, []).append(cmd)
    out = ["Type `/` to see the commands as you write. `//text` sends a message that starts with a slash.\n"]
    for g, cmds in groups.items():
        out.append(f"**{g}**")
        out.extend(f"- `{x.usage}`: {x.summary}" for x in cmds)
        out.append("")
    return "\n".join(out).strip()


@command("status", "Show this Bot's state, model, approvals and usage", "/status", bot_only=True)
def _status(c: Ctx):
    b = c.bot
    runs = [r for r in c.eng.turns.active() if r["bot_id"] == b["id"]]
    state = "paused" if b["paused"] else (runs[0]["status"].replace("_", " ") if runs else "idle")
    pend = len(_pending(c))
    n = len(c.eng.threads.rows(c.thread["id"]))
    mode = b["effective_approval_mode"]
    return "\n".join([
        f"**{b['emoji']} {b['name']}** · {state}",
        f"- Model: `{b['model'] or 'provider default'}` via `{b['profile'] or 'default provider'}`",
        f"- Approvals: {'Auto Review' if mode == 'auto_review' else 'Ask me'} · {pend} waiting",
        f"- Step limit: {b['step_limit']} · Proactive: {b['proactive']}",
        f"- This thread: “{_clip(c.thread['title'], 40)}”, {n} messages",
        f"- Tokens this week (all Bots): {_tokens(c.eng.usage.week_total())}",
    ])


@command("stop", "Stop what this Bot is doing right now", "/stop", aliases=("cancel",))
def _stop(c: Ctx):
    n = c.eng.turns.stop_thread(c.thread["id"])
    return "Stopped." if n else "Nothing was running in this chat."


@command("new", "Start a new thread with this Bot", "/new [title]", bot_only=True)
def _new(c: Ctx):
    t = c.eng.threads.create(c.bot["id"], c.args.strip()[:60] or "New thread")
    return {"text": f"Started a new thread: “{t['title']}”.", "switch_thread": t["id"]}


@command("rename", "Rename this thread", "/rename <title>", bot_only=True)
def _rename(c: Ctx):
    t = c.args.strip()
    if not t:
        return {"level": "warn", "text": "Usage: `/rename <title>`"}
    c.eng.threads.rename(c.thread["id"], t)
    return f"Renamed to “{t[:80]}”."


@command("model", "Show or change this Bot's model", "/model [name | default]", bot_only=True, group="Bot")
def _model(c: Ctx):
    a = c.args.strip()
    if not a:
        return f"Model: `{c.bot['model'] or 'provider default'}` via `{c.bot['profile'] or 'default provider'}`.\n\nChange it with `/model <name>`, or `/model default` to follow the provider's default. Pick from the detected list in *Edit Bot*."
    if a.lower() in ("default", "reset", "none"):
        c.eng.bots.update(c.bot["id"], model="")
        return "Now using the provider's default model."
    if len(a) > 120 or "\n" in a:
        return {"level": "warn", "text": "That does not look like a model name."}
    c.eng.bots.update(c.bot["id"], model=a)
    return f"{c.bot['name']} will use `{a}` from its next message."


@command("mode", "Choose how this Bot asks for approval", "/mode ask | auto", bot_only=True, group="Bot")
def _mode(c: Ctx):
    a = c.args.strip().lower()
    if c.eng.admin.approval().get("locked"):
        return {"level": "warn", "text": "The approval mode is managed by your organization and cannot be changed here."}
    if a not in ("ask", "auto", "auto_review", "auto-review"):
        cur = "Auto Review" if c.bot["effective_approval_mode"] == "auto_review" else "Ask me"
        return f"Current mode: **{cur}**.\n\n`/mode ask` asks you for every consequential action. `/mode auto` lets a reviewer model approve clearly low-risk ones; purchases, logins and new connectors always ask."
    c.eng.bots.update(c.bot["id"], approval_mode="ask" if a == "ask" else "auto_review")
    return "Now asking for every consequential action." if a == "ask" else "Auto Review is on for this Bot."


@command("pause", "Pause this Bot (it stops picking up work)", "/pause", bot_only=True, group="Bot")
def _pause(c: Ctx):
    c.eng.turns.stop_bot(c.bot["id"])
    c.eng.bots.update(c.bot["id"], paused=True)
    return f"{c.bot['name']} is paused. `/resume` to continue."


@command("resume", "Resume a paused Bot", "/resume", bot_only=True, group="Bot")
def _resume(c: Ctx):
    c.eng.bots.update(c.bot["id"], paused=False)
    return f"{c.bot['name']} is active again."


@command("approvals", "List what is waiting for your approval", "/approvals", group="Approvals", aliases=("pending",))
def _approvals(c: Ctx):
    rows = _pending(c)
    if not rows:
        return "Nothing is waiting for approval."
    lines = [f"{i}. **{a['title']}**: {_clip(a['summary'], 110)}" for i, a in enumerate(rows, 1)]
    return "\n".join(lines) + "\n\n`/approve <number>` or `/deny <number>`. `/approve all` skips purchases, logins, new connectors and anything outside the workspace."


def _decide(c: Ctx, approve: bool):
    rows = _pending(c)
    a = c.args.strip().lower()
    if not rows:
        return "Nothing is waiting for approval."
    if a in ("all", "*"):
        done, skipped = 0, []
        for r in rows:
            if approve and r["category"] in NEVER_BULK:
                skipped.append(r)
                continue
            c.eng.approvals.decide(r["id"], approve, note="via /" + ("approve" if approve else "deny"))
            done += 1
        msg = f"{'Approved' if approve else 'Denied'} {done}."
        if skipped:
            msg += f" Left {len(skipped)} for you to look at one by one: " + ", ".join(f"**{r['title']}**" for r in skipped) + "."
        return msg
    if not a:
        if len(rows) == 1:
            a = "1"
        else:
            return {"level": "warn", "text": f"{len(rows)} actions are waiting. Say which: `/{'approve' if approve else 'deny'} <number>` (see `/approvals`) or `all`."}
    if not a.isdigit() or not 1 <= int(a) <= len(rows):
        return {"level": "warn", "text": f"Pick a number from 1 to {len(rows)} (see `/approvals`)."}
    r = rows[int(a) - 1]
    if approve and r["category"] == "question":
        return {"level": "warn", "text": "That one is a question for you. Answer it on its card."}
    c.eng.approvals.decide(r["id"], approve, note="via /" + ("approve" if approve else "deny"))
    return f"{'Approved' if approve else 'Denied'}: {_clip(r['summary'], 100)}"


@command("approve", "Approve a waiting action", "/approve [number | all]", group="Approvals", aliases=("yes",))
def _approve(c: Ctx):
    return _decide(c, True)


@command("deny", "Deny a waiting action", "/deny [number | all]", group="Approvals", aliases=("no",))
def _deny(c: Ctx):
    return _decide(c, False)


@command("memory", "Show what this Bot remembers (optionally filtered)", "/memory [search]", bot_only=True, group="Memory", aliases=("mem",))
def _memory(c: Ctx):
    q = c.args.strip()
    rows = c.eng.memory.search(c.bot["id"], q) if q else c.eng.memory.list(c.bot["id"])
    if not rows:
        return "No memories match." if q else "Nothing remembered yet. Add one with `/remember <text>`."
    lines = [f"- `#{m['id']}` {'📌 ' if m.get('pinned') else ''}*{m['kind']}*: {_clip(m['text'], 160)}" for m in rows[:20]]
    more = f"\n\n…and {len(rows) - 20} more (open *Edit Bot > Memory*)." if len(rows) > 20 else ""
    return "\n".join(lines) + more + "\n\n`/forget <#id>` removes one."


@command("remember", "Add something to this Bot's memory", "/remember <text>", bot_only=True, group="Memory")
def _remember(c: Ctx):
    t = c.args.strip()
    if not t:
        return {"level": "warn", "text": "Usage: `/remember <what to remember>`"}
    mid = c.eng.memory.add(c.bot["id"], "preference", t, source="user", pinned=True)
    return f"Remembered (`#{mid}`, pinned): {_clip(t, 160)}"


@command("forget", "Remove a memory by its number", "/forget <#id>", bot_only=True, group="Memory")
def _forget(c: Ctx):
    a = c.args.strip().lstrip("#")
    if not a.isdigit():
        return {"level": "warn", "text": "Usage: `/forget <#id>` (see `/memory`)"}
    return "Forgotten." if c.eng.memory.forget(c.bot["id"], int(a)) else {"level": "warn", "text": f"No memory #{a} for this Bot."}


@command("skills", "List the skills this Bot can use", "/skills", bot_only=True, group="Skills")
def _skills(c: Ctx):
    rows = c.eng.skills.list(status="active", bot_name=c.bot["name"])
    if not rows:
        return "No active skills yet. Add or activate some on the *Skills* page."
    return "\n".join(f"- `{s['name']}`: {_clip(s['description'], 120)}" for s in rows) + "\n\nRun one with `/skill <name>`."


@command("skill", "Run a skill now", "/skill <name> [extra instructions]", bot_only=True, group="Skills")
def _skill(c: Ctx):
    parts = c.args.strip().split(None, 1)
    if not parts:
        return {"level": "warn", "text": "Usage: `/skill <name> [extra instructions]`. See `/skills`."}
    names = [s["name"] for s in c.eng.skills.list(status="active", bot_name=c.bot["name"])]
    name = next((n for n in names if n.lower() == parts[0].lower()), None)
    if not name:
        close = difflib.get_close_matches(parts[0], names, 1, 0.5)
        return {"level": "warn", "text": f"No active skill called “{parts[0]}”." + (f" Did you mean `{close[0]}`?" if close else " See `/skills`.")}
    extra = f"\n\nExtra instructions from me: {parts[1]}" if len(parts) > 1 else ""
    return {"send": f"Please run the skill “{name}” now. Read it with skill_read and follow it step by step.{extra}"}


@command("routines", "List this Bot's scheduled routines", "/routines", bot_only=True, group="Skills")
def _routines(c: Ctx):
    rows = c.eng.routines.list(c.bot["id"])
    if not rows:
        return "No routines yet. Create one on the *Routines* page."
    return "\n".join(f"- **{r['name']}** `{r['cron']}` · {'on' if r['enabled'] else 'off'} · next {_when(r['next_run_at']) if r['enabled'] else '-'}"
                     f"{' · running now' if r['running'] else ''}" for r in rows)


@command("usage", "Show token usage this week", "/usage", group="Info", aliases=("tokens",))
def _usage(c: Ctx):
    s = c.eng.usage.summary()
    lim = f" of {_tokens(s['limit'])}" if s["limit"] else ""
    lines = [f"**This week:** {_tokens(s['total'])}{lim} tokens ({_tokens(s['input_tokens'])} in, {_tokens(s['output_tokens'])} out). Resets {_when(s['resets_at'])}."]
    for r in s["per_bot"][:5]:
        lines.append(f"- {r['emoji']} {r['name']}: {_tokens((r['input_tokens'] or 0) + (r['output_tokens'] or 0))} · {r['turns']} turns")
    return "\n".join(lines)


@command("version", "Show the app version", "/version", group="Info", aliases=("about",))
def _version(c: Ctx):
    return f"OpenGrokBot **{VERSION}** · up {int((time.time() - c.eng.started) // 60)} min · data in `{c.eng.status()['data_dir']}`"


# ------------------------------------------------------------------------------------------------ dispatch
def listing() -> list[dict]:
    return [{"name": x.name, "usage": x.usage, "summary": x.summary, "bot_only": x.bot_only, "aliases": list(x.aliases), "group": x.group}
            for x in COMMANDS.values()]


def parse(text: str) -> tuple[str, str] | None:
    """('status', 'args') when the message is a slash command, else None."""
    t = (text or "").strip()
    if not t.startswith("/") or t.startswith("//"):
        return None
    m = CMD_RE.match(t)
    if not m:
        return None   # e.g. "/usr/bin/x" or "/ not a command": an ordinary message
    return m.group(1).lower(), (m.group(2) or "").strip()


def run(eng: "Engine", thread_id: str, text: str) -> dict | None:
    """Run a command if `text` is one. Returns None for ordinary messages, else
    {"name", "handled": True, "switch_thread"?: id, "send"?: text to send to the Bot as a normal message}."""
    p = parse(text)
    if p is None:
        return None
    name, args = p
    name = ALIASES.get(name, name)
    th = eng.threads.get(thread_id)
    if th is None:
        return None
    bot = eng.bots.get(th["bot_id"]) if th.get("bot_id") else None
    head = f"**/{name}**" + (f" {_clip(args, 60)}" if args else "")

    def say(body: "dict | str") -> None:
        d = {"text": body} if isinstance(body, str) else body
        lvl = d.get("level") or "cmd"
        eng.threads.notice(thread_id, f"{head}\n\n{d.get('text', '')}".strip() if lvl == "cmd" else d.get("text", ""), lvl)

    cmd = COMMANDS.get(name)
    if cmd is None:
        close = difflib.get_close_matches(name, list(COMMANDS) + list(ALIASES), 3, 0.5)
        say({"level": "warn", "text": f"Unknown command /{name}." + (f" Did you mean {', '.join('/' + x for x in close)}?" if close else "") + f" Type /help, or //{name} to send it as plain text."})
        return {"name": name, "handled": True}
    if cmd.bot_only and not bot:
        say({"level": "warn", "text": f"/{name} works in a Bot's own chat, not in a group chat."})
        return {"name": name, "handled": True}
    try:
        res = cmd.handler(Ctx(eng, th, bot, args))
    except Exception as e:  # noqa: BLE001
        say({"level": "error", "text": f"/{name} failed: {e}"})
        return {"name": name, "handled": True}
    d = {"text": res} if isinstance(res, str) else dict(res)
    if d.get("send"):
        return {"name": name, "handled": False, "send": d["send"]}
    if d.get("text"):
        say(d)
    out = {"name": name, "handled": True}
    if d.get("switch_thread"):
        out["switch_thread"] = d["switch_thread"]
    return out
