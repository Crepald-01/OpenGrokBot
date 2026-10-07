"""A safe window onto the shared workspace: browse, search, preview and delete the files Bots create.

Every path is resolved against the workspace root and refused if it points outside it (".." or a link out)."""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path

TEXT_EXT = {".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".log", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".htm", ".css", ".xml", ".yml", ".yaml",
            ".toml", ".ini", ".cfg", ".sh", ".bat", ".ps1", ".sql", ".rst", ".tex", ".env.example"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_TEXT = 200_000          # bytes of text shown in a preview
MAX_IMAGE = 6_000_000
MAX_WALK = 20_000           # files looked at when listing recent files or searching


class FileError(ValueError):
    pass


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    return "text" if ext in TEXT_EXT else "image" if ext in IMAGE_EXT else "other"


class Files:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    # -- paths ------------------------------------------------------------------
    def resolve(self, rel: str = "") -> Path:
        rel = (rel or "").replace("\\", "/").strip("/")
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise FileError("That path is outside the workspace.")
        return p

    def rel(self, p: Path) -> str:
        """Workspace-relative path with forward slashes. Uses the path as given (not its link target), so a link that
        points outside the workspace does not break listings; `resolve()` is what refuses to open such a link."""
        try:
            return p.relative_to(self.root).as_posix() if p != self.root else ""
        except ValueError:
            return p.resolve().relative_to(self.root).as_posix() if p.resolve() != self.root else ""

    def _inside(self, p: Path) -> bool:
        try:
            r = p.resolve()
        except OSError:
            return False
        return r == self.root or self.root in r.parents

    def _entry(self, p: Path) -> dict:
        if not self._inside(p):
            raise OSError("points outside the workspace")
        st = p.stat()
        d = p.is_dir()
        return {"name": p.name, "path": self.rel(p), "dir": d, "size": 0 if d else st.st_size, "mtime": st.st_mtime, "kind": "dir" if d else kind_of(p)}

    def _walk(self):
        n = 0
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                n += 1
                if n > MAX_WALK:
                    return
                yield Path(dirpath) / fn

    # -- browsing -----------------------------------------------------------------
    def list(self, rel: str = "") -> dict:
        p = self.resolve(rel)
        if not p.is_dir():
            raise FileError("That is not a folder.")
        entries = []
        for child in p.iterdir():
            if child.name.startswith("."):
                continue
            try:
                entries.append(self._entry(child))
            except OSError:
                continue
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        here = self.rel(p)
        return {"path": here, "parent": None if not here else (here.rsplit("/", 1)[0] if "/" in here else ""), "entries": entries}

    def recent(self, limit: int = 30) -> list[dict]:
        found = []
        for f in self._walk():
            try:
                found.append(self._entry(f))
            except OSError:
                continue
        found.sort(key=lambda e: -e["mtime"])
        return found[:limit]

    def search(self, query: str, limit: int = 60) -> list[dict]:
        terms = [t for t in (query or "").lower().split() if t]
        if not terms:
            return []
        out = []
        for f in self._walk():
            name = self.rel(f).lower()
            if all(t in name for t in terms):
                try:
                    out.append(self._entry(f))
                except OSError:
                    continue
                if len(out) >= limit:
                    break
        out.sort(key=lambda e: -e["mtime"])
        return out

    def stats(self) -> dict:
        n = total = 0
        for f in self._walk():
            try:
                if not self._inside(f):
                    continue
                total += f.stat().st_size
                n += 1
            except OSError:
                continue
        return {"files": n, "bytes": total}

    # -- one file -------------------------------------------------------------------
    def preview(self, rel: str) -> dict:
        p = self.resolve(rel)
        if not p.is_file():
            raise FileError("No such file.")
        size = p.stat().st_size
        k = kind_of(p)
        base = {"name": p.name, "path": self.rel(p), "size": size, "mtime": p.stat().st_mtime, "kind": k}
        if k == "text":
            raw = p.read_bytes()[:MAX_TEXT]
            return {**base, "text": raw.decode("utf-8", errors="replace"), "truncated": size > MAX_TEXT}
        if k == "image":
            return {**base, "too_big": size > MAX_IMAGE}
        # an unknown extension that is plainly text still previews
        head = p.read_bytes()[:4096]
        if head and b"\0" not in head:
            try:
                head.decode("utf-8")
                raw = p.read_bytes()[:MAX_TEXT]
                return {**base, "kind": "text", "text": raw.decode("utf-8", errors="replace"), "truncated": size > MAX_TEXT}
            except UnicodeDecodeError:
                pass
        return base

    def raw(self, rel: str) -> tuple[Path, str]:
        p = self.resolve(rel)
        if not p.is_file():
            raise FileError("No such file.")
        return p, mimetypes.guess_type(p.name)[0] or "application/octet-stream"

    def delete(self, rel: str) -> None:
        p = self.resolve(rel)
        if p == self.root:
            raise FileError("The workspace itself cannot be deleted.")
        if not p.is_file():
            raise FileError("Only files can be deleted here, not folders.")
        p.unlink()
