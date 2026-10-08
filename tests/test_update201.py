"""In-app updates: what is offered, what is downloaded, and what is allowed to run."""
import hashlib
import sys
import tempfile
import time
import unittest
from pathlib import Path

from core import updates as U
from core.apitokens import allowed


class FakeSettings:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


PAYLOAD = b"MZ" + b"installer bytes" * 1000
SHA = hashlib.sha256(PAYLOAD).hexdigest()
URL = U.DOWNLOAD_PREFIX + "v2.0.2/OpenGrokBot-Setup-2.0.2.exe"


def release(tag="v2.0.2", sha=SHA, name="OpenGrokBot-Setup-2.0.2.exe", url=URL):
    asset = {"name": name, "browser_download_url": url, "size": len(PAYLOAD)}
    if sha:
        asset["digest"] = "sha256:" + sha
    return {"tag_name": tag, "html_url": "https://github.com/x/y/releases/tag/" + tag, "name": tag, "body": "Fixes things.\nMore.", "assets": [asset]}


def make(rel=None, payload=PAYLOAD, current="2.0.1"):
    tmp = Path(tempfile.mkdtemp(prefix="upd"))
    launched = []

    def fake_download(url, dest, progress):
        dest.write_bytes(payload)
        progress(len(payload), len(payload))
        return hashlib.sha256(payload).hexdigest()

    u = U.Updates(FakeSettings(), current=current, fetch=lambda: rel or release(), folder=tmp, download=fake_download, launch=launched.append)
    return u, tmp, launched


def wait(u, status, secs=5):
    end = time.time() + secs
    while time.time() < end and u.state()["download"]["status"] not in (status, "error"):
        time.sleep(0.02)
    return u.state()["download"]


class UpdateTests(unittest.TestCase):
    def test_state_offers_the_installer_when_newer(self):
        u, _, _ = make()
        st = u.check(force=True)
        self.assertTrue(st["newer"])
        self.assertEqual(st["latest"], "2.0.2")
        self.assertEqual(st["size"], len(PAYLOAD))
        self.assertEqual(st["can_install"], sys.platform == "win32")
        self.assertIn("Fixes things", st["notes"])

    def test_up_to_date_offers_nothing(self):
        u, _, _ = make(current="2.0.2")
        st = u.check(force=True)
        self.assertFalse(st["newer"])
        self.assertFalse(st["can_install"])
        with self.assertRaises(U.UpdateError):
            u.start_download()

    def test_no_checksum_means_no_automatic_install(self):
        u, _, _ = make(rel=release(sha=""))
        st = u.check(force=True)
        self.assertTrue(st["newer"])
        self.assertFalse(st["can_install"])
        with self.assertRaises(U.UpdateError):
            u.start_download()

    def test_odd_asset_names_are_ignored(self):
        u, _, _ = make(rel=release(name="evil.exe"))
        self.assertFalse(u.check(force=True)["can_install"])

    @unittest.skipUnless(sys.platform == "win32", "installer is Windows only")
    def test_download_verify_then_install(self):
        u, tmp, launched = make()
        u.check(force=True)
        u.start_download()
        d = wait(u, "ready")
        self.assertEqual(d["status"], "ready", d)
        self.assertTrue(Path(d["path"]).exists())
        self.assertEqual(Path(d["path"]).read_bytes(), PAYLOAD)
        self.assertFalse(list(tmp.glob("*.part")))
        self.assertEqual(u.install()["version"], "2.0.2")
        self.assertEqual(launched, [Path(d["path"])])

    @unittest.skipUnless(sys.platform == "win32", "installer is Windows only")
    def test_a_tampered_download_is_thrown_away_and_never_run(self):
        u, tmp, launched = make(payload=PAYLOAD + b"tampered")
        # GitHub lists the hash of PAYLOAD, but the bytes that arrive are different
        u.check(force=True)
        u.start_download()
        d = wait(u, "error")
        self.assertEqual(d["status"], "error")
        self.assertIn("checksum", d["error"])
        self.assertFalse(list(tmp.glob("OpenGrokBot-Setup-*")))
        with self.assertRaises(U.UpdateError):
            u.install()
        self.assertEqual(launched, [])

    def test_install_needs_a_finished_download(self):
        u, _, launched = make()
        u.check(force=True)
        with self.assertRaises(U.UpdateError):
            u.install()
        self.assertEqual(launched, [])

    @unittest.skipUnless(sys.platform == "win32", "installer is Windows only")
    def test_a_foreign_download_host_is_refused(self):
        u, _, _ = make(rel=release(url="https://example.com/OpenGrokBot-Setup-2.0.2.exe"))
        u.check(force=True)
        with self.assertRaises(U.UpdateError):
            u.start_download()

    @unittest.skipUnless(sys.platform == "win32", "installer is Windows only")
    def test_second_start_while_downloading_does_not_double_up(self):
        u, _, _ = make()
        u.check(force=True)
        u.start_download()
        u.start_download()
        self.assertEqual(wait(u, "ready")["status"], "ready")

    def test_api_tokens_cannot_start_an_update(self):
        for scope in ("read", "chat", "full"):
            self.assertFalse(allowed(scope, "POST", "/api/updates/install"))
            self.assertFalse(allowed(scope, "POST", "/api/updates/download"))
        self.assertTrue(allowed("read", "GET", "/api/updates"))

    def test_check_every_six_hours(self):
        self.assertEqual(U.INTERVAL, 6 * 3600)


if __name__ == "__main__":
    unittest.main()
