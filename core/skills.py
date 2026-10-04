"""Skills: reviewable, editable markdown files describing how to do a job.

A skill is `name.md` with a small frontmatter block:

    ---
    name: inbox-triage
    description: One line that tells a Bot when to use this skill
    status: active          # draft skills are not offered to Bots until you activate them
    bot:                    # optional: restrict to one Bot (by name)
    tags: email, daily
    ---
    # Steps ...
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import paths
from .providers import ProviderError, make_provider

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

NAME_RE = re.compile(r"[^a-z0-9_\-]+")


def slug(name: str) -> str:
    s = NAME_RE.sub("-", (name or "").strip().lower()).strip("-")
    return s[:60] or "skill"


def parse(raw: str) -> tuple[dict, str]:
    meta: dict[str, str] = {}
    body = raw
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", raw, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip()
        body = m.group(2)
    return meta, body


def dump(meta: dict, body: str) -> str:
    keys = ["name", "description", "status", "bot", "tags", "source", "updated"]
    lines = ["---"] + [f"{k}: {meta.get(k, '')}" for k in keys if k in meta or k in ("name", "description", "status")]
    lines += ["---", "", body.strip(), ""]
    return "\n".join(lines)


class Skills:
    def __init__(self) -> None:
        self.dir = paths.skills_dir()
        self.seed()

    def seed(self) -> None:
        """Copy bundled starter skills into the user's editable folder (never overwrites)."""
        src = paths.resource_root() / "skills"
        if not src.is_dir():
            return
        for f in src.glob("*.md"):
            dst = self.dir / f.name
            if not dst.exists():
                shutil.copyfile(f, dst)

    def _path(self, name: str) -> Path:
        return self.dir / f"{slug(name)}.md"

    def _info(self, f: Path) -> dict:
        raw = f.read_text(encoding="utf-8", errors="replace")
        meta, body = parse(raw)
        return {"name": meta.get("name") or f.stem, "file": f.name, "description": meta.get("description", ""),
                "status": meta.get("status", "active") or "active", "bot": meta.get("bot", ""), "tags": meta.get("tags", ""),
                "source": meta.get("source", ""), "updated": f.stat().st_mtime, "body": body, "raw": raw}

    def list(self, status: str | None = None, bot_name: str | None = None, with_body: bool = False) -> list[dict]:
        out = []
        for f in sorted(self.dir.glob("*.md")):
            try:
                info = self._info(f)
            except OSError:
                continue
            if status and info["status"] != status:
                continue
            if bot_name is not None and info["bot"] and info["bot"].lower() != bot_name.lower():
                continue
            if not with_body:
                info = {k: v for k, v in info.items() if k not in ("body", "raw")}
            out.append(info)
        return out

    def get(self, name: str) -> dict | None:
        f = self._path(name)
        if not f.exists():
            # tolerate name != filename
            for i in self.list(with_body=True):
                if i["name"].lower() == name.lower():
                    return i
            return None
        return self._info(f)

    def save(self, name: str, raw: str | None = None, *, description: str = "", body: str = "", status: str = "draft",
             bot: str = "", source: str = "") -> dict:
        if raw is not None:
            meta, body = parse(raw)
            meta.setdefault("name", slug(name))
        else:
            meta = {"name": slug(name), "description": description, "status": status, "bot": bot, "source": source}
        meta["name"] = slug(meta.get("name") or name)
        meta["status"] = meta.get("status") if meta.get("status") in ("draft", "active") else status
        meta["updated"] = time.strftime("%Y-%m-%d %H:%M")
        self._path(meta["name"]).write_text(dump(meta, body), encoding="utf-8")
        return self.get(meta["name"])  # type: ignore

    def set_status(self, name: str, status: str) -> dict | None:
        s = self.get(name)
        if not s:
            return None
        meta, body = parse(s["raw"])
        meta["status"] = status
        return self.save(name, dump(meta, body))

    def delete(self, name: str) -> bool:
        s = self.get(name)
        if not s:
            return False
        (self.dir / s["file"]).unlink(missing_ok=True)
        return True

    def index_for_bot(self, bot_name: str) -> str:
        items = self.list(status="active", bot_name=bot_name)
        if not items:
            return "(no skills yet. When you work out a repeatable procedure, save it with skill_save.)"
        return "\n".join(f"- {i['name']}: {i['description']}" for i in items)


RECORDING_PROMPT = """You are turning an observed demonstration into a reusable skill that an AI agent can follow later.

The user performed a job in a shared browser while you watched. Below are the recorded steps (clicks, typed text, navigation,
and any notes the user narrated). Secrets were not recorded. Write the skill as markdown with sections:
# <Title>
## When to use
## Inputs (what the agent must ask for or look up each time)
## Steps (numbered, at the level of intent, not pixels; mention page URLs and visible button/field labels, never CSS selectors)
## Checks (how to verify it worked)
## Needs approval (steps that send, submit, purchase, delete or log in; the agent must stop for approval there)
## Edge cases (anything the notes or steps suggest)

Rules: do not invent steps that were not shown; mark guesses with (assumed). Output ONLY the markdown body, no frontmatter."""


def steps_to_text(steps: list[dict]) -> str:
    lines = []
    for i, s in enumerate(steps, 1):
        a = s.get("action")
        if a == "navigate":
            lines.append(f"{i}. Opened {s.get('url')}")
        elif a == "click":
            lines.append(f"{i}. Clicked {s.get('tag', 'element')} \"{s.get('label', '')}\" on {s.get('pageUrl', '')}")
        elif a == "type":
            lines.append(f"{i}. Typed \"{s.get('value', '')}\" into {s.get('label') or s.get('name') or s.get('tag', 'field')}")
        elif a == "select":
            lines.append(f"{i}. Selected \"{s.get('value', '')}\" in {s.get('label', 'a dropdown')}")
        elif a == "toggle":
            lines.append(f"{i}. Toggled {s.get('label', 'a checkbox')}")
        elif a == "press":
            lines.append(f"{i}. Pressed {s.get('key')} in {s.get('label', s.get('tag', ''))}")
        elif a == "submit":
            lines.append(f"{i}. Submitted a form ({s.get('label', '')})")
        elif a == "note":
            lines.append(f"{i}. USER NOTE: {s.get('text')}")
    return "\n".join(lines) or "(no steps recorded)"


def draft_from_recording(engine: "Engine", bot: dict, rec: dict, name: str | None = None) -> dict:
    steps = rec.get("steps") or []
    if not steps:
        raise ValueError("That recording has no steps.")
    provider = make_provider(engine.settings, bot.get("profile") or None, bot.get("model") or None)
    text = steps_to_text(steps)[:30000]
    try:
        res = provider.stream(RECORDING_PROMPT, [{"role": "user", "content": [{"type": "text", "text": f"Demonstration \"{rec['name']}\":\n{text}"}]}],
                              [], lambda _t: None, lambda: False, max_tokens=3000)
    except ProviderError as e:
        raise ValueError(str(e)) from e
    engine.usage.record(bot["id"], "", provider.profile.get("id", ""), provider.model, res.input_tokens, res.output_tokens)
    skill_name = slug(name or rec["name"])
    first = res.text.strip().splitlines()[0].lstrip("# ").strip() if res.text.strip() else rec["name"]
    skill = engine.skills.save(skill_name, description=f"{first[:100]} (learned from demonstration)", body=res.text.strip(),
                               status="draft", bot=bot["name"], source="recording")
    engine.db.update("recordings", rec["id"], {"skill_name": skill["name"], "status": "drafted"})
    return skill
