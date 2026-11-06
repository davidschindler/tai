import * as vscode from "vscode";
import { promptForApiKey } from "./apiKey";
import { ChatController, config, mentionFor, Shared } from "./chatController";
import { DiffManager } from "./diffs";
import { EditorTracker, FileIndex, relPath, selectionLines } from "./editorContext";

const VIEW_ID = "tai.chatView";
const PANEL_TYPE = "taiChatPanel";
const SIDEBAR_SESSION = "tai.sidebarSession";
const RESTORE_WITHIN_MS = 12 * 3600 * 1000;

let ext: Extension | undefined;

class Extension implements vscode.Disposable {
  readonly out = vscode.window.createOutputChannel("tai");
  readonly diffs = new DiffManager();
  readonly tracker = new EditorTracker();
  readonly files = new FileIndex();
  readonly status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  readonly shared: Shared;
  sidebar?: ChatController;
  sidebarView?: vscode.WebviewView;
  readonly panels = new Map<ChatController, vscode.WebviewPanel>();
  private lastFocused?: ChatController;
  private focusOwner?: ChatController;
  private subs: vscode.Disposable[] = [];
  private sidebarAttached?: () => void;

  constructor(readonly ctx: vscode.ExtensionContext) {
    this.shared = {
      ctx,
      log: (l) => this.out.appendLine(`${new Date().toISOString().slice(11, 23)} ${l}`),
      diffs: this.diffs,
      tracker: this.tracker,
      files: this.files,
      busyChanged: () => this.updateStatus(),
      focused: (c, f) => {
        if (f) {
          this.lastFocused = this.focusOwner = c;
          vscode.commands.executeCommand("setContext", "taiChatFocused", true);
        } else if (this.focusOwner === c) {      // ignore a late blur from a chat that lost focus earlier
          this.focusOwner = undefined;
          vscode.commands.executeCommand("setContext", "taiChatFocused", false);
        }
      },
      revealSession: (c, id) => {
        for (const other of [this.sidebar, ...this.panels.keys()]) {
          if (!other || other === c || other.sessionId !== id) continue;
          if (other === this.sidebar) this.showSidebar().then((s) => s.focusInput());
          else this.panels.get(other)?.reveal(undefined, false);
          return true;
        }
        return false;
      },
    };
    this.status.command = "tai.focus";
    this.updateStatus();
    this.status.show();
  }

  // ---------------------------------------------------------------- side bar
  resolveSidebar(view: vscode.WebviewView): void {
    this.sidebarView = view;
    if (!this.sidebar) {
      const saved = this.ctx.workspaceState.get<{ id: string; at: number }>(SIDEBAR_SESSION);
      const restore = saved && Date.now() - saved.at < RESTORE_WITHIN_MS ? saved.id : undefined;
      this.sidebar = new ChatController(this.shared, restore);
      this.sidebar.onSession = (s) => {
        if (s.messages) this.ctx.workspaceState.update(SIDEBAR_SESSION, { id: s.id, at: Date.now() });
        else this.ctx.workspaceState.update(SIDEBAR_SESSION, undefined);
      };
    }
    const c = this.sidebar;
    c.onAttention = (why) => {
      if (why === "clear" || view.visible) return;
      view.badge = { value: 1, tooltip: why === "approval" ? "tai is waiting for your approval" : "tai has finished" };
    };
    view.onDidChangeVisibility(() => {
      if (view.visible) view.badge = undefined;
    });
    view.onDidDispose(() => {
      if (this.sidebarView === view) this.sidebarView = undefined;
    });
    c.attach(view.webview);
    this.sidebarAttached?.();
  }

  private async showSidebar(): Promise<ChatController> {
    if (this.sidebar && this.sidebarView) {
      this.sidebarView.show(false);
      return this.sidebar;
    }
    const attached = new Promise<void>((r) => (this.sidebarAttached = r));
    await vscode.commands.executeCommand(`${VIEW_ID}.focus`);
    await Promise.race([attached, new Promise((r) => setTimeout(r, 3000))]);
    this.sidebarAttached = undefined;
    return this.sidebar!;
  }

  // ---------------------------------------------------------------- editor tabs
  openPanel(sessionId?: string, existing?: vscode.WebviewPanel): ChatController {
    const panel = existing ?? vscode.window.createWebviewPanel(PANEL_TYPE, "tai", vscode.ViewColumn.Beside, {
      enableScripts: true, retainContextWhenHidden: true });
    panel.iconPath = vscode.Uri.joinPath(this.ctx.extensionUri, "media", "tai-tab.svg");
    const c = new ChatController(this.shared, sessionId);
    this.panels.set(c, panel);
    c.onSession = (s) => {
      panel.title = s.title ? `tai · ${s.title.length > 30 ? s.title.slice(0, 29) + "…" : s.title}` : "tai";
    };
    c.onAttention = (why) => {
      if (why === "clear" || panel.active) return;
      panel.title = panel.title.startsWith("● ") ? panel.title : "● " + panel.title;
    };
    panel.onDidChangeViewState(() => {
      if (panel.active && panel.title.startsWith("● ")) panel.title = panel.title.slice(2);
      if (panel.active) this.lastFocused = c;
    });
    panel.onDidDispose(() => {
      this.panels.delete(c);
      if (this.lastFocused === c) this.lastFocused = undefined;
      if (this.focusOwner === c) {
        this.focusOwner = undefined;
        vscode.commands.executeCommand("setContext", "taiChatFocused", false);
      }
      c.dispose();
    });
    c.attach(panel.webview);
    this.lastFocused = c;
    return c;
  }

  /** The chat that commands act on: the last focused one, else the side bar. */
  async active(): Promise<ChatController> {
    const lf = this.lastFocused;
    if (lf && this.panels.has(lf)) {
      this.panels.get(lf)!.reveal(undefined, false);
      return lf;
    }
    return this.showSidebar();
  }

  updateStatus(): void {
    const all = [this.sidebar, ...this.panels.keys()].filter((c): c is ChatController => !!c);
    const waiting = all.reduce((n, c) => n + c.pendingApprovals, 0);
    const busy = all.filter((c) => c.busy).length;
    if (waiting) {
      this.status.text = "$(bell-dot) tai";
      this.status.tooltip = "tai is waiting for your approval";
      this.status.backgroundColor = new vscode.ThemeColor("statusBarItem.warningBackground");
    } else if (busy) {
      this.status.text = "$(loading~spin) tai";
      this.status.tooltip = "tai is working";
      this.status.backgroundColor = undefined;
    } else {
      this.status.text = "$(comment-discussion) tai";
      this.status.tooltip = "Open tai (Ctrl+Esc)";
      this.status.backgroundColor = undefined;
    }
  }

  registerCommands(): void {
    const reg = (id: string, fn: (...a: any[]) => unknown) =>
      this.subs.push(vscode.commands.registerCommand(id, async (...a: any[]) => {
        try {
          await fn(...a);
        } catch (e: any) {
          this.shared.log(`${id} failed: ${e?.stack ?? e}`);
          vscode.window.showErrorMessage(`tai: ${e?.message ?? e}`);
        }
      }));
    reg("tai.focus", async () => (await this.active()).focusInput());
    reg("tai.blur", () => vscode.commands.executeCommand("workbench.action.focusActiveEditorGroup"));
    reg("tai.openSidebar", async () => (await this.showSidebar()).focusInput());
    reg("tai.openInNewTab", () => this.openPanel());
    reg("tai.newConversation", async () => {
      const c = await this.active();
      await c.newSession();
      c.focusInput();
    });
    reg("tai.history", async () => (await this.active()).history());
    reg("tai.selectModel", async () => (await this.active()).pickModel());
    reg("tai.cycleMode", async () => (await this.active()).cycleMode());
    reg("tai.stop", async () => (await this.active()).cancel());
    reg("tai.restart", async () => (await this.active()).restart());
    reg("tai.exportConversation", async () => (await this.active()).exportConversation());
    reg("tai.openInTerminal", async () => (await this.active()).openTerminal());
    reg("tai.showLogs", () => this.out.show());
    reg("tai.setApiKey", async () => {
      if (await promptForApiKey(this.ctx.secrets)) {
        for (const c of [this.sidebar, ...this.panels.keys()]) if (c) await c.restart();
      }
    });
    reg("tai.insertAtMention", async () => {
      const ed = vscode.window.activeTextEditor ?? this.tracker.activeEditor;
      if (!ed || ed.document.uri.scheme !== "file") {
        vscode.window.showInformationMessage("Open a file to mention it in tai.");
        return;
      }
      const c = await this.active();
      const range = ed.selection.isEmpty ? undefined : selectionLines(ed.selection);
      c.insertMention(mentionFor(relPath(c.cwd, ed.document.uri.fsPath), range));
    });
    reg("tai.addFileToChat", async (uri?: vscode.Uri, uris?: vscode.Uri[]) => {
      const list = (uris?.length ? uris : uri ? [uri] : []).filter((u) => u.scheme === "file");
      if (!list.length) return;
      const c = await this.active();
      c.insertMention(list.map((u) => mentionFor(relPath(c.cwd, u.fsPath))).join(" "));
    });
    reg("tai.acceptDiff", () => {
      if (!this.diffs.decideActive("yes")) vscode.window.showInformationMessage("This change is no longer pending.");
    });
    reg("tai.rejectDiff", () => {
      if (!this.diffs.decideActive("no")) vscode.window.showInformationMessage("This change is no longer pending.");
    });
  }

  async dispose(): Promise<void> {
    this.subs.forEach((s) => s.dispose());
    const all = [this.sidebar, ...this.panels.keys()].filter((c): c is ChatController => !!c);
    await Promise.all(all.map((c) => c.dispose()));
    this.tracker.dispose();
    this.files.dispose();
    this.diffs.dispose();
    this.status.dispose();
    this.out.dispose();
  }
}

export function activate(ctx: vscode.ExtensionContext) {
  ext = new Extension(ctx);
  const e = ext;
  e.registerCommands();
  ctx.subscriptions.push(
    vscode.window.registerWebviewViewProvider(VIEW_ID, { resolveWebviewView: (v) => e.resolveSidebar(v) },
      { webviewOptions: { retainContextWhenHidden: true } }),
    vscode.window.registerWebviewPanelSerializer(PANEL_TYPE, {
      async deserializeWebviewPanel(panel: vscode.WebviewPanel, state: any) {
        panel.webview.options = { enableScripts: true };
        e.openPanel(typeof state?.sessionId === "string" ? state.sessionId : undefined, panel);
      },
    }),
  );
  e.shared.log(`tai extension activated (${config().get("executablePath") || "tai from PATH"})`);
  // for the integration tests
  return { ext: e };
}

export async function deactivate() {
  await ext?.dispose();
  ext = undefined;
}
