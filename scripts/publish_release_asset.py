"""Uploads (or replaces) the installer on a GitHub release using the credential Git already has.

    python scripts/publish_release_asset.py dist/OpenGrokBot-Setup-2.1.0.exe v2.1.0

The token is read from `git credential fill` and never printed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "Crepald-01/OpenGrokBot"
exe = Path(sys.argv[1])
tag = sys.argv[2]

out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n", capture_output=True, text=True,
                     env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}).stdout
tok = dict(l.split("=", 1) for l in out.splitlines() if "=" in l).get("password")
if not tok:
    sys.exit("no stored GitHub credential available")
H = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "opengrokbot-release"}


def call(method: str, url: str, data=None, headers=None, raw=False):
    body = data if raw else (json.dumps(data).encode() if data is not None else None)
    req = urllib.request.Request(url, data=body, method=method, headers={**H, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {url} -> HTTP {e.code}: {e.read().decode()[:300]}")


def notes() -> str:
    """The CHANGELOG.md section for this version, plus the install hint."""
    ver = tag.lstrip("v")
    text = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    sect = text.split(f"## {ver}", 1)[1].split("\n## ", 1)[0].strip() if f"## {ver}" in text else ""
    return (sect + "\n\n" if sect else "") + (
        f"**Install:** download `{exe.name}` below and run it (per user, no admin needed). Upgrading keeps your Bots and settings.\n\n"
        "The installer is not code-signed, so Windows SmartScreen may warn: choose *More info*, then *Run anyway*, or check the SHA-256 shown next to the file.")


try:
    rel = call("GET", f"https://api.github.com/repos/{REPO}/releases/tags/{tag}")
except SystemExit:
    rel = call("POST", f"https://api.github.com/repos/{REPO}/releases",
               {"tag_name": tag, "target_commitish": "main", "name": f"OpenGrokBot {tag.lstrip('v')}", "body": notes(), "draft": False, "prerelease": False})
    print("created release", rel["html_url"])
for a in rel.get("assets", []):
    if a["name"] == exe.name:
        call("DELETE", a["url"])
        print("removed old asset", a["name"])
up = rel["upload_url"].split("{")[0] + "?name=" + exe.name
asset = call("POST", up, exe.read_bytes(), {"Content-Type": "application/octet-stream"}, raw=True)
print("uploaded", asset["name"], round(asset["size"] / 1e6, 1), "MB | digest:", asset.get("digest"))
print("url:", asset["browser_download_url"])
