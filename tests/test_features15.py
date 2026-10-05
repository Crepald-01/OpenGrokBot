"""1.5 backend: workspace files, digest, estimated cost, backup/restore, update check, duplicate Bot, pause/resume all, and their API."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from core import backup, paths  # noqa: E402
from core.engine import Engine  # noqa: E402
from core.files import FileError, Files  # noqa: E402
from core.pricing import Pricing, fmt_money  # noqa: E402
from core.updates import Updates, is_newer, parse  # noqa: E402
from service.server import create_app  # noqa: E402

TOKEN = "t15"
_N = __import__("itertools").count(1000)     # unique Bot names even when tests run within the same millisecond


def fresh_home() -> str:
    d = tempfile.mkdtemp(prefix="gbtest-f15-")
    os.environ["OPENGROKBOT_HOME"] = d
    return d


class FilesUnit(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="gbtest-files-"))
        (self.root / "shared").mkdir()
        (self.root / "shared" / "notes.md").write_text("# Notes\nhello", encoding="utf-8")
        (self.root / "shared" / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (self.root / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 50)
        (self.root / "blob.bin").write_bytes(b"\x00\x01\x02\xff" * 100)
        (self.root / ".hidden").write_text("secret", encoding="utf-8")
        (self.root / "plain.weird").write_text("just text with an odd extension", encoding="utf-8")
        self.f = Files(self.root)

    def test_list_sorts_folders_first_and_hides_dotfiles(self):
        r = self.f.list("")
        names = [e["name"] for e in r["entries"]]
        self.assertEqual(names[0], "shared")
        self.assertNotIn(".hidden", names)
        self.assertEqual(r["parent"], None)
        sub = self.f.list("shared")
        self.assertEqual(sub["parent"], "")
        self.assertEqual({e["name"] for e in sub["entries"]}, {"notes.md", "data.csv"})

    def test_paths_cannot_escape(self):
        for bad in ("..", "../x", "shared/../../etc", "/etc/passwd", "C:/Windows/win.ini", "shared/..\\..\\x"):
            with self.assertRaises(FileError, msg=bad):
                self.f.preview(bad)
        with self.assertRaises(FileError):
            self.f.delete("..")

    def test_preview_kinds(self):
        t = self.f.preview("shared/notes.md")
        self.assertEqual((t["kind"], t["text"].splitlines()[0]), ("text", "# Notes"))
        self.assertEqual(self.f.preview("pic.png")["kind"], "image")
        self.assertEqual(self.f.preview("blob.bin")["kind"], "other")
        self.assertEqual(self.f.preview("plain.weird")["kind"], "text")          # plainly text, so it previews
        big = self.root / "big.txt"
        big.write_text("x" * 300_000, encoding="utf-8")
        p = self.f.preview("big.txt")
        self.assertTrue(p["truncated"] and len(p["text"]) == 200_000)

    def test_search_and_recent(self):
        self.assertEqual([e["name"] for e in self.f.search("notes")], ["notes.md"])
        self.assertEqual(len(self.f.search("shared csv")), 1)                     # all words must be in the path
        self.assertEqual(self.f.search(""), [])
        os.utime(self.root / "shared" / "data.csv", (time.time() + 100, time.time() + 100))
        self.assertEqual(self.f.recent(1)[0]["name"], "data.csv")
        self.assertNotIn(".hidden", [e["name"] for e in self.f.recent(50)])
        self.assertEqual(self.f.stats()["files"], 5)

    def test_delete_only_files(self):
        self.f.delete("shared/notes.md")
        self.assertFalse((self.root / "shared" / "notes.md").exists())
        with self.assertRaises(FileError):
            self.f.delete("shared")                                              # folders are not deleted here
        with self.assertRaises(FileError):
            self.f.delete("missing.txt")


class Features(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fresh_home()
        cls.eng = Engine()
        cls.eng.settings.set("notifications.toast", False)
        cls.eng.start()
        cls.c = TestClient(create_app(cls.eng, TOKEN))
        cls.h = {"Authorization": f"Bearer {TOKEN}"}

    @classmethod
    def tearDownClass(cls):
        cls.eng.stop()

    def setUp(self):
        self.bot = self.eng.bots.create(f"F15-{next(_N)}", job="Does research", instructions="Be brief.", emoji="🧪")
        self.th = self.eng.threads.main_thread(self.bot["id"])

    def say(self, text):
        self.eng.send_user_message(self.th["id"], text)
        notes = [i for i in self.eng.threads.display(self.th["id"]) if i["type"] == "notice"]
        return notes[-1]["text"] if notes else ""

    # ------------------------------------------------------------------ digest
    def _activity(self, when=None):
        when = when or time.time()
        db, bid = self.eng.db, self.bot["id"]
        for i, status in enumerate(("done", "done", "error")):
            db.insert("turns", {"id": f"t{bid}{i}{int(when)}", "bot_id": bid, "thread_id": self.th["id"], "trigger": "user", "status": status, "started_at": when, "ended_at": when})
        self.eng.actions.record(bot_id=bid, tool="fs_write", args="{}", path="shared/report.md")
        self.eng.actions.record(bot_id=bid, tool="browser_open", args="{}", url="https://www.example.com/page")
        self.eng.actions.record(bot_id=bid, tool="browser_open", args="{}", url="https://news.site.org/x")
        self.eng.actions.record(bot_id=bid, tool="fs_write", args="{}", path="shared/denied.md", status="denied")
        self.eng.usage.record(bid, "tt", "p", "m", 4000, 1000)
        db.insert("approvals", {"id": f"ap{bid}", "bot_id": bid, "thread_id": self.th["id"], "turn_id": "", "category": "send", "tool": "x", "summary": "s", "details": "{}",
                                "status": "approved", "created_at": when, "decided_at": when})

    def test_digest_counts_what_happened(self):
        self._activity()
        d = self.eng.digest.build("today")
        row = next(r for r in d["bots"] if r["bot_id"] == self.bot["id"])
        self.assertEqual((row["tasks"], row["finished"], row["problems"]), (3, 2, 1))
        self.assertEqual(row["files"], ["shared/report.md"])                      # the denied write is not counted
        self.assertEqual(sorted(row["sites"]), ["example.com", "news.site.org"])
        self.assertEqual((row["approved"], row["tokens"]), (1, 5000))
        self.assertEqual(row["top_tools"][0], ("fs_write", 2) if row["top_tools"][0][1] == 2 else row["top_tools"][0])
        md = self.eng.digest.markdown(d)
        self.assertIn("# Today", md)
        self.assertIn(self.bot["name"], md)
        self.assertIn("shared/report.md", md)
        self.assertIn("example.com", md)
        self.assertIn("task", self.eng.digest.headline(d))

    def test_digest_windows_and_quiet_bots(self):
        quiet = self.eng.bots.create(f"Quiet-{next(_N)}", job="x")
        d = self.eng.digest.build("today")
        self.assertIn(quiet["name"], d["quiet"])
        old = self.eng.bots.create(f"Old-{next(_N)}", job="x")
        self.eng.actions.record(bot_id=old["id"], tool="fs_write", args="{}", path="a.txt")
        self.eng.db.execute("UPDATE actions SET ts=? WHERE bot_id=?", (time.time() - 3 * 86400, old["id"]))
        self.assertNotIn(old["id"], [r["bot_id"] for r in self.eng.digest.build("today")["bots"]])
        self.assertIn(old["id"], [r["bot_id"] for r in self.eng.digest.build("week")["bots"]] + [old["id"]])
        self.assertEqual(self.eng.digest.build("nonsense")["label"], "Today")

    def test_digest_command(self):
        self._activity()
        t = self.say("/digest")
        self.assertIn(self.bot["name"], t)
        self.assertIn("shared/report.md", t)
        self.assertIn("Choose one of", self.say("/digest someday"))

    def test_digest_notification_sent_once_per_day(self):
        self.eng.settings.set("digest", {"enabled": False, "time": "00:00", "last_sent": ""})
        before = self.eng.db.scalar("SELECT COUNT(*) FROM notifications WHERE kind='digest'", (), 0)
        self.eng._digest_tick()
        self.assertEqual(self.eng.db.scalar("SELECT COUNT(*) FROM notifications WHERE kind='digest'", (), 0), before)      # off
        self.eng.settings.set("digest", {"enabled": True, "time": "00:00", "last_sent": ""})
        self.eng._digest_tick()
        self.eng._digest_tick()
        self.assertEqual(self.eng.db.scalar("SELECT COUNT(*) FROM notifications WHERE kind='digest'", (), 0), before + 1)  # once
        self.eng.settings.set("digest", {"enabled": True, "time": "23:59", "last_sent": "1999-01-01"})
        before = self.eng.db.scalar("SELECT COUNT(*) FROM notifications WHERE kind='digest'", (), 0)
        if time.strftime("%H:%M") < "23:59":
            self.eng._digest_tick()
            self.assertEqual(self.eng.db.scalar("SELECT COUNT(*) FROM notifications WHERE kind='digest'", (), 0), before)  # not yet time
        self.eng.settings.set("digest", {"enabled": False, "time": "18:00", "last_sent": ""})

    # -------------------------------------------------------------------- cost
    def test_pricing_rules(self):
        p = Pricing(self.eng.settings)
        self.assertIsNone(p.price("openai", "gpt-x"))
        self.assertEqual(p.price("ollama", "llama"), (0.0, 0.0))
        self.assertEqual(p.price("openrouter", "some/model:free"), (0.0, 0.0))
        p.set_prices({"openai/gpt-x": {"in": 2.5, "out": 10}, "bad": {"in": "x"}, "neg/m": {"in": -3, "out": 1}}, "€")
        self.assertEqual(p.price("openai", "gpt-x"), (2.5, 10.0))
        self.assertEqual(p.price("neg", "m"), (0.0, 1.0))                          # negatives clamp to zero
        self.assertIsNone(p.price("bad", ""))
        self.assertEqual(p.currency(), "€")
        self.assertAlmostEqual(p.cost(2_000_000, 500_000, "openai", "gpt-x"), 2.5 * 2 + 10 * 0.5)
        self.assertEqual(fmt_money(1234.5, "€"), "€1,234.50")
        self.assertEqual(fmt_money(0.0042), "$0.0042")
        self.assertEqual(fmt_money(None), "?")
        p.set_prices({}, "$")

    def test_cost_summary_by_bot_and_unpriced(self):
        bid = self.bot["id"]
        self.eng.usage.pricing.set_prices({"openai/gpt-x": {"in": 3, "out": 15}}, "$")
        self.eng.usage.record(bid, "c1", "openai", "gpt-x", 1_000_000, 100_000)       # 3 + 1.5
        self.eng.usage.record(bid, "c2", "mystery", "m1", 500_000, 0)                  # no price
        c = self.eng.usage.cost_summary()
        self.assertAlmostEqual(c["per_bot"][bid], 4.5)
        self.assertIn("mystery/m1", c["unpriced"])
        self.assertGreaterEqual(c["total"], 4.5)
        self.assertGreaterEqual(c["today"], 4.5)
        self.assertEqual(self.eng.usage.summary()["cost"]["currency"], "$")
        t = self.say("/cost")
        self.assertIn("This week", t)
        self.assertIn("no price yet", t)
        self.eng.usage.pricing.set_prices({}, "$")

    def test_pricing_api_roundtrip(self):
        r = self.c.put("/api/pricing", headers=self.h, json={"models": {"openai/x": {"in": 1, "out": 2}}, "currency": "$"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["prices"]["openai/x"], {"in": 1.0, "out": 2.0})
        self.c.put("/api/pricing", headers=self.h, json={"models": {}})

    # -------------------------------------------------------------- duplicate
    def test_duplicate_copies_setup_not_access_or_history(self):
        self.eng.bots.update(self.bot["id"], model="m1", step_limit=25, proactive="suggest", daily_token_limit=7000)
        self.eng.bots.grant(self.bot["id"], "plugin:gmail")
        self.eng.memory.add(self.bot["id"], "fact", "remember me")
        self.eng.threads.add(self.th["id"], "user", "user", "history")
        a = self.eng.bots.duplicate(self.bot["id"])
        self.assertEqual(a["name"], self.bot["name"] + " copy")
        self.assertEqual((a["job"], a["instructions"], a["emoji"], a["model"], a["step_limit"], a["proactive"], a["daily_token_limit"]),
                         (self.bot["job"], self.bot["instructions"], "🧪", "m1", 25, "suggest", 7000))
        self.assertEqual(a["grants"], [])
        self.assertEqual(self.eng.memory.list(a["id"]), [])
        b = self.eng.bots.duplicate(self.bot["id"])
        self.assertEqual(b["name"], self.bot["name"] + " copy 2")
        r = self.c.post(f"/api/bots/{self.bot['id']}/duplicate", headers=self.h, json={"name": "Clone X"})
        self.assertEqual(r.json()["name"], "Clone X")
        self.assertEqual(self.c.post("/api/bots/nope/duplicate", headers=self.h, json={}).status_code, 400)

    # ------------------------------------------------------------- pause all
    def test_pause_all_and_resume_all(self):
        other = self.eng.bots.create(f"P-{next(_N)}", job="x")
        self.eng.bots.update(other["id"], paused=True)
        t = self.say("/pauseall")
        self.assertIn("Paused", t)
        self.assertTrue(all(b["paused"] for b in self.eng.bots.list()))
        self.assertIn("already paused", self.say("/pauseall"))
        self.assertIn("Resumed", self.say("/resumeall"))
        self.assertFalse(any(b["paused"] for b in self.eng.bots.list()))
        self.assertIn("No Bot was paused", self.say("/resumeall"))
        r = self.c.post("/api/bots/pause_all", headers=self.h, json={"paused": True}).json()
        self.assertTrue(r["paused"] and r["changed"] >= 1)
        self.c.post("/api/bots/pause_all", headers=self.h, json={"paused": False})

    # --------------------------------------------------------------------- API
    def test_workspace_api(self):
        ws = paths.workspace_dir()
        (ws / "shared" / "api-test.md").write_text("# hi", encoding="utf-8")
        r = self.c.get("/api/ws/list", headers=self.h, params={"path": "shared"}).json()
        self.assertIn("api-test.md", [e["name"] for e in r["entries"]])
        self.assertEqual(self.c.get("/api/ws/list", headers=self.h, params={"path": "../.."}).status_code, 400)
        self.assertEqual(self.c.get("/api/ws/preview", headers=self.h, params={"path": "shared/api-test.md"}).json()["text"], "# hi")
        raw = self.c.get("/api/ws/raw", headers=self.h, params={"path": "shared/api-test.md"})
        self.assertEqual((raw.status_code, raw.content), (200, b"# hi"))
        self.assertIn("api-test", " ".join(e["name"] for e in self.c.get("/api/ws/search", headers=self.h, params={"q": "api-test"}).json()["entries"]))
        self.assertIn("stats", self.c.get("/api/ws/recent", headers=self.h).json())
        self.assertEqual(self.c.delete("/api/ws/file", headers=self.h, params={"path": "shared"}).status_code, 400)
        self.assertEqual(self.c.delete("/api/ws/file", headers=self.h, params={"path": "shared/api-test.md"}).status_code, 200)
        self.assertEqual(self.c.get("/api/ws/raw", headers=self.h, params={"path": "shared/api-test.md"}).status_code, 400)
        self.assertEqual(self.c.get("/api/ws/list").status_code, 401)

    def test_digest_api(self):
        self._activity()
        d = self.c.get("/api/digest", headers=self.h, params={"spec": "today"}).json()
        self.assertIn("markdown", d)
        self.assertIn(self.bot["name"], d["markdown"])

    # ----------------------------------------------------------------- updates
    def test_update_check_logic(self):
        self.assertEqual(parse("v1.5.0"), (1, 5, 0))
        self.assertTrue(is_newer("1.10.0", "1.9.9"))
        self.assertTrue(is_newer("1.5.1", "1.5.0"))
        self.assertFalse(is_newer("1.5.0", "1.5.0"))
        self.assertFalse(is_newer("1.4.9", "1.5.0"))
        calls = []

        def fetch():
            calls.append(1)
            return {"tag_name": "v9.9.9", "html_url": "https://example.test/rel", "name": "OpenGrokBot 9.9.9"}
        self.eng.settings.set("updates_state", {})
        u = Updates(self.eng.settings, "1.5.0", fetch)
        s = u.check()
        self.assertTrue(s["newer"] and s["latest"] == "9.9.9" and s["url"].endswith("/rel"))
        u.check()
        self.assertEqual(len(calls), 1)                                           # cached for a day
        u.check(force=True)
        self.assertEqual(len(calls), 2)
        self.eng.settings.set("updates.check", False)
        self.eng.settings.set("updates_state", {})
        Updates(self.eng.settings, "1.5.0", fetch).check()
        self.assertEqual(len(calls), 2)                                           # switched off: no request
        self.eng.settings.set("updates.check", True)
        self.eng.settings.set("updates_state", {})

    def test_update_check_survives_errors(self):
        def boom():
            raise OSError("offline")
        self.eng.settings.set("updates_state", {})
        s = Updates(self.eng.settings, "1.5.0", boom).check()
        self.assertFalse(s["newer"])
        self.assertIn("offline", s["error"])
        self.assertEqual(self.c.get("/api/updates", headers=self.h).status_code, 200)
        self.eng.settings.set("updates_state", {})


class BackupTests(unittest.TestCase):
    def test_backup_roundtrip_and_safety(self):
        home = fresh_home()
        eng = Engine()
        eng.start()
        bot = eng.bots.create("Keeper", job="remember things", emoji="🗄️")
        th = eng.threads.main_thread(bot["id"])
        eng.threads.add(th["id"], "user", "user", "a message that must survive")
        eng.memory.add(bot["id"], "fact", "a memory that must survive")
        (paths.skills_dir() / "my-skill.md").write_text("---\nname: my-skill\ndescription: d\nstatus: active\n---\n# s\n", encoding="utf-8")
        (paths.workspace_dir() / "shared" / "keep.txt").write_text("workspace file", encoding="utf-8")
        eng.settings.set("theme", "light")
        data = backup.create(eng)
        z = zipfile.ZipFile(io.BytesIO(data))
        names = z.namelist()
        self.assertIn("manifest.json", names)
        self.assertIn(backup.DB_NAME, names)
        self.assertIn("skills/my-skill.md", names)
        self.assertFalse(any(n.startswith("workspace/") for n in names))           # not included unless asked
        self.assertFalse(any("token" in n.lower() or "keyring" in n.lower() for n in names))
        with_ws = zipfile.ZipFile(io.BytesIO(backup.create(eng, include_workspace=True)))
        self.assertIn("workspace/shared/keep.txt", with_ws.namelist())

        # break things, stage the backup, and restart the engine on the same data folder
        eng.bots.delete(bot["id"])
        (paths.skills_dir() / "my-skill.md").unlink()
        eng.settings.set("theme", "dark")
        info = backup.stage_restore(data)
        self.assertEqual((info["bots"], info["threads"]), (1, 1))
        self.assertTrue((backup.pending_dir() / backup.DB_NAME).exists())
        eng.stop()
        eng.db.close()
        eng2 = Engine()
        self.assertFalse(backup.pending_dir().exists())
        self.assertEqual([b["name"] for b in eng2.bots.list()], ["Keeper"])
        self.assertEqual(eng2.memory.list(bot["id"])[0]["text"], "a memory that must survive")
        self.assertEqual(len(eng2.threads.display(th["id"])), 1)
        self.assertEqual(eng2.settings.get("theme"), "light")
        self.assertTrue((paths.skills_dir() / "my-skill.md").exists())
        self.assertTrue(list(Path(home).glob("before-restore-*.sqlite3")))          # the pre-restore data is kept
        eng2.stop()

    def test_restore_rejects_bad_files(self):
        fresh_home()
        with self.assertRaises(backup.BackupError):
            backup.stage_restore(b"not a zip at all")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("hello.txt", "x")
        with self.assertRaises(backup.BackupError):
            backup.stage_restore(buf.getvalue())                                    # no manifest
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("manifest.json", json.dumps({"app": "SomethingElse"}))
            z.writestr(backup.DB_NAME, "x")
        with self.assertRaises(backup.BackupError):
            backup.stage_restore(buf.getvalue())                                    # wrong app
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("manifest.json", json.dumps({"app": backup.APP}))
            z.writestr(backup.DB_NAME, "this is not a database")
        with self.assertRaises(Exception):
            backup.stage_restore(buf.getvalue())                                    # corrupt database
        self.assertFalse(backup.pending_dir().exists())

    def test_zip_paths_cannot_escape(self):
        home = fresh_home()
        eng = Engine()
        data = backup.create(eng)
        eng.stop()
        eng.db.close()
        src = zipfile.ZipFile(io.BytesIO(data))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n in src.namelist():
                z.writestr(n, src.read(n))
            z.writestr("skills/../../evil.txt", "boom")
            z.writestr("workspace/../../evil2.txt", "boom")
        backup.stage_restore(buf.getvalue())
        self.assertFalse((Path(home).parent / "evil.txt").exists())
        self.assertFalse(list(Path(home).rglob("evil*.txt")))
        backup.apply_pending()
        self.assertFalse(list(Path(home).rglob("evil*.txt")))

    def test_backup_restore_api(self):
        fresh_home()
        eng = Engine()
        eng.start()
        c = TestClient(create_app(eng, TOKEN))
        h = {"Authorization": f"Bearer {TOKEN}"}
        eng.bots.create("ApiKeeper", job="x")
        r = c.get("/api/backup", headers=h)
        self.assertEqual((r.status_code, r.headers["content-type"]), (200, "application/zip"))
        self.assertIn("attachment", r.headers["content-disposition"])
        bad = c.post("/api/backup/restore", headers=h, content=b"junk")
        self.assertEqual(bad.status_code, 400)
        ok = c.post("/api/backup/restore", headers=h, content=r.content)
        self.assertTrue(ok.json()["restart_required"])
        self.assertEqual(ok.json()["bots"], 1)
        self.assertEqual(c.get("/api/backup").status_code, 401)
        shutil_pending = backup.pending_dir()
        import shutil
        shutil.rmtree(shutil_pending, ignore_errors=True)
        eng.stop()


if __name__ == "__main__":
    unittest.main()
