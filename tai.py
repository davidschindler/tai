#!/usr/bin/env python3
"""tai — a terminal AI agent on Tilburg.ai.

  tai                          interactive agent session in the current folder
  tai "fix the typo in intro.tex"   one task, then exit (add -i to keep chatting)
  cat log.txt | tai "why did this fail?"
  tai -f draft.md "tighten the intro"
  tai -c                       continue the last conversation
  tai -r ID                    resume a saved session (sessions from the VS Code extension)
  tai --chat                   plain chat, no tools
  tai --models                 list available models

Tools: read/list/grep files and web_search run on their own. Editing files,
running commands and fetching web pages (per site) ask for approval first.
The agent can start sub-agents (task tool), which run in parallel.
Shift+Tab (or /mode) cycles the permission mode: manual → accept-edits → auto → plan.
/btw asks a side question that is not kept in the conversation.

Key: $TILBURG_AI_API_KEY, or store one with `tai --set-key`.
Models: $TAI_MODEL (default below), $TAI_SEARCH_MODEL for web searches.
"""
import argparse
import concurrent.futures
import contextlib
import datetime
import difflib
import fnmatch
import json
import os
import platform
import queue
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

API_ENDPOINT = os.environ.get("TAI_API_BASE", "https://api.tilburg.ai/v1").rstrip("/")
DEFAULT_MODEL = os.environ.get("TAI_MODEL", "gpt-5.6-sol")
SEARCH_MODEL = os.environ.get("TAI_SEARCH_MODEL", "gpt-5.4-nano")
MAX_STEPS = 60              # tool rounds per user message
SUB_MAX_STEPS = 40          # tool rounds per sub-agent
MAX_PARALLEL_AGENTS = 4
MAX_TOOL_OUTPUT = 30_000    # characters returned to the model per tool call
WINDOWS = os.name == "nt"


def _state_dir():
    if os.environ.get("XDG_STATE_HOME"):
        return Path(os.environ["XDG_STATE_HOME"]) / "tai"
    if WINDOWS and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "tai"
    return Path.home() / ".local" / "state" / "tai"


STATE_DIR = _state_dir()
KEY_FILE = STATE_DIR / "api_key"           # written by `tai --set-key`
# Windows: helper programs get a hidden console of their own. Without this, when tai itself runs
# without a console (started by VS Code), every command would pop up a console window.
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}
LAST_CONV = STATE_DIR / "last.json"
HISTORY = STATE_DIR / "history"

HOME = Path.home()
# Reading these asks for approval: their content would be sent to the API.
SENSITIVE = [HOME / ".ssh", HOME / ".gnupg", HOME / ".secrets", HOME / ".netrc", HOME / ".aws",
             HOME / ".config/gh", HOME / ".claude.json", HOME / ".npmrc", HOME / ".docker", KEY_FILE]
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
             ".pytest_cache", ".ipynb_checkpoints", ".Rproj.user"}
# Environment variables hidden from commands the agent runs.
SECRET_ENV = re.compile(r"(API_?KEY|_KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)$", re.I)

try:
    from rich import box
    from rich.align import Align
    from rich.console import Console, Group
    from rich.live import Live
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.table import Table
    from rich.text import Text

    console = Console(highlight=False)
    RICH = True
except ImportError:
    RICH = False

# Tilburg University house colors (navy brightened so it reads on dark terminals too)
GOLD = "#c49a06"
BLUE = "#3f7fc4"
GREY = "#8a8a8a"

# The Tilburg University emblem (tree in a circle, ringed by gold diamonds and squares),
# traced from the official logo as braille art: (characters, color per character).
LOGO = [
    ('       ⢠⣾⣷⡄       ', '.......gggg.......'),
    ('  ⢰⣶⣶   ⠙⠋   ⣴⣶⡆  ', '..ggg...gg...ggg..'),
    ('  ⠘⠛⠋ ⣠⢄⢐⠣⡢⡀ ⠙⠛⠃  ', '..ggg.bbbbbb.ggg..'),
    (' ⣀  ⠐⢞⡲⢍⢎⡱⡪⠶⠶⠂  ⣀ ', '.g..bbbbbbbbbb..g.'),
    ('⢾⣿⡷  ⢐⡽⡓⢻⣿⠙⡫⠆  ⢾⣿⡷', 'ggg..bbbbbbbb..ggg'),
    (' ⠉   ⣸⣗⠃⡼⣇⢰⣷⠆   ⠉ ', '.g...bbbbbbbb...g.'),
    ('  ⢠⣤⣄⠙⠫⣓⣥⣬⡺⠟⠋⣠⣤⡄  ', '..gggbbbbbbbbggg..'),
    ('  ⠸⠿⠿   ⣠⣄   ⠻⠿⠇  ', '..ggg...gg...ggg..'),
    ('       ⠘⢿⡿⠃       ', '.......gggg.......'),
]

IO_HOOKS = {"before_write": None, "after_write": None}   # set by the IDE server (tai --serve)
STREAMS = set()                 # open API responses, so a cancel can cut them off at once
STREAMS_LOCK = threading.Lock()
IO = threading.RLock()          # serializes terminal output and approval prompts
CANCEL = threading.Event()      # set by Ctrl+C; sub-agents stop at the next step
_tls = threading.local()        # per-thread output prefix (for sub-agents)
INBOX = []                      # messages typed while tai works; read at its next step
INBOX_LOCK = threading.Lock()
APPROVAL = threading.Lock()     # one approval question at a time (parallel sub-agents)


class LiveInput:
    """Terminal sessions where the prompt stays open while tai works: a turn runs in a
    background thread, its output is printed above the prompt (prompt_toolkit's
    patch_stdout), the activity line sits above the input, and questions from the turn
    (approvals) are answered in the prompt itself."""

    def __init__(self):
        self.active = False     # a prompt_toolkit prompt owns the bottom of the screen
        self.busy = False       # a turn is running in the background
        self.status = None      # object with .line() -> [(style, text)], shown above the input
        self.question = None    # (prompt text, Event, answer box) from the turn thread
        self.refill = ""        # queued messages to put back into the next prompt
        self.lock = threading.Lock()
        self.invalidate = lambda: None

    def ask(self, prompt):
        """Called from a worker thread: show prompt as the input line, wait for the answer.
        Raises EOFError when tai is stopped meanwhile."""
        ev, box = threading.Event(), []
        with self.lock:
            self.question = (prompt, ev, box)
        self.invalidate()
        try:
            while not ev.wait(0.1):
                if CANCEL.is_set():
                    raise EOFError
            return box[0]
        finally:
            with self.lock:
                if self.question and self.question[1] is ev:
                    self.question = None
            self.invalidate()

    def answer(self, text):
        """Main thread: deliver the line the user entered. False if nothing was asked."""
        with self.lock:
            q, self.question = self.question, None
        if not q:
            return False
        q[2].append(text)
        q[1].set()
        return True


LIVE = LiveInput()


class Activity:
    """Status line for a slow step (running a command, …) while the prompt stays open."""
    FRAMES = "·✢✳✶✻✽✻✶✳✢"

    def __init__(self, text):
        self.text, self.start = text, time.time()

    def line(self):
        t = time.time() - self.start
        return [("class:spin", self.FRAMES[int(t * 8) % len(self.FRAMES)] + " " + self.text + " "),
                ("class:hint", f"({int(t)}s · ctrl+c to interrupt)")]


def ask_line(prompt):
    """Read one answer line: in the open prompt when a turn runs in the background, else input()."""
    if LIVE.active and threading.current_thread() is not threading.main_thread():
        return LIVE.ask(prompt)
    return input(prompt)


class Usage:
    """Session token counters, shared by the main agent and sub-agents."""

    def __init__(self):
        self.lock = threading.Lock()
        self.inp = self.out = 0     # billed tokens this session
        self.ctx = 0                # size of the main conversation at the last reply
        self.max_ctx = None         # context window of the current model (if known)
        self.streaming = 0          # estimated output tokens of replies still streaming
        self.window_known = threading.Event()   # set when no model_window() lookup is running
        self.window_known.set()

    def add(self, usage, main=False):
        if not usage:
            return
        with self.lock:
            i, o = usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0
            self.inp += i
            self.out += o
            if main:
                self.ctx = i + o

    def stream(self, n):
        with self.lock:
            self.streaming += n

    def total(self):
        return self.inp + self.out + self.streaming

    def ctx_text(self):
        if not self.ctx:
            return ""
        if self.max_ctx:
            return f"ctx {human(self.ctx)}/{human(self.max_ctx)} ({100 * self.ctx // self.max_ctx}%)"
        return f"ctx {human(self.ctx)}"


USAGE = Usage()

# Permission modes, cycled with Shift+Tab or /mode.
MODES = ["manual", "accept-edits", "auto", "plan"]
MODE_INFO = {"manual": "asks before edits, commands and fetches",
             "accept-edits": "file edits run without asking",
             "auto": "edits, commands and fetches run without asking (secret files still ask)",
             "plan": "tai only reads and makes a plan; nothing changes until you approve the plan"}


def mode_info():
    info = dict(MODE_INFO)
    if SANDBOX.active():
        info["auto"] = ("edits in this folder, commands in the sandbox and fetches run without asking; "
                        "anything outside the sandbox asks")
    return info


# ================================================================ saved permissions
# settings.json in tai's folder holds rules that last beyond a session:
#   {"allow": {"commands": ["git status"], "sites": ["docs.python.org"]},
#    "deny":  {"commands": ["git push"], "sites": []},
#    "folders": {"/path/to/project": {"allow": {"commands": ["pytest"]}}}}
# A command rule matches a command that starts with the same words ("git status" matches
# "git status -s"); a rule with * or ? is a pattern for the whole command. Allow rules only
# approve simple commands (no ; & | > < ` $ ( ) { }), so "git status" never approves
# "git status; rm -rf ~". Deny rules are checked in every part of a command and win over
# everything, auto mode included. Site rules match the host name (patterns like *.example.org).
SETTINGS_FILE = STATE_DIR / "settings.json"
SHELL_META = re.compile(r"[;&|<>`$(){}\n\r]")
NO_RULE = {"sudo", "su", "doas", "env", "xargs", "eval", "exec", "nohup", "timeout", "watch", "time",
           "nice", "command", "rm", "rmdir", "del", "erase", "rd", "remove-item", "ri", "find", "dd",
           "mkfs", "chmod", "chown", "iex", "invoke-expression", "start-process", "cmd", "curl", "wget",
           "invoke-webrequest", "iwr", "ssh", "scp"}
INTERPRETERS = {"python", "python3", "py", "node", "rscript", "ruby", "perl", "bash", "sh", "zsh",
                "pwsh", "powershell", "deno", "julia", "stata", "stata-mp", "stata-se", "matlab"}
SUBCOMMANDS = {"git", "npm", "pnpm", "yarn", "npx", "pip", "pip3", "pipx", "uv", "conda", "mamba",
               "cargo", "go", "docker", "kubectl", "dotnet", "brew", "apt", "poetry", "gh", "quarto",
               "make", "hugo", "latexmk"}
WRAPPERS = {"sudo", "doas", "nohup", "time", "command", "exec", "nice", "env", "xargs", "timeout"}


def cmd_words(cmd):
    """The words of a command, the program name normalized (basename, lower case, no .exe)."""
    try:
        w = shlex.split(cmd, posix=not WINDOWS)
    except ValueError:
        return None
    if w:
        head = re.split(r"[\\/]", w[0])[-1].lower()
        w[0] = head[:-4] if head.endswith(".exe") else head
    return w


def command_rule(cmd):
    """What 'don't ask again' saves for this command ("git status", "python run.py"), or None
    when a rule would allow too much (rm, sudo, python -c, compound commands, …)."""
    if SHELL_META.search(cmd):
        return None
    w = cmd_words(cmd.strip())
    if not w or w[0] in NO_RULE:
        return None
    if w[0] in INTERPRETERS:
        if len(w) >= 3 and w[1] == "-m" and not w[2].startswith("-"):
            return " ".join(w[:3])
        if len(w) >= 2 and not w[1].startswith("-"):
            return " ".join(w[:2])
        return None
    if w[0] in SUBCOMMANDS:
        return " ".join(w[:2]) if len(w) >= 2 and not w[1].startswith("-") else None
    return w[0]


def command_matches(cmd, rule):
    rule = rule.strip()
    if not rule:
        return False
    if any(c in rule for c in "*?["):
        return fnmatch.fnmatchcase(" ".join(cmd.split()).lower(), " ".join(rule.split()).lower())
    w, r = cmd_words(cmd.strip()), cmd_words(rule)
    return bool(w and r) and w[:len(r)] == r


def command_parts(cmd):
    """Best-effort split of a compound command into its simple commands, wrappers removed."""
    for part in re.split(r"[;&|\n\r`(){}]|\$\(", cmd):
        w = cmd_words(part.strip()) or []
        while w and (w[0] in WRAPPERS or re.fullmatch(r"\w+=.*", w[0]) or w[0].startswith("-")):
            w = w[1:]
            if w:
                head = re.split(r"[\\/]", w[0])[-1].lower()
                w[0] = head[:-4] if head.endswith(".exe") else head
        if w:
            yield " ".join(shlex.quote(x) for x in w) if not WINDOWS else " ".join(w)


def site_matches(host, rule):
    rule = rule.strip().lower()
    return bool(rule) and (host == rule or fnmatch.fnmatchcase(host, rule))


def _same_or_inside(path, folder):
    try:
        a = os.path.normcase(os.path.realpath(path))
        b = os.path.normcase(os.path.realpath(os.path.expanduser(folder)))
    except (OSError, ValueError):
        return False
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


class Settings:
    """settings.json, re-read when it changes (so edits apply at once, also to a running session)."""

    def __init__(self, path):
        self.path, self.stamp, self.data, self.warned = path, None, {}, None
        self.lock = threading.Lock()

    def load(self):
        with self.lock:
            try:
                st = self.path.stat()
                stamp = (st.st_mtime_ns, st.st_size)
            except OSError:
                self.stamp, self.data = None, {}
                return self.data
            if stamp != self.stamp:
                self.stamp = stamp
                try:
                    d = json.loads(self.path.read_text(encoding="utf-8-sig"))
                    if not isinstance(d, dict):
                        raise ValueError("the file must hold a JSON object")
                    self.data = d
                except (OSError, ValueError) as e:
                    self.data = {}
                    if self.warned != stamp:
                        self.warned = stamp
                        err(f"tai: ignoring {self.path}: {e}")
            return self.data

    def rules(self, verdict, what):
        def strs(x):
            return [r for r in x if isinstance(r, str)] if isinstance(x, list) else []

        def pick(d):
            v = d.get(verdict) if isinstance(d, dict) else None
            return strs(v.get(what)) if isinstance(v, dict) else []
        d = self.load()
        out = pick(d)
        folders = d.get("folders")
        if isinstance(folders, dict):
            cwd = Path.cwd()
            for folder, fd in folders.items():
                if _same_or_inside(cwd, folder):
                    out += pick(fd)
        return out

    def denied(self, kind, detail):
        """The deny rule that blocks this request, or None."""
        if kind == "bash":
            cmd = str((detail or {}).get("command") or "")
            parts = list(command_parts(cmd))
            for r in self.rules("deny", "commands"):
                if command_matches(cmd, r) or any(command_matches(p, r) for p in parts):
                    return r
        elif kind.startswith("fetch:"):
            for r in self.rules("deny", "sites"):
                if site_matches(kind[6:], r):
                    return r
        return None

    def allowed(self, kind, detail):
        if kind == "bash":
            cmd = str((detail or {}).get("command") or "")
            return not SHELL_META.search(cmd) and any(
                command_matches(cmd, r) for r in self.rules("allow", "commands"))
        if kind.startswith("fetch:"):
            return any(site_matches(kind[6:], r) for r in self.rules("allow", "sites"))
        return False

    def add(self, verdict, what, rule, folder=None):
        """Save a rule (raises OSError if the file can't be written)."""
        with self.lock:
            try:
                d = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except FileNotFoundError:
                d = {}
            except ValueError:
                raise OSError(f"{self.path} is not valid JSON; fix or remove it first") from None
            if not isinstance(d, dict):
                raise OSError(f"{self.path} must hold a JSON object")
            target = d
            if folder is not None:
                target = d.setdefault("folders", {}).setdefault(str(folder), {})
            lst = target.setdefault(verdict, {}).setdefault(what, [])
            if rule not in lst:
                lst.append(rule)
            write_json_atomic(self.path, d, indent=2)
            self.stamp = None

    def ensure(self):
        """Create an empty settings file (for /permissions) and return its path."""
        if not self.path.exists():
            write_json_atomic(self.path, {"allow": {"commands": [], "sites": []},
                                          "deny": {"commands": [], "sites": []}, "folders": {}}, indent=2)
        return self.path

    def describe(self):
        lines = []
        for verdict in ("allow", "deny"):
            for what in ("commands", "sites"):
                rs = self.rules(verdict, what)
                if rs:
                    lines.append(f"  {verdict} {what}: " + ", ".join(rs))
        return lines


SETTINGS = Settings(SETTINGS_FILE)


# ================================================================ output helpers

def say(msg, style=None, one_line=False):
    """Print a str or rich renderable; sub-agent threads get their label as a prefix."""
    prefix = getattr(_tls, "prefix", "")
    with IO:
        if not RICH:
            print(prefix + str(msg))
        elif isinstance(msg, str):
            if prefix:
                msg = "\n".join(prefix + l for l in msg.lstrip("\n").split("\n"))
            console.print(msg, style=style, markup=False, highlight=False,
                          no_wrap=one_line, overflow="ellipsis" if one_line else None)
        else:
            if prefix and isinstance(msg, Text):
                msg = Text.assemble((prefix, "dim"), msg)
            console.print(msg, highlight=False, no_wrap=one_line,
                          overflow="ellipsis" if one_line else None)


class working:
    """Spinner while a slow tool runs (main thread only, so it never fights a prompt)."""

    def __init__(self, text):
        self.text, self.status = text, None

    def __enter__(self):
        self.prev = None
        if LIVE.active:
            if getattr(_tls, "label", ""):     # sub-agents: keep the main status line
                return self
            self.prev, self.live = LIVE.status, True
            LIVE.status = Activity(self.text)
        elif RICH and sys.stdout.isatty() and threading.current_thread() is threading.main_thread():
            self.status = console.status(Text(self.text, style=GREY), spinner="dots",
                                         spinner_style=GOLD)
            self.status.__enter__()
        return self

    def __exit__(self, *exc):
        if getattr(self, "live", False):
            LIVE.status = self.prev
        if self.status:
            self.status.__exit__(*exc)


class Thinking:
    """Live view of a streaming reply: the text so far plus an animated status line
    ('Thinking…' until the first token, then 'Responding…') with live token counts."""
    FRAMES = "·✢✳✶✻✽✻✶✳✢"

    def __init__(self):
        self.start = time.time()
        self.body = None        # rendered markdown of the reply so far
        self.out = 0            # estimated output tokens of this reply
        self.tool = ""          # name of a tool call being written

    def parts(self):
        """(spinner frame + verb, details) of the status line."""
        t = time.time() - self.start
        verb = f"Writing {self.tool} call… " if self.tool else "Responding… " if self.out else "Thinking… "
        bits = [f"{int(t)}s"]
        if self.out:
            bits.append(f"↓ ~{human(self.out)} tokens")
        if USAGE.total():
            bits.append(f"session {human(USAGE.total())}")
        if USAGE.ctx_text():
            bits.append(USAGE.ctx_text())
        bits.append("ctrl+c to interrupt")
        return self.FRAMES[int(t * 8) % len(self.FRAMES)] + " " + verb, f"({' · '.join(bits)})"

    def status(self):
        head, tail = self.parts()
        return Text.assemble((head[:2], GOLD), (head[2:], f"bold {GOLD}"), (tail, GREY))

    def __rich__(self):
        if self.body is None:
            return self.status()
        return Group(self.body, Text(""), self.status())

    def line(self):
        """The status as prompt_toolkit text (live prompt mode)."""
        head, tail = self.parts()
        return [("class:spin", head), ("class:hint", tail)]


def with_gutter(renderable, mark="●", style="bold"):
    """Claude-Code-style '● ' gutter in front of a block."""
    g = Table.grid(padding=(0, 1))
    g.add_column(width=1, no_wrap=True)
    g.add_column(ratio=1)
    g.add_row(Text(mark, style=style), renderable)
    return g


def short_path(p, n=44):
    s = str(p).replace(str(HOME), "~", 1)
    if len(s) <= n:
        return s
    sep = "\\" if WINDOWS and "\\" in s else "/"
    parts = s.split(sep)
    tail = parts[-1]
    for part in reversed(parts[1:-1]):
        if len(part) + len(tail) + 3 > n - len(parts[0]):
            break
        tail = part + sep + tail
    return f"{parts[0]}{sep}…{sep}{tail}"[: n]


def welcome(conv, perms=None):
    where = short_path(Path.cwd())
    if not RICH:
        print(f"tai · {conv['model']} · {where} · /help for commands, Ctrl+C twice to quit")
        return
    try:
        import pwd
        name = (pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0] or os.environ.get("USER", "")).split()[0]
    except Exception:
        name = os.environ.get("USER", "there")
    logo = Text()
    for i, (chars, colors) in enumerate(LOGO):
        for ch, k in zip(chars, colors):
            logo.append(ch, style=GOLD if k == "g" else BLUE if k == "b" else None)
        if i < len(LOGO) - 1:
            logo.append("\n")
    word = Text.assemble(("TILBURG", f"bold {BLUE}"), (" ◆ ", GOLD), ("UNIVERSITY", f"bold {BLUE}"))
    left = Group(Align.center(logo), Text(""), Align.center(word))
    mode = "agent · files, shell, web, sub-agents" if conv["tools"] else "chat · no tools"
    info = Group(
        Text.assemble(("Welcome back, ", "bold"), (name, f"bold {GOLD}"), ("!", "bold")),
        Text(""),
        Text.assemble(("model  ", GREY), (conv["model"], "bold")),
        Text.assemble(("mode   ", GREY), mode),
        *([Text.assemble(("perms  ", GREY), (perms.mode, f"bold {GOLD}" if perms.mode != "manual" else ""),
                         (" · shift+tab to cycle", GREY))] if perms and conv["tools"] else []),
        Text.assemble(("folder ", GREY), where),
        Text(""),
        Text("Getting started", style=f"bold {GOLD}"),
        Text.assemble(("· ", GREY), "Ask a question or describe a task"),
        Text.assemble(("· ", GREY), ("/", "bold"), " for commands, ", ("/model", "bold"), " to switch"),
        Text.assemble(("· ", GREY), ("alt+enter", "bold"), " new line · ", ("ctrl+c ×2", "bold"), " quit"),
    )
    if console.width >= 72:
        body = Table.grid(padding=(0, 4))
        body.add_column(no_wrap=True)
        body.add_column()
        body.add_row(left, info)
    else:
        body = Group(left, Text(""), info)
    console.print(Panel(body, box=box.ROUNDED, border_style=GOLD, padding=(1, 2), expand=False,
                        title=Text.assemble((" tai ", f"bold {GOLD}"), ("· Tilburg.ai ", GREY)),
                        title_align="left"))


def dim(msg):
    say(msg, "dim")


def err(msg):
    say(msg, "red")


def clip(s, n=100):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def human(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


# ================================================================ API

def stored_key():
    try:
        return KEY_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def api_key():
    key = os.environ.get("TILBURG_AI_API_KEY") or stored_key()
    if not key:
        sys.exit("tai: no API key. Run `tai --set-key` once, or set TILBURG_AI_API_KEY.")
    return key


def set_key():
    import getpass
    try:
        key = getpass.getpass("Tilburg.ai API key (input hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)
    if not key:
        sys.exit("tai: no key entered, nothing changed")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # create it private from the start (POSIX); on Windows the per-user folder is private already
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    if not WINDOWS:
        os.chmod(KEY_FILE, 0o600)
    print(f"Saved to {KEY_FILE}. ($TILBURG_AI_API_KEY, when set, takes precedence.)")


def request(path, payload=None):
    req = urllib.request.Request(
        API_ENDPOINT + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"},
    )
    try:
        return urllib.request.urlopen(req, timeout=600)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            body = json.loads(body).get("error", {}).get("message", body)
        except (ValueError, AttributeError):
            pass
        raise RuntimeError(f"HTTP {e.code}: {body}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"connection failed: {e.reason}") from None


class tracked:
    """Context manager around an API response: registered in STREAMS while open."""

    def __init__(self, resp):
        self.resp = resp

    def __enter__(self):
        with STREAMS_LOCK:
            STREAMS.add(self.resp)
        return self.resp

    def __exit__(self, *exc):
        with STREAMS_LOCK:
            STREAMS.discard(self.resp)
        try:
            self.resp.close()
        except Exception:
            pass


def abort_streams():
    """Shut down the sockets of all open API responses; blocked reads return at once."""
    with STREAMS_LOCK:
        resps = list(STREAMS)
    for r in resps:
        try:
            sock = r.fp.raw._sock
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
        except Exception:
            pass


def list_models():
    with request("/models") as r:
        data = json.load(r)["data"]
    return sorted(data, key=lambda m: (m.get("mode") == "embedding", m["id"].lower()))


def stream_reply(model, messages, tools, tool_choice=None):
    """Yield ("text", chunk) events, then ("done", (tool_calls, usage))."""
    payload = {"model": model, "messages": messages, "stream": True,
               "stream_options": {"include_usage": True}}
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
    calls, by_index, usage = [], {}, None
    with tracked(request("/chat/completions", payload)) as resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if chunk.get("error"):
                raise RuntimeError(str(chunk["error"]))
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    yield "text", delta["content"]
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", len(calls) - 1 if calls and not tc.get("id") else len(calls))
                    entry = by_index.get(idx)
                    if entry is None or (tc.get("id") and entry["id"] and tc["id"] != entry["id"]):
                        entry = {"id": "", "name": "", "arguments": ""}
                        by_index[idx] = entry
                        calls.append(entry)
                    if tc.get("id"):
                        entry["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    entry["name"] += fn.get("name") or ""
                    entry["arguments"] += fn.get("arguments") or ""
                    if fn.get("arguments"):
                        yield "tool", (entry["name"], fn["arguments"])
    yield "done", (calls, usage)


def est_tokens(s):
    return max(1, len(s) // 4)


# ---- images: sent to the model as image_url parts (data URLs) in user messages and tool results
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
MAX_IMAGE = 3_750_000       # bytes: about 5 MB as base64, the limit of several models
IMAGE_TOKENS = 1600         # rough cost of one image in the context window
DATA_URL = re.compile(r"data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/=\s]+)$")


def is_image(path):
    return Path(path).suffix.lower() in IMAGE_TYPES


def image_part(data, mime):
    import base64
    if len(data) > MAX_IMAGE:
        raise ToolError(f"the image is too large ({len(data) / 1e6:.1f} MB; at most {MAX_IMAGE / 1e6:.1f} MB)")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}


def image_file_part(path):
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        raise ToolError(f"file not found: {show_path(path)}")
    except OSError as e:
        raise ToolError(f"cannot read {show_path(path)}: {e.strerror}")
    return image_part(data, IMAGE_TYPES[Path(path).suffix.lower()]), len(data)


def data_url_part(url):
    """An image pasted in the editor (a data: URL) as an image part; ValueError if it isn't one."""
    import base64
    m = DATA_URL.match(str(url or "").strip())
    if not m:
        raise ValueError("not a PNG, JPEG, GIF or WebP image")
    return image_part(base64.b64decode(m.group(2)), m.group(1))


def with_images(text, images):
    """Message content: the text alone, or text and image parts."""
    if not images:
        return text
    return ([{"type": "text", "text": text}] if text else []) + list(images)


def join_content(items):
    """Joins message contents (strings or part lists) into one."""
    texts, images = [], []
    for c in items:
        if isinstance(c, list):
            t = "".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
            images += [p for p in c if isinstance(p, dict) and p.get("type") == "image_url"]
        else:
            t = c or ""
        if t:
            texts.append(t)
    return with_images("\n\n".join(texts), images)


def count_images(obj):
    if isinstance(obj, dict):
        return (obj.get("type") == "image_url") + sum(count_images(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(count_images(v) for v in obj)
    return 0


def without_images(obj, note="[image]"):
    """A copy with image parts replaced by a short text (for size estimates and cut-down requests)."""
    if isinstance(obj, dict):
        if obj.get("type") == "image_url":
            return {"type": "text", "text": note}
        return {k: without_images(v, note) for k, v in obj.items()}
    if isinstance(obj, list):
        return [without_images(v, note) for v in obj]
    return obj


def msgs_tokens(msgs):
    return est_tokens(json.dumps(without_images(msgs))) + IMAGE_TOKENS * count_images(msgs)


# ================================================================ permissions

class Denied(Exception):
    pass


class Blocked(Denied):
    """Refused by a deny rule in settings.json (the user was not asked)."""


class ToolError(Exception):
    pass


class BadPath(ToolError, ValueError):
    """An impossible path: a tool error, and a ValueError for callers that only display paths."""


class Permissions:
    def __init__(self, mode="manual"):
        self.mode = mode
        self.always = set()
        self.on_mode = None             # called when tai itself changes the mode (plan approved)

    def cycle(self):
        self.mode = MODES[(MODES.index(self.mode) + 1) % len(MODES)]
        return self.mode

    def set_mode(self, mode):
        self.mode = mode
        if self.on_mode:
            self.on_mode(mode)

    def auto_ok(self, kind, detail=None):
        detail = detail or {}
        if kind == "edit" and self.mode in ("auto", "accept-edits") and detail.get("path") \
                and SANDBOX.enabled() and outside_workspace(detail["path"]):
            return False                    # edits outside the workspace ask
        if self.mode == "auto":
            if kind == "bash" and detail.get("outside_sandbox") and SANDBOX.active():
                return False
            return kind != "read"           # secret files always ask
        return self.mode == "accept-edits" and kind == "edit"

    def preflight(self, kind, detail):
        """Before asking: raises Blocked (deny rule) or Denied (cancelled); True when the
        request is already allowed (this session, the mode, or a saved rule)."""
        if CANCEL.is_set():
            raise Denied("Cancelled.")
        if self.mode == "plan" and kind in ("edit", "bash"):
            raise ToolError("plan mode is on: tai changes nothing until the user approves a plan; "
                            "finish the plan and present it with present_plan")
        rule = SETTINGS.denied(kind, detail)
        if rule:
            raise Blocked(rule)
        return kind in self.always or self.auto_ok(kind, detail) or SETTINGS.allowed(kind, detail)

    def always_choice(self, kind, detail):
        """(text after "Yes, and don't ask again ", what to do when chosen)."""
        if kind == "bash":
            rule = command_rule(str((detail or {}).get("command") or ""))
            if rule:
                return (f"for `{rule}` commands in this folder",
                        lambda: self._save("commands", rule, Path.cwd(), "bash"))
            return "for any command this session", lambda: self.always.add("bash")
        if kind.startswith("fetch:"):
            host = kind[6:]
            return f"for {host}", lambda: self._save("sites", host, None, kind)
        if kind == "plan":
            return "", lambda: self.set_mode("accept-edits")
        text = {"edit": "for file edits this session", "read": "for sensitive files this session"}.get(
            kind, "this session")
        return text, lambda: self.always.add(kind)

    def _save(self, what, rule, folder, kind):
        try:
            SETTINGS.add("allow", what, rule, folder)
        except OSError as e:
            self.always.add(kind)
            err(f"tai: could not save the rule ({e}); allowed for this session only")

    def check(self, kind, question, title="Permission", body=None, detail=None):
        """kind: 'edit', 'bash', 'read' or 'fetch:<host>'. Raises Denied unless approved.
        Holds IO so the prompt isn't interleaved with sub-agent output (in the live prompt
        the question is the input line itself, so only the panel is printed under IO)."""
        if LIVE.active and threading.current_thread() is not threading.main_thread():
            with APPROVAL:
                return self._check(kind, question, title, body, detail, live=True)
        with IO:
            return self._check(kind, question, title, body, detail, live=False)

    def _check(self, kind, question, title, body, detail, live):
        if self.preflight(kind, detail):
            if kind == "edit" and body is not None and kind not in self.always:
                say(body)               # still show what changed
            return
        label = getattr(_tls, "label", "")
        always, remember = self.always_choice(kind, detail)
        notify_waiting()
        yes, yes2, no = choice_labels(kind, always)
        if RICH:
            items = []
            if body is not None:
                items += [body, Text("")]
            items += [Text(question, style="bold"),
                      Text.assemble(("  1. ", GOLD), yes),
                      Text.assemble(("  2. ", GOLD), yes2),
                      Text.assemble(("  3. ", GOLD), no)]
            ttl = Text.assemble((f" {label} › " if label else " ", GREY), (title + " ", f"bold {GOLD}"))
            with IO:
                console.print(Panel(Group(*items), title=ttl, title_align="left", box=box.ROUNDED,
                                    border_style=GOLD, padding=(0, 1)))
        else:
            with IO:
                if body is not None:
                    print(body)
                print(f"{question}  1 = yes, 2 = always {always}, 3 = no")
        prompt = f"  {question} 1 yes · 2 always · 3 no ❯ " if live else "  ❯ "
        while True:
            try:
                ans = ask_line(prompt).strip().lower()
            except EOFError:
                raise Denied("Cancelled." if CANCEL.is_set() else
                             "No terminal available to approve this action.")
            if ans in ("1", "y", "yes", "2", "a", "always", "3", "n", "no"):
                break
            say("  type 1, 2 or 3", GREY)
        if ans in ("2", "a", "always"):
            remember()
            return
        if ans in ("1", "y", "yes"):
            return
        try:
            why = ask_line("  What should tai change in the plan? (Enter to skip) ❯ " if kind == "plan" else
                           "  What should tai do instead? (Enter to skip) ❯ ").strip()
        except EOFError:
            why = ""
        raise Denied(why)


def choice_labels(kind, always):
    """The three answers to an approval question."""
    if kind == "plan":
        return ("Yes, start (file edits and commands still ask)", "Yes, and let file edits run without asking",
                "No, keep planning, and tell tai what to change")
    return "Yes", f"Yes, and don't ask again {always}".strip(), "No, and tell tai what to do differently"


# ================================================================ notifications
# After a long turn (and when an approval waits after one), the terminal rings its bell;
# settings.json "notify": "desktop" also shows a desktop notification, "off" turns it off.
NOTIFY_AFTER = 30                   # seconds
TURN_CLOCK = {"start": None}
WIN_TOAST = r"""$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $x.GetElementsByTagName('text')
$t.Item(0).AppendChild($x.CreateTextNode($env:TAI_NOTIFY_TITLE)) | Out-Null
$t.Item(1).AppendChild($x.CreateTextNode($env:TAI_NOTIFY_BODY)) | Out-Null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))"""


def notify_setting():
    v = SETTINGS.load().get("notify", "bell")
    return v if v in ("bell", "desktop", "off") else "bell"


def desktop_notify(title, body):
    """Show a desktop notification (macOS, Windows, Linux with notify-send). False if it can't."""
    env = None
    if sys.platform == "darwin":
        q = lambda x: json.dumps(x, ensure_ascii=False)
        argv = ["osascript", "-e", f"display notification {q(body)} with title {q(title)}"]
    elif WINDOWS:
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", WIN_TOAST]
        env = {**os.environ, "TAI_NOTIFY_TITLE": title, "TAI_NOTIFY_BODY": body}
    else:
        exe = shutil.which("notify-send")
        if not exe:
            return False
        argv = [exe, "-a", "tai", title, body]
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=env, **NO_WINDOW)
        return True
    except OSError:
        return False


def notify_user(what):
    mode = notify_setting()
    if mode == "off":
        return
    out = sys.__stdout__
    if out is not None and out.isatty():
        try:
            out.write("\a")
            out.flush()
        except (OSError, ValueError):
            pass
    if mode == "desktop":
        desktop_notify("tai", f"{what} · {Path.cwd().name}")


def notify_done():
    start, TURN_CLOCK["start"] = TURN_CLOCK["start"], None
    if start and time.time() - start >= NOTIFY_AFTER:
        notify_user("tai has finished")


def notify_waiting():
    """An approval question: ring if the turn has been running a while (then not again soon)."""
    start = TURN_CLOCK["start"]
    if start and time.time() - start >= NOTIFY_AFTER:
        TURN_CLOCK["start"] = time.time()
        notify_user("tai is waiting for your approval")


# ================================================================ file tools

def resolve(p):
    if "\0" in str(p):        # Windows path functions accept it silently; never a real file name
        raise BadPath("invalid path (contains a null character)")
    path = Path(os.path.expanduser(str(p)))
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def show_path(path):
    cwd = Path.cwd()
    # resolve() expands links (macOS /var → /private/var) and Windows short names (RUNNER~1)
    for p, base in ((path, cwd), (path, cwd.resolve()), (path.resolve(), cwd.resolve())):
        try:
            return p.relative_to(base).as_posix()     # src/m.py on every OS
        except (ValueError, OSError):
            pass
    return str(path).replace(str(HOME), "~", 1)


def is_sensitive(path):
    if path.name.startswith(".env"):
        return True
    return any(path == s or s in path.parents for s in SENSITIVE)


def is_binary(path):
    try:
        with open(path, "rb") as f:
            return b"\0" in f.read(8192)
    except OSError:
        return False


def decode(data):
    """bytes -> (text with \\n line ends, encoding, newline) so edits can be written back the same way."""
    nl = "\r\n" if data.count(b"\r\n") * 2 > data.count(b"\n") else "\n"
    for enc in ("utf-8-sig" if data.startswith(b"\xef\xbb\xbf") else "utf-8", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return text.replace("\r\n", "\n").replace("\r", "\n"), enc, nl


def text_format(path):
    """(encoding, newline) of an existing file, defaults for a new one."""
    try:
        with open(path, "rb") as f:
            return decode(f.read())[1:]
    except OSError:
        return "utf-8", "\n"


def write_text(path, text, fmt=("utf-8", "\n")):
    enc, nl = fmt
    try:
        data = text.replace("\r\n", "\n").replace("\n", nl).encode(enc)
    except UnicodeEncodeError as e:
        raise ToolError(f"{show_path(path)} is saved as {enc}, which cannot store "
                        f"{e.object[e.start:e.end]!r}; use characters that exist in {enc}")
    path.write_bytes(data)


def read_text(path):
    try:
        return decode(path.read_bytes())[0]
    except FileNotFoundError:
        raise ToolError(f"file not found: {show_path(path)}")
    except IsADirectoryError:
        raise ToolError(f"{show_path(path)} is a directory (use list_files)")
    except OSError as e:
        raise ToolError(f"cannot read {show_path(path)}: {e.strerror}")


def tool_read_file(a, perms):
    path = resolve(a["path"])
    if is_sensitive(path):
        perms.check("read", "This file may contain secrets. Send it to the model?",
                    "Read sensitive file", show_path(path), detail={"type": "read", "path": str(path)})
    if is_image(path) and path.is_file():
        part, size = image_file_part(path)
        return ([{"type": "text", "text": f"The image {show_path(path)} ({human(size)}B):"}, part],
                f"Read image ({human(size)}B)")
    images = []
    if path.is_file() and path.suffix.lower() == ".ipynb":
        lines, images = notebook_view(path)
    elif path.is_file() and path.suffix.lower() in DOC_TYPES:
        lines = document_text(path).splitlines()
    else:
        if path.is_file() and is_binary(path):
            raise ToolError(f"{show_path(path)} is a binary file")
        lines = read_text(path).splitlines()
    start = max(int(a.get("offset") or 1), 1)
    limit = int(a.get("limit") or 2000)
    chunk = lines[start - 1: start - 1 + limit]
    body = "\n".join(f"{n:>6}\t{l[:2000]}" for n, l in enumerate(chunk, start))
    more = len(lines) - (start - 1 + len(chunk))
    if more > 0:
        body += f"\n[... {more} more lines; use offset={start + len(chunk)} to continue]"
    shown = [(i, cell, part) for i, cell, part in images if start - 1 <= i < start - 1 + len(chunk)]
    if not shown:
        return body or "(empty file)", f"Read {len(chunk)} lines"
    content = [{"type": "text", "text": body}]
    for k, (i, cell, part) in enumerate(shown[:NB_IMAGES]):
        content += [{"type": "text", "text": f"[{lines[i][1:-1]}, output of cell {cell}]:"}, part]
    if len(shown) > NB_IMAGES:
        content.append({"type": "text", "text": f"[{len(shown) - NB_IMAGES} more images not shown; read "
                                                "a smaller range with offset/limit to see them]"})
    return content, f"Read {len(chunk)} lines, {min(len(shown), NB_IMAGES)} images"


def walk_files(base, name_glob=None):
    if base.is_file():
        yield base
        return
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for f in sorted(files):
            if name_glob is None or fnmatch.fnmatch(f, name_glob):
                yield Path(root) / f


def tool_list_files(a, perms):
    base = resolve(a.get("path") or ".")
    if not base.is_dir():
        raise ToolError(f"not a directory: {show_path(base)}")
    pattern = a.get("pattern") or "*"
    out = []
    for p in base.glob(pattern):
        if SKIP_DIRS.intersection(p.relative_to(base).parts):
            continue
        out.append(p.relative_to(base).as_posix() + ("/" if p.is_dir() else ""))
        if len(out) >= 1000:
            break
    out.sort()
    body = "\n".join(out) if out else "(no matches)"
    if len(out) >= 1000:
        body += "\n[truncated at 1000 entries; use a narrower pattern]"
    return body, f"{len(out)} entries"


def tool_grep(a, perms):
    try:
        rx = re.compile(a["pattern"], re.I if a.get("ignore_case") else 0)
    except re.error as e:
        raise ToolError(f"bad regex: {e}")
    base = resolve(a.get("path") or ".")
    if not base.exists():
        raise ToolError(f"no such path: {show_path(base)}")
    hits = []
    for f in walk_files(base, a.get("glob")):
        if is_sensitive(f) or is_binary(f):
            continue
        try:
            if f.stat().st_size > 5_000_000:
                continue
            with open(f, encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    if rx.search(line):
                        hits.append(f"{show_path(f)}:{n}: {line.rstrip()[:300]}")
                        if len(hits) >= 300:
                            break
        except OSError:
            continue
        if len(hits) >= 300:
            break
    body = "\n".join(hits) if hits else "(no matches)"
    if len(hits) >= 300:
        body += "\n[stopped at 300 matches; narrow the search]"
    return body, f"{len(hits)} matches"


ADD_BG, DEL_BG = "on #1f3d24", "on #4a1f22"   # dark green / dark red, readable in any theme


def diff_view(old, new, label, max_lines=120):
    """Claude-Code-style diff: line numbers, colored backgrounds. Returns a renderable (or str)."""
    old_l, new_l = ([] if old is None else old.splitlines()), new.splitlines()
    rows = []   # (old_no, new_no, sign, text)
    for line in difflib.unified_diff(old_l, new_l, lineterm="", n=2):
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)", line)
            o, n = int(m.group(1)), int(m.group(2))
            if rows:
                rows.append((None, None, "…", ""))
            continue
        sign, text = line[0], line[1:]
        if sign == "-":
            rows.append((o, None, "-", text)); o += 1
        elif sign == "+":
            rows.append((None, n, "+", text)); n += 1
        else:
            rows.append((o, n, " ", text)); o += 1; n += 1
    added = sum(r[2] == "+" for r in rows)
    removed = sum(r[2] == "-" for r in rows)
    head = (f"{label}  (new file, {added} lines)" if old is None
            else f"{label}  (+{added} −{removed})")
    if not RICH:
        return head + "\n" + "\n".join(f"{r[2]} {r[3]}" for r in rows[:max_lines])
    out = [Text(head, style="bold")]
    width = max(len(str(len(new_l))), len(str(len(old_l))), 2)
    for o, n, sign, text in rows[:max_lines]:
        if sign == "…":
            out.append(Text(" " * width + "  ⋮", style=GREY))
            continue
        num = str(n if n is not None else o).rjust(width)
        style = ADD_BG if sign == "+" else DEL_BG if sign == "-" else ""
        t = Text.assemble((num + " ", GREY), (f"{sign} {text}", style))
        t.no_wrap, t.overflow = True, "ellipsis"
        out.append(t)
    if len(rows) > max_lines:
        out.append(Text(f"… {len(rows) - max_lines} more lines", style=GREY))
    return Group(*out)


def write_after_approval(path, old, new, perms, verb, body=None):
    label = show_path(path)
    if old is not None and old == new:
        raise ToolError("the new content is identical to the current file")
    detail = {"type": "edit", "path": str(path), "old": old, "new": new}
    if old is None:
        perms.check("edit", f"Create {label}?", "Create file", body or diff_view(None, new, label), detail=detail)
    else:
        perms.check("edit", f"{verb} {label}?", "Edit file", body or diff_view(old, new, label), detail=detail)
    try:
        now = read_text(path) if path.exists() else None
    except ToolError:
        now = None
    if now != old:
        raise ToolError(f"{label} changed while waiting for approval; read it again before editing")
    fmt = text_format(path) if old is not None else ("utf-8", "\n")
    if IO_HOOKS["before_write"]:
        IO_HOOKS["before_write"](path, old)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text(path, new, fmt)
    except OSError as e:
        raise ToolError(f"cannot write {label}: {e.strerror}")
    if IO_HOOKS["after_write"]:
        IO_HOOKS["after_write"](path, old, new)


def tool_write_file(a, perms):
    path = resolve(a["path"])
    old = read_text(path) if path.exists() else None
    write_after_approval(path, old, a["content"], perms, "Overwrite")
    return f"Wrote {show_path(path)}", f"{'Created' if old is None else 'Overwrote'} {show_path(path)}"


def tool_edit_file(a, perms):
    path = resolve(a["path"])
    if path.suffix.lower() == ".ipynb":
        raise ToolError("this is a Jupyter notebook: change it with edit_notebook")
    old = read_text(path)
    find, repl = a["old_string"], a["new_string"]
    count = old.count(find) if find else 0
    if count == 0:
        raise ToolError("old_string not found in file (it must match exactly, including whitespace); "
                        "read the file again")
    if count > 1 and not a.get("replace_all"):
        raise ToolError(f"old_string occurs {count} times; add more context to make it unique, "
                        "or set replace_all")
    new = old.replace(find, repl) if a.get("replace_all") else old.replace(find, repl, 1)
    write_after_approval(path, old, new, perms, "Apply this edit to")
    return f"Edited {show_path(path)}", f"Updated {show_path(path)}"


def tool_bash(a, perms):
    cmd = a["command"]
    bg = bool(a.get("run_in_background"))
    outside = bool(a.get("outside_sandbox"))
    body = Text(cmd.strip(), style="bold") if RICH else cmd
    sandbox = perms.mode == "auto" and SANDBOX.active()
    question = "Run this command in the background?" if bg else "Run this command?"
    if sandbox and outside:
        question = question[:-1] + " outside the sandbox (with network, writing anywhere)?"
    perms.check("bash", question,
                f"{SHELL_LABEL} command" + (" · background" if bg else "") + (" · outside sandbox" if sandbox and outside else ""),
                body, detail={"type": "bash", "command": cmd, "shell": SHELL_LABEL, "background": bg,
                              "outside_sandbox": outside})
    sandboxed = sandbox and not outside and perms.mode == "auto"    # (the mode may change while asking)
    if bg:
        job = start_job(cmd, sandboxed)
        return (f"Started in the background as job {job.id}. It keeps running while you continue; "
                f"read its output with job_output (job_id \"{job.id}\", optionally wait=seconds), stop it "
                "with job_stop. It is stopped when tai exits; you are told when it finishes.",
                f"Started job {job.id} in the background")
    timeout = min(max(int(a.get("timeout") or 120), 1), 600)
    with working(f"Running {clip(cmd, 50)}…"):
        return _run_bash(cmd, timeout, sandboxed)


def shell_argv(cmd):
    if WINDOWS:
        # PowerShell; make its own output UTF-8 so we can decode it
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        pre = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $OutputEncoding = [Text.Encoding]::UTF8; "
        return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", pre + cmd]
    return [shutil.which("bash") or "/bin/sh", "-c", cmd]


def kill_tree(proc):
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, **NO_WINDOW)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def spawn_shell(cmd, sandboxed=False):
    """Start cmd in tai's shell: output (stdout + stderr) on a pipe, own process group, no
    secret environment variables, no console window; inside the sandbox if sandboxed."""
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    extra = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if WINDOWS
             else {"start_new_session": True})
    argv = shell_argv(cmd)
    if sandboxed:
        argv = SANDBOX.wrap(argv)
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                            env=env, **extra)


# ================================================================ sandbox
# In auto mode, commands run in a sandbox where they can write only in the workspace folder and
# the temp folder, and can't open network connections. A command that needs more is run with
# outside_sandbox, which asks the user. Linux: Landlock (kernel 5.13+; network rules 6.7+),
# applied by a small helper that then starts the shell. macOS: sandbox-exec. Windows: none.

LANDLOCK_HELPER = r"""
import ctypes, json, os, struct, sys
spec, argv = json.loads(sys.argv[1]), sys.argv[2:]
libc = ctypes.CDLL(None, use_errno=True)
sc = libc.syscall
sc.restype = ctypes.c_long
def fail(what):
    sys.stderr.write("tai sandbox: %s failed (%s)\n" % (what, os.strerror(ctypes.get_errno())))
    sys.exit(126)
abi = sc(444, None, 0, 1)
if abi < 1:
    fail("Landlock")
fs = 0x2 | 0x10 | 0x20 | 0x40 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800 | 0x1000
fs |= (0x2000 if abi >= 2 else 0) | (0x4000 if abi >= 3 else 0)
net = 0x3 if abi >= 4 and not spec["network"] else 0
attr = struct.pack("QQ", fs, net) if abi >= 4 else struct.pack("Q", fs)
fd = sc(444, ctypes.create_string_buffer(attr, len(attr)), len(attr), 0)
if fd < 0:
    fail("creating the rules")
for p in spec["write"]:
    try:
        pfd = os.open(p, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        continue
    access = fs if os.path.isdir(p) else fs & 0x4002
    rule = struct.pack("=Qi", access, pfd)
    if sc(445, fd, 1, ctypes.create_string_buffer(rule, len(rule)), 0) != 0:
        fail("a rule for " + p)
    os.close(pfd)
if libc.prctl(38, 1, 0, 0, 0) != 0 or sc(446, fd, 0) != 0:
    fail("Landlock")
os.close(fd)
os.execvp(argv[0], argv)
"""

SEATBELT = """(version 1)
(allow default)
(deny file-write*)
(allow file-write* (subpath "/dev") %s)
%s"""


class Sandbox:
    def __init__(self):
        self._kind = False

    @property
    def kind(self):
        """'Landlock', 'macOS sandbox' or None (not available on this system)."""
        if self._kind is False:
            self._kind = None
            if sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec"):
                self._kind = "macOS sandbox"
            elif sys.platform.startswith("linux"):
                try:
                    import ctypes
                    libc = ctypes.CDLL(None, use_errno=True)
                    libc.syscall.restype = ctypes.c_long
                    self.abi = libc.syscall(444, None, 0, 1)
                    if self.abi >= 1:
                        self._kind = "Landlock"
                except (OSError, AttributeError):
                    pass
        return self._kind

    def settings(self):
        d = SETTINGS.load().get("sandbox")
        d = d if isinstance(d, dict) else {}
        extra = d.get("writable")
        return {"enabled": d.get("enabled", True) is not False, "network": d.get("network") is True,
                "writable": [str(x) for x in extra] if isinstance(extra, list) else []}

    def enabled(self):
        """Edits outside the workspace ask in auto mode (everywhere, also without an OS sandbox)."""
        return self.settings()["enabled"]

    def active(self):
        """Commands in auto mode run sandboxed."""
        return self.enabled() and self.kind is not None

    def network_blocked(self):
        return not self.settings()["network"] and (self.kind == "macOS sandbox" or getattr(self, "abi", 0) >= 4)

    def writable(self):
        st = self.settings()
        paths = [Path.cwd(), Path(tempfile.gettempdir()), Path("/tmp"), Path("/var/tmp")]
        paths += [Path(os.path.expanduser(x)) for x in st["writable"]]
        out = []
        for p in paths:
            r = os.path.realpath(p)
            if os.path.exists(r) and r not in out:
                out.append(r)
        return out

    def wrap(self, argv):
        st = self.settings()
        if self.kind == "Landlock":
            spec = json.dumps({"write": self.writable() + ["/dev"], "network": st["network"]})
            return [sys.executable, "-I", "-S", "-c", LANDLOCK_HELPER, spec] + argv
        if self.kind == "macOS sandbox":
            ws = self.writable()
            allow = " ".join(f'(subpath (param "W{i}"))' for i in range(len(ws)))
            net = "" if st["network"] else '(deny network-outbound (remote ip "*:*"))'
            params = [x for i, w in enumerate(ws) for x in ("-D", f"W{i}={w}")]
            return ["/usr/bin/sandbox-exec", "-p", SEATBELT % (allow, net)] + params + argv
        raise ToolError("no sandbox is available on this system")

    def describe(self):
        """One line for /permissions and the editor."""
        if not self.enabled():
            return "sandbox: off (settings.json) — in auto mode commands run without limits"
        if not self.kind:
            return ("sandbox: not available on this system — in auto mode commands run without limits "
                    "(file edits outside this folder still ask)")
        net = "no network" if self.network_blocked() else "network allowed"
        return (f"sandbox: on ({self.kind}) — in auto mode commands can write only in this folder and "
                f"the temp folder, {net}; anything else asks")

    def note(self):
        """Appended to the output of a sandboxed command that failed."""
        net = "no network connections, " if self.network_blocked() else ""
        return (f"\n[This ran in tai's sandbox ({net}writes only in {Path.cwd()} and the temp folder). If the "
                "sandbox is why it failed, run it again with outside_sandbox: true; the user is asked.]")


SANDBOX = Sandbox()


def outside_workspace(path):
    """True for paths that auto mode's sandbox doesn't let tai edit without asking."""
    try:
        r = os.path.realpath(path)
    except (OSError, ValueError):
        return True
    roots = [os.path.realpath(Path.cwd()), os.path.realpath(tempfile.gettempdir())]
    roots += [os.path.realpath(os.path.expanduser(x)) for x in SANDBOX.settings()["writable"]]
    return not any(r == root or r.startswith(root.rstrip(os.sep) + os.sep) for root in roots)


def _run_bash(cmd, timeout, sandboxed=False):
    proc = spawn_shell(cmd, sandboxed)
    note, deadline = "", time.time() + timeout
    try:
        while True:
            try:
                out, _ = proc.communicate(timeout=0.3)
                break
            except subprocess.TimeoutExpired:
                if CANCEL.is_set() or time.time() > deadline:
                    kill_tree(proc)
                    out, _ = proc.communicate()
                    note = ("\n[killed: cancelled by the user]" if CANCEL.is_set()
                            else f"\n[killed: timed out after {timeout}s]")
                    break
    except KeyboardInterrupt:
        kill_tree(proc)
        proc.communicate()
        raise
    out = (out or "").rstrip()
    lines = out.splitlines()
    preview = "\n".join(lines[:5]) if lines else "(no output)"
    if len(lines) > 5:
        preview += f"\n… +{len(lines) - 5} lines"
    if proc.returncode:
        preview += f"\n[exit code {proc.returncode}]"
    hint = SANDBOX.note() if sandboxed and proc.returncode and not CANCEL.is_set() else ""
    if hint:
        preview += " · in the sandbox"
    return f"{out}{note}\n[exit code {proc.returncode}]{hint}", preview + note


# ================================================================ background jobs

MAX_JOBS = 8                    # running at the same time
JOB_KEEP = 1_000_000            # characters of output kept per job (the oldest are dropped)
JOBS = {}                       # id -> Job, for this tai process
JOBS_LOCK = threading.Lock()


class Job:
    """A command running in the background; its output is collected as it comes."""

    def __init__(self, jid, cmd, sandboxed=False):
        self.id, self.cmd, self.started, self.ended = jid, cmd, time.time(), None
        self.sandboxed = sandboxed
        self.out, self.base, self.seen = "", 0, 0     # base: characters dropped from the front
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.stopped = False
        self.announced = False      # the model was told that it finished
        self.proc = spawn_shell(cmd, sandboxed)
        threading.Thread(target=self._read, daemon=True, name=f"job-{jid}").start()

    def _read(self):
        try:
            for line in self.proc.stdout:
                with self.lock:
                    self.out += line
                    if len(self.out) > JOB_KEEP:
                        cut = len(self.out) - JOB_KEEP
                        self.out, self.base = self.out[cut:], self.base + cut
        except (OSError, ValueError):
            pass
        self.proc.wait()
        self.ended = time.time()
        self.done.set()
        hook = IO_HOOKS.get("job_done")
        if hook:
            try:
                hook(self)
            except Exception:
                pass

    @property
    def code(self):
        return self.proc.returncode if self.done.is_set() else None

    def state(self):
        if not self.done.is_set():
            return f"running for {human_secs(time.time() - self.started)}"
        how = "stopped" if self.stopped else f"exit code {self.code}"
        return f"finished ({how}) after {human_secs(self.ended - self.started)}"

    def take(self):
        """Output since the last take()."""
        with self.lock:
            start = max(self.seen, self.base)
            text = self.out[start - self.base:]
            lost = start - self.seen
            self.seen = self.base + len(self.out)
        return (f"[... {lost} older characters were dropped ...]\n" if lost else "") + text

    def tail(self, n=1):
        with self.lock:
            lines = self.out.rstrip().splitlines()
        return lines[-n:] if lines else []

    def stop(self):
        if not self.done.is_set():
            self.stopped = True
            kill_tree(self.proc)
            self.done.wait(10)

    def info(self):
        return {"id": self.id, "command": self.cmd, "running": not self.done.is_set(), "code": self.code,
                "stopped": self.stopped, "state": self.state(), "last": (self.tail() or [""])[0][:200]}


def human_secs(t):
    t = int(t)
    return f"{t}s" if t < 60 else f"{t // 60}m {t % 60:02d}s" if t < 3600 else f"{t // 3600}h {t % 3600 // 60:02d}m"


def start_job(cmd, sandboxed=False):
    with JOBS_LOCK:
        running = [j for j in JOBS.values() if not j.done.is_set()]
        if len(running) >= MAX_JOBS:
            raise ToolError(f"{MAX_JOBS} background jobs are already running; stop one with job_stop first")
        jid = f"b{len(JOBS) + 1}"
        try:
            job = JOBS[jid] = Job(jid, cmd, sandboxed)
        except OSError as e:
            raise ToolError(f"cannot start the command: {e}")
    return job


def find_job(jid):
    job = JOBS.get(str(jid or "").strip())
    if not job:
        known = ", ".join(JOBS) or "none"
        raise ToolError(f"no background job {jid!r} (jobs: {known})")
    return job


def tool_job_output(a, perms):
    job = find_job(a.get("job_id"))
    wait = min(max(int(a.get("wait") or 0), 0), 600)
    if wait:
        end = time.time() + wait
        with working(f"Waiting for job {job.id}…"):
            while not job.done.wait(0.2) and time.time() < end:
                if CANCEL.is_set():
                    break
    out = job.take()
    if job.done.is_set():
        job.announced = True
    text = out.rstrip() or "(no new output)"
    if len(text) > MAX_TOOL_OUTPUT:
        text = f"[... {len(text) - MAX_TOOL_OUTPUT} characters left out ...]\n" + text[-MAX_TOOL_OUTPUT:]
    lines = out.rstrip().splitlines()
    hint = SANDBOX.note() if job.sandboxed and job.code and not job.stopped else ""
    return (f"{text}\n[job {job.id}: {job.state()}]{hint}",
            f"{len(lines)} new line{'s' if len(lines) != 1 else ''} · {job.state()}")


def tool_job_stop(a, perms):
    job = find_job(a.get("job_id"))
    was = not job.done.is_set()
    job.stop()
    job.announced = True
    out = job.take().rstrip()
    return ((f"{out[-5000:]}\n" if out else "") + f"[job {job.id}: {job.state()}]",
            f"Stopped job {job.id}" if was else f"Job {job.id} had already {job.state()}")


def stop_all_jobs():
    for job in list(JOBS.values()):
        job.stop()


def finished_jobs_note():
    """A note for the model about background jobs that finished since it last looked."""
    done = [j for j in JOBS.values() if j.done.is_set() and not j.announced]
    for j in done:
        j.announced = True
    if not done:
        return None
    return "[Background " + "; ".join(f"job {j.id} ({clip(j.cmd, 80)}) {j.state()}" for j in done) + \
        ". job_output shows what it printed.]"


def jobs_command(arg):
    """/jobs, /jobs stop ID (terminal)."""
    words = arg.split()
    if words[:1] == ["stop"] and len(words) == 2:
        try:
            find_job(words[1]).stop()
        except ToolError as e:
            err(str(e))
            return
    elif words:
        err("usage: /jobs  or  /jobs stop ID")
        return
    if not JOBS:
        dim("no background jobs (tai starts one when a command should keep running, like a server)")
        return
    for j in JOBS.values():
        last = j.tail()
        dim(f"  {j.id}  {clip(j.cmd, 60)}  · {j.state()}" + (f"\n        {clip(last[0], 90)}" if last else ""))
    if any(not j.done.is_set() for j in JOBS.values()):
        dim("  /jobs stop ID stops one; all are stopped when tai exits")


# ================================================================ task list

TODO_STATUS = ("pending", "in_progress", "completed")
TODO_MARK = {"pending": "☐", "in_progress": "◼", "completed": "☒"}


def todo_text(items):
    return "\n".join(f"{TODO_MARK[i['status']]} {i['content']}" for i in items)


def tool_todo_write(a, perms):
    items = a.get("todos")
    if not isinstance(items, list):
        raise ToolError("todos must be a list of {content, status}")
    clean = []
    for it in items[:50]:
        if not isinstance(it, dict) or not str(it.get("content") or "").strip():
            raise ToolError("every item needs a content text and a status")
        if it.get("status") not in TODO_STATUS:
            raise ToolError(f"status must be one of {', '.join(TODO_STATUS)}")
        clean.append({"content": clip(" ".join(str(it["content"]).split()), 200), "status": it["status"]})
    hook = IO_HOOKS.get("todos")
    if hook:
        hook(clean)
    done = sum(i["status"] == "completed" for i in clean)
    note = ("" if done == len(clean) else
            " Keep it up to date: mark an item in_progress when you start it and completed as soon as it is done.")
    return (f"Task list saved ({done} of {len(clean)} done).{note}\n{todo_text(clean)}",
            todo_text(clean) or "(empty task list)")


# ================================================================ web tools

def tool_web_search(a, perms):
    query = a["query"]
    payload = {"model": SEARCH_MODEL, "tools": [{"type": "web_search"}],
               "input": f"Search the web for: {query}\n\nToday is {datetime.date.today():%d %B %Y}. "
                        "Report the relevant findings factually and in detail (numbers, dates, names, "
                        "exact titles), and cite your sources."}
    try:
        with working(f"Searching the web for {clip(query, 50)}…"), tracked(request("/responses", payload)) as r:
            d = json.load(r)
    except RuntimeError as e:
        raise ToolError(f"search failed: {e}")
    texts, sources = [], {}
    for item in d.get("output") or []:
        if item.get("type") != "message":
            continue
        for c in item.get("content") or []:
            if c.get("text"):
                texts.append(c["text"])
            for an in c.get("annotations") or []:
                if an.get("url"):
                    sources.setdefault(an["url"], an.get("title") or an["url"])
    body = "\n".join(texts) or "(no results)"
    if sources:
        body += "\n\nSources:\n" + "\n".join(f"- {t}: {u}" for u, t in sources.items())
    return body, f"{len(sources)} sources"


class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "section", "article", "header", "footer", "table",
             "ul", "ol", "pre", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd",
             "figcaption", "main", "nav", "aside", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
            if tag in ("h1", "h2", "h3"):
                self.out.append("#" * int(tag[1]) + " ")
            elif tag == "li":
                self.out.append("- ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html):
    p = _HTMLText()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    lines, blank = [], False
    for l in "".join(p.out).splitlines():
        l = " ".join(l.split())
        if l:
            lines.append(l)
            blank = False
        elif not blank:
            lines.append("")
            blank = True
    return "\n".join(lines).strip()


def pdf_to_text(data):
    exe = shutil.which("pdftotext")
    if not exe:
        raise ToolError("this is a PDF and pdftotext is not installed")
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        r = subprocess.run([exe, "-layout", f.name, "-"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120, **NO_WINDOW)
    if r.returncode:
        raise ToolError(f"pdftotext failed: {r.stderr.strip()[:200]}")
    return r.stdout


# ================================================================ documents
# read_file and attachments show Word, Excel, PowerPoint and Stata files and Jupyter notebooks
# as text, with the standard library only: Office files are zip files of XML, and the .dta
# format is documented by Stata. Notebook plots are shown to the model as images.
MAX_XML = 200_000_000           # bytes unpacked from one part of an Office file (zip bombs)
MAX_SHEET_ROWS = 20_000         # rows shown per worksheet
DTA_ROWS = 20                   # observations shown from a Stata file
NB_OUTPUT = 4000                # characters shown per notebook output
NB_IMAGES = 8                   # plots shown per read_file of a notebook
DOC_TYPES = {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx", ".dta", ".ipynb"}
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def document_text(path):
    """The text of a PDF, Word, Excel, PowerPoint, Stata or notebook file; None for other files."""
    ext = path.suffix.lower()
    if ext not in DOC_TYPES or not path.is_file():
        return None
    if ext == ".pdf":
        return pdf_to_text(path.read_bytes())
    if ext == ".ipynb":
        return "\n".join(notebook_view(path)[0])
    import struct
    import zipfile
    import xml.etree.ElementTree as ET
    read = {".docx": docx_text, ".xlsx": xlsx_text, ".xlsm": xlsx_text, ".pptx": pptx_text,
            ".dta": dta_text}[ext]
    try:
        return "\n".join(_wrap_long(read(path)))
    except zipfile.BadZipFile:
        raise ToolError(f"{show_path(path)} is not a valid {ext[1:]} file (is it password-protected?)")
    except (ET.ParseError, struct.error, KeyError, IndexError, ValueError, EOFError) as e:
        raise ToolError(f"cannot read {show_path(path)}: the file looks damaged ({e})")


def _wrap_long(lines, width=1500):
    """read_file cuts lines at 2000 characters: split long paragraphs at spaces instead."""
    for line in lines:
        while len(line) > width:
            cut = line.rfind(" ", 0, width)
            cut = cut if cut > width // 2 else width
            yield line[:cut]
            line = line[cut:].lstrip()
        yield line


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _attr(el, name):
    for k, v in el.attrib.items():
        if _local(k) == name:
            return v
    return None


def _rid(el):
    """The r:id attribute (a relationship id), not a plain id."""
    return next((v for k, v in el.attrib.items() if k.endswith("}id")), None)


def _zip_xml(z, name):
    import xml.etree.ElementTree as ET
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_XML:
        raise ToolError(f"{name} in this file is too large to read")
    return ET.fromstring(z.read(name))


def _rels(z, name, base):
    """Relationship id -> part name, from a .rels file of an Office document."""
    import posixpath
    x = _zip_xml(z, name)
    out = {}
    for r in (x if x is not None else []):
        t, rid = _attr(r, "Target") or "", _attr(r, "Id")
        if _attr(r, "TargetMode") == "External":
            continue
        out[rid] = t[1:] if t.startswith("/") else posixpath.normpath(posixpath.join(base, t))
        out[rid + ":type"] = _attr(r, "Type") or ""
    return out


def _open_zip(path):
    import zipfile
    return zipfile.ZipFile(path)


# ---- Word
def _docx_para(p):
    out = []
    for e in p.iter():
        t = _local(e.tag)
        if t == "t":
            out.append(e.text or "")
        elif t == "tab":
            out.append("\t")
        elif t in ("br", "cr"):
            out.append(" ")
        elif t in ("footnoteReference", "endnoteReference"):
            out.append(f"[^{'' if t[0] == 'f' else 'e'}{_attr(e, 'id')}]")
    text = "".join(out).strip()
    style = list_item = None
    for e in p:
        if _local(e.tag) == "pPr":
            for s in e:
                if _local(s.tag) == "pStyle":
                    style = _attr(s, "val") or ""
                elif _local(s.tag) == "numPr":
                    list_item = True
    if text and style:
        m = re.search(r"(?i)(heading|berschrift|kop|titre)\s*(\d)", style)
        if m:
            return "#" * min(int(m.group(2)), 6) + " " + text
        if style.lower() in ("title", "titel"):
            return "# " + text
        if style.lower().startswith(("list", "lijst", "aufz")):
            list_item = True
    return ("- " + text) if (text and list_item) else text


def _docx_table(tbl):
    rows = []
    for tr in tbl:
        if _local(tr.tag) != "tr":
            continue
        cells = []
        for tc in tr:
            if _local(tc.tag) == "tc":
                cells.append(" / ".join(x for x in (_docx_para(p) for p in tc.iter() if _local(p.tag) == "p") if x))
        rows.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    return rows


def _docx_blocks(parent, out):
    for el in parent:
        t = _local(el.tag)
        if t == "p":
            out.append(_docx_para(el))
        elif t == "tbl":
            out.extend(_docx_table(el))
            out.append("")
        elif t in ("sdt", "sdtContent", "customXml"):
            _docx_blocks(el, out)


def docx_text(path):
    with _open_zip(path) as z:
        doc = _zip_xml(z, "word/document.xml")
        if doc is None:
            raise ToolError(f"{show_path(path)} is not a Word document")
        body = next((e for e in doc if _local(e.tag) == "body"), doc)
        out = []
        _docx_blocks(body, out)
        for part, label in (("word/footnotes.xml", "^"), ("word/endnotes.xml", "^e")):
            x = _zip_xml(z, part)
            if x is None:
                continue
            notes = []
            for n in x:
                if _local(n.tag) not in ("footnote", "endnote") or (_attr(n, "type") or "normal") != "normal":
                    continue
                text = " ".join(t for t in (_docx_para(p) for p in n.iter() if _local(p.tag) == "p") if t)
                if text:
                    notes.append(f"[{label}{_attr(n, 'id')}] {text}")
            if notes:
                out += ["", "Footnotes:" if label == "^" else "Endnotes:"] + notes
    lines = []
    for l in out:                   # at most one empty line in a row
        if l or (lines and lines[-1]):
            lines.append(l)
    return lines or ["(no text)"]


# ---- Excel
def _col_number(ref):
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + ord(ch.upper()) - 64
    return n


def _is_date_format(code):
    code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code or "")
    return bool(re.search(r"[dmyhs]", code, re.I)) and "General" not in code


def _number(v):
    try:
        x = float(v)
    except ValueError:
        return v
    return str(int(x)) if x.is_integer() and abs(x) < 1e15 else f"{x:.15g}"


def _excel_date(v, date1904):
    try:
        x = float(v)
    except ValueError:
        return v
    base = datetime.datetime(1904, 1, 1) if date1904 else datetime.datetime(1899, 12, 30)
    try:
        d = base + datetime.timedelta(seconds=round(x * 86400))
    except OverflowError:
        return v
    if d.hour or d.minute or d.second:
        return d.strftime("%Y-%m-%d %H:%M" if not d.second else "%Y-%m-%d %H:%M:%S")
    return d.strftime("%Y-%m-%d")


def _si_text(si):
    out = []
    for e in si:
        t = _local(e.tag)
        if t == "t":
            out.append(e.text or "")
        elif t == "r":
            out += [x.text or "" for x in e if _local(x.tag) == "t"]
    return "".join(out)


def xlsx_text(path):
    import xml.etree.ElementTree as ET
    with _open_zip(path) as z:
        wb = _zip_xml(z, "xl/workbook.xml")
        if wb is None:
            raise ToolError(f"{show_path(path)} is not an Excel workbook")
        rels = _rels(z, "xl/_rels/workbook.xml.rels", "xl/")
        ss = _zip_xml(z, "xl/sharedStrings.xml")
        shared = [_si_text(si) for si in ss if _local(si.tag) == "si"] if ss is not None else []
        dates, custom = set(), {}
        st = _zip_xml(z, "xl/styles.xml")
        if st is not None:
            for e in st.iter():
                if _local(e.tag) == "numFmt":
                    custom[int(_attr(e, "numFmtId") or -1)] = _attr(e, "formatCode") or ""
            xfs = next((e for e in st if _local(e.tag) == "cellXfs"), [])
            for i, xf in enumerate(x for x in xfs if _local(x.tag) == "xf"):
                fid = int(_attr(xf, "numFmtId") or 0)
                if 14 <= fid <= 22 or 45 <= fid <= 47 or (fid in custom and _is_date_format(custom[fid])):
                    dates.add(i)
        date1904 = any(_local(e.tag) == "workbookPr" and _attr(e, "date1904") in ("1", "true")
                       for e in wb.iter())
        sheets = [e for e in wb.iter() if _local(e.tag) == "sheet"]
        out = [f"Excel workbook with {len(sheets)} sheet{'s' if len(sheets) != 1 else ''}: rows are "
               "numbered as in Excel, columns are separated by | (A, B, C, …); values as last saved."]
        for sh in sheets:
            name, target = _attr(sh, "name"), rels.get(_rid(sh) or "")
            hidden = " (hidden)" if _attr(sh, "state") in ("hidden", "veryHidden") else ""
            out += ["", f"## Sheet \"{name}\"{hidden}"]
            if not target or target not in z.namelist():
                out.append("(a chart sheet or missing sheet: no cells)")
                continue
            count = 0
            with z.open(target) as fh:
                for ev, row in ET.iterparse(fh, events=("end",)):
                    if _local(row.tag) != "row":
                        continue
                    cells, col = {}, 0
                    for c in row:
                        if _local(c.tag) != "c":
                            continue
                        ref = _attr(c, "r")
                        col = _col_number(ref) if ref else col + 1
                        t, s = _attr(c, "t"), int(_attr(c, "s") or 0)
                        v = next((x.text for x in c if _local(x.tag) == "v"), None)
                        if v is None and t != "inlineStr":
                            f_ = next((x.text for x in c if _local(x.tag) == "f"), None)
                            v, t = ("=" + f_, "str") if f_ else (None, t)
                        if t == "s" and v is not None:
                            val = shared[int(v)] if int(v) < len(shared) else ""
                        elif t == "inlineStr":
                            val = "".join(_si_text(x) for x in c if _local(x.tag) == "is")
                        elif t == "b":
                            val = "TRUE" if v == "1" else "FALSE"
                        elif v is None:
                            continue
                        elif t in ("str", "e"):
                            val = v
                        elif s in dates:
                            val = _excel_date(v, date1904)
                        else:
                            val = _number(v)
                        if val != "":
                            cells[col] = val.replace("\n", " ").replace("|", "\\|")
                    rnum = _attr(row, "r")
                    row.clear()
                    if not cells:
                        continue
                    count += 1
                    if count > MAX_SHEET_ROWS:
                        out.append("[... more rows; read the sheet with Python (pandas) for all of them]")
                        break
                    last = max(cells)
                    out.append(f"{rnum or count}: " + " | ".join(cells.get(i, "") for i in range(1, last + 1)))
            if not count:
                out.append("(empty)")
    return out


# ---- PowerPoint
def _a_paras(x):
    out = []
    for p in x.iter():
        if _local(p.tag) != "p":
            continue
        text = "".join((e.text or "") if _local(e.tag) == "t" else " " for e in p.iter()
                       if _local(e.tag) in ("t", "br")).strip()
        if text:
            out.append(text)
    return out


def pptx_text(path):
    with _open_zip(path) as z:
        pres = _zip_xml(z, "ppt/presentation.xml")
        if pres is None:
            raise ToolError(f"{show_path(path)} is not a PowerPoint presentation")
        rels = _rels(z, "ppt/_rels/presentation.xml.rels", "ppt/")
        ids = [_rid(e) for e in pres.iter() if _local(e.tag) == "sldId"]
        out = [f"PowerPoint presentation with {len(ids)} slides."]
        for n, rid in enumerate(ids, 1):
            target = rels.get(rid or "")
            x = _zip_xml(z, target) if target else None
            if x is None:
                continue
            hidden = " (hidden)" if _attr(x, "show") in ("0", "false") else ""
            out += ["", f"## Slide {n}{hidden}"] + (_a_paras(x) or ["(no text)"])
            folder, base = target.rsplit("/", 1)
            srels = _rels(z, f"{folder}/_rels/{base}.rels", folder + "/")
            notes = [v for k, v in srels.items() if not k.endswith(":type")
                     and srels.get(k + ":type", "").endswith("/notesSlide")]
            nx = _zip_xml(z, notes[0]) if notes else None
            if nx is not None:
                text = [t for t in _a_paras(nx) if not t.isdigit()]
                if text:
                    out.append("Notes: " + " ".join(text))
    return out


# ---- Stata
DTA_NUM = {"byte": ("b", 1, 100), "int": ("h", 2, 32740), "long": ("i", 4, 2147483620),
           "float": ("f", 4, None), "double": ("d", 8, None)}
DTA_OLD = {251: "byte", 252: "int", 253: "long", 254: "float", 255: "double"}
DTA_NEW = {65530: "byte", 65529: "int", 65528: "long", 65527: "float", 65526: "double"}
DTA_VERSION = {113: "Stata 8–9", 114: "Stata 10–11", 115: "Stata 12", 117: "Stata 13",
               118: "Stata 14 or newer", 119: "Stata 15 or newer, many variables"}


def dta_text(path):
    import struct
    with open(path, "rb") as f:
        head = f.read(11)
        if head == b"<stata_dta>":
            info = _dta_new(f, struct)
        elif head[:1] and head[0] in (113, 114, 115) and head[1] in (1, 2):
            info = _dta_old(f, head, struct)
        else:
            raise ToolError(f"{show_path(path)} is not a Stata data file tai can read "
                            "(Stata 8 or newer)")
    return _dta_report(info)


def _cstr(b, enc):
    return b.split(b"\0", 1)[0].decode(enc, "replace")


def _dta_value(raw, typ, bo, struct, enc, strls):
    if isinstance(typ, int):                    # str#
        return _cstr(raw, enc)
    if typ == "strL":
        return strls.get(raw, "(long string)")
    code, size, top = DTA_NUM[typ]
    v = struct.unpack(bo + code, raw)[0]
    if top is not None:
        if v > top:
            k = v - top - 1
            return "." if k == 0 else "." + chr(96 + k) if k <= 26 else "."
        return str(v)
    bits = struct.unpack(bo + ("I" if size == 4 else "Q"), raw)[0]
    if (size == 4 and 0x7f000000 <= bits < 0x80000000) or (size == 8 and 0x7fe0000000000000 <= bits < 0x8000000000000000):
        return "."
    return f"{v:.7g}" if size == 4 else f"{v:.10g}"


def _dta_rows(f, info, struct):
    widths = [t if isinstance(t, int) else 8 if t == "strL" else DTA_NUM[t][1] for t in info["types"]]
    rows = []
    for _ in range(min(info["N"], DTA_ROWS)):
        raw = f.read(sum(widths))
        if len(raw) < sum(widths):
            break
        pos, row = 0, []
        for t, w in zip(info["types"], widths):
            row.append((raw[pos:pos + w], t))
            pos += w
        rows.append(row)
    return rows


def _dta_labels(data, bo, struct, enc, namelen, new):
    """value label tables from the bytes after the data (both formats)."""
    labels, pos = {}, 0
    while pos < len(data):
        if new:
            if data[pos:pos + 5] != b"<lbl>":
                break
            pos += 5
        if pos + 4 > len(data):
            break
        ln = struct.unpack(bo + "I", data[pos:pos + 4])[0]
        name = _cstr(data[pos + 4:pos + 4 + namelen], enc)
        pos += 4 + namelen + 3
        tab = data[pos:pos + ln]
        pos += ln + (6 if new else 0)            # </lbl>
        if len(tab) < 8:
            break
        n, tl = struct.unpack(bo + "II", tab[:8])
        offs = struct.unpack(bo + f"{n}I", tab[8:8 + 4 * n])
        vals = struct.unpack(bo + f"{n}i", tab[8 + 4 * n:8 + 8 * n])
        txt = tab[8 + 8 * n:8 + 8 * n + tl]
        labels[name] = {v: _cstr(txt[o:], enc) for v, o in zip(vals, offs)}
    return labels


def _dta_old(f, head, struct):
    rel, bo = head[0], ">" if head[1] == 1 else "<"
    enc = "latin-1"
    f.seek(4)
    K, N = struct.unpack(bo + "HI", f.read(6))
    label = _cstr(f.read(81), enc)
    stamp = _cstr(f.read(18), enc)
    codes = f.read(K)
    types = [DTA_OLD.get(c, c) for c in codes]
    names = [_cstr(f.read(33), enc) for _ in range(K)]
    f.read(2 * (K + 1))
    fmts = [_cstr(f.read(12 if rel == 113 else 49), enc) for _ in range(K)]
    lblnames = [_cstr(f.read(33), enc) for _ in range(K)]
    varlabels = [_cstr(f.read(81), enc) for _ in range(K)]
    while True:                                 # expansion fields
        t = f.read(1)
        ln = struct.unpack(bo + "I", f.read(4))[0]
        if not t or (t == b"\0" and ln == 0):
            break
        f.seek(ln, 1)
    info = {"release": rel, "K": K, "N": N, "label": label, "stamp": stamp, "types": types,
            "names": names, "formats": fmts, "lblnames": lblnames, "varlabels": varlabels,
            "bo": bo, "enc": enc, "strls": {}}
    start = f.tell()
    info["rows"] = _dta_rows(f, info, struct)
    width = sum(t if isinstance(t, int) else DTA_NUM[t][1] for t in types)
    f.seek(start + N * width)
    info["labels"] = _dta_labels(f.read(20_000_000), bo, struct, enc, 33, False)
    return info


def _dta_new(f, struct):
    def tag(name):
        t = f"<{name}>".encode()
        if f.read(len(t)) != t:
            raise ValueError(f"<{name}> expected")

    def num(code):
        return struct.unpack(bo + code, f.read(struct.calcsize(code)))[0]
    bo = "<"
    tag("header")
    tag("release")
    rel = int(f.read(3))
    f.read(len("</release>"))
    tag("byteorder")
    bo = "<" if f.read(3) == b"LSF" else ">"
    f.read(len("</byteorder>"))
    tag("K")
    K = num("I" if rel == 119 else "H")
    f.read(4)
    tag("N")
    N = num("I" if rel == 117 else "Q")
    f.read(4)
    enc = "latin-1" if rel == 117 else "utf-8"
    tag("label")
    label = f.read(num("B" if rel == 117 else "H")).decode(enc, "replace")
    f.read(len("</label>"))
    tag("timestamp")
    stamp = f.read(f.read(1)[0]).decode(enc, "replace")
    f.read(len("</timestamp></header>"))
    tag("map")
    offs = struct.unpack(bo + "14Q", f.read(112))
    w = 33 if rel == 117 else 129

    def section(i, name):
        """Go to a section by the map; some files (from Stata 13) have zeros there: search instead."""
        t = f"<{name}>".encode()
        f.seek(offs[i])
        if offs[i] and f.read(len(t)) == t:
            return
        f.seek(0)
        head = f.read(max(offs[9], offs[i], 1 << 20) + len(t))
        at = head.find(t)
        if at < 0:
            raise ValueError(f"<{name}> not found")
        f.seek(at + len(t))
    section(2, "variable_types")
    codes = struct.unpack(bo + f"{K}H", f.read(2 * K))
    types = [c if c <= 2045 else "strL" if c == 32768 else DTA_NEW[c] for c in codes]
    section(3, "varnames")
    names = [_cstr(f.read(w), enc) for _ in range(K)]
    section(5, "formats")
    fmts = [_cstr(f.read(49 if rel == 117 else 57), enc) for _ in range(K)]
    section(6, "value_label_names")
    lblnames = [_cstr(f.read(w), enc) for _ in range(K)]
    section(7, "variable_labels")
    varlabels = [_cstr(f.read(81 if rel == 117 else 321), enc) for _ in range(K)]
    info = {"release": rel, "K": K, "N": N, "label": label, "stamp": stamp, "types": types,
            "names": names, "formats": fmts, "lblnames": lblnames, "varlabels": varlabels,
            "bo": bo, "enc": enc, "strls": {}}
    section(9, "data")
    info["rows"] = _dta_rows(f, info, struct)
    if "strL" in types and offs[11] - offs[10] < 50_000_000:
        f.seek(offs[10])
        data = f.read(offs[11] - offs[10])
        pos, olen = len("<strls>"), 4 if rel == 117 else 8
        while data[pos:pos + 3] == b"GSO":
            v = data[pos + 3:pos + 7]
            o = data[pos + 7:pos + 7 + olen]
            t = data[pos + 7 + olen]
            ln = struct.unpack(bo + "I", data[pos + 8 + olen:pos + 12 + olen])[0]
            body = data[pos + 12 + olen:pos + 12 + olen + ln]
            vi, oi = struct.unpack(bo + "I", v)[0], int.from_bytes(o, "little" if bo == "<" else "big")
            info["strls"][(vi, oi)] = body.rstrip(b"\0").decode(enc, "replace") if t == 130 else "(binary)"
            pos += 12 + olen + ln
        vbytes = {117: 4, 118: 2, 119: 3}[rel]
        order = "little" if bo == "<" else "big"
        keyed = {}
        for raw_row in info["rows"]:
            for raw, t in raw_row:
                if t == "strL":
                    vi, oi = int.from_bytes(raw[:vbytes], order), int.from_bytes(raw[vbytes:], order)
                    keyed[raw] = info["strls"].get((vi, oi), "")
        info["strls"] = keyed
    f.seek(offs[11])
    tag("value_labels")
    info["labels"] = _dta_labels(f.read(min(offs[12] - offs[11], 20_000_000)), bo, struct, enc, w, True)
    return info


def _stata_date(v, fmt):
    """A value with a Stata date format (%td, %tc, %tm, %tq) as the date it stands for."""
    m = re.match(r"%-?(t[cCdwmqhy]|d)", fmt or "")
    if not m or v.startswith("."):
        return v
    try:
        x = float(v)
        k = m.group(1)
        if k in ("td", "d"):
            return (datetime.date(1960, 1, 1) + datetime.timedelta(days=int(x))).isoformat()
        if k in ("tc", "tC"):
            return (datetime.datetime(1960, 1, 1) + datetime.timedelta(milliseconds=x)).strftime("%Y-%m-%d %H:%M:%S")
        n = int(x)
        if k == "tm":
            return f"{1960 + n // 12}m{n % 12 + 1}"
        if k == "tq":
            return f"{1960 + n // 4}q{n % 4 + 1}"
        if k == "th":
            return f"{1960 + n // 2}h{n % 2 + 1}"
        if k == "tw":
            return f"{1960 + n // 52}w{n % 52 + 1}"
    except (ValueError, OverflowError):
        pass
    return v


def _dta_report(info):
    import struct
    K, N, enc, bo = info["K"], info["N"], info["enc"], info["bo"]
    tname = lambda t: f"str{t}" if isinstance(t, int) else t
    out = [f"Stata data set (.dta format {info['release']}, {DTA_VERSION.get(info['release'], 'Stata')}): "
           f"{N:,} observations, {K} variables."]
    if info["label"]:
        out.append(f"Data label: {info['label']}")
    if info["stamp"]:
        out.append(f"Saved: {info['stamp']}")
    out += ["", "Variables (name · type · format · label · value labels):"]
    for i in range(K):
        bits = [info["names"][i], tname(info["types"][i]), info["formats"][i]]
        bits.append(info["varlabels"][i] or "-")
        lab = info["lblnames"][i]
        if lab:
            table = info["labels"].get(lab, {})
            shown = ", ".join(f"{v} = {t}" for v, t in sorted(table.items())[:12])
            bits.append(f"{lab}: {shown}{', …' if len(table) > 12 else ''}" if table else lab)
        out.append("  " + " · ".join(bits))
    rows = info["rows"]
    if rows:
        out += ["", f"First {len(rows)} of {N:,} observations (value labels applied):",
                "  " + " | ".join(info["names"])]
        for row in rows:
            vals = []
            for i, (raw, t) in enumerate(row):
                v = _dta_value(raw, t, bo, struct, enc, info["strls"])
                v = _stata_date(v, info["formats"][i])
                table = info["labels"].get(info["lblnames"][i]) if info["lblnames"][i] else None
                if table and v.lstrip("-").isdigit() and int(v) in table:
                    v = table[int(v)]
                v = v if len(v) <= 200 else v[:200] + "…"
                vals.append(v.replace("\n", " ").replace("|", "\\|"))
            out.append("  " + " | ".join(vals))
    if N > len(rows):
        out += ["", "To work with all the data, use Stata, pandas.read_stata in Python or haven::read_dta in R."]
    return out


# ---- Jupyter notebooks
def nb_source(cell):
    s = cell.get("source", "")
    return "".join(s) if isinstance(s, list) else str(s or "")


def _nb_text(v):
    return ANSI.sub("", "".join(v) if isinstance(v, list) else str(v or ""))


def _clip_middle(s, n=NB_OUTPUT):
    if len(s) <= n:
        return s
    return s[:n // 2] + f"\n[... {len(s) - n} characters left out ...]\n" + s[-n // 2:]


def load_notebook(path, text=None):
    try:
        nb = json.loads(read_text(path) if text is None else text)
    except ValueError as e:
        raise ToolError(f"{show_path(path)} is not a valid notebook ({e})")
    if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
        raise ToolError(f"{show_path(path)} is not a Jupyter notebook in the current format (nbformat 4)")
    return nb


def notebook_view(path):
    """(lines, images) of a notebook as the model sees it; images: [(line index, cell number, part)]."""
    nb = load_notebook(path)
    md = nb.get("metadata") if isinstance(nb.get("metadata"), dict) else {}
    lang = (md.get("kernelspec") or {}).get("language") or (md.get("language_info") or {}).get("name") or ""
    cells = [c for c in nb["cells"] if isinstance(c, dict)]
    lines = [f"Jupyter notebook{f' ({lang})' if lang else ''} with {len(cells)} cells. "
             "Change cells with edit_notebook, by the numbers below."]
    images = []
    for i, c in enumerate(cells, 1):
        typ = c.get("cell_type") or "code"
        ec = c.get("execution_count")
        lines.append(f"──── cell {i} · {typ}" + (f" · [{ec}]" if ec else ""))
        lines += nb_source(c).splitlines() or ["(empty)"]
        outs = c.get("outputs") if typ == "code" and isinstance(c.get("outputs"), list) else []
        if outs:
            lines.append(f"──── output of cell {i}")
        for o in outs:
            if not isinstance(o, dict):
                continue
            kind = o.get("output_type")
            if kind == "stream":
                lines += _clip_middle(_nb_text(o.get("text"))).splitlines()
            elif kind == "error":
                tb = _nb_text("\n".join(o.get("traceback") or [])).splitlines()[-30:]
                first = f"{o.get('ename', 'Error')}: {_nb_text(o.get('evalue'))}"
                lines += ([] if first in tb else [first]) + tb
            elif kind in ("execute_result", "display_data"):
                data = o.get("data") if isinstance(o.get("data"), dict) else {}
                pic = next((m for m in ("image/png", "image/jpeg", "image/gif", "image/webp") if m in data), None)
                if pic:
                    b64 = re.sub(r"\s", "", _nb_text(data[pic]))
                    if len(b64) * 3 // 4 > MAX_IMAGE:
                        lines.append("[image: too large to show]")
                    else:
                        part = {"type": "image_url", "image_url": {"url": f"data:{pic};base64,{b64}"}}
                        images.append((len(lines), i, part))
                        lines.append(f"[image {len(images)}]")
                elif "text/markdown" in data:
                    lines += _clip_middle(_nb_text(data["text/markdown"])).splitlines()
                elif "text/plain" in data:
                    lines += _clip_middle(_nb_text(data["text/plain"])).splitlines()
                elif "text/html" in data:
                    lines += _clip_middle(html_to_text(_nb_text(data["text/html"]))).splitlines()
                elif "image/svg+xml" in data:
                    lines.append("[SVG image: not shown]")
    return lines, images


def _nb_format(nb, text):
    """(indent, sort_keys) that reproduce the file as saved, so edits change only their cell."""
    for indent in (1, 2, 4, None):
        for sort in (True, False):
            if json.dumps(nb, indent=indent, sort_keys=sort, ensure_ascii=False).strip() == text.strip():
                return indent, sort
    return 1, True                              # how Jupyter saves notebooks


def _nb_source_lines(src):
    return src.splitlines(keepends=True)


def tool_edit_notebook(a, perms):
    path = resolve(a["path"])
    label = show_path(path)
    if path.suffix.lower() != ".ipynb":
        raise ToolError("edit_notebook is for Jupyter notebooks (.ipynb); use edit_file for other files")
    old = read_text(path)
    nb = load_notebook(path, old)
    indent, sort = _nb_format(nb, old)
    cells = nb["cells"]
    action = str(a.get("action") or "replace").lower()
    n = int(a["cell"])
    src = a.get("source")
    ctype = a.get("cell_type")
    if ctype not in (None, "code", "markdown", "raw"):
        raise ToolError("cell_type must be code, markdown or raw")

    def new_cell(typ, source, cid=None):
        c = {"cell_type": typ, "metadata": {}, "source": _nb_source_lines(source)}
        if typ == "code":
            c.update(execution_count=None, outputs=[])
        if cid or (nb.get("nbformat", 4) >= 4 and nb.get("nbformat_minor", 0) >= 5):
            import uuid
            c["id"] = cid or uuid.uuid4().hex[:8]
        return c
    before = ""
    if action == "insert":
        if not 0 <= n <= len(cells):
            raise ToolError(f"cell must be 0 to {len(cells)} for insert (the new cell goes after it)")
        if src is None:
            raise ToolError("insert needs the source of the new cell")
        cells.insert(n, new_cell(ctype or "code", src))
        shown, after, verb = n + 1, src, f"Insert cell {n + 1} into"
    elif not 1 <= n <= len(cells):
        raise ToolError(f"there is no cell {n}; the notebook has {len(cells)} cells (numbered from 1)")
    elif action == "delete":
        before, after = nb_source(cells[n - 1]), ""
        del cells[n - 1]
        shown, verb = n, f"Delete cell {n} of"
    elif action == "replace":
        if src is None:
            raise ToolError("replace needs the new source of the cell")
        cell = cells[n - 1]
        before, after = nb_source(cell), src
        typ = ctype or cell.get("cell_type") or "code"
        if typ != cell.get("cell_type"):
            cells[n - 1] = new_cell(typ, src, cell.get("id"))
        else:
            cell["source"] = _nb_source_lines(src)
            if typ == "code":               # the old outputs no longer belong to this code
                cell["outputs"], cell["execution_count"] = [], None
        shown, verb = n, f"Change cell {n} of"
    else:
        raise ToolError("action must be replace, insert or delete")
    new = json.dumps(nb, indent=indent, sort_keys=sort, ensure_ascii=False) + "\n"
    body = diff_view(before, after, f"{label} · cell {shown}")
    write_after_approval(path, old, new, perms, verb, body=body)
    done = {"insert": "Inserted", "delete": "Deleted", "replace": "Changed"}[action]
    note = " (its old outputs were removed; run the notebook to get new ones)" \
        if action == "replace" and (ctype or "code") == "code" else ""
    return (f"{done} cell {shown} of {label}{note}. Cell numbers are now as in read_file again.",
            f"{done} cell {shown} of {label}")


def tool_web_fetch(a, perms):
    url = a["url"].strip()
    if not re.match(r"https?://", url, re.I):
        raise ToolError("only http:// and https:// URLs are supported")
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    perms.check(f"fetch:{host}", f"Fetch this page from {host}?", "Fetch",
                Text(url, style="bold") if RICH else url, detail={"type": "fetch", "url": url, "host": host})
    with working(f"Fetching {host}…"):
        return _fetch(a, url, host)


def _fetch(a, url, host):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) tai/1.0",
        "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype = resp.headers.get_content_type()
            charset = resp.headers.get_content_charset() or "utf-8"
            data = resp.read(15_000_000)
            final = resp.geturl()
    except urllib.error.HTTPError as e:
        raise ToolError(f"HTTP {e.code} from {host}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ToolError(f"cannot fetch {host}: {getattr(e, 'reason', e)}")
    if ctype == "application/pdf" or data[:5] == b"%PDF-":
        text, kind = pdf_to_text(data), "PDF"
    else:
        raw = data.decode(charset, errors="replace")
        if "html" in ctype or raw.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
            text, kind = html_to_text(raw), "page"
        elif ctype.startswith("text/") or ctype.endswith(("json", "xml", "csv")):
            text, kind = raw, ctype
        else:
            raise ToolError(f"unsupported content type: {ctype}")
    limit = int(a.get("max_chars") or 25_000)
    start = max(int(a.get("offset") or 0), 0)
    part = text[start:start + limit]
    body = f"URL: {final}\n\n{part}"
    if start + limit < len(text):
        body += f"\n\n[... {len(text) - start - limit} more characters; use offset={start + limit}]"
    return body, f"{kind}, {human(len(text))} chars"


# ================================================================ tool registry

def fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": required}}}


S = {"type": "string"}
# The shell tool: bash on Linux/macOS, PowerShell on Windows (named so the model writes the right syntax).
SHELL_TOOL, SHELL_LABEL = ("powershell", "PowerShell") if WINDOWS else ("bash", "Bash")
TOOLS = [
    fn("read_file", "Read a file: text, PDF, Word (.docx), Excel (.xlsx), PowerPoint (.pptx), Stata data "
       "(.dta: variables, labels and the first rows) or a Jupyter notebook (.ipynb: cells with their "
       "outputs and plots). Returns numbered lines; use offset/limit for long files. Also looks at "
       "images (PNG, JPEG, GIF, WebP), e.g. a plot or a screenshot.",
       {"path": S, "offset": {"type": "integer", "description": "1-based first line"},
        "limit": {"type": "integer", "description": "max lines, default 2000"}}, ["path"]),
    fn("list_files", "List files and folders matching a glob pattern such as '*', '**/*.py' or "
       "'data/*.csv'. Skips .git, node_modules and similar.",
       {"pattern": S, "path": {"type": "string", "description": "base directory, default: current"}},
       ["pattern"]),
    fn("grep", "Search file contents with a Python regular expression; returns file:line: text.",
       {"pattern": S, "path": {"type": "string", "description": "file or directory, default: current"},
        "glob": {"type": "string", "description": "only file names matching this, e.g. '*.tex'"},
        "ignore_case": {"type": "boolean"}}, ["pattern"]),
    fn("write_file", "Create a file, or overwrite one completely. Requires user approval. "
       "For changes to existing files prefer edit_file.", {"path": S, "content": S}, ["path", "content"]),
    fn("edit_file", "Replace an exact piece of text in a file. old_string must match exactly once "
       "(including whitespace) unless replace_all is true. Read the file first. Requires user approval.",
       {"path": S, "old_string": S, "new_string": S, "replace_all": {"type": "boolean"}},
       ["path", "old_string", "new_string"]),
    fn("edit_notebook", "Change one cell of a Jupyter notebook (.ipynb), by the cell number read_file "
       "shows. action: replace (new source for cell N; a code cell's old outputs are removed), insert "
       "(a new cell after cell N, 0 = at the top) or delete. It doesn't run the notebook (to run it: "
       "jupyter nbconvert --to notebook --execute --inplace FILE). Requires user approval.",
       {"path": S, "cell": {"type": "integer", "description": "cell number, from 1"},
        "action": {"type": "string", "enum": ["replace", "insert", "delete"]},
        "source": {"type": "string", "description": "the cell's new content (replace, insert)"},
        "cell_type": {"type": "string", "enum": ["code", "markdown", "raw"],
                      "description": "for insert (default code), or to change a cell's type"}},
       ["path", "cell", "action"]),
    fn(SHELL_TOOL, f"Run a {SHELL_LABEL} command in the current directory; returns combined "
       "stdout/stderr and the exit code. Requires user approval. Non-interactive: never run commands "
       "that wait for input.",
       {"command": S, "timeout": {"type": "integer", "description": "seconds, default 120, max 600"},
        "run_in_background": {"type": "boolean", "description":
            "start it and return at once, for servers, watchers and long jobs; read its output "
            "later with job_output"},
        "outside_sandbox": {"type": "boolean", "description":
            "in auto mode commands run in a sandbox (no network, writes only in the current folder "
            "and the temp folder); set this to run one without it, e.g. to install packages or "
            "download data. The user is asked first. Only when needed."}},
       ["command"]),
    fn("job_output", "Read the new output of a background job (started with run_in_background) and "
       "whether it is still running. wait: seconds to wait for the job to finish first (max 600).",
       {"job_id": S, "wait": {"type": "integer", "description": "seconds, default 0"}}, ["job_id"]),
    fn("job_stop", "Stop a background job (and the processes it started).", {"job_id": S}, ["job_id"]),
    fn("todo_write", "Your task list for work with several steps (3 or more), shown to the user. Send "
       "the WHOLE list each time. Mark exactly one item in_progress while you work on it and mark it "
       "completed as soon as it is done; add items you discover. Skip it for quick, simple requests.",
       {"todos": {"type": "array", "items": {"type": "object", "properties": {
           "content": {"type": "string", "description": "the step, in a few words"},
           "status": {"type": "string", "enum": list(TODO_STATUS)}}, "required": ["content", "status"]}}},
       ["todos"]),
    fn("web_search", "Search the web. Returns a summary of findings with source URLs. Use for current "
       "information, facts you are unsure about, papers, documentation. One focused query per call; "
       "several calls in one reply run back to back.",
       {"query": {"type": "string", "description": "what to search for, as a specific question or query"}},
       ["query"]),
    fn("web_fetch", "Fetch a web page or PDF by URL and return its text. Use after web_search to read "
       "a source in full. Requires user approval per website.",
       {"url": S, "offset": {"type": "integer", "description": "character offset to continue from"},
        "max_chars": {"type": "integer", "description": "default 25000"}}, ["url"]),
    fn("task", "Start a sub-agent for a self-contained subtask, e.g. researching one question on the web, "
       "or searching/reading many files. It has the same tools (except task), works on its own and "
       "returns only its final report, which keeps your context small. Several task calls in the same "
       "reply run IN PARALLEL. The sub-agent cannot see this conversation: give it a complete prompt "
       "with all needed context and say exactly what to report back.",
       {"description": {"type": "string", "description": "3-5 word label shown to the user"},
        "prompt": {"type": "string", "description": "full, self-contained instructions"},
        "model": {"type": "string", "description": "optional model id; default: your own model"}},
       ["description", "prompt"]),
]
SUB_TOOLS = [t for t in TOOLS if t["function"]["name"] not in ("task", "todo_write")]
# plan mode: tai reads and researches, then asks the user to approve its plan
PLAN_READ = {"read_file", "list_files", "grep", "get_diagnostics", "web_search", "web_fetch", "task",
             "job_output"}
PLAN_TOOL = fn("present_plan", "Plan mode only: show the user your plan and ask them to approve it. Call "
               "it when the plan is ready. If they approve, plan mode ends and you carry the plan out "
               "right away; if not, they say what to change.",
               {"plan": {"type": "string", "description":
                   "the plan in markdown: the goal, the steps (which files change and how), what you "
                   "will check at the end, and open questions if any"}}, ["plan"])
PLAN_NOTE = ("[Plan mode is on: the user wants a plan before anything changes. Explore with the "
             "read-only tools (files, search, the web), ask the user if something essential is unclear, "
             "then present a concrete plan with present_plan. Don't edit files or run commands: that "
             "starts after the user approves the plan.]")


def plan_tools(tools, sub=False):
    """The tools for plan mode: only those that read, and present_plan."""
    if not tools:
        return tools
    kept = [t for t in tools if t["function"]["name"] in PLAN_READ]
    return kept if sub else kept + [PLAN_TOOL]


def mode_tools(tools, perms, sub=False):
    return plan_tools(tools, sub) if perms.mode == "plan" else tools


def tool_present_plan(a, perms):
    plan = str(a.get("plan") or "").strip()
    if not plan:
        raise ToolError("the plan is empty")
    if perms.mode != "plan":
        return "Plan mode is off: go ahead with the work.", "Plan (plan mode is off)"
    body = Markdown(plan) if RICH else plan
    try:
        perms.check("plan", "Start working on this plan?", "Plan", body, detail={"type": "plan", "plan": plan})
    except Denied as d:
        if CANCEL.is_set() or str(d) == "Cancelled.":
            raise
        if str(d).startswith("No terminal"):
            return ("Nobody can approve the plan here (tai runs without a terminal). Give the plan in "
                    "your reply and stop; don't call present_plan again."), "Plan (no terminal to approve it)"
        say_ = f" They say: {d}" if str(d) else " Ask them what to change if it isn't clear."
        return (f"The user did not approve the plan yet.{say_} Stay in plan mode: revise the plan and "
                "present it again with present_plan."), "Plan not approved" + (f" — {d}" if str(d) else "")
    if perms.mode == "plan":
        perms.set_mode("manual")
    how = {"accept-edits": "file edits now run without asking",
           "manual": "file edits and commands ask the user first"}.get(perms.mode, f"mode: {perms.mode}")
    return (f"The user approved the plan; plan mode is off ({how}). Carry the plan out now, step by step "
            "(keep a task list with todo_write).", f"Plan approved · {perms.mode}")
RUNNERS = {"read_file": tool_read_file, "list_files": tool_list_files, "grep": tool_grep,
           "write_file": tool_write_file, "edit_file": tool_edit_file, SHELL_TOOL: tool_bash,
           "edit_notebook": tool_edit_notebook,
           "web_search": tool_web_search, "web_fetch": tool_web_fetch, "job_output": tool_job_output,
           "job_stop": tool_job_stop, "todo_write": tool_todo_write, "present_plan": tool_present_plan}
LABELS = {"read_file": ("Read", "path"), "list_files": ("List", "pattern"), "grep": ("Grep", "pattern"),
          "write_file": ("Write", "path"), "edit_file": ("Edit", "path"),
          "edit_notebook": ("EditNotebook", "path"),
          SHELL_TOOL: (SHELL_LABEL, "command"),
          "web_search": ("WebSearch", "query"), "web_fetch": ("Fetch", "url"),
          "job_output": ("JobOutput", "job_id"), "job_stop": ("JobStop", "job_id"), "todo_write": ("Tasks", None),
          "present_plan": ("Plan", None)}


def parse_args(call):
    try:
        args = json.loads(call["arguments"] or "{}")
        return args if isinstance(args, dict) else None
    except ValueError:
        return None


def run_tool(call, perms, compact=False):
    """Run one (non-task) tool call. Returns (result_for_model, summary, style)."""
    name = call["name"]
    args = parse_args(call)
    label, key = LABELS.get(name, (name, None))
    shown = args.get(key, "") if (args and key) else ""
    if key == "path" and isinstance(shown, str) and shown:
        shown = show_path(resolve(shown))
    if name == "list_files" and args and args.get("path"):
        shown = f"{show_path(resolve(args['path']))}/{shown}"
    if not compact:
        say("")
    if name == SHELL_TOOL and args and args.get("run_in_background"):
        label = "Background"            # first, so a long command can't push it off the line
    paren = f"({clip(shown, 200)})" if shown else ""
    if RICH:
        say(Text.assemble(("● ", GOLD), (label, "bold"), (paren, "")), one_line=True)
    else:
        say(f"● {label}{paren}")

    return exec_tool(call, perms)


def exec_tool(call, perms, runners=None):
    """Run one tool call without printing. Returns (result_for_model, summary, style)."""
    runners = runners or RUNNERS
    name = call["name"]
    args = parse_args(call)
    if name not in runners:
        return f"Error: unknown tool '{name}'.", f"unknown tool {name}", "red"
    if args is None:
        return "Error: tool arguments were not valid JSON.", "invalid arguments", "red"
    try:
        result, summary = runners[name](args, perms)
        style = "dim"
    except Blocked as b:
        result = (f"Blocked: the user's permission settings forbid this (rule: {b}). Do not try to "
                  "get around the rule; if the step is needed, ask the user to do it or to change the rule.")
        summary, style = f"Blocked by your settings (rule: {b})", "red"
    except Denied as d:
        result = "The user denied this action." + (f" The user says: {d}" if str(d) else "")
        summary, style = "Denied" + (f" — {d}" if str(d) else ""), "red"
    except ToolError as e:
        result, summary, style = f"Error: {e}", f"Error: {e}", "red"
    except (KeyError, TypeError, ValueError) as e:
        result, summary, style = f"Error: bad arguments ({e})", f"bad arguments ({e})", "red"
    if isinstance(result, str) and len(result) > MAX_TOOL_OUTPUT:
        half = MAX_TOOL_OUTPUT // 2
        result = (result[:half] + f"\n[... {len(result) - MAX_TOOL_OUTPUT} characters omitted ...]\n"
                  + result[-half:])
    return result, summary, style


def print_summary(summary, style):
    style = GREY if style == "dim" else style
    lines = summary.splitlines() or [""]
    say("  ⎿  " + lines[0], style)
    for l in lines[1:]:
        say("     " + l, style)


# ================================================================ sub-agents

def subagent(args, label, perms, model):
    """Run a sub-agent to completion in this thread; return its final report."""
    _tls.prefix = f"  │ {label}: "
    _tls.label = label
    msgs = [{"role": "system", "content": system_prompt(True, sub=True)},
            {"role": "user", "content": args["prompt"]}]
    uses = tokens = streamed = 0
    text = ""
    try:
        for _ in range(SUB_MAX_STEPS):
            if CANCEL.is_set():
                return "Sub-agent stopped: the user interrupted." + (f" Notes so far: {text}" if text else "")
            text, calls, usage = "", [], None
            gen = stream_reply(model, msgs, mode_tools(SUB_TOOLS, perms, sub=True))
            try:
                for kind, val in gen:
                    if CANCEL.is_set():
                        break
                    if kind == "text":
                        text += val
                        USAGE.stream(est_tokens(val)); streamed += est_tokens(val)
                    elif kind == "tool":
                        USAGE.stream(est_tokens(val[1])); streamed += est_tokens(val[1])
                    else:
                        calls, usage = val
            finally:
                gen.close()
                USAGE.stream(-streamed)
                streamed = 0
            if CANCEL.is_set():
                continue
            USAGE.add(usage)
            if usage:
                tokens += (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
            assistant = {"role": "assistant", "content": text or None}
            if calls:
                for i, c in enumerate(calls):
                    c["id"] = c["id"] or f"call_{uses}_{i}"
                assistant["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                    "name": c["name"], "arguments": c["arguments"] or "{}"}} for c in calls]
            msgs.append(assistant)
            if not calls:
                dim(f"done · {uses} tool uses · {human(tokens)} tokens")
                return text or "(the sub-agent returned no report)"
            for c in calls:
                if CANCEL.is_set():
                    result = "Not run: the user interrupted."
                else:
                    result, summary, style = run_tool(c, perms, compact=True)
                    uses += 1
                    if style == "red":
                        print_summary(summary, style)
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": result})
        return f"Sub-agent stopped after {SUB_MAX_STEPS} steps. Last notes: {text}"
    except RuntimeError as e:
        err(f"failed: {e}")
        return f"Sub-agent failed: {e}"
    finally:
        _tls.prefix = _tls.label = ""


def run_tasks(tasks, perms, model):
    """Run task calls in parallel. Returns {call_id: result}."""
    results, jobs = {}, []
    for i, c in enumerate(tasks):
        a = parse_args(c)
        if not a or not a.get("prompt"):
            say(f"\n● Task(?)", "bold magenta")
            print_summary("Error: a task needs a prompt", "red")
            results[c["id"]] = "Error: the task tool needs a 'prompt'."
            continue
        label = clip(a.get("description") or a["prompt"], 30)
        say("")
        if RICH:
            say(Text.assemble(("● ", BLUE), ("Task", "bold"), (f"({label})", "")), one_line=True)
            say(Text("  ⎿  " + clip(a["prompt"], 300), style=GREY), one_line=True)
        else:
            say(f"● Task({label})\n  ⎿  {clip(a['prompt'], 110)}")
        jobs.append((c, a, f"{i + 1}·{clip(label, 18)}" if len(tasks) > 1 else clip(label, 22)))
    if not jobs:
        return results
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL_AGENTS)
    futures = {pool.submit(subagent, a, label, perms, a.get("model") or model): c
               for c, a, label in jobs}
    pending = set(futures)
    while pending:
        try:
            _, pending = concurrent.futures.wait(pending, timeout=0.2)
        except KeyboardInterrupt:
            if not CANCEL.is_set():
                CANCEL.set()
                err("\n[stopping sub-agents… press Enter if an approval prompt is open]")
    pool.shutdown()
    for fut, c in futures.items():
        try:
            results[c["id"]] = fut.result()
        except Exception as e:  # never let one sub-agent crash the session
            results[c["id"]] = f"Sub-agent crashed: {e}"
    return results


def execute_calls(calls, perms, model):
    """Run the tool calls of one model reply; task calls run in parallel at the end."""
    results = {}
    tasks = [c for c in calls if c["name"] == "task"]
    for c in calls:
        if c["name"] == "task":
            continue
        if CANCEL.is_set():
            results[c["id"]] = "Not run: the user interrupted."
            continue
        try:
            result, summary, style = run_tool(c, perms)
            print_summary(summary, style)
        except KeyboardInterrupt:
            CANCEL.set()
            err("\n[interrupted]")
            result = "Interrupted by the user."
        results[c["id"]] = result
    if tasks and not CANCEL.is_set():
        results.update(run_tasks(tasks, perms, model))
    return [results.get(c["id"], "Not run: the user interrupted.") for c in calls]


# ================================================================ agent loop

def system_prompt(use_tools, extra=None, sub=False, ide=False):
    cwd = Path.cwd()
    where = "inside Visual Studio Code (tai extension)" if ide else "in the user's terminal"
    parts = [f"You are tai, an AI assistant running {where} on {platform.system()} "
             f"(via the Tilburg.ai API). Today is {datetime.date.today():%A %d %B %Y}. "
             f"Current directory: {cwd}",
             "Today's date above is correct. Your training data ends earlier, so events up to today that "
             "you don't know about are real, not 'future': trust dated web results over your own prior "
             "knowledge."]
    if sub:
        parts.append(
            "You are a SUB-AGENT: another AI agent gave you one focused task. Work through it on your "
            "own with your tools. Your final message is the only thing the other agent will see (not "
            "your tool results), so make it a complete, self-contained report: concrete findings, file "
            "paths with line numbers, URLs of sources. Don't ask questions; if something is unclear, "
            "make a reasonable assumption and state it. Don't edit files or run commands unless the "
            "task asks for it.")
    if use_tools:
        parts.append(
            f"You have tools to list, search and read files (including PDFs), edit and write files, run "
            f"{SHELL_LABEL} commands, search the web and fetch web pages. Use them to find things out instead of "
            "guessing.\n"
            "- Work step by step. Read a file before editing it; prefer edit_file with small, exact "
            "replacements over rewriting whole files.\n"
            "- read_file also shows you images (PNG, JPEG, GIF, WebP): look at plots you made to check "
            "them, and at screenshots the user mentions. It also reads Word, Excel, PowerPoint, Stata and "
            "notebook files; change notebooks with edit_notebook.\n"
            "- For current events, facts you're unsure of, papers or documentation: use web_search, "
            "then web_fetch to read the best sources. Cite URLs in your answer.\n"
            + ("" if sub else
               "- For larger jobs, delegate self-contained parts to sub-agents with the task tool; "
               "several task calls in one reply run in parallel (e.g. one per question or source).\n")
            + f"- write_file, edit_file, edit_notebook, {SHELL_TOOL} and web_fetch need the user's approval. If the user denies "
            "an action, don't retry it; follow their instructions or ask.\n"
            "- Never run destructive or irreversible commands (deleting files, git reset --hard, "
            "overwriting data, sending data to remote services) unless the user explicitly asked.\n"
            "- Commands run non-interactively with a timeout; don't start editors or pagers. For servers, "
            "watchers and long builds or analyses, set run_in_background and check on them with "
            "job_output; stop them with job_stop when they are no longer needed.\n"
            + ("" if sub else
               "- For a task with several steps, keep a task list with todo_write and update it as you go.\n"
               f"- Asked to remember something for later conversations: add a line to {PERSONAL_MD} "
               "(all projects) or ./TAI.md (this project).\n")
            +
            "- When done, say briefly what you did. Be concise; use markdown.")
    else:
        parts.append("Be concise; use markdown.")
    if ide and use_tools:
        parts.append(
            "You are shown in a VS Code chat panel; the user sees your file edits as diffs. A user "
            "message may start with an <ide_context> block saying which file is open in the editor and "
            "what is selected, and with <file> blocks the user attached. Use them when relevant; don't "
            "comment on them otherwise. After editing code, use get_diagnostics to check for errors "
            "and warnings that VS Code reports. Refer to files with paths relative to the current "
            "directory (with :line where useful); the user can click them.")
    try:
        personal = read_text(PERSONAL_MD)[:20000].strip() if PERSONAL_MD.is_file() else ""
    except ToolError:
        personal = ""
    if personal:
        parts.append(f"The user's own instructions for all projects (from {PERSONAL_MD}):\n{personal}")
    for name in PROJECT_MD:
        f = cwd / name
        if use_tools and f.is_file():
            parts.append(f"Project instructions from {name}:\n" + read_text(f)[:20000])
            break
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)


def ready_blocks(buf):
    """Split streamed markdown into (complete blocks, rest): cut after a blank line that is
    not inside a ``` code block, so each printed piece renders correctly on its own."""
    cut, fence, pos = 0, False, 0
    for line in buf.splitlines(keepends=True):
        pos += len(line)
        if not line.endswith("\n"):
            break
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        elif not fence and not line.strip():
            cut = pos
    return buf[:cut], buf[cut:]


def stream_and_render(model, messages, tools, markdown, main=True, tool_choice=None, mark="●"):
    """Stream one model reply. Returns (text, tool_calls, usage). KeyboardInterrupt propagates
    with the partial text attached as .partial."""
    if LIVE.active:
        return _stream_live(model, messages, tools, markdown, main, tool_choice, mark)
    parts = []
    events = stream_reply(model, messages, tools, tool_choice)
    calls, usage = [], None
    view = Thinking() if RICH else None
    streamed = 0

    def progress(s):
        nonlocal streamed
        n = est_tokens(s)
        streamed += n
        USAGE.stream(n)
        if view:
            view.out += n
    try:
        if RICH and sys.stdout.isatty():
            with Live(view, console=console, refresh_per_second=12,
                      vertical_overflow="visible") as live:
                for kind, val in events:
                    if kind == "text":
                        parts.append(val)
                        progress(val)
                        view.tool = ""
                        view.body = (with_gutter(Markdown("".join(parts))) if markdown
                                     else Text("".join(parts)))
                    elif kind == "tool":
                        view.tool = val[0]
                        progress(val[1])
                    else:
                        calls, usage = val
                text = "".join(parts).strip()
                live.update(with_gutter(Markdown(text) if markdown else Text(text)) if text else Text(""))
        else:
            for kind, val in events:
                if kind == "text":
                    parts.append(val)
                    sys.stdout.write(val)
                    sys.stdout.flush()
                elif kind == "done":
                    calls, usage = val
            if parts:
                print()
    except KeyboardInterrupt as e:
        e.partial = "".join(parts)
        raise
    finally:
        events.close()
        USAGE.stream(-streamed)
    USAGE.add(usage, main=main)
    return "".join(parts), calls, usage


def _stream_live(model, messages, tools, markdown, main, tool_choice, mark):
    """Live-prompt variant: finished paragraphs are printed above the prompt as they arrive;
    the 'Thinking…' line is shown above the input. Ctrl+C (CANCEL) cuts the stream."""
    parts, pending = [], ""
    calls, usage, streamed, first = [], None, 0, True
    view = Thinking()
    if main:
        prev, LIVE.status = LIVE.status, view

    def emit(chunk):
        nonlocal first
        chunk = chunk.strip("\n")
        if not chunk.strip():
            return
        body = Markdown(chunk) if (markdown and RICH) else (Text(chunk) if RICH else chunk)
        say(with_gutter(body, mark=mark if first else " ") if RICH else (mark + " " if first else "  ") + chunk)
        first = False

    events = stream_reply(model, messages, tools, tool_choice)
    try:
        try:
            for kind, val in events:
                if CANCEL.is_set():
                    raise KeyboardInterrupt
                if kind == "text":
                    parts.append(val)
                    n = est_tokens(val)
                    streamed += n
                    USAGE.stream(n)
                    view.out += n
                    view.tool = ""
                    pending += val
                    done, pending = ready_blocks(pending)
                    if done:
                        emit(done)
                elif kind == "tool":
                    view.tool = val[0]
                    n = est_tokens(val[1])
                    streamed += n
                    USAGE.stream(n)
                    view.out += n
                else:
                    calls, usage = val
        except KeyboardInterrupt:
            raise
        except Exception:
            if CANCEL.is_set():         # the socket was shut down by Ctrl+C
                raise KeyboardInterrupt
            raise
        emit(pending)
    except KeyboardInterrupt as e:
        emit(pending)
        e.partial = "".join(parts)
        raise
    finally:
        events.close()
        USAGE.stream(-streamed)
        if main:
            LIVE.status = prev
        LIVE.invalidate()
    USAGE.add(usage, main=main)
    return "".join(parts), calls, usage


def take_inbox():
    with INBOX_LOCK:
        items = INBOX[:]
        INBOX.clear()
    return items


def run_turn(conv, perms, markdown, use_tools):
    """Run model ↔ tools until the model answers without tool calls."""
    msgs = conv["messages"]
    tools = use_tools if isinstance(use_tools, list) else TOOLS if use_tools else None
    total_in = total_out = 0
    start = time.time()
    replied = auto_failed = retried = False
    for step in range(MAX_STEPS):
        if replied and not auto_failed and compact_due(msgs):
            r = compact_conv(conv, midway=True, auto=True)
            if r == "cancelled":
                return
            auto_failed = r != "done"
        try:
            text, calls, usage = stream_and_render(conv["model"], msgs, mode_tools(tools, perms), markdown)
        except KeyboardInterrupt as e:
            err("\n[interrupted]")
            if getattr(e, "partial", ""):
                msgs.append({"role": "assistant", "content": e.partial})
            return
        except RuntimeError as e:
            if not retried and CONTEXT_FULL.search(str(e)) and not CANCEL.is_set():
                retried = True          # the API says it's too long: compact and try again
                if replied:
                    r = compact_conv(conv, midway=True, auto=True)
                else:                   # summarize what came before, then ask again
                    user = msgs.pop()
                    r = compact_conv(conv, auto=True)
                    msgs.append(user)
                    start_checkpoint(conv, len(msgs) - 1)
                if r == "done":
                    continue
                if r == "cancelled":
                    return
            err(f"tai: {e}")
            return
        if usage:
            total_in += usage.get("prompt_tokens") or 0
            total_out += usage.get("completion_tokens") or 0

        assistant = {"role": "assistant", "content": text or None}
        if calls:
            for i, c in enumerate(calls):
                c["id"] = c["id"] or f"call_{step}_{i}"
            assistant["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                "name": c["name"], "arguments": c["arguments"] or "{}"}} for c in calls]
        elif not text:
            assistant["content"] = "(no response)"
        msgs.append(assistant)
        replied = True
        if not calls:
            break

        results = execute_calls(calls, perms, conv["model"])
        for c, r in zip(calls, results):
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": r})
        if CANCEL.is_set():
            return
        note = finished_jobs_note()
        if note:
            msgs.append({"role": "user", "content": note})
        queued = take_inbox()
        if queued:
            msgs.append({"role": "user", "content": join_content(queued)})
            dim(f"  ↳ tai is reading your {'message' if len(queued) == 1 else f'{len(queued)} messages'}")
        print()
    else:
        err(f"[stopped after {MAX_STEPS} steps — say 'continue' to keep going]")
    if total_in or total_out:
        bits = [f"{human(total_in)} in", f"{human(total_out)} out", f"{int(time.time() - start)}s"]
        if USAGE.ctx_text():
            bits.append(USAGE.ctx_text())
        bits += [f"session {human(USAGE.inp + USAGE.out)}", conv["model"]]
        dim("  " + " · ".join(bits))


# ================================================================ conversation & REPL

def read_file_block(path):
    p = Path(path).expanduser()
    if is_image(p):
        try:
            part, size = image_file_part(p.resolve())
        except ToolError as e:
            raise RuntimeError(str(e)) from None
        return [{"type": "text", "text": f'<file name="{p.name}">(image, attached below)</file>'}, part]
    try:
        text = document_text(p.resolve()) if p.suffix.lower() in DOC_TYPES else None
        if text is None:
            text = decode(p.read_bytes())[0]
    except ToolError as e:
        raise RuntimeError(str(e)) from None
    except OSError as e:
        raise RuntimeError(f"cannot read {path}: {e.strerror}") from None
    return f'<file name="{p.name}">\n{text}\n</file>'


def save_conv(conv):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        LAST_CONV.write_text(json.dumps(conv, indent=1), encoding="utf-8")
    except OSError:
        pass


def load_conv():
    try:
        return json.loads(LAST_CONV.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def to_markdown(conv):
    out = [f"<!-- tai conversation · model: {conv['model']} -->\n"]
    for m in conv["messages"]:
        if m["role"] == "user":
            out.append(f"## You\n\n{text_of(m['content'])}\n")
        elif m["role"] == "assistant":
            if m.get("content"):
                out.append(f"## {conv['model']}\n\n{m['content']}\n")
            for tc in m.get("tool_calls") or []:
                out.append(f"*→ {tc['function']['name']}* `{clip(tc['function']['arguments'], 200)}`\n")
        elif m["role"] == "tool":
            out.append(f"<details><summary>tool result</summary>\n\n```\n{text_of(m['content'])[:3000]}\n```\n"
                       "</details>\n")
    return "\n".join(out)


HELP = """Commands:
  /model [name]   show or switch model        /models   list models
  /tools          toggle agent tools on/off   /clear    start a new conversation
  /system [text]  show or add instructions    /save PATH  save conversation as markdown
  /file PATH      attach a file to your next message
  /btw QUESTION   quick side question; answer is not added to the conversation
  /mode [name]    show or set permission mode (manual, accept-edits, auto, plan)
  /usage          token usage this session
  /compact [what to keep]  summarize the conversation to free up context (also automatic
                  when the context window is 80% full)
  /rewind [N]     go back to an earlier message and undo tai's file changes since then
  /permissions    show the saved permission rules, where they are stored, and the sandbox
  /remember TEXT  keep an instruction for all later conversations (/remember project TEXT:
                  for this project, in its TAI.md); /remember alone shows both
  /review [REF] [focus]  review the changes in git: not yet committed, or everything since
                  REF (a branch or commit, e.g. /review main); tai only reads, it changes nothing
  /jobs           commands running in the background; /jobs stop ID stops one
  /help           this help                   /exit     quit (or Ctrl+C twice, or Ctrl+D)
Alt+Enter inserts a new line (pasting multi-line text also works).
Shift+Tab cycles the permission mode (manual → accept-edits → auto → plan: tai plans first).
While tai works the prompt stays open: /btw QUESTION is answered right away, and a
message you type is read at tai's next step.
Ctrl+C clears what you typed, then stops tai (queued messages come back); at an empty
prompt with tai idle, twice quits.
Approvals: 1 = yes, 2 = don't ask again (saved for that command in this folder, or for
that site; edits and other commands: this session), 3 = no."""


def read_input(prompt):
    line = input(prompt)
    if not line.lstrip().startswith('"""'):
        return line
    first = line.lstrip()[3:]
    if first.rstrip().endswith('"""') and first.strip():
        return first.rstrip()[:-3]
    lines = [first] if first else []
    while True:
        nxt = input("… ")
        if nxt.rstrip().endswith('"""'):
            lines.append(nxt.rstrip()[:-3])
            return "\n".join(lines).strip()
        lines.append(nxt)


PERSONAL_MD = STATE_DIR / "TAI.md"           # the user's instructions for all projects (/remember)
PROJECT_MD = ("TAI.md", "CLAUDE.md", "AGENTS.md")


def project_md():
    cwd = Path.cwd()
    return next((cwd / n for n in PROJECT_MD if (cwd / n).is_file()), cwd / "TAI.md")


def remember(text, project=False):
    """Add a line to the personal or the project instructions; returns the file."""
    text = " ".join(str(text).split())
    if not text:
        raise ValueError("nothing to remember")
    path = project_md() if project else PERSONAL_MD
    try:
        old = read_text(path) if path.exists() else ""
    except ToolError:
        old = ""
    if not old.strip() and not project:
        old = "# My instructions for tai (all projects)\n\n"
    elif old and not old.endswith("\n"):
        old += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, old + f"- {text}\n", text_format(path) if path.exists() else ("utf-8", "\n"))
    return path


def memory_text():
    """/remember without text: where the instructions are, and what they say."""
    out = []
    for label, path in (("Yours, for all projects", PERSONAL_MD), ("This project's", project_md())):
        try:
            body = read_text(path).strip() if path.is_file() else ""
        except ToolError:
            body = ""
        out.append(f"{label}: {path}" + ("" if body else " (empty)"))
        out += ["  " + l for l in body.splitlines()[:40]]
    return "\n".join(out)


def new_conv(model, use_tools, extra_system=None):
    return {"model": model, "tools": use_tools, "extra_system": extra_system,
            "messages": [{"role": "system", "content": system_prompt(use_tools, extra_system)}]}


def reset_system(conv):
    msgs = [m for m in conv["messages"] if m["role"] != "system"]
    conv["messages"] = [{"role": "system",
                         "content": system_prompt(conv["tools"], conv.get("extra_system"))}] + msgs


def ask(conv, perms, markdown, content, tools=None):
    """tools: a tool list for this message only (e.g. read-only ones for /review)."""
    CANCEL.clear()
    msgs = conv["messages"]
    user = {"role": "user", "content": content}
    if compact_due(msgs + [user]) and compact_conv(conv, auto=True) == "cancelled":
        return False
    note = finished_jobs_note()
    if note:
        msgs.append({"role": "user", "content": note})
    if perms.mode == "plan" and conv["tools"] and tools is None:
        msgs.append({"role": "user", "content": PLAN_NOTE})
    msgs.append(user)
    start_checkpoint(conv, len(msgs) - 1)
    print()
    TURN_CLOCK["start"] = time.time()
    try:
        run_turn(conv, perms, markdown, conv["tools"] if tools is None else tools)
    finally:
        notify_done()
    if msgs and msgs[-1] is user:               # request failed before any reply: allow retry
        msgs.pop()
        turns = conv.get("turns") or []
        if turns and turns[-1]["msg"] == len(msgs):
            turns.pop()
        return False
    save_conv(conv)
    return True


# ---- checkpoints: the terminal remembers each file's content before a turn first changes it,
# so /rewind can put files back (the editor server keeps its own, per session)
CHECKPOINT = {"turn": None}
CHECKPOINT_LOCK = threading.Lock()


def start_checkpoint(conv, idx):
    turn = {"msg": idx, "files": {}}
    with CHECKPOINT_LOCK:
        conv.setdefault("turns", []).append(turn)
        CHECKPOINT["turn"] = turn


def checkpoint_write(path, old):
    with CHECKPOINT_LOCK:
        turn = CHECKPOINT["turn"]
        if turn is not None and str(path) not in turn["files"]:
            turn["files"][str(path)] = snapshot(path)


def rewind_files(turns, msg_index):
    """Files changed in turns at or after msg_index, with the oldest snapshot of each."""
    files = {}
    for t in reversed(turns or []):
        if t["msg"] >= msg_index:
            files.update(t["files"])       # earlier turns overwrite: oldest snapshot wins
    return files


def is_note(m):
    """Messages tai wrote itself (compaction summary, 'continue' note), not the user's."""
    c = m.get("content")
    return isinstance(c, str) and (c.startswith(("[Summary of the earlier conversation]", "[Background job"))
                                   or c in (CONTINUE_NOTE, PLAN_NOTE))


def visible_text(content):
    """What the user typed, without attached files and piped input."""
    if not isinstance(content, str):
        content = text_of(content)
    return re.sub(r"<file name=\"[^\"]*\">.*?</file>|<stdin>.*?</stdin>", "", content, flags=re.S).strip()


def text_of(content):
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return content or ""


def rewind_points(conv):
    return [i for i, m in enumerate(conv["messages"]) if i and m.get("role") == "user" and not is_note(m)]


def rewind(conv, arg):
    """/rewind: list your messages; /rewind N [keep-files]: go back to the N-th latest one."""
    points = rewind_points(conv)[::-1]
    turns = conv.get("turns") or []
    if not points:
        dim("nothing to rewind yet")
        return
    words = arg.split()
    if not words:
        dim("Go back to one of your messages: the conversation after it is removed and tai's file")
        dim("changes since then are undone. Your message comes back in the input to edit and resend.")
        for n, i in enumerate(points[:15], 1):
            files = rewind_files(turns, i)
            extra = f"  · {len(files)} file{'s' if len(files) != 1 else ''} changed since" if files else ""
            dim(f"  {n:>2}  {clip(visible_text(conv['messages'][i]['content']).replace(chr(10), ' '), 70)}{extra}")
        dim("  /rewind N goes back to message N  ·  /rewind N keep-files leaves the files as they are")
        return
    if not words[0].isdigit() or not 1 <= int(words[0]) <= len(points) or \
            any(w not in ("keep-files", "--keep-files") for w in words[1:]):
        err(f"usage: /rewind N [keep-files], with N from 1 to {len(points)} (/rewind lists them)")
        return
    idx = points[int(words[0]) - 1]
    msgs = conv["messages"]
    text = visible_text(msgs[idx]["content"])
    restored, failed = [], []
    if len(words) == 1:
        for path, snap in rewind_files(turns, idx).items():
            try:
                restore(Path(path), snap)
                restored.append(show_path(Path(path)))
            except (OSError, ValueError) as e:
                failed.append(f"{show_path(Path(path))} ({e})")
    del msgs[idx:]
    conv["turns"] = [t for t in turns if t["msg"] < idx]
    USAGE.ctx = 0
    save_conv(conv)
    if restored:
        dim("  restored " + ", ".join(restored))
    if failed:
        err("  could not restore " + ", ".join(failed))
    if LIVE.active:
        LIVE.refill = text
        dim("  rewound · your message is back in the input: edit it and press Enter")
    else:
        dim("  rewound · your message was:")
        print(text)


def quiet_reply(model, msgs, tools):
    """A reply that is not shown (for the summary when compacting)."""
    parts = []
    gen = pumped(model, msgs, tools, CANCEL.is_set, "none" if tools else None)
    try:
        for kind, val in gen:
            if kind == "text":
                parts.append(val)
            elif kind == "done":
                calls, usage = val
                USAGE.add(usage)
                if calls:
                    raise RuntimeError("the model tried to use tools instead of summarizing; try again")
    except (RuntimeError, Cancelled):
        raise
    except Exception as e:
        raise RuntimeError(f"connection lost: {e.__class__.__name__}: {e}") from None
    finally:
        gen.close()
    return "".join(parts)


def compact_conv(conv, instructions="", midway=False, auto=False):
    """Replace the conversation with a summary. Returns "done", "cancelled" or "failed"."""
    msgs = conv["messages"]
    if not any(m.get("role") == "assistant" for m in msgs):
        dim("nothing to compact yet")
        return "failed"
    before = context_estimate(msgs)
    tools = TOOLS if conv["tools"] else None
    if auto:
        full = f" ({100 * before // USAGE.max_ctx}% full)" if USAGE.max_ctx else ""
        say("")
        dim(f"  the context window is almost full{full}: summarizing the conversation so far")
    try:
        with working("Compacting the conversation…"):
            summary = summarize(lambda m: quiet_reply(conv["model"], m, tools), msgs, instructions, midway)
    except (Cancelled, KeyboardInterrupt):
        err("  [compacting interrupted · the conversation is unchanged]")
        return "cancelled"
    except RuntimeError as e:
        err(f"tai: compacting failed: {e}")
        return "failed"
    msgs[:] = compacted_messages(msgs[0], summary, midway, conv.get("todos"))
    conv["turns"] = []
    with CHECKPOINT_LOCK:
        CHECKPOINT["turn"] = None
    USAGE.ctx = msgs_tokens(msgs)
    save_conv(conv)
    dim(f"  conversation compacted · {human(before)} → {human(USAGE.ctx)} tokens"
        + (" · tai continues" if midway else " · /save shows the summary"))
    return "done"


def btw(conv, markdown, question):
    """Answer a side question with the conversation as context, without changing it.
    Works while a turn runs: it sees the conversation up to the last complete step."""
    if not LIVE.busy:
        CANCEL.clear()
    msgs = consistent_prefix(list(conv["messages"])) + [{"role": "user", "content":
        "[Side question — answer briefly from what you already know and the conversation so far. "
        "Do not call any tools; this exchange will not be kept in the conversation.]\n\n" + question}]
    tools = TOOLS if conv["tools"] else None   # keep the tool schema so earlier tool calls stay valid
    print()
    try:
        text, _, _ = stream_and_render(conv["model"], msgs, tools and TOOLS, markdown, main=False,
                                       tool_choice="none" if tools else None, mark="◆")
    except KeyboardInterrupt:
        err("\n[interrupted]")
        return
    except RuntimeError as e:
        err(f"tai: {e}")
        return
    if not text.strip():
        dim("(no answer)")
    dim("  /btw · not added to the conversation")


# ---- /review: the model reviews the changes in git (read-only tools for that turn)
REVIEW_TOOLS = {"read_file", "list_files", "grep", "get_diagnostics"}
MAX_REVIEW_DIFF = 150_000
REVIEW_ASK = (
    "Review {what} in this git repository and report the problems you find. Look for real defects: "
    "bugs and wrong logic, edge cases that break, wrong results in analyses (e.g. merges, filters, "
    "missing values, units), security problems, error handling that hides failures, and tests that "
    "don't test what they claim. Read the surrounding code with read_file and grep where you need "
    "it to be sure. Do NOT change any files.\n\n"
    "Report the findings most important first. For each: file:line, what is wrong, a concrete case "
    "where it goes wrong, and how to fix it. Leave out style preferences and nitpicks. If you find "
    "nothing that matters, say so in one sentence.{focus}\n\n"
    "Changed files:\n{stat}\n\n<diff>\n{diff}\n</diff>")


def git(*args, check=True):
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60, **NO_WINDOW)
    except FileNotFoundError:
        raise RuntimeError("git is not installed") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {args[0]} took too long") from None
    if check and r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip()
                           else f"git {args[0]} failed")
    return r


def review_request(arg):
    """(message for the model, short description) for /review [REF] [what to focus on]."""
    if git("rev-parse", "--is-inside-work-tree", check=False).returncode:
        raise RuntimeError("/review needs a git repository (this folder is not one)")
    words = arg.split(maxsplit=1)
    ref = None
    if words and not git("rev-parse", "--verify", "--quiet", words[0] + "^{commit}", check=False).returncode:
        ref, arg = words[0], (words[1] if len(words) > 1 else "")
    has_head = not git("rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode
    if ref:
        mb = git("merge-base", ref, "HEAD", check=False)
        base = mb.stdout.strip() if not mb.returncode and mb.stdout.strip() else ref
        what = f"all changes since {ref} (committed and not yet committed)"
    else:
        base = "HEAD" if has_head else None
        what = "the changes that are not committed yet"
    diff_args = [base] if base else ["--cached"]
    diff = git("diff", "--no-color", "--no-ext-diff", *diff_args).stdout
    stat = git("diff", "--stat", "--no-color", *diff_args).stdout.rstrip()
    new = [f for f in git("ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0") if f]
    extra = []
    for f in new[:50]:
        path = Path(f)
        try:
            if path.stat().st_size > 100_000 or is_binary(path):
                extra.append(f"new file {f} (binary or large, not shown)")
                continue
            lines = read_text(path).splitlines()
        except (OSError, ToolError):
            continue
        extra.append(f"diff --git a/{f} b/{f}\nnew file (not yet added to git)\n--- /dev/null\n+++ b/{f}\n"
                     f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + l for l in lines))
    if new:
        stat += ("\n" if stat else "") + "\n".join(f" {f} (new, untracked)" for f in new[:50])
    full = diff + ("\n" if diff and extra else "") + "\n".join(extra)
    if not full.strip():
        raise RuntimeError("nothing to review: there are no uncommitted changes "
                           "(/review main reviews everything since main, /review HEAD~1 the last commit)")
    if len(full) > MAX_REVIEW_DIFF:
        full = full[:MAX_REVIEW_DIFF] + (f"\n[... the diff is {len(full)} characters; the rest is left out, "
                                         "read the changed files for it ...]")
    n = len([l for l in stat.splitlines() if "|" in l or "(new, untracked)" in l])
    focus = f"\n\nThe user asks you to focus on: {arg.strip()}" if arg.strip() else ""
    label = f"{n} file{'s' if n != 1 else ''}" + (f" since {ref}" if ref else ", not committed")
    return REVIEW_ASK.format(what=what, focus=focus, stat=stat or "(none)", diff=full), label


def review_tools(tools):
    return [t for t in tools if t["function"]["name"] in REVIEW_TOOLS] if tools else tools


# ---- custom commands: Markdown files in .tai/commands/ (this project) or <state dir>/commands/
# (yours). /NAME runs the file's text as a message; $ARGUMENTS is replaced by what follows /NAME.
BUILTIN_COMMANDS = {"help", "model", "models", "tools", "clear", "system", "file", "save", "btw", "mode",
                    "usage", "compact", "rewind", "permissions", "jobs", "exit", "quit", "q", "review",
                    "history", "terminal", "restart", "new", "remember"}


def command_dirs():
    return [("project", Path.cwd() / ".tai" / "commands"), ("yours", STATE_DIR / "commands")]


def custom_commands():
    """name -> {"description", "body", "path", "scope"}; project commands win over yours."""
    out = {}
    for scope, d in command_dirs():
        try:
            files = sorted(d.glob("*.md"))
        except OSError:
            continue
        for f in files:
            name = f.stem.lower()
            if name in out or name in BUILTIN_COMMANDS or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", name):
                continue
            try:
                text = read_text(f)
            except ToolError:
                continue
            desc = ""
            m = re.match(r"---\s*\n(.*?)\n---\s*\n", text, re.S)
            if m:
                text = text[m.end():]
                for line in m.group(1).splitlines():
                    k, _, v = line.partition(":")
                    if k.strip().lower() == "description":
                        desc = v.strip().strip("\"'")
            body = text.strip()
            if not body:
                continue
            desc = desc or clip(next((l.strip("# ").strip() for l in body.splitlines() if l.strip()), ""), 60)
            out[name] = {"description": desc, "body": body, "path": str(f), "scope": scope}
    return out


def expand_command(cmd, arg):
    body = cmd["body"]
    if "$ARGUMENTS" in body:
        return body.replace("$ARGUMENTS", arg.strip())
    return body + (f"\n\n{arg.strip()}" if arg.strip() else "")


def commands_help():
    cmds = custom_commands()
    if not cmds:
        return ("Your own commands: put a Markdown file in .tai/commands/ (this project) or in "
                f"{STATE_DIR / 'commands'} (all projects); /NAME then sends its text ($ARGUMENTS = what you "
                "type after it).")
    return "Your own commands:\n" + "\n".join(
        f"  /{n:<14} {c['description']}  ({c['scope']})" for n, c in sorted(cmds.items()))


COMMANDS = [("/help", "show commands"), ("/model", "show or switch model"), ("/models", "list models"),
            ("/tools", "toggle agent tools on/off"), ("/clear", "start a new conversation"),
            ("/system", "show or set extra instructions"), ("/file", "attach a file to your next message"),
            ("/save", "save conversation as markdown"), ("/btw", "side question, not kept in the conversation"),
            ("/mode", "show or set permission mode (shift+tab cycles)"), ("/usage", "token usage this session"),
            ("/compact", "summarize the conversation to free up context"),
            ("/rewind", "go back to an earlier message (and undo file changes)"),
            ("/permissions", "show saved permission rules and the sandbox"),
            ("/remember", "keep an instruction for later conversations"),
            ("/review", "review the changes in git (/review main: everything since main)"),
            ("/jobs", "background commands (/jobs stop ID)"),
            ("/exit", "quit")]


def make_reader(conv, perms):
    """Return a function that reads one user message (raises EOFError to quit).
    Ctrl+C clears the line; on an empty line, Ctrl+C twice within 2 s quits."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = {"ctrl_c": 0.0}
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.formatted_text import FormattedText
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.styles import Style
    except ImportError:
        return _readline_reader(state)

    class Slash(Completer):
        def get_completions(self, document, event):
            t = document.text_before_cursor
            if t.startswith("/") and " " not in t:
                own = [(f"/{n}", c["description"]) for n, c in sorted(custom_commands().items())]
                for cmd, desc in COMMANDS + own:
                    if cmd.startswith(t):
                        yield Completion(cmd, start_position=-len(t), display_meta=desc)

    kb = KeyBindings()

    @kb.add("c-c")
    def _(event):
        buf = event.app.current_buffer
        if buf.text:
            buf.reset()
            state["ctrl_c"] = 0.0
        elif LIVE.busy:
            queued = stop_turn()
            if queued:      # give unsent messages back, like the VS Code extension
                buf.insert_text("\n\n".join(q if isinstance(q, str) else visible_text(q) for q in queued))
        elif time.time() - state["ctrl_c"] < 2:
            event.app.exit(exception=EOFError())
        else:
            state["ctrl_c"] = time.time()
            event.app.invalidate()

    @kb.add("escape", "enter")
    def _(event):
        event.current_buffer.insert_text("\n")

    @kb.add("s-tab")
    def _(event):
        perms.cycle()
        event.app.invalidate()

    def cols():
        return shutil.get_terminal_size((80, 24)).columns

    def message():
        out = []
        # the status line is live information: leave it out of the copy of the prompt that
        # stays in the scrollback after Enter
        if LIVE.busy and not session.app.is_done:
            st = LIVE.status
            out += (st.line() if st else [("class:hint", "  working… (ctrl+c to interrupt)")]) + [("", "\n")]
        out.append(("class:rule", "─" * cols() + "\n"))
        q = LIVE.question
        out.append(("class:warn", q[0]) if q else ("class:prompt", "❯ "))
        return FormattedText(out)

    def placeholder():
        if LIVE.busy and not LIVE.question:
            return FormattedText([("class:hint", "/btw to ask on the side · a message is read at tai's next step")])
        return ""

    def toolbar():
        rule = ("class:rule", "─" * cols() + "\n")
        if time.time() - state["ctrl_c"] < 2:
            return FormattedText([rule, ("class:warn", "  Press Ctrl+C again to exit")])
        where = short_path(Path.cwd(), 32)
        mode = "agent" if conv["tools"] else "chat"
        line = [rule, ("class:model", f"  {conv['model']}"), ("class:hint", f" · {mode} · {where}")]
        used = USAGE.inp + USAGE.out
        if used:
            line.append(("class:hint", f" · {human(used)} tokens"))
        if USAGE.ctx_text():
            line.append(("class:hint", f" · {USAGE.ctx_text()}"))
        line.append(("class:rule", "\n"))
        if conv["tools"]:
            cls = {"manual": "class:hint", "accept-edits": "class:modeedit", "auto": "class:modeauto",
                   "plan": "class:modeplan"}[perms.mode]
            icon = {"manual": "  ⏸ manual", "accept-edits": "  ⏵ accept edits", "auto": "  ⏵⏵ auto",
                    "plan": "  ◇ plan"}[perms.mode]
            line += [(cls, icon), ("class:hint", " (shift+tab to cycle)")]
        else:
            line.append(("class:hint", "  chat mode"))
        line.append(("class:hint", "   / commands · alt+enter newline · ctrl+c ×2 quit"))
        return FormattedText(line)

    style = Style.from_dict({
        "rule": "#5a5a5a", "prompt": f"{GOLD} bold", "model": GOLD, "hint": "#8a8a8a",
        "warn": "#e0a040 bold", "modeedit": f"{BLUE} bold", "modeauto": "#d0605a bold",
        "modeplan": "#4fa88a bold",
        "spin": f"{GOLD} bold",
        "bottom-toolbar": "noreverse bg:default",
        "completion-menu.completion": "bg:#2b2b2b #d0d0d0",
        "completion-menu.completion.current": f"bg:{GOLD} #000000",
        "completion-menu.meta.completion": "bg:#2b2b2b #8a8a8a",
        "completion-menu.meta.completion.current": f"bg:{GOLD} #000000"})
    session = PromptSession(history=FileHistory(str(HISTORY)), completer=Slash(),
                            complete_while_typing=True, key_bindings=kb, bottom_toolbar=toolbar,
                            style=style, refresh_interval=0.25, placeholder=placeholder)
    LIVE.invalidate = lambda: session.app.invalidate() if session.app.is_running else None

    def refill():
        # runs once the prompt is up, i.e. after prompt_toolkit took over Ctrl+C: messages
        # given back by an earlier Ctrl+C (signal handler) can't miss this prompt
        text, LIVE.refill = LIVE.refill, ""
        if text:
            session.default_buffer.insert_text(text)

    def read():
        if not LIVE.busy:
            print()
        return session.prompt(message, pre_run=refill)
    read.live = sys.stdout.isatty()     # the prompt can stay open while tai works
    return read


def _readline_reader(state):
    try:
        import readline
        try:
            readline.read_history_file(HISTORY)
        except OSError:
            pass
        readline.set_history_length(2000)
        import atexit
        atexit.register(lambda: readline.write_history_file(HISTORY))
    except (ImportError, OSError):
        pass

    def read():
        while True:
            try:
                return read_input("\n❯ ")
            except KeyboardInterrupt:
                if time.time() - state["ctrl_c"] < 2:
                    raise EOFError
                state["ctrl_c"] = time.time()
                print("\n  (press Ctrl+C again to exit)")
    return read


def model_window(model):
    """Look up the model's context window in the background (for the ctx % display and
    automatic compacting)."""
    def look():
        try:
            for m in list_models():
                if m["id"] == model:
                    USAGE.max_ctx = m.get("max_input_tokens")
                    return
            USAGE.max_ctx = None
        except Exception:
            pass
        finally:
            USAGE.window_known.set()
    USAGE.window_known.clear()
    threading.Thread(target=look, daemon=True).start()


def stop_turn():
    """Ctrl+C while tai works: stop the running turn at once. Returns unsent queued messages."""
    CANCEL.set()
    abort_streams()
    queued = take_inbox()
    say("  [interrupted]" + (" · your queued message is back in the input" if queued else ""), "red")
    return queued


# commands that make sense while tai is working (others change what the turn is working on)
BUSY_OK = {"/btw", "/mode", "/usage", "/help", "/model", "/models", "/file", "/save", "/permissions", "/jobs",
           "/remember",
           "/exit", "/quit", "/q"}


def repl(conv, perms, markdown, pending=None):
    pending = pending or []
    welcome(conv, perms)
    model_window(conv["model"])
    reader = make_reader(conv, perms)
    live = getattr(reader, "live", False)
    worker = None

    def start(content=None, job=None):
        """Run the turn (or job, e.g. /compact), then any messages queued meanwhile; in the
        background when live, so the prompt stays open."""
        nonlocal worker
        if not live:
            job() if job else ask(conv, perms, markdown, content)
            return

        def run():
            c = content
            try:
                if job:
                    CANCEL.clear()
                    job()
                    with INBOX_LOCK:
                        if not INBOX:
                            LIVE.busy = False
                            return
                        c = join_content(INBOX)
                        INBOX.clear()
                while True:
                    ask(conv, perms, markdown, c)
                    with INBOX_LOCK:
                        # stop_turn() took what was queued before Ctrl+C; anything here was
                        # typed after it (while the stopped turn wound down): run it
                        if INBOX:
                            c = join_content(INBOX)
                            INBOX.clear()
                            continue
                        LIVE.busy = False
                        break
            except Exception as e:      # never leave the prompt in 'working' state
                err(f"tai: internal error: {e}")
            finally:
                LIVE.busy, LIVE.status = False, None
                LIVE.invalidate()
        LIVE.busy = True
        worker = threading.Thread(target=run, daemon=True, name="tai-turn")
        worker.start()

    def finish():
        if LIVE.busy:
            stop_turn()
        if worker:
            worker.join(15)

    if live:
        from prompt_toolkit.patch_stdout import patch_stdout
        screen = patch_stdout(raw=True)
        LIVE.active = True
    else:
        screen = contextlib.nullcontext()
    try:
        with screen:
            _repl_loop(conv, perms, markdown, pending, reader, start)
            finish()
    finally:
        LIVE.active = False
        if len(conv["messages"]) > 1:
            say("\n  Bye! Resume this conversation with  tai -c", GREY)


def interrupted():
    """Ctrl+C that reached tai's own code (between two prompts): same as the key."""
    if LIVE.busy:
        LIVE.refill = "\n\n".join(filter(None, [LIVE.refill, *stop_turn()]))


def _repl_loop(conv, perms, markdown, pending, reader, start):
    while True:
        try:
            text = reader().strip()
            if _repl_line(text, conv, perms, markdown, pending, start) == "exit":
                break
        except EOFError:
            break
        except KeyboardInterrupt:
            interrupted()


def _repl_line(text, conv, perms, markdown, pending, start):
    """Handle one line typed at the prompt. Returns "exit" to end the session."""
    if LIVE.answer(text):           # the answer to an approval asked by the running turn
        return
    if not text:
        return
    busy = LIVE.busy
    if text.startswith("/"):
        cmd, _, arg = text.partition(" ")
        own = custom_commands().get(cmd[1:].lower())
        if own:                     # one of your own commands: its text is the message
            text = expand_command(own, arg)
    if text.startswith("/"):
        cmd, _, arg = text.partition(" ")
        arg = arg.strip()
        if busy and cmd not in BUSY_OK:
            err(f"{cmd} has to wait until tai is done (ctrl+c stops it)")
            return
        if cmd in ("/exit", "/quit", "/q"):
            return "exit"
        elif cmd == "/help":
            print(HELP)
            print(commands_help())
        elif cmd == "/models":
            print_models(conv["model"])
        elif cmd == "/model":
            if arg:
                conv["model"] = arg
                model_window(arg)
            dim(f"model: {conv['model']}")
        elif cmd == "/mode":
            if arg and arg not in MODES:
                err(f"unknown mode {arg} — use one of: {', '.join(MODES)}")
                return
            if arg:
                perms.mode = arg
            dim(f"mode: {perms.mode} — {mode_info()[perms.mode]}"
                + ("" if arg else f"  (shift+tab or /mode {'|'.join(MODES)})"))
        elif cmd == "/usage":
            dim(f"session: {human(USAGE.inp)} in · {human(USAGE.out)} out · "
                f"{human(USAGE.inp + USAGE.out)} total"
                + (f" · {USAGE.ctx_text()}" if USAGE.ctx_text() else ""))
        elif cmd == "/btw":
            if not arg:
                err("usage: /btw QUESTION")
                return
            if LIVE.active:      # answered alongside whatever tai is doing
                threading.Thread(target=btw, args=(conv, markdown, arg), daemon=True).start()
            else:
                btw(conv, markdown, arg)
        elif cmd == "/tools":
            conv["tools"] = not conv["tools"]
            reset_system(conv)
            dim(f"tools {'on (agent mode)' if conv['tools'] else 'off (chat mode)'}")
        elif cmd == "/clear":
            conv["messages"] = conv["messages"][:1]
            conv["turns"] = []
            USAGE.ctx = 0
            pending.clear()
            dim("conversation cleared")
        elif cmd == "/compact":
            start(job=lambda: compact_conv(conv, arg))
        elif cmd == "/rewind":
            rewind(conv, arg)
        elif cmd == "/review":
            try:
                content, label = review_request(arg)
            except RuntimeError as e:
                err(f"tai: {e}")
                return
            dim(f"  reviewing {label} · tai only reads files for this")
            tools = review_tools(TOOLS) if conv["tools"] else None
            start(job=lambda: ask(conv, perms, markdown, content, tools=tools))
        elif cmd == "/jobs":
            jobs_command(arg)
        elif cmd == "/remember":
            if not arg:
                dim(memory_text())
                dim("  /remember TEXT adds a line for all your projects; /remember project TEXT for this one")
                return
            here, _, rest = arg.partition(" ")
            project = here.lower() == "project" and rest.strip()
            try:
                path = remember(rest if project else arg, bool(project))
            except (OSError, ToolError, ValueError) as e:
                err(f"tai: could not save it: {e}")
                return
            msgs = conv["messages"]
            if msgs and msgs[0].get("role") == "system":
                msgs[0] = {"role": "system", "content": system_prompt(conv["tools"], conv.get("extra_system"))}
            dim(("  remembered for this project" if project else "  remembered for all your projects")
                + f" (in {show_path(path)})")
        elif cmd == "/permissions":
            lines = SETTINGS.describe()
            dim(f"mode: {perms.mode} — {mode_info()[perms.mode]}")
            if perms.always:
                dim("  allowed this session: " + ", ".join(sorted(perms.always)))
            for line in lines or ["  no saved rules yet (answer 2 to an approval to save one)"]:
                dim(line)
            dim(f"  rules are saved in {SETTINGS.path} — edit it to add or remove them")
            dim("  " + SANDBOX.describe())
        elif cmd == "/system":
            if arg:
                conv["extra_system"] = arg
                reset_system(conv)
                dim("instructions set")
            else:
                dim(conv.get("extra_system") or "(no extra instructions)")
        elif cmd == "/file":
            try:
                pending.append(read_file_block(arg))
                dim(f"attached {arg} (sent with your next message)")
            except RuntimeError as e:
                err(str(e))
        elif cmd == "/save":
            if not arg:
                err("usage: /save PATH")
                return
            try:
                Path(arg).expanduser().write_text(to_markdown(conv), encoding="utf-8")
                dim(f"saved to {arg}")
            except OSError as e:
                err(f"cannot save: {e.strerror}")
        else:
            err(f"unknown command {cmd} — /help")
        return

    content = join_content(pending + [text])
    pending.clear()
    with INBOX_LOCK:
        if LIVE.busy:
            INBOX.append(content)
            queued = True
        else:
            queued = False
    if queued:
        dim("  queued · tai reads it at its next step (ctrl+c stops tai and gives it back)")
    else:
        start(content)


def print_models(current=None):
    try:
        models = list_models()
    except RuntimeError as e:
        return err(f"tai: {e}")
    for m in models:
        mark = "*" if m["id"] == current else " "
        ctx = m.get("max_input_tokens")
        extra = f"  {ctx // 1000}k ctx" if ctx else ""
        if m.get("mode") == "embedding":
            extra += "  (embedding)"
        print(f" {mark} {m['id']}{extra}")


# ================================================================ IDE server (tai --serve)
#
# A JSON-lines protocol on stdin/stdout, used by the tai VS Code extension.
#   client → server   {"id": n, "method": "...", "params": {...}}   answered with {"id": n, "result"|"error"}
#   server → client   {"method": "event", "params": {"type": "...", ...}}   (notifications)
#                     {"id": "s<n>", "method": "approval" | "ide/...", "params": {...}}   (requests the
#                     client answers with {"id": "s<n>", "result": ...})
# The agent loop mirrors run_turn/subagent above, but reports structured events instead of printing.

PROTOCOL_VERSION = 1
TAI_VERSION = "1.3.6"
SESSIONS = STATE_DIR / "sessions"
MAX_PREVIEW = 2_000_000         # larger files are not sent to the editor for diff previews
MAX_SNAPSHOT = 5_000_000        # larger files are not snapshotted for rewind
MAX_ATTACH = 200_000            # characters per attached file

IDE_TOOLS = [
    fn("get_diagnostics", "Get the errors and warnings VS Code currently reports (the Problems panel) "
       "for one file, or for all files if no path is given. Use it after editing code.",
       {"path": {"type": "string", "description": "file path; omit for all files"}}, []),
]


class Cancelled(Exception):
    def __init__(self, partial=""):
        super().__init__("cancelled")
        self.partial = partial


ABORTED = object()


def pumped(model, msgs, tools, cancelled, tool_choice=None):
    """stream_reply() events, read by a helper thread so that cancelled() takes effect at once,
    even while the API has not answered yet. Raises Cancelled when cancelled() becomes true."""
    q, stop = queue.Queue(), threading.Event()
    msgs = list(msgs)

    def produce():
        gen = stream_reply(model, msgs, tools, tool_choice)
        try:
            for ev in gen:
                if stop.is_set():
                    break
                q.put(ev)
        except BaseException as e:      # noqa: B902 — handed to the consumer
            q.put(("error", e))
        finally:
            gen.close()
            q.put(("end", None))
    threading.Thread(target=produce, daemon=True).start()
    try:
        while True:
            try:
                kind, val = q.get(timeout=0.05)
            except queue.Empty:
                if cancelled():
                    raise Cancelled()
                continue
            if kind == "end":
                return
            if cancelled():
                raise Cancelled()
            if kind == "error":
                raise val
            yield kind, val
    finally:
        stop.set()


class Proto:
    """JSON-lines transport. Thread-safe; requests to the client block their calling thread."""

    def __init__(self, out):
        self.out = out
        self.lock = threading.Lock()
        self.pending = {}
        self.n = 0
        self.closed = threading.Event()

    def send(self, obj):
        line = json.dumps(obj)          # ASCII escapes: lone surrogates can't break the pipe
        with self.lock:
            try:
                self.out.write(line + "\n")
                self.out.flush()
            except OSError:
                self.closed.set()
            except ValueError:          # stdout closed
                self.closed.set()

    def event(self, type_, **params):
        self.send({"method": "event", "params": {"type": type_, **params}})

    def reply(self, id_, result=None, error=None):
        if error is not None:
            self.send({"id": id_, "error": {"message": str(error)}})
        else:
            self.send({"id": id_, "result": result})

    def request(self, method, params, timeout=None, abort=None):
        """Ask the client and wait. Returns its result; None if it answered with an error, went
        away or timed out; ABORTED if abort() became true (the client is told to drop the request)."""
        ev = threading.Event()
        slot = {}
        with self.lock:
            self.n += 1
            rid = f"s{self.n}"
            self.pending[rid] = (ev, slot)
        self.send({"id": rid, "method": method, "params": params})
        deadline = time.time() + timeout if timeout else None
        try:
            while not ev.wait(0.05):
                if self.closed.is_set():
                    return None
                if abort and abort():
                    self.event("request_cancelled", requestId=rid)
                    return ABORTED
                if deadline and time.time() > deadline:
                    self.event("request_cancelled", requestId=rid)
                    return None
            return slot.get("result")
        finally:
            with self.lock:
                self.pending.pop(rid, None)

    def resolve(self, msg):
        with self.lock:
            entry = self.pending.get(msg.get("id"))
        if entry:
            ev, slot = entry
            slot["result"] = msg.get("result") if "error" not in msg else None
            ev.set()


class ServerPermissions(Permissions):
    """Approvals are asked in the editor; parallel sub-agents may wait on several at once."""

    def __init__(self, proto, mode="manual"):
        super().__init__(mode)
        self.proto = proto

    def check(self, kind, question, title="Permission", body=None, detail=None):
        if self.preflight(kind, detail):
            return
        always, remember = self.always_choice(kind, detail)
        shown = dict(detail or {})
        for k in ("old", "new"):
            if isinstance(shown.get(k), str) and len(shown[k]) > MAX_PREVIEW:
                shown[k] = None
                shown["tooLarge"] = True

        def settled():
            try:
                return CANCEL.is_set() or self.preflight(kind, detail)
            except (Denied, ToolError):
                return True
        res = self.proto.request("approval", {
            "kind": kind, "title": title, "question": question, "always": always,
            "labels": list(choice_labels(kind, always)),
            "detail": shown, "agent": getattr(_tls, "label", "")}, abort=settled)
        if self.preflight(kind, detail):
            return                  # approved meanwhile ("always" in another card, or mode changed)
        if res is ABORTED or CANCEL.is_set():
            raise Denied("Cancelled.")
        if not isinstance(res, dict):
            raise Denied("The editor did not answer the approval request.")
        decision = res.get("decision")
        if decision == "always":
            remember()
            return
        if decision == "yes":
            return
        raise Denied(str(res.get("feedback") or "").strip())


def describe_call(name, args):
    """(label, shown) for a tool call, like the terminal's '● Label(shown)'."""
    if name == "get_diagnostics":
        p = (args or {}).get("path")
        try:
            return "Diagnostics", show_path(resolve(p)) if isinstance(p, str) and p else "workspace"
        except (OSError, ValueError, RuntimeError):
            return "Diagnostics", str(p)
    label, key = LABELS.get(name, (name, None))
    shown = args.get(key, "") if (args and key) else ""
    if not isinstance(shown, str):
        shown = str(shown)
    if name == SHELL_TOOL and args and args.get("run_in_background"):
        label += " (background)"
    try:
        if key == "path" and shown:
            shown = show_path(resolve(shown))
        if name == "list_files" and args and args.get("path"):
            shown = f"{show_path(resolve(args['path']))}/{shown}"
    except (OSError, ValueError, RuntimeError):
        pass
    return label, shown


def ui_args(args, cap=4000):
    if not isinstance(args, dict):
        return {}
    return {k: (v[:cap] + "…" if isinstance(v, str) and len(v) > cap else v) for k, v in args.items()}


def consistent_prefix(msgs):
    """Messages up to the last point where every tool call has its result (for side requests
    made while a turn is still running)."""
    out, i = [], 0
    while i < len(msgs):
        m = msgs[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = {tc["id"] for tc in m["tool_calls"]}
            j, got = i + 1, set()
            while j < len(msgs) and msgs[j].get("role") == "tool":
                got.add(msgs[j].get("tool_call_id"))
                j += 1
            if not ids <= got:
                break
            out.extend(msgs[i:j])
            i = j
            continue
        out.append(m)
        i += 1
    return out


def _fraction(v, default=0.8):
    v = str(v).strip().lower()
    if v in ("0", "off", "no", "false"):
        return 0.0
    try:
        f = float(v.rstrip("%"))
    except ValueError:
        return default
    return f / 100 if f > 1 else f


# Compacting: when the conversation nears the model's context window (TAI_AUTO_COMPACT,
# default 80%; 0 turns it off), or the API says the request is too long, tai replaces the
# conversation with a summary written by the model and carries on.
COMPACT_AT = _fraction(os.environ.get("TAI_AUTO_COMPACT", "0.8"))
CONTEXT_FULL = re.compile(r"context[ _-]?(length|window|limit)|maximum context|too many tokens|"
                          r"prompt is too long|input is too long|reduce the length|token limit", re.I)
COMPACT_ASK = ("Summarize our conversation so far so that the work can continue in a fresh context. "
               "Include: the user's goals and requests, decisions made, files read or changed (paths), "
               "important findings, the current state, and what remains to be done. Be complete but "
               "concise; use markdown. Reply with the summary only.")
COMPACT_MIDWAY = ("\n\nYou are in the middle of a task right now: say exactly what you are doing, which "
                  "steps are done, and what the next step is, so that you can continue without the user.")
CONTINUE_NOTE = ("[The conversation was summarized because the context window was nearly full. Continue "
                 "the task you were working on from where you left off. Files may have changed since you "
                 "last read them: read them again before editing.]")


def context_estimate(msgs):
    """Tokens the next request will send: the size at the last reply plus what came after it."""
    last = max((i for i, m in enumerate(msgs) if m.get("role") == "assistant"), default=None)
    if last is None or not USAGE.ctx:
        return msgs_tokens(msgs)
    return USAGE.ctx + msgs_tokens(msgs[last + 1:])


def compact_due(msgs):
    if not COMPACT_AT or not any(m.get("role") == "assistant" for m in msgs):
        return False
    USAGE.window_known.wait(5)      # the context window may still be being looked up
    return bool(USAGE.max_ctx) and context_estimate(msgs) >= COMPACT_AT * USAGE.max_ctx


def _shrunk(m):
    m = without_images(m, "[image left out]")
    c = m.get("content")
    if m.get("role") in ("tool", "user") and isinstance(c, str) and len(c) > 4000:
        return {**m, "content": c[:2500] + f"\n[... {len(c) - 3500} characters left out ...]\n" + c[-1000:]}
    return m


def summarize(reply, msgs, instructions="", midway=False):
    """The model's summary of msgs. reply(messages) -> text. If the request itself is too long,
    it is tried once more with long tool outputs and attachments cut down."""
    ask = COMPACT_ASK + (COMPACT_MIDWAY if midway else "") + (f"\n\nAlso: {instructions}" if instructions else "")
    base = consistent_prefix(list(msgs))
    for shrink in (False, True):
        req = ([base[0]] + [_shrunk(m) for m in base[1:]]) if shrink else base
        try:
            text = reply(req + [{"role": "user", "content": ask}])
        except RuntimeError as e:
            if not shrink and CONTEXT_FULL.search(str(e)):
                continue
            raise
        if not text.strip():
            raise RuntimeError("the model returned an empty summary")
        return text.strip()


def compacted_messages(system, summary, midway, todos=None):
    if todos and any(t["status"] != "completed" for t in todos):
        summary += "\n\nYour task list (todo_write) at this point:\n" + todo_text(todos)
    out = [system, {"role": "user", "content": "[Summary of the earlier conversation]\n\n" + summary},
           {"role": "assistant", "content": "Understood. I'll continue from this summary."}]
    if midway:
        out.append({"role": "user", "content": CONTINUE_NOTE})
    return out


def clean_text(s):
    """Replaces lone UTF-16 surrogates (e.g. half an emoji cut off by a selection limit)."""
    s = str(s or "")
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def clean(obj):
    if isinstance(obj, str):
        return clean_text(obj)
    if isinstance(obj, list):
        return [clean(x) for x in obj]
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    return obj


def fill_missing_results(msgs, note="Error: the tool call was not completed."):
    """Every assistant tool call needs a tool result, or the API rejects the conversation."""
    i = 0
    while i < len(msgs):
        m = msgs[i]
        i += 1
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        j, got = i, set()
        while j < len(msgs) and msgs[j].get("role") == "tool":
            got.add(msgs[j].get("tool_call_id"))
            j += 1
        missing = [{"role": "tool", "tool_call_id": tc["id"], "content": note}
                   for tc in m["tool_calls"] if tc["id"] not in got]
        msgs[j:j] = missing
        i = j + len(missing)


def new_session_id():
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}"


def session_path(sid):
    if not re.fullmatch(r"[\w.-]+", sid or ""):
        raise ValueError("bad session id")
    return SESSIONS / f"{sid}.json"


def write_json_atomic(path, obj, indent=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, indent=indent), encoding="utf-8")
    os.replace(tmp, path)


def snapshot(path):
    """Contents of a file before tai changes it: None (did not exist), text, or base64 bytes."""
    import base64
    try:
        if not path.exists():
            return None
        if path.stat().st_size > MAX_SNAPSHOT:
            return {"skipped": "too large"}
        data = path.read_bytes()
    except OSError as e:
        return {"skipped": e.strerror or "unreadable"}
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return {"b64": base64.b64encode(data).decode()}


def restore(path, snap):
    import base64
    if isinstance(snap, dict) and "skipped" in snap:
        raise OSError(f"no snapshot ({snap['skipped']})")
    if snap is None:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(snap, dict):
        path.write_bytes(base64.b64decode(snap["b64"]))
    else:
        path.write_bytes(snap.encode("utf-8"))      # exact bytes back, line endings included


class Server:
    def __init__(self, proto, model, mode):
        self.proto = proto
        self.perms = ServerPermissions(proto, mode)
        self.perms.on_mode = lambda m: self.proto.event("state", state=self.state())
        self.lock = threading.RLock()
        self.busy = False
        self.compacting = False
        self.inbox = []             # messages sent while a turn runs; injected at the next step
        self.worker = None
        self.caps = {}
        self.cur_turn = None
        self.streaming_text = ""
        self.btw_cancel = {}
        self.session = None
        self.new_session(model, True, None)
        IO_HOOKS["before_write"] = self._before_write
        IO_HOOKS["after_write"] = self._after_write
        IO_HOOKS["todos"] = self._todos
        IO_HOOKS["job_done"] = lambda j: self.proto.event("job_done", job=j.info())

    # ------------------------------------------------------------ session state
    @property
    def conv(self):
        return self.session["conv"]

    @property
    def ide(self):
        return bool(self.caps.get("ide"))

    def new_session(self, model, tools, extra):
        self.session = {"id": new_session_id(), "title": "", "cwd": str(Path.cwd()),
                        "created": time.time(), "updated": time.time(),
                        "conv": {"model": model, "tools": tools, "extra_system": extra,
                                 "messages": [], "display": {}},
                        "turns": []}
        self.reset_system()
        USAGE.ctx = 0

    def reset_system(self):
        c = self.conv
        msgs = [m for m in c["messages"] if m.get("role") != "system"]
        c["messages"] = [{"role": "system", "content": system_prompt(
            c["tools"], c.get("extra_system"), ide=self.ide)}] + msgs

    def meta(self, s=None):
        s = s or self.session
        c = s["conv"]
        return {"id": s["id"], "title": s.get("title") or "", "cwd": s.get("cwd"),
                "created": s.get("created"), "updated": s.get("updated"), "model": c["model"],
                "tools": c["tools"], "system": c.get("extra_system") or "",
                "messages": sum(m.get("role") == "user" for m in c["messages"])}

    def save(self):
        s = self.session
        if not any(m.get("role") == "user" for m in self.conv["messages"]):
            return
        s["updated"] = time.time()
        try:
            write_json_atomic(session_path(s["id"]), s)
            save_conv({k: v for k, v in self.conv.items()})     # so `tai -c` continues it
        except Exception as e:
            self.proto.event("notice", level="warning", text=f"could not save the session: {e}")
        self.proto.event("session", session=self.meta())

    def tools(self, sub=False):
        if not self.conv["tools"]:
            return None
        base = SUB_TOOLS if sub else TOOLS
        return base + IDE_TOOLS if self.ide else base

    def state(self):
        return {"session": self.meta(), "model": self.conv["model"], "mode": self.perms.mode,
                "tools": self.conv["tools"], "system": self.conv.get("extra_system") or "",
                "busy": self.busy, "usage": self.usage(), "todos": self.conv.get("todos") or [],
                "jobs": [j.info() for j in JOBS.values()],
                "commands": [{"name": n, "description": c["description"], "scope": c["scope"]}
                             for n, c in sorted(custom_commands().items())]}

    def usage(self):
        return {"input": USAGE.inp, "output": USAGE.out, "ctx": USAGE.ctx, "maxCtx": USAGE.max_ctx}

    def emit_usage(self):
        self.proto.event("usage", usage=self.usage())

    # ------------------------------------------------------------ checkpoints
    def _before_write(self, path, old):
        with self.lock:
            turn = self.cur_turn
            if turn is not None and str(path) not in turn["files"]:
                turn["files"][str(path)] = snapshot(path)

    def _todos(self, items):
        self.conv["todos"] = items
        self.proto.event("todos", items=items)

    def add_note(self, text):
        """A message for the model that the user doesn't see as theirs (e.g. a job finished)."""
        with self.lock:
            msgs = self.conv["messages"]
            msgs.append({"role": "user", "content": text})
            self.conv.setdefault("display", {})[str(len(msgs) - 1)] = {"kind": "hidden"}

    def _after_write(self, path, old, new):
        _tls.last_write = (path, old, new)
        self.proto.event("file_changed", path=str(path), created=old is None)

    def rewind_files(self, msg_index):
        """Files changed in turns at or after msg_index, oldest snapshot per file."""
        files = {}
        for t in reversed(self.session["turns"]):
            if t["msg"] >= msg_index:
                files.update(t["files"])       # earlier turns overwrite: oldest snapshot wins
        return files

    # ------------------------------------------------------------ transcript for the UI
    def transcript(self):
        msgs, disp = self.conv["messages"], self.conv.get("display") or {}
        items, results = [], {}
        for m in msgs:
            if m.get("role") == "tool":
                results[m.get("tool_call_id")] = text_of(m.get("content"))
        for i, m in enumerate(msgs):
            role = m.get("role")
            if role == "user":
                d = disp.get(str(i)) or {}
                if d.get("kind") == "hidden":
                    continue
                items.append({"type": "user", "index": i, "text": d.get("text", text_of(m.get("content"))),
                              "files": d.get("files") or [], "context": d.get("context") or "",
                              "kind": d.get("kind") or "", "time": d.get("time"), "images": d.get("images") or []})
            elif role == "assistant":
                if m.get("content"):
                    items.append({"type": "assistant", "text": m["content"]})
                for tc in m.get("tool_calls") or []:
                    name = tc["function"]["name"]
                    args = parse_args({"arguments": tc["function"].get("arguments")}) or {}
                    res = results.get(tc["id"], "")
                    if name == "task":
                        items.append({"type": "task", "id": tc["id"],
                                      "label": clip(args.get("description") or args.get("prompt") or "", 40),
                                      "prompt": args.get("prompt") or "", "result": res,
                                      "done": tc["id"] in results})
                        continue
                    label, shown = describe_call(name, args)
                    bad = res.startswith(("Error", "The user denied", "Not run", "Interrupted"))
                    done = tc["id"] in results
                    items.append({"type": "tool", "id": tc["id"], "name": name, "label": label,
                                  "shown": shown, "args": ui_args(args), "output": res[:6000],
                                  "summary": clip(res.splitlines()[0] if res else "", 120) if done else "",
                                  "error": bad, "done": done})
        if self.busy and self.streaming_text:
            items.append({"type": "assistant", "text": self.streaming_text, "streaming": True})
        return items

    # ------------------------------------------------------------ composing user messages
    def compose(self, p):
        text = str(p.get("text") or "")
        blocks, files, images, pasted = [], [], [], []
        for im in p.get("images") or []:          # pasted into the chat (data: URLs)
            name = str((im or {}).get("name") or "image") if isinstance(im, dict) else "image"
            try:
                images.append(data_url_part(im.get("data") if isinstance(im, dict) else im))
                pasted.append(name)
            except (ToolError, ValueError) as e:
                raise ValueError(f"cannot attach {name}: {e}") from None
        for a in p.get("attachments") or []:
            try:
                path = resolve(a["path"])
                rel = show_path(path)
                rng = a.get("range")
                if is_image(path) and path.is_file():
                    images.append(image_file_part(path)[0])
                    blocks.append(f'<file path="{rel}">(image, attached below)</file>')
                    files.append(rel)
                    continue
                if path.is_dir():
                    names = sorted(x.name + ("/" if x.is_dir() else "") for x in path.iterdir()
                                   if x.name not in SKIP_DIRS)[:500]
                    blocks.append(f'<directory path="{rel}">\n' + "\n".join(names) + "\n</directory>")
                    files.append(rel + "/")
                    continue
                if path.suffix.lower() in DOC_TYPES:
                    body = document_text(path)
                elif is_binary(path):
                    blocks.append(f'<file path="{rel}">(binary file, not attached)</file>')
                    files.append(rel)
                    continue
                else:
                    body = read_text(path)
                label = rel
                if rng:
                    a0, b0 = int(rng[0]), int(rng[1])
                    lines = body.splitlines()
                    body = "\n".join(f"{n:>6}\t{l}" for n, l in
                                     enumerate(lines[a0 - 1:b0], a0))
                    label = f"{rel}:{a0}-{b0}" if b0 != a0 else f"{rel}:{a0}"
                if len(body) > MAX_ATTACH:
                    body = body[:MAX_ATTACH] + f"\n[... truncated; read_file {rel} for the rest]"
                blocks.append(f'<file path="{label}">\n{body}\n</file>')
                files.append(label)
            except (ToolError, OSError, KeyError, ValueError, TypeError) as e:
                blocks.append(f"<file path=\"{a.get('path')}\">(could not attach: {e})</file>")
                files.append(str(a.get("path")))
        ctx, ctx_label = p.get("context") or {}, ""
        if ctx.get("path"):
            try:
                rel = show_path(resolve(ctx["path"]))
            except (OSError, ValueError):
                rel = ctx["path"]
            parts = [f"The user has {rel} open in the editor."]
            ctx_label = rel
            sel = ctx.get("selection") or {}
            if sel.get("text"):
                a0, b0 = sel.get("start"), sel.get("end")
                sel_text = sel["text"][:20000]
                parts.append(f"Selected (lines {a0}-{b0}):\n```\n{sel_text}\n```")
                ctx_label = f"{rel}:{a0}-{b0}" if a0 != b0 else f"{rel}:{a0}"
            blocks.insert(0, "<ide_context>\n" + "\n".join(parts) + "\n</ide_context>")
        content = "\n\n".join(blocks + [text]) if blocks else text
        display = {"text": text, "files": files, "context": ctx_label, "time": time.time()}
        if pasted:
            display["images"] = pasted
        return with_images(content, images), display

    def add_user(self, content, display, client_id=None, injected=False):
        with self.lock:
            msgs = self.conv["messages"]
            idx = len(msgs)
            msgs.append({"role": "user", "content": content})
            self.conv.setdefault("display", {})[str(idx)] = display
            self.cur_turn = {"msg": idx, "files": {}}
            self.session["turns"].append(self.cur_turn)
            if not self.session.get("title"):
                self.session["title"] = clip(display.get("text") or text_of(content) or "image", 80)
        self.proto.event("user", index=idx, clientId=client_id, injected=injected,
                         text=display.get("text", ""), files=display.get("files") or [],
                         context=display.get("context") or "", images=display.get("images") or [])
        return idx

    def drop_user(self, idx):
        with self.lock:
            msgs = self.conv["messages"]
            if len(msgs) == idx + 1:
                msgs.pop()
                self.conv.get("display", {}).pop(str(idx), None)
                turns = self.session["turns"]
                if turns and turns[-1]["msg"] == idx:
                    turns.pop()
                if not any(m.get("role") == "user" for m in msgs):
                    self.session["title"] = ""

    # ------------------------------------------------------------ model streaming
    def stream(self, model, msgs, tools, main=True, agent=None, on_text=None, tool_choice=None):
        parts, streamed = [], 0
        last = [0.0]
        calls, usage = [], None
        gen = pumped(model, msgs, tools, CANCEL.is_set, tool_choice)
        try:
            for kind, val in gen:
                if kind == "text":
                    parts.append(val)
                    n = est_tokens(val)
                    streamed += n
                    USAGE.stream(n)
                    if on_text:
                        on_text(val)
                elif kind == "tool":
                    n = est_tokens(val[1])
                    streamed += n
                    USAGE.stream(n)
                    if main and time.time() - last[0] > 0.15:
                        last[0] = time.time()
                        self.proto.event("tool_writing", name=val[0], tokens=streamed, agent=agent)
                else:
                    calls, usage = val
        except Cancelled:
            raise Cancelled("".join(parts)) from None
        except Exception as e:
            if CANCEL.is_set():
                raise Cancelled("".join(parts))
            if isinstance(e, RuntimeError):
                raise
            raise RuntimeError(f"connection lost: {e.__class__.__name__}: {e}") from None
        finally:
            gen.close()
            USAGE.stream(-streamed)
        USAGE.add(usage, main=main)
        self.emit_usage()
        return "".join(parts), calls, usage

    # ------------------------------------------------------------ tools
    def runners(self):
        r = dict(RUNNERS)
        r["get_diagnostics"] = self.tool_diagnostics
        return r

    def tool_diagnostics(self, a, perms):
        path = a.get("path")
        target = str(resolve(path)) if path else None
        res = self.proto.request("ide/diagnostics", {"path": target}, timeout=15)
        if not isinstance(res, list):
            raise ToolError("VS Code did not return diagnostics")
        lines = []
        for d in res[:300]:
            try:
                where = show_path(Path(d["path"]))
            except (KeyError, TypeError, ValueError):
                continue
            src = f" [{d['source']}]" if d.get("source") else ""
            lines.append(f"{where}:{d.get('line')}:{d.get('col')}: {d.get('severity')}: "
                         f"{d.get('message')}{src}")
        if not lines:
            return "(no problems reported)", "no problems"
        n_err = sum(d.get("severity") == "error" for d in res)
        return "\n".join(lines), f"{n_err} errors, {len(res) - n_err} other"

    def run_tool(self, call, agent=None):
        try:
            return self._run_tool(call, agent)
        except Exception as e:      # never leave a tool call without a result
            self.proto.event("tool_end", id=call["id"], summary=f"Error: {e}", error=True,
                             output=f"Error: {e}", diff=None, agent=agent)
            return f"Error: {e}"

    def _run_tool(self, call, agent=None):
        args = parse_args(call)
        label, shown = describe_call(call["name"], args)
        self.proto.event("tool_start", id=call["id"], name=call["name"], label=label, shown=shown,
                         args=ui_args(args), agent=agent)
        if (self.caps.get("autosave") and args and isinstance(args.get("path"), str)
                and call["name"] in ("read_file", "edit_file", "write_file")):
            try:
                self.proto.request("ide/saveFile", {"path": str(resolve(args["path"]))}, timeout=5)
            except (OSError, ValueError, RuntimeError):
                pass
        _tls.last_write = None
        try:
            if call["name"] in ("web_search", "web_fetch"):
                result, summary, style = self.interruptible(call)
            else:
                result, summary, style = exec_tool(call, self.perms, self.runners())
        except Exception as e:      # a tool must never take the session down
            result, summary, style = f"Error: {e}", f"Error: {e}", "red"
        if call["name"] == "todo_write" and style != "red":
            m = re.search(r"\d+ of \d+ done", text_of(result))
            summary = m.group(0) if m else summary        # the list itself is in the panel
        diff = None
        lw = getattr(_tls, "last_write", None)
        if lw and style != "red":
            path, old, new = lw
            if len(new) <= MAX_PREVIEW and (old is None or len(old) <= MAX_PREVIEW):
                diff = {"path": str(path), "old": old, "new": new}
        _tls.last_write = None
        self.proto.event("tool_end", id=call["id"], summary=summary, error=style == "red",
                         output=text_of(result)[:6000], diff=diff, agent=agent)
        return result

    def interruptible(self, call):
        """Runs a read-only network tool in a helper thread so that Stop doesn't wait for a slow
        search or download; an abandoned request finishes in the background and is discarded."""
        box, label = {}, getattr(_tls, "label", "")

        def work():
            _tls.label = label
            try:
                box["r"] = exec_tool(call, self.perms, self.runners())
            except Exception as e:
                box["r"] = (f"Error: {e}", f"Error: {e}", "red")
        t = threading.Thread(target=work, daemon=True)
        t.start()
        while t.is_alive():
            t.join(0.05)
            if CANCEL.is_set() and t.is_alive():
                return "Interrupted by the user.", "Interrupted", "red"
        return box["r"]

    def execute(self, calls, model, agent=None):
        results = {}
        tasks = [c for c in calls if c["name"] == "task"]
        for c in calls:
            if c["name"] == "task":
                continue
            if CANCEL.is_set():
                results[c["id"]] = "Not run: the user interrupted."
                continue
            results[c["id"]] = self.run_tool(c, agent)
        if tasks and not CANCEL.is_set():
            results.update(self.run_tasks(tasks, model))
        return [results.get(c["id"], "Not run: the user interrupted.") for c in calls]

    def run_tasks(self, tasks, model):
        results, jobs = {}, []
        for c in tasks:
            a = parse_args(c)
            if not a or not a.get("prompt"):
                results[c["id"]] = "Error: the task tool needs a 'prompt'."
                self.proto.event("task_start", id=c["id"], label="?", prompt="")
                self.proto.event("task_end", id=c["id"], result=results[c["id"]], error=True)
                continue
            label = clip(a.get("description") or a["prompt"], 40)
            self.proto.event("task_start", id=c["id"], label=label, prompt=a["prompt"],
                             model=a.get("model") or model)
            jobs.append((c, a, label))
        if not jobs:
            return results
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL_AGENTS) as pool:
            futs = {pool.submit(self.subagent, c["id"], a, label, a.get("model") or model): c
                    for c, a, label in jobs}
            for fut, c in futs.items():
                try:
                    results[c["id"]], stats = fut.result()
                    err_ = False
                except Exception as e:
                    results[c["id"]], stats, err_ = f"Sub-agent crashed: {e}", {}, True
                self.proto.event("task_end", id=c["id"], result=results[c["id"]][:20000],
                                 error=err_, **stats)
        return results

    def subagent(self, task_id, args, label, model):
        _tls.label = label
        msgs = [{"role": "system", "content": system_prompt(True, sub=True, ide=self.ide)},
                {"role": "user", "content": args["prompt"]}]
        uses = tokens = 0
        text = ""
        try:
            for step in range(SUB_MAX_STEPS):
                if CANCEL.is_set():
                    return ("Sub-agent stopped: the user interrupted."
                            + (f" Notes so far: {text}" if text else ""), {"uses": uses, "tokens": tokens})
                try:
                    text, calls, usage = self.stream(model, msgs, mode_tools(self.tools(sub=True), self.perms, sub=True), main=False,
                                                     agent=task_id)
                except Cancelled as c:
                    text = c.partial
                    continue
                if usage:
                    tokens += (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
                assistant = {"role": "assistant", "content": text or None}
                if calls:
                    for i, c in enumerate(calls):
                        c["id"] = c["id"] or f"call_{task_id}_{step}_{i}"
                    assistant["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                        "name": c["name"], "arguments": c["arguments"] or "{}"}} for c in calls]
                msgs.append(assistant)
                if not calls:
                    return text or "(the sub-agent returned no report)", {"uses": uses, "tokens": tokens}
                if text:
                    self.proto.event("task_note", id=task_id, text=clip(text, 300))
                for c in calls:
                    if CANCEL.is_set():
                        result = "Not run: the user interrupted."
                    else:
                        result = self.run_tool(c, agent=task_id)
                        uses += 1
                    msgs.append({"role": "tool", "tool_call_id": c["id"], "content": result})
            return (f"Sub-agent stopped after {SUB_MAX_STEPS} steps. Last notes: {text}",
                    {"uses": uses, "tokens": tokens})
        except RuntimeError as e:
            return f"Sub-agent failed: {e}", {"uses": uses, "tokens": tokens}
        finally:
            _tls.label = ""

    # ------------------------------------------------------------ turns
    def start_turn(self, content, display, client_id):
        with self.lock:
            if self.compacting:
                raise RuntimeError("tai is compacting the conversation; send your message when it is done")
            if self.busy:
                self.inbox.append((content, display, client_id))
                return True
            self.busy = True
        CANCEL.clear()
        self.worker = threading.Thread(target=self.turn, args=(content, display, client_id), daemon=True)
        self.worker.start()
        return False

    def drain_inbox(self):
        with self.lock:
            items, self.inbox = self.inbox, []
        for content, display, cid in items:
            self.add_user(content, display, cid, injected=True)
        return bool(items)

    def turn(self, content, display, client_id):
        msgs = self.conv["messages"]
        note = finished_jobs_note()
        if note:
            self.add_note(note)
        if self.perms.mode == "plan" and self.conv["tools"] and not display.get("readonly"):
            self.add_note(PLAN_NOTE)
        idx = self.add_user(content, display, client_id)
        self.proto.event("turn_start", index=idx)
        replied, reason, error = False, "done", None
        t_in = t_out = 0
        start = time.time()

        def on_text(chunk):
            self.streaming_text += chunk
            self.proto.event("text", delta=chunk)
        def compact(why_full):
            """Compact before the next step. Before tai has replied, the new message is taken
            out, the rest summarized, and the message added again; afterwards the summary
            includes the task in progress."""
            nonlocal idx
            if not replied:
                self.drop_user(idx)
            ok = self.auto_compact(midway=replied)
            if not replied:
                idx = self.add_user(content, display, client_id)
            return ok
        try:
            step, auto_failed, retried = 0, False, False
            while True:
                if step >= MAX_STEPS:
                    reason = "max_steps"
                    break
                step += 1
                self.streaming_text = ""
                if not auto_failed and compact_due(msgs):
                    auto_failed = not compact(False)
                try:
                    tools = review_tools(self.tools()) if display.get("readonly") else \
                        mode_tools(self.tools(), self.perms)
                    text, calls, usage = self.stream(self.conv["model"], msgs, tools, on_text=on_text)
                except RuntimeError as e:
                    if retried or not CONTEXT_FULL.search(str(e)) or self.streaming_text:
                        raise
                    retried = True          # the API says it's too long: compact and try again
                    if not compact(True):
                        raise
                    continue
                self.streaming_text = ""
                if usage:
                    t_in += usage.get("prompt_tokens") or 0
                    t_out += usage.get("completion_tokens") or 0
                assistant = {"role": "assistant", "content": text or None}
                if calls:
                    for i, c in enumerate(calls):
                        c["id"] = c["id"] or f"call_{idx}_{step}_{i}"
                    assistant["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                        "name": c["name"], "arguments": c["arguments"] or "{}"}} for c in calls]
                elif not text:
                    assistant["content"] = "(no response)"
                    self.proto.event("text", delta="(no response)")
                with self.lock:
                    msgs.append(assistant)
                replied = True
                self.proto.event("message_end")
                if calls:
                    results = self.execute(calls, self.conv["model"])
                    with self.lock:
                        for c, r in zip(calls, results):
                            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": r})
                    if CANCEL.is_set():
                        raise Cancelled()
                if calls:
                    note = finished_jobs_note()
                    if note:
                        self.add_note(note)
                injected = self.drain_inbox()
                if not calls and not injected:
                    break
        except Cancelled as c:
            reason = "cancelled"
            if c.partial:
                with self.lock:
                    msgs.append({"role": "assistant", "content": c.partial})
                replied = True
        except RuntimeError as e:
            reason, error = "error", str(e)
        except SystemExit as e:
            reason, error = "error", str(e.code)
        except Exception as e:
            import traceback
            traceback.print_exc()
            reason, error = "error", f"internal error: {e.__class__.__name__}: {e}"
        finally_ok = False
        try:
            with self.lock:
                fill_missing_results(msgs)
            removed = None
            if not replied:
                removed = {"index": idx, "text": display.get("text", "")}
                self.drop_user(idx)
            self.streaming_text = ""
            self.save()
            finally_ok = True
        except Exception as e:      # never leave the client waiting for turn_end
            import traceback
            traceback.print_exc()
            removed, reason, error = None, "error", error or f"internal error: {e}"
        chain = None
        with self.lock:
            if reason == "done" and self.inbox:
                chain, self.inbox = self.inbox[0], self.inbox[1:]
                unsent = []
            else:
                unsent = [d.get("text", "") for _, d, _ in self.inbox]
                self.inbox = []
                self.busy = False
            self.cur_turn = None
            # emitted under the lock, so a new turn's events can't come before it
            self.proto.event("turn_end", reason=reason, error=error,
                             removedUser=removed if finally_ok else None, unsent=unsent,
                             stats={"input": t_in, "output": t_out, "seconds": round(time.time() - start, 1),
                                    "model": self.conv["model"]}, usage=self.usage())
        if chain:
            CANCEL.clear()
            self.turn(*chain)

    def cancel(self):
        CANCEL.set()
        abort_streams()

    def btw(self, bid, question):
        flag = threading.Event()
        self.btw_cancel[bid] = flag
        msgs = consistent_prefix(list(self.conv["messages"])) + [{"role": "user", "content":
            "[Side question — answer briefly from what you already know and the conversation so far. "
            "Do not call any tools; this exchange will not be kept in the conversation.]\n\n" + question}]
        tools = TOOLS + IDE_TOOLS if (self.conv["tools"] and self.ide) else (TOOLS if self.conv["tools"] else None)
        error = None
        try:
            gen = pumped(self.conv["model"], msgs, tools, flag.is_set, "none" if tools else None)
            try:
                for kind, val in gen:
                    if kind == "text":
                        self.proto.event("btw_text", id=bid, delta=val)
                    elif kind == "done":
                        USAGE.add(val[1])
            finally:
                gen.close()
        except Cancelled:
            error = "interrupted"
        except SystemExit as e:
            error = str(e.code)
        except Exception as e:
            error = "interrupted" if flag.is_set() else str(e)
        self.btw_cancel.pop(bid, None)
        self.emit_usage()
        self.proto.event("btw_end", id=bid, error=error)

    def compact_now(self, instructions="", midway=False):
        """Replace the conversation with the model's summary of it (in place). Raises
        Cancelled or RuntimeError, leaving the conversation as it was."""
        def reply(msgs):
            text, calls, _ = self.stream(self.conv["model"], msgs, self.tools(), main=False,
                                         tool_choice="none")
            if calls:
                raise RuntimeError("the model tried to use tools instead of summarizing; try again")
            return text
        try:
            summary = summarize(reply, self.conv["messages"], instructions, midway)
        except Cancelled:
            raise Cancelled() from None         # the partial summary is not a reply
        with self.lock:
            msgs = self.conv["messages"]
            msgs[:] = compacted_messages(msgs[0], summary, midway, self.conv.get("todos"))
            self.conv["display"] = {"1": {"text": summary, "kind": "summary", "time": time.time()}}
            if midway:
                self.conv["display"]["3"] = {"kind": "hidden"}
            self.session["turns"] = []
            self.cur_turn = None
            USAGE.ctx = msgs_tokens(msgs)

    def auto_compact(self, midway):
        """Compact during a turn; the editor shows it like /compact. Raises Cancelled."""
        before = context_estimate(self.conv["messages"])
        self.proto.event("auto_compact", phase="start")
        error = "cancelled"
        try:
            self.compact_now("", midway)
            error = None
        except RuntimeError as e:
            error = str(e)
        finally:
            self.proto.event("auto_compact", phase="end", error=error, before=before,
                             after=USAGE.ctx if not error else None, usage=self.usage())
        return error is None

    def compact(self, instructions):
        error = None
        try:
            self.compact_now(instructions)
            self.save()
        except Cancelled:
            error = "cancelled"
        except (RuntimeError, SystemExit) as e:
            error = str(e)
        with self.lock:
            self.busy = self.compacting = False
        self.proto.event("compact_end", error=error, transcript=self.transcript() if not error else None,
                         usage=self.usage())

    # ------------------------------------------------------------ dispatch
    def handle(self, method, p):
        h = getattr(self, "m_" + method.replace("/", "_"), None)
        if h is None:
            raise ValueError(f"unknown method {method}")
        return h(clean(p or {}))

    def require_idle(self):
        if self.busy:
            raise RuntimeError("tai is busy; stop the current task first")

    def m_initialize(self, p):
        self.caps = {"ide": bool(p.get("ide")), "autosave": bool(p.get("autosave"))}
        if p.get("mode") in MODES:
            self.perms.mode = p["mode"]
        if p.get("model"):
            self.conv["model"] = p["model"]
        self.reset_system()
        model_window(self.conv["model"])
        return {"protocol": PROTOCOL_VERSION, "version": TAI_VERSION, "cwd": str(Path.cwd()),
                "defaultModel": DEFAULT_MODEL, "modes": MODES, "modeInfo": mode_info(),
                "apiKey": bool(os.environ.get("TILBURG_AI_API_KEY")), "state": self.state()}

    def m_send(self, p):
        cmd = str(p.get("command") or "").lower().lstrip("/")
        if cmd:
            arg = str(p.get("arg") or "")
            typed = str(p.get("text") or f"/{cmd} {arg}".strip())
            if cmd == "review":
                body, label = review_request(arg)
                content, display = self.compose({**p, "text": body, "context": None})
                display.update(text=typed, kind="review", readonly=True, files=[])
                self.proto.event("notice", level="info", text=f"Reviewing {label}; tai only reads files for this.")
            else:
                own = custom_commands().get(cmd)
                if not own:
                    raise ValueError(f"unknown command /{cmd}")
                content, display = self.compose({**p, "text": expand_command(own, arg)})
                display["text"] = typed
            queued = self.start_turn(content, display, p.get("clientId"))
            return {"queued": queued}
        content, display = self.compose(p)
        if not text_of(content).strip() and not count_images(content):
            raise ValueError("empty message")
        queued = self.start_turn(content, display, p.get("clientId"))
        return {"queued": queued}

    def m_cancel(self, p):
        with self.lock:
            unsent = [d.get("text", "") for _, d, _ in self.inbox]
            self.inbox = []
        if self.busy:
            self.cancel()
        for f in list(self.btw_cancel.values()):
            f.set()
        return {"unsent": unsent}

    def m_btw(self, p):
        q = str(p.get("question") or "").strip()
        if not q:
            raise ValueError("empty question")
        bid = str(p.get("id") or new_session_id())
        threading.Thread(target=self.btw, args=(bid, q), daemon=True).start()
        return {"id": bid}

    def m_compact(self, p):
        with self.lock:
            self.require_idle()
            if not any(m.get("role") == "user" for m in self.conv["messages"]):
                raise RuntimeError("nothing to compact yet")
            self.busy = self.compacting = True
            CANCEL.clear()
        threading.Thread(target=self.compact, args=(str(p.get("instructions") or ""),), daemon=True).start()
        return {}

    def m_jobs(self, p):
        return {"jobs": [j.info() for j in JOBS.values()]}

    def m_job_stop(self, p):
        job = JOBS.get(str(p.get("id") or ""))
        if not job:
            raise ValueError(f"no background job {p.get('id')}")
        job.stop()
        return job.info()

    def m_permissions(self, p):
        """The settings file with the saved rules (created if missing), for the editor to open."""
        return {"path": str(SETTINGS.ensure()), "rules": SETTINGS.describe(), "sandbox": SANDBOX.describe()}

    def m_remember(self, p):
        """Add a line to the user's (scope "personal") or the project's instructions; without
        text, just return the files (created when "create" is set, for the editor to open)."""
        project = p.get("scope") == "project"
        text = str(p.get("text") or "").strip()
        if text:
            path = remember(text, project)
            with self.lock:
                msgs = self.conv["messages"]
                if msgs and msgs[0].get("role") == "system":
                    msgs[0] = {"role": "system", "content": system_prompt(
                        self.conv["tools"], self.conv.get("extra_system"), ide=self.ide)}
        else:
            path = project_md() if project else PERSONAL_MD
            if p.get("create") and not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                write_text(path, "" if project else "# My instructions for tai (all projects)\n\n")
        return {"path": str(path), "personal": str(PERSONAL_MD), "project": str(project_md())}

    def m_notify(self, p):
        """A desktop notification, for the editor when its window is not in front."""
        return {"shown": desktop_notify(str(p.get("title") or "tai"), str(p.get("text") or ""))}

    def m_models(self, p):
        return {"models": [{"id": m["id"], "maxInput": m.get("max_input_tokens"),
                            "embedding": m.get("mode") == "embedding"} for m in list_models()]}

    def m_set(self, p):
        if "mode" in p:
            if p["mode"] not in MODES:
                raise ValueError(f"unknown mode {p['mode']}")
            self.perms.mode = p["mode"]
        if p.get("model"):
            self.conv["model"] = str(p["model"])
            USAGE.max_ctx = None
            model_window(self.conv["model"])
        if "tools" in p or "system" in p:
            self.require_idle()
            if "tools" in p:
                self.conv["tools"] = bool(p["tools"])
            if "system" in p:
                self.conv["extra_system"] = str(p["system"] or "") or None
            self.reset_system()
        if any(m.get("role") == "user" for m in self.conv["messages"]):
            self.save()
        st = self.state()
        self.proto.event("state", state=st)
        return st

    def m_state(self, p):
        return self.state()

    def m_new_session(self, p):
        self.require_idle()
        c = self.conv
        self.new_session(p.get("model") or c["model"], c["tools"], None)
        self.perms.always.clear()
        return self.state()

    def m_sessions(self, p):
        out, cwd = [], str(Path.cwd())
        try:
            files = sorted(SESSIONS.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        except OSError:
            files = []
        for f in files[:500]:
            try:
                s = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not p.get("all") and s.get("cwd") != cwd:
                continue
            out.append(self.meta(s))
        return {"sessions": out}

    def m_load_session(self, p):
        self.require_idle()
        s = json.loads(session_path(p["id"]).read_text(encoding="utf-8"))
        s["conv"].setdefault("display", {})
        s.setdefault("turns", [])
        self.session = s
        self.reset_system()
        USAGE.ctx = 0
        self.perms.always.clear()
        return {"state": self.state(), "transcript": self.transcript()}

    def m_delete_session(self, p):
        if p["id"] == self.session["id"]:
            self.require_idle()
        path = session_path(p["id"])
        if path.exists():
            path.unlink()
        if p["id"] == self.session["id"]:
            self.new_session(self.conv["model"], self.conv["tools"], None)
        return {}

    def m_rename_session(self, p):
        title = clip(str(p.get("title") or ""), 120)
        if p["id"] == self.session["id"]:
            self.session["title"] = title
            self.save()
            return {}
        path = session_path(p["id"])
        s = json.loads(path.read_text(encoding="utf-8"))
        s["title"] = title
        write_json_atomic(path, s)
        return {}

    def m_transcript(self, p):
        return {"transcript": self.transcript(), "state": self.state()}

    def m_rewind_preview(self, p):
        idx = int(p["index"])
        files = self.rewind_files(idx)
        return {"files": [{"path": k, "rel": show_path(Path(k)), "created": v is None,
                           "restorable": not (isinstance(v, dict) and "skipped" in v)}
                          for k, v in files.items()]}

    def m_rewind(self, p):
        self.require_idle()
        idx = int(p["index"])
        msgs = self.conv["messages"]
        if not (0 < idx < len(msgs)) or msgs[idx].get("role") != "user":
            raise ValueError("not a user message")
        restored, failed = [], []
        if p.get("files", True):
            for path, snap in self.rewind_files(idx).items():
                try:
                    restore(Path(path), snap)
                    restored.append(show_path(Path(path)))
                    self.proto.event("file_changed", path=path, created=False)
                except OSError as e:
                    failed.append({"path": show_path(Path(path)), "error": str(e)})
        d = (self.conv.get("display") or {}).get(str(idx)) or {}
        text = d.get("text", msgs[idx].get("content") or "")
        if p.get("conversation", True):
            with self.lock:
                del msgs[idx:]
                self.conv["display"] = {k: v for k, v in (self.conv.get("display") or {}).items()
                                        if int(k) < idx}
                self.session["turns"] = [t for t in self.session["turns"] if t["msg"] < idx]
                USAGE.ctx = 0
        else:
            for t in self.session["turns"]:
                if t["msg"] >= idx:
                    t["files"] = {}
        self.save()
        return {"text": text, "restored": restored, "failed": failed,
                "transcript": self.transcript(), "state": self.state()}

    def m_export(self, p):
        md = to_markdown(self.conv)
        if p.get("path"):
            Path(p["path"]).expanduser().write_text(md, encoding="utf-8")
        return {"markdown": md}

    def m_usage(self, p):
        return self.usage()

    def m_ping(self, p):
        return {"pong": True}


def serve(args):
    # Protocol messages own the real stdout; anything else printed goes to stderr.
    proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    if RICH:
        console.file = sys.stderr
    proto = Proto(proto_out)
    mode = "plan" if args.plan else "auto" if args.auto else "accept-edits" if args.auto_edit else "manual"
    server = Server(proto, args.model or DEFAULT_MODEL, mode)
    if args.chat:
        server.conv["tools"] = False
        server.reset_system()
    proto.event("ready", version=TAI_VERSION, protocol=PROTOCOL_VERSION,
                hasKey=bool(os.environ.get("TILBURG_AI_API_KEY") or stored_key()))

    def work(id_, method, params):
        try:
            res = server.handle(method, params)
            if id_ is not None:
                proto.reply(id_, res)
        except SystemExit as e:
            if id_ is not None:
                proto.reply(id_, error=str(e.code))
        except Exception as e:
            if id_ is not None:
                proto.reply(id_, error=f"{e}" if isinstance(e, (RuntimeError, ValueError, KeyError))
                            else f"{e.__class__.__name__}: {e}")
    slow = {"models", "export"}
    import io
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            proto.event("notice", level="error", text="tai: received invalid JSON")
            continue
        if not isinstance(msg, dict):
            continue
        method = msg.get("method")
        if not method:
            proto.resolve(msg)              # answer to one of our requests
            continue
        if method == "shutdown":
            proto.reply(msg.get("id"), {})
            break
        if method in slow:
            threading.Thread(target=work, args=(msg.get("id"), method, msg.get("params")),
                             daemon=True).start()
        else:
            work(msg.get("id"), method, msg.get("params"))
    # client gone: stop running work, save, exit
    proto.closed.set()
    if server.busy:
        server.cancel()
        if server.worker:
            server.worker.join(timeout=5)
    stop_all_jobs()
    os._exit(0)


# ================================================================ main

def main():
    ap = argparse.ArgumentParser(
        prog="tai", description="A terminal AI agent on Tilburg.ai.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=HELP)
    ap.add_argument("prompt", nargs="*", help="task or question (omit for an interactive session)")
    ap.add_argument("-m", "--model", help=f"model id (default: {DEFAULT_MODEL})")
    ap.add_argument("-s", "--system", help="extra instructions for the model")
    ap.add_argument("-f", "--file", action="append", default=[], help="attach a file (repeatable)")
    ap.add_argument("-c", "--continue", dest="cont", action="store_true",
                    help="continue the last conversation")
    ap.add_argument("-i", "--interactive", action="store_true",
                    help="stay in the session after the first answer")
    ap.add_argument("--chat", action="store_true", help="plain chat: no tools")
    ap.add_argument("--auto-edit", action="store_true",
                    help="start in accept-edits mode: file edits run without asking")
    ap.add_argument("--auto", action="store_true",
                    help="start in auto mode: edits, commands and fetches run without asking")
    ap.add_argument("--plan", action="store_true",
                    help="start in plan mode: tai reads and plans, and asks you to approve the plan first")
    ap.add_argument("--raw", action="store_true", help="plain text output, no markdown rendering")
    ap.add_argument("--models", action="store_true", help="list available models")
    ap.add_argument("-r", "--resume", metavar="ID", help="resume a saved session (e.g. one from VS Code)")
    ap.add_argument("--set-key", action="store_true", help="store your Tilburg.ai API key for tai")
    ap.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)  # JSON protocol for the VS Code extension
    ap.add_argument("--version", action="version", version=f"tai {TAI_VERSION}")
    args = ap.parse_args()

    if args.serve:
        serve(args)
        return

    if args.set_key:
        set_key()
        return

    if args.models:
        print_models(args.model or DEFAULT_MODEL)
        return

    IO_HOOKS["before_write"] = checkpoint_write      # for /rewind
    IO_HOOKS["job_done"] = lambda j: dim(f"  ⎿  background job {j.id} {j.state()}: {clip(j.cmd, 60)}")
    import atexit
    atexit.register(stop_all_jobs)
    conv = None
    if args.resume:
        try:
            sess = json.loads(session_path(args.resume).read_text(encoding="utf-8"))
            conv = sess["conv"]
            conv.setdefault("turns", sess.get("turns") or [])
            conv.setdefault("tools", True)
        except (OSError, ValueError, KeyError):
            sys.exit(f"tai: no saved session {args.resume}")
    elif args.cont:
        conv = load_conv()
        if not conv:
            dim("no previous conversation, starting a new one")
        else:
            conv.setdefault("tools", True)
    use_tools = not args.chat
    if not conv:
        conv = new_conv(args.model or DEFAULT_MODEL, use_tools, args.system)
    else:
        if args.model:
            conv["model"] = args.model
        if args.chat:
            conv["tools"] = False
        if args.system:
            conv["extra_system"] = args.system
        reset_system(conv)
    perms = Permissions("plan" if args.plan else "auto" if args.auto else "accept-edits" if args.auto_edit else "manual")
    IO_HOOKS["todos"] = lambda items: conv.__setitem__("todos", items)

    try:
        attachments = [read_file_block(f) for f in args.file]
    except RuntimeError as e:
        sys.exit(f"tai: {e}")

    piped = None
    if not sys.stdin.isatty():
        piped = sys.stdin.read()
        if piped.strip():
            attachments.append(f"<stdin>\n{piped}\n</stdin>")
        try:  # reattach the keyboard for approvals and -i
            sys.stdin = open("CONIN$" if WINDOWS else "/dev/tty", encoding="utf-8")
        except OSError:
            pass

    prompt = " ".join(args.prompt).strip()
    markdown = not args.raw
    one_tools = None
    if prompt.startswith("/"):
        cmd, _, arg = prompt.partition(" ")
        own = custom_commands().get(cmd[1:].lower())
        if cmd == "/review":
            try:
                prompt, label = review_request(arg)
            except RuntimeError as e:
                sys.exit(f"tai: {e}")
            dim(f"reviewing {label}")
            one_tools = review_tools(TOOLS) if conv["tools"] else None
        elif own:
            prompt = expand_command(own, arg)

    if prompt or (piped and piped.strip()):
        content = join_content(attachments + ([prompt] if prompt else []))
        if any(m.get("role") == "assistant" for m in conv["messages"]):
            model_window(conv["model"])     # a continued conversation may need compacting
        ok = ask(conv, perms, markdown, content, tools=one_tools)
        if not args.interactive:
            sys.exit(0 if ok else 1)
        attachments = []

    if not sys.stdin.isatty():
        sys.exit("tai: an interactive session needs a terminal")
    repl(conv, perms, markdown, pending=attachments)


def cli():
    """Entry point (also of the installed `tai` command)."""
    for stream in (sys.stdout, sys.stderr):
        try:      # never crash on a character the console can't show (e.g. Windows code pages)
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(130)


if __name__ == "__main__":
    cli()
