"""Shared types for tools (built-in, plugin connectors, MCP)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover
    from .agent import AgentRun
    from .engine import Engine


@dataclass
class Risk:
    """A consequential action that needs approval (or auto review)."""
    category: str            # send | submit | purchase | delete | overwrite | command | outside_workspace | login | access | schedule | write | download
    summary: str
    details: dict = field(default_factory=dict)
    never_auto: bool = False  # categories the reviewer model may never auto-approve


@dataclass
class ToolResult:
    """Result of a tool call.

    `text` is trusted output from our own code. `data` is external content (web pages, files, emails)
    and is always wrapped as untrusted. If only `untrusted` is set, the whole `text` is treated as data.
    """
    text: str = ""
    data: str = ""
    images: list[str] = field(default_factory=list)   # file paths
    is_error: bool = False
    url: str = ""
    path: str = ""
    untrusted: str = ""   # label of the external source, e.g. "web page example.com"


@dataclass
class ToolContext:
    engine: "Engine"
    bot: dict
    thread_id: str
    turn_id: str
    run: "AgentRun"

    @property
    def stopped(self) -> bool:
        return self.run.stop.is_set()


ToolHandler = Callable[[ToolContext, dict], ToolResult]
RiskFn = Callable[[ToolContext, dict], "Risk | None"]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: ToolHandler
    risk: RiskFn | None = None
    label: Callable[[dict], str] | None = None
    group: str = "core"        # core | computer | plugin:<id> | mcp:<id>
    read_only: bool = False
    needs_screen: bool = False  # uses the Bot's screen (one computer-use task at a time)

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def obj(props: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props or {}, "required": required or []}


def s(desc: str, **kw: Any) -> dict:
    return {"type": "string", "description": desc, **kw}


def i(desc: str, **kw: Any) -> dict:
    return {"type": "integer", "description": desc, **kw}


def b(desc: str) -> dict:
    return {"type": "boolean", "description": desc}


def safe_name(name: str) -> str:
    """Tool names must match ^[a-zA-Z0-9_-]{1,64}$ for both major providers."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:64]


def short(v: Any, n: int = 80) -> str:
    t = str(v).replace("\n", " ")
    return t if len(t) <= n else t[: n - 1] + "…"
