"""The phone web app in a real (mobile-sized) Chromium: sign in with the link, open a Bot, use slash commands."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

from core.engine import Engine  # noqa: E402
from service.server import create_app  # noqa: E402

SHOTS = os.environ.get("PWA_SHOTS")


class PwaCommands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-pwa-")
        cls.eng = Engine()
        cls.eng.settings.set("notifications.toast", False)
        cls.eng.start()
        cls.port, cls.token = 18769, "pwa-token"
        cls.server = uvicorn.Server(uvicorn.Config(create_app(cls.eng, cls.token), host="127.0.0.1", port=cls.port, log_level="warning"))
        threading.Thread(target=cls.server.run, daemon=True).start()
        time.sleep(1.5)
        cls.bot = cls.eng.bots.create("Inbox", job="Triage email", emoji="📥")
        cls.eng.threads.main_thread(cls.bot["id"])
        cls.eng.memory.add(cls.bot["id"], "preference", "Keep summaries to one screen", pinned=True)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.eng.stop()

    def test_commands_on_the_phone(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            ctx = b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
            pg = ctx.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(f"http://127.0.0.1:{self.port}/?token={self.token}")
            pg.wait_for_selector("[data-bot]")
            pg.click("[data-bot]")
            pg.wait_for_selector("#input")

            pg.fill("#input", "/")
            pg.wait_for_selector("#cmdlist button")
            self.assertGreaterEqual(pg.locator("#cmdlist button").count(), 5)
            pg.fill("#input", "/mem")
            self.assertEqual(pg.locator("#cmdlist button").first.get_attribute("data-cmd"), "memory")
            if SHOTS:
                pg.screenshot(path=f"{SHOTS}/phone-popup.png")
            pg.locator("#cmdlist button").first.click()                 # tap to complete
            self.assertEqual(pg.input_value("#input"), "/memory ")
            self.assertTrue(pg.locator("#cmdlist").is_hidden())

            pg.click("#send")                                            # tap Send
            pg.wait_for_selector(".notice.cmd")
            self.assertIn("Keep summaries to one screen", pg.inner_text(".notice.cmd"))

            pg.fill("#input", "/status")
            pg.click("#send")
            pg.wait_for_function("document.querySelectorAll('.notice.cmd').length >= 2")
            self.assertIn("Inbox", pg.inner_text("#messages"))
            if SHOTS:
                pg.screenshot(path=f"{SHOTS}/phone-output.png")

            pg.fill("#input", "/new Phone plan")
            pg.click("#send")
            pg.wait_for_function("!document.querySelector('.notice.cmd')")   # switched to the fresh thread
            self.assertEqual(len([t for t in self.eng.threads.list_for_bot(self.bot["id"]) if t["title"] == "Phone plan"]), 1)
            self.assertEqual(errs, [])
            b.close()


if __name__ == "__main__":
    unittest.main()
