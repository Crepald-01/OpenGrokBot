"""2.0 backend: knowledge base, triggers, workflows, model fallback, file history, insights, channels, API tokens, thread branches."""
from __future__ import annotations

import base64
import io
import itertools
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from core import agent as agent_mod  # noqa: E402
from core import knowledge as kn  # noqa: E402
from core import paths  # noqa: E402
from core.engine import Engine  # noqa: E402
from core.providers import LLMResult, ProviderError, ToolCall  # noqa: E402
from service.server import create_app  # noqa: E402

TOKEN = "t20"
_N = itertools.count(100)


def fresh_home() -> str:
    d = tempfile.mkdtemp(prefix="gbtest-f20-")
    os.environ["OPENGROKBOT_HOME"] = d
    return d


def wait_for(cond, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class FakeProvider:
    vision = False
    model = "fake"
    profile = {"id": "fake", "label": "Fake"}

    def __init__(self, script, model="fake", pid="fake"):
        self.script = list(script)
        self.calls = []
        self.model = model
        self.profile = {"id": pid, "label": pid}

    def stream(self, system, messages, tools, on_text, should_stop, max_tokens=8192):
        import copy
        self.calls.append({"system": system, "messages": copy.deepcopy(messages), "tools": [t["name"] for t in tools]})
        step = self.script.pop(0) if self.script else LLMResult(text="done")
        if isinstance(step, Exception):
            raise step
        if callable(step):
            step = step(messages)
        if step.text:
            on_text(step.text)
        step.input_tokens, step.output_tokens = 100, 20
        return step


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fresh_home()
        cls.eng = Engine()
        cls.eng.settings.set("notifications.toast", False)
        cls.eng.start()
        cls.c = TestClient(create_app(cls.eng, TOKEN))
        cls.h = {"Authorization": f"Bearer {TOKEN}"}
        cls._orig_make = agent_mod.make_provider

    @classmethod
    def tearDownClass(cls):
        agent_mod.make_provider = cls._orig_make
        cls.eng.stop()

    def use(self, script, **kw):
        fake = FakeProvider(script, **kw)
        agent_mod.make_provider = lambda *a, **k: fake
        import core.approvals as ap
        ap.make_provider = lambda *a, **k: fake
        return fake

    def new_bot(self, name=None):
        bot = self.eng.bots.create(name or f"B20-{next(_N)}", job="Test job")
        th = self.eng.threads.main_thread(bot["id"])
        return bot, th

    def run_turn(self, bot, th, text="go"):
        self.eng.send_user_message(th["id"], text)
        self.assertTrue(wait_for(lambda: not self.eng.turns.is_busy(bot["id"])), "turn did not finish")
        time.sleep(0.2)


def make_docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


# ==================================================================================================== 1. knowledge base
class KnowledgeUnit(unittest.TestCase):
    def test_chunks_are_bounded_overlapping_and_complete(self):
        text = " ".join(f"Sentence number {i} says something distinct." for i in range(400))
        chunks = kn.chunk_text(text, size=500, overlap=60)
        self.assertGreater(len(chunks), 10)
        self.assertTrue(all(len(c) <= 520 for c in chunks))
        self.assertIn("Sentence number 0 ", chunks[0])
        self.assertIn("number 399", chunks[-1])
        self.assertEqual(kn.chunk_text("short"), ["short"])
        self.assertEqual(kn.chunk_text("   "), [])

    def test_reading_formats(self):
        self.assertEqual(kn.extract_text("a.md", b"# Title\r\nbody"), "# Title\nbody")
        self.assertEqual(kn.extract_text("p.html", b"<html><head><style>x{}</style></head><body><h1>Hi</h1><p>there &amp; you</p><script>bad()</script></body></html>"), "Hi\n there & you")
        self.assertIn("Second paragraph", kn.extract_text("d.docx", make_docx(["First paragraph", "Second paragraph"])))
        with self.assertRaises(kn.KnowledgeError):
            kn.extract_text("scan.pdf", b"%PDF-1.4")
        with self.assertRaises(kn.KnowledgeError):
            kn.extract_text("thing.bin", b"\x00\x01\x02\x03")
        with self.assertRaises(kn.KnowledgeError):
            kn.extract_text("broken.docx", b"not a zip")


class KnowledgeTests(Base):
    def test_search_ranks_scopes_and_forgets(self):
        a, _ = self.new_bot()
        b, _ = self.new_bot()
        k = self.eng.knowledge
        k.add_text("Travel policy", "Employees may book economy flights. Business class needs approval from a director.\n\nHotels are capped at 180 euros a night.")
        k.add_text("Lunch menu", "Pasta on Monday, soup on Tuesday, salad on Wednesday.")
        mine = k.add_text("A's secret notes", "The launch codename is Blue Heron.", bot_id=a["id"])
        hits = k.search("business class flight approval", a["id"])
        self.assertEqual(hits[0]["name"], "Travel policy")
        self.assertIn("[", hits[0]["snippet"])                                     # matches are highlighted in the snippet
        self.assertEqual([h["name"] for h in k.search("codename heron", a["id"])], ["A's secret notes"])
        self.assertEqual(k.search("codename heron", b["id"]), [])                  # another Bot cannot see a private source
        self.assertTrue(k.search("hotel 180 euros"))                               # prefix/stemming style matching works
        k.remove(mine["id"])
        self.assertEqual(k.search("codename heron", a["id"]), [])
        self.assertEqual(k.search("   "), [])

    def test_workspace_files_and_folders_stay_fresh(self):
        ws = self.eng.computer.workspace
        (ws / "docs").mkdir(exist_ok=True)
        f = ws / "docs" / "handbook.md"
        f.write_text("# Handbook\n\nVacation days: 25 per year.", encoding="utf-8")
        (ws / "docs" / "ignore.bin").write_bytes(b"\x00\x01\x02")
        added = self.eng.knowledge.add_workspace(self.eng.files, "docs")
        self.assertEqual([s["name"] for s in added], ["handbook.md"])
        self.assertEqual(self.eng.knowledge.search("vacation days")[0]["name"], "handbook.md")
        time.sleep(0.05)
        f.write_text("# Handbook\n\nVacation days: 30 per year, plus the holidays.", encoding="utf-8")
        os.utime(f, (time.time() + 5, time.time() + 5))
        self.assertEqual(self.eng.knowledge.refresh(self.eng.files), 1)
        self.assertIn("30", self.eng.knowledge.search("vacation days")[0]["text"])
        self.assertEqual(len([s for s in self.eng.knowledge.sources() if s["name"] == "handbook.md"]), 1)   # re-indexed in place, not duplicated
        f.unlink()
        self.eng.knowledge.refresh(self.eng.files)
        self.assertEqual(self.eng.knowledge.search("vacation holidays"), [])      # the deleted file's passages are gone

    def test_a_bot_uses_the_tools_and_knows_the_base_exists(self):
        bot, th = self.new_bot()
        self.eng.knowledge.add_text("Refund rules", "Refunds are possible within 30 days of purchase with a receipt.")
        fake = self.use([
            LLMResult(tool_calls=[ToolCall("k1", "knowledge_search", {"query": "refund within days"})]),
            LLMResult(tool_calls=[ToolCall("k2", "knowledge_read", {"source": "refund rules"})]),
            LLMResult(text="Refunds take 30 days (Refund rules)."),
        ])
        self.run_turn(bot, th, "What is our refund policy?")
        self.assertIn("Knowledge base", fake.calls[0]["system"])
        self.assertIn("knowledge_search", fake.calls[0]["tools"])
        first = json.dumps(fake.calls[1]["messages"][-1])
        self.assertIn("Refund rules", first)
        self.assertIn("untrusted_content", first)                                  # documents are data, never instructions
        self.assertIn("30 days", json.dumps(fake.calls[2]["messages"][-1]))

    def test_api(self):
        r = self.c.post("/api/knowledge/note", headers=self.h, json={"name": "API note", "text": "The wifi password rotates every Monday."})
        self.assertEqual(r.status_code, 200)
        sid = r.json()["id"]
        up = self.c.post("/api/knowledge/upload", headers=self.h, json={"filename": "faq.docx", "data_b64": base64.b64encode(make_docx(["Our office is in Lisbon."])).decode()})
        self.assertEqual(up.status_code, 200)
        self.assertEqual(self.c.get("/api/knowledge/search", headers=self.h, params={"q": "office lisbon"}).json()["hits"][0]["name"], "faq.docx")
        self.assertIn("wifi", self.c.get(f"/api/knowledge/{sid}", headers=self.h).json()["text"])
        listing = self.c.get("/api/knowledge", headers=self.h).json()
        self.assertGreaterEqual(listing["stats"]["sources"], 2)
        self.assertEqual(self.c.post("/api/knowledge/upload", headers=self.h, json={"filename": "x.pdf", "data_b64": "AAAA"}).status_code, 400)
        self.assertEqual(self.c.post("/api/knowledge/note", headers=self.h, json={"name": "", "text": "x"}).status_code, 400)
        self.assertEqual(self.c.post("/api/knowledge/workspace", headers=self.h, json={"path": "../outside"}).status_code, 400)
        self.assertEqual(self.c.delete(f"/api/knowledge/{sid}", headers=self.h).status_code, 200)
        self.assertEqual(self.c.get("/api/knowledge/search", headers=self.h, params={"q": "wifi password"}).json()["hits"], [])


# ==================================================================================================== 2. model fallback
class FallbackTests(Base):
    def route(self, primary, backup):
        """make_provider that hands out the primary or the backup fake depending on what the Bot asks for."""
        def make(settings, profile, model):
            if (profile or "", model or "") == ("backup-p", "m2"):
                return backup
            if primary is None:
                raise ProviderError("auth", "No API key set for the main model.")
            return primary
        agent_mod.make_provider = make

    def bot_with_backup(self, **kw):
        bot = self.eng.bots.create(f"FB-{next(_N)}", job="x", profile="primary-p", model="m1")
        self.eng.bots.update(bot["id"], fallback_profile="backup-p", fallback_model="m2", **kw)
        return bot, self.eng.threads.main_thread(bot["id"])

    def test_rate_limit_switches_to_the_backup_and_stays_there(self):
        self.eng.settings.set("memory.auto_reflect", False)   # the end-of-task memory pass would also call a model
        bot, th = self.bot_with_backup()
        primary = FakeProvider([ProviderError("rate_limit", "slow down", retry_after=0.01), ProviderError("rate_limit", "slow down", retry_after=0.01)], model="m1", pid="primary-p")
        backup = FakeProvider([LLMResult(tool_calls=[ToolCall("t1", "memory_save", {"kind": "fact", "text": "backup was here"})]), LLMResult(text="Finished on the backup.")], model="m2", pid="backup-p")
        self.route(primary, backup)
        self.run_turn(bot, th)
        self.assertEqual(len(primary.calls), 2)                                        # one try + one retry, then it gave up quickly
        self.assertEqual(len(backup.calls), 2)                                         # both steps ran on the backup
        notes = [i["text"] for i in self.eng.threads.display(th["id"]) if i["type"] == "notice"]
        self.assertTrue(any("backup model" in n for n in notes), notes)
        self.assertTrue(any("backup was here" in m["text"] for m in self.eng.memory.list(bot["id"])))
        used = {r["model"] for r in self.eng.db.query("SELECT model FROM usage WHERE bot_id=?", (bot["id"],))}
        self.assertEqual(used, {"m2"})                                                 # usage is charged to the model that did the work
        self.assertEqual(self.eng.turns.active(), [])

    def test_outages_network_and_bad_keys_fail_over_but_bad_requests_do_not(self):
        for kind, expect_backup in (("server", True), ("network", True), ("auth", True), ("bad_request", False)):
            bot, th = self.bot_with_backup()
            primary = FakeProvider([ProviderError(kind, "boom", retry_after=0.01)] * 3, model="m1", pid="primary-p")
            backup = FakeProvider([LLMResult(text="backup answer")], model="m2", pid="backup-p")
            self.route(primary, backup)
            self.run_turn(bot, th)
            self.assertEqual(bool(backup.calls), expect_backup, kind)
            if not expect_backup:
                self.assertTrue(any(i["type"] == "notice" and "Request rejected" in i["text"] for i in self.eng.threads.display(th["id"])))

    def test_no_backup_means_the_error_is_shown_as_before(self):
        bot = self.eng.bots.create(f"NB-{next(_N)}", job="x", profile="primary-p", model="m1")
        th = self.eng.threads.main_thread(bot["id"])
        primary = FakeProvider([ProviderError("server", "down", retry_after=0.01)] * 4, model="m1", pid="primary-p")
        self.route(primary, FakeProvider([]))
        self.run_turn(bot, th)
        self.assertTrue(any(i["type"] == "notice" and "Provider outage" in i["text"] for i in self.eng.threads.display(th["id"])))

    def test_the_same_model_is_not_its_own_backup_and_the_backup_is_tried_once(self):
        bot = self.eng.bots.create(f"SM-{next(_N)}", job="x", profile="primary-p", model="m1")
        self.eng.bots.update(bot["id"], fallback_profile="primary-p", fallback_model="m1")
        th = self.eng.threads.main_thread(bot["id"])
        primary = FakeProvider([ProviderError("server", "down", retry_after=0.01)] * 6, model="m1", pid="primary-p")
        agent_mod.make_provider = lambda *a, **k: primary
        self.run_turn(bot, th)
        self.assertLessEqual(len(primary.calls), 4)
        bot2, th2 = self.bot_with_backup()
        primary2 = FakeProvider([ProviderError("server", "down", retry_after=0.01)] * 3, model="m1", pid="primary-p")
        backup2 = FakeProvider([ProviderError("server", "also down", retry_after=0.01)] * 6, model="m2", pid="backup-p")
        self.route(primary2, backup2)
        self.run_turn(bot2, th2)
        self.assertLessEqual(len(backup2.calls), 4)                                    # it does not bounce between models forever

    def test_app_wide_backup_and_a_missing_primary_key(self):
        bot = self.eng.bots.create(f"GL-{next(_N)}", job="x", profile="primary-p", model="m1")
        th = self.eng.threads.main_thread(bot["id"])
        self.eng.settings.set("fallback", {"profile": "backup-p", "model": "m2"})
        try:
            backup = FakeProvider([LLMResult(text="answered by the app-wide backup")], model="m2", pid="backup-p")
            self.route(None, backup)                                                   # the main model cannot even be created (no key)
            self.run_turn(bot, th)
            self.assertEqual(len(backup.calls), 1)
            self.assertTrue(any("answered by the app-wide backup" in json.dumps(i) for i in self.eng.threads.display(th["id"])))
        finally:
            self.eng.settings.set("fallback", {"profile": "", "model": ""})

    def test_duplicate_keeps_the_backup(self):
        bot, _ = self.bot_with_backup()
        copy = self.eng.bots.duplicate(bot["id"])
        self.assertEqual((copy["fallback_profile"], copy["fallback_model"]), ("backup-p", "m2"))


if __name__ == "__main__":
    unittest.main()
