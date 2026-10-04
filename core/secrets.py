"""Secret storage in the OS credential store (Windows Credential Manager via keyring).

Secrets are never written to the database, settings or any plain-text file. On
hosts with no credential backend (e.g. a headless Docker host) secrets can only
be supplied through environment variables, which are read-only to the app.
"""
from __future__ import annotations

import os
import re

SERVICE = "OpenGrokBot"

try:  # keyring is a hard dependency, but fail soft so the app can still explain itself
    import keyring
    from keyring.errors import KeyringError, PasswordDeleteError
except Exception:  # pragma: no cover
    keyring = None  # type: ignore

    class KeyringError(Exception):  # type: ignore
        pass

    class PasswordDeleteError(Exception):  # type: ignore
        pass


class SecretStoreError(RuntimeError):
    pass


def _env_name(name: str) -> str:
    return "OPENGROKBOT_SECRET_" + re.sub(r"[^A-Za-z0-9]+", "_", name).upper().strip("_")


_ENV_ALIASES = {
    "provider:anthropic": "ANTHROPIC_API_KEY",
    "provider:openai": "OPENAI_API_KEY",
    "provider:openrouter": "OPENROUTER_API_KEY",
    "provider:groq": "GROQ_API_KEY",
}


def backend_available() -> bool:
    if keyring is None:
        return False
    try:
        kr = keyring.get_keyring()
        return "fail" not in type(kr).__module__.lower() and "null" not in type(kr).__module__.lower()
    except Exception:
        return False


def get_secret(name: str) -> str | None:
    if keyring is not None:
        try:
            v = keyring.get_password(SERVICE, name)
            if v:
                return v
        except Exception:
            pass
    for env in (_env_name(name), _ENV_ALIASES.get(name, "")):
        if env and os.environ.get(env):
            return os.environ[env]
    return None


def has_secret(name: str) -> bool:
    return bool(get_secret(name))


def set_secret(name: str, value: str) -> None:
    if keyring is None or not backend_available():
        raise SecretStoreError(
            "No credential store is available on this machine. Set the environment variable "
            f"{_env_name(name)} instead (secrets are never stored in plain text)."
        )
    try:
        keyring.set_password(SERVICE, name, value)
    except Exception as e:
        raise SecretStoreError(f"Could not save secret to the credential store: {e}") from e


def delete_secret(name: str) -> None:
    if keyring is None:
        return
    try:
        keyring.delete_password(SERVICE, name)
    except (PasswordDeleteError, KeyringError):
        pass
    except Exception:
        pass


def redact(text: str, extra: list[str] | None = None) -> str:
    """Best-effort scrub of obvious secret shapes from text headed to logs/exports."""
    if not text:
        return text
    patterns = [
        r"sk-ant-[A-Za-z0-9_\-]{20,}",
        r"sk-[A-Za-z0-9_\-]{20,}",
        r"xox[abprs]-[A-Za-z0-9\-]{10,}",
        r"gh[pousr]_[A-Za-z0-9]{30,}",
        r"github_pat_[A-Za-z0-9_]{30,}",
        r"AIza[0-9A-Za-z_\-]{30,}",
        r"lin_api_[A-Za-z0-9]{30,}",
        r"secret_[A-Za-z0-9]{30,}",
        r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}",
    ]
    out = text
    for p in patterns:
        out = re.sub(p, "[REDACTED]", out)
    for s in extra or []:
        if s and len(s) > 6:
            out = out.replace(s, "[REDACTED]")
    return out
