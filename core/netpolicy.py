"""Network policy: global and per-Bot allow/deny lists of domains, plus an optional admin preset."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from .db import Database, jload
from .settings import Admin, Settings


def host_of(url: str) -> str:
    try:
        u = urlparse(url if "://" in url else "http://" + url)
        return (u.hostname or "").lower().strip(".")
    except ValueError:
        return ""


def _match(host: str, pattern: str) -> bool:
    p = pattern.lower().strip().strip(".")
    if not p:
        return False
    if p.startswith("*."):
        base = p[2:]
        return host == base or host.endswith("." + base)
    return host == p or host.endswith("." + p)


def _is_private(host: str) -> bool:
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast


class NetworkPolicy:
    def __init__(self, db: Database, settings: Settings, admin: Admin):
        self.db, self.settings, self.admin = db, settings, admin

    def _layers(self, bot_id: str | None) -> list[dict]:
        layers = []
        adm = self.admin.network()
        if adm:
            layers.append({"mode": adm.get("mode", "open"), "allow": adm.get("allow", []), "deny": adm.get("deny", []),
                           "name": "administrator policy", "private": adm.get("block_private", True)})
        locked = bool(adm and adm.get("locked"))
        if not locked:
            g = self.settings.get("network")
            layers.append({"mode": g.get("mode", "open"), "allow": g.get("allow", []), "deny": g.get("deny", []),
                           "name": "global policy", "private": g.get("block_private", True)})
            if bot_id:
                b = self.db.one("SELECT net_mode, net_allow, net_deny FROM bots WHERE id=?", (bot_id,))
                if b and b["net_mode"] in ("open", "allowlist"):
                    layers.append({"mode": b["net_mode"], "allow": jload(b["net_allow"], []), "deny": jload(b["net_deny"], []),
                                   "name": "this Bot's policy", "private": False})
        return layers

    def check(self, bot_id: str | None, url: str, resolve: bool = False) -> tuple[bool, str]:
        host = host_of(url)
        if not host:
            return False, "Invalid URL."
        scheme = urlparse(url if "://" in url else "http://" + url).scheme
        if scheme not in ("http", "https"):
            return False, f"Scheme '{scheme}' is not allowed (only http/https)."
        layers = self._layers(bot_id)
        for layer in layers:
            if layer["private"] and _is_private(host):
                return False, f"{host} is a local/private address and is blocked by the {layer['name']}."
            if any(_match(host, d) for d in layer["deny"]):
                return False, f"{host} is on the deny list of the {layer['name']}."
            if layer["mode"] == "allowlist" and not any(_match(host, a) for a in layer["allow"]):
                return False, f"{host} is not on the allow list of the {layer['name']}."
        if resolve and any(l["private"] for l in layers):
            try:
                for info in socket.getaddrinfo(host, None):
                    if _is_private(info[4][0]):
                        return False, f"{host} resolves to a private address and is blocked."
            except OSError:
                pass
        return True, ""

    def describe(self, bot_id: str | None) -> list[dict]:
        return [{"name": l["name"], "mode": l["mode"], "allow": l["allow"], "deny": l["deny"]} for l in self._layers(bot_id)]
