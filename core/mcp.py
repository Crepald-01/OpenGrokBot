"""MCP client support. Each configured server runs as a long-lived task on a dedicated asyncio loop.

Tools are exposed to Bots as `mcp__<server>__<tool>`. A Bot only sees an MCP server's tools once the user
has granted that server to it ("grant access as it asks").
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re
import threading
import time
from contextlib import AsyncExitStack
from typing import Any, Callable

from . import secrets
from .db import Database, jdump, jload, new_id
from .settings import Admin
from .tooling import Risk, ToolContext, ToolResult, ToolSpec, safe_name, short


def _g(obj: Any, *names: str, default: Any = None) -> Any:
    """Attribute/key access tolerant of snake_case vs camelCase across MCP SDK versions."""
    for n in names:
        if isinstance(obj, dict):
            if n in obj and obj[n] is not None:
                return obj[n]
        elif getattr(obj, n, None) is not None:
            return getattr(obj, n)
    return default


def resolve_secret_refs(value: str) -> str:
    """Replace ${secret:NAME} with a value from the credential store."""
    def sub(m: re.Match) -> str:
        return secrets.get_secret(m.group(1)) or ""
    return re.sub(r"\$\{secret:([^}]+)\}", sub, value)


class McpConnection:
    def __init__(self, cfg: dict, loop: asyncio.AbstractEventLoop):
        self.cfg, self.loop = cfg, loop
        self.session: Any = None
        self.tools: list[Any] = []
        self.error = ""
        self.status = "starting"
        self.ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Future | None = None

    def start(self) -> None:
        self._task = asyncio.run_coroutine_threadsafe(self._run(), self.loop)

    async def _run(self) -> None:
        self._stop = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                cfg = self.cfg
                if cfg["transport"] == "stdio":
                    from mcp import StdioServerParameters, stdio_client
                    env = {k: resolve_secret_refs(str(v)) for k, v in (jload(cfg["env"], {}) or {}).items()}
                    import os
                    full_env = {**{k: v for k, v in os.environ.items() if not re.search(r"(KEY|TOKEN|SECRET|PASSWORD)", k, re.I)}, **env}
                    params = StdioServerParameters(command=cfg["command"], args=[resolve_secret_refs(a) for a in jload(cfg["args"], [])],
                                                   env=full_env)
                    streams = await stack.enter_async_context(stdio_client(params))
                else:
                    headers = {k: resolve_secret_refs(str(v)) for k, v in (jload(cfg["headers"], {}) or {}).items()}
                    if cfg["transport"] == "sse":
                        from mcp.client.sse import sse_client
                        streams = await stack.enter_async_context(sse_client(cfg["url"], headers=headers or None))
                    else:
                        streams = await stack.enter_async_context(self._http_streams(cfg["url"], headers))
                read, write = streams[0], streams[1]
                from mcp import ClientSession
                session = await stack.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(session.initialize(), 60)
                self.tools = list((await session.list_tools()).tools)
                self.session = session
                self.status, self.error = "connected", ""
                self.ready.set()
                await self._stop.wait()
        except asyncio.CancelledError:
            pass
        except BaseException as e:  # noqa: BLE001 - anyio ExceptionGroups included
            self.status = "error"
            msg = str(e)
            if hasattr(e, "exceptions") and getattr(e, "exceptions"):
                msg = "; ".join(str(x) for x in e.exceptions)  # type: ignore[attr-defined]
            if isinstance(e, FileNotFoundError):
                msg = f"Command not found: {self.cfg['command']}. Is it installed and on PATH?"
            self.error = msg[:500] or type(e).__name__
        finally:
            self.session = None
            if self.status == "connected":
                self.status = "stopped"
            self.ready.set()

    @staticmethod
    def _http_streams(url: str, headers: dict):
        try:
            from mcp.client.streamable_http import streamable_http_client as shc  # newer SDKs
            http_client = None
            if headers:
                import httpx2 as hx  # type: ignore
                http_client = hx.AsyncClient(headers=headers, timeout=60, follow_redirects=True)
            return shc(url, http_client=http_client)
        except ImportError:
            from mcp.client.streamable_http import streamablehttp_client  # older SDKs
            return streamablehttp_client(url, headers=headers or None)

    def stop(self) -> None:
        if self._stop is not None:
            self.loop.call_soon_threadsafe(self._stop.set)

    def call(self, name: str, args: dict, timeout: float = 120) -> Any:
        if self.session is None:
            raise RuntimeError(f"MCP server '{self.cfg['name']}' is not connected ({self.error or self.status}).")
        fut = asyncio.run_coroutine_threadsafe(self.session.call_tool(name, args), self.loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError as e:
            fut.cancel()
            raise RuntimeError("The MCP tool call timed out.") from e


class McpManager:
    def __init__(self, db: Database, admin: Admin):
        self.db, self.admin = db, admin
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run_loop, name="mcp-host", daemon=True).start()
        self.conns: dict[str, McpConnection] = {}

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    # -- config ---------------------------------------------------------------
    def configs(self) -> list[dict]:
        out = []
        for r in self.db.query("SELECT * FROM mcp_servers ORDER BY name"):
            r["args"], r["env"], r["headers"] = jload(r["args"], []), jload(r["env"], {}), jload(r["headers"], {})
            r["enabled"] = bool(r["enabled"])
            c = self.conns.get(r["id"])
            r["status"] = c.status if c else ("disabled" if not r["enabled"] else "stopped")
            r["error"] = c.error if c else ""
            r["tool_count"] = len(c.tools) if c else 0
            r["allowed"] = self.admin.allowed_mcp() is None or r["name"] in (self.admin.allowed_mcp() or []) or r["id"] in (self.admin.allowed_mcp() or [])
            out.append(r)
        return out

    def save(self, cfg: dict) -> dict:
        sid = cfg.get("id") or new_id()
        name = re.sub(r"[^A-Za-z0-9_-]+", "_", (cfg.get("name") or "server").strip())[:32] or "server"
        row = {"id": sid, "name": name, "transport": cfg.get("transport", "stdio"), "command": cfg.get("command", ""),
               "args": jdump(cfg.get("args", [])), "env": jdump(cfg.get("env", {})), "url": cfg.get("url", ""),
               "headers": jdump(cfg.get("headers", {})), "enabled": int(bool(cfg.get("enabled", True))), "trust": cfg.get("trust", "ask")}
        if row["transport"] not in ("stdio", "http", "sse"):
            raise ValueError("transport must be stdio, http or sse")
        if row["transport"] == "stdio" and not row["command"]:
            raise ValueError("A stdio server needs a command.")
        if row["transport"] != "stdio" and not row["url"]:
            raise ValueError("A remote server needs a URL.")
        if self.db.one("SELECT id FROM mcp_servers WHERE id=?", (sid,)):
            self.db.update("mcp_servers", sid, {k: v for k, v in row.items() if k != "id"})
        else:
            self.db.insert("mcp_servers", row)
        self.restart(sid)
        return next(c for c in self.configs() if c["id"] == sid)

    def import_json(self, text: str) -> list[dict]:
        """Accepts the common {"mcpServers": {name: {command,args,env | url,headers}}} format."""
        data = json.loads(text)
        servers = data.get("mcpServers") or data.get("servers") or data
        out = []
        for name, c in servers.items():
            if not isinstance(c, dict):
                continue
            transport = "stdio" if c.get("command") else ("sse" if c.get("type") == "sse" else "http")
            out.append(self.save({"name": name, "transport": transport, "command": c.get("command", ""), "args": c.get("args", []),
                                  "env": c.get("env", {}), "url": c.get("url", ""), "headers": c.get("headers", {})}))
        return out

    def delete(self, sid: str) -> None:
        self.stop(sid)
        self.db.execute("DELETE FROM mcp_servers WHERE id=?", (sid,))

    # -- lifecycle --------------------------------------------------------------
    def start_all(self) -> None:
        for c in self.configs():
            if c["enabled"] and c["allowed"]:
                self.start(c["id"])

    def start(self, sid: str) -> None:
        cfg = next((c for c in self.configs() if c["id"] == sid), None)
        if not cfg or not cfg["enabled"] or not cfg["allowed"]:
            return
        self.stop(sid)
        raw = self.db.one("SELECT * FROM mcp_servers WHERE id=?", (sid,))
        conn = McpConnection(raw, self.loop)  # type: ignore[arg-type]
        self.conns[sid] = conn
        conn.start()

    def stop(self, sid: str) -> None:
        c = self.conns.pop(sid, None)
        if c:
            c.stop()

    def restart(self, sid: str) -> None:
        self.stop(sid)
        self.start(sid)

    def shutdown(self) -> None:
        for sid in list(self.conns):
            self.stop(sid)
        time.sleep(0.3)
        self.loop.call_soon_threadsafe(self.loop.stop)

    # -- tools -------------------------------------------------------------------
    def tool_specs(self, granted: set[str], names_only: bool = False) -> list[ToolSpec]:
        specs: list[ToolSpec] = []
        for sid, conn in list(self.conns.items()):
            if conn.status != "connected" or f"mcp:{conn.cfg['name']}" not in granted:
                continue
            server = conn.cfg["name"]
            trust = conn.cfg.get("trust", "ask")
            for t in conn.tools:
                tname = _g(t, "name")
                ann = _g(t, "annotations")
                read_only = bool(_g(ann, "read_only_hint", "readOnlyHint", default=False)) if ann is not None else False
                schema = _g(t, "input_schema", "inputSchema", default={"type": "object", "properties": {}})
                full = safe_name(f"mcp__{server}__{tname}")
                desc = f"[MCP: {server}] " + (_g(t, "description", default="") or tname)

                def handler(ctx: ToolContext, args: dict, _c=conn, _n=tname, _srv=server) -> ToolResult:
                    res = _c.call(_n, args)
                    parts, images = [], []
                    for c in _g(res, "content", default=[]) or []:
                        ctype = _g(c, "type", default="")
                        if ctype == "text":
                            parts.append(_g(c, "text", default=""))
                        elif ctype == "image":
                            parts.append("[image returned by tool]")
                        elif ctype == "resource":
                            r = _g(c, "resource")
                            parts.append(_g(r, "text", default="[binary resource]") if r is not None else "")
                        else:
                            parts.append(str(c)[:500])
                    text = "\n".join(p for p in parts if p) or "(no output)"
                    return ToolResult(text, is_error=bool(_g(res, "is_error", "isError", default=False)), untrusted=f"MCP server {_srv}")

                def risk(ctx: ToolContext, args: dict, _ro=read_only, _tr=trust, _t=tname, _srv=server) -> Risk | None:
                    if _tr == "trusted" or (_tr == "readonly_auto" and _ro):
                        return None
                    return Risk("write", f"MCP tool {_srv}.{_t}({short(json.dumps(args), 160)})", {"server": _srv, "tool": _t, "pattern": f"{_srv}.{_t}"})

                specs.append(ToolSpec(full, desc[:1000], schema if isinstance(schema, dict) else {"type": "object"}, handler, risk=risk,
                                      label=lambda a, _t=tname, _s=server: f"{_s}: {_t}", group=f"mcp:{server}", read_only=read_only))
        return specs
