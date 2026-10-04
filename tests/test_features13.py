"""1.3 features: global search, Do Not Disturb / quiet hours, per-Bot daily budget, chat export and /retry."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import agent as agent_mod, quiet  # noqa: E402
from core.db import Database  # noqa: E402
from core.engine import Engine  # noqa: E402
from core.providers import LLMResult  # noqa: E402
from core.providers import ToolCall  # noqa: E402
from tests.test_engine import FakeProvider, wait_for  # noqa: E402


class QuietUnit(unittest.TestCase):
    def test_window_crossing_midnight(self):
        self.assertTrue(quiet.in_window(23 * 60, 22 * 60, 7 * 60))
        self.assertTrue(quiet.in_window(6 * 60 + 59, 22 * 60, 7 * 60))
        self.assertFalse(quiet.in_window(7 * 60, 22 * 60, 7 * 60))
        self.assertFalse(quiet.in_window(12 * 60, 22 * 60, 7 * 60))
        self.assertTrue(quiet.in_window(13 * 60, 12 * 60, 14 * 60))
        self.assertFalse(quiet.in_window(5, 480, 480))           # empty window

    def test_is_quiet(self):
        noon = datetime(2026, 1, 5, 12, 0).timestamp()
        night = datetime(2026, 1, 5, 23, 30).timestamp()
        cfg = {"quiet_enabled": True, "quiet_start": "22:00", "quiet_end": "07:00", "dnd_until": 0}
        self.assertFalse(quiet.is_quiet(cfg, noon))
        self.assertTrue(quiet.is_quiet(cfg, night))
        self.assertFalse(quiet.is_quiet({**cfg, "quiet_enabled": False}, night))
        self.assertTrue(quiet.is_quiet({**cfg, "quiet_enabled": False, "dnd_until": noon + 60}, noon))   # DND overrides
        self.assertFalse(quiet.is_quiet({**cfg, "quiet_enabled": False, "dnd_until": noon - 1}, noon))
        self.assertTrue(quiet.in_window(1, 22 * 60, quiet.minutes_of("garbage", 7 * 60)))                  # bad time falls back

    def test_durations(self):
        self.assertEqual(quiet.parse_duration("30m"), 1800)
        self.assertEqual(quiet.parse_duration("2h"), 7200)
        self.assertEqual(quiet.parse_duration("1.5 hours"), 5400)
        self.assertEqual(quiet.parse_duration("45"), 2700)
        self.assertEqual(quiet.parse_duration("1d"), 86400)
        for bad in ("", "soon", "-5m", "0", "99d"):
            self.assertIsNone(quiet.parse_duration(bad), bad)


class Migration(unittest.TestCase):
    def test_old_database_gets_the_new_column(self):
        path = Path(tempfile.mkdtemp(prefix="gbtest-mig-")) / "old.db"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE bots(id TEXT PRIMARY KEY, name TEXT NOT NULL, emoji TEXT DEFAULT '', job TEXT DEFAULT '', instructions TEXT DEFAULT '', "
                    "profile TEXT DEFAULT '', model TEXT DEFAULT '', approval_mode TEXT DEFAULT 'ask', step_limit INTEGER DEFAULT 40, net_mode TEXT DEFAULT 'inherit', "
                    "net_allow TEXT DEFAULT '[]', net_deny TEXT DEFAULT '[]', grants TEXT DEFAULT '[]', proactive TEXT DEFAULT 'off', template TEXT DEFAULT '', "
                    "paused INTEGER DEFAULT 0, archived INTEGER DEFAULT 0, created_at REAL, updated_at REAL)")
        con.execute("INSERT INTO bots(id,name) VALUES('b1','Old Bot')")
        con.commit()
        con.close()
        db = Database(path)
        row = db.one("SELECT * FROM bots WHERE id='b1'")
        self.assertEqual(row["daily_token_limit"], 0)
        db.close()
        Database(path).close()    # opening again must not fail on the existing column


class Features(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-f13-")
        cls.eng = Engine()
        cls.eng.settings.set("notifications.toast", False)
        cls.fake = FakeProvider([LLMResult(text="ok")] * 30)
        cls._orig = agent_mod.make_provider
        agent_mod.make_provider = lambda *a, **k: cls.fake
        cls.eng.start()

    @classmethod
    def tearDownClass(cls):
        agent_mod.make_provider = cls._orig
        cls.eng.stop()

    def setUp(self):
        self.bot = self.eng.bots.create(f"Feat{id(self) % 99999}{int(time.time() * 1000) % 100000}", job="test", emoji="🧪")
        self.th = self.eng.threads.main_thread(self.bot["id"])
        self.eng.settings.set("notifications.dnd_until", 0)
        self.eng.settings.set("notifications.quiet_enabled", False)

    def say(self, text, thread=None):
        tid = thread or self.th["id"]
        r = self.eng.send_user_message(tid, text)
        notes = [i for i in self.eng.threads.display(tid) if i["type"] == "notice"]
        return r, (notes[-1]["text"] if notes else "")

    def turns(self):
        return self.eng.db.scalar("SELECT COUNT(*) FROM turns WHERE bot_id=?", (self.bot["id"],), 0)

    # -------------------------------------------------------------- search
    def test_search_finds_messages_and_memories(self):
        t = self.eng.threads
        t.add(self.th["id"], "user", "user", "Please book the Lisbon flights for the offsite")
        t.add(self.th["id"], self.bot["id"], "assistant", "Lisbon flights are booked: TAP 123 on Monday.")
        t.add(self.th["id"], "user", "user", "Unrelated message about taxes")
        self.eng.memory.add(self.bot["id"], "fact", "Prefers aisle seats on Lisbon trips")
        r = self.eng.search.run("lisbon flights")
        self.assertEqual(len(r["messages"]), 2)
        self.assertEqual({m["who"] for m in r["messages"]}, {"You", self.bot["name"]})
        self.assertTrue(all(m["thread_id"] == self.th["id"] and m["where"] == self.bot["name"] for m in r["messages"]))
        self.assertIn("Lisbon", r["messages"][0]["snippet"])
        self.assertEqual(self.eng.search.run("lisbon aisle")["memories"][0]["where"], self.bot["name"])
        self.assertEqual(len(self.eng.search.run("lisbon aisle")["messages"]), 0)          # all words must match in one message
        self.assertEqual(self.eng.search.run("")["messages"], [])
        self.assertEqual(self.eng.search.run("a")["messages"], [])                         # too short

    def test_search_wildcards_are_literal(self):
        self.eng.threads.add(self.th["id"], "user", "user", "discount is 50% off today")
        self.eng.threads.add(self.th["id"], "user", "user", "plain text here")
        self.assertEqual(len(self.eng.search.run("50%")["messages"]), 1)
        self.assertEqual(len(self.eng.search.run("%%")["messages"]), 0)
        self.assertEqual(len(self.eng.search.run("_ext")["messages"]), 0)

    def test_search_ignores_tool_results_and_notices(self):
        self.eng.threads.add(self.th["id"], self.bot["id"], "user", [{"type": "tool_result", "tool_use_id": "x", "content": [{"type": "text", "text": "zebracode in a tool result"}]}])
        self.eng.threads.notice(self.th["id"], "zebracode in a notice")
        self.assertEqual(self.eng.search.run("zebracode")["messages"], [])

    def test_search_group_chat_names_the_group(self):
        a = self.eng.bots.create(f"GA{int(time.time() * 1000) % 100000}", job="x")
        b = self.eng.bots.create(f"GB{int(time.time() * 1000) % 100000}", job="x")
        g = self.eng.messaging.create_group("Planning room", [a["id"], b["id"]], lead=a["id"])
        self.eng.threads.add(g["thread_id"], "user", "user", "quarterly roadmap kickoff")
        hit = self.eng.search.run("roadmap kickoff")["messages"][0]
        self.assertEqual(hit["where"], "Planning room")
        self.assertEqual(hit["thread_id"], g["thread_id"])

    def test_search_command(self):
        self.eng.threads.add(self.th["id"], "user", "user", "the quokka report is due")
        _, t = self.say("/search quokka")
        self.assertIn("quokka", t)
        _, t = self.say("/search nothingmatcheshere")
        self.assertIn("Nothing found", t)
        _, t = self.say("/search")
        self.assertIn("Search for what", t)

    # ----------------------------------------------------------------- DND
    def test_notifications_are_muted_during_dnd(self):
        tag = f"Done-{time.time()}"
        self.eng.notify("finished", self.bot, self.th["id"], tag + "-a", "body")
        self.eng.settings.set("notifications.dnd_until", time.time() + 600)
        self.eng.notify("finished", self.bot, self.th["id"], tag + "-b", "body")
        notes = [e for e in self.eng.events.since(0) if e.get("type") == "notification" and e.get("title", "").startswith(tag)]   # (notification events carry their own id)
        self.assertEqual([(n["title"][-1], n["muted"]) for n in notes], [("a", False), ("b", True)])

    def test_dnd_still_records_the_notification(self):
        self.eng.settings.set("notifications.dnd_until", time.time() + 600)
        before = self.eng.db.scalar("SELECT COUNT(*) FROM notifications", (), 0)
        self.eng.notify("approval", self.bot, self.th["id"], "Needs you", "x", urgent=True)
        self.assertEqual(self.eng.db.scalar("SELECT COUNT(*) FROM notifications", (), 0), before + 1)

    def test_dnd_command(self):
        _, t = self.say("/dnd 2h")
        self.assertIn("Do Not Disturb until", t)
        until = float(self.eng.settings.get("notifications.dnd_until"))
        self.assertAlmostEqual(until - time.time(), 7200, delta=30)
        _, t = self.say("/dnd")
        self.assertIn("Do Not Disturb until", t)
        _, t = self.say("/dnd off")
        self.assertIn("is off", t)
        self.assertEqual(float(self.eng.settings.get("notifications.dnd_until")), 0)
        _, t = self.say("/dnd soon")
        self.assertIn("How long", t)
        self.assertEqual(self.turns(), 0)

    # -------------------------------------------------------------- budget
    def test_budget_command_sets_and_clears(self):
        _, t = self.say("/budget 50k")
        self.assertEqual(self.eng.bots.get(self.bot["id"])["daily_token_limit"], 50000)
        _, t = self.say("/budget")
        self.assertIn("50.0k", t)
        _, t = self.say("/budget 1.5m")
        self.assertEqual(self.eng.bots.get(self.bot["id"])["daily_token_limit"], 1_500_000)
        _, t = self.say("/budget off")
        self.assertEqual(self.eng.bots.get(self.bot["id"])["daily_token_limit"], 0)
        _, t = self.say("/budget lots")
        self.assertIn("Give a number", t)

    def test_budget_blocks_new_turns_when_used_up(self):
        self.eng.bots.update(self.bot["id"], daily_token_limit=1000)
        self.eng.usage.record(self.bot["id"], "t0", "p", "m", 800, 300)       # 1,100 tokens today
        self.assertTrue(self.eng.usage.bot_over_budget(self.eng.bots.get(self.bot["id"])))
        r, t = self.say("hello there")
        self.assertEqual(r["started"], [])
        self.assertIn("daily budget", t)
        self.assertEqual(self.turns(), 0)

    def test_budget_stops_a_running_turn_between_steps(self):
        """Each model call is 120 tokens in the fake provider: with a 200 budget the Bot gets two steps, then stops mid-task."""
        self.eng.bots.update(self.bot["id"], daily_token_limit=200)
        self.fake.script = [LLMResult(text="", tool_calls=[ToolCall(f"c{i}", "fs_list", {"path": "shared"})]) for i in range(12)]
        r = self.eng.send_user_message(self.th["id"], "keep listing files")
        self.assertTrue(r["started"])
        self.assertTrue(wait_for(lambda: not self.eng.turns.is_busy(self.bot["id"])))
        turn = self.eng.db.one("SELECT * FROM turns WHERE bot_id=? ORDER BY started_at DESC", (self.bot["id"],))
        self.assertEqual(turn["status"], "error")
        self.assertEqual(turn["steps"], 3)
        self.assertIn("Daily budget", turn["error"])
        self.assertLess(len(self.fake.script), 12)
        self.assertGreater(len(self.fake.script), 8)           # it did not run all twelve
        self.fake.script = [LLMResult(text="ok")] * 30

    def test_budget_does_not_block_other_bots_or_unlimited(self):
        other = self.eng.bots.create(f"Other{int(time.time() * 1000) % 100000}", job="x")
        self.eng.bots.update(self.bot["id"], daily_token_limit=1000)
        self.eng.usage.record(self.bot["id"], "t1", "p", "m", 5000, 0)
        self.assertFalse(self.eng.usage.bot_over_budget(self.eng.bots.get(other["id"])))
        self.eng.bots.update(self.bot["id"], daily_token_limit=0)
        self.assertFalse(self.eng.usage.bot_over_budget(self.eng.bots.get(self.bot["id"])))

    def test_budget_counts_only_today(self):
        self.eng.bots.update(self.bot["id"], daily_token_limit=1000)
        self.eng.db.insert("usage", {"ts": self.eng.usage.day_start() - 3600, "bot_id": self.bot["id"], "turn_id": "old", "profile": "p", "model": "m",
                                     "input_tokens": 9000, "output_tokens": 0})
        self.assertEqual(self.eng.usage.bot_today(self.bot["id"]), 0)
        self.assertFalse(self.eng.usage.bot_over_budget(self.eng.bots.get(self.bot["id"])))

    def test_budget_in_usage_summary_and_commands(self):
        self.eng.bots.update(self.bot["id"], daily_token_limit=2000)
        self.eng.usage.record(self.bot["id"], "t2", "p", "m", 500, 100)
        self.assertEqual(self.eng.usage.summary()["today"][self.bot["id"]], 600)
        _, t = self.say("/usage")
        self.assertIn("Daily budgets", t)
        _, t = self.say("/status")
        self.assertIn("daily budget", t)

    def test_budget_validation(self):
        b = self.eng.bots.update(self.bot["id"], daily_token_limit=-5)
        self.assertEqual(b["daily_token_limit"], 0)
        b = self.eng.bots.update(self.bot["id"], daily_token_limit="12000")
        self.assertEqual(b["daily_token_limit"], 12000)

    # -------------------------------------------------------- export + retry
    def test_export_markdown(self):
        t = self.eng.threads
        t.add(self.th["id"], "user", "user", "What is 2+2?")
        t.add(self.th["id"], self.bot["id"], "assistant", "It is **4**.")
        t.notice(self.th["id"], "a notice that should not be exported")
        md = t.export_markdown(self.th["id"])
        self.assertIn("**You**", md)
        self.assertIn("What is 2+2?", md)
        self.assertIn(f"**{self.bot['name']}**", md)
        self.assertIn("It is **4**.", md)
        self.assertNotIn("a notice that should not", md)
        self.assertTrue(md.startswith("# "))

    def test_export_command_writes_a_file(self):
        self.eng.threads.add(self.th["id"], "user", "user", "export me please")
        _, t = self.say("/export")
        self.assertIn("shared/exports/", t)
        files = list((self.eng.computer.workspace / "shared" / "exports").glob("*.md"))
        self.assertTrue(any("export me please" in f.read_text(encoding="utf-8") for f in files))

    def test_retry_resends_the_last_message(self):
        _, t = self.say("/retry")
        self.assertIn("no earlier message", t)
        self.eng.threads.add(self.th["id"], "user", "user", "summarise my week")
        r = self.eng.send_user_message(self.th["id"], "/retry")
        self.assertTrue(r["started"])
        users = [i["text"] for i in self.eng.threads.display(self.th["id"]) if i["type"] == "user"]
        self.assertEqual(users.count("summarise my week"), 2)


if __name__ == "__main__":
    unittest.main()
