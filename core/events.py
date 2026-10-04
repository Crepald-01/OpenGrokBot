"""In-process event bus. The service fans events out to the desktop UI and the mobile PWA."""
from __future__ import annotations

import itertools
import queue
import threading
import time
from collections import deque
from typing import Any


class EventBus:
    def __init__(self, backlog: int = 2000):
        self._lock = threading.Lock()
        self._subs: dict[int, tuple[queue.Queue, str]] = {}
        self._ids = itertools.count(1)
        self._sub_ids = itertools.count(1)
        self._backlog: deque[dict] = deque(maxlen=backlog)
        self._last = 0

    def last_id(self) -> int:
        return self._last

    def publish(self, type_: str, **data: Any) -> dict:
        ev = {"id": next(self._ids), "type": type_, "ts": time.time(), **data}
        self._last = ev["id"]
        with self._lock:
            if type_ != "delta":  # streaming chunks are not replayed
                self._backlog.append(ev)
            subs = list(self._subs.values())
        for q, _kind in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass
        return ev

    def subscribe(self, kind: str = "desktop", since: int = 0) -> tuple[int, queue.Queue]:
        q: queue.Queue = queue.Queue(maxsize=5000)
        with self._lock:
            sid = next(self._sub_ids)
            self._subs[sid] = (q, kind)
            if since:
                for ev in self._backlog:
                    if ev["id"] > since:
                        q.put_nowait(ev)
        return sid, q

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            self._subs.pop(sid, None)

    def has_subscriber(self, kind: str) -> bool:
        with self._lock:
            return any(k == kind for _q, k in self._subs.values())

    def since(self, after: int) -> list[dict]:
        with self._lock:
            return [e for e in self._backlog if e["id"] > after]
