"""The interactive terminal session, driven through a real pseudo-terminal (Linux, macOS).

While tai works the prompt stays open: /btw is answered at once, messages are queued for
tai's next step, approvals are answered in the prompt, Ctrl+C stops tai."""
import json
import os
import sys
import time

import pytest

from conftest import SHELL, TAI, WINDOWS, mock

pexpect = pytest.importorskip("pexpect") if not WINDOWS else None
pytestmark = pytest.mark.skipif(WINDOWS, reason="pexpect needs a POSIX terminal")

PROMPT = "❯ "


@pytest.fixture
def term(api, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "TILBURG_AI_API_KEY"}
    env.update({"TAI_API_BASE": api, "TILBURG_AI_API_KEY": "test-key",
                "XDG_STATE_HOME": str(tmp_path / "state"), "TERM": "xterm-256color"})
    procs = []

    def start(*args):
        p = pexpect.spawn(sys.executable, [TAI, *args], cwd=str(ws), env=env, dimensions=(40, 140),
                          encoding="utf-8", timeout=15)
        procs.append(p)
        p.expect("Welcome")
        p.expect(PROMPT)
        p.ws = ws
        return p
    yield start
    for p in procs:
        if p.isalive():
            p.terminate(force=True)


def done(p):
    """Wait for the end of a turn (its stats line), then the prompt."""
    p.expect(r"out · \d+s", timeout=15)
    p.expect(PROMPT)


def busy(p):
    """Wait until the running turn shows its status line above the prompt."""
    p.expect("ctrl\\+c to interrupt")


def test_btw_is_answered_while_tai_works(term):
    p = term()
    p.sendline(mock({"text": "MAIN DONE", "delay": 6}))
    busy(p)
    p.sendline("/btw " + mock({"text": "SIDE ANSWER"}))
    assert p.expect(["SIDE ANSWER", "MAIN DONE"], timeout=10) == 0, "btw waited for the main turn"
    p.expect("not added to the conversation")
    p.expect("MAIN DONE", timeout=15)
    p.sendline("/exit")
    p.expect(pexpect.EOF)


def test_btw_does_not_change_the_conversation(term):
    p = term()
    p.sendline("/btw " + mock({"text": "SIDE"}))
    p.expect("not added to the conversation")
    p.expect(PROMPT)
    p.sendline("/save conv.md")
    p.expect("saved to")
    assert "SIDE" not in (p.ws / "conv.md").read_text()
    p.sendline("/exit")
    p.expect(pexpect.EOF)


def test_message_typed_while_working_is_read_at_the_next_step(term):
    p = term("--auto")
    p.sendline(mock({"tools": [{"name": SHELL, "args": {"command": "sleep 4"}}]}, {"text": "never reached"}))
    busy(p)
    p.sendline("also check the tests")
    p.expect("queued")
    p.expect("tai is reading your message", timeout=15)
    p.expect("Echo: also check the tests", timeout=15)       # the model saw the queued message
    p.expect(PROMPT)
    p.sendline("/exit")
    p.expect(pexpect.EOF)


def test_message_queued_during_the_last_step_starts_a_new_turn(term):
    p = term()
    p.sendline(mock({"text": "FIRST", "delay": 4}))
    busy(p)
    p.sendline("second question")
    p.expect("queued")
    p.expect("FIRST", timeout=15)
    p.expect("Echo: second question", timeout=15)
    p.sendline("/exit")
    p.expect(pexpect.EOF)


def test_approval_is_answered_in_the_prompt(term):
    p = term()
    (p.ws / "f.txt").write_text("x = 1\n")
    p.sendline(mock({"tools": [{"name": "edit_file", "args": {"path": "f.txt", "old_string": "x = 1",
                                                             "new_string": "x = 2"}}]}, {"text": "EDITED"}))
    p.expect("1 yes · 2 always · 3 no")
    p.sendline("1")
    p.expect("EDITED")
    assert (p.ws / "f.txt").read_text() == "x = 2\n"
    p.sendline("/exit")
    p.expect(pexpect.EOF)


def test_denial_with_feedback_in_the_prompt(term):
    p = term()
    p.sendline(mock({"tools": [{"name": SHELL, "args": {"command": "echo hi"}}]}, {"text": "OK THEN"}))
    p.expect("1 yes · 2 always · 3 no")
    p.sendline("3")
    p.expect("What should tai do instead")
    p.sendline("use python instead")
    p.expect("Denied — use python instead")
    p.expect("OK THEN")
    p.sendline("/exit")
    p.expect(pexpect.EOF)


def test_ctrl_c_stops_a_running_turn_and_the_prompt_stays_usable(term):
    p = term()
    p.sendline(mock({"text": "SLOW", "hang": 30}))
    busy(p)
    t = time.time()
    p.sendcontrol("c")
    p.expect("interrupted", timeout=5)
    assert time.time() - t < 5
    p.sendline("hello again")
    p.expect("Echo: hello again")
    p.sendline("/exit")
    p.expect(pexpect.EOF)


def test_ctrl_c_gives_queued_messages_back(term):
    p = term()
    p.sendline(mock({"text": "SLOW", "hang": 30}))
    busy(p)
    p.sendline("not sent yet")
    p.expect("queued")
    p.sendcontrol("c")
    p.expect("back in the input")
    p.sendline("")                     # the restored text is in the input line: Enter sends it
    p.expect("Echo: not sent yet")
    p.sendline("/exit")
    p.expect(pexpect.EOF)


def test_ctrl_c_first_clears_typed_text(term):
    p = term()
    p.sendline(mock({"text": "STILL RUNNING", "delay": 4}))
    busy(p)
    p.send("half typed")
    p.sendcontrol("c")                 # clears the input, does not stop tai
    p.expect("STILL RUNNING", timeout=10)


def test_commands_that_change_the_conversation_wait(term):
    p = term()
    p.sendline(mock({"text": "DONE", "delay": 4}))
    busy(p)
    p.sendline("/clear")
    p.expect("/clear has to wait until tai is done")
    p.sendline("/mode auto")
    p.expect("mode: auto")
    p.expect("DONE", timeout=10)
    p.sendline("/exit")
    p.expect(pexpect.EOF)


def test_exit_while_working_stops_cleanly(term):
    p = term()
    p.sendline(mock({"text": "SLOW", "hang": 30}))
    busy(p)
    t = time.time()
    p.sendcontrol("d")
    p.expect(pexpect.EOF, timeout=10)
    assert time.time() - t < 10


def test_streamed_paragraphs_and_code_blocks_render_whole(term):
    p = term()
    text = "First paragraph.\n\n```python\nprint('a')\n\nprint('b')\n```\n\nLast paragraph."
    p.sendline(mock({"text": text, "chunk_delay": 0.01}))
    p.expect("First paragraph")
    p.expect("print\\('b'\\)")
    p.expect("Last paragraph")
    p.expect(PROMPT)
    p.sendline("/exit")
    p.expect(pexpect.EOF)


def test_classic_session_still_works(term):
    """Echo, an approved edit, /mode, and exiting with Ctrl+D (the original REPL checks)."""
    p = term()
    (p.ws / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    p.sendline("hello repl")
    p.expect("Echo: hello repl")
    p.expect(PROMPT)
    p.sendline(mock({"tools": [{"name": "edit_file", "args": {"path": "crlf.txt", "old_string": "b",
                                                             "new_string": "B"}}]}, {"text": "edited it"}))
    p.expect("1 yes · 2 always · 3 no")
    p.sendline("1")
    p.expect("edited it")
    assert (p.ws / "crlf.txt").read_bytes() == b"a\r\nB\r\n"
    p.expect(PROMPT)
    p.sendline("/mode")
    p.expect("mode: manual")
    p.expect(PROMPT)
    p.sendcontrol("d")
    p.expect(pexpect.EOF)


def test_compact_command(term):
    p = term()
    p.sendline(mock({"text": "FIRST ANSWER"}))
    done(p)
    p.sendline("/compact keep the numbers")
    p.expect("conversation compacted", timeout=15)
    p.expect(PROMPT)
    p.sendline("/save conv.md")
    p.expect("saved to")
    text = (p.ws / "conv.md").read_text()
    assert "[Summary of the earlier conversation]" in text and "FIRST ANSWER" not in text
    p.sendline("/exit")
    p.expect(pexpect.EOF)


def test_rewind_lists_restores_files_and_refills_the_input(term):
    p = term("--auto")
    f = p.ws / "doc.txt"
    f.write_text("v0\n")
    edit = lambda o, n: {"name": "edit_file", "args": {"path": "doc.txt", "old_string": o, "new_string": n}}
    p.sendline("first " + mock({"tools": [edit("v0", "v1")]}, {"text": "ONE DONE"}))
    done(p)
    p.sendline("second " + mock({"tools": [edit("v1", "v2")]}, {"text": "TWO DONE"}))
    done(p)
    assert f.read_text() == "v2\n"
    p.sendline("/rewind")
    p.expect("1  second")
    p.expect("1 file changed since")
    p.expect("2  first")
    p.expect(PROMPT)
    p.sendline("/rewind 1")
    p.expect("restored doc.txt")
    p.expect("back in the input")
    assert f.read_text() == "v1\n"
    p.sendcontrol("u")                       # clear the refilled text, type something else
    p.sendline("/rewind 1 keep-files")       # now 'first' is the latest message
    p.expect("back in the input")
    assert f.read_text() == "v1\n"
    p.sendcontrol("e")
    p.sendline("")                           # Enter resends the refilled message (auto mode)
    p.expect("old_string not found")         # v0 → v1 again: the file is still v1 (keep-files)
    done(p)
    assert f.read_text() == "v1\n"
    p.sendline("/exit")
    p.expect(pexpect.EOF)


def test_approval_2_saves_a_rule_shown_by_permissions(term):
    p = term()
    p.sendline(mock({"tools": [{"name": SHELL, "args": {"command": "echo saved-rule"}}]}, {"text": "RAN"}))
    p.expect("don't ask again for `echo` commands in this folder")
    p.sendline("2")
    done(p)
    p.sendline("/permissions")
    p.expect("allow commands: echo")
    p.expect("settings.json")
    p.sendline(mock({"tools": [{"name": SHELL, "args": {"command": "echo again"}}]}, {"text": "NO QUESTION"}))
    i = p.expect([r"out · \d+s", "1 yes · 2 always"])
    assert i == 0, "the saved rule did not apply"
    p.sendline("/exit")
    p.expect(pexpect.EOF)


def test_auto_compact_in_a_live_session(term):
    p = term("--auto", "-m", "mock-window")
    (p.ws / "big.txt").write_text("lorem ipsum dolor sit amet\n" * 230)
    p.sendline("hello")
    done(p)
    p.sendline(mock({"tools": [{"name": "read_file", "args": {"path": "big.txt"}}]}))
    p.expect("context window is almost full", timeout=15)
    p.expect("tai continues", timeout=15)
    p.expect("Echo: \\[The conversation was summarized")
    done(p)
    p.sendline("/exit")
    p.expect(pexpect.EOF)


def test_background_job_in_a_live_session(term):
    p = term("--auto")
    cmd = f"'{sys.executable}' -c \"import time; time.sleep(5); print('job says hi')\""
    p.sendline(mock({"tools": [{"name": SHELL, "args": {"command": cmd, "run_in_background": True}},
                               {"name": "todo_write", "args": {"todos": [
                                   {"content": "Start the job", "status": "completed"},
                                   {"content": "Wait for it", "status": "in_progress"}]}}]}, {"text": "k"}))
    p.expect("Background")                   # (capital B: not the typed run_in_background)
    p.expect("Started job b1")
    p.expect("◼ Wait for it")
    done(p)
    p.sendline("/jobs")
    p.expect("running for")
    p.expect("background job b1 finished \\(exit code 0\\)", timeout=10)
    p.sendline("/jobs")
    p.expect("finished \\(exit code 0\\)")
    p.expect("job says hi")                  # the last line it printed
    p.sendline("/exit")
    p.expect(pexpect.EOF)
