"""Optional meaning-based search for the knowledge base. Off by default.

Why it is opt-in: keyword search keeps every document on this PC; the only text a model ever sees is the few passages a Bot
reads. Embeddings are different: to index a document, every passage of it is sent to the embedding service you choose. So the
feature needs an explicit switch with a warning, the administrator policy and the network policy apply, and a local service
(Ollama, LM Studio) is the way to get it without anything leaving the PC.

Only OpenAI-compatible profiles offer embeddings (Anthropic does not), and each vector is stored with the model that made it, so
changing the model re-indexes. Vectors are kept as float32 blobs and compared in pure Python, which is fast enough for a few
thousand passages; bigger libraries search the most recent ones for meaning and all of them by keyword."""
from __future__ import annotations

import array
import math
from operator import mul
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from . import secrets

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

BATCH = 48
MAX_SCAN = 10_000          # passages compared for meaning per search
INDEX_PER_RUN = 192        # passages embedded per background run
DEFAULT_MODEL = "text-embedding-3-small"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class EmbedError(Exception):
    pass


def pack(v: list[float]) -> bytes:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return array.array("f", [x / n for x in v]).tobytes()


def unpack(b: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(b)
    return a


def cosine(a: array.array, b: array.array) -> float:
    return sum(map(mul, a, b)) if len(a) == len(b) else -1.0      # both are unit length, so the dot product is the cosine


class Embedder:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.last_error = ""

    # ------------------------------------------------------------------ configuration
    def config(self) -> dict:
        c = self.engine.settings.get("knowledge", {}) or {}
        return {"enabled": bool(c.get("embeddings", False)), "profile": c.get("profile", ""), "model": c.get("model", "") or DEFAULT_MODEL}

    def _profile(self) -> dict | None:
        pid = self.config()["profile"]
        profs = self.engine.settings.profiles()
        if not pid or pid not in profs:
            return None
        return {**profs[pid], "id": pid}

    def endpoint(self, prof: dict) -> str:
        return (prof.get("base_url") or "https://api.openai.com/v1").rstrip("/") + "/embeddings"

    def is_local(self, prof: dict) -> bool:
        return (urlparse(self.endpoint(prof)).hostname or "") in LOCAL_HOSTS

    def usable(self) -> tuple[bool, str]:
        """(True, '') when meaning-based search may run right now, else (False, why)."""
        c = self.config()
        if not c["enabled"]:
            return False, "Meaning-based search is switched off."
        prof = self._profile()
        if not prof:
            return False, "Choose a provider for embeddings."
        if prof.get("kind") != "openai":
            return False, f"{prof.get('label', prof['id'])} has no embeddings service. Choose an OpenAI-compatible provider (OpenAI, Ollama, LM Studio...)."
        allowed = self.engine.admin.allowed_providers()
        if allowed is not None and prof["id"] not in allowed:
            return False, "The administrator policy does not allow this provider."
        if not self.engine.admin.embeddings_allowed():
            return False, "The administrator policy does not allow sending documents for embedding."
        if prof.get("needs_key", True) and not secrets.get_secret(f"provider:{prof['id']}"):
            return False, "That provider has no API key yet (Settings > Models)."
        if not self.is_local(prof):
            ok, why = self.engine.net.check(None, self.endpoint(prof))
            if not ok:
                return False, f"Blocked by the network policy: {why}"
        return True, ""

    def describe(self) -> dict:
        ok, why = self.usable()
        prof = self._profile()
        return {**self.config(), "usable": ok, "why": why, "local": bool(prof and self.is_local(prof)), "last_error": self.last_error,
                "endpoint_host": urlparse(self.endpoint(prof)).hostname if prof else "", "allowed": self.engine.admin.embeddings_allowed(),
                "providers": [{"id": k, "label": v.get("label", k), "local": self.is_local({**v, "id": k})}
                              for k, v in self.engine.settings.profiles().items() if v.get("kind") == "openai"]}

    # ------------------------------------------------------------------ calling the service
    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx
        ok, why = self.usable()
        if not ok:
            raise EmbedError(why)
        prof, model = self._profile(), self.config()["model"]
        key = secrets.get_secret(f"provider:{prof['id']}") if prof.get("needs_key", True) else ""      # type: ignore[index]
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH):
            batch = [t[:6000] for t in texts[i:i + BATCH]]
            try:
                r = httpx.post(self.endpoint(prof), json={"model": model, "input": batch}, headers=headers, timeout=60)   # type: ignore[arg-type]
            except httpx.HTTPError as e:
                self.last_error = f"Could not reach the embedding service: {e}"
                raise EmbedError(self.last_error) from e
            if r.status_code in (401, 403):
                self.last_error = "The embedding service refused the API key."
                raise EmbedError(self.last_error)
            if r.status_code >= 400:
                self.last_error = f"The embedding service answered HTTP {r.status_code}: {r.text[:160]}"
                raise EmbedError(self.last_error)
            try:
                rows = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
                vecs = [d["embedding"] for d in rows]
            except (ValueError, KeyError, TypeError) as e:
                self.last_error = "The embedding service sent an answer that is not an embedding list."
                raise EmbedError(self.last_error) from e
            if len(vecs) != len(batch):
                self.last_error = "The embedding service returned a different number of vectors than passages."
                raise EmbedError(self.last_error)
            out.extend(vecs)
        self.last_error = ""
        return out
