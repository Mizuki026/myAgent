"""Scripted external Chat Completions server; never executes agent tools."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def tool_message(name, arguments, call_id):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
    }


def final_message(kind, **fields):
    return {"role": "assistant", "content": json.dumps({"kind": kind, **fields})}


class ScriptedModelServer:
    def __init__(self, steps, expected_api_key=None):
        self.steps = list(steps)
        self.requests = []
        self.delivered = []
        self.errors = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 2_000_000:
                        raise ValueError("Invalid request size")
                    request = json.loads(self.rfile.read(size))
                    with owner._lock:
                        owner.requests.append(copy.deepcopy(request))
                        if self.path not in ("/chat/completions", "/v1/chat/completions"):
                            raise ValueError(f"Unexpected endpoint: {self.path}")
                        if expected_api_key and self.headers.get("Authorization") != f"Bearer {expected_api_key}":
                            raise ValueError("Missing or invalid test API key")
                        if request.get("stream"):
                            raise ValueError("Acceptance server requires stream=false")
                        if len(owner.delivered) >= len(owner.steps):
                            raise ValueError("Model script exhausted: unexpected extra request")
                        step = owner.steps[len(owner.delivered)]
                        if step.get("before_send"):
                            step["before_send"](request)
                        message = copy.deepcopy(step["message"])
                        owner.delivered.append(step)
                    payload = {
                        "id": f"fixture-{len(owner.delivered)}",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "fixture-model",
                        "choices": [{
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                        }],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                    }
                    encoded = json.dumps(payload).encode("utf-8")
                    self.send_response(200)
                except Exception as error:
                    with owner._lock:
                        owner.errors.append(str(error))
                    encoded = json.dumps({"error": {"message": str(error)}}).encode("utf-8")
                    self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True
        )

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    @property
    def implementation_replies(self):
        return sum(step.get("implementation", False) for step in self.delivered)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
