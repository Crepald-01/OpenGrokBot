"""Skill library: ready-made skills you can browse and install.

The app ships with the library bundled (`skills/library/*.md`), so browsing works offline. "Check for new skills"
fetches `catalog.json` from the project site (only when you click it) and can offer newer versions or extra skills.
Every installed skill lands as a DRAFT: you read it, then activate it. Downloads are checked against the SHA-256
in the catalog, and skills are plain text, never code.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from . import paths
from .skills import Skills, parse, slug

BASE = "https://crepald-01.github.io/OpenGrokBot/library/"
MAX_BYTES = 200_000

# Words that deserve a second look before a skill is activated.
FLAGS = [
    (re.compile(r"\b(without|skip|bypass|disable)\b[^.\n]{0,30}\bapprov", re.I), "talks about skipping approvals"),
    (re.compile(r"\b(password|passcode|api[ _-]?key|token|secret|credential)s?\b", re.I), "mentions credentials or secrets"),
    (re.compile(r"https?://", re.I), "contains links"),
    (re.compile(r"\b(send|submit|purchase|pay|delete|post|publish)\b", re.I), "can send, spend, delete or publish"),
]


class LibraryError(ValueError):
    pass


def sha(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def flags_for(text: str) -> list[str]:
    return [msg for rx, msg in FLAGS if rx.search(text)]


def entry_for(name: str, raw: str) -> dict:
    meta, _ = parse(raw)
    return {"name": slug(meta.get("name") or name), "description": meta.get("description", ""), "tags": meta.get("tags", ""),
            "version": int(re.sub(r"\D", "", meta.get("version", "1")) or 1), "sha256": sha(raw), "file": f"{slug(meta.get('name') or name)}.md"}


def build_catalog(folder: Path) -> list[dict]:
    out = []
    for f in sorted(folder.glob("*.md")):
        out.append(entry_for(f.stem, f.read_text(encoding="utf-8")))
    return out


class Library:
    def __init__(self, skills: Skills) -> None:
        self.skills = skills
        self.bundled = paths.resource_root() / "skills" / "library"
        self.cache = paths.data_dir() / "library_catalog.json"

    # ---------------------------------------------------------------- catalog
    def _remote_cached(self) -> dict:
        try:
            return json.loads(self.cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def entries(self) -> list[dict]:
        rows: dict[str, dict] = {}
        if self.bundled.is_dir():
            for e in build_catalog(self.bundled):
                rows[e["name"]] = {**e, "origin": "bundled"}
        remote = self._remote_cached()
        for e in remote.get("skills", []):
            if not isinstance(e, dict) or not e.get("name") or not e.get("sha256"):
                continue
            cur = rows.get(e["name"])
            if not cur or int(e.get("version", 1)) > cur["version"]:
                rows[e["name"]] = {"name": slug(e["name"]), "description": str(e.get("description", ""))[:200], "tags": str(e.get("tags", "")),
                                   "version": int(e.get("version", 1)), "sha256": e["sha256"], "file": str(e.get("file") or f"{slug(e['name'])}.md"),
                                   "origin": "online"}
        mine = {i["name"]: i for i in self.skills.list()}
        out = []
        for e in sorted(rows.values(), key=lambda r: r["name"]):
            have = mine.get(e["name"])
            ver = 0
            if have:
                m = re.match(r"library:v?(\d+)", have.get("source", ""))
                ver = int(m.group(1)) if m else 0
            state = "available"
            if have:
                state = "update" if (ver and e["version"] > ver) else "installed"
            out.append({**e, "state": state, "installed_status": have["status"] if have else ""})
        return out

    def refresh(self) -> dict:
        """Fetch the online catalog. Only called when the user asks."""
        import httpx
        try:
            r = httpx.get(BASE + "catalog.json", headers={"User-Agent": "opengrokbot-library"}, timeout=10.0, follow_redirects=True)
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # noqa: BLE001
            raise LibraryError(f"Could not reach the skill library: {e}") from e
        if not isinstance(data, dict) or not isinstance(data.get("skills"), list):
            raise LibraryError("The skill library returned something unexpected.")
        self.cache.write_text(json.dumps({"fetched": time.time(), "skills": data["skills"][:500]}), encoding="utf-8")
        return {"count": len(self.entries()), "fetched": time.time()}

    # ---------------------------------------------------------------- text
    def _text(self, e: dict) -> str:
        if e["origin"] == "bundled":
            f = self.bundled / f"{e['name']}.md"
            try:
                raw = f.read_text(encoding="utf-8")
            except OSError as ex:
                raise LibraryError("That bundled skill is missing.") from ex
        else:
            import httpx
            try:
                r = httpx.get(BASE + e["file"], headers={"User-Agent": "opengrokbot-library"}, timeout=15.0, follow_redirects=True)
                r.raise_for_status()
                raw = r.text
            except Exception as ex:  # noqa: BLE001
                raise LibraryError(f"Download failed: {ex}") from ex
        if len(raw.encode("utf-8")) > MAX_BYTES:
            raise LibraryError("That skill is too large.")
        if sha(raw) != e["sha256"]:
            raise LibraryError("Checksum mismatch: the file does not match the catalog, so it was not installed.")
        return raw

    def _find(self, name: str) -> dict:
        for e in self.entries():
            if e["name"] == slug(name):
                return e
        raise LibraryError(f"No library skill named {name}.")

    def preview(self, name: str) -> dict:
        e = self._find(name)
        raw = self._text(e)
        return {**e, "raw": raw, "flags": flags_for(raw)}

    def install(self, name: str) -> dict:
        e = self._find(name)
        raw = self._text(e)
        return self._save_draft(e["name"], raw, f"library:v{e['version']}")

    def _save_draft(self, name: str, raw: str, source: str) -> dict:
        meta, body = parse(raw)
        meta["name"] = slug(meta.get("name") or name)
        meta["status"] = "draft"  # never active on arrival
        meta["source"] = source
        from .skills import dump
        return self.skills.save(meta["name"], dump(meta, body))

    # ---------------------------------------------------------------- import
    def import_text(self, raw: str, source: str = "import") -> dict:
        raw = (raw or "").strip()
        if not raw or len(raw.encode("utf-8")) > MAX_BYTES:
            raise LibraryError("The skill is empty or too large.")
        meta, body = parse(raw)
        if not meta.get("name") and not meta.get("description"):
            raise LibraryError("A skill needs a front-matter block with at least a name and description.")
        return self._save_draft(meta.get("name") or "imported-skill", raw, source)

    def import_url(self, url: str) -> dict:
        u = urlparse(url or "")
        if u.scheme != "https" or not u.hostname:
            raise LibraryError("Only https links can be imported.")
        import httpx
        try:
            r = httpx.get(url, headers={"User-Agent": "opengrokbot-library"}, timeout=15.0, follow_redirects=True)
            r.raise_for_status()
        except Exception as ex:  # noqa: BLE001
            raise LibraryError(f"Download failed: {ex}") from ex
        if len(r.content) > MAX_BYTES:
            raise LibraryError("That file is too large.")
        return self.import_text(r.text, f"url:{u.hostname}")

    def import_file(self, path: str) -> list[dict]:
        p = Path(path)
        if p.suffix.lower() == ".zip":
            out = []
            with zipfile.ZipFile(p) as z:
                for info in z.infolist()[:100]:
                    if info.filename.lower().endswith(".md") and info.file_size <= MAX_BYTES and not info.is_dir():
                        out.append(self.import_text(z.read(info).decode("utf-8", "replace"), f"file:{p.name}"))
            if not out:
                raise LibraryError("No skills (.md files) found in that zip.")
            return out
        if p.stat().st_size > MAX_BYTES:
            raise LibraryError("That file is too large.")
        return [self.import_text(p.read_text(encoding="utf-8", errors="replace"), f"file:{p.name}")]

    def export_zip(self, dest: str, names: list[str] | None = None) -> int:
        n = 0
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            for s in self.skills.list(with_body=True):
                if names and s["name"] not in names:
                    continue
                z.writestr(f"{s['name']}.md", s["raw"])
                n += 1
        return n
