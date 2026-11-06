#!/usr/bin/env python3
"""A scriptable stand-in for the Tilburg.ai (OpenAI-style) API, for tests.

The fake model reads the LAST user message. If it contains `MOCK:` followed by a JSON list, each
element scripts one assistant reply after that message (the n-th reply after the user message uses
element n):
    {"text": "...", "tools": [{"name": "read_file", "args": {...}}], "delay": 0.2,
     "chunk_delay": 0.01, "error": 500, "hang": 30}
When the script is used up, or there is no script, the model replies "Echo: <message text>".
GET /_log returns every chat request received; POST /_reset clears it.

  python3 mock_api.py [port]        prints "PORT <n>" once listening
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = []
LOCK = threading.Lock()
MODELS = [{"id": "gpt-5.6-sol", "max_input_tokens": 400000, "mode": "chat"},
          {"id": "mock-small", "max_input_tokens": 1000, "mode": "chat"},
          {"id": "text-embedding-3", "mode": "embedding"}]


def text_of(content):
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return content or ""


def script_step(messages):
    last = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last = i
            break
    if last is None:
        return {"text": "Echo: (nothing)"}, ""
    content = text_of(messages[last].get("content"))
    n = sum(1 for m in messages[last + 1:] if m.get("role") == "assistant")
    if "MOCK:" in content:
        try:
            script = json.loads(content.split("MOCK:", 1)[1].strip())
        except ValueError as e:
            return {"text": f"bad mock script: {e}"}, content
        if n < len(script):
            return script[n], content
        return {"text": "done"}, content
    visible = content.split("</ide_context>")[-1].strip()
    return {"text": f"Echo: {visible}"}, content


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            return self._json({"data": MODELS})
        if self.path.endswith("/_log"):
            with LOCK:
                return self._json(LOG)
        self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        if self.path.endswith("/_reset"):
            with LOCK:
                LOG.clear()
            return self._json({})
        if self.headers.get("Authorization") != "Bearer test-key":
            return self._json({"error": {"message": "invalid api key"}}, 401)
        if self.path.endswith("/responses"):
            q = payload.get("input", "")
            return self._json({"output": [{"type": "message", "content": [{
                "text": f"Search results for: {q.splitlines()[0]}",
                "annotations": [{"url": "https://example.org/a", "title": "Example A"}]}]}]})
        if not self.path.endswith("/chat/completions"):
            return self._json({"error": {"message": "not found"}}, 404)
        with LOCK:
            LOG.append(payload)
        step, _ = script_step(payload.get("messages") or [])
        if step.get("delay"):
            time.sleep(step["delay"])
        if step.get("error"):
            return self._json({"error": {"message": step.get("message", "mock failure")}}, step["error"])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def ev(obj):
            self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
            self.wfile.flush()
        try:
            if step.get("hang"):
                ev({"choices": [{"delta": {"content": step.get("text", "")}}]})
                time.sleep(step["hang"])
            text = step.get("text", "")
            cd = step.get("chunk_delay", 0)
            for i in range(0, len(text), 7):
                ev({"choices": [{"delta": {"content": text[i:i + 7]}}]})
                if cd:
                    time.sleep(cd)
            for k, t in enumerate(step.get("tools") or []):
                args = json.dumps(t.get("args", {}))
                ev({"choices": [{"delta": {"tool_calls": [{"index": k, "id": t.get("id", f"call_{time.time_ns()}_{k}"),
                                                           "function": {"name": t["name"], "arguments": ""}}]}}]})
                for i in range(0, len(args), 20):
                    ev({"choices": [{"delta": {"tool_calls": [{"index": k, "function": {"arguments": args[i:i + 20]}}]}}]})
            ptoks = len(json.dumps(payload.get("messages"))) // 4
            ev({"choices": [], "usage": {"prompt_tokens": ptoks, "completion_tokens": max(1, len(text) // 4)}})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def start(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    srv = start(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
    print(f"PORT {srv.server_address[1]}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
