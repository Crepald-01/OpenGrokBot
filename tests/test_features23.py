"""2.3 backend: tokens per routine run, notification unread count and mark-read."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from core.db import now  # noqa: E402
from core.engine import Engine  # noqa: E402
from service.server import create_app  # noqa: E402

TOKEN = "t23"


class Features23(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-f23-")
        cls.eng = Engine()
        cls.eng.settings.set("notifications.toast", False)
        cls.eng.start()
        cls.c = TestClient(create_app(cls.eng, TOKEN))
        cls.h = {"Authorization": f"Bearer {TOKEN}"}

    @classmethod
    def tearDownClass(cls):
        cls.eng.stop()

    def test_routine_runs_report_the_tokens_of_their_turn(self):
        db = self.eng.db
        db.insert("routine_runs", {"id": "rr-1", "routine_id": "r1", "bot_id": "b1", "thread_id": "t", "turn_id": "turn-a", "started_at": now(),
                                   "ended_at": now(), "status": "ok", "result": "x", "error": ""})
        db.insert("routine_runs", {"id": "rr-2", "routine_id": "r1", "bot_id": "b1", "thread_id": "t", "turn_id": "", "started_at": now() - 5,
                                   "ended_at": now(), "status": "error", "result": "", "error": "boom"})
        db.insert("usage", {"ts": now(), "bot_id": "b1", "turn_id": "turn-a", "profile": "p", "model": "m", "input_tokens": 1200, "output_tokens": 300})
        db.insert("usage", {"ts": now(), "bot_id": "b1", "turn_id": "turn-a", "profile": "p", "model": "m", "input_tokens": 100, "output_tokens": 0})
        rows = {r["id"]: r for r in self.c.get("/api/routine_runs", headers=self.h, params={"routine_id": "r1"}).json()}
        self.assertEqual(rows["rr-1"]["tokens"], 1600)
        self.assertEqual(rows["rr-2"]["tokens"], 0)   # no turn, nothing is attributed to it

    def test_unread_count_and_mark_read(self):
        self.eng.db.execute("DELETE FROM notifications")
        for i in range(3):
            self.eng.db.insert("notifications", {"ts": now(), "kind": "done", "bot_id": "", "thread_id": "", "title": f"n{i}", "body": "", "read": 0})
        self.assertEqual(self.c.get("/api/notifications/unread", headers=self.h).json()["count"], 3)
        self.assertTrue(self.c.post("/api/notifications/read", headers=self.h, json={}).json()["ok"])
        self.assertEqual(self.c.get("/api/notifications/unread", headers=self.h).json()["count"], 0)
        self.assertEqual(len(self.c.get("/api/notifications", headers=self.h).json()), 3)   # history is kept


if __name__ == "__main__":
    unittest.main()
