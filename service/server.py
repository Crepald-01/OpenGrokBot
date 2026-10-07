"""Local HTTP API used by the desktop app and the mobile PWA. Everything except /api/health and the
OAuth callback requires the access token (Bearer header, ?token= or the gb_token cookie)."""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import queue
import re
import socket
import time
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from core import VERSION, apitokens, backup as backup_mod, commands, packages, paths, secrets, skills as skills_mod
from core.browser import BrowserError
from core.bots import BotError
from core.computer import ComputerError
from core.engine import Engine
from core.files import FileError
from core.plugins import ConnectorError
from core.providers import ProviderError, make_provider
from core.routines import PRESETS
from core.secrets import SecretStoreError
from core.settings import PROVIDER_PRESETS
from core.templates import TEMPLATES

SECRET_PREFIXES = ("provider:", "plugin:", "mcp:", "ntfy:")


def lan_addresses() -> list[str]:
    out = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in out:
                out.append(ip)
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith("127."):
            if ip in out:   # the address Windows uses to reach the network (Wi-Fi/LAN) must come first: it is the one phones can reach
                out.remove(ip)
            out.insert(0, ip)
    except OSError:
        pass
    return out


def create_app(engine: Engine, token: str) -> FastAPI:
    app = FastAPI(title="OpenGrokBot service", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None)
    eng = engine
    fails: dict[str, list[float]] = {}

    # ----------------------------------------------------------------- auth
    def presented(request: Request) -> str:
        h = request.headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            return h[7:].strip()
        return request.query_params.get("token") or request.cookies.get("gb_token") or ""

    def check_token(tok: str, ip: str) -> bool:
        now = time.time()
        recent = [t for t in fails.get(ip, []) if now - t < 300]
        fails[ip] = recent
        if len(recent) >= 10:
            raise HTTPException(429, "Too many failed attempts. Wait a few minutes.")
        ok = bool(tok) and hmac.compare_digest(tok.encode(), token.encode())
        if not ok:
            fails[ip].append(now)
        return ok

    def identify(tok: str, ip: str) -> str | None:
        """"main" for the access token, the scope for a valid API token, None for anything else. Failures count towards the same lockout."""
        now = time.time()
        recent = [t for t in fails.get(ip, []) if now - t < 300]
        fails[ip] = recent
        if len(recent) >= 10:
            raise HTTPException(429, "Too many failed attempts. Wait a few minutes.")
        if tok and hmac.compare_digest(tok.encode(), token.encode()):
            return "main"
        row = eng.apitokens.verify(tok)
        if row:
            return row["scope"]
        fails[ip].append(now)
        return None

    def auth(request: Request) -> None:
        ip = request.client.host if request.client else "?"
        who = identify(presented(request), ip)
        if who is None:
            raise HTTPException(401, "Missing or wrong access token.")
        if who != "main" and not apitokens.allowed(who, request.method, request.url.path):
            raise HTTPException(403, f"This API token ({who}) is not allowed to do that. Use the main access token for it.")

    api = Depends(auth)

    @app.exception_handler(ValueError)
    @app.exception_handler(BotError)
    @app.exception_handler(SecretStoreError)
    @app.exception_handler(ConnectorError)
    @app.exception_handler(ComputerError)
    @app.exception_handler(BrowserError)
    @app.exception_handler(ProviderError)
    async def _bad(_req: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=400)

    # --------------------------------------------------------------- basics
    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "name": "OpenGrokBot", "version": VERSION}

    @app.post("/api/login")
    def login(request: Request, response: Response, body: dict = Body(...)) -> dict:
        ip = request.client.host if request.client else "?"
        if not check_token(str(body.get("token", "")).strip(), ip):
            raise HTTPException(401, "That token is not right.")
        response.set_cookie("gb_token", token, httponly=True, samesite="strict", max_age=86400 * 365)
        return {"ok": True}

    @app.get("/api/bootstrap", dependencies=[api])
    def bootstrap() -> dict:
        bots = eng.bots.list()
        return {"bots": bots, "groups": eng.messaging.list_groups(), "status": eng.status(), "approvals": eng.approvals.list("pending"),
                "profiles": _profiles(), "templates": TEMPLATES, "settings": _public_settings(), "last_event": eng.events.last_id(),
                "presets": PRESETS, "handoffs": eng.messaging.list_handoffs(status="open", limit=50)}

    def _profiles() -> list[dict]:
        out = []
        allowed = eng.admin.allowed_providers()
        for pid, p in eng.settings.profiles().items():
            if allowed is not None and pid not in allowed:
                continue
            out.append({**p, "id": pid, "key_set": secrets.has_secret(f"provider:{pid}"), "preset": pid in PROVIDER_PRESETS})
        return out

    def _public_settings() -> dict:
        s = eng.settings.all()
        s.pop("providers", None)
        s["secrets_backend"] = secrets.backend_available()
        return s

    @app.get("/api/status", dependencies=[api])
    def status() -> dict:
        return eng.status()

    # ----------------------------------------------------------------- bots
    @app.get("/api/bots", dependencies=[api])
    def bots_list() -> list[dict]:
        return eng.bots.list()

    @app.post("/api/bots", dependencies=[api])
    def bots_create(body: dict = Body(...)) -> dict:
        bot = eng.bots.create(body.get("name", ""), job=body.get("job", ""), instructions=body.get("instructions", ""), emoji=body.get("emoji", "🤖"),
                              **{k: v for k, v in body.items() if k in ("profile", "model", "approval_mode", "step_limit", "net_mode", "net_allow", "net_deny", "grants", "proactive", "template")})
        eng.threads.main_thread(bot["id"])
        return bot

    @app.post("/api/bots/from_template", dependencies=[api])
    def bots_from_template(body: dict = Body(...)) -> dict:
        return eng.create_from_template(body["template"], body.get("name") or None)

    @app.post("/api/teams", dependencies=[api])
    def teams(body: dict = Body(default={})) -> dict:
        return eng.create_team(body.get("templates"), body.get("group_name", "Ops room"))

    @app.get("/api/templates", dependencies=[api])
    def templates() -> list[dict]:
        return TEMPLATES

    @app.get("/api/bots/{bot_id}", dependencies=[api])
    def bots_get(bot_id: str) -> dict:
        b = eng.bots.get(bot_id)
        if not b:
            raise HTTPException(404, "No such Bot.")
        b["network_layers"] = eng.net.describe(bot_id)
        b["rules"] = eng.approvals.rules(bot_id)
        return b

    @app.put("/api/bots/{bot_id}", dependencies=[api])
    def bots_update(bot_id: str, body: dict = Body(...)) -> dict:
        return eng.bots.update(bot_id, **body)

    @app.delete("/api/bots/{bot_id}", dependencies=[api])
    def bots_delete(bot_id: str) -> dict:
        eng.turns.stop_bot(bot_id)
        eng.computer.browser.drop_screen(bot_id)
        eng.bots.delete(bot_id)
        return {"ok": True}

    @app.post("/api/bots/{bot_id}/stop", dependencies=[api])
    def bots_stop(bot_id: str) -> dict:
        return {"stopped": eng.turns.stop_bot(bot_id)}

    @app.get("/api/bots/{bot_id}/export", dependencies=[api])
    def bots_export(bot_id: str, memory: bool = False) -> Response:
        data = packages.export_bot(eng, bot_id, include_memory=memory)
        name = re.sub(r"[^A-Za-z0-9\-]+", "_", (eng.bots.get(bot_id) or {}).get("name", "bot")).strip("_") or "bot"   # ASCII only: headers are Latin-1
        return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}.gbbot"'})

    @app.post("/api/bots/import", dependencies=[api])
    async def bots_import(request: Request) -> dict:
        data = await request.body()
        return await asyncio.to_thread(packages.import_bot, eng, data, request.query_params.get("name"))

    @app.delete("/api/rules/{rid}", dependencies=[api])
    def rule_delete(rid: int) -> dict:
        eng.approvals.delete_rule(rid)
        return {"ok": True}

    # -------------------------------------------------------------- threads
    @app.get("/api/bots/{bot_id}/threads", dependencies=[api])
    def threads_for_bot(bot_id: str) -> list[dict]:
        main = eng.threads.main_thread(bot_id)
        rows = eng.threads.list_for_bot(bot_id)
        for r in rows:
            r["main"] = r["id"] == main["id"]
        return rows

    @app.post("/api/bots/{bot_id}/threads", dependencies=[api])
    def threads_create(bot_id: str, body: dict = Body(default={})) -> dict:
        return eng.threads.create(bot_id, body.get("title") or "New thread")

    @app.get("/api/threads/{tid}", dependencies=[api])
    def thread_get(tid: str, limit: int = 400) -> dict:
        th = eng.threads.get(tid)
        if not th:
            raise HTTPException(404, "No such thread.")
        g = eng.messaging.group_by_thread(tid)
        running = [r for r in eng.turns.active() if r["thread_id"] == tid]
        return {"thread": th, "group": g, "items": eng.threads.display(tid, limit), "running": running,
                "approvals": [a for a in eng.approvals.list("pending") if a["thread_id"] == tid]}

    @app.put("/api/threads/{tid}", dependencies=[api])
    def thread_rename(tid: str, body: dict = Body(...)) -> dict:
        eng.threads.rename(tid, body.get("title", ""))
        return {"ok": True}

    @app.delete("/api/threads/{tid}", dependencies=[api])
    def thread_delete(tid: str) -> dict:
        eng.turns.stop_thread(tid)
        th = eng.threads.get(tid)
        if th and th["group_id"]:
            eng.messaging.delete_group(th["group_id"])
        else:
            eng.threads.delete(tid)
        return {"ok": True}

    @app.post("/api/threads/{tid}/messages", dependencies=[api])
    def thread_post(tid: str, body: dict = Body(...)) -> dict:
        paths_: list[str] = []
        for im in (body.get("images") or [])[:6]:
            raw = base64.b64decode(im.get("data", ""))
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("Image is too large (max 8 MB).")
            ext = ".jpg" if raw[:3] == b"\xff\xd8\xff" else ".png"
            p = paths.screenshots_dir() / f"upload-{int(time.time() * 1000)}{ext}"
            p.write_bytes(raw)
            paths_.append(str(p))
        return eng.send_user_message(tid, body.get("text", ""), paths_)

    @app.get("/api/digest", dependencies=[api])
    def digest(spec: str = "today") -> dict:
        d = eng.digest.build(spec)
        return {**d, "headline": eng.digest.headline(d), "markdown": eng.digest.markdown(d)}

    @app.post("/api/bots/pause_all", dependencies=[api])
    def bots_pause_all(body: dict = Body(default={})) -> dict:
        paused = bool(body.get("paused", True))
        n = eng.bots.set_paused_all(paused)
        if paused:
            for r in eng.turns.active():
                eng.turns.stop_bot(r["bot_id"])
        return {"changed": n, "paused": paused}

    @app.post("/api/bots/{bot_id}/duplicate", dependencies=[api])
    def bots_duplicate(bot_id: str, body: dict = Body(default={})) -> dict:
        return eng.bots.duplicate(bot_id, body.get("name"))

    @app.get("/api/updates", dependencies=[api])
    def updates(refresh: bool = False) -> dict:
        return eng.updates.check(force=True) if refresh else eng.updates.state()

    @app.put("/api/pricing", dependencies=[api])
    def pricing_put(body: dict = Body(...)) -> dict:
        eng.usage.pricing.set_prices(body.get("models", {}), body.get("currency"))
        return eng.usage.cost_summary()

    @app.get("/api/backup", dependencies=[api])
    def backup_get(workspace: bool = False) -> Response:
        try:
            data = backup_mod.create(eng, include_workspace=workspace)
        except backup_mod.BackupError as e:
            raise HTTPException(400, str(e))
        name = "opengrokbot-backup-" + time.strftime("%Y%m%d-%H%M") + ".zip"
        return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/backup/restore", dependencies=[api])
    async def backup_restore(request: Request) -> dict:
        data = await request.body()
        try:
            info = await asyncio.to_thread(backup_mod.stage_restore, data)
        except backup_mod.BackupError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "restart_required": True, **info}

    @app.get("/api/ws/list", dependencies=[api])
    def ws_list(path: str = "") -> dict:
        try:
            return eng.files.list(path)
        except FileError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/ws/recent", dependencies=[api])
    def ws_recent(limit: int = 30) -> dict:
        return {"entries": eng.files.recent(max(1, min(200, limit))), "stats": eng.files.stats()}

    @app.get("/api/ws/search", dependencies=[api])
    def ws_search(q: str = "") -> dict:
        return {"entries": eng.files.search(q)}

    @app.get("/api/ws/preview", dependencies=[api])
    def ws_preview(path: str) -> dict:
        try:
            return eng.files.preview(path)
        except FileError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/ws/raw", dependencies=[api])
    def ws_raw(path: str) -> Response:
        try:
            p, mime = eng.files.raw(path)
        except FileError as e:
            raise HTTPException(400, str(e))
        # FileResponse streams from disk and encodes names outside Latin-1 (RFC 5987), which a hand-built header cannot
        return FileResponse(p, media_type=mime, filename=p.name, content_disposition_type="attachment")

    @app.delete("/api/ws/file", dependencies=[api])
    def ws_delete(path: str) -> dict:
        try:
            eng.files.delete(path)
        except FileError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.get("/api/search", dependencies=[api])
    def search(q: str = "", limit: int = 30) -> dict:
        return eng.search.run(q, max(1, min(100, limit)))

    @app.get("/api/threads/{tid}/export", dependencies=[api])
    def thread_export(tid: str) -> Response:
        th = eng.threads.get(tid)
        if not th:
            raise HTTPException(404, "No such thread.")
        name = re.sub(r"[^A-Za-z0-9\-]+", "_", th.get("title") or "chat").strip("_")[:40] or "chat"
        return Response(eng.threads.export_markdown(tid).encode("utf-8"), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}.md"'})

    @app.get("/api/commands", dependencies=[api])
    def commands_list() -> list[dict]:
        return commands.listing()

    @app.post("/api/threads/{tid}/stop", dependencies=[api])
    def thread_stop(tid: str) -> dict:
        return {"stopped": eng.turns.stop_thread(tid)}

    @app.get("/api/files/shots/{name}")
    def shot(name: str, request: Request) -> Response:
        auth(request)
        p = (paths.screenshots_dir() / Path(name).name)
        if not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, headers={"Cache-Control": "private, max-age=3600"})

    # --------------------------------------------------------------- groups
    @app.get("/api/groups", dependencies=[api])
    def groups_list(pairs: bool = False) -> list[dict]:
        return eng.messaging.list_groups(include_pairs=pairs)

    @app.post("/api/groups", dependencies=[api])
    def groups_create(body: dict = Body(...)) -> dict:
        return eng.messaging.create_group(body["name"], body.get("members", []), body.get("lead", ""), body.get("goal", ""))

    @app.put("/api/groups/{gid}", dependencies=[api])
    def groups_update(gid: str, body: dict = Body(...)) -> dict:
        return eng.messaging.update_group(gid, **body)

    @app.delete("/api/groups/{gid}", dependencies=[api])
    def groups_delete(gid: str) -> dict:
        g = eng.messaging.get_group(gid)
        if g:
            eng.turns.stop_thread(g["thread_id"])
        eng.messaging.delete_group(gid)
        return {"ok": True}

    # ------------------------------------------------------------ approvals
    @app.get("/api/approvals", dependencies=[api])
    def approvals_list(status: str = "pending", bot_id: str | None = None, limit: int = 100) -> list[dict]:
        return eng.approvals.list(None if status == "all" else status, bot_id, limit)

    @app.post("/api/approvals/{aid}/decide", dependencies=[api])
    def approvals_decide(aid: str, body: dict = Body(...)) -> dict:
        r = eng.approvals.decide(aid, bool(body.get("approve")), bool(body.get("remember")), body.get("note", ""), body.get("answer", ""))
        if not r:
            raise HTTPException(404, "No such approval.")
        return r

    # --------------------------------------------------------------- memory
    @app.get("/api/bots/{bot_id}/memory", dependencies=[api])
    def memory_list(bot_id: str) -> list[dict]:
        return eng.memory.list(bot_id)

    @app.post("/api/bots/{bot_id}/memory", dependencies=[api])
    def memory_add(bot_id: str, body: dict = Body(...)) -> dict:
        mid = eng.memory.add(bot_id, body.get("kind", "fact"), body.get("text", ""), source="user", verify=bool(body.get("verify")), pinned=bool(body.get("pinned")))
        return {"id": mid}

    @app.put("/api/bots/{bot_id}/memory/{mid}", dependencies=[api])
    def memory_update(bot_id: str, mid: int, body: dict = Body(...)) -> dict:
        return {"ok": eng.memory.update(bot_id, mid, body.get("text"), body.get("kind"), body.get("pinned"))}

    @app.delete("/api/bots/{bot_id}/memory/{mid}", dependencies=[api])
    def memory_delete(bot_id: str, mid: int) -> dict:
        return {"ok": eng.memory.forget(bot_id, mid)}

    @app.get("/api/projects", dependencies=[api])
    def projects_list() -> list[dict]:
        return eng.memory.project_list()

    @app.get("/api/projects/{name}", dependencies=[api])
    def projects_get(name: str) -> dict:
        p = eng.memory.project_get(name)
        if not p:
            raise HTTPException(404)
        return p

    @app.put("/api/projects/{name}", dependencies=[api])
    def projects_put(name: str, body: dict = Body(...)) -> dict:
        return eng.memory.project_write(name, body.get("content", ""), by="user")

    @app.delete("/api/projects/{name}", dependencies=[api])
    def projects_delete(name: str) -> dict:
        eng.memory.project_delete(name)
        return {"ok": True}

    # -------------------------------------------------- handoffs / followups
    @app.get("/api/handoffs", dependencies=[api])
    def handoffs_list(status: str = "open", bot_id: str | None = None) -> list[dict]:
        return eng.messaging.list_handoffs(bot_id, None if status == "all" else status)

    @app.post("/api/handoffs", dependencies=[api])
    def handoffs_create(body: dict = Body(...)) -> dict:
        return eng.messaging.create_handoff("user", body["to_bot"], body["title"], body.get("brief", ""))

    @app.put("/api/handoffs/{hid}", dependencies=[api])
    def handoffs_update(hid: str, body: dict = Body(...)) -> dict:
        return eng.messaging.update_handoff(hid, "user", body["status"], body.get("note", ""))

    @app.get("/api/followups", dependencies=[api])
    def followups_list(bot_id: str | None = None) -> list[dict]:
        return eng.messaging.list_followups(bot_id)

    @app.delete("/api/followups/{fid}", dependencies=[api])
    def followups_cancel(fid: str) -> dict:
        eng.messaging.cancel_followup(fid)
        return {"ok": True}

    # ------------------------------------------------------------- computer
    @app.get("/api/computer", dependencies=[api])
    def computer_status() -> dict:
        d = eng.computer.status()
        d["terminal"] = list(eng.computer.terminal_log)[-30:]
        return d

    @app.get("/api/bots/{bot_id}/screen.jpg")
    def screen_jpg(bot_id: str, request: Request, q: int = 60) -> Response:
        auth(request)
        scr = eng.computer.browser.screen(bot_id)
        data, _ = eng.computer.browser.call(scr.screenshot, False, max(20, min(q, 90)), False, timeout=30)
        url = scr.page.url if scr.page is not None and not scr.page.is_closed() else ""
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store", "X-Page-Url": base64.b64encode(url.encode()).decode()})

    @app.post("/api/bots/{bot_id}/screen/input", dependencies=[api])
    def screen_input(bot_id: str, body: dict = Body(...)) -> dict:
        return eng.computer.browser.call(eng.computer.browser.screen(bot_id).user_input, body, timeout=40)

    @app.post("/api/bots/{bot_id}/takeover", dependencies=[api])
    def takeover(bot_id: str, body: dict = Body(...)) -> dict:
        action = body.get("action")
        comp = eng.computer
        if action == "start":
            if not comp.takeover_active(bot_id):
                comp.takeover_begin(bot_id, "You are in control")
        elif action == "handback":
            for a in eng.approvals.list("pending", bot_id):
                if a["category"] in ("takeover", "login") and a["tool"] == "request_takeover":
                    eng.approvals.decide(a["id"], True, note=body.get("note", "Handed back from the browser view"))
            if comp.takeover_active(bot_id):
                comp.takeover_end(bot_id)
        else:
            raise ValueError("action must be start or handback")
        return {"active": comp.takeover_active(bot_id)}

    @app.post("/api/bots/{bot_id}/record", dependencies=[api])
    def record(bot_id: str, body: dict = Body(...)) -> dict:
        comp = eng.computer
        a = body.get("action")
        if a == "start":
            if not comp.takeover_active(bot_id):
                comp.takeover_begin(bot_id, "Follow-along: you are showing the Bot a job")
            return comp.rec_start(bot_id, body.get("name", "Untitled demo"))
        if a == "note":
            comp.rec_note(bot_id, body.get("text", ""))
            return {"ok": True}
        if a == "pause":
            scr = comp.browser.screen(bot_id)
            if scr.recording is not None:
                scr.recording["paused"] = bool(body.get("paused", True))
            return {"ok": True}
        if a == "stop":
            return comp.rec_stop(bot_id) or {}
        raise ValueError("action must be start, note, pause or stop")

    @app.get("/api/recordings", dependencies=[api])
    def recordings(bot_id: str | None = None) -> list[dict]:
        return eng.computer.recordings(bot_id)

    @app.post("/api/recordings/{rid}/draft_skill", dependencies=[api])
    def draft_skill(rid: str, body: dict = Body(default={})) -> dict:
        rec = eng.computer.recording(rid)
        bot = eng.bots.get(rec["bot_id"]) if rec else None
        if not rec or not bot:
            raise ValueError("Unknown recording.")
        return skills_mod.draft_from_recording(eng, bot, rec, body.get("name"))

    @app.delete("/api/recordings/{rid}", dependencies=[api])
    def recording_delete(rid: str) -> dict:
        eng.db.execute("DELETE FROM recordings WHERE id=?", (rid,))
        return {"ok": True}

    def ws_path(p: str) -> Path:
        full, inside = eng.computer.resolve(p)
        if not inside:
            raise ValueError("That path is outside the shared workspace.")
        return full

    @app.get("/api/computer/files", dependencies=[api])
    def files_list(path: str = ".") -> dict:
        ws_path(path)
        return {"path": eng.computer.rel(eng.computer.resolve(path)[0]), "items": eng.computer.list_dir(path)}

    @app.get("/api/computer/file", dependencies=[api])
    def file_get(path: str, download: bool = False) -> Response:
        f = ws_path(path)
        if not f.is_file():
            raise HTTPException(404)
        if download:
            return FileResponse(f, filename=f.name)
        text, trunc = eng.computer.read_file(path, 400_000)
        return JSONResponse({"path": path, "content": text, "truncated": trunc})

    @app.put("/api/computer/file", dependencies=[api])
    def file_put(body: dict = Body(...)) -> dict:
        ws_path(body["path"])
        eng.computer.write_file(body["path"], body.get("content", ""))
        return {"ok": True}

    @app.post("/api/computer/mkdir", dependencies=[api])
    def mkdir(body: dict = Body(...)) -> dict:
        ws_path(body["path"]).mkdir(parents=True, exist_ok=True)
        return {"ok": True}

    @app.delete("/api/computer/file", dependencies=[api])
    def file_delete(path: str) -> dict:
        ws_path(path)
        eng.computer.delete(path)
        return {"ok": True}

    @app.post("/api/computer/upload", dependencies=[api])
    async def file_upload(request: Request, path: str) -> dict:
        f = ws_path(path)
        data = await request.body()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
        return {"ok": True, "size": len(data)}

    @app.post("/api/computer/terminal", dependencies=[api])
    def terminal(body: dict = Body(...)) -> dict:
        ws_path(body.get("cwd") or ".")
        return eng.computer.run_command(body["command"], body.get("cwd"), int(body.get("timeout", 120)), body.get("shell", "default"), who="you")

    # --------------------------------------------------------------- skills
    @app.get("/api/skills", dependencies=[api])
    def skills_list() -> list[dict]:
        return eng.skills.list()

    @app.get("/api/skills/{name}", dependencies=[api])
    def skills_get(name: str) -> dict:
        s = eng.skills.get(name)
        if not s:
            raise HTTPException(404)
        return s

    @app.put("/api/skills/{name}", dependencies=[api])
    def skills_put(name: str, body: dict = Body(...)) -> dict:
        return eng.skills.save(name, body["raw"])

    @app.post("/api/skills/{name}/status", dependencies=[api])
    def skills_status(name: str, body: dict = Body(...)) -> dict:
        s = eng.skills.set_status(name, body["status"])
        if not s:
            raise HTTPException(404)
        return s

    @app.delete("/api/skills/{name}", dependencies=[api])
    def skills_delete(name: str) -> dict:
        return {"ok": eng.skills.delete(name)}

    @app.post("/api/skills/{name}/test", dependencies=[api])
    def skills_test(name: str, body: dict = Body(...)) -> dict:
        return eng.test_skill(body["bot_id"], name, bool(body.get("dry_run", True)))

    # -------------------------------------------------------------- routines
    @app.get("/api/routines", dependencies=[api])
    def routines_list(bot_id: str | None = None) -> list[dict]:
        return eng.routines.list(bot_id)

    @app.post("/api/routines", dependencies=[api])
    def routines_create(body: dict = Body(...)) -> dict:
        return eng.routines.create(body["bot_id"], body.get("name", "Routine"), body["cron"], body.get("skill", ""), body.get("prompt", ""),
                                   bool(body.get("enabled", True)), bool(body.get("dry_run", False)), body.get("notify", "always"), bool(body.get("catch_up", False)))

    @app.put("/api/routines/{rid}", dependencies=[api])
    def routines_update(rid: str, body: dict = Body(...)) -> dict:
        return eng.routines.update(rid, **body)

    @app.delete("/api/routines/{rid}", dependencies=[api])
    def routines_delete(rid: str) -> dict:
        eng.routines.delete(rid)
        return {"ok": True}

    @app.post("/api/routines/{rid}/run", dependencies=[api])
    def routines_run(rid: str) -> dict:
        eng.routines.run_now(rid)
        return {"ok": True}

    @app.get("/api/routine_runs", dependencies=[api])
    def routine_runs(routine_id: str | None = None, bot_id: str | None = None, limit: int = 50) -> list[dict]:
        return eng.routines.runs(routine_id, bot_id, limit)

    # ------------------------------------------------------ plugins and MCP
    @app.get("/api/plugins", dependencies=[api])
    def plugins_list() -> dict:
        return {"plugins": eng.plugins.list(), "catalog": eng.plugins.catalog(), "install_allowed": eng.admin.plugin_install_allowed()}

    @app.post("/api/plugins/install", dependencies=[api])
    def plugins_install(body: dict = Body(...)) -> dict:
        return eng.plugins.install(body["source"])

    @app.delete("/api/plugins/{pid}", dependencies=[api])
    def plugins_remove(pid: str) -> dict:
        eng.plugins.uninstall(pid)
        return {"ok": True}

    @app.put("/api/plugins/{pid}/config", dependencies=[api])
    def plugins_config(pid: str, body: dict = Body(...)) -> dict:
        eng.plugins.set_config(pid, body)
        return eng.plugins.info(pid)

    @app.post("/api/plugins/{pid}/test", dependencies=[api])
    def plugins_test(pid: str) -> dict:
        return {"message": eng.plugins.test(pid)}

    @app.post("/api/plugins/{pid}/connect", dependencies=[api])
    def plugins_connect(pid: str) -> dict:
        return {"url": eng.plugins.begin_oauth(pid, eng.base_url.rstrip("/") + "/oauth/callback")}

    @app.post("/api/plugins/{pid}/disconnect", dependencies=[api])
    def plugins_disconnect(pid: str) -> dict:
        eng.plugins.clear_secrets(pid)
        return eng.plugins.info(pid)

    @app.post("/api/plugins/rest", dependencies=[api])
    def plugins_rest(body: dict = Body(...)) -> dict:
        return eng.plugins.add_rest(body)

    @app.get("/oauth/callback", response_class=HTMLResponse)
    def oauth_callback(code: str = "", state: str = "", error: str = "") -> str:
        if error or not code:
            return f"<h3>Sign-in failed</h3><p>{error or 'No code returned.'}</p>"
        try:
            pid = eng.plugins.finish_oauth(state, code)
            eng.events.publish("plugins", change="connected", plugin_id=pid)
            return "<body style='font-family:sans-serif;background:#111;color:#eee;padding:40px'><h2>Connected ✔</h2><p>You can close this tab and return to OpenGrokBot.</p></body>"
        except Exception as e:  # noqa: BLE001
            return f"<h3>Sign-in failed</h3><p>{e}</p>"

    @app.get("/api/mcp", dependencies=[api])
    def mcp_list() -> list[dict]:
        return eng.mcp.configs()

    @app.post("/api/mcp", dependencies=[api])
    def mcp_save(body: dict = Body(...)) -> dict:
        if body.get("import_json"):
            return {"servers": eng.mcp.import_json(body["import_json"])}
        return eng.mcp.save(body)

    @app.delete("/api/mcp/{sid}", dependencies=[api])
    def mcp_delete(sid: str) -> dict:
        eng.mcp.delete(sid)
        return {"ok": True}

    @app.post("/api/mcp/{sid}/restart", dependencies=[api])
    def mcp_restart(sid: str) -> dict:
        eng.mcp.restart(sid)
        return {"ok": True}

    # ----------------------------------------------------- settings / secrets
    @app.get("/api/settings", dependencies=[api])
    def settings_get() -> dict:
        return _public_settings()

    @app.put("/api/settings", dependencies=[api])
    def settings_put(body: dict = Body(...)) -> dict:
        old_headless = eng.settings.get("computer.headless")
        for k, v in body.items():
            if k in ("providers",) or k.startswith("plugin_cfg"):
                continue
            eng.settings.set(k, v)
        if eng.settings.get("computer.headless") != old_headless:
            eng.computer.browser.restart()
        return _public_settings()

    @app.get("/api/providers", dependencies=[api])
    def providers_list() -> list[dict]:
        return _profiles()

    @app.put("/api/providers/{pid}", dependencies=[api])
    def providers_put(pid: str, body: dict = Body(...)) -> dict:
        allowed = eng.admin.allowed_providers()
        if allowed is not None and pid not in allowed:
            raise ValueError("This provider is not allowed by your administrator.")
        profs = dict(eng.settings.get("providers", {}) or {})
        cur = dict(profs.get(pid) or PROVIDER_PRESETS.get(pid, {}))
        for k in ("label", "kind", "base_url", "model", "vision", "needs_key"):
            if k in body:
                cur[k] = body[k]
        if cur.get("kind") not in ("anthropic", "openai"):
            raise ValueError("kind must be 'anthropic' or 'openai' (any OpenAI-compatible endpoint).")
        cur.setdefault("label", pid)
        profs[pid] = cur
        eng.settings.set("providers", profs)
        if body.get("api_key"):
            secrets.set_secret(f"provider:{pid}", body["api_key"])
        if body.get("make_default"):
            eng.settings.set("default_profile", pid)
        return next(p for p in _profiles() if p["id"] == pid)

    @app.delete("/api/providers/{pid}", dependencies=[api])
    def providers_delete(pid: str) -> dict:
        profs = dict(eng.settings.get("providers", {}) or {})
        profs.pop(pid, None)
        eng.settings.set("providers", profs)
        secrets.delete_secret(f"provider:{pid}")
        return {"ok": True}

    @app.post("/api/providers/{pid}/test", dependencies=[api])
    def providers_test(pid: str) -> dict:
        prov = make_provider(eng.settings, pid, eng.settings.profile(pid).get("model") or "test")
        if prov.profile.get("needs_key", True) and not prov.api_key:
            raise ValueError("No API key saved for this provider yet.")
        return {"models": prov.list_models()}

    @app.put("/api/secrets/{name}", dependencies=[api])
    def secret_put(name: str, body: dict = Body(...)) -> dict:
        if not name.startswith(SECRET_PREFIXES):
            raise ValueError("Unsupported secret name.")
        value = str(body.get("value", ""))
        if value:
            secrets.set_secret(name, value)
        else:
            secrets.delete_secret(name)
        return {"ok": True, "set": bool(value)}

    @app.get("/api/usage", dependencies=[api])
    def usage() -> dict:
        return eng.usage.summary()

    @app.get("/api/network", dependencies=[api])
    def network(bot_id: str | None = None) -> list[dict]:
        return eng.net.describe(bot_id)

    @app.get("/api/admin", dependencies=[api])
    def admin() -> dict:
        eng.admin.reload()
        return eng.admin.summary()

    # ----------------------------------------------------------- action log
    @app.get("/api/actions", dependencies=[api])
    def actions(bot_id: str | None = None, limit: int = 200, offset: int = 0, tool: str | None = None, status: str | None = None) -> list[dict]:
        return eng.actions.list(bot_id, limit, offset, tool, status)

    @app.get("/api/actions/export", dependencies=[api])
    def actions_export(bot_id: str | None = None, format: str = "json") -> Response:
        data, mime, name = eng.actions.export(bot_id, format)
        return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # --------------------------------------------------------- notifications
    @app.get("/api/notifications", dependencies=[api])
    def notifications(limit: int = 50, after: int = 0) -> list[dict]:
        return eng.db.query("SELECT n.*, COALESCE(b.name,'') AS bot_name FROM notifications n LEFT JOIN bots b ON b.id=n.bot_id WHERE n.id>? ORDER BY n.id DESC LIMIT ?", (after, limit))

    @app.post("/api/notifications/test", dependencies=[api])
    def notifications_test() -> dict:
        eng.notify("test", None, None, "OpenGrokBot test", "Notifications are working.", urgent=True)
        return {"ok": True}

    @app.post("/api/notifications/read", dependencies=[api])
    def notifications_read() -> dict:
        eng.db.execute("UPDATE notifications SET read=1")
        return {"ok": True}

    # -------------------------------------------------------------- mobile
    @app.get("/api/mobile", dependencies=[api])
    def mobile(request: Request) -> dict:
        port = int(eng.settings.get("mobile.port", 8765))
        host = eng.settings.get("mobile.host", "127.0.0.1")
        ips = lan_addresses()
        return {"host": host, "port": port, "lan": host != "127.0.0.1",
                "urls": [f"http://{ip}:{port}/?token={token}" for ip in ips], "local_url": f"http://127.0.0.1:{port}/?token={token}",
                "token": token, "ntfy": {"url": eng.settings.get("notifications.ntfy_url"), "topic": eng.settings.get("notifications.ntfy_topic")}}

    @app.post("/api/service/stop", dependencies=[api])
    def service_stop() -> dict:
        import os
        import threading
        threading.Timer(0.5, lambda: os._exit(0)).start()
        return {"ok": True}

    # --------------------------------------------------------------- events
    @app.get("/api/events", dependencies=[api])
    async def events(request: Request, since: int = 0, kind: str = "desktop") -> StreamingResponse:
        sid, q = eng.events.subscribe(kind, since)

        async def gen():
            loop = asyncio.get_running_loop()
            try:
                yield "retry: 2000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await loop.run_in_executor(None, lambda: q.get(timeout=10))
                    except queue.Empty:
                        yield ": keepalive\n\n"
                        continue
                    yield f"id: {ev['id']}\ndata: {json.dumps(ev, default=str)}\n\n"
            finally:
                eng.events.unsubscribe(sid)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ API tokens
    @app.get("/api/tokens", dependencies=[api])
    def tokens_list() -> dict:
        return {"tokens": eng.apitokens.list(), "scopes": list(apitokens.SCOPES)}

    @app.post("/api/tokens", dependencies=[api])
    def tokens_create(body: dict = Body(...)) -> dict:
        return eng.apitokens.create(str(body.get("name", "")), str(body.get("scope", "read")), int(body.get("days", 0) or 0))

    @app.delete("/api/tokens/{tid}", dependencies=[api])
    def tokens_revoke(tid: str, delete: bool = False) -> dict:
        (eng.apitokens.delete if delete else eng.apitokens.revoke)(tid)
        return {"ok": True}

    # ----------------------------------------------------------------- file history
    @app.get("/api/ws/history", dependencies=[api])
    def ws_history(path: str) -> dict:
        return {"versions": eng.filehistory.versions(path), "stats": eng.filehistory.stats()}

    @app.get("/api/ws/version", dependencies=[api])
    def ws_version(id: int) -> dict:
        return eng.filehistory.preview(id)

    @app.post("/api/ws/restore", dependencies=[api])
    def ws_restore(body: dict = Body(...)) -> dict:
        return eng.filehistory.restore(str(body.get("path", "")), int(body.get("version_id", 0)))

    @app.get("/api/ws/deleted", dependencies=[api])
    def ws_deleted() -> dict:
        names = {b["id"]: b["name"] for b in eng.bots.list(include_archived=True)}
        rows = eng.filehistory.deleted()
        for r in rows:
            r["bot_name"] = names.get(r["bot_id"], "") if r["bot_id"] else ""
        return {"entries": rows}

    # ------------------------------------------------------------- knowledge base
    @app.get("/api/knowledge", dependencies=[api])
    def knowledge_list(bot_id: str | None = None) -> dict:
        return {"sources": eng.knowledge.sources(bot_id), "stats": eng.knowledge.stats()}

    @app.post("/api/knowledge/note", dependencies=[api])
    def knowledge_note(body: dict = Body(...)) -> dict:
        return eng.knowledge.add_text(str(body.get("name", "")), str(body.get("text", "")), str(body.get("bot_id", "") or ""))

    @app.post("/api/knowledge/upload", dependencies=[api])
    def knowledge_upload(body: dict = Body(...)) -> dict:
        import base64
        try:
            data = base64.b64decode(str(body.get("data_b64", "")), validate=False)
        except ValueError:
            raise HTTPException(400, "The file data was not valid.")
        return eng.knowledge.add_upload(str(body.get("filename", "document.txt")), data, str(body.get("bot_id", "") or ""))

    @app.post("/api/knowledge/workspace", dependencies=[api])
    def knowledge_workspace(body: dict = Body(...)) -> dict:
        try:
            added = eng.knowledge.add_workspace(eng.files, str(body.get("path", "")), str(body.get("bot_id", "") or ""))
        except FileError as e:
            raise HTTPException(400, str(e))
        return {"added": added}

    @app.post("/api/knowledge/refresh", dependencies=[api])
    def knowledge_refresh() -> dict:
        return {"changed": eng.knowledge.refresh(eng.files)}

    @app.get("/api/knowledge/search", dependencies=[api])
    def knowledge_search(q: str = "", bot_id: str | None = None, limit: int = 8) -> dict:
        return {"hits": eng.knowledge.search(q, bot_id, limit)}

    @app.get("/api/knowledge/{sid}", dependencies=[api])
    def knowledge_read(sid: str, seq: int | None = None) -> dict:
        return eng.knowledge.read(sid, seq)

    @app.delete("/api/knowledge/{sid}", dependencies=[api])
    def knowledge_delete(sid: str) -> dict:
        eng.knowledge.remove(sid)
        return {"ok": True}

    @app.get("/api/poll", dependencies=[api])
    def poll(after: int = 0) -> dict:
        evs = eng.events.since(after)
        return {"events": evs[-200:], "last": evs[-1]["id"] if evs else after}

    # -------------------------------------------------------------- the PWA
    web = paths.resource_root() / "web"

    @app.get("/")
    def index(request: Request) -> Response:
        t = request.query_params.get("token")
        if t:
            ip = request.client.host if request.client else "?"
            if check_token(t, ip):
                r = RedirectResponse("/", status_code=303)
                r.set_cookie("gb_token", token, httponly=True, samesite="strict", max_age=86400 * 365)
                return r
        return FileResponse(web / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/{name:path}")
    def static(name: str) -> Response:
        f = (web / name).resolve()
        if not name or not f.is_file() or not f.is_relative_to(web.resolve()):
            raise HTTPException(404)
        return FileResponse(f, headers={"Cache-Control": "no-cache"})

    return app
