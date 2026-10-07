"""File history: a safety net under the shared workspace.

Before a Bot overwrites, appends to, deletes or moves over a file (or you delete one from the Files page), the old contents are
kept as a version. You can look at earlier versions, restore one, or bring back a deleted file. Versions live in the app's data
folder (not in the workspace), are stored once per distinct content, and old ones are pruned.

Changes made by commands a Bot runs in the terminal are not tracked; only the file tools and the Files page are."""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from .db import Database, now

KEEP_PER_FILE = 20
MAX_VERSION_BYTES = 5_000_000
MAX_TOTAL_BYTES = 500_000_000
MAX_TREE_FILES = 200
PREVIEW_BYTES = 200_000


class HistoryError(ValueError):
    pass


class FileHistory:
    def __init__(self, db: Database, root: Path, store: Path):
        self.db = db
        self.root = Path(root).resolve()
        self.store = Path(store)
        self.store.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------------- paths
    def _rel(self, p: Path) -> str | None:
        try:
            r = p.resolve()
            return r.relative_to(self.root).as_posix()
        except (ValueError, OSError):
            return None

    def _abs(self, rel: str) -> Path:
        rel = (rel or "").replace("\\", "/").strip("/")
        p = (self.root / rel).resolve()
        if not rel or self.root not in p.parents:
            raise HistoryError("That path is outside the workspace.")
        return p

    def _blob(self, sha: str) -> Path:
        return self.store / sha[:2] / sha

    # ------------------------------------------------------------------------- saving versions
    def snapshot(self, p: Path, reason: str, bot_id: str = "") -> int | None:
        """Keep the current contents of a file as a version. Returns the version id, or None when there was nothing worth keeping
        (not a file, outside the workspace, too large, empty, or identical to the newest version)."""
        try:
            p = Path(p)
            rel = self._rel(p)
            if rel is None or not p.is_file() or p.is_symlink():
                return None
            size = p.stat().st_size
            if size == 0 or size > MAX_VERSION_BYTES:
                return None
            data = p.read_bytes()
        except OSError:
            return None
        sha = hashlib.sha256(data).hexdigest()
        last = self.db.one("SELECT id, sha FROM file_versions WHERE path=? ORDER BY id DESC LIMIT 1", (rel,))
        if last and last["sha"] == sha:
            return last["id"]
        blob = self._blob(sha)
        if not blob.exists():
            blob.parent.mkdir(parents=True, exist_ok=True)
            tmp = blob.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, blob)
        vid = self.db.insert("file_versions", {"path": rel, "ts": now(), "size": len(data), "sha": sha, "reason": reason[:60], "bot_id": bot_id or ""})
        self._prune(rel)
        return vid

    def snapshot_tree(self, p: Path, reason: str, bot_id: str = "") -> int:
        """Keep every file in a folder (before the folder is deleted). Returns how many versions were saved."""
        p = Path(p)
        if p.is_file():
            return 1 if self.snapshot(p, reason, bot_id) else 0
        n = seen = 0
        for dirpath, dirnames, filenames in os.walk(p):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                seen += 1
                if seen > MAX_TREE_FILES:
                    return n
                if self.snapshot(Path(dirpath) / fn, reason, bot_id):
                    n += 1
        return n

    def _prune(self, rel: str) -> None:
        rows = self.db.query("SELECT id, sha FROM file_versions WHERE path=? ORDER BY id DESC", (rel,))
        for r in rows[KEEP_PER_FILE:]:
            self.db.execute("DELETE FROM file_versions WHERE id=?", (r["id"],))
            self._drop_blob_if_unused(r["sha"])
        total = int(self.db.scalar("SELECT COALESCE(SUM(size),0) FROM file_versions", (), 0))
        if total > MAX_TOTAL_BYTES:   # over the budget: drop the oldest versions (never the newest one of a file) until it fits
            for r in self.db.query("SELECT id, sha, size, path FROM file_versions ORDER BY id ASC"):
                newest = self.db.scalar("SELECT MAX(id) FROM file_versions WHERE path=?", (r["path"],), 0)
                if r["id"] == newest:
                    continue
                self.db.execute("DELETE FROM file_versions WHERE id=?", (r["id"],))
                self._drop_blob_if_unused(r["sha"])
                total -= r["size"]
                if total <= MAX_TOTAL_BYTES * 0.9:
                    break

    def _drop_blob_if_unused(self, sha: str) -> None:
        if not self.db.one("SELECT 1 FROM file_versions WHERE sha=? LIMIT 1", (sha,)):
            try:
                self._blob(sha).unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------------- reading
    def versions(self, rel: str) -> list[dict]:
        rel = self._rel(self._abs(rel)) or ""
        names = {b["id"]: b["name"] for b in self.db.query("SELECT id, name FROM bots")}
        rows = self.db.query("SELECT * FROM file_versions WHERE path=? ORDER BY id DESC", (rel,))
        for r in rows:
            r["bot_name"] = names.get(r["bot_id"], "") if r["bot_id"] else ""
        return rows

    def version(self, vid: int) -> dict:
        r = self.db.one("SELECT * FROM file_versions WHERE id=?", (int(vid),))
        if not r:
            raise HistoryError("No such version.")
        return r

    def read(self, vid: int) -> bytes:
        r = self.version(vid)
        blob = self._blob(r["sha"])
        if not blob.is_file():
            raise HistoryError("That version's data is gone.")
        return blob.read_bytes()

    def preview(self, vid: int) -> dict:
        r = self.version(vid)
        data = self.read(vid)[:PREVIEW_BYTES]
        if b"\0" in data[:4096]:
            return {**r, "binary": True, "text": ""}
        return {**r, "binary": False, "text": data.decode("utf-8", errors="replace"), "truncated": r["size"] > PREVIEW_BYTES}

    def has_history(self, rel: str) -> bool:
        return bool(self.db.one("SELECT 1 FROM file_versions WHERE path=? LIMIT 1", (rel,)))

    def deleted(self, limit: int = 100) -> list[dict]:
        """Files that have versions saved but no longer exist: newest deletion first."""
        out = []
        rows = self.db.query("SELECT v.* FROM file_versions v JOIN (SELECT path, MAX(id) AS mid FROM file_versions GROUP BY path) m ON v.id=m.mid ORDER BY v.id DESC")
        for r in rows:
            try:
                if (self.root / r["path"]).exists():
                    continue
            except OSError:
                continue
            out.append({"path": r["path"], "ts": r["ts"], "size": r["size"], "reason": r["reason"], "version_id": r["id"], "bot_id": r["bot_id"]})
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------------- restoring
    def restore(self, rel: str, vid: int) -> dict:
        """Put an earlier version back. The file as it is now is kept as a version first, so a restore can itself be undone."""
        target = self._abs(rel)
        r = self.version(vid)
        if r["path"] != self._rel(target):
            raise HistoryError("That version belongs to a different file.")
        data = self.read(vid)
        if target.exists():
            if not target.is_file():
                raise HistoryError("There is a folder in the way.")
            self.snapshot(target, "before restore")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".restoring")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return {"path": r["path"], "restored_from": r["ts"], "size": len(data)}

    def forget(self, rel: str) -> None:
        rel = self._rel(self._abs(rel)) or ""
        rows = self.db.query("SELECT DISTINCT sha FROM file_versions WHERE path=?", (rel,))
        self.db.execute("DELETE FROM file_versions WHERE path=?", (rel,))
        for r in rows:
            self._drop_blob_if_unused(r["sha"])

    def stats(self) -> dict:
        return {"versions": int(self.db.scalar("SELECT COUNT(*) FROM file_versions", (), 0)), "files": int(self.db.scalar("SELECT COUNT(DISTINCT path) FROM file_versions", (), 0)),
                "bytes": int(self.db.scalar("SELECT COALESCE(SUM(size),0) FROM file_versions", (), 0))}

    def clear(self) -> None:
        self.db.execute("DELETE FROM file_versions")
        shutil.rmtree(self.store, ignore_errors=True)
        self.store.mkdir(parents=True, exist_ok=True)
