"""Estimated spend. Prices are entered by you (per million tokens) because providers change them: nothing is guessed.
Local models (Ollama, LM Studio) and `:free` models count as free automatically."""
from __future__ import annotations

from typing import Any

FREE_PROFILES = {"ollama", "lmstudio"}


def key_for(profile: str, model: str) -> str:
    return f"{profile or 'default'}/{model or 'default'}"


class Pricing:
    def __init__(self, settings: Any):
        self.settings = settings

    def table(self) -> dict[str, dict[str, float]]:
        raw = (self.settings.get("pricing", {}) or {}).get("models", {}) or {}
        out = {}
        for k, v in raw.items():
            try:
                out[k] = {"in": max(0.0, float(v.get("in", 0))), "out": max(0.0, float(v.get("out", 0)))}
            except (TypeError, ValueError, AttributeError):
                continue
        return out

    def currency(self) -> str:
        return str((self.settings.get("pricing", {}) or {}).get("currency", "$") or "$")[:3]

    def price(self, profile: str, model: str) -> tuple[float, float] | None:
        """(input, output) price per million tokens, or None when unknown."""
        t = self.table().get(key_for(profile, model))
        if t is not None:
            return t["in"], t["out"]
        if (profile or "") in FREE_PROFILES or (model or "").endswith(":free"):
            return 0.0, 0.0
        return None

    def cost(self, input_tokens: int, output_tokens: int, profile: str, model: str) -> float | None:
        p = self.price(profile, model)
        if p is None:
            return None
        return (input_tokens or 0) / 1e6 * p[0] + (output_tokens or 0) / 1e6 * p[1]

    def set_prices(self, models: dict[str, Any], currency: str | None = None) -> None:
        clean = {}
        for k, v in (models or {}).items():
            try:
                clean[str(k)[:160]] = {"in": max(0.0, round(float(v["in"]), 6)), "out": max(0.0, round(float(v["out"]), 6))}
            except (KeyError, TypeError, ValueError):
                continue
        self.settings.set("pricing", {"currency": (currency or self.currency())[:3], "models": clean})


def fmt_money(amount: float | None, cur: str = "$") -> str:
    if amount is None:
        return "?"
    return f"{cur}{amount:,.2f}" if amount >= 0.01 or amount == 0 else f"{cur}{amount:.4f}"
