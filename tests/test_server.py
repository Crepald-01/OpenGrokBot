"""API tests through FastAPI's TestClient.  python -m unittest tests.test_server"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-srv-")

from fastapi.testclient import TestClient  # noqa: E402

from core.engine import Engine  # noqa: E402
from service.server import create_app  # noqa: E402

TOKEN = "test-token-123"


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-")   # isolated data dir per test class
        cls.eng = Engine()
        cls.eng.settings.set("notifications.toast", False)
        cls.eng.start()
        cls.c = TestClient(create_app(cls.eng, TOKEN))
        cls.h = {"Authorization": f"Bearer {TOKEN}"}

    @classmethod
    def tearDownClass(cls):
        cls.eng.stop()

    def test_auth_required(self):
        self.assertEqual(self.c.get("/api/health").status_code, 200)
        self.assertEqual(self.c.get("/api/bots").status_code, 401)
        self.assertEqual(self.c.get("/api/bots", headers={"Authorization": "Bearer nope"}).status_code, 401)
        self.assertEqual(self.c.get("/api/bots", headers=self.h).status_code, 200)
        r = self.c.post("/api/login", json={"token": TOKEN})
        self.assertEqual(r.status_code, 200)
        self.assertIn("gb_token", r.cookies)

    def test_pwa_served_and_token_link(self):
        r = self.c.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("OpenGrokBot", r.text)
        r = self.c.get(f"/?token={TOKEN}", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("gb_token", r.headers.get("set-cookie", ""))
        self.assertEqual(self.c.get("/manifest.json").status_code, 200)
        self.assertEqual(self.c.get("/sw.js").status_code, 200)
        self.assertEqual(self.c.get("/../core/db.py").status_code, 404)

    def test_bot_crud_threads_templates_export_import(self):
        r = self.c.post("/api/bots", headers=self.h, json={"name": "API Bot", "job": "Test", "emoji": "🧪"})
        self.assertEqual(r.status_code, 200, r.text)
        bid = r.json()["id"]
        self.assertEqual(self.c.post("/api/bots", headers=self.h, json={"name": "API Bot"}).status_code, 400)
        self.assertEqual(self.c.put(f"/api/bots/{bid}", headers=self.h, json={"job": "New job", "approval_mode": "auto_review"}).json()["job"], "New job")
        ths = self.c.get(f"/api/bots/{bid}/threads", headers=self.h).json()
        self.assertTrue(ths[0]["main"])
        t = self.c.get(f"/api/threads/{ths[0]['id']}", headers=self.h).json()
        self.assertEqual(t["items"], [])
        tm = self.c.get("/api/templates", headers=self.h).json()
        self.assertGreaterEqual(len(tm), 9)
        self.assertIn("chief_of_staff", {x["id"] for x in tm})
        ex = self.c.get(f"/api/bots/{bid}/export", headers=self.h)
        self.assertEqual(ex.status_code, 200)
        imp = self.c.post("/api/bots/import", headers=self.h, content=ex.content)
        self.assertEqual(imp.status_code, 200, imp.text)
        self.assertEqual(imp.json()["bot"]["name"], "API Bot (copy)")
        self.assertEqual(self.c.post("/api/bots/import", headers=self.h, content=b"junk").status_code, 400)

    def test_team_creation_makes_group_with_lead(self):
        r = self.c.post("/api/teams", headers=self.h, json={"templates": ["chief_of_staff", "inbox"]})
        self.assertEqual(r.status_code, 200, r.text)
        g = r.json()["group"]
        self.assertEqual(len(g["members"]), 2)
        self.assertTrue(g["lead_bot"])

    def test_settings_providers_secrets(self):
        s = self.c.get("/api/settings", headers=self.h).json()
        self.assertEqual(s["theme"], "dark")
        self.assertNotIn("providers", s)
        p = self.c.get("/api/providers", headers=self.h).json()
        self.assertEqual({"anthropic", "openai", "openrouter", "groq", "ollama", "lmstudio"} - {x["id"] for x in p}, set())
        r = self.c.put("/api/providers/myendpoint", headers=self.h, json={"label": "Mine", "kind": "openai", "base_url": "http://localhost:9999/v1", "model": "x", "needs_key": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.c.put("/api/providers/bad", headers=self.h, json={"kind": "nope"}).status_code, 400)
        self.assertEqual(self.c.put("/api/secrets/not-allowed", headers=self.h, json={"value": "x"}).status_code, 400)
        self.assertEqual(self.c.put("/api/settings", headers=self.h, json={"network": {"mode": "open", "allow": [], "deny": ["x.test"], "block_private": True}}).status_code, 200)

    def test_files_terminal_and_workspace_sandbox(self):
        self.assertEqual(self.c.put("/api/computer/file", headers=self.h, json={"path": "shared/hello.txt", "content": "hi"}).status_code, 200)
        self.assertEqual(self.c.get("/api/computer/file", headers=self.h, params={"path": "shared/hello.txt"}).json()["content"], "hi")
        names = [i["name"] for i in self.c.get("/api/computer/files", headers=self.h, params={"path": "shared"}).json()["items"]]
        self.assertIn("hello.txt", names)
        self.assertEqual(self.c.get("/api/computer/file", headers=self.h, params={"path": "../../etc/passwd"}).status_code, 400)
        out = self.c.post("/api/computer/terminal", headers=self.h, json={"command": "echo hello-from-term", "cwd": "shared"}).json()
        self.assertIn("hello-from-term", out["output"])
        self.assertEqual(out["exit"], 0)

    def test_skills_routines_memory_projects_actions(self):
        bid = self.c.post("/api/bots", headers=self.h, json={"name": "Sched Bot"}).json()["id"]
        sk = self.c.get("/api/skills", headers=self.h).json()
        self.assertTrue(any(s["name"] == "inbox-triage" for s in sk), "starter skills should be seeded")
        raw = "---\nname: my-skill\ndescription: does a thing\nstatus: draft\n---\n# Steps\n1. Do it\n"
        self.assertEqual(self.c.put("/api/skills/my-skill", headers=self.h, json={"raw": raw}).json()["status"], "draft")
        self.assertEqual(self.c.post("/api/skills/my-skill/status", headers=self.h, json={"status": "active"}).json()["status"], "active")
        bad = self.c.post("/api/routines", headers=self.h, json={"bot_id": bid, "name": "x", "cron": "bogus", "prompt": "p"})
        self.assertEqual(bad.status_code, 400)
        ok = self.c.post("/api/routines", headers=self.h, json={"bot_id": bid, "name": "Overnight", "cron": "0 2 * * *", "skill": "my-skill"})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertGreater(ok.json()["next_run_at"], time.time())
        self.assertEqual(self.c.post(f"/api/bots/{bid}/memory", headers=self.h, json={"kind": "preference", "text": "Short answers"}).status_code, 200)
        self.assertEqual(len(self.c.get(f"/api/bots/{bid}/memory", headers=self.h).json()), 1)
        self.assertEqual(self.c.put("/api/projects/Q4 plan", headers=self.h, json={"content": "# Plan"}).status_code, 200)
        self.assertEqual(self.c.get("/api/projects/Q4 plan", headers=self.h).json()["content"], "# Plan")
        csv = self.c.get("/api/actions/export", headers=self.h, params={"format": "csv"})
        self.assertEqual(csv.status_code, 200)
        self.assertIn("text/csv", csv.headers["content-type"])

    def test_plugins_and_mcp_listing(self):
        d = self.c.get("/api/plugins", headers=self.h).json()
        ids = {p["id"] for p in d["plugins"]}
        self.assertTrue({"gmail", "google_calendar", "slack", "notion", "linear", "jira", "github"} <= ids)
        gh = next(p for p in d["plugins"] if p["id"] == "github")
        self.assertFalse(gh["configured"])
        self.assertTrue(any(t["name"] == "github_create_issue" for t in gh["tools"]))
        r = self.c.post("/api/plugins/rest", headers=self.h, json={"name": "Acme API", "base_url": "https://api.acme.test", "auth": "none"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["id"], "rest-acme-api")
        self.assertEqual(self.c.get("/api/mcp", headers=self.h).json(), [])
        bad = self.c.post("/api/mcp", headers=self.h, json={"name": "x", "transport": "stdio"})
        self.assertEqual(bad.status_code, 400)

    def test_message_flow_with_scripted_model(self):
        from tests.test_engine import FakeProvider
        from core.providers import LLMResult
        import core.agent as agent_mod
        fake = FakeProvider([LLMResult(text="Hello from the Bot!")])
        agent_mod.make_provider = lambda *a, **k: fake
        bid = self.c.post("/api/bots", headers=self.h, json={"name": "Chatty"}).json()["id"]
        tid = self.c.get(f"/api/bots/{bid}/threads", headers=self.h).json()[0]["id"]
        sid, q = self.eng.events.subscribe("desktop")
        r = self.c.post(f"/api/threads/{tid}/messages", headers=self.h, json={"text": "Hi!"})
        self.assertEqual(r.json()["started"], [bid])
        end = time.time() + 10
        items = []
        while time.time() < end:
            items = self.c.get(f"/api/threads/{tid}", headers=self.h).json()["items"]
            if any(i["type"] == "assistant" for i in items):
                break
            time.sleep(0.1)
        self.assertEqual([i["type"] for i in items][:2], ["user", "assistant"])
        self.assertEqual(items[1]["text"], "Hello from the Bot!")
        types = set()
        while not q.empty():
            types.add(q.get_nowait()["type"])
        self.assertTrue({"message", "turn", "delta"} <= types, types)
        self.eng.events.unsubscribe(sid)
        u = self.c.get("/api/usage", headers=self.h).json()
        self.assertGreater(u["total"], 0)
        self.assertTrue(any(b["bot_id"] == bid for b in u["per_bot"]))


if __name__ == "__main__":
    unittest.main()
