"""Provider tests against local fake servers (no network, no API keys).  python -m unittest tests.test_providers"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENGROKBOT_HOME", tempfile.mkdtemp(prefix="gbtest-prov-"))

from core.providers import AnthropicProvider, OpenAICompatProvider, ProviderError, StopRequested  # noqa: E402

MODE = {"status": 200}


def sse(lines):
    return "".join(lines).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def _send(self, code, body, ctype="text/event-stream", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            if self.path.startswith("/v1/models") and "anthropic" in self.headers.get("User-Agent", "").lower():
                return self._send(200, json.dumps({"data": [{"id": "claude-x", "type": "model"}], "has_more": False}).encode(), "application/json")
            return self._send(200, json.dumps({"object": "list", "data": [{"id": "gpt-b", "object": "model"}, {"id": "gpt-a", "object": "model"}]}).encode(), "application/json")
        self._send(404, b"{}", "application/json")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        self.server.last_body = body  # type: ignore[attr-defined]
        if MODE["status"] != 200:
            err = {"error": {"message": "nope", "type": "x"}, "type": "error"}
            return self._send(MODE["status"], json.dumps(err).encode(), "application/json", {"retry-after": "3"})
        if self.path.startswith("/oai/chat/completions") or self.path.endswith("/chat/completions"):
            ch = lambda d, fr=None: "data: " + json.dumps({"id": "1", "object": "chat.completion.chunk", "created": 1, "model": "m",
                                                          "choices": [{"index": 0, "delta": d, "finish_reason": fr}]}) + "\n\n"
            out = sse([
                ch({"role": "assistant", "content": "Hel"}), ch({"content": "lo"}),
                ch({"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "fs_list", "arguments": "{\"pa"}}]}),
                ch({"tool_calls": [{"index": 0, "function": {"arguments": "th\": \".\"}"}}]}),
                ch({}, "tool_calls"),
                "data: " + json.dumps({"id": "1", "object": "chat.completion.chunk", "created": 1, "model": "m", "choices": [],
                                       "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}) + "\n\n",
                "data: [DONE]\n\n"])
            return self._send(200, out)
        if self.path.endswith("/v1/messages"):
            ev = lambda t, d: f"event: {t}\ndata: {json.dumps(d)}\n\n"
            out = sse([
                ev("message_start", {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "x", "content": [],
                                                                         "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 11, "output_tokens": 1}}}),
                ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi "}}),
                ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "there"}}),
                ev("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ev("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "fs_list", "input": {}}}),
                ev("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"path\":"}}),
                ev("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": " \".\"}"}}),
                ev("content_block_stop", {"type": "content_block_stop", "index": 1}),
                ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 12}}),
                ev("message_stop", {"type": "message_stop"})])
            return self._send(200, out)
        self._send(404, b"{}", "application/json")


class ProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        MODE["status"] = 200

    def openai(self):
        return OpenAICompatProvider({"id": "t", "label": "Test", "kind": "openai", "base_url": f"http://127.0.0.1:{self.port}/oai", "vision": True, "needs_key": False}, "m", None)

    def anthropic(self):
        return AnthropicProvider({"id": "a", "label": "Anthropic", "kind": "anthropic", "base_url": f"http://127.0.0.1:{self.port}", "vision": True}, "claude-x", "sk-test")

    TOOLS = [{"name": "fs_list", "description": "list", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}]
    MSGS = [{"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}, {"type": "tool_use", "id": "t0", "name": "fs_list", "input": {"path": "."}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t0", "content": [{"type": "text", "text": "a.txt"}], "is_error": False}]}]

    def test_openai_stream_text_tools_usage(self):
        got = []
        r = self.openai().stream("sys", self.MSGS, self.TOOLS, got.append, lambda: False)
        self.assertEqual(r.text, "Hello")
        self.assertEqual("".join(got), "Hello")
        self.assertEqual(r.tool_calls[0].name, "fs_list")
        self.assertEqual(r.tool_calls[0].input, {"path": "."})
        self.assertEqual((r.input_tokens, r.output_tokens), (10, 5))
        sent = self.srv.last_body
        self.assertEqual(sent["messages"][0]["role"], "system")
        self.assertEqual(sent["messages"][-1]["role"], "tool")
        self.assertEqual(sent["tools"][0]["function"]["name"], "fs_list")

    def test_anthropic_stream_text_tools_usage(self):
        got = []
        r = self.anthropic().stream("sys", self.MSGS, self.TOOLS, got.append, lambda: False)
        self.assertEqual(r.text, "Hi there")
        self.assertEqual(r.tool_calls[0].input, {"path": "."})
        self.assertEqual(r.stop_reason, "tool_use")
        self.assertEqual(r.output_tokens, 12)
        sent = self.srv.last_body
        self.assertEqual(sent["messages"][-1]["content"][0]["type"], "tool_result")

    def test_errors_are_classified(self):
        for status, kind in ((401, "auth"), (429, "rate_limit"), (500, "server"), (400, "bad_request")):
            MODE["status"] = status
            for prov in (self.openai(), self.anthropic()):
                with self.assertRaises(ProviderError) as cm:
                    prov.stream("s", self.MSGS, [], lambda t: None, lambda: False)
                self.assertEqual(cm.exception.kind, kind, (status, type(prov).__name__))
        MODE["status"] = 429
        with self.assertRaises(ProviderError) as cm:
            self.openai().stream("s", self.MSGS, [], lambda t: None, lambda: False)
        self.assertEqual(cm.exception.retry_after, 3.0)

    def test_network_down(self):
        p = OpenAICompatProvider({"id": "t", "label": "T", "kind": "openai", "base_url": "http://127.0.0.1:9/v1", "needs_key": False}, "m", None)
        with self.assertRaises(ProviderError) as cm:
            p.stream("s", self.MSGS, [], lambda t: None, lambda: False)
        self.assertEqual(cm.exception.kind, "network")

    def test_stop_requested(self):
        with self.assertRaises(StopRequested):
            self.openai().stream("s", self.MSGS, [], lambda t: None, lambda: True)

    def test_missing_key(self):
        p = AnthropicProvider({"id": "a", "label": "Anthropic", "kind": "anthropic"}, "m", None)
        with self.assertRaises(ProviderError) as cm:
            p.stream("s", self.MSGS, [], lambda t: None, lambda: False)
        self.assertEqual(cm.exception.kind, "auth")

    def test_list_models(self):
        self.assertEqual(self.openai().list_models(), ["gpt-a", "gpt-b"])


if __name__ == "__main__":
    unittest.main()
