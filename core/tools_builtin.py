"""Built-in tools every Bot gets: computer use, files, terminal, memory, skills, routines and teamwork."""
from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .browser import BrowserError, Screen
from .computer import ComputerError
from .netpolicy import host_of
from .tooling import Risk, ToolContext, ToolResult, ToolSpec, b, i, obj, s, short

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

PURCHASE_RE = re.compile(r"\b(buy( now)?|purchase|place (your |the |my )?order|pay( now)?|checkout|check out|subscribe|confirm (order|purchase|payment)|complete (order|purchase)|add (a )?payment|donate|upgrade plan)\b", re.I)
DELETE_RE = re.compile(r"\b(delete|remove|erase|permanently|close account|cancel (my )?(subscription|account|order|booking)|unsubscribe|deactivate|empty (trash|bin))\b", re.I)
SEND_RE = re.compile(r"\b(send|post|publish|reply|tweet|share|submit|apply|confirm|book|reserve|register|sign ?up|create account|schedule|invite|accept|approve|save( changes)?|update|upload|finish|continue to payment)\b", re.I)
LOGIN_RE = re.compile(r"(log ?in|sign ?in|password|2fa|two-factor|verification code|captcha|authenticate|session expired)", re.I)


def _fmt_elements(elements: list[dict]) -> str:
    out = []
    for e in elements:
        bits = [f"[{e['id']}] {e['tag']}" + (f"({e['type']})" if e.get("type") else (f"(role={e['role']})" if e.get("role") else ""))]
        if e.get("label"):
            bits.append(f"\"{e['label']}\"")
        if e.get("password"):
            bits.append("(password field: only the user may fill it)")
        if e.get("value"):
            bits.append(f"value=\"{e['value']}\"")
        if e.get("checked"):
            bits.append("checked")
        if e.get("href"):
            bits.append(f"-> {e['href']}")
        out.append(" ".join(bits))
    return "\n".join(out) or "(no interactive elements in view)"


def builtin_tools(eng: "Engine") -> list[ToolSpec]:
    comp = eng.computer
    br = comp.browser
    T: list[ToolSpec] = []

    def add(name, desc, props, required, handler, *, risk=None, label=None, group="core", read_only=False, screen=False):
        T.append(ToolSpec(name, desc, obj(props, required), handler, risk=risk, label=label or (lambda a, n=name: n.replace("_", " ")),
                          group=group, read_only=read_only, needs_screen=screen))

    # ------------------------------------------------------------------ talking to the user
    def notify_user(ctx: ToolContext, a: dict) -> ToolResult:
        eng.notify("bot_message", ctx.bot, ctx.thread_id, f"{ctx.bot['name']}", short(a["message"], 200), urgent=bool(a.get("urgent")))
        return ToolResult("Notification sent to the user's devices.")

    add("notify_user", "Send the user a short push notification (desktop toast and phone). Use sparingly: only when something is done that they are waiting for, "
        "or when you are blocked and they are probably away. Normal progress goes in your chat messages.",
        {"message": s("Short notification text"), "urgent": b("True only if it needs attention soon")}, ["message"], notify_user,
        label=lambda a: "Notify you")

    def ask_user(ctx: ToolContext, a: dict) -> ToolResult:
        d = eng.approvals.ask(ctx, a["question"], a.get("options") or [])
        if not d.approved and not d.answer:
            return ToolResult("The user did not answer (" + d.reason + "). Proceed with your best judgment if safe, or report that you are blocked.")
        return ToolResult(f"User answered: {d.answer or '(approved)'}")

    add("ask_user", "Ask the user a question and wait for the answer. Use only for judgment calls you cannot make yourself or missing info you cannot look up. "
        "Offer options when the answer is a choice.", {"question": s("The question"), "options": {"type": "array", "items": {"type": "string"}, "description": "Optional quick-reply choices"}},
        ["question"], ask_user, label=lambda a: f"Ask: {short(a.get('question', ''), 60)}")

    def request_takeover(ctx: ToolContext, a: dict) -> ToolResult:
        scr = br.screen(ctx.bot["id"])
        if a.get("url"):
            try:
                br.call(scr.goto, a["url"], timeout=60)
            except Exception:
                pass
        try:
            url = br.call(comp._current_url, ctx.bot["id"], timeout=20)
        except Exception:
            url = ""
        host = host_of(url)
        reason = a["reason"]
        is_login = bool(LOGIN_RE.search(reason)) and host and not comp.known_login(host)
        comp.takeover_begin(ctx.bot["id"], reason)
        try:
            d = eng.approvals._ask_user(ctx, "login" if is_login else "takeover", "request_takeover",
                                        f"{ctx.bot['name']} needs you at its browser: {reason}",
                                        {"url": url, "host": host, "new_service": bool(is_login), "pattern": host}, a)
        finally:
            comp.takeover_end(ctx.bot["id"])
        try:
            url2 = br.call(comp._current_url, ctx.bot["id"], timeout=20)
            if d.approved:
                comp.remember_login(host_of(url2))
        except Exception:
            pass
        if not d.approved:
            return ToolResult("The user could not or would not do that step. Do not retry it; report that this step is blocked and continue with anything else you can.")
        return ToolResult("The user finished the step and handed the browser back. Take a fresh browser_screenshot or browser_snapshot to see where things stand, then continue.")

    add("request_takeover", "Hand a step to the user in the shared browser: logins, passwords, 2FA codes, CAPTCHAs, payment entry, or any site that blocks automation. "
        "Never try to work around these. This pauses until the user finishes and hands the browser back.",
        {"reason": s("What the user needs to do, e.g. 'Log in to Xero and complete 2FA'"), "url": s("Optional page to open first")}, ["reason"], request_takeover,
        label=lambda a: f"Needs you: {short(a.get('reason', ''), 60)}")

    def request_access(ctx: ToolContext, a: dict) -> ToolResult:
        res = a["resource"].strip()
        key = res if res.startswith(("plugin:", "mcp:")) else (f"mcp:{res[4:]}" if res.startswith("mcp_") else f"plugin:{res}")
        kind, _, name = key.partition(":")
        if key in ctx.bot["grants"]:
            return ToolResult(f"You already have access to {name}.")
        if kind == "plugin":
            if name not in eng.plugins.defs:
                return ToolResult(f"No connector named '{name}'. Available: {', '.join(eng.plugins.defs)}.", is_error=True)
            from .settings import plugin_allowed
            if not plugin_allowed(eng.admin, name):
                return ToolResult(f"'{name}' is blocked by your organization's policy.", is_error=True)
            title, configured = eng.plugins.defs[name].name, eng.plugins.configured(name)
        else:
            conn = next((c for c in eng.mcp.conns.values() if c.cfg["name"] == name), None)
            if not conn:
                return ToolResult(f"No MCP server named '{name}'. Available: {', '.join(c.cfg['name'] for c in eng.mcp.conns.values()) or 'none'}.", is_error=True)
            title, configured = f"MCP server {name}", conn.status == "connected"
        d = eng.approvals.request(ctx, Risk("access", f"Give {ctx.bot['name']} access to {title}. Reason: {a.get('reason', '')}",
                                            {"resource": key, "configured": configured, "pattern": key}, never_auto=True), "request_access", a)
        if not d.approved:
            return ToolResult(f"Access to {title} was not granted. {d.reason}")
        eng.bots.grant(ctx.bot["id"], key)
        ctx.bot["grants"] = eng.bots.get(ctx.bot["id"])["grants"]  # type: ignore[index]
        if not configured:
            return ToolResult(f"Access to {title} is granted, but it has no credentials yet. Tell the user to open Plugins and connect it; its tools appear once it is connected.")
        return ToolResult(f"Access to {title} granted. Its tools are available from your next step.")

    add("request_access", "Ask the user to grant you a connector/plugin (e.g. gmail, slack, github, notion, linear, jira, google_calendar) or an MCP server (mcp:<name>). "
        "Check the 'Connectors you can request' list in your instructions.", {"resource": s("Connector id, e.g. 'gmail' or 'mcp:filesystem'"), "reason": s("Why you need it")},
        ["resource", "reason"], request_access, label=lambda a: f"Request access: {a.get('resource')}")

    # ------------------------------------------------------------------ memory
    def memory_save(ctx: ToolContext, a: dict) -> ToolResult:
        mid = eng.memory.add(ctx.bot["id"], a.get("kind", "fact"), a["text"], source=ctx.thread_id, verify=bool(a.get("verify_before_use")))
        return ToolResult(f"Saved to your memory (id {mid}).")

    add("memory_save", "Save something worth remembering across sessions: the user's stable preferences, role context, their writing voice, edge cases, or facts. "
        "One short, specific sentence each. Do not save secrets or one-off details.",
        {"kind": s("preference | role | voice | edge_case | fact | work_summary"), "text": s("What to remember"),
         "verify_before_use": b("True for facts that change (prices, statuses, who owns what) so you re-check the source before relying on them")}, ["kind", "text"],
        memory_save, label=lambda a: f"Remember: {short(a.get('text', ''), 60)}")

    def memory_search(ctx: ToolContext, a: dict) -> ToolResult:
        rows = eng.memory.search(ctx.bot["id"], a["query"])
        return ToolResult("\n".join(f"[{r['kind']}#{r['id']}] {r['text']}" for r in rows) or "Nothing in memory matches.")

    add("memory_search", "Search your own memory.", {"query": s("Keywords")}, ["query"], memory_search, read_only=True, label=lambda a: f"Recall: {a.get('query')}")

    def memory_forget(ctx: ToolContext, a: dict) -> ToolResult:
        return ToolResult("Forgotten." if eng.memory.forget(ctx.bot["id"], int(a["id"])) else "No such memory.")

    add("memory_forget", "Delete a memory that is wrong or outdated.", {"id": i("Memory id")}, ["id"], memory_forget, label=lambda a: "Forget a memory")

    def recall(ctx: ToolContext, a: dict) -> ToolResult:
        rows = eng.threads.search(ctx.bot["id"], a["query"])
        return ToolResult("\n\n".join(f"[{r['thread']}] {r['author']}: {r['text']}" for r in rows) or "No earlier conversation mentions that.")

    add("recall_conversations", "Search your earlier conversations (all your threads and group chats) to resume previous work.", {"query": s("Text to look for")}, ["query"],
        recall, read_only=True, label=lambda a: f"Search past chats: {a.get('query')}")

    # ------------------------------------------------------------------ shared project notes
    def project_list(ctx: ToolContext, a: dict) -> ToolResult:
        rows = eng.memory.project_list()
        return ToolResult("\n".join(f"- {r['name']} ({r['size']} chars, updated by {r['updated_by'] or 'user'})" for r in rows) or "No project notes yet.")

    add("project_list", "List the shared project notes (visible to every Bot).", {}, [], project_list, read_only=True, label=lambda a: "List project notes")

    def project_read(ctx: ToolContext, a: dict) -> ToolResult:
        p = eng.memory.project_get(a["name"])
        return ToolResult(p["content"] if p else f"No project note named '{a['name']}'.", is_error=not p)

    add("project_read", "Read a shared project note.", {"name": s("Note name")}, ["name"], project_read, read_only=True, label=lambda a: f"Read project note {a.get('name')}")

    def project_write(ctx: ToolContext, a: dict) -> ToolResult:
        p = eng.memory.project_write(a["name"], a["content"], by=ctx.bot["name"], append=bool(a.get("append")))
        return ToolResult(f"Project note '{p['name']}' saved.")

    add("project_write", "Create or update a shared project note so other Bots and the user share context. Never put secrets in notes.",
        {"name": s("Note name"), "content": s("Markdown content"), "append": b("Append instead of replacing")}, ["name", "content"], project_write,
        label=lambda a: f"Update project note {a.get('name')}")

    # ------------------------------------------------------------------ skills / routines
    def skill_list(ctx: ToolContext, a: dict) -> ToolResult:
        return ToolResult(eng.skills.index_for_bot(ctx.bot["name"]))

    add("skill_list", "List the skills available to you.", {}, [], skill_list, read_only=True, label=lambda a: "List skills")

    def skill_read(ctx: ToolContext, a: dict) -> ToolResult:
        sk = eng.skills.get(a["name"])
        if not sk or sk["status"] != "active" or (sk["bot"] and sk["bot"].lower() != ctx.bot["name"].lower()):
            return ToolResult(f"No active skill named '{a['name']}'.", is_error=True)
        return ToolResult(f"# Skill: {sk['name']}\n\n{sk['body']}")

    add("skill_read", "Read a skill and follow it.", {"name": s("Skill name")}, ["name"], skill_read, read_only=True, label=lambda a: f"Read skill {a.get('name')}")

    def skill_save(ctx: ToolContext, a: dict) -> ToolResult:
        sk = eng.skills.save(a["name"], description=a["description"], body=a["body"], status="draft", bot=ctx.bot["name"], source="bot")
        eng.notify("skill", ctx.bot, ctx.thread_id, f"{ctx.bot['name']} drafted a skill", sk["name"])
        return ToolResult(f"Skill '{sk['name']}' saved as a DRAFT. The user must review and activate it in Skills before it is used.")

    add("skill_save", "Write down a repeatable procedure as a reusable skill (markdown). It is saved as a draft for the user to review and edit.",
        {"name": s("short-kebab-case name"), "description": s("One line: when to use it"), "body": s("Markdown: when to use, inputs, numbered steps, checks, what needs approval")},
        ["name", "description", "body"], skill_save, label=lambda a: f"Draft skill {a.get('name')}")

    def routine_create(ctx: ToolContext, a: dict) -> ToolResult:
        r = eng.routines.create(ctx.bot["id"], a["name"], a["cron"], skill=a.get("skill", ""), prompt=a.get("prompt", ""), enabled=True, catch_up=False)
        return ToolResult(f"Routine '{r['name']}' scheduled ({r['cron']}).")

    add("routine_create", "Schedule a recurring unattended job for yourself (cron, 5 fields, local time). Requires approval.",
        {"name": s("Routine name"), "cron": s("5-field cron, e.g. '0 8 * * 1-5'"), "prompt": s("What to do each run"), "skill": s("Optional active skill to follow")},
        ["name", "cron"], routine_create,
        risk=lambda ctx, a: Risk("schedule", f"Schedule routine '{a.get('name')}' with cron '{a.get('cron')}' for {ctx.bot['name']}",
                                 {"cron": a.get("cron"), "prompt": a.get("prompt"), "skill": a.get("skill"), "pattern": "*"}),
        label=lambda a: f"Schedule routine {a.get('name')}")

    def routine_list(ctx: ToolContext, a: dict) -> ToolResult:
        rows = eng.routines.list(ctx.bot["id"])
        return ToolResult("\n".join(f"- {r['name']} [{r['cron']}] {'on' if r['enabled'] else 'off'} last: {r['last_status'] or 'never'}" for r in rows) or "No routines.")

    add("routine_list", "List your routines.", {}, [], routine_list, read_only=True, label=lambda a: "List routines")

    # ------------------------------------------------------------------ teamwork
    def bots_list(ctx: ToolContext, a: dict) -> ToolResult:
        lines = []
        for bt in eng.bots.list():
            busy = eng.turns.is_busy(bt["id"])
            lines.append(f"- {bt['name']}{' (you)' if bt['id'] == ctx.bot['id'] else ''}: {bt['job'][:140]} [{'working' if busy else 'idle'}{', paused' if bt['paused'] else ''}]")
        return ToolResult("\n".join(lines))

    add("bots_list", "List all Bots on the team with their jobs and whether they are busy.", {}, [], bots_list, read_only=True, label=lambda a: "List teammates")

    def message_bot(ctx: ToolContext, a: dict) -> ToolResult:
        to, tid = eng.messaging.send_to_bot(ctx.bot, a["bot"], a["message"], ctx.thread_id)
        return ToolResult(f"Message delivered to {to['name']} (thread {tid}). They will reply there; you will see it when you are next triggered.")

    add("message_bot", "Message another Bot. Delivered in a shared thread the user can read. They respond asynchronously. Reply PASS when you have nothing to add to avoid chatter.",
        {"bot": s("Bot name"), "message": s("What you need, with the context they need")}, ["bot", "message"], message_bot, label=lambda a: f"Message {a.get('bot')}")

    def handoff_create(ctx: ToolContext, a: dict) -> ToolResult:
        h = eng.messaging.create_handoff(ctx.bot["id"], a["to_bot"], a["title"], a["brief"], ctx.thread_id)
        return ToolResult(f"Handoff {h['id']} created. {h['to_name']} owns '{h['title']}' now and has been notified. Track it with handoff_list.")

    add("handoff_create", "Hand a task to another Bot (one owner per task). Include the goal, relevant context or project note names, constraints and the definition of done.",
        {"to_bot": s("Bot name"), "title": s("Short task title"), "brief": s("Full brief")}, ["to_bot", "title", "brief"], handoff_create,
        label=lambda a: f"Hand off to {a.get('to_bot')}: {short(a.get('title', ''), 50)}")

    def handoff_list(ctx: ToolContext, a: dict) -> ToolResult:
        rows = eng.messaging.list_handoffs(ctx.bot["id"], a.get("status") or "open")
        return ToolResult("\n".join(f"- {r['id']} [{r['status']}] {r['from_name']} -> {r['to_name']}: {r['title']}" + (f" | result: {short(r['result'], 120)}" if r["result"] else "") for r in rows)
                          or "No handoffs.")

    add("handoff_list", "List handoffs you gave or received.", {"status": s("open (default) | done | all statuses by name")}, [], handoff_list, read_only=True, label=lambda a: "List handoffs")

    def handoff_update(ctx: ToolContext, a: dict) -> ToolResult:
        h = eng.messaging.update_handoff(a["id"], ctx.bot["id"], a["status"], a.get("note", ""))
        return ToolResult(f"Handoff {h['id']} is now {h['status']}.")

    add("handoff_update", "Update a handoff you own: accepted, in_progress, blocked, done or cancelled, with a note (result, or what is blocking you).",
        {"id": s("Handoff id"), "status": s("accepted | in_progress | blocked | done | cancelled"), "note": s("Result or blocker")}, ["id", "status"], handoff_update,
        label=lambda a: f"Handoff {a.get('id')} -> {a.get('status')}")

    def group_create(ctx: ToolContext, a: dict) -> ToolResult:
        ids = [ctx.bot["id"]]
        for n in a["members"]:
            bt = eng.bots.find(n)
            if not bt:
                return ToolResult(f"No Bot named '{n}'.", is_error=True)
            ids.append(bt["id"])
        lead = eng.bots.find(a.get("lead", "")) if a.get("lead") else None
        g = eng.messaging.create_group(a["name"], ids, lead=lead["id"] if lead else ctx.bot["id"], goal=a.get("goal", ""))
        eng.threads.add(g["thread_id"], ctx.bot["id"], "assistant", f"Group created. Goal: {a.get('goal', '(none)')}\nMembers: {', '.join(g['member_names'])}")
        return ToolResult(f"Group '{g['name']}' created with {', '.join(g['member_names'])}. Post with group_post.")

    add("group_create", "Create a group chat of Bots to coordinate on a goal. You are included and lead by default. The user can read and join it.",
        {"name": s("Group name"), "members": {"type": "array", "items": {"type": "string"}, "description": "Bot names to add"}, "goal": s("Shared goal"), "lead": s("Lead Bot name (optional)")},
        ["name", "members"], group_create, label=lambda a: f"Create group {a.get('name')}")

    def group_post(ctx: ToolContext, a: dict) -> ToolResult:
        g = next((x for x in eng.messaging.list_groups() if x["name"].lower() == a["group"].lower() or x["id"] == a["group"]), None)
        if not g or ctx.bot["id"] not in g["members"]:
            return ToolResult(f"You are not a member of a group named '{a['group']}'.", is_error=True)
        eng.threads.add(g["thread_id"], ctx.bot["id"], "assistant", a["message"])
        eng.messaging.after_bot_message(g["thread_id"], ctx.bot, a["message"])
        return ToolResult("Posted. Mention teammates with @Name to hand them the floor.")

    add("group_post", "Post a message in a group chat you belong to. @Name mentions trigger that Bot.", {"group": s("Group name"), "message": s("Message")}, ["group", "message"],
        group_post, label=lambda a: f"Post in {a.get('group')}")

    def followup(ctx: ToolContext, a: dict) -> ToolResult:
        f = eng.messaging.schedule_followup(ctx.bot["id"], ctx.thread_id, a["note"], minutes=a.get("in_minutes"), at_iso=a.get("at"))
        return ToolResult(f"Follow-up {f['id']} scheduled for {time.strftime('%Y-%m-%d %H:%M', time.localtime(f['due_at']))}. You will be re-triggered then.")

    add("followup_schedule", "Schedule yourself to check back on something (a reply you are waiting for, a stalled handoff, an item due later). You will be woken in this thread.",
        {"note": s("What to check and why"), "in_minutes": i("Minutes from now"), "at": s("Or an ISO local time, e.g. 2026-10-06T09:00")}, ["note"], followup,
        label=lambda a: f"Follow up: {short(a.get('note', ''), 50)}")

    # ------------------------------------------------------------------ files
    def rel(p) -> str:
        return comp.rel(p)

    def fs_list(ctx: ToolContext, a: dict) -> ToolResult:
        items = comp.list_dir(a.get("path", "."))
        lines = []
        for x in items:
            lines.append(("[dir] " + x["name"]) if x["dir"] else "%s (%d bytes)" % (x["name"], x["size"]))
        return ToolResult("\n".join(lines) or "(empty folder)", path=a.get("path", "."))

    def r_outside(a: dict, verb: str) -> Risk | None:
        f, inside = comp.resolve(a.get("path", "."))
        return None if inside else Risk("outside_workspace", f"{verb} {f} (outside the workspace)", {"path": str(f), "pattern": str(f.parent)}, never_auto=True)

    add("fs_list", "List a folder in the shared workspace (relative paths are inside the workspace).", {"path": s("Folder, default '.'")}, [], fs_list, read_only=True,
        risk=lambda ctx, a: r_outside(a, "List"), label=lambda a: f"List {a.get('path', '.')}")

    def fs_read(ctx: ToolContext, a: dict) -> ToolResult:
        text, trunc = comp.read_file(a["path"], int(a.get("max_bytes", 100000)))
        return ToolResult(f"Contents of {a['path']}{' (truncated)' if trunc else ''}:", data=text, untrusted=f"file {a['path']}", path=a["path"])

    add("fs_read", "Read a text file.", {"path": s("File path"), "max_bytes": i("Max bytes, default 100000")}, ["path"], fs_read, read_only=True,
        risk=lambda ctx, a: r_outside(a, "Read"), label=lambda a: f"Read {a.get('path')}")

    def fs_write(ctx: ToolContext, a: dict) -> ToolResult:
        f = comp.write_file(a["path"], a["content"], bool(a.get("append")), who=ctx.bot["id"])
        return ToolResult(f"Wrote {len(a['content'])} characters to {rel(f)}.", path=str(f))

    def r_write(ctx: ToolContext, a: dict) -> Risk | None:
        f, inside = comp.resolve(a["path"])
        if not inside:
            return Risk("outside_workspace", f"Write {f} (outside the workspace)", {"path": str(f), "pattern": str(f.parent), "preview": short(a.get("content", ""), 500)}, never_auto=True)
        if f.exists() and not a.get("append"):
            return Risk("overwrite", f"Overwrite {rel(f)}", {"path": str(f), "pattern": str(f), "preview": short(a.get("content", ""), 500)})
        return None

    add("fs_write", "Write a text file in the workspace (creates folders). Overwriting an existing file needs approval; new files do not. Never write secrets.",
        {"path": s("File path"), "content": s("Content"), "append": b("Append instead of overwrite")}, ["path", "content"], fs_write, risk=r_write,
        label=lambda a: f"Write {a.get('path')}")

    def fs_delete(ctx: ToolContext, a: dict) -> ToolResult:
        comp.delete(a["path"], who=ctx.bot["id"])
        return ToolResult(f"Deleted {a['path']}.")

    def r_delete(ctx: ToolContext, a: dict) -> Risk:
        f, inside = comp.resolve(a["path"])
        if not inside:
            return Risk("outside_workspace", f"Delete {f} (outside the workspace)", {"path": str(f), "pattern": str(f)}, never_auto=True)
        return Risk("delete", f"Delete {rel(f)}", {"path": str(f), "pattern": str(f)})

    add("fs_delete", "Delete a file or folder. Always needs approval.", {"path": s("Path")}, ["path"], fs_delete, risk=r_delete, label=lambda a: f"Delete {a.get('path')}")

    def fs_move(ctx: ToolContext, a: dict) -> ToolResult:
        f = comp.move(a["src"], a["dst"], who=ctx.bot["id"])
        return ToolResult(f"Moved to {rel(f)}.")

    def r_move(ctx: ToolContext, a: dict) -> Risk | None:
        src, in1 = comp.resolve(a["src"])
        dst, in2 = comp.resolve(a["dst"])
        if not (in1 and in2):
            return Risk("outside_workspace", f"Move {src} to {dst}", {"path": str(dst), "pattern": str(dst.parent)}, never_auto=True)
        if dst.exists():
            return Risk("overwrite", f"Move {rel(src)} over existing {rel(dst)}", {"path": str(dst), "pattern": str(dst)})
        return None

    add("fs_move", "Move or rename a file/folder.", {"src": s("From"), "dst": s("To")}, ["src", "dst"], fs_move, risk=r_move, label=lambda a: f"Move {a.get('src')} to {a.get('dst')}")

    # ------------------------------------------------------------------ terminal
    def run_command(ctx: ToolContext, a: dict) -> ToolResult:
        res = comp.run_command(a["command"], a.get("cwd"), int(a.get("timeout", 60)), a.get("shell", "default"), stop=ctx.run.stop, who=ctx.bot["name"])
        head = f"exit code {res['exit']} after {res['seconds']}s"
        if res["timed_out"]:
            head += " (TIMED OUT and was killed)"
        if res["stopped"]:
            head += " (stopped by user)"
        return ToolResult(head, data=res["output"] or "(no output)", untrusted="command output", is_error=res["exit"] not in (0,) and not res["stopped"])

    add("run_command", "Run a terminal command in the shared workspace (Windows: cmd by default, or shell='powershell'). Commands outside the workspace, deleting, installing, "
        "downloading or running unfamiliar programs need approval. Output is truncated.",
        {"command": s("Command line"), "cwd": s("Working folder inside the workspace"), "timeout": i("Seconds, default 60, max 900"), "shell": s("default | powershell")},
        ["command"], run_command, risk=lambda ctx, a: comp.command_risk(a["command"], a.get("cwd")), label=lambda a: f"Run: {short(a.get('command', ''), 70)}")

    def web_fetch(ctx: ToolContext, a: dict) -> ToolResult:
        r = comp.web_fetch(ctx.bot["id"], a["url"])
        return ToolResult(f"HTTP {r['status']} {r['url']}" + (f" | {r['title']}" if r["title"] else ""), data=r["text"], untrusted=f"web page {host_of(r['url'])}", url=r["url"])

    add("web_fetch", "Fetch a URL over HTTP and return its text (fast, no JavaScript). Use the browser tools for interactive or JS-heavy pages.", {"url": s("URL")}, ["url"],
        web_fetch, read_only=True, label=lambda a: f"Fetch {short(a.get('url', ''), 60)}")

    # ------------------------------------------------------------------ browser (the Bot's own screen)
    def scr_of(ctx: ToolContext) -> Screen:
        scr = br.screen(ctx.bot["id"])
        if scr.takeover:
            raise BrowserError("The user is currently controlling this browser. Wait for them to hand it back.")
        scr.busy = True
        return scr

    def observe(ctx: ToolContext, scr: Screen, state: dict, headline: str) -> ToolResult:
        host = host_of(state.get("url", ""))
        blocks = br.call(scr.detect_block, timeout=20)
        lines = [headline, f"URL: {state.get('url')}"]
        if state.get("notice"):
            lines.append(f"Notice: {state['notice']}")
        if blocks:
            lines.append("HUMAN STEP NEEDED: this page shows " + "; ".join(blocks) + ". Do NOT try to bypass it or type credentials. Call request_takeover so the user can handle it.")
        images, data = [], f"Title: {state.get('title')}\n"
        if ctx.run.vision:
            _, path = br.call(scr.screenshot, timeout=30)
            images = [path]
        else:
            snap = br.call(scr.snapshot, 1500, timeout=30)
            data += _fmt_elements(snap["elements"][:60]) + "\n---\n" + snap["text"]
        return ToolResult("\n".join(lines), data=data, untrusted=f"web page {host}", images=images, url=state.get("url", ""))

    def b_goto(ctx: ToolContext, a: dict) -> ToolResult:
        scr = scr_of(ctx)
        st = br.call(scr.goto, a["url"], timeout=70)
        return observe(ctx, scr, st, f"Opened {a['url']}.")

    add("browser_goto", "Open a URL in your browser tab (shared logins). Returns a screenshot (or an element list if your model cannot see images).", {"url": s("URL")}, ["url"],
        b_goto, screen=True, label=lambda a: f"Open {short(a.get('url', ''), 70)}")

    def b_snapshot(ctx: ToolContext, a: dict) -> ToolResult:
        scr = scr_of(ctx)
        snap = br.call(scr.snapshot, int(a.get("text_chars", 5000)), timeout=30)
        host = host_of(snap["url"])
        head = f"URL: {snap['url']}\n" + (("HUMAN STEP NEEDED: page shows " + "; ".join(snap["blocks"]) + ". Call request_takeover; do not bypass.\n") if snap["blocks"] else "")
        body = f"Title: {snap['title']}\nInteractive elements in view (use the number with browser_click / browser_type):\n{_fmt_elements(snap['elements'])}\n---\nPage text:\n{snap['text']}"
        return ToolResult(head, data=body, untrusted=f"web page {host}", url=snap["url"])

    add("browser_snapshot", "List the numbered interactive elements in view plus the page text. Use element numbers with browser_click/browser_type. Take a new snapshot after the page changes.",
        {"text_chars": i("Max page text characters, default 5000")}, [], b_snapshot, screen=True, read_only=True, label=lambda a: "Read the page")

    def b_screenshot(ctx: ToolContext, a: dict) -> ToolResult:
        if not ctx.run.vision:
            return ToolResult("Your model cannot view images; use browser_snapshot instead.", is_error=True)
        scr = scr_of(ctx)
        _, path = br.call(scr.screenshot, bool(a.get("full_page")), timeout=40)
        st = br.call(scr.state, timeout=20)
        return ToolResult(f"Screenshot of {st['url']}", images=[path], url=st["url"])

    add("browser_screenshot", "Take a screenshot of your browser tab (1280x800 viewport; coordinates for browser_click x/y refer to this image).",
        {"full_page": b("Capture the full scrollable page")}, [], b_screenshot, screen=True, read_only=True, label=lambda a: "Take a screenshot")

    def b_click(ctx: ToolContext, a: dict) -> ToolResult:
        scr = scr_of(ctx)
        st = br.call(scr.click, element=a.get("element"), x=a.get("x"), y=a.get("y"), double=bool(a.get("double")), right=bool(a.get("right")), timeout=40)
        return observe(ctx, scr, st, "Clicked." + (" The page navigated." if st.get("navigated") else ""))

    def r_click(ctx: ToolContext, a: dict) -> Risk | None:
        scr = br.screen(ctx.bot["id"])
        info = br.call(scr.describe, element=a.get("element"), x=a.get("x"), y=a.get("y"), timeout=15)
        return classify_target(info, "Click")

    def classify_target(info: dict, verb: str) -> Risk | None:
        if not info.get("found"):
            return None
        text = f"{info.get('text', '')} {info.get('href', '')}".strip()
        host = host_of(info.get("pageUrl", ""))
        base = {"target": info.get("text"), "page": info.get("pageUrl"), "pattern": f"{host}|{(info.get('text') or '').lower()[:40]}"}
        if PURCHASE_RE.search(text):
            return Risk("purchase", f"{verb} \"{short(info.get('text'), 60)}\" on {host} (looks like a purchase/payment)", base, never_auto=True)
        if DELETE_RE.search(text):
            return Risk("delete", f"{verb} \"{short(info.get('text'), 60)}\" on {host} (looks destructive)", base)
        interactive_submit = info.get("tag") in ("button", "input") or info.get("role") == "button"
        if interactive_submit and (info.get("type") == "submit" or (info.get("inForm") and info.get("tag") == "button" and not info.get("explicitType"))
                                   or SEND_RE.search(info.get("text", ""))):
            return Risk("submit", f"{verb} \"{short(info.get('text'), 60)}\" on {host} (submits a form or sends something)", {**base, "form_action": info.get("formAction")})
        return None

    add("browser_click", "Click an element by number (from browser_snapshot) or by x,y pixel coordinates from a screenshot. Submitting forms, sending, purchases and deletes need approval.",
        {"element": i("Element number from browser_snapshot"), "x": i("Pixel x"), "y": i("Pixel y"), "double": b("Double click"), "right": b("Right click")}, [], b_click,
        risk=r_click, screen=True, label=lambda a: f"Click {('element ' + str(a['element'])) if a.get('element') else ('(' + str(a.get('x')) + ',' + str(a.get('y')) + ')')}")

    def b_type(ctx: ToolContext, a: dict) -> ToolResult:
        scr = scr_of(ctx)
        st = br.call(scr.type_text, a["text"], element=a.get("element"), clear=bool(a.get("clear", True)), submit=bool(a.get("submit")), timeout=40)
        return observe(ctx, scr, st, f"Typed {len(a['text'])} characters.")

    def r_type(ctx: ToolContext, a: dict) -> Risk | None:
        if not a.get("submit"):
            return None
        info = br.call(br.screen(ctx.bot["id"]).describe, element=a.get("element"), timeout=15)
        host = host_of(info.get("pageUrl", ""))
        if info.get("found") and info.get("inForm"):
            return Risk("submit", f"Type into a field and press Enter to submit a form on {host}", {"page": info.get("pageUrl"), "pattern": f"{host}|enter"})
        return None

    add("browser_type", "Type text into an element (by number) or the focused field. Never for passwords or payment details: use request_takeover for those.",
        {"text": s("Text"), "element": i("Element number"), "clear": b("Clear the field first (default true)"), "submit": b("Press Enter afterwards")}, ["text"], b_type,
        risk=r_type, screen=True, label=lambda a: f"Type \"{short(a.get('text', ''), 40)}\"")

    def b_select(ctx: ToolContext, a: dict) -> ToolResult:
        scr = scr_of(ctx)
        st = br.call(scr.select_option, int(a["element"]), a["value"], timeout=30)
        return observe(ctx, scr, st, f"Selected {a['value']}.")

    add("browser_select", "Choose an option in a <select> dropdown by visible label or value.", {"element": i("Element number"), "value": s("Option label or value")}, ["element", "value"],
        b_select, screen=True, label=lambda a: f"Select {a.get('value')}")

    def b_press(ctx: ToolContext, a: dict) -> ToolResult:
        scr = scr_of(ctx)
        st = br.call(scr.press, a["key"], timeout=30)
        return observe(ctx, scr, st, f"Pressed {a['key']}.")

    def r_press(ctx: ToolContext, a: dict) -> Risk | None:
        if a.get("key", "").lower() not in ("enter", "numpadenter", "return"):
            return None
        info = br.call(br.screen(ctx.bot["id"]).describe, timeout=15)
        host = host_of(info.get("pageUrl", ""))
        if info.get("found") and info.get("inForm") and info.get("tag") in ("input", "select", "textarea"):
            return Risk("submit", f"Press Enter in a form field on {host} (submits the form)", {"page": info.get("pageUrl"), "pattern": f"{host}|enter"})
        return None

    add("browser_press", "Press a key or combo (Enter, Tab, Escape, ArrowDown, Control+A, PageDown ...).", {"key": s("Key name")}, ["key"], b_press, risk=r_press, screen=True,
        label=lambda a: f"Press {a.get('key')}")

    def b_scroll(ctx: ToolContext, a: dict) -> ToolResult:
        scr = scr_of(ctx)
        st = br.call(scr.scroll, a.get("direction", "down"), int(a.get("amount", 600)), timeout=20)
        return observe(ctx, scr, st, f"Scrolled {a.get('direction', 'down')}.")

    add("browser_scroll", "Scroll the page.", {"direction": s("up | down | left | right"), "amount": i("Pixels, default 600")}, [], b_scroll, screen=True, label=lambda a: f"Scroll {a.get('direction', 'down')}")

    def b_back(ctx: ToolContext, a: dict) -> ToolResult:
        scr = scr_of(ctx)
        return observe(ctx, scr, br.call(scr.back, timeout=40), "Went back.")

    add("browser_back", "Go back one page.", {}, [], b_back, screen=True, label=lambda a: "Go back")

    def b_text(ctx: ToolContext, a: dict) -> ToolResult:
        scr = scr_of(ctx)
        r = br.call(scr.read_text, a.get("selector"), int(a.get("max_chars", 15000)), timeout=30)
        return ToolResult(f"Text of {r['url']}{' (truncated)' if r['truncated'] else ''}:", data=r["text"], untrusted=f"web page {host_of(r['url'])}", url=r["url"])

    add("browser_read_text", "Read the text of the page or of a CSS selector.", {"selector": s("Optional CSS selector"), "max_chars": i("Default 15000")}, [], b_text, screen=True,
        read_only=True, label=lambda a: "Read page text")

    def b_wait(ctx: ToolContext, a: dict) -> ToolResult:
        scr = scr_of(ctx)
        st = br.call(scr.wait_for, a.get("text"), float(a.get("seconds", 2)), timeout=45)
        return observe(ctx, scr, st, "Waited.")

    add("browser_wait", "Wait for some seconds or until text appears on the page.", {"seconds": i("Seconds (max 30)"), "text": s("Text to wait for")}, [], b_wait, screen=True,
        label=lambda a: "Wait")

    # ------------------------------------------------------------------ knowledge base
    def knowledge_search(ctx: ToolContext, a: dict) -> ToolResult:
        eng.knowledge.refresh(eng.files)   # pick up workspace files that changed since they were indexed
        hits = eng.knowledge.search(a["query"], ctx.bot["id"], int(a.get("limit", 6) or 6))
        if not hits:
            return ToolResult(f"No passages in the knowledge base match '{a['query']}'. Try other words, or ask the user to add the document.")
        lines = [f"[{h['name']} · passage {h['seq']} · id {h['source_id']}]\n{h['text'][:900]}" for h in hits]
        return ToolResult(f"{len(hits)} passage(s) from the user's knowledge base for '{a['query']}':", data="\n\n---\n\n".join(lines), untrusted="the knowledge base")

    add("knowledge_search", "Search the user's knowledge base (documents they added: notes, manuals, policies, reports). Returns the best matching passages with their source. "
        "Use it before answering questions about the user's own material, and cite the source name.",
        {"query": s("What to look for, in plain words"), "limit": i("How many passages, default 6")}, ["query"], knowledge_search, read_only=True,
        label=lambda a: f"Search knowledge: {short(a.get('query', ''), 40)}")

    def knowledge_read(ctx: ToolContext, a: dict) -> ToolResult:
        src = eng.knowledge.find_source(str(a["source"]), ctx.bot["id"])
        if not src:
            return ToolResult(f"No knowledge source '{a['source']}'. Use knowledge_search to find one.", is_error=True)
        r = eng.knowledge.read(src["id"], a.get("from_passage"))
        more = f" (passage {r['from_seq']} onward; {r['passages']} in total, pass from_passage to read further)" if r["passages"] > 6 else ""
        return ToolResult(f"{r['name']}{more}:", data=r["text"], untrusted=f"knowledge document {r['name']}")

    add("knowledge_read", "Read a document from the knowledge base by name or id, starting at a passage number.",
        {"source": s("Source name or id (from knowledge_search)"), "from_passage": i("Passage number to start at, default 0")}, ["source"], knowledge_read, read_only=True,
        label=lambda a: f"Read knowledge: {short(a.get('source', ''), 40)}")

    return T
