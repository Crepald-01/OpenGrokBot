"""Share a Bot as a package: role, skills, routines. Never secrets, never conversation history."""
from __future__ import annotations

import io
import json
import time
import zipfile
from typing import TYPE_CHECKING

from . import secrets
from .db import jload
from .skills import slug

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

FORMAT = 1
MAX_PACKAGE = 5 * 1024 * 1024


def export_bot(engine: "Engine", bot_id: str, include_memory: bool = False) -> bytes:
    bot = engine.bots.get(bot_id)
    if not bot:
        raise ValueError("No such Bot.")
    redacted = 0

    def clean(text: str) -> str:
        nonlocal redacted
        out = secrets.redact(text or "")
        if out != (text or ""):
            redacted += 1
        return out

    routines = engine.routines.list(bot_id)
    skill_names = {r["skill"] for r in routines if r["skill"]}
    skills = {s["name"]: s for s in engine.skills.list(with_body=True) if s["name"] in skill_names or s["bot"].lower() == bot["name"].lower()}
    manifest = {
        "format": FORMAT, "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "bot": {k: clean(bot[k]) if isinstance(bot[k], str) else bot[k] for k in
                ("name", "emoji", "job", "instructions", "approval_mode", "step_limit", "net_mode", "net_allow", "net_deny", "proactive", "template")},
        "connectors_needed": sorted(g for g in bot["grants"]),
        "routines": [{"name": r["name"], "skill": r["skill"], "prompt": clean(r["prompt"]), "cron": r["cron"], "dry_run": bool(r["dry_run"]),
                      "notify": r["notify"], "catch_up": bool(r["catch_up"])} for r in routines],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("bot.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        for name, s in skills.items():
            z.writestr(f"skills/{slug(name)}.md", clean(s["raw"]))
        if include_memory:
            mem = [{"kind": m["kind"], "text": clean(m["text"])} for m in engine.memory.list(bot_id) if m["kind"] in ("role", "preference", "voice", "edge_case")]
            z.writestr("memory.json", json.dumps(mem, indent=2, ensure_ascii=False))
        z.writestr("README.txt", "OpenGrokBot Bot package. Contains the Bot's role, skills and routines. It contains no secrets, credentials or conversations.\n"
                                 "Import it from Bots > Import. Review skills and routines before enabling them; grant connectors again on your own account.\n")
    return buf.getvalue()


def import_bot(engine: "Engine", data: bytes, rename: str | None = None) -> dict:
    if len(data) > MAX_PACKAGE:
        raise ValueError("Package is too large.")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError("That file is not a Bot package.") from e
    names = z.namelist()
    if any(n.startswith("/") or ".." in n.split("/") for n in names) or sum(i.file_size for i in z.infolist()) > 20 * 1024 * 1024:
        raise ValueError("Package contents are not safe to import.")
    if "bot.json" not in names:
        raise ValueError("Package has no bot.json.")
    m = json.loads(z.read("bot.json").decode("utf-8"))
    if m.get("format") != FORMAT:
        raise ValueError(f"Unsupported package format {m.get('format')}.")
    b = m["bot"]
    name = (rename or b["name"]).strip()
    base, n = name, 1
    while engine.bots.get_by_name(name):
        n += 1
        name = f"{base} ({'copy' if n == 2 else 'copy ' + str(n - 1)})"
    bot = engine.bots.create(name, job=b.get("job", ""), instructions=b.get("instructions", ""), emoji=b.get("emoji", "🤖"),
                             approval_mode=b.get("approval_mode", "ask"), step_limit=b.get("step_limit"), net_mode=b.get("net_mode", "inherit"),
                             net_allow=b.get("net_allow", []), net_deny=b.get("net_deny", []), proactive=b.get("proactive", "off"), template=b.get("template", ""))
    skills_in = []
    for n_ in names:
        if n_.startswith("skills/") and n_.endswith(".md"):
            raw = secrets.redact(z.read(n_).decode("utf-8", "replace"))
            sk = engine.skills.save(slug(n_[7:-3]), raw, status="draft")   # imported skills are drafts until you review them
            skills_in.append(sk["name"])
    routines_in = []
    for r in m.get("routines", []):
        try:
            routines_in.append(engine.routines.create(bot["id"], r["name"], r["cron"], skill=r.get("skill", "") if r.get("skill") in skills_in else "",
                                                      prompt=r.get("prompt", ""), enabled=False, dry_run=bool(r.get("dry_run")),
                                                      notify=r.get("notify", "always"), catch_up=bool(r.get("catch_up")))["name"])
        except Exception:
            continue
    if "memory.json" in names:
        for mem in jload(z.read("memory.json").decode("utf-8"), []):
            engine.memory.add(bot["id"], mem.get("kind", "fact"), mem.get("text", ""), source="import")
    return {"bot": bot, "skills": skills_in, "routines": routines_in, "connectors_needed": m.get("connectors_needed", [])}
