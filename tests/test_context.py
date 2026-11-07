"""A full context window: tai summarizes the conversation and carries on (automatically near
the limit, or when the API refuses a request as too long), in VS Code and in the terminal.

The mock model "mock-window" has a 3000-token window (characters / 4) and refuses anything
larger; tai compacts at 80% of it."""
import json
import os
import subprocess
import sys

from conftest import TAI, api_log, mock, reset_log

def tai_system_prompt():
    import importlib.util
    spec = importlib.util.spec_from_file_location("tai_ctx", TAI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.system_prompt(True, ide=True)


PAD = "lorem ipsum dolor sit amet\n" * 310     # 8370 characters ≈ 2100 tokens


def init(c, **p):
    return c.call("initialize", {"ide": True, "model": "mock-window", **p})


def summary_requests(api):
    return [r for r in api_log(api) if "Summarize our conversation" in str(r["messages"][-1]["content"])]


def test_compacts_before_a_message_when_nearly_full(client, api):
    c = client()
    init(c)
    _, end, _ = c.send(PAD + mock({"text": "ONE"}))
    assert end["reason"] == "done"
    reset_log(api)
    m = c.mark()
    _, end, _ = c.send(mock({"text": "TWO"}))
    assert end["reason"] == "done", end
    phases = [e["phase"] for e in c.of("auto_compact", m)]
    assert phases == ["start", "end"]
    done = c.of("auto_compact", m)[-1]
    assert done["error"] is None and done["after"] < done["before"]
    assert "TWO" in c.text_since(m)
    assert len(summary_requests(api)) == 1
    last = api_log(api)[-1]["messages"]
    assert [x["role"] for x in last] == ["system", "user", "assistant", "user"]
    assert last[1]["content"].startswith("[Summary of the earlier conversation]")
    assert "TWO" in last[3]["content"] and PAD not in str(last)
    # the editor's transcript: summary, then the new message (its index points at it)
    tr = c.call("transcript")["transcript"]
    users = [i for i in tr if i["type"] == "user"]
    assert users[0]["kind"] == "summary" and "TWO" in users[1]["text"]
    assert c.call("transcript")["state"]["session"]["messages"] == 2
    ev = [e for e in c.of("user", m)][-1]
    assert ev["index"] == users[1]["index"] == 3


def test_compacts_in_the_middle_of_a_task(client, api):
    c = client()
    init(c, mode="auto")
    (c.ws / "big.txt").write_text("lorem ipsum dolor sit amet\n" * 230)   # with line numbers ≈ 1900 tokens
    _, end, _ = c.send(mock({"text": "warm up"}))
    reset_log(api)
    m = c.mark()
    _, end, _ = c.send(mock({"tools": [{"name": "read_file", "args": {"path": "big.txt"}}]}), timeout=20)
    assert end["reason"] == "done", end
    assert [e["phase"] for e in c.of("auto_compact", m)] == ["start", "end"]
    req = summary_requests(api)
    assert len(req) == 1 and "middle of a task" in req[0]["messages"][-1]["content"]
    # after the summary the model is told to carry on with the task
    last = api_log(api)[-1]["messages"]
    assert "Continue the task you were working on" in last[-1]["content"]
    assert "Echo: [The conversation was summarized" in c.text_since(m)
    # the note is not shown as a message of the user
    tr = c.call("transcript")["transcript"]
    assert not any("Continue the task" in i.get("text", "") for i in tr if i["type"] == "user")


def test_summary_request_is_cut_down_when_it_is_too_long(client, api):
    c = client()
    init(c)
    # fill the window to just under its limit (the system prompt's size changes as tai grows)
    lines = (2960 * 4 - len(json.dumps(tai_system_prompt())) - 300) // 28
    _, end, _ = c.send("lorem ipsum dolor sit amet\n" * lines + mock({"text": "ONE"}))
    assert end["reason"] == "done", end
    reset_log(api)
    _, end, _ = c.send(mock({"text": "TWO"}))
    assert end["reason"] == "done", end
    req = summary_requests(api)
    assert len(req) == 2        # the first was refused as too long, the second cut down
    assert "characters left out" in str(req[1]["messages"])


def test_context_error_compacts_and_retries(client, api):
    c = client(extra_env={"TAI_AUTO_COMPACT": "0"})    # only the API's refusal triggers it
    init(c)
    _, end, _ = c.send(PAD + mock({"text": "ONE"}))
    assert end["reason"] == "done"
    reset_log(api)
    m = c.mark()
    _, end, _ = c.send("x" * 4000 + mock({"text": "AFTER RETRY"}), timeout=20)
    assert end["reason"] == "done", end
    assert "AFTER RETRY" in c.text_since(m)
    assert len(summary_requests(api)) == 1
    assert end["removedUser"] is None


def test_auto_compact_can_be_turned_off(client, api):
    c = client(extra_env={"TAI_AUTO_COMPACT": "off"})
    init(c)
    c.send(PAD + mock({"text": "ONE"}))
    m = c.mark()
    _, end, _ = c.send(mock({"text": "TWO"}))
    assert end["reason"] == "done" and not c.of("auto_compact", m)


def test_cancel_while_compacting_keeps_the_conversation(client, api):
    c = client()
    init(c)
    c.send(PAD + "SLOW_SUMMARY" + mock({"text": "ONE"}))
    before = c.call("transcript")["transcript"]
    m = c.mark()
    c.call("send", {"text": mock({"text": "TWO"})})
    c.wait("auto_compact", lambda e: e["phase"] == "start", start=m)
    c.call("cancel")
    end = c.wait("turn_end", start=m, timeout=15)
    assert end["reason"] == "cancelled"
    assert c.wait("auto_compact", lambda e: e["phase"] == "end", start=m)["error"] == "cancelled"
    assert end["removedUser"] and "TWO" in end["removedUser"]["text"]
    assert c.call("transcript")["transcript"] == before


# ------------------------------------------------------------------ terminal (one-shot, -c)

def run(api, tmp_path, *args, env_extra=None):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "TILBURG_AI_API_KEY"}
    env.update({"TAI_API_BASE": api, "TILBURG_AI_API_KEY": "test-key", "PYTHONIOENCODING": "utf-8",
                "XDG_STATE_HOME": str(tmp_path / "state"), **(env_extra or {})})
    return subprocess.run([sys.executable, TAI, "-m", "mock-window", "--raw", *args], input="",
                          capture_output=True, text=True, encoding="utf-8", env=env, cwd=ws, timeout=60)


def test_terminal_compacts_a_continued_conversation(api, tmp_path):
    assert run(api, tmp_path, PAD + mock({"text": "ONE"})).returncode == 0
    reset_log(api)
    r = run(api, tmp_path, "-c", mock({"text": "TWO"}))
    assert r.returncode == 0, r.stderr
    assert "TWO" in r.stdout
    assert "conversation compacted" in r.stdout + r.stderr
    assert len(summary_requests(api)) == 1


def test_terminal_retries_after_a_context_error(api, tmp_path):
    off = {"TAI_AUTO_COMPACT": "0"}
    assert run(api, tmp_path, PAD + mock({"text": "ONE"}), env_extra=off).returncode == 0
    r = run(api, tmp_path, "-c", "y" * 4000 + mock({"text": "RETRIED"}), env_extra=off)
    assert r.returncode == 0, r.stderr
    assert "RETRIED" in r.stdout


def test_terminal_compacts_midway(api, tmp_path):
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "big.txt").write_text(PAD)
    assert run(api, tmp_path, mock({"text": "warm up"})).returncode == 0
    r = run(api, tmp_path, "-c", "--auto",
            mock({"tools": [{"name": "read_file", "args": {"path": "big.txt"}}]}))
    assert r.returncode == 0, r.stderr
    assert "tai continues" in r.stdout + r.stderr
    assert "Echo: [The conversation was summarized" in r.stdout
