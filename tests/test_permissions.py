"""Saved permissions (settings.json): "don't ask again" for a command in this folder or for a
site lasts beyond the session; deny rules block even in auto mode."""
import importlib.util
import json

import pytest

from conftest import SHELL, TAI, WINDOWS, mock

spec = importlib.util.spec_from_file_location("tai_mod", TAI)
tai = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tai)


def init(c, **p):
    return c.call("initialize", {"ide": True, **p})


def sh(cmd):
    return {"name": SHELL, "args": {"command": cmd}}


def approvals(c, m):
    return [x["params"] for x in c.of("_request", m) if x["method"] == "approval"]


def settings(c):
    return json.loads((c.state / "tai" / "settings.json").read_text())


# ------------------------------------------------------------------ rule logic

@pytest.mark.parametrize("cmd, rule", [
    ("git status -s", "git status"),
    ("pytest tests -q", "pytest"),
    ("python run.py --fast", "python run.py"),
    ("python -m pytest -x", "python -m pytest"),
    ("npm test", "npm test"),
    ("/usr/bin/ls -la", "ls"),
    ("python -c 'import os'", None),       # would allow any Python code
    ("rm -rf build", None),
    ("sudo apt install x", None),
    ("git status && rm -rf ~", None),       # compound: no rule
    ("cat a > b", None),
    ("echo $HOME", None),
    ("git -C x push", None),
])
def test_command_rule(cmd, rule):
    assert tai.command_rule(cmd) == rule


def test_matching():
    assert tai.command_matches("git status -s", "git status")
    assert not tai.command_matches("git statusx", "git status")
    assert not tai.command_matches("git push", "git status")
    assert tai.command_matches("pytest tests/test_a.py -q", "pytest *")
    parts = list(tai.command_parts("echo hi && sudo git push origin; ls | grep x"))
    assert any(tai.command_matches(p, "git push") for p in parts)
    assert tai.site_matches("docs.python.org", "*.python.org")
    assert not tai.site_matches("python.org.evil.com", "*.python.org")


# ------------------------------------------------------------------ in the editor (tai --serve)

def test_always_saves_a_command_rule_for_this_folder(client):
    c = client()
    init(c)
    c.approve = lambda p: {"decision": "always"}
    _, end, m = c.send(mock({"tools": [sh("echo first")]}, {"text": "k"}))
    req = approvals(c, m)
    assert len(req) == 1 and req[0]["always"] == "for `echo` commands in this folder"
    s = settings(c)
    (folder, rules), = s["folders"].items()
    assert rules["allow"]["commands"] == ["echo"]
    # a new session (another process) in the same folder: no question for echo
    c2 = client()
    init(c2)
    c2.approve = lambda p: {"decision": "no"}
    _, end, m = c2.send(mock({"tools": [sh("echo second")]}, {"text": "k"}))
    assert not approvals(c2, m)
    out = c2.wait("tool_end", start=m)
    assert "second" in out["output"] and not out["error"]
    # ... but a compound command still asks
    _, end, m = c2.send(mock({"tools": [sh("echo a; echo b")]}, {"text": "k"}))
    assert len(approvals(c2, m)) == 1


def test_rules_of_another_folder_do_not_apply(client, tmp_path):
    c = client()
    (c.state / "tai").mkdir(parents=True, exist_ok=True)
    (c.state / "tai" / "settings.json").write_text(json.dumps(
        {"folders": {str(tmp_path / "elsewhere"): {"allow": {"commands": ["echo"]}}}}))
    init(c)
    c.approve = lambda p: {"decision": "no"}
    _, end, m = c.send(mock({"tools": [sh("echo x")]}, {"text": "k"}))
    assert len(approvals(c, m)) == 1


def test_compound_command_offers_a_session_wide_choice(client):
    c = client()
    init(c)
    c.approve = lambda p: {"decision": "always"}
    _, end, m = c.send(mock({"tools": [sh("echo a; echo b")]}, {"tools": [sh("echo c; echo d")]}, {"text": "k"}))
    req = approvals(c, m)
    assert len(req) == 1 and req[0]["always"] == "for any command this session"
    assert not (c.state / "tai" / "settings.json").exists()


def test_deny_rule_blocks_even_in_auto_mode(client):
    c = client()
    (c.state / "tai").mkdir(parents=True, exist_ok=True)
    (c.state / "tai" / "settings.json").write_text(json.dumps({"deny": {"commands": ["git push"]}}))
    init(c, mode="auto")
    marker = c.ws / "ran.txt"
    _, end, m = c.send(mock({"tools": [sh("echo x > ran.txt; git push origin main")]}, {"text": "k"}))
    out = c.wait("tool_end", start=m)
    assert out["error"] and "Blocked" in out["output"] and "git push" in out["output"]
    assert not marker.exists() and not approvals(c, m)


def test_site_rule_is_saved_and_deny_wins(client):
    c = client()
    init(c)
    c.approve = lambda p: {"decision": "always"}
    fetch = lambda u: {"name": "web_fetch", "args": {"url": u}}
    c.send(mock({"tools": [fetch("https://example.invalid/a")]}, {"text": "k"}))
    assert settings(c)["allow"]["sites"] == ["example.invalid"]
    c2 = client()
    init(c2)
    _, end, m = c2.send(mock({"tools": [fetch("https://example.invalid/b")]}, {"text": "k"}))
    assert not approvals(c2, m)
    s = settings(c2)
    s.setdefault("deny", {})["sites"] = ["*.invalid"]
    (c2.state / "tai" / "settings.json").write_text(json.dumps(s))
    _, end, m = c2.send(mock({"tools": [fetch("https://example.invalid/c")]}, {"text": "k"}))
    assert "Blocked" in c2.wait("tool_end", start=m)["output"]


def test_broken_settings_file_is_ignored_with_a_warning(client):
    c = client()
    (c.state / "tai").mkdir(parents=True, exist_ok=True)
    (c.state / "tai" / "settings.json").write_text("{ not json")
    init(c)
    c.approve = lambda p: {"decision": "always"}
    _, end, m = c.send(mock({"tools": [sh("echo x")]}, {"text": "k"}))
    assert len(approvals(c, m)) == 1 and end["reason"] == "done"
    assert "settings.json" in "".join(c.stderr)
    assert (c.state / "tai" / "settings.json").read_text() == "{ not json"     # not overwritten


def test_permissions_method_creates_the_file(client):
    c = client()
    init(c)
    r = c.call("permissions")
    data = json.loads(open(r["path"], encoding="utf-8").read())
    assert data["allow"] == {"commands": [], "sites": []} and r["rules"] == []


@pytest.mark.skipif(not WINDOWS, reason="Windows command names")
def test_windows_exe_names_match():
    assert tai.command_matches("Git.exe status", "git status")
