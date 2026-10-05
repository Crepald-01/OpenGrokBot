"""Back up and restore your OpenGrokBot data: Bots, conversations, memory, routines, settings and skills (and, if you
ask, the shared workspace). Secrets in the Windows Credential Manager (API keys, the service token) are never included.

Restoring is staged: the zip is checked, unpacked into a pending folder, and applied the next time the service starts,
when no database connection is open. The current database is kept next to it as before-restore-<time>.sqlite3."""
from __future__ import annotations

import io
import json
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from . import VERSION, paths

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

APP = "OpenGrokBot"
DB_NAME = "opengrokbot.sqlite3"
MAX_WORKSPACE = 300 * 1024 * 1024
MAX_RESTORE = 600 * 1024 * 1024


class BackupError(ValueError):
    pass


def pending_dir() -> Path:
    return paths.data_dir() / "restore-pending"


def _files_under(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file():
            yield p


def create(eng: "Engine", include_workspace: bool = False) -> bytes:
    tmp = Path(tempfile.mkdtemp(prefix="gb-backup-"))
    try:
        snap = tmp / DB_NAME
        dst = sqlite3.connect(snap)
        try:
            eng.db.conn.backup(dst)   # a consistent copy even while Bots are working
        finally:
            dst.close()
        ws = paths.workspace_dir()
        if include_workspace and sum(f.stat().st_size for f in _files_under(ws)) > MAX_WORKSPACE:
            raise BackupError("The workspace is larger than 300 MB. Back up without it, or copy that folder yourself.")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps({"app": APP, "version": VERSION, "created": time.time(), "workspace": bool(include_workspace)}, indent=2))
            z.write(snap, DB_NAME)
            for f in _files_under(paths.skills_dir()):
                z.write(f, "skills/" + f.relative_to(paths.skills_dir()).as_posix())
            if include_workspace:
                for f in _files_under(ws):
                    z.write(f, "workspace/" + f.relative_to(ws).as_posix())
        return buf.getvalue()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _safe(name: str) -> PurePosixPath | None:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return None
    return p


def stage_restore(data: bytes) -> dict:
    """Validate a backup and stage it. Raises BackupError with a plain-language reason when it is not a usable backup."""
    if len(data) > MAX_RESTORE:
        raise BackupError("That file is too large to be a backup.")
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise BackupError("That is not a backup file (it is not a zip).")
    z = zipfile.ZipFile(io.BytesIO(data))
    try:
        manifest = json.loads(z.read("manifest.json"))
    except (KeyError, ValueError):
        raise BackupError("That zip is not an OpenGrokBot backup (no manifest).") from None
    if manifest.get("app") != APP:
        raise BackupError("That zip is not an OpenGrokBot backup.")
    if DB_NAME not in z.namelist():
        raise BackupError("The backup has no database in it.")
    tmp = Path(tempfile.mkdtemp(prefix="gb-restore-"))
    try:
        db_file = tmp / DB_NAME
        db_file.write_bytes(z.read(DB_NAME))
        con = sqlite3.connect(db_file)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("The backup's database is damaged.")
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"bots", "threads", "messages", "settings"} <= tables:
                raise BackupError("The backup's database does not look like OpenGrokBot's.")
            info = {"bots": con.execute("SELECT COUNT(*) FROM bots").fetchone()[0], "threads": con.execute("SELECT COUNT(*) FROM threads").fetchone()[0],
                    "messages": con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]}
        finally:
            con.close()
        out = pending_dir()
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        shutil.copyfile(db_file, out / DB_NAME)
        skills = workspace = 0
        for name in z.namelist():
            for prefix, counter in (("skills/", "skills"), ("workspace/", "workspace")):
                if name.startswith(prefix) and not name.endswith("/"):
                    rel = _safe(name)
                    if rel is None:
                        continue
                    target = out / Path(*rel.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(z.read(name))
                    if counter == "skills":
                        skills += 1
                    else:
                        workspace += 1
        (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return {**info, "skills": skills, "workspace_files": workspace, "version": manifest.get("version", "?"), "created": manifest.get("created", 0)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def apply_pending() -> str | None:
    """Called before the database is opened. Returns a short description when a restore was applied."""
    src = pending_dir()
    if not (src / DB_NAME).exists():
        return None
    live = paths.db_path()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if live.exists():
        try:
            a = sqlite3.connect(live)
            b = sqlite3.connect(paths.data_dir() / f"before-restore-{stamp}.sqlite3")
            a.backup(b)
            a.close()
            b.close()
        except sqlite3.Error:
            shutil.copyfile(live, paths.data_dir() / f"before-restore-{stamp}.sqlite3")
    for suffix in ("-wal", "-shm"):
        side = Path(str(live) + suffix)
        if side.exists():
            side.unlink()
    shutil.copyfile(src / DB_NAME, live)
    for sub, dest in (("skills", paths.skills_dir()), ("workspace", paths.workspace_dir())):
        root = src / sub
        if root.exists():
            for f in _files_under(root):
                target = dest / f.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, target)
    shutil.rmtree(src, ignore_errors=True)
    return f"Restored from a backup. Your previous data is kept as before-restore-{stamp}.sqlite3."
