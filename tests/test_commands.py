"""Slash commands: parsing, the commands themselves, and that none of them reach the model."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import agent as agent_mod, commands  # noqa: E402
from core.engine import Engine  # noqa: E402
from core.providers import LLMResult  # noqa: E402
from tests.test_engine import FakeProvider  # noqa: E402


class CommandParsing(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(commands.parse("/status"), ("status", ""))
        self.assertEqual(commands.parse("  /Model gpt-5 mini "), ("model", "gpt-5 mini"))
        self.assertEqual(commands.parse("/remember I prefer short emails\nand no emoji"), ("remember", "I prefer short emails\nand no emoji"))

    def test_not_commands(self):
        for t in ("hello", "/usr/bin/python run it", "//status", "/ status", "/123", "check /status later", ""):
            self.assertIsNone(commands.parse(t), t)


class CommandRuns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-cmd-")
        cls.eng = Engine()
        cls.eng.settings.set("notifications.toast", False)
        cls.fake = FakeProvider([LLMResult(text="ok")] * 20)
        cls._orig = agent_mod.make_provider
        agent_mod.make_provider = lambda *a, **k: cls.fake
        cls.eng.start()

    @classmethod
    def tearDownClass(cls):
        agent_mod.make_provider = cls._orig
        cls.eng.stop()

    def setUp(self):
        self.bot = self.eng.bots.create(f"Tester{id(self) % 9999}", job="test")
        self.th = self.eng.threads.main_thread(self.bot["id"])
        self.calls_before = len(self.fake.calls)

    def say(self, text):
        r = self.eng.send_user_message(self.th["id"], text)
        notes = [i for i in self.eng.threads.display(self.th["id"]) if i["type"] == "notice"]
        return r, (notes[-1]["text"] if notes else "")

    def no_model_call(self):
        # count this Bot's turns (the shared fake model is also hit by background threads of other tests)
        n = self.eng.db.scalar("SELECT COUNT(*) FROM turns WHERE bot_id=?", (self.bot["id"],), 0)
        self.assertEqual(n, 0, "a command must not start a model turn")

    def test_help_and_unknown(self):
        r, t = self.say("/help")
        self.assertTrue(r["handled"] and "/approve" in t and "/model" in t)
        r, t = self.say("/halp")
        self.assertIn("Unknown command", t)
        self.assertIn("/help", t)
        self.no_model_call()

    def test_status_model_mode(self):
        _, t = self.say("/status")
        self.assertIn(self.bot["name"], t)
        self.say("/model gpt-test-1")
        self.assertEqual(self.eng.bots.get(self.bot["id"])["model"], "gpt-test-1")
        self.say("/model default")
        self.assertEqual(self.eng.bots.get(self.bot["id"])["model"], "")
        self.say("/mode auto")
        self.assertEqual(self.eng.bots.get(self.bot["id"])["approval_mode"], "auto_review")
        self.say("/mode ask")
        self.assertEqual(self.eng.bots.get(self.bot["id"])["approval_mode"], "ask")
        self.no_model_call()

    def test_memory_commands(self):
        self.say("/remember Keep replies under five lines")
        mem = self.eng.memory.list(self.bot["id"])
        self.assertEqual(len(mem), 1)
        self.assertTrue(mem[0]["pinned"])
        _, t = self.say("/memory")
        self.assertIn("Keep replies", t)
        self.say(f"/forget #{mem[0]['id']}")
        self.assertEqual(self.eng.memory.list(self.bot["id"]), [])
        self.no_model_call()

    def test_new_thread_and_rename(self):
        r, _ = self.say("/new Planning")
        self.assertIn("switch_thread", r)
        self.assertEqual(self.eng.threads.get(r["switch_thread"])["title"], "Planning")
        self.say("/rename Weekly plan")
        self.assertEqual(self.eng.threads.get(self.th["id"])["title"], "Weekly plan")

    def test_pause_resume(self):
        self.say("/pause")
        self.assertTrue(self.eng.bots.get(self.bot["id"])["paused"])
        self.say("/resume")
        self.assertFalse(self.eng.bots.get(self.bot["id"])["paused"])

    def _approval(self, category, summary):
        aid = self.eng.db.insert("approvals", {"id": f"ap{self.eng.db.scalar('SELECT COUNT(*) FROM approvals', (), 0)}{category}", "bot_id": self.bot["id"],
                                               "thread_id": self.th["id"], "turn_id": "", "status": "pending", "category": category, "tool": "t",
                                               "summary": summary, "details": "{}", "created_at": __import__("time").time() + len(summary) / 1000})
        return aid

    def test_approve_rules(self):
        db = self.eng.db
        cols = [r["name"] for r in db.query("PRAGMA table_info(approvals)")]
        if "id" not in cols:
            self.skipTest("unexpected approvals schema")
        a1 = self._approval("send", "Send email to Ana")
        a2 = self._approval("purchase", "Buy the thing")
        _, t = self.say("/approvals")
        self.assertIn("Send email to Ana", t)
        self.assertIn("Buy the thing", t)
        _, t = self.say("/approve all")
        self.assertIn("Left 1", t)                       # the purchase is never approved in bulk
        left = {a["id"]: a["status"] for a in self.eng.approvals.list(None, self.bot["id"])}
        self.assertNotEqual(left.get(a2), "approved")
        self.say("/deny all")
        self.assertEqual({a["status"] for a in self.eng.approvals.list(None, self.bot["id"])} - {"denied", "approved"}, set())
        self.no_model_call()

    def test_skill_command_goes_to_the_model(self):
        (self.eng.skills.dir / "cmd-demo.md").write_text("---\nname: cmd-demo\ndescription: Demo skill\nstatus: active\n---\n# Demo\n1. Do it\n", encoding="utf-8")
        _, t = self.say("/skills")
        self.assertIn("cmd-demo", t)
        r = self.eng.send_user_message(self.th["id"], "/skill cmd-demo be quick")
        self.assertTrue(r["started"], "a skill command starts a normal turn")
        user_msgs = [i["text"] for i in self.eng.threads.display(self.th["id"]) if i["type"] == "user"]
        self.assertTrue(any("cmd-demo" in m and "be quick" in m for m in user_msgs))

    def test_escape_and_paths_are_plain_messages(self):
        for text, expect in (("//status please", "/status please"), ("/tmp/notes.txt is the file", "/tmp/notes.txt is the file")):
            r = self.eng.send_user_message(self.th["id"], text)
            self.assertTrue(r["started"])
            users = [i["text"] for i in self.eng.threads.display(self.th["id"]) if i["type"] == "user"]
            self.assertIn(expect, users)

    def test_group_scope(self):
        a = self.eng.bots.create(f"GA{id(self) % 9999}", job="x")
        b = self.eng.bots.create(f"GB{id(self) % 9999}", job="x")
        g = self.eng.messaging.create_group("cmd room", [a["id"], b["id"]], lead=a["id"])
        r = self.eng.send_user_message(g["thread_id"], "/model x")
        notes = [i for i in self.eng.threads.display(g["thread_id"]) if i["type"] == "notice"]
        self.assertIn("Bot's own chat", notes[-1]["text"])
        self.eng.send_user_message(g["thread_id"], "/usage")
        self.assertIn("This week", [i for i in self.eng.threads.display(g["thread_id"]) if i["type"] == "notice"][-1]["text"])


if __name__ == "__main__":
    unittest.main()
