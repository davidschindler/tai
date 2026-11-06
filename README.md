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

- **Approvals:** file edits, commands and web page fetches ask first: *1* yes, *2* yes and don't ask again, *3* no (and tell tai what to do instead). `Shift+Tab` switches between *manual*, *accept edits* and *auto*.
- `@file` attaches a file, `/` lists commands (`/btw` for a side question, `/model`, `/compact`, `/rewind`, …), `Esc` stops tai (`Ctrl+C` in the terminal).
- **While tai works** you can keep typing: `/btw QUESTION` is answered right away without disturbing the task, and a normal message is read at tai's next step.
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
