"""Notification delivery: Windows toasts (when no UI is attached) and optional ntfy push to a phone."""
from __future__ import annotations

import os
import subprocess
import threading

_TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$n = $t.GetElementsByTagName('text')
$n.Item(0).AppendChild($t.CreateTextNode($env:GB_TITLE)) > $null
$n.Item(1).AppendChild($t.CreateTextNode($env:GB_BODY)) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($t)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show($toast)
"""


def toast(title: str, body: str) -> None:
    """Native Windows toast without extra dependencies (used by the background service when the window is closed)."""
    if os.name != "nt":
        return
    env = {**os.environ, "GB_TITLE": title[:120], "GB_BODY": body[:240]}
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", _TOAST_PS], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError:
        pass


def ntfy(base_url: str, topic: str, title: str, body: str, urgent: bool = False, click_url: str = "") -> None:
    """Push to a phone through an ntfy server (https://ntfy.sh or your own)."""
    if not base_url or not topic:
        return

    def send() -> None:
        try:
            import httpx
            headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore") or "OpenGrokBot", "Priority": "high" if urgent else "default", "Tags": "robot"}
            if click_url:
                headers["Click"] = click_url
            httpx.post(f"{base_url.rstrip('/')}/{topic}", content=body.encode("utf-8"), headers=headers, timeout=10)
        except Exception:
            pass

    threading.Thread(target=send, daemon=True).start()
