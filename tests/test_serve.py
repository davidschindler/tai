"""Protocol tests for `tai --serve` against the mock API."""
import json
import os
import subprocess
import sys
import time

from conftest import SHELL, TAI, WINDOWS, api_log, mock, reset_log, wait_until


def init(c, **p):
    return c.call("initialize", {"ide": True, **p})


# ---------------------------------------------------------------- basics

def test_initialize(client):
    c = client()
    r = init(c)
    assert r["protocol"] == 1 and r["version"]
    assert r["state"]["mode"] == "manual" and r["state"]["tools"] is True
    assert r["apiKey"] is True
    assert r["cwd"] == str(c.ws)


def test_echo_turn_and_session_saved(client):
    c = client()
    init(c)
    r, end, m = c.send("hello there")
    assert r == {"queued": False}
    u = c.wait("user", start=m)
    assert u["index"] == 1 and u["text"] == "hello there"
    assert end["reason"] == "done" and end["error"] is None
    assert c.text_since(m) == "Echo: hello there"
    sess = c.wait("session", start=m)["session"]
    assert sess["title"] == "hello there" and sess["messages"] == 1
    f = c.state / "tai" / "sessions" / f"{sess['id']}.json"
    assert f.exists()
    assert json.loads((c.state / "tai" / "last.json").read_text())["messages"][-1]["content"] == "Echo: hello there"
    t = c.call("transcript")["transcript"]
    assert [i["type"] for i in t] == ["user", "assistant"]


def test_unknown_method_and_bad_json(client):
    c = client()
    r = c.call("nope", ok=False)
    assert "unknown method" in r["error"]["message"]
    m = c.mark()
    c.p.stdin.write("{not json\n")
    c.p.stdin.flush()
    c.wait("notice", start=m)
    assert c.call("ping") == {"pong": True}


def test_empty_message_rejected(client):
    c = client()
    init(c)
    r = c.call("send", {"text": "   "}, ok=False)
    assert "empty" in r["error"]["message"]


# ---------------------------------------------------------------- tools & approvals

def test_read_tool_runs_without_approval(client):
    c = client()
    init(c)
    (c.ws / "a.txt").write_text("alpha\nbeta\n")
    _, end, m = c.send(mock({"tools": [{"name": "read_file", "args": {"path": "a.txt"}}]},
                            {"text": "read it"}))
    assert end["reason"] == "done"
    s, e = c.wait("tool_start", start=m), c.wait("tool_end", start=m)
    assert s["label"] == "Read" and s["shown"] == "a.txt"
    assert "alpha" in e["output"] and not e["error"]
    assert not [x for x in c.of("_request", m) if x["method"] == "approval"]
    assert c.text_since(m) == "read it"


def test_edit_approval_yes_gives_diff(client):
    c = client()
    init(c)
    f = c.ws / "f.py"
    f.write_text("x = 1\n")
    _, end, m = c.send(mock({"tools": [{"name": "edit_file", "args": {
        "path": "f.py", "old_string": "x = 1", "new_string": "x = 2"}}]}, {"text": "ok"}))
    req = [x for x in c.of("_request", m) if x["method"] == "approval"][0]["params"]
    assert req["kind"] == "edit" and req["detail"]["type"] == "edit"
    assert req["detail"]["old"] == "x = 1\n" and req["detail"]["new"] == "x = 2\n"
    assert req["always"] == "for file edits this session"
    assert f.read_text() == "x = 2\n"
    te = c.wait("tool_end", start=m)
    assert te["diff"]["old"] == "x = 1\n" and te["diff"]["new"] == "x = 2\n"
    assert c.wait("file_changed", start=m)["path"] == str(f)


def test_edit_denied_with_feedback(client, api):
    c = client()
    init(c)
    f = c.ws / "f.py"
    f.write_text("x = 1\n")
    c.approve = lambda p: {"decision": "no", "feedback": "use tabs"}
    reset_log(api)
    _, end, m = c.send(mock({"tools": [{"name": "edit_file", "args": {
        "path": "f.py", "old_string": "x = 1", "new_string": "x = 2"}}]}, {"text": "fine"}))
    assert f.read_text() == "x = 1\n"
    te = c.wait("tool_end", start=m)
    assert te["error"] and "use tabs" in te["summary"]
    tool_msgs = [mm for mm in api_log(api)[-1]["messages"] if mm["role"] == "tool"]
    assert "The user says: use tabs" in tool_msgs[-1]["content"]


def test_always_skips_later_approvals(client):
    c = client()
    init(c)
    (c.ws / "f.txt").write_text("a\n")
    c.approve = lambda p: {"decision": "always"}
    edit = lambda o, n: {"name": "edit_file", "args": {"path": "f.txt", "old_string": o, "new_string": n}}
    _, end, m = c.send(mock({"tools": [edit("a", "b")]}, {"tools": [edit("b", "c")]}, {"text": "x"}))
    assert (c.ws / "f.txt").read_text() == "c\n"
    assert len([x for x in c.of("_request", m) if x["method"] == "approval"]) == 1


def test_modes(client):
    c = client()
    init(c, mode="accept-edits")
    secret = c.ws / ".env"
    secret.write_text("TOKEN=1\n")
    asked = []
    c.approve = lambda p: (asked.append(p["kind"]), {"decision": "yes"})[1]
    script = mock({"tools": [{"name": "write_file", "args": {"path": "n.txt", "content": "hi"}},
                             {"name": SHELL, "args": {"command": "echo run"}},
                             {"name": "read_file", "args": {"path": ".env"}}]}, {"text": "ok"})
    c.send(script)
    assert asked == ["bash", "read"]
    asked.clear()
    st = c.call("set", {"mode": "auto"})
    assert st["mode"] == "auto"
    c.send(script)
    assert asked == ["read"]           # secret files still ask in auto mode
    assert c.call("set", {"mode": "bogus"}, ok=False)["error"]


def test_bash_output_and_secret_env_hidden(client):
    c = client(extra_env={"MY_API_KEY": "s3cret"})
    init(c, mode="auto")
    _, end, m = c.send(mock({"tools": [{"name": SHELL, "args": {"command": ("echo out; if ($env:MY_API_KEY) { echo $env:MY_API_KEY } else { echo none }; exit 3" if WINDOWS
                 else "echo out; echo ${MY_API_KEY:-none}; exit 3")}}]},
                            {"text": "k"}))
    te = c.wait("tool_end", start=m)
    assert "out" in te["output"] and "none" in te["output"] and "s3cret" not in te["output"]
    assert "[exit code 3]" in te["output"]


def test_fetch_approval_is_per_site(client):
    c = client()
    init(c)
    c.approve = lambda p: {"decision": "no"}
    _, end, m = c.send(mock({"tools": [{"name": "web_fetch", "args": {"url": "https://example.invalid/x"}}]},
                            {"text": "k"}))
    req = [x for x in c.of("_request", m) if x["method"] == "approval"][0]["params"]
    assert req["kind"] == "fetch:example.invalid" and req["always"] == "for example.invalid"
    assert req["detail"] == {"type": "fetch", "url": "https://example.invalid/x", "host": "example.invalid"}


def test_web_search(client):
    c = client()
    init(c)
    _, end, m = c.send(mock({"tools": [{"name": "web_search", "args": {"query": "tilburg"}}]}, {"text": "k"}))
    te = c.wait("tool_end", start=m)
    assert "https://example.org/a" in te["output"] and te["summary"] == "1 sources"


def test_bad_tool_arguments(client):
    c = client()
    init(c)
    _, end, m = c.send(mock({"tools": [{"name": "read_file", "args": {}}]}, {"text": "k"}))
    te = c.wait("tool_end", start=m)
    assert te["error"] and "bad arguments" in te["summary"]
    _, end, m = c.send(mock({"tools": [{"name": "no_such_tool", "args": {}}]}, {"text": "k"}))
    assert c.wait("tool_end", start=m)["error"]
    assert end["reason"] == "done"


# ---------------------------------------------------------------- cancel

def test_cancel_while_streaming(client):
    c = client()
    init(c)
    m = c.mark()
    c.call("send", {"text": mock({"text": "partial answer", "hang": 30})})
    c.wait("text", start=m)
    t = time.time()
    c.call("cancel")
    end = c.wait("turn_end", start=m, timeout=5)
    assert time.time() - t < 2
    assert end["reason"] == "cancelled"
    msgs = [x for x in c.call("transcript")["transcript"]]
    assert msgs[-1] == {"type": "assistant", "text": "partial answer"}
    _, end2, m2 = c.send("again")          # session keeps working afterwards
    assert c.text_since(m2) == "Echo: again"


def test_cancel_while_waiting_for_first_token(client):
    c = client()
    init(c)
    m = c.mark()
    c.call("send", {"text": mock({"text": "late", "delay": 30})})
    c.wait("turn_start", start=m)
    time.sleep(0.3)
    t = time.time()
    c.call("cancel")
    end = c.wait("turn_end", start=m, timeout=5)
    assert time.time() - t < 2 and end["reason"] == "cancelled"
    assert end["removedUser"]["text"].startswith("MOCK")


def test_cancel_running_command(client):
    c = client()
    init(c, mode="auto")
    m = c.mark()
    c.call("send", {"text": mock({"tools": [{"name": SHELL, "args": {"command": "sleep 30"}}]}, {"text": "x"})})
    c.wait("tool_start", start=m)
    t = time.time()
    c.call("cancel")
    end = c.wait("turn_end", start=m, timeout=5)
    assert time.time() - t < 2 and end["reason"] == "cancelled"
    assert "killed: cancelled" in c.wait("tool_end", start=m)["output"]


def test_cancel_pending_approval(client):
    c = client()
    init(c)
    c.approve = lambda p: None                 # never answer
    m = c.mark()
    c.call("send", {"text": mock({"tools": [{"name": SHELL, "args": {"command": "echo hi"}}]}, {"text": "x"})})
    req = c.wait("_request", start=m)
    c.call("cancel")
    rc = c.wait("request_cancelled", start=m)
    assert rc["requestId"] == req["id"]
    end = c.wait("turn_end", start=m)
    assert end["reason"] == "cancelled"


# ---------------------------------------------------------------- sub-agents

def test_subagents_run_in_parallel(client):
    c = client()
    init(c, mode="auto")
    (c.ws / "one.txt").write_text("ONE")
    sub = lambda f: mock({"delay": 1.0, "tools": [{"name": "read_file", "args": {"path": f}}]},
                         {"delay": 0.2, "text": f"report on {f}"})
    t = time.time()
    _, end, m = c.send(mock({"tools": [{"name": "task", "args": {"description": "first", "prompt": sub("one.txt")}},
                                       {"name": "task", "args": {"description": "second", "prompt": sub("one.txt")}}]},
                            {"text": "both done"}), timeout=20)
    took = time.time() - t
    assert end["reason"] == "done"
    starts, ends = c.of("task_start", m), c.of("task_end", m)
    assert len(starts) == 2 and len(ends) == 2
    assert all(e["result"] == "report on one.txt" for e in ends)
    sub_tools = [e for e in c.of("tool_start", m) if e["agent"]]
    assert {e["agent"] for e in sub_tools} == {s["id"] for s in starts}
    assert took < 3.5, took            # sequential would be ≥ 2.4 s of delays plus overhead
    t = c.call("transcript")["transcript"]
    assert [i["type"] for i in t].count("task") == 2


def test_subagent_missing_prompt(client):
    c = client()
    init(c)
    _, end, m = c.send(mock({"tools": [{"name": "task", "args": {"description": "x"}}]}, {"text": "k"}))
    assert c.wait("task_end", start=m)["error"]


# ---------------------------------------------------------------- queue, errors

def test_message_sent_while_busy_is_injected(client, api):
    c = client()
    init(c, mode="auto")
    m = c.mark()
    c.call("send", {"text": mock({"delay": 1.0, "tools": [{"name": "list_files", "args": {"pattern": "*"}}]},
                                 {"text": "first done"}), "clientId": "c1"})
    c.wait("turn_start", start=m)
    r = c.call("send", {"text": "and also this", "clientId": "c2"})
    assert r == {"queued": True}
    end = c.wait("turn_end", start=m, timeout=15)
    inj = c.wait("user", lambda e: e["clientId"] == "c2", start=m)
    assert inj["injected"]
    assert end["reason"] == "done"
    assert c.text_since(m).endswith("Echo: and also this")
    assert len(c.of("turn_end", m)) == 1


def test_cancel_returns_unsent_queue(client):
    c = client()
    init(c)
    m = c.mark()
    c.call("send", {"text": mock({"text": "x", "hang": 30})})
    c.wait("text", start=m)
    c.call("send", {"text": "queued one"})
    r = c.call("cancel")
    assert r["unsent"] == ["queued one"]
    assert c.wait("turn_end", start=m)["reason"] == "cancelled"


def test_api_error_removes_user_message(client):
    c = client()
    init(c)
    _, end, m = c.send(mock({"error": 500, "message": "boom"}))
    assert end["reason"] == "error" and "boom" in end["error"]
    assert end["removedUser"]["index"] == 1
    assert c.call("transcript")["transcript"] == []


def test_missing_api_key(client):
    c = client(key=None)
    r = init(c)
    assert r["apiKey"] is False
    _, end, m = c.send("hi")
    assert end["reason"] == "error" and "TILBURG_AI_API_KEY" in end["error"]


def test_tool_error_then_continue(client):
    c = client()
    init(c)
    _, end, m = c.send(mock({"tools": [{"name": "read_file", "args": {"path": "missing.txt"}}]}, {"text": "sorry"}))
    te = c.wait("tool_end", start=m)
    assert te["error"] and "file not found" in te["summary"]
    assert end["reason"] == "done"


# ---------------------------------------------------------------- btw, compact

def test_btw_not_kept(client, api):
    c = client()
    init(c)
    c.send("main question")
    m = c.mark()
    c.call("btw", {"question": "side?", "id": "b1"})
    end = c.wait("btw_end", start=m)
    assert end["id"] == "b1" and end["error"] is None
    assert "".join(e["delta"] for e in c.of("btw_text", m)) == "Echo: [Side question — answer briefly from what you already know and the conversation so far. Do not call any tools; this exchange will not be kept in the conversation.]\n\nside?"
    assert [i["type"] for i in c.call("transcript")["transcript"]] == ["user", "assistant"]


def test_btw_during_running_turn_uses_consistent_history(client, api):
    c = client()
    init(c)
    c.approve = lambda p: None
    m = c.mark()
    c.call("send", {"text": mock({"tools": [{"name": SHELL, "args": {"command": "echo"}}]}, {"text": "x"})})
    c.wait("_request", start=m)              # turn is waiting for approval; tool call has no result yet
    reset_log(api)
    c.call("btw", {"question": "quick", "id": "b2"})
    assert c.wait("btw_end", start=m)["error"] is None
    sent = api_log(api)[-1]["messages"]
    assert not any(mm.get("tool_calls") for mm in sent)
    c.call("cancel")
    c.wait("turn_end", start=m)


def test_compact(client):
    c = client()
    init(c)
    c.send("one")
    c.send("two")
    m = c.mark()
    c.call("compact", {})
    e = c.wait("compact_end", start=m)
    assert e["error"] is None
    t = e["transcript"]
    assert t[0]["kind"] == "summary" and t[0]["text"].startswith("Echo: Summarize our conversation")
    _, end, m2 = c.send("three")
    assert c.text_since(m2) == "Echo: three"
    assert c.call("compact", {}, ok=True) == {}
    c.wait("compact_end", start=c.mark() - 1)


def test_compact_requires_messages(client):
    c = client()
    init(c)
    assert "nothing to compact" in c.call("compact", ok=False)["error"]["message"]


# ---------------------------------------------------------------- settings & context

def test_set_tools_system_model(client, api):
    c = client()
    init(c)
    st = c.call("set", {"tools": False, "system": "Answer in Dutch", "model": "mock-small"})
    assert st["tools"] is False and st["system"] == "Answer in Dutch" and st["model"] == "mock-small"
    reset_log(api)
    c.send("x")
    req = api_log(api)[-1]
    assert "tools" not in req and req["model"] == "mock-small"
    assert "Answer in Dutch" in req["messages"][0]["content"]
    c.call("set", {"tools": True})
    reset_log(api)
    c.send("y")
    names = [t["function"]["name"] for t in api_log(api)[-1]["tools"]]
    assert "get_diagnostics" in names and "task" in names


def test_usage_reports_context_window(client):
    c = client()
    init(c)
    c.send("x")
    assert wait_until(lambda: c.call("usage")["maxCtx"] == 400000)
    u = c.call("usage")
    assert u["input"] > 0 and u["ctx"] > 0


def test_attachments_and_ide_context(client, api):
    c = client()
    init(c)
    (c.ws / "src").mkdir()
    (c.ws / "src" / "m.py").write_text("\n".join(f"line{i}" for i in range(1, 11)))
    (c.ws / "bin.dat").write_bytes(b"\0\1\2")
    reset_log(api)
    _, end, m = c.send("explain", attachments=[{"path": "src/m.py", "range": [2, 3]}, {"path": "src"},
                                               {"path": "bin.dat"}, {"path": "nope.txt"}],
                       context={"path": str(c.ws / "src" / "m.py"),
                                "selection": {"start": 4, "end": 5, "text": "line4\nline5"}})
    content = api_log(api)[-1]["messages"][-1]["content"]
    assert content.startswith("<ide_context>") and "src/m.py open" in content and "line4\nline5" in content
    assert '<file path="src/m.py:2-3">' in content and "line2" in content and "line4" not in content.split("<file")[1]
    assert '<directory path="src">' in content and "m.py" in content
    assert "binary file" in content and "could not attach" in content
    assert content.endswith("explain")
    u = c.wait("user", start=m)
    assert u["text"] == "explain" and u["context"] == "src/m.py:4-5"
    assert u["files"][:2] == ["src/m.py:2-3", "src/"]
    assert c.text_since(m).endswith("explain")


def test_ide_diagnostics_tool(client):
    c = client()
    init(c)
    (c.ws / "x.py").write_text("x")
    c.ide["ide/diagnostics"] = lambda p: [{"path": str(c.ws / "x.py"), "line": 1, "col": 1,
                                           "severity": "error", "message": "undefined name", "source": "pyright"}]
    _, end, m = c.send(mock({"tools": [{"name": "get_diagnostics", "args": {"path": "x.py"}}]}, {"text": "k"}))
    req = c.wait("_request", lambda e: e["method"] == "ide/diagnostics", start=m)
    assert req["params"]["path"] == str(c.ws / "x.py")
    te = c.wait("tool_end", start=m)
    assert "x.py:1:1: error: undefined name [pyright]" in te["output"] and te["summary"] == "1 errors, 0 other"


def test_no_ide_tools_without_ide_capability(client, api):
    c = client()
    c.call("initialize", {"ide": False})
    reset_log(api)
    c.send("x")
    req = api_log(api)[-1]
    assert "get_diagnostics" not in [t["function"]["name"] for t in req["tools"]]
    assert "Visual Studio Code" not in req["messages"][0]["content"]


def test_autosave_request_before_edit(client):
    c = client()
    init(c, autosave=True, mode="auto")
    (c.ws / "s.txt").write_text("a")
    saved = []
    c.ide["ide/saveFile"] = lambda p: (saved.append(p["path"]), {})[1]
    c.send(mock({"tools": [{"name": "read_file", "args": {"path": "s.txt"}}]}, {"text": "k"}))
    assert saved == [str(c.ws / "s.txt")]


# ---------------------------------------------------------------- sessions & rewind

def test_sessions_list_load_rename_delete(client):
    c = client()
    init(c)
    (c.ws / "r.txt").write_text("R")
    c.send(mock({"tools": [{"name": "read_file", "args": {"path": "r.txt"}}]}, {"text": "first session"}))
    sid1 = c.call("state")["session"]["id"]
    c.call("new_session")
    c.send("second")
    sid2 = c.call("state")["session"]["id"]
    ss = c.call("sessions")["sessions"]
    assert [s["id"] for s in ss] == [sid2, sid1]
    r = c.call("load_session", {"id": sid1})
    types = [i["type"] for i in r["transcript"]]
    assert types == ["user", "tool", "assistant"]
    tool = r["transcript"][1]
    assert tool["label"] == "Read" and tool["shown"] == "r.txt" and "R" in tool["output"]
    _, end, m = c.send("continue")             # loaded session keeps going
    assert c.call("state")["session"]["id"] == sid1
    c.call("rename_session", {"id": sid2, "title": "Renamed"})
    assert {s["id"]: s["title"] for s in c.call("sessions")["sessions"]}[sid2] == "Renamed"
    c.call("delete_session", {"id": sid2})
    assert [s["id"] for s in c.call("sessions")["sessions"]] == [sid1]
    assert c.call("load_session", {"id": "../../etc"}, ok=False)["error"]


def test_sessions_filtered_by_folder(client, api, tmp_path):
    c = client()
    init(c)
    c.send("here")
    other = tmp_path / "other"
    other.mkdir()
    s = json.loads(next((c.state / "tai" / "sessions").glob("*.json")).read_text())
    s["id"], s["cwd"] = "elsewhere-1", str(other)
    (c.state / "tai" / "sessions" / "elsewhere-1.json").write_text(json.dumps(s))
    assert len(c.call("sessions")["sessions"]) == 1
    assert len(c.call("sessions", {"all": True})["sessions"]) == 2


def test_rewind_restores_files_and_conversation(client):
    c = client()
    init(c, mode="auto")
    f = c.ws / "doc.txt"
    f.write_text("v0\n")
    c.send(mock({"tools": [{"name": "edit_file", "args": {"path": "doc.txt", "old_string": "v0", "new_string": "v1"}},
                           {"name": "write_file", "args": {"path": "new/created.txt", "content": "fresh"}}]},
                {"text": "turn one"}))
    c.send(mock({"tools": [{"name": "edit_file", "args": {"path": "doc.txt", "old_string": "v1", "new_string": "v2"}}]},
                {"text": "turn two"}))
    assert f.read_text() == "v2\n" and (c.ws / "new/created.txt").exists()
    users = [i for i in c.call("transcript")["transcript"] if i["type"] == "user"]
    second = users[1]["index"]
    prev = c.call("rewind_preview", {"index": second})["files"]
    assert [p["rel"] for p in prev] == ["doc.txt"]
    r = c.call("rewind", {"index": second})
    assert f.read_text() == "v1\n" and r["restored"] == ["doc.txt"]
    assert r["text"].startswith("MOCK")
    assert [i["type"] for i in r["transcript"]].count("user") == 1
    r = c.call("rewind", {"index": users[0]["index"]})
    assert f.read_text() == "v0\n" and not (c.ws / "new/created.txt").exists()
    assert r["transcript"] == []


def test_rewind_code_only_and_binary(client):
    c = client()
    init(c, mode="auto")
    b = c.ws / "data.bin"
    b.write_bytes(b"\xff\xfe\x00raw")
    c.send(mock({"tools": [{"name": "write_file", "args": {"path": "data.bin", "content": "text now"}}]},
                {"text": "k"}))
    idx = [i for i in c.call("transcript")["transcript"] if i["type"] == "user"][0]["index"]
    r = c.call("rewind", {"index": idx, "conversation": False})
    assert b.read_bytes() == b"\xff\xfe\x00raw"
    assert len(r["transcript"]) > 1                     # conversation kept
    assert c.call("rewind_preview", {"index": idx})["files"] == []   # snapshots consumed
    assert c.call("rewind", {"index": 0}, ok=False)["error"]


def test_busy_guards(client):
    c = client()
    init(c)
    m = c.mark()
    c.call("send", {"text": mock({"text": "x", "hang": 30})})
    c.wait("text", start=m)
    for meth, p in [("new_session", {}), ("compact", {}), ("rewind", {"index": 1}), ("set", {"tools": False})]:
        assert "busy" in c.call(meth, p, ok=False)["error"]["message"], meth
    assert c.call("set", {"mode": "auto"})["mode"] == "auto"     # mode can change mid-turn
    c.call("cancel")
    c.wait("turn_end", start=m)


def test_mode_change_releases_pending_approval(client):
    c = client()
    init(c)
    c.approve = lambda p: None
    m = c.mark()
    c.call("send", {"text": mock({"tools": [{"name": SHELL, "args": {"command": "echo released"}}]}, {"text": "x"})})
    c.wait("_request", start=m)
    c.call("set", {"mode": "auto"})
    end = c.wait("turn_end", start=m)
    assert end["reason"] == "done"
    assert "released" in c.wait("tool_end", start=m)["output"]
    assert c.of("request_cancelled", m)


def test_export_markdown(client, tmp_path):
    c = client()
    init(c)
    c.send("export me")
    out = tmp_path / "conv.md"
    md = c.call("export", {"path": str(out)})["markdown"]
    assert "## You\n\nexport me" in md and out.read_text(encoding="utf-8") == md


def test_shutdown_while_busy_exits(client):
    c = client()
    init(c, mode="auto")
    m = c.mark()
    c.call("send", {"text": mock({"tools": [{"name": SHELL, "args": {"command": "sleep 60"}}]})})
    c.wait("tool_start", start=m)
    t = time.time()
    c.p.stdin.close()
    c.p.wait(8)
    assert time.time() - t < 6


# ---------------------------------------------------------------- terminal CLI still works

def _cli(api, tmp_path, *args, stdin=None):
    env = {k: v for k, v in os.environ.items()}
    env.update({"TAI_API_BASE": api, "TILBURG_AI_API_KEY": "test-key", "XDG_STATE_HOME": str(tmp_path / "state")})
    return subprocess.run([sys.executable, TAI, *args], env=env, cwd=tmp_path, capture_output=True, text=True,
                          input=stdin, timeout=30)


def test_cli_one_shot(api, tmp_path):
    r = _cli(api, tmp_path, "--raw", "hello cli")
    assert r.returncode == 0 and "Echo: hello cli" in r.stdout


def test_cli_pipe_and_continue(api, tmp_path):
    r = _cli(api, tmp_path, "--raw", "summarize", stdin="piped text")
    assert r.returncode == 0 and "piped text" in r.stdout
    r = _cli(api, tmp_path, "--raw", "-c", "again")
    assert "Echo: again" in r.stdout
    last = json.loads((tmp_path / "state/tai/last.json").read_text())
    assert sum(m["role"] == "user" for m in last["messages"]) == 2


def test_cli_resume_vscode_session(api, tmp_path, client):
    c = client()
    init(c)
    c.send("from vscode")
    sid = c.call("state")["session"]["id"]
    env_state = c.state
    env = dict(os.environ, TAI_API_BASE=api, TILBURG_AI_API_KEY="test-key", XDG_STATE_HOME=str(env_state))
    r = subprocess.run([sys.executable, TAI, "--raw", "-r", sid, "and in the terminal"], env=env, cwd=c.ws,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "Echo: and in the terminal" in r.stdout
    r = subprocess.run([sys.executable, TAI, "-r", "nope", "x"], env=env, capture_output=True, text=True)
    assert r.returncode and "no saved session" in r.stderr


def test_cli_models(api, tmp_path):
    r = _cli(api, tmp_path, "--models")
    assert "gpt-5.6-sol" in r.stdout and "400k ctx" in r.stdout


# ---------------------------------------------------------------- regressions from the review

def test_lone_surrogate_does_not_break_the_session(client):
    c = client()
    init(c)
    m = c.mark()
    # raw JSON with a lone high surrogate, as JSON.stringify produces for half an emoji
    c.p.stdin.write('{"id": 900, "method": "send", "params": {"text": "half an emoji \\ud83d here", '
                    '"context": {"path": "x.py", "selection": {"start": 1, "end": 1, "text": "\\ud83d"}}}}\n')
    c.p.stdin.flush()
    end = c.wait("turn_end", start=m)
    assert end["reason"] == "done", end
    assert "half an emoji" in c.text_since(m)
    _, end, m = c.send(mock({"tools": [{"name": SHELL, "args": {"command": "echo 'still ok'"}}]}, {"text": "k"}))
    assert "still ok" in c.wait("tool_end", start=m)["output"]      # approvals still reach the client
    assert c.call("sessions")["sessions"]


def test_bad_diagnostics_path_keeps_conversation_valid(client):
    c = client()
    init(c)
    _, end, m = c.send(mock({"tools": [{"name": "get_diagnostics", "args": {"path": "a\u0000b"}}]}, {"text": "k"}))
    assert end["reason"] == "done", end
    assert c.wait("tool_end", start=m)["error"]
    _, end, m = c.send("next")          # the strict mock rejects tool calls without results
    assert end["reason"] == "done" and c.text_since(m) == "Echo: next"


def test_internal_error_mid_turn_still_ends_turn_and_repairs_history(client):
    c = client()
    init(c)
    c.ide["ide/diagnostics"] = lambda p: "not a list"
    _, end, m = c.send(mock({"tools": [{"name": "get_diagnostics", "args": {}}, {"name": "list_files", "args": {"pattern": "*"}}]},
                            {"text": "k"}))
    assert end["reason"] == "done"
    _, end, m = c.send("again")
    assert end["reason"] == "done", end


def test_compact_and_btw_forbid_tool_calls(client, api):
    c = client()
    init(c)
    c.send("one")
    reset_log(api)
    m = c.mark()
    c.call("compact")
    assert c.wait("compact_end", start=m)["error"] is None
    c.call("btw", {"question": "q", "id": "b"})
    c.wait("btw_end", start=m)
    assert [r.get("tool_choice") for r in api_log(api)] == ["none", "none"]


def test_edit_refused_if_file_changed_during_approval(client):
    c = client()
    init(c)
    f = c.ws / "race.txt"
    f.write_text("a\n")

    def approve(p):
        f.write_text("a\nuser typed this\n")        # user saves the file while the card is open
        return {"decision": "yes"}
    c.approve = approve
    _, end, m = c.send(mock({"tools": [{"name": "edit_file", "args": {"path": "race.txt", "old_string": "a", "new_string": "b"}}]},
                            {"text": "k"}))
    te = c.wait("tool_end", start=m)
    assert te["error"] and "changed while waiting" in te["summary"]
    assert f.read_text() == "a\nuser typed this\n"


def test_transcript_mid_turn_includes_streaming_text_and_running_tools(client):
    c = client()
    init(c)
    m = c.mark()
    c.call("send", {"text": mock({"text": "streaming so far", "hang": 30})})
    c.wait("text", start=m)
    t = c.call("transcript")["transcript"]
    assert t[-1] == {"type": "assistant", "text": "streaming so far", "streaming": True}
    c.call("cancel")
    c.wait("turn_end", start=m)
    c.approve = lambda p: None
    m = c.mark()
    c.call("send", {"text": mock({"tools": [{"name": SHELL, "args": {"command": "echo"}}]})})
    c.wait("_request", start=m)
    t = c.call("transcript")["transcript"]
    assert t[-1]["type"] == "tool" and t[-1]["done"] is False
    c.call("cancel")
    c.wait("turn_end", start=m)


def test_cancel_right_after_compact_is_not_lost(client):
    c = client()
    init(c)
    c.send("x")
    m = c.mark()
    c.call("compact")
    c.call("cancel")
    e = c.wait("compact_end", start=m)
    assert e["error"] == "cancelled" or e["error"] is None


def test_cancel_during_slow_web_search(client):
    c = client()
    init(c)
    m = c.mark()
    c.call("send", {"text": mock({"tools": [{"name": "web_search", "args": {"query": "slow search"}}]}, {"text": "x"})})
    c.wait("tool_start", start=m)
    time.sleep(0.3)
    t = time.time()
    c.call("cancel")
    end = c.wait("turn_end", start=m, timeout=5)
    assert end["reason"] == "cancelled" and time.time() - t < 1.5
    assert c.wait("tool_end", start=m)["summary"] == "Interrupted"
    _, end, m = c.send("next")
    assert end["reason"] == "done"
