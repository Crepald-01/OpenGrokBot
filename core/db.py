"""SQLite storage. One connection per thread, WAL mode, JSON helpers."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS bots(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, emoji TEXT DEFAULT '', job TEXT DEFAULT '',
  instructions TEXT DEFAULT '', profile TEXT DEFAULT '', model TEXT DEFAULT '',
  approval_mode TEXT DEFAULT 'ask', step_limit INTEGER DEFAULT 40,
  net_mode TEXT DEFAULT 'inherit', net_allow TEXT DEFAULT '[]', net_deny TEXT DEFAULT '[]',
  grants TEXT DEFAULT '[]', proactive TEXT DEFAULT 'off', template TEXT DEFAULT '',
  paused INTEGER DEFAULT 0, archived INTEGER DEFAULT 0, created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS threads(
  id TEXT PRIMARY KEY, bot_id TEXT, group_id TEXT, kind TEXT DEFAULT 'dm', title TEXT DEFAULT '',
  summary TEXT DEFAULT '', summary_upto INTEGER DEFAULT 0, hops INTEGER DEFAULT 0,
  created_at REAL, updated_at REAL, archived INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_threads_bot ON threads(bot_id, updated_at);
CREATE TABLE IF NOT EXISTS groups(
  id TEXT PRIMARY KEY, name TEXT, goal TEXT DEFAULT '', lead_bot TEXT DEFAULT '',
  members TEXT DEFAULT '[]', thread_id TEXT, kind TEXT DEFAULT 'group', created_at REAL
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL, author TEXT, role TEXT,
  kind TEXT DEFAULT 'llm', content TEXT, anchor INTEGER, turn_id TEXT, created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, id);
CREATE TABLE IF NOT EXISTS turns(
  id TEXT PRIMARY KEY, bot_id TEXT, thread_id TEXT, trigger TEXT, status TEXT,
  started_at REAL, ended_at REAL, steps INTEGER DEFAULT 0, error TEXT DEFAULT '', tainted INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memories(
  id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT, kind TEXT DEFAULT 'fact', text TEXT,
  source TEXT DEFAULT '', verify INTEGER DEFAULT 0, pinned INTEGER DEFAULT 0, created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_memories_bot ON memories(bot_id);
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY, name TEXT UNIQUE, content TEXT DEFAULT '', updated_by TEXT DEFAULT '', updated_at REAL
);
CREATE TABLE IF NOT EXISTS handoffs(
  id TEXT PRIMARY KEY, from_bot TEXT, to_bot TEXT, title TEXT, brief TEXT, status TEXT DEFAULT 'open',
  thread_id TEXT, result TEXT DEFAULT '', created_at REAL, updated_at REAL, last_nudge_at REAL DEFAULT 0,
  nudges INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS followups(
  id TEXT PRIMARY KEY, bot_id TEXT, thread_id TEXT, note TEXT, due_at REAL, status TEXT DEFAULT 'open', created_at REAL
);
CREATE TABLE IF NOT EXISTS approvals(
  id TEXT PRIMARY KEY, bot_id TEXT, thread_id TEXT, turn_id TEXT, category TEXT, tool TEXT,
  summary TEXT, details TEXT DEFAULT '{}', status TEXT DEFAULT 'pending', decided_by TEXT DEFAULT '',
  reason TEXT DEFAULT '', answer TEXT DEFAULT '', created_at REAL, decided_at REAL, tainted INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);
CREATE TABLE IF NOT EXISTS approval_rules(
  id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT, category TEXT, pattern TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS actions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, bot_id TEXT, thread_id TEXT, turn_id TEXT, tool TEXT,
  args TEXT, result TEXT, status TEXT, category TEXT, url TEXT DEFAULT '', path TEXT DEFAULT '', duration_ms INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_actions_bot ON actions(bot_id, id);
CREATE TABLE IF NOT EXISTS routines(
  id TEXT PRIMARY KEY, bot_id TEXT, name TEXT, skill TEXT DEFAULT '', prompt TEXT DEFAULT '', cron TEXT,
  enabled INTEGER DEFAULT 1, dry_run INTEGER DEFAULT 0, notify TEXT DEFAULT 'always', catch_up INTEGER DEFAULT 0,
  created_at REAL, last_run_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS routine_runs(
  id TEXT PRIMARY KEY, routine_id TEXT, bot_id TEXT, thread_id TEXT, turn_id TEXT, started_at REAL,
  ended_at REAL, status TEXT, result TEXT DEFAULT '', error TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS recordings(
  id TEXT PRIMARY KEY, bot_id TEXT, name TEXT, steps TEXT DEFAULT '[]', status TEXT DEFAULT 'recording',
  skill_name TEXT DEFAULT '', created_at REAL
);
CREATE TABLE IF NOT EXISTS usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, bot_id TEXT, turn_id TEXT, profile TEXT, model TEXT,
  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage(ts);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS mcp_servers(
  id TEXT PRIMARY KEY, name TEXT, transport TEXT DEFAULT 'stdio', command TEXT DEFAULT '', args TEXT DEFAULT '[]',
  env TEXT DEFAULT '{}', url TEXT DEFAULT '', headers TEXT DEFAULT '{}', enabled INTEGER DEFAULT 1, trust TEXT DEFAULT 'ask'
);
CREATE TABLE IF NOT EXISTS plugins(
  id TEXT PRIMARY KEY, source TEXT, path TEXT, enabled INTEGER DEFAULT 1, installed_at REAL, version TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, bot_id TEXT, thread_id TEXT, title TEXT, body TEXT,
  read INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS known_logins(domain TEXT PRIMARY KEY, added_at REAL);
CREATE TABLE IF NOT EXISTS knowledge_sources(
  id TEXT PRIMARY KEY, name TEXT, kind TEXT DEFAULT 'text', path TEXT DEFAULT '', bot_id TEXT DEFAULT '',
  size INTEGER DEFAULT 0, chunks INTEGER DEFAULT 0, mtime REAL DEFAULT 0, created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS knowledge_chunks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT, seq INTEGER, text TEXT
);
CREATE INDEX IF NOT EXISTS idx_kchunks_source ON knowledge_chunks(source_id, seq);
CREATE TABLE IF NOT EXISTS triggers(
  id TEXT PRIMARY KEY, name TEXT, kind TEXT, bot_id TEXT, prompt TEXT DEFAULT '', secret_hash TEXT DEFAULT '', secret_hint TEXT DEFAULT '',
  folder TEXT DEFAULT '', pattern TEXT DEFAULT '*', enabled INTEGER DEFAULT 1, dry_run INTEGER DEFAULT 0, created_at REAL,
  last_fired_at REAL DEFAULT 0, fired INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trigger_runs(
  id TEXT PRIMARY KEY, trigger_id TEXT, started_at REAL, ended_at REAL, status TEXT, detail TEXT DEFAULT '', result TEXT DEFAULT '', thread_id TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_trigger_runs ON trigger_runs(trigger_id, started_at);
CREATE TABLE IF NOT EXISTS trigger_seen(trigger_id TEXT, path TEXT, mtime REAL, size INTEGER, PRIMARY KEY(trigger_id, path));
CREATE TABLE IF NOT EXISTS workflows(
  id TEXT PRIMARY KEY, name TEXT, description TEXT DEFAULT '', steps TEXT DEFAULT '[]', cron TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
  created_at REAL, updated_at REAL, last_run_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS workflow_runs(
  id TEXT PRIMARY KEY, workflow_id TEXT, started_at REAL, ended_at REAL, status TEXT, input TEXT DEFAULT '', steps TEXT DEFAULT '[]',
  result TEXT DEFAULT '', error TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_wruns ON workflow_runs(workflow_id, started_at);
CREATE TABLE IF NOT EXISTS file_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, ts REAL, size INTEGER, sha TEXT, reason TEXT DEFAULT '', bot_id TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_fv_path ON file_versions(path, ts);
CREATE TABLE IF NOT EXISTS api_tokens(
  id TEXT PRIMARY KEY, name TEXT, prefix TEXT, hash TEXT, scope TEXT DEFAULT 'read', created_at REAL, expires_at REAL DEFAULT 0,
  last_used_at REAL DEFAULT 0, revoked INTEGER DEFAULT 0
);
"""


# columns added after the first release: applied to existing databases when they are opened
MIGRATIONS = [("bots", "daily_token_limit", "INTEGER DEFAULT 0"),
              ("bots", "fallback_profile", "TEXT DEFAULT ''"), ("bots", "fallback_model", "TEXT DEFAULT ''"),
              ("triggers", "workflow_id", "TEXT DEFAULT ''")]


def new_id(n: int = 12) -> str:
    return uuid.uuid4().hex[:n]


def now() -> float:
    return time.time()


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        with self._write_lock:
            self.conn.executescript(SCHEMA)
            for table, col, decl in MIGRATIONS:
                have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            self.conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=OFF")
            self._local.conn = c
        return c

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a write statement; returns lastrowid."""
        with self._write_lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur.lastrowid or 0

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._write_lock:
            self.conn.executemany(sql, [tuple(r) for r in rows])
            self.conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        cur = self.conn.execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        cur = self.conn.execute(sql, tuple(params))
        r = cur.fetchone()
        return dict(r) if r else None

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        r = self.conn.execute(sql, tuple(params)).fetchone()
        return r[0] if r and r[0] is not None else default

    def insert(self, table: str, row: dict) -> int:
        cols = ",".join(row.keys())
        marks = ",".join("?" for _ in row)
        return self.execute(f"INSERT INTO {table}({cols}) VALUES({marks})", list(row.values()))

    def update(self, table: str, row_id: Any, row: dict, key: str = "id") -> None:
        if not row:
            return
        sets = ",".join(f"{k}=?" for k in row)
        self.execute(f"UPDATE {table} SET {sets} WHERE {key}=?", [*row.values(), row_id])

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None


def jdump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def jload(s: Any, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    if not isinstance(s, (str, bytes)):
        return s
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default
