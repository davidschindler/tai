"""/review (the model reviews the changes in git with read-only tools) and the user's own
commands (Markdown files in .tai/commands/ or tai's commands folder)."""
import os
import subprocess
import sys

import pytest

from conftest import TAI, api_log, mock, reset_log

pytestmark = pytest.mark.skipif(not __import__("shutil").which("git"), reason="needs git")


def init(c, **p):
    return c.call("initialize", {"ide": True, **p})


def git(ws, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=ws,
                   check=True, capture_output=True)


def repo(ws):
    git(ws, "init", "-q", "-b", "main")
    (ws / "calc.py").write_text("def mean(xs):\n    return sum(xs) / len(xs)\n")
    git(ws, "add", ".")
    git(ws, "commit", "-q", "-m", "first")


def last_user(api):
    return api_log(api)[-1]["messages"][-1]["content"]


def tool_names(api):
    return {t["function"]["name"] for t in api_log(api)[-1].get("tools") or []}


# ------------------------------------------------------------------ /review

def test_review_of_uncommitted_changes(client, api):
    c = client()
    repo(c.ws)
    (c.ws / "calc.py").write_text("def mean(xs):\n    return sum(xs) / (len(xs) - 1)\n")
    (c.ws / "new.py").write_text("print('new file')\n")
    init(c)
    reset_log(api)
    m = c.mark()
    _, end, _ = c.send("/review", command="review", arg="")
    assert end["reason"] == "done"
    msg = last_user(api)
    assert "not committed yet" in msg and "(len(xs) - 1)" in msg and "+print('new file')" in msg
    assert "Do NOT change any files" in msg
    assert tool_names(api) == {"read_file", "list_files", "grep", "get_diagnostics"}
    assert "Reviewing 2 files, not committed" in [e["text"] for e in c.of("notice", m)][0]
    # shown as what the user typed; tools are back for the next message
    tr = c.call("transcript")["transcript"]
    assert [i["text"] for i in tr if i["type"] == "user"] == ["/review"]
    reset_log(api)
    c.send(mock({"text": "ok"}))
    assert "edit_file" in tool_names(api)


def test_review_since_a_branch_with_focus(client, api):
    c = client()
    repo(c.ws)
    git(c.ws, "checkout", "-q", "-b", "feature")
    (c.ws / "calc.py").write_text("def mean(xs):\n    return 0\n")
    git(c.ws, "commit", "-q", "-am", "change")
    init(c)
    reset_log(api)
    _, end, _ = c.send("/review main the edge cases", command="review", arg="main the edge cases")
    msg = last_user(api)
    assert "since main" in msg and "return 0" in msg and "focus on: the edge cases" in msg


def test_review_with_nothing_to_review(client):
    c = client()
    repo(c.ws)
    init(c)
    r = c.call("send", {"text": "/review", "command": "review", "arg": ""}, ok=False)
    assert "nothing to review" in r["error"]["message"]


def test_review_outside_a_repository(client):
    c = client()
    init(c)
    r = c.call("send", {"text": "/review", "command": "review"}, ok=False)
    assert "git repository" in r["error"]["message"]


# ------------------------------------------------------------------ custom commands

def test_custom_commands_from_the_project_and_the_user(client, api):
    c = client()
    proj = c.ws / ".tai" / "commands"
    proj.mkdir(parents=True)
    (proj / "referee.md").write_text("---\ndescription: Draft a referee report\n---\n"
                                     "Write a referee report on $ARGUMENTS. " + mock({"text": "REPORT"}))
    mine = c.state / "tai" / "commands"
    mine.mkdir(parents=True)
    (mine / "tidy.md").write_text("# Tidy the data\nClean the data set.")
    (mine / "referee.md").write_text("shadowed by the project's")
    (mine / "help.md").write_text("built-ins win")
    st = init(c)["state"]
    assert st["commands"] == [{"name": "referee", "description": "Draft a referee report", "scope": "project"},
                              {"name": "tidy", "description": "Tidy the data", "scope": "yours"}]
    reset_log(api)
    _, end, m = c.send("/referee paper.pdf", command="referee", arg="paper.pdf")
    assert "REPORT" in c.text_since(m)
    assert last_user(api).startswith("Write a referee report on paper.pdf.")
    tr = c.call("transcript")["transcript"]
    assert [i["text"] for i in tr if i["type"] == "user"] == ["/referee paper.pdf"]
    reset_log(api)
    c.send("/tidy wave 2", command="tidy", arg="wave 2")
    assert last_user(api) == "# Tidy the data\nClean the data set.\n\nwave 2"
    assert c.call("send", {"text": "/nope", "command": "nope"}, ok=False)["error"]["message"] == "unknown command /nope"


def test_terminal_one_shot_review_and_custom_command(api, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    repo(ws)
    (ws / "calc.py").write_text("def mean(xs):\n    return 1\n")
    (ws / ".tai" / "commands").mkdir(parents=True)
    (ws / ".tai" / "commands" / "hello.md").write_text("Say hi to $ARGUMENTS. " + mock({"text": "HI THERE"}))
    env = {k: v for k, v in os.environ.items() if k != "TILBURG_AI_API_KEY"}
    env.update({"TAI_API_BASE": api, "TILBURG_AI_API_KEY": "test-key", "PYTHONIOENCODING": "utf-8",
                "XDG_STATE_HOME": str(tmp_path / "state")})
    run = lambda *a: subprocess.run([sys.executable, TAI, "--raw", *a], input="", capture_output=True,
                                    text=True, encoding="utf-8", env=env, cwd=ws, timeout=60)
    reset_log(api)
    r = run("/review")
    assert r.returncode == 0, r.stderr
    assert "return 1" in last_user(api) and tool_names(api) == {"read_file", "list_files", "grep"}
    r = run("/hello", "Ana")
    assert r.returncode == 0, r.stderr
    assert "HI THERE" in r.stdout and last_user(api).startswith("Say hi to Ana.")
