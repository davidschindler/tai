# tai for VS Code

A chat panel for **tai**, the Tilburg.ai agent, in the spirit of the Claude Code and Codex extensions. Works on Windows, macOS and Linux. The extension starts `tai --serve` and talks to it over a JSON-lines protocol, so the agent, its tools and its permission rules are the same as in the terminal.

## Features

- **Chat in the side bar or in editor tabs.** Each tab is its own conversation with its own tai process (`Ctrl+Shift+Esc`, or the ↗ button). Replies stream as markdown, with highlighted code blocks (copy / insert at cursor) and clickable `file.py:12` references.
- **Tool calls as they happen.** Read, List, Grep, Edit, Write, Bash (PowerShell on Windows), WebSearch, Fetch and Diagnostics appear as compact rows you can expand. Edits show a line-numbered diff, and sub-agents (`task`) appear as boxes with their own tool calls, running in parallel.
- **Approvals.** File edits, commands and web fetches ask first: *1 Yes · 2 Yes, don't ask again · 3 No, and tell tai what to do instead*. Edits also open in VS Code's **native diff editor**, with ✓/✗ buttons in the editor title (`Ctrl+Enter` / `Ctrl+Backspace`).
- **Permission modes.** *manual* → *accept edits* → *auto* (`Shift+Tab` in the input, or click the mode pill). Secret files (`~/.ssh`, `.env`, …) always ask.
- **Editor context.** tai sees the file you have open and your selection (the chip above the input; click it to leave it out of the next message). `Alt+K` inserts an `@file#L5-9` mention of the selection, and *Add to tai Chat* in the explorer menu mentions files and folders.
- **@-mentions.** Type `@` to fuzzy-search workspace files. Mentioned files, folders and line ranges are attached to the message.
- **Slash commands**: `/btw` (side question, not kept), `/compact`, `/model`, `/mode`, `/history`, `/rewind`, `/tools`, `/system`, `/usage`, `/save`, `/terminal`, `/restart`, `/clear`, `/help`.
- **Queue while tai works.** Messages sent during a task are injected at tai's next step.
- **Stop** with `Esc` or the ■ button. Streaming, commands, sub-agents and pending approvals stop immediately.
- **Checkpoints and rewind.** Hover a message of yours → ↶ to restore code and/or the conversation to that point; the message goes back into the input to edit. tai snapshots each file before it edits it. Changes made by shell commands are not tracked.
- **History.** Conversations are saved per folder (🕘 button / `/history`). They survive window reloads, and `tai -r <id>` (or `/terminal`) continues one in the terminal.
- **Diagnostics tool.** tai can read VS Code's Problems (`get_diagnostics`) to check its own edits.
- **Autosave.** Unsaved editor changes are saved before tai reads or edits that file.
- Token usage and context fill in the footer, a status bar item (spinner while working, bell when an approval waits), and a badge on the side bar when tai finishes while hidden.

## Setup

1. **Python 3.9 or newer** must be installed ([python.org](https://www.python.org/downloads/); on Windows tick *Add python.exe to PATH*). tai itself is built into the extension and needs no extra packages.
2. **API key.** The first time, tai shows a *Set API key* button; the key goes into your system's keychain (VS Code secret storage) and never into settings or files. It is also taken from `TILBURG_AI_API_KEY`, from a key stored with `tai --set-key`, or from `~/.secrets`.

To use your own copy of tai instead of the built-in one, set `tai.executablePath`.

## Settings

| Setting | Default | |
|---|---|---|
| `tai.model` | (tai's default) | model for new conversations; the last model you picked is remembered |
| `tai.initialPermissionMode` | `manual` | `manual`, `accept-edits` or `auto` |
| `tai.autoOpenDiff` | `true` | open the native diff when tai wants to edit a file |
| `tai.autosave` | `true` | save unsaved changes before tai reads/edits the file |
| `tai.attachOpenFile` | `true` | send the open file and selection as context |
| `tai.useCtrlEnterToSend` | `false` | Ctrl+Enter sends, Enter inserts a new line |
| `tai.showMessageTimestamps` | `false` | |
| `tai.environmentVariables` | `{}` | extra environment for tai, e.g. `TAI_SEARCH_MODEL` |
| `tai.executablePath`, `tai.pythonPath` | | where tai / which Python |

## Keys

| | |
|---|---|
| `Ctrl+Esc` | focus tai / back to the editor |
| `Ctrl+Shift+Esc` | new tai tab |
| `Alt+K` | mention the selection |
| `Ctrl+N` (in tai) | new conversation |
| `Enter` / `Shift+Enter` | send / new line |
| `Esc` | stop |
| `Shift+Tab` | cycle permission mode |
| `↑` / `↓` | earlier messages |
| `1` `2` `3` | answer an approval |

## Development

```
npm install
npm run build          # dist/extension.js, dist/webview.js
npm test               # typecheck, build, then all three test layers
npm run package        # tai-vscode-<version>.vsix
```

`npm run build` also copies `../tai.py` into `tai/`, the copy shipped in the `.vsix`.

Tests (no real API calls; `../tests/mock_api.py` stands in for Tilburg.ai and is scripted through `MOCK:` messages):

- `../tests`: tai itself: the `tai --serve` protocol (streaming, tools, approvals, modes, cancel, sub-agents, queueing, errors, /btw, /compact, sessions, rewind, attachments, diagnostics, autosave), the terminal program, and per-OS behaviour (encodings, line endings, shell, key storage).
- `test/webview`: the real UI bundle in headless Chrome, wired to a real `tai --serve` (screenshots in `test/webview/screenshots`).
- `test/vscode`: integration tests in a downloaded, isolated VS Code (native diffs and their buttons, diagnostics, autosave, editor context, quick picks, tabs, crash recovery). Run them headless on a private X server: `TAI_TEST_DISPLAY=:99 npm run test:vscode`.
