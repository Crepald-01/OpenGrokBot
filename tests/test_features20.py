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


# ==================================================================================================== 3. file history
class FileHistoryTests(Base):
    def test_versions_are_kept_on_overwrite_append_delete_and_move(self):
        comp, fh = self.eng.computer, self.eng.filehistory
        comp.write_file("hist/a.txt", "version one")
        self.assertEqual(fh.versions("hist/a.txt"), [])                            # a brand-new file has nothing to keep yet
        comp.write_file("hist/a.txt", "version two")
        comp.write_file("hist/a.txt", " + tail", append=True)
        vs = fh.versions("hist/a.txt")
        self.assertEqual([fh.read(v["id"]).decode() for v in vs], ["version two", "version one"])
        self.assertEqual([v["reason"] for v in vs], ["appended to", "overwritten"])
        comp.write_file("hist/b.txt", "to be replaced by a move")
        comp.write_file("hist/c.txt", "mover")
        comp.move("hist/c.txt", "hist/b.txt")
        self.assertEqual(fh.read(fh.versions("hist/b.txt")[0]["id"]).decode(), "to be replaced by a move")
        comp.delete("hist/a.txt")
        self.assertFalse((comp.workspace / "hist" / "a.txt").exists())
        gone = [d for d in fh.deleted() if d["path"] == "hist/a.txt"]
        self.assertEqual(len(gone), 1)
        self.assertEqual(fh.read(gone[0]["version_id"]).decode(), "version two + tail")

    def test_restore_brings_back_deleted_files_and_can_itself_be_undone(self):
        comp, fh = self.eng.computer, self.eng.filehistory
        comp.write_file("hist/r.txt", "original")
        comp.write_file("hist/r.txt", "edited")
        first = fh.versions("hist/r.txt")[0]["id"]                                 # "original"
        out = fh.restore("hist/r.txt", first)
        self.assertEqual((comp.workspace / "hist" / "r.txt").read_text(), "original")
        self.assertEqual(out["path"], "hist/r.txt")
        self.assertEqual(fh.read(fh.versions("hist/r.txt")[0]["id"]).decode(), "edited")   # the edit it replaced is kept: restore is undoable
        comp.delete("hist/r.txt")
        d = next(x for x in fh.deleted() if x["path"] == "hist/r.txt")
        fh.restore("hist/r.txt", d["version_id"])
        self.assertEqual((comp.workspace / "hist" / "r.txt").read_text(), "original")
        self.assertFalse(any(x["path"] == "hist/r.txt" for x in fh.deleted()))   # no longer listed as deleted
        from core.filehistory import HistoryError
        with self.assertRaises(HistoryError):
            fh.restore("hist/other.txt", first)                                    # a version cannot be restored onto a different file
        with self.assertRaises(HistoryError):
            fh.restore("../escape.txt", first)

    def test_identical_content_is_stored_once_and_old_versions_are_pruned(self):
        comp, fh = self.eng.computer, self.eng.filehistory
        comp.write_file("hist/same.txt", "x")
        for _ in range(3):
            comp.write_file("hist/same.txt", "x")                                  # rewriting the same content adds nothing
        self.assertEqual(len(fh.versions("hist/same.txt")), 1)
        for i in range(30):
            comp.write_file("hist/many.txt", f"revision {i}")
        vs = fh.versions("hist/many.txt")
        self.assertEqual(len(vs), 20)
        self.assertEqual(fh.read(vs[0]["id"]).decode(), "revision 28")
        shas = {v["sha"] for v in vs}
        blobs = {p.name for p in (paths.data_dir() / "history").rglob("*") if p.is_file()}
        self.assertTrue(shas <= blobs)
        self.assertFalse("revision 0" in "".join(fh.read(v["id"]).decode() for v in vs))     # the oldest were dropped with their data

    def test_files_outside_the_workspace_and_oversized_files_are_ignored(self):
        fh = self.eng.filehistory
        outside = Path(tempfile.mkdtemp(prefix="gbtest-out-")) / "o.txt"
        outside.write_text("not in the workspace")
        self.assertIsNone(fh.snapshot(outside, "x"))
        big = self.eng.computer.workspace / "hist" / "big.bin"
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_bytes(b"a" * 5_100_000)
        self.assertIsNone(fh.snapshot(big, "x"))
        empty = self.eng.computer.workspace / "hist" / "empty.txt"
        empty.write_text("")
        self.assertIsNone(fh.snapshot(empty, "x"))

    def test_deleting_a_folder_keeps_every_file_in_it(self):
        comp, fh = self.eng.computer, self.eng.filehistory
        for n in ("one", "two", "three"):
            comp.write_file(f"hist/folder/{n}.txt", f"content of {n}")
        comp.delete("hist/folder")
        gone = {d["path"] for d in fh.deleted()}
        self.assertTrue({"hist/folder/one.txt", "hist/folder/two.txt", "hist/folder/three.txt"} <= gone)

    def test_a_bot_overwriting_a_file_is_recorded_with_its_name(self):
        bot, th = self.new_bot()
        (self.eng.computer.workspace / "hist").mkdir(exist_ok=True)
        (self.eng.computer.workspace / "hist" / "bot.txt").write_text("before the bot")
        self.use([LLMResult(tool_calls=[ToolCall("t1", "fs_write", {"path": "hist/bot.txt", "content": "after the bot"})]), LLMResult(text="ok")])
        self.eng.send_user_message(th["id"], "go")
        self.assertTrue(wait_for(lambda: self.eng.approvals.pending_count() > 0))
        a = self.eng.approvals.list("pending", bot["id"])[0]
        self.eng.approvals.decide(a["id"], True)
        self.assertTrue(wait_for(lambda: not self.eng.turns.is_busy(bot["id"])))
        v = self.eng.filehistory.versions("hist/bot.txt")[0]
        self.assertEqual((v["bot_id"], v["bot_name"], v["reason"]), (bot["id"], bot["name"], "overwritten"))

    def test_api_and_the_files_page_delete(self):
        ws = self.eng.computer.workspace
        (ws / "hist").mkdir(exist_ok=True)
        (ws / "hist" / "api.txt").write_text("api original")
        self.assertEqual(self.c.delete("/api/ws/file", headers=self.h, params={"path": "hist/api.txt"}).status_code, 200)      # deleted from the Files page
        d = self.c.get("/api/ws/deleted", headers=self.h).json()["entries"]
        entry = next(e for e in d if e["path"] == "hist/api.txt")
        self.assertEqual(entry["reason"], "deleted in Files")
        pv = self.c.get("/api/ws/version", headers=self.h, params={"id": entry["version_id"]}).json()
        self.assertEqual((pv["text"], pv["binary"]), ("api original", False))
        r = self.c.post("/api/ws/restore", headers=self.h, json={"path": "hist/api.txt", "version_id": entry["version_id"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((ws / "hist" / "api.txt").read_text(), "api original")
        hist = self.c.get("/api/ws/history", headers=self.h, params={"path": "hist/api.txt"}).json()
        self.assertEqual(len(hist["versions"]), 1)
        self.assertEqual(self.c.post("/api/ws/restore", headers=self.h, json={"path": "hist/api.txt", "version_id": 99999}).status_code, 400)
        self.assertEqual(self.c.get("/api/ws/history", headers=self.h, params={"path": "../x"}).status_code, 400)


# ==================================================================================================== 4. API tokens
class ApiTokenTests(Base):
    def make(self, scope, name=None, days=0):
        r = self.c.post("/api/tokens", headers=self.h, json={"name": name or f"t-{next(_N)}", "scope": scope, "days": days})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def hdr(self, tok):
        return {"Authorization": f"Bearer {tok['token']}"}

    def test_a_token_is_shown_once_and_only_its_hash_is_kept(self):
        t = self.make("read", "dashboard")
        self.assertTrue(t["token"].startswith("gbt_") and len(t["token"]) > 30)
        listing = self.c.get("/api/tokens", headers=self.h).json()["tokens"]
        row = next(x for x in listing if x["id"] == t["id"])
        self.assertNotIn("token", row)
        self.assertNotIn("hash", row)
        self.assertEqual(row["prefix"], t["token"][:8])
        stored = self.eng.db.one("SELECT hash FROM api_tokens WHERE id=?", (t["id"],))["hash"]
        self.assertNotEqual(stored, t["token"])
        self.assertNotIn(t["token"], json.dumps(self.eng.db.query("SELECT * FROM api_tokens")))

    def test_read_chat_and_full_scopes(self):
        read, chat, full = self.make("read"), self.make("chat"), self.make("full")
        bot, th = self.new_bot()
        self.use([LLMResult(text="hi")] * 4)
        # read: look but do not touch
        self.assertEqual(self.c.get("/api/bots", headers=self.hdr(read)).status_code, 200)
        self.assertEqual(self.c.post(f"/api/threads/{th['id']}/messages", headers=self.hdr(read), json={"text": "hello"}).status_code, 403)
        # chat: may also send messages, nothing else
        self.assertEqual(self.c.post(f"/api/threads/{th['id']}/messages", headers=self.hdr(chat), json={"text": "hello"}).status_code, 200)
        wait_for(lambda: not self.eng.turns.is_busy(bot["id"]))
        self.assertEqual(self.c.post("/api/bots", headers=self.hdr(chat), json={"name": "Nope"}).status_code, 403)
        # full: may change things...
        made = self.c.post("/api/bots", headers=self.hdr(full), json={"name": f"ByApi{next(_N)}", "job": "x"})
        self.assertEqual(made.status_code, 200, made.text)
        # ...but not the sensitive areas, which need the main token
        for method, path in (("get", "/api/tokens"), ("post", "/api/tokens"), ("get", "/api/backup"), ("put", "/api/settings"), ("post", "/api/computer/terminal"),
                             ("post", "/api/approvals/x/decide"), ("put", "/api/providers/anthropic"), ("delete", "/api/rules/1"), ("post", "/api/triggers")):
            r = getattr(self.c, method)(path, headers=self.hdr(full), **({"json": {}} if method in ("post", "put") else {}))
            self.assertEqual(r.status_code, 403, f"{method} {path}")
        self.assertEqual(self.c.get("/api/tokens", headers=self.h).status_code, 200)         # the main token still can

    def test_revoked_expired_and_wrong_tokens_are_refused(self):
        t = self.make("full")
        self.assertEqual(self.c.get("/api/bots", headers=self.hdr(t)).status_code, 200)
        self.assertEqual(self.c.delete(f"/api/tokens/{t['id']}", headers=self.h).status_code, 200)
        self.assertEqual(self.c.get("/api/bots", headers=self.hdr(t)).status_code, 401)
        short = self.make("read", days=1)
        self.eng.db.update("api_tokens", short["id"], {"expires_at": time.time() - 5})
        self.assertEqual(self.c.get("/api/bots", headers=self.hdr(short)).status_code, 401)
        self.assertEqual(self.c.get("/api/bots", headers={"Authorization": "Bearer gbt_not-a-real-token"}).status_code, 401)
        flags = {x["id"]: x for x in self.c.get("/api/tokens", headers=self.h).json()["tokens"]}
        self.assertTrue(flags[t["id"]]["revoked"])
        self.assertTrue(flags[short["id"]]["expired"])

    def test_last_used_is_recorded_and_a_token_cannot_log_in_for_a_cookie(self):
        t = self.make("read")
        self.c.get("/api/bots", headers=self.hdr(t))
        row = next(x for x in self.c.get("/api/tokens", headers=self.h).json()["tokens"] if x["id"] == t["id"])
        self.assertGreater(row["last_used_at"], 0)
        self.assertEqual(self.c.post("/api/login", json={"token": t["token"]}).status_code, 401)   # the web login only takes the main access token

    def test_bad_input_and_the_limit(self):
        self.assertEqual(self.c.post("/api/tokens", headers=self.h, json={"name": "", "scope": "read"}).status_code, 400)
        self.assertEqual(self.c.post("/api/tokens", headers=self.h, json={"name": "x", "scope": "root"}).status_code, 400)
        from core import apitokens as at
        self.assertFalse(at.allowed("full", "GET", "/api/tokens"))
        self.assertTrue(at.allowed("read", "GET", "/api/bots"))
        self.assertFalse(at.allowed("read", "POST", "/api/bots"))
        self.assertFalse(at.allowed("chat", "POST", "/api/threads/abc/stop"))


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
