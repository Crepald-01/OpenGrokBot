"""1.6: regression tests for the bug fixes (workspace links, crafted backups, non-Latin file names, digest and update edge cases)."""
from __future__ import annotations

import io
import itertools
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from core import backup, paths  # noqa: E402
from core.digest import tokens  # noqa: E402
from core.engine import Engine  # noqa: E402
from core.files import Files  # noqa: E402
from core.updates import is_newer, parse  # noqa: E402
from service.server import create_app  # noqa: E402

TOKEN = "t16"
_N = itertools.count(5000)


def fresh_home() -> str:
    d = tempfile.mkdtemp(prefix="gbtest-f16-")
    os.environ["OPENGROKBOT_HOME"] = d
    return d


def make_link(link: Path, target: Path) -> bool:
    """A directory link that needs no admin rights on Windows (a junction), or a symlink elsewhere."""
    try:
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
        else:
            os.symlink(target, link, target_is_directory=True)
        return link.exists()
    except (OSError, subprocess.CalledProcessError):
        return False


class WorkspaceLinks(unittest.TestCase):
    """A link inside the workspace that points outside it used to crash listings with a ValueError (HTTP 500)."""

    def setUp(self):
        base = Path(tempfile.mkdtemp(prefix="gbtest-links-"))
        self.root = base / "ws"
        self.outside = base / "outside"
        self.root.mkdir()
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("not for Bots", encoding="utf-8")
        (self.root / "ok.txt").write_text("fine", encoding="utf-8")
        self.linked = make_link(self.root / "escape", self.outside)
        self.f = Files(self.root)

    def test_listing_survives_and_hides_links_out_of_the_workspace(self):
        if not self.linked:
            self.skipTest("cannot create a directory link here")
        names = [e["name"] for e in self.f.list("")["entries"]]
        self.assertIn("ok.txt", names)
        self.assertNotIn("escape", names)
        self.assertEqual([e["name"] for e in self.f.recent()], ["ok.txt"])
        self.assertEqual(self.f.search("secret"), [])
        self.assertEqual(self.f.stats()["files"], 1)

    def test_the_linked_file_still_cannot_be_opened_or_deleted(self):
        if not self.linked:
            self.skipTest("cannot create a directory link here")
        from core.files import FileError
        for op in (self.f.preview, self.f.delete, self.f.raw):
            with self.assertRaises(FileError):
                op("escape/secret.txt")
        self.assertTrue((self.outside / "secret.txt").exists())

    def test_backup_skips_linked_files(self):
        if not self.linked:
            self.skipTest("cannot create a directory link here")
        names = [p.name for p in backup._files_under(self.root)]
        self.assertIn("ok.txt", names)


class CraftedBackups(unittest.TestCase):
    def test_backslash_and_drive_names_are_not_written_anywhere(self):
        home = fresh_home()
        mark = "escaped-" + os.urandom(4).hex()          # unique, so a leftover from an earlier run cannot confuse the search
        eng = Engine()
        eng.start()
        eng.bots.create("Keeper", job="x")
        good = backup.create(eng)
        eng.stop()
        eng.db.close()
        src = zipfile.ZipFile(io.BytesIO(good))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n in src.namelist():
                z.writestr(n, src.read(n))
            z.writestr(f"skills\\..\\..\\{mark}-a.txt", "bad")
            z.writestr(f"workspace\\..\\..\\..\\{mark}-b.txt", "bad")
            z.writestr(f"workspace/C:/{mark}-c.txt", "bad")
            z.writestr("workspace/ok/fine.txt", "fine")
        info = backup.stage_restore(buf.getvalue())
        self.assertEqual(info["workspace_files"], 1)       # only the clean entry was staged
        found = [p.name for p in Path(home).parent.rglob(mark + "*")]
        self.assertEqual(found, [])
        pending = backup.pending_dir()
        self.assertTrue((pending / "workspace" / "ok" / "fine.txt").exists())
        self.assertEqual([p.name for p in pending.rglob(mark + "*")], [])


class SmallEdges(unittest.TestCase):
    def test_token_counts_never_read_1000k(self):
        self.assertEqual(tokens(999_949), "999.9k")
        self.assertEqual(tokens(999_950), "1.0M")
        self.assertEqual(tokens(1_500_000), "1.5M")
        self.assertEqual(tokens(950), "950")

    def test_versions_with_different_lengths_compare_equal(self):
        self.assertFalse(is_newer("1.6", "1.6.0"))
        self.assertFalse(is_newer("v1.6.0", "1.6"))
        self.assertTrue(is_newer("1.10", "1.9.9"))
        self.assertEqual(parse("1.6.0-beta"), (1, 6, 0))


class ServerFixes(unittest.TestCase):
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

    def test_long_bot_names_that_only_differ_after_40_characters_collide(self):
        a = "x" * 40 + "-first"
        b = "x" * 40 + "-second"
        self.eng.bots.create(a)
        from core.bots import BotError
        with self.assertRaises(BotError):
            self.eng.bots.create(b)

    def test_digest_says_so_when_nothing_happened(self):
        d = self.eng.digest.build("week")
        self.assertIn("No Bot did anything", self.eng.digest.markdown(d))
        self.assertEqual(self.eng.digest.build("nonsense")["spec"], "today")

    def test_downloads_with_non_latin_names_work(self):
        (paths.workspace_dir() / "shared").mkdir(parents=True, exist_ok=True)
        (paths.workspace_dir() / "shared" / "отчёт 文档.txt").write_text("hello", encoding="utf-8")
        r = self.c.get("/api/ws/raw", headers=self.h, params={"path": "shared/отчёт 文档.txt"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"hello")
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertIn("filename*=", r.headers["content-disposition"])

    def test_exports_with_non_latin_bot_and_chat_names_work(self):
        bot = self.eng.bots.create(f"Рассылка {next(_N)}", job="x")
        r = self.c.get(f"/api/bots/{bot['id']}/export", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertIn(".gbbot", r.headers["content-disposition"])
        th = self.eng.threads.main_thread(bot["id"])
        self.eng.threads.rename(th["id"], "План поездки") if hasattr(self.eng.threads, "rename") else None
        r = self.c.get(f"/api/threads/{th['id']}/export", headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertIn(".md", r.headers["content-disposition"])

    def test_digest_time_without_a_leading_zero_still_fires_once(self):
        import time
        self.eng.settings.set("digest", {"enabled": True, "time": "0:00", "last_sent": ""})
        self.eng._digest_tick()
        self.assertEqual(self.eng.settings.get("digest.last_sent"), time.strftime("%Y-%m-%d"))
        self.eng.settings.set("digest", {"enabled": False})


if __name__ == "__main__":
    unittest.main()
