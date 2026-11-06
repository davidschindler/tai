"""The terminal program, run non-interactively (works the same on Linux, macOS and Windows)."""
import os
import subprocess
import sys

from conftest import SHELL, TAI, mock


def run(api, tmp_path, *args, stdin=None, key="test-key"):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "TILBURG_AI_API_KEY"}
    env.update({"TAI_API_BASE": api, "XDG_STATE_HOME": str(tmp_path / "state"), "PYTHONIOENCODING": "utf-8"})
    if key:
        env["TILBURG_AI_API_KEY"] = key
    return subprocess.run([sys.executable, TAI, *args], input=stdin if stdin is not None else "",
                          capture_output=True, text=True, encoding="utf-8", env=env, cwd=ws, timeout=60)


def test_version(api, tmp_path):
    r = run(api, tmp_path, "--version")
    assert r.returncode == 0 and r.stdout.startswith("tai 1.")


def test_one_shot_answer(api, tmp_path):
    r = run(api, tmp_path, "--raw", mock({"text": "Hallo, wereld — ünïcödé ✓"}))
    assert r.returncode == 0, r.stderr
    assert "Hallo, wereld — ünïcödé ✓" in r.stdout


def test_one_shot_with_tools_in_auto_mode(api, tmp_path):
    r = run(api, tmp_path, "--auto", "--raw",
            mock({"tools": [{"name": "write_file", "args": {"path": "out.txt", "content": "made\n"}},
                            {"name": SHELL, "args": {"command": "echo shell-ran"}}]},
                 {"text": "all done"}))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "ws" / "out.txt").read_text() == "made\n"
    assert "all done" in r.stdout


def test_piped_input_is_attached(api, tmp_path):
    r = run(api, tmp_path, "--raw", mock({"text": "got it"}), stdin="line from a pipe\n")
    assert r.returncode == 0, r.stderr
    assert "got it" in r.stdout


def test_missing_key_explains_set_key(api, tmp_path):
    r = run(api, tmp_path, "--raw", "hello", key=None)
    assert r.returncode != 0
    assert "tai --set-key" in r.stderr


def test_continue_uses_last_conversation(api, tmp_path):
    assert run(api, tmp_path, "--raw", mock({"text": "first"})).returncode == 0
    r = run(api, tmp_path, "-c", "--raw", mock({"text": "second"}))
    assert r.returncode == 0 and "second" in r.stdout
