"""OpenGrokBot entry point.

    python main.py                    open the desktop app (starts the background service if needed)
    python main.py --tray             start hidden in the system tray
    python main.py --service          run only the background service (no UI), e.g. on a VM or in Docker
    python main.py --install-browsers download the Playwright Chromium engine
"""
from __future__ import annotations

import argparse
import multiprocessing
import sys


def main() -> int:
    multiprocessing.freeze_support()
    import os
    if sys.stdout is None:   # windowed .exe has no console; keep print()/logging from crashing
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    if getattr(sys, "frozen", False) and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        # Playwright defaults to "0" (inside the bundle) when frozen; use the normal shared location instead,
        # for both `--install-browsers` and the service (which re-runs this exe with --service).
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(base, "ms-playwright")
    p = argparse.ArgumentParser(prog="OpenGrokBot", description="Always-on AI teammates with a computer of their own.")
    p.add_argument("--service", action="store_true", help="run the background service only (no window)")
    p.add_argument("--tray", action="store_true", help="start minimised to the system tray")
    p.add_argument("--install-browsers", action="store_true", help="install the Playwright Chromium browser")
    p.add_argument("--host", help="service bind address (service mode). Use 0.0.0.0 for phone/LAN access")
    p.add_argument("--port", type=int, help="service port (default 8765)")
    p.add_argument("--ssl-certfile")
    p.add_argument("--ssl-keyfile")
    p.add_argument("--reset-token", action="store_true", help="generate a new access token for the service and the mobile app")
    args = p.parse_args()

    if args.install_browsers:
        from playwright.__main__ import main as pw_main
        sys.argv = ["playwright", "install", "chromium"]
        pw_main()
        return 0
    if args.reset_token:
        from service.runner import reset_token
        print("New access token:", reset_token())
        return 0
    if args.service:
        from service.runner import run_service
        return run_service(args.host, args.port, args.ssl_certfile, args.ssl_keyfile)
    from ui.app import run_ui
    return run_ui(start_hidden=args.tray)


if __name__ == "__main__":
    sys.exit(main())
