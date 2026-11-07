"""The sandbox of auto mode: commands can write only in the workspace and the temp folder and
can't open network connections; outside_sandbox asks first; edits outside the workspace ask."""
import importlib.util
import json
import shutil
import sys
import uuid
from pathlib import Path

import pytest

from conftest import SHELL, TAI, WINDOWS, mock

spec = importlib.util.spec_from_file_location("tai_sbx", TAI)
tai = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tai)

HAVE = tai.SANDBOX.kind is not None
needs_sandbox = pytest.mark.skipif(not HAVE, reason="no sandbox on this system")


def init(c, **p):
    return c.call("initialize", {"ide": True, **p})


def sh(cmd, **kw):
    return {"name": SHELL, "args": {"command": cmd, **kw}}


def approvals(c, m):
    return [x["params"] for x in c.of("_request", m) if x["method"] == "approval"]


def py(code):
    exe = sys.executable
    return f"& '{exe}' -c \"{code}\"" if WINDOWS else f"'{exe}' -c \"{code}\""


@pytest.fixture
def outside():
    """A folder outside the workspace and the temp folder (next to the tests)."""
    d = Path(__file__).resolve().parent / f".sbx-{uuid.uuid4().hex[:8]}"
    d.mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def settings(c, data):
    (c.state / "tai").mkdir(parents=True, exist_ok=True)
    (c.state / "tai" / "settings.json").write_text(json.dumps(data))


@needs_sandbox
def test_auto_mode_writes_only_in_the_workspace(client, outside):
    c = client()
    init(c, mode="auto")
    target = outside / "x.txt"
    _, end, m = c.send(mock({"tools": [sh(f"echo in > inside.txt; echo out > '{target}'")]}, {"text": "k"}))
    out = c.wait("tool_end", start=m)
    assert (c.ws / "inside.txt").read_text().strip() == "in"
    assert not target.exists()
    assert "sandbox" in out["output"] and "outside_sandbox" in out["output"]
    assert not approvals(c, m)


@needs_sandbox
def test_temp_folder_is_writable(client):
    c = client()
    init(c, mode="auto")
    code = "import tempfile; f = tempfile.NamedTemporaryFile(delete=False); f.write(b'ok'); print('TMP', f.name)"
    _, end, m = c.send(mock({"tools": [sh(py(code))]}, {"text": "k"}))
    out = c.wait("tool_end", start=m)
    assert "TMP" in out["output"] and not out["error"]


@needs_sandbox
def test_network_is_blocked(client, api):
    if not tai.SANDBOX.network_blocked():
        pytest.skip("this kernel's Landlock can't block network connections (needs Linux 6.7)")
    c = client()
    init(c, mode="auto")
    port = api.split(":")[2].split("/")[0]
    code = (f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=5); print('CONN' + 'ECTED')")
    _, end, m = c.send(mock({"tools": [sh(py(code))]}, {"text": "k"}))
    out = c.wait("tool_end", start=m)
    assert "CONNECTED" not in out["output"]


@needs_sandbox
def test_outside_sandbox_asks_in_auto_mode(client, outside):
    c = client()
    init(c, mode="auto")
    target = outside / "y.txt"
    _, end, m = c.send(mock({"tools": [sh(f"echo out > '{target}'", outside_sandbox=True)]}, {"text": "k"}))
    req = approvals(c, m)
    assert len(req) == 1 and "outside the sandbox" in req[0]["question"]
    assert req[0]["detail"]["outside_sandbox"] is True
    assert target.read_text().strip() == "out"


@needs_sandbox
def test_background_jobs_are_sandboxed(client, outside):
    c = client()
    init(c, mode="auto")
    target = outside / "job.txt"
    _, end, m = c.send(mock({"tools": [sh(f"echo out > '{target}'", run_in_background=True)]},
                            {"tools": [{"name": "job_output", "args": {"job_id": "b1", "wait": 20}}]},
                            {"text": "k"}), timeout=30)
    outs = c.of("tool_end", m)
    assert not target.exists()
    assert "sandbox" in outs[1]["output"]


@needs_sandbox
def test_sandbox_can_be_turned_off(client, outside):
    c = client()
    settings(c, {"sandbox": {"enabled": False}})
    init(c, mode="auto")
    target = outside / "z.txt"
    c.send(mock({"tools": [sh(f"echo out > '{target}'")]}, {"text": "k"}))
    assert target.read_text().strip() == "out"
    assert "off" in c.call("permissions")["sandbox"]


@needs_sandbox
def test_extra_writable_folders(client, outside):
    c = client()
    settings(c, {"sandbox": {"writable": [str(outside)]}})
    init(c, mode="auto")
    c.send(mock({"tools": [sh(f"echo ok > '{outside / 'w.txt'}'")]}, {"text": "k"}))
    assert (outside / "w.txt").read_text().strip() == "ok"


@needs_sandbox
def test_manual_mode_runs_approved_commands_without_the_sandbox(client, outside):
    c = client()
    init(c)
    target = outside / "m.txt"
    _, end, m = c.send(mock({"tools": [sh(f"echo out > '{target}'")]}, {"text": "k"}))
    assert len(approvals(c, m)) == 1
    assert target.read_text().strip() == "out"


def test_edits_outside_the_workspace_ask_in_auto_mode(client, outside):
    c = client()
    init(c, mode="auto")
    c.approve = lambda p: {"decision": "no"}
    write = lambda path: {"name": "write_file", "args": {"path": str(path), "content": "x\n"}}
    _, end, m = c.send(mock({"tools": [write(c.ws / "in.txt"), write(outside / "out.txt")]}, {"text": "k"}))
    req = approvals(c, m)
    assert len(req) == 1 and req[0]["detail"]["path"] == str((outside / "out.txt").resolve())
    assert (c.ws / "in.txt").exists() and not (outside / "out.txt").exists()


def test_permissions_report_the_sandbox(client):
    c = client()
    init(c)
    text = c.call("permissions")["sandbox"]
    assert text.startswith("sandbox: ")
    assert ("on (" in text) == HAVE


def test_mode_info_mentions_the_sandbox(client):
    c = client()
    r = init(c)
    assert ("sandbox" in r["modeInfo"]["auto"]) == HAVE
