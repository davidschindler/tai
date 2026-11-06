import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import { KEY_VAR, promptForApiKey, resolveApiKey } from "./apiKey";
import { DiffManager } from "./diffs";
import { EditorTracker, FileIndex, mentionFor, parseMentions, relPath } from "./editorContext";
import type {
  ApprovalParams, Decision, HostMessage, Mode, ModelInfo, SessionMeta, Settings, TaiEvent, TaiState,
  TranscriptItem, WebviewMessage,
} from "./protocol";
import { findExecutable, TaiClient } from "./taiClient";
import { webviewHtml } from "./webviewHtml";

export interface Shared {
  ctx: vscode.ExtensionContext;
  log: (line: string) => void;
  diffs: DiffManager;
  tracker: EditorTracker;
  files: FileIndex;
  busyChanged: () => void;
  focused: (c: ChatController, focused: boolean) => void;
  /** Another chat already shows this session: focus it and return true. */
  revealSession: (c: ChatController, sessionId: string) => boolean;
}

let nextControllerId = 1;

interface PendingApproval {
  params: ApprovalParams;
  resolve: (r: { decision: Decision; feedback?: string } | null) => void;
}

const MODES: Mode[] = ["manual", "accept-edits", "auto"];

export function config() {
  return vscode.workspace.getConfiguration("tai");
}

export function settings(): Settings {
  const c = config();
  return {
    ctrlEnterToSend: c.get("useCtrlEnterToSend", false),
    attachOpenFile: c.get("attachOpenFile", true),
    showTimestamps: c.get("showMessageTimestamps", false),
  };
}

/** Folder tai works in: the workspace folder of the active file, else the first one, else home. */
export function pickCwd(tracker: EditorTracker): string {
  const folders = vscode.workspace.workspaceFolders ?? [];
  const active = tracker.activeEditor?.document.uri;
  if (active) {
    const f = vscode.workspace.getWorkspaceFolder(active);
    if (f && f.uri.scheme === "file") return f.uri.fsPath;
  }
  const first = folders.find((f) => f.uri.scheme === "file");
  return first ? first.uri.fsPath : os.homedir();
}

function ago(t: number): string {
  const s = Date.now() / 1000 - t;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 7) return `${Math.floor(s / 86400)} d ago`;
  return new Date(t * 1000).toLocaleDateString();
}

function human(n: number): string {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

/**
 * One chat: a webview (side bar or editor tab) plus its own `tai --serve` process.
 */
export class ChatController implements vscode.Disposable {
  private webview?: vscode.Webview;
  private webviewSubs: vscode.Disposable[] = [];
  private client?: TaiClient;
  private starting?: Promise<boolean>;
  private approvals = new Map<string, PendingApproval>();
  private recentChanges = new Map<string, number>();
  private modelsCache?: { at: number; models: ModelInfo[] };
  private modeInfo: Record<string, string> = {};
  private version = "";
  private disposed = false;
  /** Prefix for approval ids: every tai process numbers its requests s1, s2, … */
  private readonly uid = `c${nextControllerId++}`;
  private subs: vscode.Disposable[] = [];
  state?: TaiState;
  readonly cwd: string;
  /** Called when the session changes (title for tabs, persistence). */
  onSession?: (s: SessionMeta) => void;
  /** Called when a turn ends or an approval waits while the chat is not visible. */
  onAttention?: (why: "done" | "approval" | "clear") => void;

  constructor(private shared: Shared, private sessionToLoad?: string, cwd?: string) {
    this.cwd = cwd ?? pickCwd(shared.tracker);
    this.subs.push(shared.tracker.onChange(() => this.postContext()));
    this.subs.push(vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("tai")) this.post({ type: "settings", settings: settings() });
    }));
  }

  get busy(): boolean {
    return !!this.state?.busy;
  }

  get sessionId(): string | undefined {
    return this.state?.session.id ?? this.sessionToLoad;
  }

  get pendingApprovals(): number {
    return this.approvals.size;
  }

  // ------------------------------------------------------------------ webview
  attach(webview: vscode.Webview): void {
    this.webviewSubs.forEach((s) => s.dispose());
    this.webview = webview;
    const ext = this.shared.ctx.extensionUri;
    webview.options = { enableScripts: true, localResourceRoots: [vscode.Uri.joinPath(ext, "dist"), vscode.Uri.joinPath(ext, "media")] };
    webview.html = webviewHtml(webview, ext);
    this.webviewSubs = [webview.onDidReceiveMessage((m: WebviewMessage) => {
      this.onMessage(m).catch((e) => {
        this.shared.log(`webview message ${m.type} failed: ${e?.stack ?? e}`);
        this.notice("error", e?.message ?? String(e));
      });
    })];
  }

  post(msg: HostMessage): void {
    this.webview?.postMessage(msg).then(undefined, () => { /* webview gone */ });
  }

  private notice(level: "info" | "warning" | "error", text: string) {
    this.post({ type: "notice", level, text });
  }

  private postContext() {
    this.post({ type: "context", context: settings().attachOpenFile ? this.shared.tracker.context(this.cwd) : null });
  }

  focusInput(): void {
    this.post({ type: "focus" });
  }

  insertMention(text: string): void {
    this.post({ type: "insertMention", text });
  }

  // ------------------------------------------------------------------ process
  private async env(): Promise<{ env: NodeJS.ProcessEnv; hasKey: boolean }> {
    const extra = config().get<Record<string, string>>("environmentVariables", {}) ?? {};
    const key = await resolveApiKey(this.shared.ctx.secrets, extra);
    const env: NodeJS.ProcessEnv = { ...process.env, ...extra };
    if (key) env[KEY_VAR] = key;
    return { env, hasKey: !!key };
  }

  /** Starts tai if needed. Resolves false (after telling the webview why) if it cannot. */
  ensureStarted(): Promise<boolean> {
    if (this.client?.running) return Promise.resolve(true);
    if (this.starting) return this.starting;
    this.starting = this.start().finally(() => (this.starting = undefined));
    return this.starting;
  }

  private async start(): Promise<boolean> {
    this.post({ type: "status", status: "starting" });
    const exe = findExecutable(config().get("executablePath"));
    if (!exe) {
      this.post({ type: "status", status: "error", canRestart: true,
        message: "Could not find the tai program. Install it to ~/.local/bin/tai or set “tai.executablePath”." });
      return false;
    }
    const { env, hasKey } = await this.env();
    const python = (config().get<string>("pythonPath") ?? "").trim() || undefined;
    const client = new TaiClient({ executable: exe, python, cwd: this.cwd, env, log: this.shared.log });
    this.client = client;
    client.onEvent((e) => this.onEvent(client, e));
    client.onRequest((m, p, id) => this.onServerRequest(m, p, id));
    client.onExit((code) => {
      if (this.client !== client) return;
      this.shared.log(`tai exited with code ${code}`);
      // resume the same conversation when tai is started again
      if (this.state?.session.messages) this.sessionToLoad = this.state.session.id;
      this.dropApprovals();
      if (this.state) this.state.busy = false;
      this.shared.busyChanged();
      if (!this.disposed) {
        this.post({ type: "status", status: "stopped", canRestart: true,
          message: `tai stopped${code ? ` (exit code ${code})` : ""}. It restarts with your next message.` });
      }
    });
    try {
      this.shared.log(`starting ${python ? python + " " : ""}${exe} --serve in ${this.cwd}`);
      const ready = await client.start();
      this.version = ready.version;
      const mode = this.state?.mode ?? config().get<Mode>("initialPermissionMode", "manual");
      const model = this.state?.model || config().get<string>("model") || this.shared.ctx.globalState.get<string>("tai.lastModel") || undefined;
      const init = await client.call("initialize", {
        ide: true, autosave: config().get("autosave", true), mode, model }, 15000);
      this.modeInfo = init.modeInfo ?? {};
      this.state = init.state;
      let items: TranscriptItem[] = [];
      const want = this.sessionToLoad;
      if (want) {
        try {
          const r = await client.call("load_session", { id: want }, 15000);
          this.state = r.state;
          items = r.transcript;
        } catch (e: any) {
          this.shared.log(`could not load session ${want}: ${e.message}`);
        }
        this.sessionToLoad = undefined;
      }
      this.post({ type: "status", status: "ready", needsKey: !hasKey });
      this.sendInit(items);
      return true;
    } catch (e: any) {
      this.shared.log(`start failed: ${e.message}`);
      this.post({ type: "status", status: "error", canRestart: true, message: e.message });
      client.kill();
      return false;
    }
  }

  private sendInit(items?: TranscriptItem[]) {
    if (!this.state) return;
    this.post({ type: "init", state: this.state, settings: settings(), version: this.version, cwd: this.cwd,
      modeInfo: this.modeInfo });
    if (items) this.post({ type: "transcript", items, state: this.state });
    this.postContext();
    for (const [requestId, a] of this.approvals) this.post({ type: "approval", requestId, params: a.params });
    if (this.state.session) this.onSession?.(this.state.session);
  }

  /** The webview (re)loaded: show the current conversation. */
  private async onReady() {
    if (this.client?.running && this.state) {
      try {
        const r = await this.client.call("transcript", {}, 10000);
        this.state = r.state;
        this.post({ type: "status", status: "ready" });
        this.sendInit(r.transcript);
        return;
      } catch { /* fall through to a fresh start */ }
    }
    await this.ensureStarted();
  }

  async restart(): Promise<void> {
    const keep = this.state?.session.messages ? this.state.session.id : this.sessionToLoad;
    const old = this.client;
    this.client = undefined;
    this.dropApprovals();
    if (old) await old.dispose();
    this.sessionToLoad = keep;
    if (this.state) this.state.busy = false;
    this.shared.busyChanged();
    await this.ensureStarted();
  }

  private dropApprovals() {
    for (const [id, a] of this.approvals) {
      a.resolve(null);
      this.shared.diffs.close(this.diffKey(id));
      this.post({ type: "approvalDone", requestId: id });
    }
    this.approvals.clear();
  }

  private setBusy(b: boolean) {
    if (!this.state) return;
    this.state.busy = b;
    this.shared.busyChanged();
  }

  // ------------------------------------------------------------------ events from tai
  private onEvent(client: TaiClient, e: TaiEvent) {
    if (client !== this.client) return;
    switch (e.type) {
      case "turn_start":
        this.setBusy(true);
        break;
      case "turn_end":
        this.setBusy(false);
        if (this.state) this.state.usage = e.usage;
        this.onAttention?.("done");
        break;
      case "compact_end":
        this.setBusy(false);
        break;
      case "state":
        this.state = e.state;
        this.shared.busyChanged();
        break;
      case "session":
        if (this.state) this.state.session = e.session;
        this.onSession?.(e.session);
        break;
      case "usage":
        if (this.state) this.state.usage = e.usage;
        break;
      case "request_cancelled": {
        const a = this.approvals.get(e.requestId);
        if (a) {
          this.approvals.delete(e.requestId);
          a.resolve(null);
          this.shared.diffs.close(this.diffKey(e.requestId));
          this.post({ type: "approvalDone", requestId: e.requestId });
          if (!this.approvals.size) this.shared.busyChanged();
        }
        return;
      }
      case "file_changed":
        this.recentChanges.set(e.path, Date.now());
        break;
    }
    this.post({ type: "event", event: e });
  }

  private async onServerRequest(method: string, params: any, id: string): Promise<unknown> {
    switch (method) {
      case "approval":
        return this.askApproval(id, params as ApprovalParams);
      case "ide/diagnostics":
        return this.diagnostics(params?.path ?? null);
      case "ide/saveFile": {
        const real = (p: string) => { try { return fs.realpathSync(p); } catch { return p; } };
        const want = real(String(params?.path ?? ""));
        const doc = vscode.workspace.textDocuments.find((d) => d.uri.scheme === "file" &&
          (d.uri.fsPath === params?.path || real(d.uri.fsPath) === want));
        if (doc?.isDirty) await doc.save();
        return {};
      }
      default:
        throw new Error(`unknown request ${method}`);
    }
  }

  private askApproval(id: string, params: ApprovalParams): Promise<unknown> {
    return new Promise((resolve) => {
      this.approvals.set(id, { params, resolve });
      this.post({ type: "approval", requestId: id, params });
      this.shared.busyChanged();
      this.onAttention?.("approval");
      const d = params.detail;
      if (d.type === "edit" && d.path && typeof d.new === "string" && config().get("autoOpenDiff", true)) {
        this.shared.diffs.openProposal(this.diffKey(id), d.path, d.old ?? null, d.new, (dec) => this.decide(id, dec))
          .catch((err) => this.shared.log(`diff failed: ${err}`));
      }
    });
  }

  decide(id: string, decision: Decision, feedback?: string): void {
    const a = this.approvals.get(id);
    if (!a) return;
    this.approvals.delete(id);
    a.resolve({ decision, feedback: feedback ?? "" });
    this.shared.diffs.close(this.diffKey(id));
    this.post({ type: "approvalDone", requestId: id });
    this.shared.busyChanged();
  }

  private diffKey(requestId: string): string {
    return `${this.uid}-${requestId}`;
  }

  ownsApproval(id: string): boolean {
    return this.approvals.has(id);
  }

  private async diagnostics(file: string | null) {
    const sev = ["error", "warning", "info", "hint"];
    const collect = () => {
      const all: [vscode.Uri, readonly vscode.Diagnostic[]][] = file
        ? [[vscode.Uri.file(file), vscode.languages.getDiagnostics(vscode.Uri.file(file))]]
        : vscode.languages.getDiagnostics();
      const out: object[] = [];
      for (const [uri, ds] of all) {
        if (uri.scheme !== "file") continue;
        for (const d of ds) {
          if (d.severity === vscode.DiagnosticSeverity.Hint) continue;
          out.push({ path: uri.fsPath, line: d.range.start.line + 1, col: d.range.start.character + 1,
            severity: sev[d.severity], message: d.message,
            source: d.source ?? (typeof d.code === "object" ? String(d.code.value) : d.code ?? "") });
        }
      }
      return out;
    };
    // Language servers need a moment after an edit; open the file and wait for fresh results.
    const targets = file ? [file] : [...this.recentChanges.keys()];
    const fresh = targets.filter((t) => Date.now() - (this.recentChanges.get(t) ?? 0) < 10000);
    if (fresh.length) {
      for (const t of fresh) {
        try { await vscode.workspace.openTextDocument(vscode.Uri.file(t)); } catch { /* deleted */ }
      }
      await new Promise<void>((resolve) => {
        const timer = setTimeout(() => { sub.dispose(); resolve(); }, 2500);
        const sub = vscode.languages.onDidChangeDiagnostics((ev) => {
          if (ev.uris.some((u) => fresh.includes(u.fsPath))) {
            clearTimeout(timer);
            sub.dispose();
            setTimeout(resolve, 300);
          }
        });
      });
    }
    return collect();
  }

  // ------------------------------------------------------------------ messages from the webview
  private async onMessage(m: WebviewMessage): Promise<void> {
    switch (m.type) {
      case "ready":
        return this.onReady();
      case "send":
        return this.send(m.text, m.clientId, m.includeContext);
      case "cancel":
        return this.cancel();
      case "approve":
        return this.decide(m.requestId, m.decision, m.feedback);
      case "openDiff": {
        if (m.requestId) {
          const a = this.approvals.get(m.requestId);
          const d = a?.params.detail;
          if (d?.path && typeof d.new === "string") {
            await this.shared.diffs.openProposal(this.diffKey(m.requestId), d.path, d.old ?? null, d.new,
              (dec) => this.decide(m.requestId!, dec), false);
          }
        } else if (m.diff) {
          await this.shared.diffs.openView(m.diff.path, m.diff.old, m.diff.new);
        }
        return;
      }
      case "openFile":
        return this.openFile(m.path, m.line);
      case "searchFiles": {
        const files = await this.shared.files.search(m.query, this.cwd);
        this.post({ type: "fileResults", requestId: m.requestId, files });
        return;
      }
      case "command":
        return this.command(m.name, m.arg);
      case "setMode":
        return this.setMode(m.mode);
      case "pickModel":
        return this.pickModel();
      case "history":
        return this.history();
      case "newSession":
        return this.newSession();
      case "rewind":
        return this.rewind(m.index);
      case "btw":
        if (!(await this.ensureStarted())) return;
        await this.client!.call("btw", { question: m.question, id: m.id });
        return;
      case "copy":
        await vscode.env.clipboard.writeText(m.text);
        return;
      case "insertCode":
        return this.insertCode(m.text);
      case "restart":
        return this.restart();
      case "setApiKey":
        if (await promptForApiKey(this.shared.ctx.secrets)) await this.restart();
        return;
      case "openTerminal":
        return this.openTerminal();
      case "focusChanged":
        this.shared.focused(this, m.focused);
        return;
      case "saveDraft":
        return;
    }
  }

  private async send(text: string, clientId: string, includeContext: boolean) {
    if (!(await this.ensureStarted())) {
      this.post({ type: "event", event: { type: "turn_end", reason: "error", error: "tai is not running",
        removedUser: null, unsent: [text], stats: { input: 0, output: 0, seconds: 0, model: "" },
        usage: this.state?.usage ?? { input: 0, output: 0, ctx: 0, maxCtx: null } } });
      return;
    }
    const attachments = parseMentions(text, this.cwd);
    const context = includeContext && settings().attachOpenFile ? this.shared.tracker.context(this.cwd) : null;
    try {
      await this.client!.call("send", { text, clientId, attachments, context }, 30000);
    } catch (e: any) {
      this.post({ type: "event", event: { type: "turn_end", reason: "error", error: e.message,
        removedUser: null, unsent: [text], stats: { input: 0, output: 0, seconds: 0, model: "" },
        usage: this.state?.usage ?? { input: 0, output: 0, ctx: 0, maxCtx: null } } });
    }
  }

  async cancel(): Promise<void> {
    if (!this.client?.running) return;
    const r = await this.client.call("cancel", {}, 10000);
    if (r?.unsent?.length) this.post({ type: "prefill", text: r.unsent.join("\n\n") });
  }

  /** Stops a running task (after asking) so that the conversation can be switched. */
  private async stopIfBusy(what: string): Promise<boolean> {
    if (!this.busy) return true;
    const ok = await vscode.window.showWarningMessage(`tai is still working. Stop it and ${what}?`,
      { modal: true }, "Stop");
    if (ok !== "Stop") return false;
    await this.cancel();
    const t = Date.now();
    while (this.busy && Date.now() - t < 8000) await new Promise((r) => setTimeout(r, 50));
    return !this.busy;
  }

  async newSession(): Promise<void> {
    if (!(await this.ensureStarted())) return;
    if (!(await this.stopIfBusy("start a new conversation"))) return;
    this.dropApprovals();
    this.state = await this.client!.call("new_session", {}, 10000);
    this.post({ type: "transcript", items: [], state: this.state! });
    this.onSession?.(this.state!.session);
  }

  async loadSession(id: string): Promise<void> {
    if (!(await this.ensureStarted())) return;
    if (id === this.state?.session.id) return;
    if (this.shared.revealSession(this, id)) {
      vscode.window.showInformationMessage("That conversation is already open in another tai chat.");
      return;
    }
    if (!(await this.stopIfBusy("open another conversation"))) return;
    const r = await this.client!.call("load_session", { id }, 15000);
    this.state = r.state;
    this.post({ type: "transcript", items: r.transcript, state: r.state });
    this.onSession?.(r.state.session);
  }

  async setMode(mode: Mode): Promise<void> {
    if (!MODES.includes(mode) || !(await this.ensureStarted())) return;
    this.state = await this.client!.call("set", { mode }, 10000);
    this.post({ type: "state", state: this.state! });
  }

  async cycleMode(): Promise<void> {
    const cur = this.state?.mode ?? "manual";
    await this.setMode(MODES[(MODES.indexOf(cur) + 1) % MODES.length]);
  }

  private async models(): Promise<ModelInfo[]> {
    if (this.modelsCache && Date.now() - this.modelsCache.at < 300000) return this.modelsCache.models;
    const r = await this.client!.call("models", {}, 30000);
    this.modelsCache = { at: Date.now(), models: r.models };
    return r.models;
  }

  async setModel(model: string): Promise<void> {
    if (!(await this.ensureStarted())) return;
    this.state = await this.client!.call("set", { model }, 10000);
    await this.shared.ctx.globalState.update("tai.lastModel", model);
    this.post({ type: "state", state: this.state! });
  }

  async pickModel(): Promise<void> {
    if (!(await this.ensureStarted())) return;
    const qp = vscode.window.createQuickPick<vscode.QuickPickItem & { id?: string }>();
    qp.title = "tai: model for this conversation";
    qp.placeholder = "Type to filter, or enter any model id";
    qp.busy = true;
    qp.show();
    let items: (vscode.QuickPickItem & { id?: string })[] = [];
    try {
      const models = (await this.models()).filter((m) => !m.embedding);
      items = models.map((m) => ({
        id: m.id, label: (m.id === this.state?.model ? "$(check) " : "") + m.id,
        description: m.maxInput ? `${human(m.maxInput)} context` : undefined,
      }));
    } catch (e: any) {
      qp.placeholder = `Could not list models (${e.message}); enter a model id`;
    }
    qp.items = items;
    qp.activeItems = items.filter((i) => i.id === this.state?.model);
    qp.busy = false;
    qp.onDidChangeValue((v) => {
      const val = v.trim();
      qp.items = val && !items.some((i) => i.id === val)
        ? [...items, { id: val, label: `Use “${val}”`, description: "custom model id" }] : items;
    });
    const picked = await new Promise<string | undefined>((resolve) => {
      qp.onDidAccept(() => resolve(qp.selectedItems[0]?.id));
      qp.onDidHide(() => resolve(undefined));
    });
    qp.dispose();
    if (picked) await this.setModel(picked);
  }

  async history(): Promise<void> {
    if (!(await this.ensureStarted())) return;
    type Item = vscode.QuickPickItem & { id?: string; all?: boolean };
    const del: vscode.QuickInputButton = { iconPath: new vscode.ThemeIcon("trash"), tooltip: "Delete" };
    const ren: vscode.QuickInputButton = { iconPath: new vscode.ThemeIcon("edit"), tooltip: "Rename" };
    let all = false;
    const qp = vscode.window.createQuickPick<Item>();
    qp.title = "tai: conversation history";
    qp.placeholder = "Search conversations";
    qp.matchOnDescription = true;
    const load = async () => {
      qp.busy = true;
      const r = await this.client!.call("sessions", { all }, 15000);
      const sessions: SessionMeta[] = r.sessions;
      const items: Item[] = sessions.map((s) => ({
        id: s.id, label: (s.id === this.state?.session.id ? "$(comment-discussion) " : "") + (s.title || "(untitled)"),
        description: `${ago(s.updated)} · ${s.messages} message${s.messages === 1 ? "" : "s"} · ${s.model}`,
        detail: all ? s.cwd.replace(os.homedir(), "~") : undefined, buttons: [ren, del],
      }));
      if (!all) items.push({ label: "$(folder-library) Show conversations from all folders", all: true, alwaysShow: true });
      if (!sessions.length && all) items.push({ label: "No saved conversations yet", alwaysShow: true });
      qp.items = items;
      qp.busy = false;
    };
    qp.onDidTriggerItemButton(async (e) => {
      if (!e.item.id) return;
      if (e.button === del) {
        await this.client!.call("delete_session", { id: e.item.id }, 10000);
        if (e.item.id === this.state?.session.id) {
          this.state = await this.client!.call("state", {}, 10000);
          this.post({ type: "transcript", items: [], state: this.state! });
        }
      } else {
        qp.hide();
        const title = await vscode.window.showInputBox({ title: "Rename conversation",
          value: e.item.label.replace(/^\$\([^)]*\) /, "") });
        if (title !== undefined) await this.client!.call("rename_session", { id: e.item.id, title }, 10000);
        return;
      }
      await load();
    });
    const picked = new Promise<Item | undefined>((resolve) => {
      qp.onDidAccept(() => {
        const it = qp.selectedItems[0];
        if (it?.all) {
          all = true;
          load();
          return;
        }
        resolve(it);
        qp.hide();
      });
      qp.onDidHide(() => resolve(undefined));
    });
    qp.show();
    await load();
    const it = await picked;
    qp.dispose();
    if (it?.id) {
      try {
        await this.loadSession(it.id);
      } catch (e: any) {
        this.notice("error", `Could not open the conversation: ${e.message}`);
      }
    }
  }

  async rewind(index: number): Promise<void> {
    if (!(await this.ensureStarted())) return;
    if (this.busy) {
      this.notice("warning", "Stop the current task before rewinding.");
      return;
    }
    const prev = await this.client!.call("rewind_preview", { index }, 10000);
    const files: { rel: string; created: boolean; restorable: boolean }[] = prev.files;
    type Item = vscode.QuickPickItem & { files: boolean; conversation: boolean };
    const list = files.map((f) => (f.created ? `${f.rel} (delete)` : f.rel) + (f.restorable ? "" : " (cannot restore)")).join(", ");
    const items: Item[] = files.length ? [
      { label: "$(discard) Restore code and conversation", detail: `Files: ${list}`, files: true, conversation: true },
      { label: "$(comment-discussion) Restore conversation only", detail: "Files stay as they are now", files: false, conversation: true },
      { label: "$(file-code) Restore code only", detail: `Keep the conversation; files: ${list}`, files: true, conversation: false },
    ] : [
      { label: "$(comment-discussion) Rewind the conversation to here",
        detail: "tai made no file edits from here on (changes made by shell commands are not tracked)",
        files: false, conversation: true },
    ];
    const pick = await vscode.window.showQuickPick(items, {
      title: "Rewind to this message", placeHolder: "Your message is put back into the input box to edit and resend" });
    if (!pick) return;
    const r = await this.client!.call("rewind", { index, files: pick.files, conversation: pick.conversation }, 30000);
    this.state = r.state;
    this.post({ type: "transcript", items: r.transcript, state: r.state });
    if (pick.conversation) this.post({ type: "prefill", text: r.text });
    if (r.failed?.length) {
      this.notice("warning", "Could not restore: " + r.failed.map((f: any) => `${f.path} (${f.error})`).join(", "));
    } else if (r.restored?.length) {
      this.notice("info", `Restored ${r.restored.join(", ")}`);
    }
  }

  async exportConversation(): Promise<void> {
    if (!(await this.ensureStarted())) return;
    const title = (this.state?.session.title || "conversation").replace(/[^\w.-]+/g, "-").slice(0, 50);
    const uri = await vscode.window.showSaveDialog({
      defaultUri: vscode.Uri.file(path.join(this.cwd, `tai-${title}.md`)), filters: { Markdown: ["md"] } });
    if (!uri) return;
    await this.client!.call("export", { path: uri.fsPath }, 15000);
    this.notice("info", `Saved to ${relPath(this.cwd, uri.fsPath)}`);
  }

  async openTerminal(): Promise<void> {
    const exe = findExecutable(config().get("executablePath"));
    if (!exe) {
      vscode.window.showErrorMessage("Could not find the tai program.");
      return;
    }
    const { env } = await this.env();
    const keep: Record<string, string> = {};
    for (const k of [KEY_VAR, ...Object.keys(config().get("environmentVariables", {}) ?? {})]) {
      if (env[k]) keep[k] = env[k]!;
    }
    const term = vscode.window.createTerminal({ name: "tai", cwd: this.cwd, env: keep });
    term.show();
    const q = (s: string) => `'${s.replace(/'/g, `'\\''`)}'`;
    const id = this.state?.session.messages ? this.state.session.id : undefined;
    const py = (config().get<string>("pythonPath") ?? "").trim();
    term.sendText(`${py ? q(py) + " " : ""}${q(exe)}${id ? ` -r ${id}` : ""}`);
  }

  private async openFile(p: string, line?: number) {
    let target = path.isAbsolute(p) ? p : path.resolve(this.cwd, p.replace(/^~(?=\/)/, os.homedir()));
    if (!fs.existsSync(target)) {
      const found = await vscode.workspace.findFiles(`**/${p.replace(/^\.?\//, "")}`, "**/node_modules/**", 1);
      if (!found.length) {
        this.notice("warning", `File not found: ${p}`);
        return;
      }
      target = found[0].fsPath;
    }
    const uri = vscode.Uri.file(target);
    if (fs.statSync(target).isDirectory()) {
      await vscode.commands.executeCommand("revealInExplorer", uri);
      return;
    }
    const opts: vscode.TextDocumentShowOptions = { preview: true, viewColumn: vscode.ViewColumn.One };
    if (line && line > 0) {
      const pos = new vscode.Position(line - 1, 0);
      opts.selection = new vscode.Range(pos, pos);
    }
    try {
      await vscode.window.showTextDocument(uri, opts);
    } catch {
      await vscode.commands.executeCommand("vscode.open", uri);
    }
  }

  private async insertCode(text: string) {
    const ed = this.shared.tracker.activeEditor;
    if (ed && !ed.document.isClosed) {
      await vscode.window.showTextDocument(ed.document, ed.viewColumn);
      await ed.edit((b) => ed.selections.forEach((s) => b.replace(s, text)));
    } else {
      const doc = await vscode.workspace.openTextDocument({ content: text });
      await vscode.window.showTextDocument(doc);
    }
  }

  // ------------------------------------------------------------------ slash commands handled here
  private compactFailed(error: string) {
    this.post({ type: "event", event: { type: "compact_end", error, transcript: null,
      usage: this.state?.usage ?? { input: 0, output: 0, ctx: 0, maxCtx: null } } });
  }

  private async command(name: string, arg: string) {
    if (!(await this.ensureStarted())) {
      if (name === "compact") this.compactFailed("tai is not running");
      return;
    }
    const c = this.client!;
    switch (name) {
      case "model":
        return arg ? this.setModel(arg) : this.pickModel();
      case "tools": {
        if (this.busy) return this.notice("warning", "Stop the current task first.");
        this.state = await c.call("set", { tools: !this.state?.tools }, 10000);
        this.post({ type: "state", state: this.state! });
        return this.notice("info", this.state!.tools ? "Tools on (agent mode)" : "Tools off (plain chat)");
      }
      case "system": {
        if (this.busy) return this.notice("warning", "Stop the current task first.");
        let text: string | undefined = arg;
        if (!arg) {
          text = await vscode.window.showInputBox({ title: "Extra instructions for this conversation",
            prompt: "Added to tai's system prompt. Leave empty to remove.", value: this.state?.system ?? "" });
          if (text === undefined) return;
        }
        this.state = await c.call("set", { system: text }, 10000);
        this.post({ type: "state", state: this.state! });
        return this.notice("info", text ? "Instructions set" : "Instructions removed");
      }
      case "compact":
        if (this.busy) return this.compactFailed("Stop the current task first.");
        this.setBusy(true);          // before the call: compact_end may arrive before its reply
        try {
          await c.call("compact", { instructions: arg }, 10000);
        } catch (e: any) {
          this.setBusy(false);
          this.compactFailed(e.message);
        }
        return;
      case "usage": {
        const u = await c.call("usage", {}, 10000);
        const ctx = u.ctx ? ` · context ${human(u.ctx)}${u.maxCtx ? ` of ${human(u.maxCtx)} (${Math.round(100 * u.ctx / u.maxCtx)}%)` : ""}` : "";
        return this.notice("info", `This session: ${human(u.input)} in · ${human(u.output)} out · ${human(u.input + u.output)} total${ctx}`);
      }
      case "save":
        if (arg) {
          const p = path.resolve(this.cwd, arg.replace(/^~(?=\/)/, os.homedir()));
          await c.call("export", { path: p }, 15000);
          return this.notice("info", `Saved to ${relPath(this.cwd, p)}`);
        }
        return this.exportConversation();
      case "history":
      case "resume":
        return this.history();
      case "terminal":
        return this.openTerminal();
      case "restart":
        return this.restart();
      case "rewind": {
        const t = await c.call("transcript", {}, 10000);
        const users = (t.transcript as TranscriptItem[]).filter((i): i is Extract<TranscriptItem, { type: "user" }> =>
          i.type === "user" && i.kind !== "summary");
        if (!users.length) return this.notice("info", "Nothing to rewind yet.");
        const pick = await vscode.window.showQuickPick(users.reverse().map((u) => ({
          label: u.text.split("\n")[0].slice(0, 100) || "(empty)", index: u.index })), { title: "Rewind to which message?" });
        if (pick) return this.rewind(pick.index);
        return;
      }
      default:
        return this.notice("error", `Unknown command /${name} — type /help`);
    }
  }

  async dispose(): Promise<void> {
    this.disposed = true;
    this.dropApprovals();
    this.subs.forEach((s) => s.dispose());
    this.webviewSubs.forEach((s) => s.dispose());
    const c = this.client;
    this.client = undefined;
    this.shared.busyChanged();
    if (c) await c.dispose();
  }
}

export { mentionFor };
