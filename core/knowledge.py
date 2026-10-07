"""The knowledge base: documents your Bots can search.

Add notes, text files, Word documents or whole workspace folders. They are cut into passages and indexed with SQLite full-text
search (ranked by relevance, no model call and nothing leaves your PC). A source can be shared with every Bot or kept for one.
Bots find things with the knowledge_search tool and read more with knowledge_read."""
from __future__ import annotations

import html
import io
import os
import re
import zipfile
from pathlib import Path

from .db import Database, new_id, now

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".xml", ".yml", ".yaml", ".toml", ".ini",
            ".cfg", ".sql", ".rst", ".tex", ".sh", ".bat", ".ps1"}
HTML_EXT = {".html", ".htm"}
SUPPORTED = TEXT_EXT | HTML_EXT | {".docx"}
MAX_SOURCE_CHARS = 2_000_000      # text kept per source
MAX_FILE_BYTES = 20_000_000       # files larger than this are skipped
MAX_FOLDER_FILES = 500
CHUNK_CHARS = 1200
OVERLAP = 150


class KnowledgeError(ValueError):
    pass


# ----------------------------------------------------------------------------- reading documents
def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|head|svg|noscript).*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|section|article)>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)).strip()


def _docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    except (KeyError, zipfile.BadZipFile) as e:
        raise KnowledgeError("That is not a readable Word document (.docx).") from e
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
    return html.unescape(re.sub(r"<[^>]+>", "", xml)).strip()


def extract_text(filename: str, data: bytes) -> str:
    """Plain text from a document. Raises KnowledgeError for formats that cannot be read."""
    ext = Path(filename).suffix.lower()
    if ext == ".docx":
        return _docx_text(data)
    if ext == ".pdf":
        raise KnowledgeError("PDF files are not supported yet. Export the PDF to text or Word and add that.")
    if ext not in SUPPORTED:
        head = data[:4096]
        if b"\0" in head:
            raise KnowledgeError(f"{Path(filename).name} looks like a binary file, which cannot be added.")
    text = data.decode("utf-8", errors="replace")
    if ext in HTML_EXT:
        text = _strip_html(text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = OVERLAP) -> list[str]:
    """Passages of about `size` characters, cut at paragraph or sentence breaks, overlapping a little so a fact on the edge is not lost."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        end = min(n, i + size)
        if end < n:
            window = text[i + size // 2:end]
            for sep in ("\n\n", "\n", ". ", " "):
                k = window.rfind(sep)
                if k >= 0:
                    end = i + size // 2 + k + len(sep)
                    break
        piece = text[i:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        i = max(end - overlap, i + 1)
    return out


def _fts_query(q: str, mode: str = "and") -> str:
    terms = re.findall(r"\w+", q.lower(), flags=re.UNICODE)[:12]
    if not terms:
        return ""
    parts = [f'"{t}"*' if len(t) > 2 else f'"{t}"' for t in terms]
    return (" OR " if mode == "or" else " ").join(parts)


class Knowledge:
    def __init__(self, db: Database):
        self.db = db
        self.fts = True
        try:
            with db._write_lock:
                db.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(text, tokenize='porter unicode61')")
                db.conn.commit()
        except Exception:  # noqa: BLE001  (a SQLite build without FTS5: fall back to plain LIKE matching)
            self.fts = False

    # ------------------------------------------------------------------------- adding
    def _store(self, name: str, kind: str, text: str, bot_id: str = "", path: str = "", mtime: float = 0.0, size: int = 0, source_id: str | None = None) -> dict:
        text = text[:MAX_SOURCE_CHARS]
        chunks = chunk_text(text)
        if not chunks:
            raise KnowledgeError(f"There is no text in {name}.")
        sid = source_id or new_id()
        with self.db._write_lock:
            if source_id:
                self._drop_chunks(sid)
                self.db.update("knowledge_sources", sid, {"name": name, "size": size or len(text), "chunks": len(chunks), "mtime": mtime, "updated_at": now()})
            else:
                self.db.insert("knowledge_sources", {"id": sid, "name": name[:160], "kind": kind, "path": path, "bot_id": bot_id or "", "size": size or len(text),
                                                     "chunks": len(chunks), "mtime": mtime, "created_at": now(), "updated_at": now()})
            for seq, c in enumerate(chunks):
                cid = self.db.conn.execute("INSERT INTO knowledge_chunks(source_id, seq, text) VALUES(?,?,?)", (sid, seq, c)).lastrowid
                if self.fts:
                    self.db.conn.execute("INSERT INTO knowledge_fts(rowid, text) VALUES(?,?)", (cid, c))
            self.db.conn.commit()
        return self.get(sid)  # type: ignore

    def add_text(self, name: str, text: str, bot_id: str = "") -> dict:
        name = (name or "").strip()
        if not name:
            raise KnowledgeError("Give the note a name.")
        self._check_bot(bot_id)
        return self._store(name, "note", text, bot_id)

    def add_upload(self, filename: str, data: bytes, bot_id: str = "") -> dict:
        """A document sent from the app (the app reads the file, so this also works when the service is on another PC)."""
        if len(data) > MAX_FILE_BYTES:
            raise KnowledgeError("That file is too large (the limit is 20 MB).")
        self._check_bot(bot_id)
        text = extract_text(filename, data)
        return self._store(Path(filename).name, "upload", text, bot_id, size=len(data))

    def add_workspace(self, files, rel: str, bot_id: str = "") -> list[dict]:
        """Index a file or a folder from the shared workspace (re-read later when it changes). `files` is core.files.Files."""
        self._check_bot(bot_id)
        p = files.resolve(rel)
        if p.is_file():
            return [self._add_path(p, files, bot_id)]
        if not p.is_dir():
            raise KnowledgeError("No such file or folder in the workspace.")
        out, n = [], 0
        for dirpath, dirnames, filenames in os.walk(p):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in sorted(filenames):
                f = Path(dirpath) / fn
                if fn.startswith(".") or f.suffix.lower() not in SUPPORTED:
                    continue
                n += 1
                if n > MAX_FOLDER_FILES:
                    return out
                try:
                    out.append(self._add_path(f, files, bot_id))
                except (KnowledgeError, OSError):
                    continue
        if not out:
            raise KnowledgeError("That folder has no readable documents in it.")
        return out

    def _add_path(self, f: Path, files, bot_id: str) -> dict:
        rel = files.rel(f)
        existing = self.db.one("SELECT id FROM knowledge_sources WHERE kind='file' AND path=? AND bot_id=?", (rel, bot_id or ""))
        st = f.stat()
        if st.st_size > MAX_FILE_BYTES:
            raise KnowledgeError(f"{f.name} is too large.")
        text = extract_text(f.name, f.read_bytes())
        return self._store(f.name, "file", text, bot_id, path=rel, mtime=st.st_mtime, size=st.st_size, source_id=existing["id"] if existing else None)

    def refresh(self, files) -> int:
        """Re-read workspace files that changed since they were indexed, and drop sources whose file is gone. Returns how many changed."""
        changed = 0
        for s in self.db.query("SELECT * FROM knowledge_sources WHERE kind='file'"):
            try:
                f = files.resolve(s["path"])
            except Exception:  # noqa: BLE001
                continue
            if not f.is_file():
                self.remove(s["id"])
                changed += 1
                continue
            if f.stat().st_mtime > (s["mtime"] or 0) + 0.01:
                try:
                    self._add_path(f, files, s["bot_id"])
                    changed += 1
                except (KnowledgeError, OSError):
                    continue
        return changed

    def _check_bot(self, bot_id: str) -> None:
        if bot_id and not self.db.one("SELECT id FROM bots WHERE id=?", (bot_id,)):
            raise KnowledgeError("No such Bot.")

    # ------------------------------------------------------------------------- managing
    def _drop_chunks(self, sid: str) -> None:
        ids = [r["id"] for r in self.db.query("SELECT id FROM knowledge_chunks WHERE source_id=?", (sid,))]
        if self.fts and ids:
            self.db.conn.executemany("DELETE FROM knowledge_fts WHERE rowid=?", [(i,) for i in ids])
        self.db.conn.execute("DELETE FROM knowledge_chunks WHERE source_id=?", (sid,))

    def remove(self, sid: str) -> None:
        with self.db._write_lock:
            self._drop_chunks(sid)
            self.db.conn.execute("DELETE FROM knowledge_sources WHERE id=?", (sid,))
            self.db.conn.commit()

    def get(self, sid: str) -> dict | None:
        return self.db.one("SELECT * FROM knowledge_sources WHERE id=?", (sid,))

    def sources(self, bot_id: str | None = None) -> list[dict]:
        """All sources, or the ones a given Bot can see (shared + its own)."""
        if bot_id is None:
            rows = self.db.query("SELECT * FROM knowledge_sources ORDER BY updated_at DESC")
        else:
            rows = self.db.query("SELECT * FROM knowledge_sources WHERE bot_id='' OR bot_id=? ORDER BY updated_at DESC", (bot_id,))
        names = {b["id"]: b["name"] for b in self.db.query("SELECT id, name FROM bots")}
        for r in rows:
            r["bot_name"] = names.get(r["bot_id"], "") if r["bot_id"] else ""
        return rows

    def count_for(self, bot_id: str) -> int:
        return int(self.db.scalar("SELECT COUNT(*) FROM knowledge_sources WHERE bot_id='' OR bot_id=?", (bot_id,), 0))

    def stats(self) -> dict:
        return {"sources": int(self.db.scalar("SELECT COUNT(*) FROM knowledge_sources", (), 0)), "passages": int(self.db.scalar("SELECT COUNT(*) FROM knowledge_chunks", (), 0)),
                "chars": int(self.db.scalar("SELECT COALESCE(SUM(LENGTH(text)),0) FROM knowledge_chunks", (), 0)), "fts": self.fts}

    # ------------------------------------------------------------------------- finding
    def search(self, query: str, bot_id: str | None = None, limit: int = 8) -> list[dict]:
        """Best passages for a question: [{source_id, name, seq, text, snippet}], most relevant first."""
        query = (query or "").strip()
        if not query:
            return []
        limit = max(1, min(30, limit))
        scope_sql, scope_args = ("", [])
        if bot_id is not None:
            scope_sql, scope_args = (" AND (s.bot_id='' OR s.bot_id=?)", [bot_id])
        rows: list[dict] = []
        if self.fts:
            for mode in ("and", "or"):
                q = _fts_query(query, mode)
                if not q:
                    return []
                try:
                    rows = self.db.query(
                        "SELECT c.source_id, s.name, c.seq, c.text, snippet(knowledge_fts, 0, '[', ']', ' … ', 24) AS snippet FROM knowledge_fts f "
                        "JOIN knowledge_chunks c ON c.id=f.rowid JOIN knowledge_sources s ON s.id=c.source_id "
                        f"WHERE knowledge_fts MATCH ?{scope_sql} ORDER BY bm25(knowledge_fts) LIMIT ?", [q, *scope_args, limit])
                except Exception:  # noqa: BLE001
                    rows = []
                if rows:
                    break
        else:
            terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1][:6]
            if not terms:
                return []
            like = " AND ".join("LOWER(c.text) LIKE ?" for _ in terms)
            rows = self.db.query("SELECT c.source_id, s.name, c.seq, c.text, substr(c.text,1,240) AS snippet FROM knowledge_chunks c "
                                 f"JOIN knowledge_sources s ON s.id=c.source_id WHERE {like}{scope_sql} LIMIT ?", [*(f"%{t}%" for t in terms), *scope_args, limit])
        return rows

    def read(self, sid: str, seq: int | None = None, max_chars: int = 6000) -> dict:
        """One passage (with seq) or the start of a whole source, plus how many passages there are."""
        s = self.get(sid)
        if not s:
            raise KnowledgeError("No such source.")
        if seq is not None:
            rows = self.db.query("SELECT seq, text FROM knowledge_chunks WHERE source_id=? AND seq>=? ORDER BY seq LIMIT 6", (sid, max(0, int(seq))))
        else:
            rows = self.db.query("SELECT seq, text FROM knowledge_chunks WHERE source_id=? ORDER BY seq LIMIT 6", (sid,))
        text = "\n\n".join(r["text"] for r in rows)[:max_chars]
        return {"source_id": sid, "name": s["name"], "passages": s["chunks"], "from_seq": rows[0]["seq"] if rows else 0, "text": text}

    def find_source(self, ref: str, bot_id: str | None = None) -> dict | None:
        """A source by id or (case-insensitive) name, limited to what the Bot can see."""
        for s in self.sources(bot_id):
            if s["id"] == ref or s["name"].lower() == (ref or "").strip().lower():
                return s
        return None
