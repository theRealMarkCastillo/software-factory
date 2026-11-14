"""Scripted chat completions for attestation, not a general model provider.

Original implementation using only the standard library. Requests with tools
are main agent turns and consume the script; tool-less auxiliary requests get
the default text without consuming probes. Both are recorded. The listener is
always IPv4 loopback on an OS-selected ephemeral port, with no remote calls.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True)
class Text:
    content: str


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


class FakeLLMServer:
    """Context-managed, local-only OpenAI chat-completions script."""

    def __init__(
        self, script: list[Text | ToolCall], *, default_text: str = "finished"
    ) -> None:
        self._script = deque(script)
        self.default_text = default_text
        self.requests: list[dict] = []
        self._lock = threading.Lock()

    def drop_unconsumed(self) -> int:
        with self._lock:
            count = len(self._script)
            self._script.clear()
            return count

    def _completion(self, body: dict) -> tuple[dict, str]:
        kind = "main" if body.get("tools") else "auxiliary"
        with self._lock:
            self.requests.append({"kind": kind, "body": body})
            turn = (
                self._script.popleft()
                if kind == "main" and self._script
                else Text(self.default_text)
            )
            call_id = f"call_{len(self.requests)}"
        if isinstance(turn, ToolCall):
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": turn.name,
                            "arguments": json.dumps(turn.arguments),
                        },
                    }
                ],
            }, "tool_calls"
        return {"role": "assistant", "content": turn.content}, "stop"

    def __enter__(self) -> FakeLLMServer:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:
                pass

            def do_POST(self) -> None:
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 8 * 1024 * 1024:
                        self.send_error(413)
                        return
                    body = json.loads(self.rfile.read(size))
                    if not isinstance(body, dict):
                        raise ValueError("expected an object")
                except (ValueError, UnicodeError):
                    self.send_error(400)
                    return
                message, finish = owner._completion(body)
                envelope = {
                    "id": "chatcmpl-loopback",
                    "created": 0,
                    "model": body.get("model", "fake-model"),
                }
                if body.get("stream"):
                    delta = dict(message)
                    if "tool_calls" in delta:
                        delta["tool_calls"] = [
                            dict(call, index=index)
                            for index, call in enumerate(delta["tool_calls"])
                        ]
                    chunks = [
                        {
                            **envelope,
                            "object": "chat.completion.chunk",
                            "choices": [
                                {"index": 0, "delta": delta, "finish_reason": None}
                            ],
                        },
                        {
                            **envelope,
                            "object": "chat.completion.chunk",
                            "choices": [
                                {"index": 0, "delta": {}, "finish_reason": finish}
                            ],
                        },
                    ]
                    data = (
                        "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
                        + "data: [DONE]\n\n"
                    ).encode()
                    content_type = "text/event-stream"
                else:
                    data = json.dumps(
                        {
                            **envelope,
                            "object": "chat.completion",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": message,
                                    "finish_reason": finish,
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 0,
                                "completion_tokens": 0,
                                "total_tokens": 0,
                            },
                        }
                    ).encode()
                    content_type = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
