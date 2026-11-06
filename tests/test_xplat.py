"""Behaviour that differs between operating systems: encodings, line endings, the shell tool,
the state folder and the stored API key. Runs on Linux, macOS and Windows (CI)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import SHELL, TAI, WINDOWS, mock


def init(c, **p):
    return c.call("initialize", {"ide": True, **p})


def edit(path, old, new):
    return {"name": "edit_file", "args": {"path": path, "old_string": old, "new_string": new}}


def test_edit_keeps_crlf_line_endings(client):
    c = client()
    init(c, mode="auto")
    f = c.ws / "win.txt"
    f.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    c.send(mock({"tools": [edit("win.txt", "two\nthree", "TWO\nthree")]}, {"text": "k"}))
    assert f.read_bytes() == b"one\r\nTWO\r\nthree\r\n"


def test_edit_keeps_lf_line_endings(client):
    c = client()
    init(c, mode="auto")
    f = c.ws / "unix.txt"
    f.write_bytes(b"a\nb\n")
    c.send(mock({"tools": [edit("unix.txt", "a\nb", "a\nB\nc")]}, {"text": "k"}))
    assert f.read_bytes() == b"a\nB\nc\n"


def test_edit_keeps_legacy_windows_encoding(client):
    c = client()
    init(c, mode="auto")
    f = c.ws / "stata.do"
    f.write_bytes("* Gebühren in €\r\ngen x = 1\r\n".encode("cp1252"))
    c.send(mock({"tools": [edit("stata.do", "gen x = 1", "gen x = 2 // naïve")]}, {"text": "k"}))
    assert f.read_bytes() == "* Gebühren in €\r\ngen x = 2 // naïve\r\n".encode("cp1252")


def test_edit_refuses_characters_the_encoding_cannot_store(client):
    c = client()
    init(c, mode="auto")
    f = c.ws / "old.txt"
    before = "caf\xe9\n".encode("cp1252")
    f.write_bytes(before)
    _, end, m = c.send(mock({"tools": [edit("old.txt", "café", "café 日本")]}, {"text": "k"}))
    te = c.wait("tool_end", start=m)
    assert te["error"] and "cp1252" in te["output"]
    assert f.read_bytes() == before


def test_utf8_bom_is_kept(client):
    c = client()
    init(c, mode="auto")
    f = c.ws / "bom.csv"
    f.write_bytes(b"\xef\xbb\xbfa;b\r\n1;2\r\n")
    c.send(mock({"tools": [edit("bom.csv", "1;2", "1;3")]}, {"text": "k"}))
    assert f.read_bytes() == b"\xef\xbb\xbfa;b\r\n1;3\r\n"


def test_new_files_are_utf8(client):
    c = client()
    init(c, mode="auto")
    c.send(mock({"tools": [{"name": "write_file", "args": {"path": "n.md", "content": "Zoë — 日本\n"}}]},
                {"text": "k"}))
    assert (c.ws / "n.md").read_bytes() == "Zoë — 日本\n".encode("utf-8")


def test_rewind_restores_exact_bytes(client):
    c = client()
    init(c, mode="auto")
    f = c.ws / "r.txt"
    f.write_bytes(b"v0\r\nend\r\n")
    c.send(mock({"tools": [edit("r.txt", "v0", "v1")]}, {"text": "one"}))
    assert f.read_bytes() == b"v1\r\nend\r\n"
    first = [i for i in c.call("transcript")["transcript"] if i["type"] == "user"][0]["index"]
    c.call("rewind", {"index": first})
    assert f.read_bytes() == b"v0\r\nend\r\n"


def test_non_ascii_message_over_raw_utf8_stdin(client):
    """VS Code sends JSON.stringify output: raw UTF-8, not \\u escapes."""
    c = client()
    init(c)
    text = "Grüße ☕ 日本 " + mock({"text": "ok"})
    m = c.mark()
    c.n += 1
    line = json.dumps({"id": c.n, "method": "send", "params": {"text": text}}, ensure_ascii=False)
    c.p.stdin.write(line + "\n")          # the pipe is UTF-8 (see conftest)
    c.p.stdin.flush()
    c.wait("turn_end", start=m, timeout=15)
    users = [i for i in c.call("transcript")["transcript"] if i["type"] == "user"]
    assert users[0]["text"].startswith("Grüße ☕ 日本")


def test_shell_tool_matches_the_os(client):
    c = client()
    r = init(c, mode="auto")
    _, end, m = c.send(mock({"tools": [{"name": SHELL, "args": {"command": "echo héllo-wörld"}}]}, {"text": "k"}))
    ts = c.wait("tool_start", start=m)
    assert ts["label"] == ("PowerShell" if WINDOWS else "Bash")
    te = c.wait("tool_end", start=m)
    assert "héllo-wörld" in te["output"] and "[exit code 0]" in te["output"]


def test_shell_approval_names_the_shell(client):
    c = client()
    init(c)
    _, end, m = c.send(mock({"tools": [{"name": SHELL, "args": {"command": "echo x"}}]}, {"text": "k"}))
    req = [x for x in c.of("_request", m) if x["method"] == "approval"][0]["params"]
    assert req["detail"]["shell"] == ("PowerShell" if WINDOWS else "Bash")
    assert req["title"] == ("PowerShell command" if WINDOWS else "Bash command")


def test_stop_kills_child_processes(client, tmp_path):
    """Stop must end the whole process tree, not only the shell."""
    c = client()
    init(c, mode="auto")
    marker = tmp_path / "late.txt"
    py = sys.executable
    child = f"import time; time.sleep(4); open(r'{marker}', 'w').write('x')"
    cmd = (f"& '{py}' -c \"{child}\"" if WINDOWS else f"'{py}' -c \"{child}\"")
    m = c.mark()
    c.call("send", {"text": mock({"tools": [{"name": SHELL, "args": {"command": cmd}}]}, {"text": "x"})})
    c.wait("tool_start", start=m)
    import time
    time.sleep(1.5)
    c.call("cancel")
    c.wait("turn_end", start=m, timeout=15)
    time.sleep(5)
    assert not marker.exists(), "the child process survived Stop"


def test_stored_key_is_used_and_reported(api, tmp_path):
    from conftest import Client
    c = Client(api, tmp_path, key=None)
    try:
        assert c.wait("ready")["hasKey"] is False
    finally:
        c.close()
    keyfile = tmp_path / "state" / "tai" / "api_key"
    keyfile.parent.mkdir(parents=True, exist_ok=True)
    keyfile.write_text("test-key\n")
    c = Client(api, tmp_path, key=None)
    try:
        assert c.wait("ready")["hasKey"] is True
        init(c)
        _, end, m = c.send(mock({"text": "answered with the stored key"}))
        assert end["reason"] == "done" and "stored key" in c.text_since(m)
    finally:
        c.close()


@pytest.mark.skipif(WINDOWS, reason="getpass reads the Windows console, not a pipe")
def test_set_key_writes_a_private_file(tmp_path):
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path)}
    env.pop("TILBURG_AI_API_KEY", None)
    r = subprocess.run([sys.executable, TAI, "--set-key"], input="  abc123  \n", capture_output=True,
                       text=True, env=env, timeout=30, start_new_session=True)
    assert r.returncode == 0, r.stderr
    f = tmp_path / "tai" / "api_key"
    assert f.read_text() == "abc123\n"
    assert (f.stat().st_mode & 0o777) == 0o600


def test_state_dir_default_per_os(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "XDG_STATE_HOME"}
    env["HOME"] = env["USERPROFILE"] = str(tmp_path)
    env["LOCALAPPDATA"] = str(tmp_path / "AppData" / "Local")
    code = ("import importlib.util as u; s=u.spec_from_file_location('t', r'%s'); m=u.module_from_spec(s); "
            "s.loader.exec_module(m); print(m.STATE_DIR)") % TAI
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env).stdout.strip()
    want = (tmp_path / "AppData" / "Local" / "tai") if WINDOWS else (tmp_path / ".local" / "state" / "tai")
    assert Path(out) == want
