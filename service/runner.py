"""Starts, finds and stops the background service process that keeps Bots and routines alive."""
from __future__ import annotations

import json
import os
import secrets as pysecrets
import subprocess
import sys
import time
from pathlib import Path

from core import paths, secrets

TOKEN_NAME = "service:token"


def service_token() -> str:
    """Access token for the local API. Order: env var, credential store, (headless hosts only) a private file."""
    env = os.environ.get("OPENGROKBOT_TOKEN")
    if env:
        return env
    tok = secrets.get_secret(TOKEN_NAME)
    if tok:
        return tok
    tok = pysecrets.token_urlsafe(32)
    try:
        secrets.set_secret(TOKEN_NAME, tok)
        return tok
    except secrets.SecretStoreError:
        f = paths.data_dir() / "service_token.txt"
        if f.exists():
            return f.read_text().strip()
        f.write_text(tok)
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
        return tok


def reset_token() -> str:
    tok = pysecrets.token_urlsafe(32)
    secrets.set_secret(TOKEN_NAME, tok)
    return tok


def read_info() -> dict | None:
    try:
        return json.loads(paths.service_info_path().read_text())
    except (OSError, ValueError):
        return None


def probe(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        import httpx
        r = httpx.get(f"http://{host}:{port}/api/health", timeout=timeout)
        return r.status_code == 200 and r.json().get("name") == "OpenGrokBot"
    except Exception:
        return False


def configured_endpoint() -> tuple[str, int]:
    """Host/port the service should use, from settings (db) with sane defaults."""
    host, port = "127.0.0.1", 8765
    try:
        from core.db import Database
        from core.settings import Settings
        s = Settings(Database(paths.db_path()))
        host = s.get("mobile.host", host)
        port = int(s.get("mobile.port", port))
    except Exception:
        pass
    return host, port


def local_url(host: str, port: int) -> str:
    return f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"


def spawn_detached() -> None:
    """Launch the service as a detached process (survives closing the app window)."""
    if getattr(sys, "frozen", False):
        args = [sys.executable, "--service"]
    else:
        args = [sys.executable, str(Path(__file__).resolve().parent.parent / "main.py"), "--service"]
    log = open(paths.logs_dir() / "service-stdout.log", "ab")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=flags, close_fds=True,
                     start_new_session=(os.name != "nt"))


def ensure_running(timeout: float = 45.0) -> tuple[str, int]:
    host, port = configured_endpoint()
    h = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    if probe(h, port):
        return h, port
    spawn_detached()
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.5)
        if probe(h, port):
            return h, port
    raise RuntimeError(f"The background service did not start. See {paths.logs_dir() / 'service.log'}")


def run_service(host: str | None = None, port: int | None = None, ssl_certfile: str | None = None, ssl_keyfile: str | None = None) -> int:
    """Run the engine + API in this process (blocking)."""
    import uvicorn
    from core.engine import Engine
    from service.server import create_app

    cfg_host, cfg_port = configured_endpoint()
    host = host or cfg_host
    port = port or cfg_port
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    if probe(probe_host, port):
        print(f"OpenGrokBot service is already running on port {port}.")
        return 0
    engine = Engine()
    scheme = "https" if ssl_certfile else "http"
    engine.base_url = f"{scheme}://127.0.0.1:{port}"
    token = service_token()
    app = create_app(engine, token)
    engine.start()
    paths.service_info_path().write_text(json.dumps({"host": host, "port": port, "pid": os.getpid(), "started": time.time(), "scheme": scheme}))
    engine.log.info("Service listening on %s://%s:%d", scheme, host, port)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning", ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile, timeout_graceful_shutdown=3)
    finally:
        engine.stop()
        try:
            paths.service_info_path().unlink()
        except OSError:
            pass
    return 0
