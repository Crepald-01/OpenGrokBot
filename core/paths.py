"""Filesystem locations used by OpenGrokBot.

Everything the account owns lives under one data directory. The shared computer
(workspace, browser profile, downloads) belongs to the account, not to any Bot.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def resource_root() -> Path:
    """Directory holding bundled resources (web/, skills/, plugins/, assets/)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    override = os.environ.get("OPENGROKBOT_HOME")
    if override:
        base = Path(override)
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home()))) / "OpenGrokBot"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "OpenGrokBot"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _sub(*parts: str) -> Path:
    p = data_dir().joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return data_dir() / "opengrokbot.sqlite3"


def workspace_dir() -> Path:
    """The shared filesystem workspace every Bot can see."""
    p = _sub("workspace")
    for name in ("shared", "downloads", "bots"):
        (p / name).mkdir(exist_ok=True)
    return p


def browser_profile_dir() -> Path:
    return _sub("computer", "browser-profile")


def screenshots_dir() -> Path:
    return _sub("computer", "screenshots")


def skills_dir() -> Path:
    return _sub("skills")


def plugins_dir() -> Path:
    return _sub("plugins")


def exports_dir() -> Path:
    return _sub("exports")


def logs_dir() -> Path:
    return _sub("logs")


def service_info_path() -> Path:
    return data_dir() / "service.json"


def ui_config_path() -> Path:
    return data_dir() / "ui_config.json"


def admin_policy_paths() -> list[Path]:
    """Places an administrator may drop an admin.json (first existing wins)."""
    paths: list[Path] = []
    env = os.environ.get("OPENGROKBOT_ADMIN")
    if env:
        paths.append(Path(env))
    pd = os.environ.get("PROGRAMDATA")
    if pd:
        paths.append(Path(pd) / "OpenGrokBot" / "admin.json")
    paths.append(data_dir() / "admin.json")
    return paths
