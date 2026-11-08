"""Plan mode (tai reads and plans; nothing changes until the user approves the plan),
/remember with the personal TAI.md, and notifications."""
import importlib.util
import json
import os
import sys

from conftest import SHELL, TAI, api_log, mock, reset_log

spec = importlib.util.spec_from_file_location("tai_plan", TAI)
tai = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tai)


def init(c, **p):
    return c.call("initialize", {"ide": True, **p})


def tool_names(req):
    return {t["function"]["name"] for t in req.get("tools") or []}


def approvals(c, m):
    return [x["params"] for x in c.of("_request", m) if x["method"] == "approval"]


PLAN = {"name": "present_plan", "args": {"plan": "1. Create `a.txt`\n2. Check it"}}
WRITE = {"name": "write_file", "args": {"path": "a.txt", "content": "x\n"}}


# ------------------------------------------------------------------ plan mode

def test_plan_mode_reads_then_asks_to_approve_the_plan(client, api):
    c = client()
    r = init(c, mode="plan")
    assert "plan" in r["modes"] and "approve" in r["modeInfo"]["plan"]
    reset_log(api)
    m = c.mark()
    c.approve = lambda p: {"decision": "yes"}
    _, end, _ = c.send(mock({"tools": [WRITE]}, {"tools": [PLAN]}, {"tools": [WRITE]}, {"text": "done"}))
    assert end["reason"] == "done"
    outs = c.of("tool_end", m)
    # an edit before the plan is approved is refused, without asking
    assert outs[0]["error"] and "plan mode" in outs[0]["output"]
    assert outs[1]["summary"].startswith("Plan approved") and not outs[1]["error"]
    assert not outs[2]["error"] and (c.ws / "a.txt").read_text() == "x\n"
    req = approvals(c, m)
    assert [q["kind"] for q in req] == ["plan", "edit"]           # the edit asks again (manual mode)
    assert req[0]["detail"] == {"type": "plan", "plan": PLAN["args"]["plan"]}
    assert req[0]["labels"][0].startswith("Yes, start")
    # the model saw only reading tools and present_plan until the plan was approved
    log = api_log(api)
    first, last = tool_names(log[0]), tool_names(log[-1])
    assert "present_plan" in first and not first & {"write_file", "edit_file", SHELL, "edit_notebook"}
    assert "write_file" in last and "present_plan" not in last
    assert "Plan mode is on" in json.dumps(log[0]["messages"])
    # the editor is told that the mode changed
    states = [e["state"]["mode"] for e in c.of("state", m)]
    assert states and states[-1] == "manual"
    # the note is not shown as the user's message
    tr = c.call("transcript")["transcript"]
    assert [i["type"] for i in tr if i["type"] == "user"] == ["user"]


def test_plan_approved_with_accept_edits(client, api):
    c = client()
    init(c, mode="plan")
    c.approve = lambda p: {"decision": "always"}
    m = c.mark()
    c.send(mock({"tools": [PLAN]}, {"tools": [WRITE]}, {"text": "done"}))
    assert [q["kind"] for q in approvals(c, m)] == ["plan"]       # the edit didn't ask
    assert c.call("state")["mode"] == "accept-edits"
    assert (c.ws / "a.txt").exists()


def test_plan_not_approved_stays_in_plan_mode(client, api):
    c = client()
    init(c, mode="plan")
    c.approve = lambda p: {"decision": "no", "feedback": "use b.txt instead"}
    reset_log(api)
    m = c.mark()
    _, end, _ = c.send(mock({"tools": [PLAN]}, {"text": "revised"}))
    out = c.of("tool_end", m)[0]
    assert out["summary"] == "Plan not approved — use b.txt instead" and not out["error"]
    assert c.call("state")["mode"] == "plan"
    tool_msg = [x for x in api_log(api)[-1]["messages"] if x["role"] == "tool"][-1]
    assert "use b.txt instead" in tool_msg["content"] and "Stay in plan mode" in tool_msg["content"]
    assert "present_plan" in tool_names(api_log(api)[-1])


def test_sub_agents_in_plan_mode_only_read(client, api):
    c = client()
    init(c, mode="plan")
    reset_log(api)
    c.send(mock({"tools": [{"name": "task", "args": {"description": "look", "prompt": mock({"text": "R"})}}]},
                {"text": "k"}))
    sub = [r for r in api_log(api) if "SUB-AGENT" in r["messages"][0]["content"]]
    assert sub and not tool_names(sub[0]) & {"write_file", "edit_file", SHELL, "present_plan"}
    assert "read_file" in tool_names(sub[0])


def test_present_plan_outside_plan_mode(client):
    c = client()
    init(c, mode="auto")
    m = c.mark()
    c.send(mock({"tools": [PLAN]}, {"text": "k"}))
    assert c.of("tool_end", m)[0]["summary"] == "Plan (plan mode is off)" and not approvals(c, m)


# ------------------------------------------------------------------ /remember

def same(a, b):
    from pathlib import Path
    return Path(a).resolve() == Path(b).resolve()


def test_remember_in_the_editor(client, api):
    c = client()
    init(c)
    r = c.call("remember", {"text": "Use robust standard errors.", "scope": "personal"})
    personal = c.state / "tai" / "TAI.md"
    assert same(r["path"], personal)
    assert personal.read_text() == "# My instructions for tai (all projects)\n\n- Use robust standard errors.\n"
    r = c.call("remember", {"text": "Data are in data/raw.", "scope": "project"})
    assert same(r["path"], c.ws / "TAI.md") and (c.ws / "TAI.md").read_text() == "- Data are in data/raw.\n"
    reset_log(api)
    c.send(mock({"text": "k"}))
    system = api_log(api)[-1]["messages"][0]["content"]
    assert "Use robust standard errors." in system and "Data are in data/raw." in system
    # no text: the files, created for the editor to open
    (c.ws / "TAI.md").unlink()
    (c.ws / "AGENTS.md").write_text("# Agents\n")
    r = c.call("remember", {"scope": "project", "create": True})
    assert same(r["path"], c.ws / "AGENTS.md") and same(r["personal"], personal)
    c.call("remember", {"text": "Second line", "scope": "project"})
    assert (c.ws / "AGENTS.md").read_text() == "# Agents\n- Second line\n"
    r = c.call("remember", {"scope": "personal"})
    assert same(r["path"], personal)


def test_remember_in_the_terminal(api, tmp_path):
    import pytest
    pexpect = pytest.importorskip("pexpect")
    if os.name == "nt":
        pytest.skip("pexpect needs a POSIX terminal")
    ws = tmp_path / "ws"
    ws.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "TILBURG_AI_API_KEY"}
    env.update({"TAI_API_BASE": api, "TILBURG_AI_API_KEY": "test-key",
                "XDG_STATE_HOME": str(tmp_path / "state"), "TERM": "xterm-256color"})
    p = pexpect.spawn(sys.executable, [TAI], cwd=str(ws), env=env, dimensions=(40, 140), encoding="utf-8",
                      timeout=15)
    p.expect("Welcome")
    p.expect("❯ ")
    p.sendline("/remember Always answer in English.")
    p.expect("for all your projects")
    p.expect("❯ ")
    p.sendline("/remember project Run make test after changes.")
    p.expect("remembered in TAI.md")
    p.expect("❯ ")
    p.sendline("/remember")
    p.expect("Always answer in English")
    p.expect("Run make test after changes")
    p.expect("❯ ")
    reset_log(api)
    p.sendline(mock({"text": "fine"}))
    p.expect(r"out · \d+s")
    p.expect("❯ ")
    system = api_log(api)[-1]["messages"][0]["content"]
    assert "Always answer in English." in system and "Run make test after changes." in system
    assert (tmp_path / "state" / "tai" / "TAI.md").read_text().endswith("- Always answer in English.\n")
    p.sendline("/exit")
    p.expect(pexpect.EOF)


def test_terminal_plan_mode(api, tmp_path):
    import pytest
    pexpect = pytest.importorskip("pexpect")
    if os.name == "nt":
        pytest.skip("pexpect needs a POSIX terminal")
    ws = tmp_path / "ws"
    ws.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "TILBURG_AI_API_KEY"}
    env.update({"TAI_API_BASE": api, "TILBURG_AI_API_KEY": "test-key",
                "XDG_STATE_HOME": str(tmp_path / "state"), "TERM": "xterm-256color"})
    p = pexpect.spawn(sys.executable, [TAI, "--plan"], cwd=str(ws), env=env, dimensions=(40, 140),
                      encoding="utf-8", timeout=15)
    p.expect("Welcome")
    p.expect("❯ ")
    p.sendline("/mode")
    p.expect("mode: plan")
    p.expect("❯ ")
    p.sendline(mock({"tools": [PLAN]}, {"tools": [WRITE]}, {"text": "all done"}))
    p.expect("Start working on this plan")
    p.expect("Yes, and let file edits run without asking")
    p.expect("❯ ")
    p.sendline("2")
    p.expect("Plan approved · accept-edits")
    p.expect("all done")
    p.expect(r"out · \d+s")
    p.expect("❯ ")
    assert (ws / "a.txt").read_text() == "x\n"
    p.sendline("/mode")
    p.expect("mode: accept-edits")
    p.sendline("/exit")
    p.expect(pexpect.EOF)


# ------------------------------------------------------------------ notifications

def test_notify_setting(tmp_path, monkeypatch):
    s = tai.Settings(tmp_path / "settings.json")
    monkeypatch.setattr(tai, "SETTINGS", s)
    assert tai.notify_setting() == "bell"
    (tmp_path / "settings.json").write_text(json.dumps({"notify": "desktop"}))
    assert tai.notify_setting() == "desktop"
    (tmp_path / "settings.json").write_text(json.dumps({"notify": "loud"}))
    assert tai.notify_setting() == "bell"


def test_desktop_notification_command(monkeypatch):
    calls = []

    class P:
        def __init__(self, argv, **kw):
            calls.append((argv, kw.get("env")))
    monkeypatch.setattr(tai.subprocess, "Popen", P)
    monkeypatch.setattr(tai.shutil, "which", lambda x: "/usr/bin/notify-send")
    shown = tai.desktop_notify("tai", 'done "quoted"')
    argv, env = calls[-1]
    if sys.platform == "darwin":
        assert argv[0] == "osascript" and '"done \\"quoted\\""' in argv[2]
    elif os.name == "nt":
        assert argv[0] == "powershell.exe" and env["TAI_NOTIFY_BODY"] == 'done "quoted"'
    else:
        assert argv == ["/usr/bin/notify-send", "-a", "tai", "tai", 'done "quoted"']
    assert shown


def test_notify_after_a_long_turn_only(monkeypatch):
    seen = []
    monkeypatch.setattr(tai, "notify_user", seen.append)
    tai.TURN_CLOCK["start"] = tai.time.time() - 5
    tai.notify_done()
    tai.TURN_CLOCK["start"] = tai.time.time() - 60
    tai.notify_waiting()
    tai.notify_waiting()                       # not again right away
    tai.notify_done()
    assert seen == ["tai is waiting for your approval"]
    tai.TURN_CLOCK["start"] = tai.time.time() - 60
    tai.notify_done()
    assert seen[-1] == "tai has finished" and tai.TURN_CLOCK["start"] is None

