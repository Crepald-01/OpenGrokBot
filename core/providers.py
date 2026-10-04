"""Model providers: Anthropic and any OpenAI-compatible endpoint.

Internally conversations use Anthropic-style content blocks:
  {"type":"text","text":...}
  {"type":"image","path":...,"media_type":"image/jpeg"}          (loaded lazily from disk)
  {"type":"tool_use","id":...,"name":...,"input":{...}}
  {"type":"tool_result","tool_use_id":...,"content":[text/image blocks],"is_error":bool}
Providers translate to their wire formats and normalise errors to ProviderError.
"""
from __future__ import annotations

import base64
import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import secrets
from .settings import Settings


class ProviderError(Exception):
    """kind: auth | rate_limit | network | bad_request | server | unknown"""

    def __init__(self, kind: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class StopRequested(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    def blocks(self) -> list[dict]:
        out: list[dict] = []
        if self.text.strip():
            out.append({"type": "text", "text": self.text})
        for tc in self.tool_calls:
            out.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
        return out


# ---------------------------------------------------------------- helpers
def estimate_tokens(messages: list[dict], system: str = "") -> int:
    n = len(system)
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            n += len(c)
            continue
        for b in c or []:
            t = b.get("type")
            if t == "text":
                n += len(b.get("text", ""))
            elif t == "tool_use":
                n += len(json.dumps(b.get("input", {}))) + 40
            elif t == "tool_result":
                for x in b.get("content") or []:
                    n += len(x.get("text", "")) if x.get("type") == "text" else 1200
            elif t == "image":
                n += 1200
    return n // 4


def _load_image(block: dict) -> tuple[str, str] | None:
    if block.get("data"):
        return block.get("media_type", "image/jpeg"), block["data"]
    p = block.get("path")
    if p and Path(p).is_file():
        return block.get("media_type", "image/jpeg"), base64.b64encode(Path(p).read_bytes()).decode()
    return None


def prepare_messages(messages: list[dict], vision: bool, keep_images: int = 3) -> list[dict]:
    """Deep-copy, drop UI-only keys, keep only the newest N images, inline image bytes."""
    msgs = copy.deepcopy(messages)
    total = 0
    for m in msgs:
        if isinstance(m.get("content"), list):
            for b in m["content"]:
                if b.get("type") == "image":
                    total += 1
                elif b.get("type") == "tool_result":
                    total += sum(1 for x in b.get("content") or [] if x.get("type") == "image")
    drop = max(0, total - keep_images)

    def fix(b: dict) -> dict | None:
        nonlocal drop
        if b.get("type") != "image":
            return b
        if not vision:
            return {"type": "text", "text": "[image omitted: this model has no vision support. Use browser_snapshot for page text.]"}
        if drop > 0:
            drop -= 1
            return {"type": "text", "text": "[older screenshot omitted to save context]"}
        loaded = _load_image(b)
        if not loaded:
            return {"type": "text", "text": "[image no longer available]"}
        return {"type": "image", "media_type": loaded[0], "data": loaded[1]}

    for m in msgs:
        if isinstance(m.get("content"), str):
            m["content"] = [{"type": "text", "text": m["content"]}]
            continue
        new = []
        for b in m.get("content") or []:
            if b.get("type") == "tool_result":
                inner = []
                for x in b.get("content") or []:
                    fx = fix(x)
                    if fx:
                        inner.append(fx)
                if isinstance(b.get("content"), str):
                    inner = [{"type": "text", "text": b["content"]}]
                new.append({"type": "tool_result", "tool_use_id": b["tool_use_id"], "content": inner,
                            "is_error": bool(b.get("is_error"))})
            elif b.get("type") == "tool_use":
                new.append({"type": "tool_use", "id": b["id"], "name": b["name"], "input": b.get("input") or {}})
            else:
                fb = fix(b)
                if fb:
                    new.append(fb)
        m["content"] = new
    return msgs


def _merge_same_role(msgs: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in msgs:
        if not m["content"]:
            continue
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] = out[-1]["content"] + m["content"]
        else:
            out.append({"role": m["role"], "content": list(m["content"])})
    return out


class Provider:
    kind = ""

    def __init__(self, profile: dict, model: str, api_key: str | None):
        self.profile = profile
        self.model = model
        self.api_key = api_key
        self.vision = bool(profile.get("vision", True))
        self.label = profile.get("label", profile.get("id", ""))

    def stream(self, system: str, messages: list[dict], tools: list[dict], on_text: Callable[[str], None],
               should_stop: Callable[[], bool], max_tokens: int = 8192) -> LLMResult:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        raise NotImplementedError


# ---------------------------------------------------------------- Anthropic
class AnthropicProvider(Provider):
    kind = "anthropic"

    def _client(self):
        import anthropic
        # a plain float works across SDK generations (newer SDKs reject httpx.Timeout objects)
        kwargs: dict[str, Any] = {"api_key": self.api_key, "max_retries": 0, "timeout": 180.0}
        if self.profile.get("base_url"):
            kwargs["base_url"] = self.profile["base_url"]
        return anthropic.Anthropic(**kwargs)

    def _convert(self, messages: list[dict]) -> list[dict]:
        msgs = prepare_messages(messages, self.vision)
        out = []
        for m in msgs:
            blocks = []
            for b in m["content"]:
                t = b["type"]
                if t == "text":
                    if b["text"].strip():
                        blocks.append({"type": "text", "text": b["text"]})
                elif t == "image":
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": b["media_type"], "data": b["data"]}})
                elif t == "tool_use":
                    blocks.append(b)
                elif t == "tool_result":
                    inner = []
                    for x in b["content"]:
                        if x["type"] == "text":
                            inner.append({"type": "text", "text": x["text"] or "(empty)"})
                        elif x["type"] == "image":
                            inner.append({"type": "image", "source": {"type": "base64", "media_type": x["media_type"], "data": x["data"]}})
                    blocks.append({"type": "tool_result", "tool_use_id": b["tool_use_id"],
                                   "content": inner or [{"type": "text", "text": "(no output)"}], "is_error": b["is_error"]})
            out.append({"role": m["role"], "content": blocks})
        out = _merge_same_role(out)
        # tool_result blocks must come first in a user message
        for m in out:
            if m["role"] == "user":
                m["content"].sort(key=lambda b: 0 if b["type"] == "tool_result" else 1)
        return out

    def stream(self, system, messages, tools, on_text, should_stop, max_tokens=8192) -> LLMResult:
        import anthropic
        if not self.api_key:
            raise ProviderError("auth", f"No API key set for {self.label}. Add it in Settings > Providers.")
        client = self._client()
        kwargs: dict[str, Any] = dict(model=self.model, max_tokens=max_tokens, messages=self._convert(messages),
                                      system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}])
        if tools:
            kwargs["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in tools]
        res = LLMResult()
        try:
            with client.messages.stream(**kwargs) as stream:
                for text in stream.text_stream:
                    if should_stop():
                        raise StopRequested()
                    res.text += text
                    on_text(text)
                final = stream.get_final_message()
            for b in final.content:
                if b.type == "tool_use":
                    res.tool_calls.append(ToolCall(b.id, b.name, dict(b.input or {})))
            res.stop_reason = final.stop_reason or ""
            res.input_tokens = (final.usage.input_tokens or 0) + (getattr(final.usage, "cache_read_input_tokens", 0) or 0) \
                + (getattr(final.usage, "cache_creation_input_tokens", 0) or 0)
            res.output_tokens = final.usage.output_tokens or 0
            if not res.text:
                res.text = "".join(b.text for b in final.content if b.type == "text")
            return res
        except StopRequested:
            raise
        except anthropic.APIStatusError as e:
            raise self._map_status(e) from e
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            raise ProviderError("network", "Network connection lost while contacting Anthropic. Check your internet connection.") from e
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError("unknown", f"{type(e).__name__}: {e}") from e

    @staticmethod
    def _map_status(e) -> ProviderError:
        code = getattr(e, "status_code", 0)
        msg = getattr(e, "message", str(e))
        if code in (401, 403):
            return ProviderError("auth", f"Authentication failed (HTTP {code}). Check the API key in Settings > Providers. {msg}")
        if code == 429:
            ra = None
            try:
                ra = float(e.response.headers.get("retry-after"))
            except Exception:
                pass
            return ProviderError("rate_limit", f"Rate limited by the provider. {msg}", ra)
        if code == 404:
            return ProviderError("bad_request", f"Model or endpoint not found. Check the model name. {msg}")
        if code in (400, 413, 422):
            return ProviderError("bad_request", f"The provider rejected the request: {msg}")
        if code >= 500 or code == 529:
            return ProviderError("server", f"The provider is having trouble (HTTP {code}). {msg}")
        return ProviderError("unknown", f"HTTP {code}: {msg}")

    def list_models(self) -> list[str]:
        import anthropic
        try:
            page = self._client().models.list(limit=100)
            return [m.id for m in page.data]
        except anthropic.APIStatusError as e:
            raise self._map_status(e) from e
        except Exception as e:
            raise ProviderError("network", f"Could not reach Anthropic: {e}") from e


# ---------------------------------------------------------------- OpenAI-compatible
class OpenAICompatProvider(Provider):
    kind = "openai"

    def _client(self):
        import openai
        return openai.OpenAI(api_key=self.api_key or "not-needed", base_url=self.profile.get("base_url") or None,
                             max_retries=0, timeout=180.0)

    def _convert(self, system: str, messages: list[dict]) -> list[dict]:
        msgs = prepare_messages(messages, self.vision)
        out: list[dict] = [{"role": "system", "content": system}]
        for m in msgs:
            if m["role"] == "assistant":
                text = "".join(b["text"] for b in m["content"] if b["type"] == "text")
                calls = [{"id": b["id"], "type": "function",
                          "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                         for b in m["content"] if b["type"] == "tool_use"]
                msg: dict[str, Any] = {"role": "assistant", "content": text or None}
                if calls:
                    msg["tool_calls"] = calls
                if text or calls:
                    out.append(msg)
            else:
                parts: list[dict] = []
                image_parts: list[dict] = []
                for b in m["content"]:
                    if b["type"] == "text":
                        parts.append({"type": "text", "text": b["text"]})
                    elif b["type"] == "image":
                        parts.append({"type": "image_url", "image_url": {"url": f"data:{b['media_type']};base64,{b['data']}"}})
                    elif b["type"] == "tool_result":
                        txt = "\n".join(x["text"] for x in b["content"] if x["type"] == "text") or "(no output)"
                        if b["is_error"]:
                            txt = "ERROR: " + txt
                        out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": txt})
                        for x in b["content"]:
                            if x["type"] == "image":
                                image_parts.append({"type": "image_url", "image_url": {"url": f"data:{x['media_type']};base64,{x['data']}"}})
                if image_parts:
                    out.append({"role": "user", "content": [{"type": "text", "text": "(Image output from the previous tool call)"}] + image_parts})
                if parts:
                    if all(p["type"] == "text" for p in parts):
                        out.append({"role": "user", "content": "\n".join(p["text"] for p in parts)})
                    else:
                        out.append({"role": "user", "content": parts})
        return out

    def stream(self, system, messages, tools, on_text, should_stop, max_tokens=8192) -> LLMResult:
        import openai
        client = self._client()
        if self.profile.get("needs_key", True) and not self.api_key:
            raise ProviderError("auth", f"No API key set for {self.label}. Add it in Settings > Providers.")
        kwargs: dict[str, Any] = dict(model=self.model, messages=self._convert(system, messages), stream=True,
                                      stream_options={"include_usage": True})
        if tools:
            kwargs["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                                "parameters": t["input_schema"]}} for t in tools]
        res = LLMResult()
        acc: dict[int, dict] = {}
        for attempt in (0, 1):
            try:
                with client.chat.completions.create(**kwargs) as stream:
                    for chunk in stream:
                        if should_stop():
                            raise StopRequested()
                        if getattr(chunk, "usage", None):
                            res.input_tokens = chunk.usage.prompt_tokens or 0
                            res.output_tokens = chunk.usage.completion_tokens or 0
                        if not chunk.choices:
                            continue
                        ch = chunk.choices[0]
                        d = ch.delta
                        if d is not None and getattr(d, "content", None):
                            res.text += d.content
                            on_text(d.content)
                        for tc in (getattr(d, "tool_calls", None) or []):
                            slot = acc.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                            if tc.id:
                                slot["id"] = tc.id
                            if tc.function:
                                if tc.function.name:
                                    slot["name"] += tc.function.name
                                if tc.function.arguments:
                                    slot["args"] += tc.function.arguments
                        if ch.finish_reason:
                            res.stop_reason = ch.finish_reason
                break
            except StopRequested:
                raise
            except openai.BadRequestError as e:
                if attempt == 0 and "stream_options" in str(e):
                    kwargs.pop("stream_options", None)
                    res, acc = LLMResult(), {}
                    continue
                raise self._map_status(e) from e
            except openai.APIStatusError as e:
                raise self._map_status(e) from e
            except (openai.APIConnectionError, openai.APITimeoutError) as e:
                where = self.profile.get("base_url") or "the provider"
                raise ProviderError("network", f"Network connection lost while contacting {where}. "
                                               f"Is the server running and are you online?") from e
            except ProviderError:
                raise
            except Exception as e:
                raise ProviderError("unknown", f"{type(e).__name__}: {e}") from e
        for i in sorted(acc):
            s = acc[i]
            try:
                args = json.loads(s["args"] or "{}")
                if not isinstance(args, dict):
                    args = {"value": args}
            except ValueError:
                args = {"__parse_error": s["args"]}
            res.tool_calls.append(ToolCall(s["id"] or f"call_{i}_{abs(hash(s['name'])) % 99999}", s["name"], args))
        if not res.input_tokens:
            res.input_tokens = estimate_tokens(messages, system)
            res.output_tokens = max(1, len(res.text) // 4)
        return res

    @staticmethod
    def _map_status(e) -> ProviderError:
        code = getattr(e, "status_code", 0)
        msg = getattr(e, "message", str(e))
        if code in (401, 403):
            return ProviderError("auth", f"Authentication failed (HTTP {code}). Check the API key in Settings > Providers. {msg}")
        if code == 429:
            ra = None
            try:
                ra = float(e.response.headers.get("retry-after"))
            except Exception:
                pass
            return ProviderError("rate_limit", f"Rate limited by the provider. {msg}", ra)
        if code == 404:
            return ProviderError("bad_request", f"Model or endpoint not found. Check the model name and base URL. {msg}")
        if code in (400, 413, 422):
            return ProviderError("bad_request", f"The provider rejected the request: {msg}")
        if code >= 500:
            return ProviderError("server", f"The provider is having trouble (HTTP {code}). {msg}")
        return ProviderError("unknown", f"HTTP {code}: {msg}")

    def list_models(self) -> list[str]:
        import openai
        try:
            return sorted(m.id for m in self._client().models.list().data)
        except openai.APIStatusError as e:
            raise self._map_status(e) from e
        except Exception as e:
            raise ProviderError("network", f"Could not reach {self.profile.get('base_url')}: {e}") from e


def make_provider(settings: Settings, profile_id: str | None, model: str | None) -> Provider:
    prof = settings.profile(profile_id)
    key = secrets.get_secret(f"provider:{prof['id']}")
    mdl = model or prof.get("model") or ""
    if not mdl:
        raise ProviderError("bad_request", f"No model chosen for {prof.get('label', prof['id'])}. Pick one in Settings.")
    cls = AnthropicProvider if prof.get("kind") == "anthropic" else OpenAICompatProvider
    return cls(prof, mdl, key)


def friendly_error(err: ProviderError) -> str:
    prefix = {"auth": "API key problem", "rate_limit": "Rate limit", "network": "Network problem",
              "bad_request": "Request rejected", "server": "Provider outage", "budget": "Daily budget"}.get(err.kind, "Model error")
    return f"{prefix}: {err}"


def extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a model reply."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) else None
    except ValueError:
        return None
