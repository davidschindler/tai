// The tai chat UI (runs inside a VS Code webview).
import type {
  ApprovalParams, Decision, EditorContext, FileHit, HostMessage, Mode, Settings, TaiEvent, TaiState,
  TranscriptItem, Usage, WebviewMessage,
} from "../src/protocol";
import { escapeHtml, icon, iconButton, renderDiff, renderMarkdown, RenderHooks } from "./render";
import css from "./style.css";

declare function acquireVsCodeApi(): { postMessage(m: unknown): void; getState(): any; setState(s: any): void };
const vscode = acquireVsCodeApi();
const send = (m: WebviewMessage) => vscode.postMessage(m);

// ------------------------------------------------------------------ state
interface UiState {
  state?: TaiState;
  settings: Settings;
  cwd: string;
  version: string;
  modeInfo: Record<string, string>;
  ready: boolean;
  busy: boolean;
  compacting: boolean;
  autoCompacting: boolean;
  turnStart: number;
  outTokens: number;
  writingTool: string;
  context: EditorContext | null;
  includeContext: boolean;
  history: string[];
  historyPos: number;
  images: PastedImage[];
}

type PastedImage = { name: string; data: string };
const MAX_IMAGE = 3_750_000;     // bytes; as in tai

const saved = (vscode.getState() ?? {}) as { sessionId?: string; draft?: string; history?: string[] };
const S: UiState = {
  settings: { ctrlEnterToSend: false, attachOpenFile: true, showTimestamps: false },
  cwd: "", version: "", modeInfo: {}, ready: false, busy: false, compacting: false, autoCompacting: false, turnStart: 0, outTokens: 0,
  writingTool: "", context: null, includeContext: true, history: saved.history ?? [], historyPos: -1,
  images: [],
};

function persist() {
  vscode.setState({ sessionId: S.state?.session.id, draft: input.value, history: S.history.slice(-50) });
}

const MODES: Mode[] = ["manual", "accept-edits", "auto", "plan"];

const hooks: RenderHooks = {
  openFile: (path, line) => send({ type: "openFile", path, line }),
  copy: (text) => { send({ type: "copy", text }); toast("Copied"); },
  insert: (text) => send({ type: "insertCode", text }),
};

// ------------------------------------------------------------------ DOM skeleton
const style = document.createElement("style");
style.textContent = css;
document.head.append(style);

const app = document.getElementById("app")!;
const LOGO = app.dataset.logo ?? "";
const LOGO_DARK = app.dataset.logoDark ?? LOGO;
app.innerHTML = `
  <div id="banner" hidden></div>
  <div id="scroller"><div id="messages"></div><div id="thinking" hidden></div></div>
  <div id="todos" hidden></div>
  <div id="approvals"></div>
  <div id="composer">
    <div id="menu" hidden role="listbox"></div>
    <div id="chips"></div>
    <textarea id="input" rows="1" spellcheck="true" aria-label="Message tai"></textarea>
    <div id="toolbar">
      <button id="modeBtn" class="pill" title="Permission mode (Shift+Tab)"></button>
      <button id="modelBtn" class="pill" title="Switch model"></button>
      <button id="mentionBtn" class="iconbtn" title="Mention a file (@)">${icon("at")}</button>
      <span class="spacer"></span>
      <span id="usage" class="dim"></span>
      <button id="sendBtn" class="sendbtn" title="Send (Enter)">${icon("send")}</button>
    </div>
  </div>
  <div id="toast" hidden></div>`;
const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const banner = $("banner"), scroller = $("scroller"), messages = $("messages"), thinking = $("thinking");
const todosEl = $("todos");
const approvalsEl = $("approvals"), menu = $("menu"), chips = $("chips"), usageEl = $("usage");
const input = $<HTMLTextAreaElement>("input"), modeBtn = $("modeBtn"), modelBtn = $("modelBtn"), sendBtn = $<HTMLButtonElement>("sendBtn");
const mentionBtn = $("mentionBtn");

input.value = saved.draft ?? "";

// ------------------------------------------------------------------ helpers
function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function human(n: number): string {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

function nearBottom(): boolean {
  return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 120;
}

let stick = true;
scroller.addEventListener("scroll", () => (stick = nearBottom()));
function scrollDown(force = false) {
  if (force || stick) {
    scroller.scrollTop = scroller.scrollHeight;
    stick = true;
  }
}

let toastTimer = 0;
function toast(text: string) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => (t.hidden = true), 1400);
}

function clientId(): string {
  return Math.random().toString(36).slice(2, 10);
}

// ------------------------------------------------------------------ messages
let assistant: { el: HTMLElement; body: HTMLElement; text: string; timer: number } | null = null;
const tools = new Map<string, HTMLElement>();      // tool id → row
const tasks = new Map<string, HTMLElement>();      // task id → box
const btws = new Map<string, { body: HTMLElement; text: string; box: HTMLElement }>();

function showEmpty() {
  if (messages.children.length) return;
  const e = el("div", "empty");
  e.innerHTML = `
    <img class="logo logo-light" src="${LOGO}" alt="">
    <img class="logo logo-dark" src="${LOGO_DARK}" alt="">
    <h2>What can tai do for you?</h2>
    <p class="dim">tai reads and edits files, runs commands, searches the web and starts sub-agents.
    It asks before it changes anything (see the mode below).</p>
    <ul class="tips">
      <li><kbd>@</kbd> mention files &nbsp;·&nbsp; <kbd>/</kbd> commands</li>
      <li><kbd>Alt</kbd>+<kbd>K</kbd> in the editor mentions the selection</li>
      <li><kbd>Shift</kbd>+<kbd>Tab</kbd> switches the permission mode</li>
      <li><kbd>Esc</kbd> stops tai &nbsp;·&nbsp; <kbd>/btw</kbd> asks a side question</li>
    </ul>`;
  messages.append(e);
}

function clearEmpty() {
  messages.querySelector(".empty")?.remove();
}

function imageChip(name: string): HTMLElement {
  const c = el("span", "chip small img");
  c.innerHTML = icon("image") + `<span>${escapeHtml(name)}</span>`;
  c.title = "Image sent with this message";
  return c;
}

function addUser(text: string, opts: { index?: number; files?: string[]; context?: string; clientId?: string;
  pending?: boolean; queued?: boolean; time?: number; kind?: string; images?: string[] } = {}): HTMLElement {
  clearEmpty();
  if (opts.kind === "summary") {
    const d = el("details", "summary-block");
    const s = el("summary", undefined, "Conversation compacted — summary of the earlier part");
    const body = el("div", "md");
    renderMarkdown(body, text, hooks, true);
    d.append(s, body);
    messages.append(d);
    return d;
  }
  const m = el("div", "msg user");
  if (opts.index !== undefined) m.dataset.index = String(opts.index);
  if (opts.clientId) m.dataset.client = opts.clientId;
  if (opts.pending) m.classList.add("pending");
  if (opts.queued) m.classList.add("queued");
  const bubble = el("div", "bubble");
  bubble.innerHTML = highlightMentions(text);
  m.append(bubble);
  const meta = el("div", "meta");
  for (const f of [...(opts.context ? [opts.context] : []), ...(opts.files ?? [])]) {
    const c = el("span", "chip small");
    c.innerHTML = icon("file") + `<span>${escapeHtml(f)}</span>`;
    c.title = "Open " + f;
    c.addEventListener("click", () => {
      const mm = /^(.*?)(?::(\d+)(?:-\d+)?)?$/.exec(f)!;
      hooks.openFile(mm[1], mm[2] ? parseInt(mm[2], 10) : undefined);
    });
    meta.append(c);
  }
  for (const name of opts.images ?? []) meta.append(imageChip(name));
  if (opts.queued) meta.append(el("span", "queued-label", "queued — sent when tai finishes its current step"));
  if (S.settings.showTimestamps) {
    meta.append(el("span", "time dim", new Date((opts.time ?? Date.now() / 1000) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })));
  }
  const actions = el("div", "actions");
  actions.append(
    iconButton("rewind", "Rewind to here (edit & resend, optionally restore files)", () => {
      if (m.dataset.index) send({ type: "rewind", index: parseInt(m.dataset.index, 10) });
    }, "iconbtn rewind"),
    iconButton("copy", "Copy", () => hooks.copy(text)),
  );
  m.append(meta, actions);
  messages.append(m);
  return m;
}

function highlightMentions(text: string): string {
  return escapeHtml(text).replace(/(^|[\s(])(@(?:&quot;[^&]+&quot;|[^\s]+))/g, '$1<span class="mention">$2</span>');
}

function startAssistant(): NonNullable<typeof assistant> {
  if (assistant) return assistant;
  clearEmpty();
  const m = el("div", "msg assistant");
  const body = el("div", "md");
  m.append(body);
  messages.append(m);
  assistant = { el: m, body, text: "", timer: 0 };
  return assistant;
}

function appendText(delta: string) {
  const a = startAssistant();
  a.text += delta;
  if (!a.timer) {
    a.timer = window.setTimeout(() => {
      a.timer = 0;
      renderMarkdown(a.body, a.text, hooks, false);
      scrollDown();
    }, 50);
  }
}

function endAssistant() {
  if (!assistant) return;
  clearTimeout(assistant.timer);
  if (assistant.text.trim()) {
    renderMarkdown(assistant.body, assistant.text, hooks, true);
    const copy = iconButton("copy", "Copy answer", () => hooks.copy(assistant0.text), "iconbtn msgcopy");
    const assistant0 = assistant;
    assistant.el.append(copy);
  } else {
    assistant.el.remove();
  }
  assistant = null;
  scrollDown();
}

function addAssistant(text: string) {
  startAssistant().text = text;
  endAssistant();
}

const LABEL_ARG: Record<string, string> = { bash: "command", powershell: "command", web_search: "query", web_fetch: "url", grep: "pattern" };

function toolRow(id: string, name: string, label: string, shown: string, args: Record<string, unknown>, agent?: string | null): HTMLElement {
  const row = el("div", "tool running");
  row.dataset.name = name;
  const head = el("div", "tool-head");
  const dot = el("span", "dot");
  const lab = el("span", "tool-label", label);
  const arg = el("span", "tool-arg");
  const argText = shown || String(args[LABEL_ARG[name]] ?? "");
  arg.textContent = argText;
  if (["read_file", "write_file", "edit_file"].includes(name) && shown) {
    arg.classList.add("path-link");
    arg.title = "Open " + shown;
    arg.addEventListener("click", (e) => {
      e.stopPropagation();
      hooks.openFile(String(args.path ?? shown));
    });
  }
  const chev = el("span", "chev");
  chev.innerHTML = icon("chevron");
  head.append(dot, lab, arg, chev);
  const summary = el("div", "tool-summary dim");
  const detail = el("div", "tool-detail");
  detail.hidden = true;
  row.append(head, summary, detail);
  head.addEventListener("click", () => {
    detail.hidden = !detail.hidden;
    row.classList.toggle("open", !detail.hidden);
  });
  if ((name === "bash" || name === "powershell") && typeof args.command === "string") {
    const pre = el("pre", "cmd");
    pre.textContent = (name === "powershell" ? "PS> " : "$ ") + args.command;
    detail.append(pre);
  }
  tools.set(id, row);
  const parent = agent ? tasks.get(agent)?.querySelector(".task-body") : null;
  if (parent) parent.append(row);
  else {
    clearEmpty();
    messages.append(row);
  }
  return row;
}

function finishTool(row: HTMLElement, summary: string, error: boolean, output: string,
  diff: { path: string; old: string | null; new: string } | null) {
  row.classList.remove("running");
  row.classList.add(error ? "failed" : "ok");
  const sum = row.querySelector(".tool-summary") as HTMLElement;
  const detail = row.querySelector(".tool-detail") as HTMLElement;
  const lines = summary.split("\n");
  sum.textContent = "⎿  " + lines[0] + (lines.length > 1 ? `  (+${lines.length - 1} lines)` : "");
  if (error) sum.classList.add("err");
  if (diff) {
    const { el: d, stats } = renderDiff(diff.old, diff.new, 300);
    sum.textContent = "⎿  " + (diff.old === null ? `Created · ${stats.added} lines` : `+${stats.added} −${stats.removed}`);
    const bar = el("div", "detail-bar");
    const open = el("button", "linkbtn");
    open.innerHTML = icon("diff") + " Open diff";
    open.addEventListener("click", () => send({ type: "openDiff", diff }));
    bar.append(open);
    detail.append(bar, d);
    // edits are shown expanded, like in the terminal
    if (stats.added + stats.removed <= 60) {
      detail.hidden = false;
      row.classList.add("open");
    }
  } else if (output) {
    const pre = el("pre", "output");
    pre.textContent = output;
    detail.append(pre);
  }
}

function addTask(id: string, label: string, prompt: string): HTMLElement {
  clearEmpty();
  const box = el("div", "task running");
  const head = el("div", "task-head");
  head.innerHTML = `<span class="dot"></span><span class="tool-label">Task</span>`;
  head.append(el("span", "tool-arg", label));
  const p = el("div", "task-prompt dim", prompt.length > 300 ? prompt.slice(0, 299) + "…" : prompt);
  p.title = prompt;
  const body = el("div", "task-body");
  const res = el("div", "task-result");
  box.append(head, p, body, res);
  tasks.set(id, box);
  messages.append(box);
  return box;
}

function finishTask(id: string, result: string, error: boolean, uses?: number, tokens?: number) {
  const box = tasks.get(id) ?? addTask(id, "Task", "");
  box.classList.remove("running");
  box.classList.add(error ? "failed" : "ok");
  const res = box.querySelector(".task-result") as HTMLElement;
  const d = el("details");
  const s = el("summary", undefined, `Report${uses !== undefined ? ` · ${uses} tool uses` : ""}${tokens ? ` · ${human(tokens)} tokens` : ""}`);
  const md = el("div", "md");
  renderMarkdown(md, result, hooks, true);
  d.append(s, md);
  res.replaceChildren(d);
}

function addNotice(level: "info" | "warning" | "error", text: string, action?: { label: string; run: () => void }) {
  clearEmpty();
  const n = el("div", `notice ${level}`);
  n.append(el("span", undefined, text));
  if (action) {
    const b = el("button", "linkbtn", action.label);
    b.addEventListener("click", () => { action.run(); b.remove(); });
    n.append(b);
  }
  messages.append(n);
  scrollDown();
}

function addStats(stats: { input: number; output: number; seconds: number; model: string }, usage: Usage) {
  if (!stats.input && !stats.output) return;
  const bits = [`${human(stats.input)} in`, `${human(stats.output)} out`, `${Math.round(stats.seconds)}s`];
  if (usage.ctx) bits.push(ctxText(usage));
  bits.push(stats.model);
  messages.append(el("div", "stats", bits.join(" · ")));
}

function ctxText(u: Usage): string {
  if (!u.ctx) return "";
  return u.maxCtx ? `ctx ${Math.round((100 * u.ctx) / u.maxCtx)}%` : `ctx ${human(u.ctx)}`;
}

function renderTranscript(items: TranscriptItem[]) {
  messages.replaceChildren();
  tools.clear();
  tasks.clear();
  assistant = null;
  for (const it of items) {
    if (it.type === "user") addUser(it.text, { index: it.index, files: it.files, context: it.context, time: it.time, kind: it.kind, images: it.images });
    else if (it.type === "assistant") {
      if (it.streaming) {          // the reply being written right now: keep appending to it
        const a = startAssistant();
        a.text = it.text;
        renderMarkdown(a.body, a.text, hooks, false);
      } else addAssistant(it.text);
    } else if (it.type === "tool") {
      const row = toolRow(it.id, it.name, it.label, it.shown, it.args);
      if (it.done) finishTool(row, it.summary || (it.error ? "failed" : "done"), it.error, it.output, null);
    } else if (it.type === "task") {
      addTask(it.id, it.label, it.prompt);
      if (it.done) finishTask(it.id, it.result, it.result.startsWith("Sub-agent failed"));
    }
  }
  showEmpty();
  scrollDown(true);
}

// ------------------------------------------------------------------ thinking indicator
const FRAMES = "·✢✳✶✻✽✻✶✳✢";
let tick = 0;
setInterval(() => {
  if (thinking.hidden) return;
  tick++;
  renderThinking();
}, 125);

function renderThinking() {
  const secs = Math.floor((Date.now() - S.turnStart) / 1000);
  const verb = S.compacting || S.autoCompacting ? "Compacting conversation…" : S.writingTool ? `Writing ${S.writingTool} call…`
    : assistant?.text ? "Responding…" : "Thinking…";
  const bits = [`${secs}s`];
  if (S.outTokens) bits.push(`↓ ~${human(S.outTokens)} tokens`);
  bits.push("esc to interrupt");
  thinking.innerHTML = `<span class="spin">${FRAMES[tick % FRAMES.length]}</span><span class="verb">${escapeHtml(verb)}</span><span class="dim">(${bits.join(" · ")})</span>`;
}

function updateThinking() {
  const show = (S.busy || S.compacting) && approvalsEl.children.length === 0;
  thinking.hidden = !show;
  if (show) renderThinking();
}

function setBusy(b: boolean) {
  S.busy = b;
  if (S.state) S.state.busy = b;
  if (b) {
    S.turnStart = Date.now();
    S.outTokens = 0;
    S.writingTool = "";
  }
  updateThinking();
  updateSendBtn();
}

// ------------------------------------------------------------------ approvals
function addApproval(requestId: string, p: ApprovalParams) {
  if (approvalsEl.querySelector(`[data-req="${requestId}"]`)) return;
  const card = el("div", "approval");
  card.dataset.req = requestId;
  card.tabIndex = 0;
  const head = el("div", "approval-head");
  head.append(el("span", "approval-title", p.title));
  if (p.agent) head.append(el("span", "dim", `· ${p.agent}`));
  card.append(head);
  const d = p.detail ?? {};
  if (d.type === "edit") {
    const pathRow = el("div", "approval-path");
    const link = el("span", "path-link", relPath(d.path ?? ""));
    link.addEventListener("click", () => hooks.openFile(d.path ?? ""));
    pathRow.append(link);
    if (!d.tooLarge && typeof d.new === "string") {
      const { el: diffEl, stats } = renderDiff(d.old ?? null, d.new, 80);
      pathRow.append(el("span", "dim", d.old == null ? ` new file · ${stats.added} lines` : ` +${stats.added} −${stats.removed}`));
      const open = el("button", "linkbtn");
      open.innerHTML = icon("diff") + " Open diff";
      open.addEventListener("click", () => send({ type: "openDiff", requestId }));
      pathRow.append(open);
      card.append(pathRow, diffEl);
    } else {
      card.append(pathRow, el("div", "dim", "(file too large to preview)"));
    }
  } else if (d.type === "bash") {
    const pre = el("pre", "cmd");
    pre.textContent = d.command ?? "";
    card.append(pre);
  } else if (d.type === "fetch") {
    card.append(el("div", "approval-url", d.url ?? ""));
  } else if (d.type === "read") {
    card.append(el("div", "approval-path", relPath(d.path ?? "")));
  } else if (d.type === "plan") {
    const plan = el("div", "approval-plan md");
    renderMarkdown(plan, d.plan ?? "", hooks, true);
    card.append(plan);
  }
  card.append(el("div", "approval-q", p.question));
  const opts = el("div", "approval-opts");
  const mk = (n: string, label: string, dec: Decision, cls = "") => {
    const b = el("button", "opt " + cls);
    b.innerHTML = `<kbd>${n}</kbd> ${escapeHtml(label)}`;
    b.addEventListener("click", () => choose(dec));
    return b;
  };
  const [yes, yes2, no] = p.labels ?? ["Yes", `Yes, and don't ask again ${p.always}`.trim(), "No, and tell tai what to do differently"];
  opts.append(mk("1", yes, "yes", "primary"), mk("2", yes2, "always"), mk("3", no, "no"));
  card.append(opts);
  const fb = el("div", "feedback");
  fb.hidden = true;
  const fbInput = el("input") as HTMLInputElement;
  fbInput.placeholder = d.type === "plan" ? "What should tai change in the plan? (Enter to send, Esc to skip)"
    : "What should tai do instead? (Enter to send, Esc to skip)";
  fb.append(fbInput);
  card.append(fb);

  const answer = (dec: Decision, feedback?: string) => {
    send({ type: "approve", requestId, decision: dec, feedback });
    card.classList.add("answered");
  };
  function choose(dec: Decision) {
    if (card.classList.contains("answered")) return;
    if (dec !== "no") return answer(dec);
    fb.hidden = false;
    opts.hidden = true;
    fbInput.focus();
  }
  fbInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); answer("no", fbInput.value.trim()); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); answer("no", ""); }
  });
  card.addEventListener("keydown", (e) => {
    if (e.target !== card && (e.target as HTMLElement).tagName !== "BUTTON") return;
    if (e.key === "1" || e.key === "y") { e.preventDefault(); choose("yes"); }
    else if (e.key === "2" || e.key === "a") { e.preventDefault(); choose("always"); }
    else if (e.key === "3" || e.key === "n") { e.preventDefault(); choose("no"); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); choose("no"); }
  });
  approvalsEl.append(card);
  updateThinking();
  if (!input.value.trim() || document.activeElement !== input) card.focus();
}

function removeApproval(requestId: string) {
  const card = approvalsEl.querySelector(`[data-req="${requestId}"]`);
  const hadFocus = card?.contains(document.activeElement);
  card?.remove();
  updateThinking();
  const next = approvalsEl.querySelector<HTMLElement>(".approval");
  if (hadFocus) (next ?? input).focus();
}

function relPath(p: string): string {
  if (!S.cwd) return p;
  const win = S.cwd.includes("\\");                     // Windows: either separator, any case
  const norm = (s: string) => (win ? s.replace(/\\/g, "/").toLowerCase() : s);
  const base = norm(S.cwd).replace(/\/+$/, "") + "/";
  return norm(p).startsWith(base) ? p.slice(base.length).replace(/\\/g, "/") : p;
}

// ------------------------------------------------------------------ /btw
function startBtw(question: string) {
  const id = clientId();
  clearEmpty();
  const box = el("div", "btw");
  const head = el("div", "btw-head");
  head.append(el("span", "btw-tag", "/btw"), el("span", "btw-q", question));
  head.append(iconButton("close", "Dismiss", () => box.remove()));
  const body = el("div", "md");
  body.append(el("span", "dim", "…"));
  box.append(head, body, el("div", "btw-foot dim", "side question · not added to the conversation"));
  messages.append(box);
  btws.set(id, { body, text: "", box });
  scrollDown(true);
  send({ type: "btw", question, id });
}

// ------------------------------------------------------------------ composer
const COMMANDS: [string, string][] = [
  ["/btw", "side question, not kept in the conversation"],
  ["/clear", "start a new conversation"],
  ["/compact", "summarize the conversation to free up context"],
  ["/model", "switch model"],
  ["/mode", "set permission mode (manual, accept-edits, auto, plan)"],
  ["/remember", "keep an instruction for later conversations (all projects or this one)"],
  ["/history", "open an earlier conversation"],
  ["/rewind", "go back to an earlier message"],
  ["/permissions", "show and edit the saved permission rules, and the sandbox"],
  ["/review", "review the changes in git (/review main: everything since main)"],
  ["/jobs", "commands running in the background (/jobs stop ID)"],
  ["/tools", "toggle agent tools on/off (plain chat)"],
  ["/system", "set extra instructions for this conversation"],
  ["/usage", "token usage this session"],
  ["/save", "save the conversation as markdown"],
  ["/terminal", "continue this conversation in the terminal"],
  ["/restart", "restart the tai process"],
  ["/help", "keys and commands"],
];

const HELP = [
  "**Keys** — Enter send · Shift+Enter new line · Esc stop · Shift+Tab permission mode · ↑ previous message",
  "**In the editor** — Alt+K mention the selection · Ctrl+Esc focus tai · Ctrl+Shift+Esc new tab",
  "**Mentions** — `@path` attaches a file or folder (images too), `@file#L10-20` a line range; paste a screenshot with Ctrl+V",
  "",
  "**Commands**",
  "",
  ...COMMANDS.map((c) => `- \`${c[0]}\` ${c[1]}`),
].join("\n\n").replace(/\n\n- /g, "\n- ");

function autosize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, Math.max(120, window.innerHeight * 0.4)) + "px";
}

function updateSendBtn() {
  const stop = (S.busy || S.compacting) && !input.value.trim() && !S.images.length;
  sendBtn.innerHTML = icon(stop ? "stop" : "send");
  sendBtn.title = stop ? "Stop (Esc)" : S.busy ? "Queue message (sent at tai's next step)" : `Send (${S.settings.ctrlEnterToSend ? "Ctrl+Enter" : "Enter"})`;
  sendBtn.classList.toggle("stop", stop);
  sendBtn.disabled = !stop && !input.value.trim() && !S.images.length;
}

function updatePlaceholder() {
  input.placeholder = S.busy ? "Queue another message…" : "Ask tai… (@ to mention files, / for commands)";
}

// the task list tai keeps (todo_write): shown above the composer while it has open items
function renderTodos() {
  const items = S.state?.todos ?? [];
  const done = items.filter((t) => t.status === "completed").length;
  todosEl.hidden = !items.length || done === items.length;
  if (todosEl.hidden) return;
  const head = el("div", "todos-head", `Tasks · ${done} of ${items.length} done`);
  const list = el("ul");
  for (const t of items) {
    const li = el("li", t.status.replace("_", "-"));
    li.append(el("span", "mark", t.status === "completed" ? "☒" : t.status === "in_progress" ? "◼" : "☐"),
      el("span", "text", t.content));
    list.append(li);
  }
  todosEl.replaceChildren(head, list);
}

function updateToolbar() {
  renderTodos();
  const st = S.state;
  const mode: Mode = st?.mode ?? "manual";
  const label = { manual: "⏸ manual", "accept-edits": "⏵ accept edits", auto: "⏵⏵ auto", plan: "◇ plan" }[mode];
  modeBtn.textContent = st?.tools === false ? "chat only" : label;
  modeBtn.className = "pill mode-" + (st?.tools === false ? "chat" : mode);
  modeBtn.title = (st?.tools === false ? "Tools are off (/tools)" : (S.modeInfo[mode] ?? mode)) + " — Shift+Tab to cycle";
  modelBtn.textContent = (st?.model ?? "model") + " ▾";
  if (st) usageEl.textContent = ctxText(st.usage);
  usageEl.title = st ? `Session: ${human(st.usage.input)} in · ${human(st.usage.output)} out` +
    (st.usage.ctx ? ` · context ${human(st.usage.ctx)}${st.usage.maxCtx ? ` of ${human(st.usage.maxCtx)}` : ""}` : "") : "";
  updatePlaceholder();
}

function renderChips() {
  chips.replaceChildren();
  S.images.forEach((im, i) => {
    const chip = el("span", "chip image");
    const img = document.createElement("img");
    img.src = im.data;
    img.alt = im.name;
    chip.append(img, el("span", "chip-text", im.name));
    chip.title = "Sent with your next message. Click to remove.";
    chip.addEventListener("click", () => { S.images.splice(i, 1); renderChips(); updateSendBtn(); });
    chips.append(chip);
  });
  const c = S.context;
  if (!c || !S.settings.attachOpenFile) return;
  const chip = el("span", "chip ctx" + (S.includeContext ? "" : " off"));
  const label = c.selection ? `${c.rel}:${c.selection.start === c.selection.end ? c.selection.start : `${c.selection.start}-${c.selection.end}`}` : c.rel;
  chip.innerHTML = icon("file") + `<span class="chip-text">${escapeHtml(label)}</span>`;
  chip.title = S.includeContext ? "tai sees this file and selection. Click to leave it out of the next message." : "Left out. Click to include.";
  const eye = el("span", "eye");
  eye.innerHTML = icon(S.includeContext ? "eye" : "eyeoff");
  chip.append(eye);
  chip.addEventListener("click", () => { S.includeContext = !S.includeContext; renderChips(); });
  chips.append(chip);
}

function submit() {
  const text = input.value.trim();
  if (!text && !S.images.length) {
    if (S.busy || S.compacting) send({ type: "cancel" });
    return;
  }
  closeMenu();
  if (S.compacting && !text.startsWith("/")) {
    toast("Wait until compacting has finished");
    return;
  }
  if (text.startsWith("/") && !text.startsWith("//")) {
    const [cmd, ...rest] = text.slice(1).split(/\s+/);
    const arg = text.slice(1 + cmd.length).trim();
    if (runCommand(cmd.toLowerCase(), arg, rest)) {
      remember(text);
      input.value = "";
      autosize();
      updateSendBtn();
      persist();
    }
    return;
  }
  sendMessage(text.startsWith("//") ? text.slice(1) : text);
}

/** Sends a message (or a /review or custom command, which tai expands) and shows it. */
function sendMessage(msg: string, command?: { name: string; arg: string }) {
  const id = clientId();
  const images = S.images;
  S.images = [];
  const withContext = command?.name !== "review" && S.includeContext;
  addUser(msg, { clientId: id, pending: !S.busy, queued: S.busy, images: images.map((i) => i.name),
    context: withContext && S.settings.attachOpenFile && S.context ? ctxLabel(S.context) : undefined });
  send({ type: "send", text: msg, clientId: id, includeContext: withContext,
    ...(images.length ? { images } : {}), ...(command ? { command: command.name, arg: command.arg } : {}) });
  remember(msg);
  input.value = "";
  S.includeContext = true;
  renderChips();
  autosize();
  updateSendBtn();
  persist();
  scrollDown(true);
  if (!S.busy) setBusy(true);
}

function ctxLabel(c: EditorContext): string {
  return c.selection ? `${c.rel}:${c.selection.start === c.selection.end ? c.selection.start : `${c.selection.start}-${c.selection.end}`}` : c.rel;
}

function remember(text: string) {
  if (S.history[S.history.length - 1] !== text) S.history.push(text);
  S.historyPos = -1;
}

function runCommand(cmd: string, arg: string, _rest: string[]): boolean {
  switch (cmd) {
    case "help": {
      clearEmpty();
      const n = el("div", "notice info");
      const md = el("div", "md");
      const own = ownCommands();
      const ownHelp = own.length ? "\n\n**Your own commands**\n\n" + own.map((c) => `- \`${c[0]}\` ${c[1]}`).join("\n")
        : "\n\n**Your own commands** — a Markdown file in `.tai/commands/` becomes a command: `.tai/commands/referee.md` → `/referee` (`$ARGUMENTS` = what you type after it).";
      renderMarkdown(md, HELP + ownHelp + (S.version ? `\n\n*tai ${S.version}*` : ""), hooks, true);
      n.append(md);
      messages.append(n);
      scrollDown(true);
      return true;
    }
    case "btw":
      if (!arg) { addNotice("error", "Usage: /btw your question"); return false; }
      startBtw(arg);
      return true;
    case "clear":
    case "new":
      send({ type: "newSession" });
      return true;
    case "mode": {
      if (arg && !MODES.includes(arg as Mode)) { addNotice("error", `Unknown mode ${arg}: use manual, accept-edits, auto or plan`); return false; }
      const cur = S.state?.mode ?? "manual";
      send({ type: "setMode", mode: (arg as Mode) || MODES[(MODES.indexOf(cur) + 1) % MODES.length] });
      return true;
    }
    case "models":
      send({ type: "pickModel" });
      return true;
    case "model": case "tools": case "system": case "compact": case "usage": case "save":
    case "history": case "resume": case "terminal": case "restart": case "rewind": case "permissions": case "jobs":
    case "remember":
      if (cmd === "compact" && !S.busy) {
        S.compacting = true;
        S.turnStart = Date.now();
        updateThinking();
        updateSendBtn();
      }
      send({ type: "command", name: cmd, arg });
      return true;
    case "review":
      sendMessage(`/review${arg ? " " + arg : ""}`, { name: "review", arg });
      return false;           // sendMessage already cleared the input
    default:
      if ((S.state?.commands ?? []).some((c) => c.name === cmd)) {
        sendMessage(`/${cmd}${arg ? " " + arg : ""}`, { name: cmd, arg });
        return false;
      }
      addNotice("error", `Unknown command /${cmd} — type /help`);
      return false;
  }
}

function ownCommands(): [string, string][] {
  return (S.state?.commands ?? []).map((c) => [`/${c.name}`, c.description + (c.scope === "project" ? " (project)" : "")]);
}

// ---- slash and @ menus
type MenuItem = { label: string; desc?: string; apply: () => void };
let menuItems: MenuItem[] = [];
let menuSel = 0;
let menuKind: "" | "slash" | "at" = "";
let fileReq = 0;
let atStart = -1;

function openMenu(kind: "slash" | "at", items: MenuItem[]) {
  menuKind = kind;
  menuItems = items;
  menuSel = 0;
  if (!items.length) return closeMenu();
  renderMenu();
}

function renderMenu() {
  menu.replaceChildren();
  menuItems.forEach((it, i) => {
    const row = el("div", "menu-item" + (i === menuSel ? " sel" : ""));
    row.setAttribute("role", "option");
    row.append(el("span", "menu-label", it.label));
    if (it.desc) row.append(el("span", "menu-desc dim", it.desc));
    row.addEventListener("mousedown", (e) => { e.preventDefault(); it.apply(); });
    menu.append(row);
  });
  menu.hidden = false;
  (menu.children[menuSel] as HTMLElement | undefined)?.scrollIntoView({ block: "nearest" });
}

function closeMenu() {
  menu.hidden = true;
  menuKind = "";
  menuItems = [];
  atStart = -1;
}

function updateMenus() {
  const v = input.value;
  const pos = input.selectionStart ?? v.length;
  const before = v.slice(0, pos);
  if (/^\/\S*$/.test(before) && !before.startsWith("//")) {
    const q = before.slice(1).toLowerCase();
    openMenu("slash", [...COMMANDS, ...ownCommands()].filter(([c]) => c.slice(1).startsWith(q)).map(([c, d]) => ({
      label: c, desc: d, apply: () => {
        input.value = c + " " + v.slice(pos).trimStart();
        input.setSelectionRange(c.length + 1, c.length + 1);
        closeMenu();
        autosize();
        updateSendBtn();
      },
    })));
    return;
  }
  const m = /(^|[\s(])@("?[^\s"]*)$/.exec(before);
  if (m) {
    atStart = pos - m[2].length - 1;
    const q = m[2].replace(/^"/, "");
    const req = ++fileReq;
    menuKind = "at";
    send({ type: "searchFiles", query: q, requestId: req });
    return;
  }
  if (menuKind) closeMenu();
}

function showFiles(requestId: number, files: FileHit[]) {
  if (requestId !== fileReq || menuKind !== "at" || atStart < 0) return;
  const start = atStart;
  openMenu("at", files.map((f) => ({
    label: f.rel, desc: f.dir ? "folder" : undefined, apply: () => {
      const pos = input.selectionStart ?? input.value.length;
      const token = "@" + (/\s/.test(f.rel) ? `"${f.rel}"` : f.rel);
      input.value = input.value.slice(0, start) + token + " " + input.value.slice(pos);
      const caret = start + token.length + 1;
      input.setSelectionRange(caret, caret);
      closeMenu();
      autosize();
      updateSendBtn();
      input.focus();
    },
  })));
  if (!files.length) {
    menu.replaceChildren(el("div", "menu-item dim", "No matching files"));
    menu.hidden = false;
  }
}

function insertAtCursor(text: string) {
  const s = input.selectionStart ?? input.value.length;
  const e = input.selectionEnd ?? s;
  const before = input.value.slice(0, s);
  const pad = before && !/\s$/.test(before) ? " " : "";
  input.value = before + pad + text + " " + input.value.slice(e);
  const caret = s + pad.length + text.length + 1;
  input.focus();
  input.setSelectionRange(caret, caret);
  autosize();
  updateSendBtn();
  persist();
}

input.addEventListener("input", () => {
  autosize();
  updateSendBtn();
  updateMenus();
  persist();
});
input.addEventListener("click", updateMenus);
// pasted screenshots and images: attached to the next message
input.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.items ?? [])].filter((i) => i.kind === "file" && /^image\/(png|jpeg|gif|webp)$/.test(i.type))
    .map((i) => i.getAsFile()).filter((f): f is File => !!f);
  if (!files.length) return;
  e.preventDefault();
  for (const f of files) {
    if (f.size > MAX_IMAGE) { toast(`That image is too large (${(f.size / 1e6).toFixed(1)} MB; at most 3.7 MB)`); continue; }
    const r = new FileReader();
    r.onload = () => {
      const n = S.images.length + 1;
      S.images.push({ name: f.name && f.name !== "image.png" ? f.name : `image ${n}.${f.type.split("/")[1].replace("jpeg", "jpg")}`, data: String(r.result) });
      renderChips();
      updateSendBtn();
    };
    r.readAsDataURL(f);
  }
});
input.addEventListener("blur", () => setTimeout(() => { if (document.activeElement !== input) closeMenu(); }, 150));

input.addEventListener("keydown", (e) => {
  if (e.isComposing) return;
  if (!menu.hidden && menuItems.length) {
    if (e.key === "ArrowDown" || (e.key === "n" && e.ctrlKey)) { e.preventDefault(); menuSel = (menuSel + 1) % menuItems.length; renderMenu(); return; }
    if (e.key === "ArrowUp" || (e.key === "p" && e.ctrlKey)) { e.preventDefault(); menuSel = (menuSel - 1 + menuItems.length) % menuItems.length; renderMenu(); return; }
    if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey && menuKind === "at") ||
        (e.key === "Enter" && !e.shiftKey && menuKind === "slash" && input.value.trim() !== menuItems[menuSel].label)) {
      e.preventDefault();
      menuItems[menuSel].apply();
      return;
    }
  }
  if (e.key === "Escape") {
    if (!menu.hidden) { e.preventDefault(); e.stopPropagation(); closeMenu(); return; }
    if (S.busy || S.compacting) { e.preventDefault(); e.stopPropagation(); send({ type: "cancel" }); return; }
  }
  if (e.key === "Tab" && e.shiftKey) {
    e.preventDefault();
    cycleMode();
    return;
  }
  const ctrlEnter = S.settings.ctrlEnterToSend;
  if (e.key === "Enter" && !e.shiftKey && !e.altKey) {
    if (ctrlEnter ? (e.ctrlKey || e.metaKey) : !(e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      submit();
      return;
    }
    if (!ctrlEnter) return;      // Ctrl+Enter with Enter-to-send: let VS Code have it
  }
  if (e.key === "ArrowUp" && !e.shiftKey && (!input.value || S.historyPos >= 0) && S.history.length) {
    if (input.selectionStart === 0 || !input.value || S.historyPos >= 0) {
      e.preventDefault();
      S.historyPos = S.historyPos < 0 ? S.history.length - 1 : Math.max(0, S.historyPos - 1);
      input.value = S.history[S.historyPos];
      autosize();
      updateSendBtn();
    }
  } else if (e.key === "ArrowDown" && S.historyPos >= 0) {
    e.preventDefault();
    S.historyPos++;
    if (S.historyPos >= S.history.length) { S.historyPos = -1; input.value = ""; }
    else input.value = S.history[S.historyPos];
    autosize();
    updateSendBtn();
  } else if (e.key !== "ArrowUp" && e.key !== "ArrowDown" && e.key !== "Shift") {
    S.historyPos = -1;
  }
});

function cycleMode() {
  const cur = S.state?.mode ?? "manual";
  const next = MODES[(MODES.indexOf(cur) + 1) % MODES.length];
  if (S.state) S.state.mode = next;
  updateToolbar();
  send({ type: "setMode", mode: next });
}

sendBtn.addEventListener("click", submit);
modeBtn.addEventListener("click", cycleMode);
modelBtn.addEventListener("click", () => send({ type: "pickModel" }));
mentionBtn.addEventListener("click", () => {
  insertAtCursor("@");
  input.value = input.value.replace(/@ $/, "@");
  const caret = input.value.length;
  input.setSelectionRange(caret, caret);
  updateMenus();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !e.defaultPrevented && (S.busy || S.compacting) &&
      !["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName ?? "") &&
      !approvalsEl.contains(document.activeElement)) {
    send({ type: "cancel" });
  }
});
window.addEventListener("focus", () => send({ type: "focusChanged", focused: true }));
window.addEventListener("blur", () => send({ type: "focusChanged", focused: false }));
window.addEventListener("resize", autosize);

// ------------------------------------------------------------------ banner
function setBanner(kind: "" | "info" | "error", html: string, buttons: { label: string; run: () => void }[] = []) {
  if (!kind) { banner.hidden = true; return; }
  banner.className = "banner " + kind;
  banner.innerHTML = `<span>${html}</span>`;
  for (const b of buttons) {
    const btn = el("button", "linkbtn", b.label);
    btn.addEventListener("click", b.run);
    banner.append(btn);
  }
  banner.hidden = false;
}

// ------------------------------------------------------------------ host messages
function onEvent(e: TaiEvent) {
  switch (e.type) {
    case "user": {
      const pending = e.clientId ? messages.querySelector<HTMLElement>(`.msg.user[data-client="${e.clientId}"]`) : null;
      if (pending) {
        pending.dataset.index = String(e.index);
        pending.classList.remove("pending", "queued");
        pending.querySelector(".queued-label")?.remove();
        if (e.injected) {
          endAssistant();
          messages.append(pending);        // a queued message goes where the agent actually picked it up
        }
        // show the attachments tai resolved
        const meta = pending.querySelector(".meta")!;
        meta.querySelectorAll(".chip:not(.img)").forEach((c) => c.remove());
        for (const f of [...(e.context ? [e.context] : []), ...e.files].reverse()) {
          const c = el("span", "chip small");
          c.innerHTML = icon("file") + `<span>${escapeHtml(f)}</span>`;
          c.addEventListener("click", () => {
            const mm = /^(.*?)(?::(\d+)(?:-\d+)?)?$/.exec(f)!;
            hooks.openFile(mm[1], mm[2] ? parseInt(mm[2], 10) : undefined);
          });
          meta.prepend(c);
        }
      } else {
        endAssistant();
        addUser(e.text, { index: e.index, files: e.files, context: e.context, images: e.images });
      }
      scrollDown();
      break;
    }
    case "turn_start":
      setBusy(true);
      break;
    case "text":
      S.writingTool = "";
      S.outTokens += Math.max(1, Math.round(e.delta.length / 4));
      appendText(e.delta);
      break;
    case "message_end":
      endAssistant();
      break;
    case "tool_writing":
      if (!e.agent) { S.writingTool = e.name; S.outTokens = Math.max(S.outTokens, e.tokens); }
      break;
    case "tool_start":
      if (!e.agent) { endAssistant(); S.writingTool = ""; }
      toolRow(e.id, e.name, e.label, e.shown, e.args, e.agent);
      scrollDown();
      break;
    case "tool_end": {
      const row = tools.get(e.id);
      if (row) finishTool(row, e.summary, e.error, e.output, e.diff);
      scrollDown();
      break;
    }
    case "task_start":
      endAssistant();
      addTask(e.id, e.label, e.prompt);
      scrollDown();
      break;
    case "task_note": {
      const body = tasks.get(e.id)?.querySelector(".task-body");
      body?.append(el("div", "task-note dim", e.text));
      break;
    }
    case "task_end":
      finishTask(e.id, e.result, e.error, e.uses, e.tokens);
      scrollDown();
      break;
    case "usage":
      if (S.state) { S.state.usage = e.usage; updateToolbar(); }
      break;
    case "turn_end": {
      endAssistant();
      setBusy(false);
      messages.querySelectorAll(".tool.running").forEach((r) => {
        r.classList.remove("running");
        r.classList.add("failed");
      });
      messages.querySelectorAll(".task.running").forEach((r) => r.classList.remove("running"));
      const restore: string[] = [];
      if (e.removedUser) {
        const b = messages.querySelector(`.msg.user[data-index="${e.removedUser.index}"]`) ??
          [...messages.querySelectorAll<HTMLElement>(".msg.user.pending")].pop();
        b?.remove();
        restore.push(e.removedUser.text);
      }
      messages.querySelectorAll<HTMLElement>(".msg.user.queued, .msg.user.pending").forEach((b) => b.remove());
      restore.push(...e.unsent);
      if (restore.length) prefill(restore.join("\n\n"));
      if (e.reason === "cancelled") addNotice("warning", "Interrupted — tell tai what to do instead.");
      else if (e.reason === "error") addNotice("error", e.error ?? "Something went wrong.",
        restore.length ? { label: "Retry", run: submit } : undefined);
      else if (e.reason === "max_steps") addNotice("warning", "tai stopped after the maximum number of steps — say “continue” to keep going.");
      if (S.state) S.state.usage = e.usage;
      addStats(e.stats, e.usage);
      updateToolbar();
      showEmpty();
      scrollDown();
      break;
    }
    case "session":
      if (S.state) S.state.session = e.session;
      persist();
      break;
    case "state":
      S.state = e.state;
      updateToolbar();
      break;
    case "notice":
      addNotice(e.level, e.text);
      break;
    case "btw_text": {
      const b = btws.get(e.id);
      if (!b) break;
      b.text += e.delta;
      renderMarkdown(b.body, b.text, hooks, false);
      scrollDown();
      break;
    }
    case "btw_end": {
      const b = btws.get(e.id);
      if (!b) break;
      if (e.error) b.body.replaceChildren(el("span", "err", e.error));
      else if (!b.text.trim()) b.body.replaceChildren(el("span", "dim", "(no answer)"));
      else renderMarkdown(b.body, b.text, hooks, true);
      btws.delete(e.id);
      break;
    }
    case "todos":
      if (S.state) S.state.todos = e.items;
      renderTodos();
      break;
    case "job_done": {
      const j = e.job;
      addNotice(j.stopped || j.code === 0 ? "info" : "warning", `Background job ${j.id} ${j.state}: ${j.command}`);
      break;
    }
    case "auto_compact":
      // the context window was nearly full during a turn: tai summarized the conversation
      S.autoCompacting = e.phase === "start";
      if (e.phase === "end") {
        if (e.error === null) {
          // earlier messages are now part of the summary: they can't be rewound to any more
          messages.querySelectorAll<HTMLElement>(".msg.user[data-index]").forEach((b) => {
            delete b.dataset.index;
            b.querySelector(".actions .rewind")?.remove();
          });
          addNotice("info", "The context window was nearly full, so tai summarized the conversation so far and carries on. " +
            `(${human(e.before)} → ${human(e.after ?? 0)} tokens)`);
        } else if (e.error !== "cancelled") {
          addNotice("warning", `Compacting failed: ${e.error}`);
        }
        if (S.state) S.state.usage = e.usage;
        updateToolbar();
      }
      updateThinking();
      break;
    case "compact_end":
      S.compacting = false;
      setBusy(false);
      if (e.transcript) {
        renderTranscript(e.transcript);
        addNotice("info", "Conversation compacted. tai continues from the summary.");
      } else if (e.error) {
        addNotice(e.error === "cancelled" ? "warning" : "error", `Compacting failed: ${e.error}`);
      }
      if (S.state) S.state.usage = e.usage;
      updateToolbar();
      break;
    default:
      break;
  }
}

function prefill(text: string) {
  input.value = input.value.trim() ? input.value.replace(/\s+$/, "") + "\n\n" + text : text;
  autosize();
  updateSendBtn();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  persist();
}

window.addEventListener("message", (ev: MessageEvent<HostMessage>) => {
  const m = ev.data;
  switch (m.type) {
    case "status":
      if (m.status === "starting") setBanner("info", "Starting tai…");
      else if (m.status === "ready") {
        S.ready = true;
        if (m.needsKey) setBanner("error", "No Tilburg.ai API key found. Set it once (stored in your system's keychain):", [{ label: "Set API key", run: () => send({ type: "setApiKey" }) }]);
        else setBanner("", "");
      } else {
        S.ready = false;
        setBusy(false);
        S.compacting = false;
        setBanner(m.status === "error" ? "error" : "info", escapeHtml(m.message ?? "tai stopped"),
          m.canRestart ? [{ label: "Restart", run: () => send({ type: "restart" }) }] : []);
      }
      break;
    case "init":
      S.state = m.state;
      S.settings = m.settings;
      S.cwd = m.cwd;
      S.version = m.version;
      S.modeInfo = m.modeInfo;
      S.busy = m.state.busy;
      updateThinking();
      updateToolbar();
      updateSendBtn();
      renderChips();
      persist();
      break;
    case "transcript":
      S.state = m.state;
      renderTranscript(m.items);
      setBusy(m.state.busy);
      updateToolbar();
      persist();
      break;
    case "state":
      S.state = m.state;
      updateToolbar();
      break;
    case "settings":
      S.settings = m.settings;
      renderChips();
      updateSendBtn();
      break;
    case "event":
      onEvent(m.event);
      break;
    case "approval":
      addApproval(m.requestId, m.params);
      scrollDown();
      break;
    case "approvalDone":
      removeApproval(m.requestId);
      break;
    case "context": {
      const changed = m.context?.path !== S.context?.path;
      S.context = m.context;
      if (changed) S.includeContext = true;
      renderChips();
      break;
    }
    case "fileResults":
      showFiles(m.requestId, m.files);
      break;
    case "insertMention":
      insertAtCursor(m.text);
      break;
    case "prefill":
      prefill(m.text);
      break;
    case "notice":
      addNotice(m.level, m.text);
      break;
    case "focus":
      input.focus();
      break;
    case "stop":
      send({ type: "cancel" });
      break;
  }
});

// ------------------------------------------------------------------ start
autosize();
updateSendBtn();
updateToolbar();
showEmpty();
send({ type: "ready" });
