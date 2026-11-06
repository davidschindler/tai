import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mock_api  # noqa: E402

TAI = os.environ.get("TAI_BIN", str(Path(__file__).resolve().parents[1] / "tai.py"))
WINDOWS = os.name == "nt"
SHELL = "powershell" if WINDOWS else "bash"     # name of tai's shell tool on this OS


@pytest.fixture(scope="session")
def api():
    srv = mock_api.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def mock(*steps):
    return "MOCK: " + json.dumps(list(steps))


class Client:
    """Drives `tai --serve`. Every stdout line must be valid JSON (protocol purity)."""

    def __init__(self, api, tmp_path, extra_env=None, args=(), key="test-key"):
        self.ws = tmp_path / "ws"
        self.ws.mkdir(exist_ok=True)
        self.state = tmp_path / "state"
        env = {k: v for k, v in os.environ.items() if k != "TILBURG_AI_API_KEY"}
        env.update({"TAI_API_BASE": api, "XDG_STATE_HOME": str(self.state), "PYTHONUNBUFFERED": "1"})
        if key:
            env["TILBURG_AI_API_KEY"] = key
        env.update(extra_env or {})
        self.p = subprocess.Popen([sys.executable, TAI, "--serve", *args], cwd=self.ws, env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", bufsize=1)
        self.events = []
        self.cond = threading.Condition()
        self.replies = {}
        self.n = 0
        self.bad_lines = []
        self.approve = lambda params: {"decision": "yes"}
        self.ide = {}
        self.stderr = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=lambda: self.stderr.extend(self.p.stderr), daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                self.bad_lines.append(line)
                continue
            if msg.get("method") == "event":
                with self.cond:
                    self.events.append(msg["params"])
                    self.cond.notify_all()
            elif msg.get("method"):          # request from the server
                threading.Thread(target=self._answer, args=(msg,), daemon=True).start()
            else:
                with self.cond:
                    self.replies[msg["id"]] = msg
                    self.cond.notify_all()

    def _answer(self, msg):
        m, params = msg["method"], msg.get("params") or {}
        with self.cond:
            self.events.append({"type": "_request", "method": m, "params": params, "id": msg["id"]})
            self.cond.notify_all()
        if m == "approval":
            res = self.approve(params)
        elif m in self.ide:
            res = self.ide[m](params)
        else:
            res = None
        if res is not None:
            self.write({"id": msg["id"], "result": res})

    def write(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def call(self, method, params=None, timeout=10, ok=True):
        self.n += 1
        rid = self.n
        self.write({"id": rid, "method": method, "params": params or {}})
        with self.cond:
            if not self.cond.wait_for(lambda: rid in self.replies, timeout):
                raise TimeoutError(f"no reply to {method}")
            r = self.replies.pop(rid)
        if ok:
            assert "error" not in r, r
            return r["result"]
        return r

    def wait(self, type_, pred=lambda e: True, timeout=10, start=0):
        with self.cond:
            found = []

            def look():
                found[:] = [e for e in self.events[start:] if e["type"] == type_ and pred(e)]
                return bool(found)
            if not self.cond.wait_for(look, timeout):
                raise TimeoutError(f"no {type_} event; got {[e['type'] for e in self.events]}")
            return found[0]

    def mark(self):
        with self.cond:
            return len(self.events)

    def of(self, type_, start=0):
        with self.cond:
            return [e for e in self.events[start:] if e["type"] == type_]

    def send(self, text, timeout=15, **kw):
        m = self.mark()
        r = self.call("send", {"text": text, **kw})
        end = self.wait("turn_end", timeout=timeout, start=m)
        return r, end, m

    def text_since(self, m):
        return "".join(e["delta"] for e in self.of("text", m))

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(8)
        except subprocess.TimeoutExpired:
            self.p.kill()
        assert not self.bad_lines, f"non-JSON on stdout: {self.bad_lines}"


@pytest.fixture
def client(api, tmp_path):
    clients = []

    def make(**kw):
        c = Client(api, tmp_path, **kw)
        clients.append(c)
        c.wait("ready")
        return c
    yield make
    for c in clients:
        c.close()


def api_log(api):
    import urllib.request
    with urllib.request.urlopen(api + "/_log") as r:
        return json.load(r)


def reset_log(api):
    import urllib.request
    urllib.request.urlopen(urllib.request.Request(api + "/_reset", data=b"{}", method="POST")).close()


def wait_until(pred, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False
