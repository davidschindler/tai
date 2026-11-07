"""The task list (todo_write) and commands in the background (run_in_background, job_output,
job_stop), in the editor protocol and the terminal program."""
import importlib.util
import os
import subprocess
import sys
import time

from conftest import SHELL, TAI, WINDOWS, api_log, mock, reset_log, wait_until

spec = importlib.util.spec_from_file_location("tai_mod2", TAI)
tai = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tai)


def init(c, **p):
    return c.call("initialize", {"ide": True, **p})


def py(code):
    """A shell command that runs Python code, for the shell of this OS."""
    exe = sys.executable
    return f"& '{exe}' -c \"{code}\"" if WINDOWS else f"'{exe}' -c \"{code}\""


def sh(cmd, bg=False):
    return {"name": SHELL, "args": {"command": cmd, **({"run_in_background": True} if bg else {})}}


TODOS = [{"content": "Read the data", "status": "completed"},
         {"content": "Clean the columns", "status": "in_progress"},
         {"content": "Run the regressions", "status": "pending"}]


# ------------------------------------------------------------------ task list

def test_todo_write_updates_the_editor_and_the_state(client):
    c = client()
    init(c)
    _, end, m = c.send(mock({"tools": [{"name": "todo_write", "args": {"todos": TODOS}}]}, {"text": "k"}))
    assert c.wait("todos", start=m)["items"] == TODOS
    te = c.wait("tool_end", start=m)
    assert not te["error"] and "1 of 3 done" in te["output"]
    assert te["summary"] == "1 of 3 done"
    assert "☒ Read the data\n◼ Clean the columns\n☐ Run the regressions" in te["output"]
    assert c.wait("tool_start", start=m)["label"] == "Tasks"
    assert c.call("state")["todos"] == TODOS
    assert not [x for x in c.of("_request", m) if x["method"] == "approval"]    # no approval needed
    # saved with the session
    sid = c.call("state")["session"]["id"]
    c.call("new_session")
    assert c.call("state")["todos"] == []
    assert c.call("load_session", {"id": sid})["state"]["todos"] == TODOS


def test_todo_write_rejects_bad_items(client):
    c = client()
    init(c)
    _, end, m = c.send(mock({"tools": [{"name": "todo_write", "args": {"todos": [
        {"content": "x", "status": "done"}]}}]}, {"text": "k"}))
    te = c.wait("tool_end", start=m)
    assert te["error"] and "status must be one of" in te["output"]
    assert not c.of("todos", m)


def test_sub_agents_have_no_task_list_but_can_use_jobs():
    names = {t["function"]["name"] for t in tai.SUB_TOOLS}
    assert "todo_write" not in names and "task" not in names
    assert {"job_output", "job_stop"} <= names


def test_compaction_keeps_open_tasks():
    msgs = tai.compacted_messages({"role": "system", "content": "s"}, "SUMMARY", False, TODOS)
    assert "◼ Clean the columns" in msgs[1]["content"]
    done = [{**t, "status": "completed"} for t in TODOS]
    assert "task list" not in tai.compacted_messages({"role": "system", "content": "s"}, "S", False, done)[1]["content"]


# ------------------------------------------------------------------ background jobs

def test_background_job_runs_while_tai_continues(client):
    c = client()
    init(c, mode="auto")
    cmd = py("import time; print('job-start', flush=True); time.sleep(2); print('job-end')")
    t = time.time()
    _, end, m = c.send(mock({"tools": [sh(cmd, bg=True)]},
                            {"tools": [{"name": "job_output", "args": {"job_id": "b1", "wait": 20}}]},
                            {"text": "k"}), timeout=30)
    starts = [e for e in c.of("tool_start", m)]
    assert starts[0]["label"].endswith("(background)")
    ends = c.of("tool_end", m)
    assert "Started job b1" in ends[0]["summary"] and "job_output" in ends[0]["output"]
    assert "job-start" in ends[1]["output"] and "job-end" in ends[1]["output"]
    assert "exit code 0" in ends[1]["output"]
    assert time.time() - t < 25
    jobs = c.call("jobs")["jobs"]
    assert jobs[0]["id"] == "b1" and not jobs[0]["running"] and jobs[0]["code"] == 0


def test_background_command_still_needs_approval(client):
    c = client()
    init(c)
    c.approve = lambda p: {"decision": "no"}
    _, end, m = c.send(mock({"tools": [sh("echo hi", bg=True)]}, {"text": "k"}))
    req = [x for x in c.of("_request", m) if x["method"] == "approval"][0]["params"]
    assert req["title"].endswith("· background") and req["detail"]["background"] is True
    assert c.call("jobs")["jobs"] == []


def test_model_is_told_when_a_job_finished(client, api):
    c = client()
    init(c, mode="auto")
    c.send(mock({"tools": [sh(py("print('quick')"), bg=True)]}, {"text": "started"}))
    done = c.wait("job_done", timeout=15)
    assert done["job"]["id"] == "b1" and done["job"]["code"] == 0
    reset_log(api)
    c.send(mock({"text": "next"}))
    sent = api_log(api)[-1]["messages"]
    assert sent[-2]["role"] == "user" and sent[-2]["content"].startswith("[Background job b1")
    assert "exit code 0" in sent[-2]["content"]
    users = [i for i in c.call("transcript")["transcript"] if i["type"] == "user"]
    assert not any("Background job" in u["text"] for u in users)
    reset_log(api)
    c.send(mock({"text": "again"}))                 # told only once
    assert "Background job" not in str(api_log(api)[-1]["messages"][-2:])


def test_job_stop_ends_the_process_tree(client, tmp_path):
    c = client()
    init(c, mode="auto")
    marker = tmp_path / "late.txt"
    cmd = py(f"import time; time.sleep(4); open(r'{marker}', 'w').write('x')")
    c.send(mock({"tools": [sh(cmd, bg=True)]}, {"text": "started"}))
    assert c.call("jobs")["jobs"][0]["running"]
    _, end, m = c.send(mock({"tools": [{"name": "job_stop", "args": {"job_id": "b1"}}]}, {"text": "k"}))
    assert "Stopped job b1" in c.wait("tool_end", start=m)["summary"]
    info = c.call("jobs")["jobs"][0]
    assert not info["running"] and info["stopped"] and "stopped" in info["state"]
    time.sleep(5)
    assert not marker.exists(), "the job's process survived job_stop"


def test_jobs_stop_when_tai_exits(client, tmp_path):
    c = client()
    init(c, mode="auto")
    marker = tmp_path / "after-exit.txt"
    cmd = py(f"import time; time.sleep(4); open(r'{marker}', 'w').write('x')")
    c.send(mock({"tools": [sh(cmd, bg=True)]}, {"text": "started"}))
    c.close()
    assert wait_until(lambda: c.p.poll() is not None, 10)
    time.sleep(5)
    assert not marker.exists(), "a background job outlived tai"


def test_unknown_job(client):
    c = client()
    init(c)
    _, end, m = c.send(mock({"tools": [{"name": "job_output", "args": {"job_id": "b9"}}]}, {"text": "k"}))
    te = c.wait("tool_end", start=m)
    assert te["error"] and "no background job" in te["output"]
    assert c.call("job_stop", {"id": "b9"}, ok=False)["error"]


# ------------------------------------------------------------------ terminal program (one-shot)

def test_terminal_background_job_and_task_list(api, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "TILBURG_AI_API_KEY"}
    env.update({"TAI_API_BASE": api, "TILBURG_AI_API_KEY": "test-key", "PYTHONIOENCODING": "utf-8",
                "XDG_STATE_HOME": str(tmp_path / "state")})
    script = mock({"tools": [{"name": "todo_write", "args": {"todos": TODOS}},
                             sh(py("print('from-the-job')"), bg=True)]},
                  {"tools": [{"name": "job_output", "args": {"job_id": "b1", "wait": 20}}]},
                  {"text": "all done"})
    r = subprocess.run([sys.executable, TAI, "--auto", "--raw", script], input="", capture_output=True,
                       text=True, encoding="utf-8", env=env, cwd=ws, timeout=60)
    assert r.returncode == 0, r.stderr
    out = r.stdout + r.stderr
    assert "◼ Clean the columns" in out and "Started job b1" in out
    assert "exit code 0" in out and "all done" in out
