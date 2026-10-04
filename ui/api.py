"""Client for the background service: non-blocking REST calls and a live event stream.

The UI never touches the engine directly. It talks HTTP to the service, which may run on this PC
(started automatically) or on a remote Windows VM / Docker host ("remote mode").
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Signal, Slot

from core import paths, secrets


@dataclass
class Connection:
    mode: str = "local"            # local | remote
    url: str = "http://127.0.0.1:8765"
    token: str = ""

    @property
    def base(self) -> str:
        return self.url.rstrip("/")


def load_ui_config() -> dict:
    try:
        return json.loads(paths.ui_config_path().read_text())
    except (OSError, ValueError):
        return {}


def save_ui_config(cfg: dict) -> None:
    paths.ui_config_path().write_text(json.dumps(cfg, indent=2))


def remote_token() -> str:
    return secrets.get_secret("ui:remote_token") or ""


class ApiError(Exception):
    pass


class _Signals(QObject):
    finished = Signal(int, object, str)


class _Task(QRunnable):
    def __init__(self, tid: int, fn: Callable[[], Any], sig: _Signals):
        super().__init__()
        self.tid, self.fn, self.sig = tid, fn, sig

    def run(self) -> None:
        try:
            res, err = self.fn(), ""
        except Exception as e:  # noqa: BLE001
            res, err = None, str(e) or type(e).__name__
        self.sig.finished.emit(self.tid, res, err)


class Api(QObject):
    """request(...) returns immediately; ok/err callbacks run on the UI thread."""

    def __init__(self, conn: Connection):
        super().__init__()
        self.conn = conn
        self._sig = _Signals()
        self._sig.finished.connect(self._done)
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(8)
        self._next = 0
        self._cbs: dict[int, tuple[Callable | None, Callable | None]] = {}
        self._client = httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))

    # -- plumbing -----------------------------------------------------------------
    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.conn.token}"}

    def set_connection(self, conn: Connection) -> None:
        self.conn = conn

    def _do(self, method: str, path: str, json_body: Any = None, params: dict | None = None, content: bytes | None = None,
            raw: bool = False, timeout: float | None = None) -> Any:
        try:
            r = self._client.request(method, self.conn.base + path, headers=self.headers, json=json_body if content is None else None,
                                     params=params, content=content, timeout=timeout or 120.0)
        except httpx.ConnectError as e:
            raise ApiError("Cannot reach the Bot service. Is it running?") from e
        except httpx.HTTPError as e:
            raise ApiError(f"Network problem talking to the service: {e}") from e
        if r.status_code == 401:
            raise ApiError("The service rejected the access token. Check Settings > Computer > Remote connection.")
        if r.status_code >= 400:
            try:
                msg = r.json().get("error") or r.json().get("detail") or r.text
            except ValueError:
                msg = r.text
            raise ApiError(str(msg))
        if raw:
            return r.content
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return r.content

    def call(self, method: str, path: str, **kw: Any) -> Any:
        """Blocking call (startup and worker threads only)."""
        return self._do(method, path, **kw)

    def request(self, method: str, path: str, ok: Callable | None = None, err: Callable[[str], None] | None = None, **kw: Any) -> None:
        self._next += 1
        tid = self._next
        self._cbs[tid] = (ok, err)
        self._pool.start(_Task(tid, lambda: self._do(method, path, **kw), self._sig))

    def get(self, path: str, ok: Callable | None = None, err: Callable | None = None, **kw: Any) -> None:
        self.request("GET", path, ok, err, **kw)

    def post(self, path: str, body: Any = None, ok: Callable | None = None, err: Callable | None = None, **kw: Any) -> None:
        self.request("POST", path, ok, err, json_body=body if body is not None else {}, **kw)

    def put(self, path: str, body: Any, ok: Callable | None = None, err: Callable | None = None) -> None:
        self.request("PUT", path, ok, err, json_body=body)

    def delete(self, path: str, ok: Callable | None = None, err: Callable | None = None, **kw: Any) -> None:
        self.request("DELETE", path, ok, err, **kw)

    @Slot(int, object, str)
    def _done(self, tid: int, res: Any, error: str) -> None:
        ok, err = self._cbs.pop(tid, (None, None))
        if error:
            if err:
                err(error)
            else:
                self.on_unhandled_error(error)
        elif ok:
            ok(res)

    on_unhandled_error: Callable[[str], None] = staticmethod(lambda msg: None)  # replaced by the main window

    def url(self, path: str) -> str:
        return self.conn.base + path


class EventThread(QThread):
    """Server-sent events from /api/events. Reconnects automatically (network loss, service restart)."""
    received = Signal(dict)
    connection = Signal(bool)

    def __init__(self, api: Api):
        super().__init__()
        self.api = api
        self._stop = threading.Event()
        self.last_id = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with httpx.Client(timeout=httpx.Timeout(None, connect=10.0)) as c:
                    with c.stream("GET", self.api.url("/api/events"), headers=self.api.headers, params={"kind": "desktop", "since": self.last_id}) as r:
                        if r.status_code != 200:
                            raise ApiError(f"HTTP {r.status_code}")
                        self.connection.emit(True)
                        backoff = 1.0
                        for line in r.iter_lines():
                            if self._stop.is_set():
                                return
                            if line.startswith("data:"):
                                try:
                                    ev = json.loads(line[5:].strip())
                                except ValueError:
                                    continue
                                self.last_id = max(self.last_id, ev.get("id", 0))
                                self.received.emit(ev)
            except Exception:
                pass
            if self._stop.is_set():
                return
            self.connection.emit(False)
            end = time.time() + backoff
            while time.time() < end and not self._stop.is_set():
                time.sleep(0.2)
            backoff = min(backoff * 1.7, 15.0)
