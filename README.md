# tai

An AI agent on the [Tilburg.ai](https://tilburg.ai) API, for the terminal and for VS Code. It reads and edits files, runs commands, searches the web and starts sub-agents, and asks before it changes anything. Works on Windows, macOS and Linux.

You need a **Tilburg.ai API key** and **Python 3.9 or newer**.

## 1. Install Python (if you don't have it)

- **Windows:** install from [python.org](https://www.python.org/downloads/). In the installer, tick **Add python.exe to PATH**.
- **macOS:** install from [python.org](https://www.python.org/downloads/) (or `brew install python`).
- **Linux:** already installed.

## 2. VS Code extension

1. Download `tai-vscode-<version>.vsix` from the [latest release](../../releases/latest).
2. In VS Code: **Extensions** view → **⋯** menu (top right) → **Install from VSIX…** → pick the file.
3. Click the Tilburg University emblem at the top right of any open file, or press `Ctrl+Esc` (`Cmd+Esc` on a Mac).
4. The first time, click **Set API key** and paste your key. It is stored in your system's keychain.

That's all: tai is built into the extension.

## 3. Terminal program (optional)

Download `tai_agent-<version>-py3-none-any.whl` from the [latest release](../../releases/latest), then in a terminal (PowerShell on Windows):

```
python -m pip install --user pipx
python -m pipx ensurepath
python -m pipx install path/to/tai_agent-<version>-py3-none-any.whl
```

(On Windows, write `py` instead of `python` if `python` is not found. On macOS, `python3`.)

Open a **new** terminal, store your key once, and start:

```
tai --set-key
tai
```

`tai --help` shows all options. To update, install the newer `.whl` with `pipx install --force …`, and the newer `.vsix` the same way as above.

## Using it

- **Approvals:** file edits, commands and web page fetches ask first: *1* yes, *2* yes and don't ask again, *3* no (and tell tai what to do instead). `Shift+Tab` switches between *manual*, *accept edits*, *auto* and *plan*.
- **Plan mode:** for larger changes, switch to *plan* (or start with `tai --plan`). tai reads, searches and thinks first, and changes nothing; then it shows you a plan. *1* starts the work (edits and commands still ask), *2* starts it with file edits running without asking, *3* lets you say what to change in the plan.
- **Sandbox (auto mode):** on macOS and Linux, commands that tai runs on its own in auto mode can only write in the project folder and the temp folder, and can't connect to the internet. When a command needs more (installing a package, downloading data), tai asks first. File edits outside the project folder also ask. Windows has no sandbox: there, auto mode runs commands without limits. Turn it off or add folders in `settings.json`: `"sandbox": {"enabled": true, "network": false, "writable": ["~/data"]}`.
- **Saved permissions:** *2* on a command such as `git status` or `pytest` remembers it for that folder, and on a web page for that site, also in later sessions. `/permissions` shows the rules and the file they are kept in (`settings.json` next to the saved conversations). You can edit it, including `deny` rules that block a command or site even in auto mode:
  ```json
  {
    "allow":   {"commands": ["git status", "git diff"], "sites": ["docs.python.org"]},
    "deny":    {"commands": ["git push", "rm -rf"], "sites": []},
    "folders": {"/path/to/project": {"allow": {"commands": ["make test"]}}}
  }
  ```
  A command rule covers commands that start with those words; an allow rule never covers commands chained with `;`, `&&`, `|`, `>` and the like.
- `@file` attaches a file, `/` lists commands (`/btw` for a side question, `/model`, `/compact`, `/rewind`, `/review`, `/permissions`, …), `Esc` stops tai (`Ctrl+C` in the terminal).
- **Documents and data:** tai reads Word (`.docx`), Excel (`.xlsx`), PowerPoint (`.pptx`) and PDF files, Stata data sets (`.dta`: variables, labels and the first rows) and Jupyter notebooks (cells with their output and plots). It changes notebooks cell by cell, so you see exactly which cell changed. Attach them with `@` like any other file.
- **Images:** paste a screenshot into the chat (`Ctrl+V`), or attach an image with `@plot.png` (`/file plot.png` or `-f plot.png` in the terminal). tai can also look at images itself, for example a plot it has just made.
- **`/review`** reviews your changes in git and lists the problems it finds, without changing anything: `/review` for what isn't committed yet, `/review main` for everything since `main`, and anything after that tells it what to focus on.
- **`/remember`** keeps an instruction for later conversations: `/remember always use robust standard errors`. In VS Code you choose *for all my projects* (your own `TAI.md` next to the saved conversations) or *for this project* (`TAI.md` in the project folder, which colleagues get through git); in the terminal `/remember project …` is for the project. `/remember` alone opens (VS Code) or shows (terminal) the instructions. You can also just tell tai to remember something.
- **Your own commands:** a Markdown file in `.tai/commands/` (for this project, shareable with colleagues through git) or in `commands/` next to the saved conversations (for all your projects) becomes a command: `.tai/commands/referee.md` → `/referee`. `$ARGUMENTS` in the file is replaced with what you type after the command; a `description:` line in a `---` header shows in the `/` menu.
- **While tai works** you can keep typing: `/btw QUESTION` is answered right away without disturbing the task, and a normal message is read at tai's next step.
- **Notifications:** when tai finishes a task that took a while, or waits for your approval, VS Code tells you if the chat is hidden, with a desktop notification when VS Code is in the background (setting *tai: Notifications*). The terminal rings its bell; `"notify": "desktop"` in `settings.json` adds a desktop notification, `"off"` turns it off.
- **Long conversations:** when the context window is 80% full, tai summarizes the conversation so far and carries on, also in the middle of a task (`/compact` does it on request; set `TAI_AUTO_COMPACT=0` to turn the automatic one off).
- **Task list:** for work with several steps tai keeps a checklist, shown above the input in VS Code and as `☒ ◼ ☐` in the terminal.
- **Background commands:** servers, watchers and long runs can keep going while tai works on; tai reads their output when it needs it and is told when they finish. `/jobs` lists them, `/jobs stop ID` stops one, and all of them stop when tai does.
- **Going back:** `/rewind` returns to one of your earlier messages and undoes the file changes tai made since then; the message comes back in the input to edit and resend.
- Commands run in **bash** on macOS/Linux and in **PowerShell** on Windows.
- Conversations are saved locally (`%LOCALAPPDATA%\tai` on Windows, `~/.local/state/tai` elsewhere). `tai -c` continues the last one.

**Privacy:** whatever tai reads (files, command output, web pages) is sent to the Tilburg.ai API as part of the conversation. Reading files that usually hold secrets (`.env`, `~/.ssh`, …) always asks first, and secret environment variables are hidden from the commands tai runs.

## Development

`tai.py` is the whole program (the `tai` command, and `tai --serve` for the extension); `vscode/` is the extension; `tests/` tests tai.

```
pip install pytest pexpect rich prompt_toolkit
python -m pytest tests            # tai: protocol, terminal program and session, per-OS behaviour
cd vscode && npm install && npm test
```

GitHub Actions runs everything on Windows, macOS and Linux for every push. Pushing a tag such as `v1.3.1` builds the `.vsix` and `.whl` and attaches them to a release.

## License

[MIT](LICENSE) © 2026 David Schindler
