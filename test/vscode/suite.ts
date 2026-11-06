// Integration tests in a real VS Code: the extension, real `tai --serve`, and the mock API.
import * as assert from "assert";
import * as fs from "fs";
import * as http from "http";
import * as path from "path";
import * as vscode from "vscode";
import type { ChatController } from "../../src/chatController";
import type { HostMessage, TaiEvent } from "../../src/protocol";

const API = process.env.TAI_API_BASE!;
const WS = vscode.workspace.workspaceFolders![0].uri.fsPath;
const mock = (...steps: object[]) => "MOCK: " + JSON.stringify(steps);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function until<T>(f: () => T | undefined | false | Promise<T | undefined | false>, ms = 15000, what = "condition"): Promise<T> {
  const t = Date.now();
  while (Date.now() - t < ms) {
    const v = await f();
    if (v) return v;
    await sleep(40);
  }
  throw new Error(`timed out waiting for ${what}`);
}

function apiLog(): Promise<any[]> {
  return new Promise((resolve, reject) => {
    http.get(API + "/_log", (res) => {
      let b = "";
      res.on("data", (d) => (b += d));
      res.on("end", () => resolve(JSON.parse(b)));
    }).on("error", reject);
  });
}

/** Records what a controller posts to its webview, and lets tests act as the webview. */
class Probe {
  posted: HostMessage[] = [];
  private orig: (m: HostMessage) => void;
  constructor(public c: ChatController) {
    this.orig = c.post.bind(c);
    c.post = (m: HostMessage) => {
      this.posted.push(m);
      this.orig(m);
    };
  }
  events(type?: string): TaiEvent[] {
    return this.posted.filter((m): m is Extract<HostMessage, { type: "event" }> => m.type === "event")
      .map((m) => m.event).filter((e) => !type || e.type === type);
  }
  mark() {
    return this.posted.length;
  }
  async webview(msg: any) {
    await (this.c as any).onMessage(msg);
  }
  async send(text: string, includeContext = false, timeout = 20000) {
    const m = this.mark();
    await this.webview({ type: "send", text, clientId: "t" + Math.random(), includeContext });
    return this.turnEnd(m, timeout);
  }
  async turnEnd(m: number, timeout = 20000) {
    return until(() => this.posted.slice(m).find((x) => x.type === "event" && x.event.type === "turn_end") as any,
      timeout, "turn_end").then((x) => x.event);
  }
  since(m: number, type: string): any[] {
    return this.posted.slice(m).filter((x) => x.type === type || (x.type === "event" && x.event.type === type))
      .map((x: any) => (x.type === "event" ? x.event : x));
  }
}

function diffTabs(): vscode.Tab[] {
  return vscode.window.tabGroups.all.flatMap((g) => g.tabs)
    .filter((t) => t.input instanceof vscode.TabInputTextDiff && t.input.modified.scheme === "tai-proposed");
}

let ext: any;
let probe: Probe;

before(async function () {
  this.timeout(60000);
  const e = vscode.extensions.getExtension("davidschindler.tai-vscode")!;
  const api = await e.activate();
  ext = api.ext;
  await vscode.commands.executeCommand("tai.openSidebar");
  await until(() => ext.sidebar, 10000, "sidebar controller");
  probe = new Probe(ext.sidebar);
  await until(() => ext.sidebar.state, 20000, "tai to start");
});

describe("tai extension", function () {

  afterEach(async () => {
    await vscode.commands.executeCommand("workbench.action.closeAllEditors");
  });

  it("registers its commands", async () => {
    const cmds = await vscode.commands.getCommands(true);
    for (const c of ["tai.focus", "tai.openInNewTab", "tai.newConversation", "tai.history", "tai.insertAtMention",
      "tai.acceptDiff", "tai.rejectDiff", "tai.selectModel", "tai.openInTerminal", "tai.setApiKey"]) {
      assert.ok(cmds.includes(c), c);
    }
  });

  it("the real webview loads and talks to the controller", async () => {
    // the webview posts 'cancel' when told to stop: a full round trip through the webview script
    const seen: string[] = [];
    const orig = (ext.sidebar as any).onMessage.bind(ext.sidebar);
    (ext.sidebar as any).onMessage = (m: any) => { seen.push(m.type); return orig(m); };
    try {
      ext.sidebar.post({ type: "stop" });
      await until(() => seen.includes("cancel"), 10000, "webview round trip");
    } finally {
      (ext.sidebar as any).onMessage = orig;
    }
  });

  it("answers a message", async () => {
    const end = await probe.send("hello from vscode");
    assert.strictEqual(end.reason, "done", end.error);
    const text = probe.events("text").map((e: any) => e.delta).join("");
    assert.ok(text.includes("Echo: hello from vscode"), text);
    assert.ok(ext.sidebar.state.session.title.startsWith("hello from vscode"));
  });

  it("opens a native diff for an edit and accepts it with tai.acceptDiff", async () => {
    const f = path.join(WS, "accept.txt");
    fs.writeFileSync(f, "old line\n");
    const m = probe.mark();
    await probe.webview({ type: "send", text: mock({ tools: [{ name: "edit_file", args: { path: "accept.txt", old_string: "old", new_string: "new" } }] }, { text: "done" }), clientId: "a", includeContext: false });
    await until(() => diffTabs().length === 1, 15000, "diff tab");
    const tab = diffTabs()[0];
    assert.match(tab.label, /accept\.txt.*tai's proposal/);
    const right = (tab.input as vscode.TabInputTextDiff).modified;
    assert.strictEqual((await vscode.workspace.openTextDocument(right)).getText(), "new line\n");
    // make the diff the active editor, then accept from the editor title command
    await vscode.commands.executeCommand("vscode.diff", (tab.input as vscode.TabInputTextDiff).original, right);
    await vscode.commands.executeCommand("tai.acceptDiff");
    const end = await probe.turnEnd(m);
    assert.strictEqual(end.reason, "done", end.error);
    assert.strictEqual(fs.readFileSync(f, "utf8"), "new line\n");
    await until(() => diffTabs().length === 0, 5000, "diff tab closed");
    assert.ok(probe.since(m, "approvalDone").length === 1);
  });

  it("rejects an edit with tai.rejectDiff", async () => {
    const f = path.join(WS, "reject.txt");
    fs.writeFileSync(f, "keep me\n");
    const m = probe.mark();
    await probe.webview({ type: "send", text: mock({ tools: [{ name: "write_file", args: { path: "reject.txt", content: "gone" } }] }, { text: "ok" }), clientId: "r", includeContext: false });
    const tab = await until(() => diffTabs()[0], 15000, "diff tab");
    await vscode.commands.executeCommand("vscode.diff", (tab.input as vscode.TabInputTextDiff).original,
      (tab.input as vscode.TabInputTextDiff).modified);
    await vscode.commands.executeCommand("tai.rejectDiff");
    await probe.turnEnd(m);
    assert.strictEqual(fs.readFileSync(f, "utf8"), "keep me\n");
    const te = probe.since(m, "tool_end")[0];
    assert.ok(te.error && /Denied/.test(te.summary), te.summary);
  });

  it("answering in the chat closes the diff tab", async () => {
    fs.writeFileSync(path.join(WS, "chat.txt"), "a\n");
    const m = probe.mark();
    await probe.webview({ type: "send", text: mock({ tools: [{ name: "edit_file", args: { path: "chat.txt", old_string: "a", new_string: "b" } }] }, { text: "ok" }), clientId: "c", includeContext: false });
    const ap = await until(() => probe.since(m, "approval")[0], 15000, "approval");
    await until(() => diffTabs().length === 1, 15000, "diff tab");
    await probe.webview({ type: "approve", requestId: ap.requestId, decision: "yes" });
    await probe.turnEnd(m);
    await until(() => diffTabs().length === 0, 5000, "diff tab closed");
    assert.strictEqual(fs.readFileSync(path.join(WS, "chat.txt"), "utf8"), "b\n");
  });

  it("stopping tai withdraws the approval and closes its diff", async () => {
    fs.writeFileSync(path.join(WS, "stop.txt"), "a\n");
    const m = probe.mark();
    await probe.webview({ type: "send", text: mock({ tools: [{ name: "edit_file", args: { path: "stop.txt", old_string: "a", new_string: "b" } }] }), clientId: "s", includeContext: false });
    await until(() => diffTabs().length === 1, 15000, "diff tab");
    await probe.webview({ type: "cancel" });
    const end = await probe.turnEnd(m);
    assert.strictEqual(end.reason, "cancelled");
    await until(() => diffTabs().length === 0, 5000, "diff closed");
    assert.strictEqual(ext.sidebar.pendingApprovals, 0);
  });

  it("gives tai VS Code's diagnostics", async () => {
    const f = path.join(WS, "diag.py");
    fs.writeFileSync(f, "x = \n");
    const coll = vscode.languages.createDiagnosticCollection("test");
    coll.set(vscode.Uri.file(f), [new vscode.Diagnostic(new vscode.Range(0, 4, 0, 5), "Expected expression", vscode.DiagnosticSeverity.Error)]);
    try {
      const m = probe.mark();
      await probe.webview({ type: "send", text: mock({ tools: [{ name: "get_diagnostics", args: { path: "diag.py" } }] }, { text: "seen" }), clientId: "d", includeContext: false });
      await probe.turnEnd(m);
      const te = probe.since(m, "tool_end")[0];
      assert.match(te.output, /diag\.py:1:5: error: Expected expression/);
    } finally {
      coll.dispose();
    }
  });

  it("saves unsaved changes before tai reads a file", async () => {
    const f = path.join(WS, "dirty.txt");
    fs.writeFileSync(f, "on disk\n");
    const doc = await vscode.workspace.openTextDocument(f);
    const ed = await vscode.window.showTextDocument(doc);
    await ed.edit((b) => b.insert(new vscode.Position(0, 0), "UNSAVED "));
    assert.ok(doc.isDirty);
    const m = probe.mark();
    await probe.webview({ type: "send", text: mock({ tools: [{ name: "read_file", args: { path: "dirty.txt" } }] }, { text: "ok" }), clientId: "u", includeContext: false });
    await probe.turnEnd(m);
    assert.match(probe.since(m, "tool_end")[0].output, /UNSAVED on disk/);
    assert.ok(!doc.isDirty);
  });

  it("sends the open file and selection as context", async () => {
    const f = path.join(WS, "ctx.py");
    fs.writeFileSync(f, "one\ntwo\nthree\nfour\n");
    const ed = await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(f));
    ed.selection = new vscode.Selection(1, 0, 3, 0);      // lines 2-3 (ends at column 0 of line 4)
    const ctx = await until(() => {
      const c = [...probe.posted].reverse().find((x) => x.type === "context") as any;
      return c?.context?.selection ? c.context : undefined;
    }, 5000, "context message");
    assert.deepStrictEqual([ctx.rel, ctx.selection.start, ctx.selection.end, ctx.selection.text], ["ctx.py", 2, 3, "two\nthree\n"]);
    await probe.send("explain this", true);
    const last = (await apiLog()).pop();
    const content = last.messages[last.messages.length - 1].content;
    assert.match(content, /<ide_context>\nThe user has ctx\.py open in the editor\.\nSelected \(lines 2-3\)/);
    const u = probe.events("user").pop() as any;
    assert.strictEqual(u.context, "ctx.py:2-3");
  });

  it("attaches @-mentioned files", async () => {
    fs.mkdirSync(path.join(WS, "sub dir"), { recursive: true });
    fs.writeFileSync(path.join(WS, "sub dir", "notes.md"), "line1\nline2\nline3\n");
    await probe.send('compare @"sub dir/notes.md"#L2-3 and @missing.txt please');
    const last = (await apiLog()).pop();
    const content = last.messages[last.messages.length - 1].content;
    assert.match(content, /<file path="sub dir\/notes\.md:2-3">\n +2\tline2\n +3\tline3\n<\/file>/);
    assert.ok(!content.includes("missing.txt\">"));
  });

  it("Alt+K inserts an @-mention of the selection", async () => {
    const f = path.join(WS, "mention.ts");
    fs.writeFileSync(f, "a\nb\nc\nd\n");
    const ed = await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(f));
    ed.selection = new vscode.Selection(1, 0, 2, 1);
    const m = probe.mark();
    await vscode.commands.executeCommand("tai.insertAtMention");
    const ins = await until(() => probe.since(m, "insertMention")[0], 5000, "insertMention");
    assert.strictEqual(ins.text, "@mention.ts#L2-3");
  });

  it("explorer 'Add to tai Chat' inserts mentions", async () => {
    const m = probe.mark();
    await vscode.commands.executeCommand("tai.addFileToChat", vscode.Uri.file(path.join(WS, "accept.txt")),
      [vscode.Uri.file(path.join(WS, "accept.txt")), vscode.Uri.file(path.join(WS, "sub dir"))]);
    const ins = await until(() => probe.since(m, "insertMention")[0], 5000, "insertMention");
    assert.strictEqual(ins.text, '@accept.txt @"sub dir"');
  });

  it("finds files for the @ menu", async () => {
    const m = probe.mark();
    await probe.webview({ type: "searchFiles", query: "notes", requestId: 7 });
    const r = await until(() => probe.since(m, "fileResults")[0], 10000, "fileResults");
    assert.strictEqual(r.requestId, 7);
    assert.strictEqual(r.files[0].rel, "sub dir/notes.md");
  });

  it("switches the permission mode", async () => {
    await vscode.commands.executeCommand("tai.cycleMode");
    assert.strictEqual(ext.sidebar.state.mode, "accept-edits");
    await probe.webview({ type: "setMode", mode: "manual" });
    assert.strictEqual(ext.sidebar.state.mode, "manual");
  });

  it("picks a model from the quick pick", async () => {
    const p = ext.sidebar.pickModel();
    await sleep(1500);
    await vscode.commands.executeCommand("workbench.action.quickOpenSelectNext");
    await vscode.commands.executeCommand("workbench.action.acceptSelectedQuickOpenItem");
    await p;
    assert.strictEqual(ext.sidebar.state.model, "mock-small");
    await ext.sidebar.setModel("gpt-5.6-sol");
  });

  it("rewinds code and conversation from the quick pick", async () => {
    await ext.sidebar.newSession();
    const f = path.join(WS, "rewind.txt");
    fs.writeFileSync(f, "v0\n");
    await vscode.commands.executeCommand("tai.cycleMode");      // accept-edits: no approvals
    await probe.send("first message");
    const m = probe.mark();
    await probe.send(mock({ tools: [{ name: "edit_file", args: { path: "rewind.txt", old_string: "v0", new_string: "v1" } }] }, { text: "edited" }));
    assert.strictEqual(fs.readFileSync(f, "utf8"), "v1\n");
    const idx = (probe.since(m, "user")[0] as any).index;
    const p = ext.sidebar.rewind(idx);
    await sleep(800);
    await vscode.commands.executeCommand("workbench.action.acceptSelectedQuickOpenItem");   // "Restore code and conversation"
    await p;
    assert.strictEqual(fs.readFileSync(f, "utf8"), "v0\n");
    const t = [...probe.posted].reverse().find((x) => x.type === "transcript") as any;
    assert.deepStrictEqual(t.items.filter((i: any) => i.type === "user").map((i: any) => i.text), ["first message"]);
    const pre = [...probe.posted].reverse().find((x) => x.type === "prefill") as any;
    assert.match(pre.text, /^MOCK:/);
    await probe.webview({ type: "setMode", mode: "manual" });
  });

  it("loads an earlier conversation and starts new ones", async () => {
    await ext.sidebar.newSession();
    await probe.send("remember the word banana");
    const id = ext.sidebar.state.session.id;
    await vscode.commands.executeCommand("tai.newConversation");
    assert.notStrictEqual(ext.sidebar.state.session.id, id);
    const m = probe.mark();
    await ext.sidebar.loadSession(id);
    const t = probe.since(m, "transcript")[0];
    assert.deepStrictEqual(t.items.map((i: any) => i.type), ["user", "assistant"]);
    assert.strictEqual(ext.sidebar.state.session.id, id);
  });

  it("opens tai in an editor tab with its own process", async () => {
    await vscode.commands.executeCommand("tai.openInNewTab");
    const c: ChatController = await until(() => [...ext.panels.keys()][0], 5000, "panel") as ChatController;
    const pp = new Probe(c);
    await until(() => c.state, 20000, "panel tai");
    await pp.send("tab conversation");
    const panel = ext.panels.get(c);
    await until(() => panel.title.includes("tab conversation"), 5000, "panel title");
    assert.notStrictEqual(c.state!.session.id, ext.sidebar.state.session.id);
    panel.dispose();
    await until(() => ext.panels.size === 0, 5000, "panel disposed");
  });

  it("keeps approvals of two chats apart", async () => {
    await vscode.commands.executeCommand("tai.openInNewTab");
    const b: ChatController = await until(() => [...ext.panels.keys()][0], 5000, "panel") as ChatController;
    const pb = new Probe(b);
    await until(() => b.state, 20000, "panel tai");
    await ext.sidebar.newSession();
    await ext.sidebar.restart();            // fresh process: both number their requests from s1
    await until(() => (ext.sidebar as any).client?.running && ext.sidebar.state, 10000, "restarted");
    fs.writeFileSync(path.join(WS, "same.txt"), "A\n");
    const ma = probe.mark(), mb = pb.mark();
    const edit = (n: string) => mock({ tools: [{ name: "edit_file", args: { path: "same.txt", old_string: "A", new_string: n } }] }, { text: "ok" });
    await probe.webview({ type: "send", text: edit("FROM_SIDEBAR"), clientId: "x1", includeContext: false });
    await until(() => diffTabs().length === 1, 15000, "first diff");
    await pb.webview({ type: "send", text: edit("FROM_TAB"), clientId: "x2", includeContext: false });
    await until(() => diffTabs().length === 2, 15000, "two diffs");
    const apA = probe.since(ma, "approval")[0], apB = pb.since(mb, "approval")[0];
    assert.strictEqual(apA.requestId, apB.requestId, "both processes use the same request id");
    const docs = await Promise.all(diffTabs().map((t) => vscode.workspace.openTextDocument((t.input as vscode.TabInputTextDiff).modified)));
    assert.deepStrictEqual(docs.map((d) => d.getText()).sort(), ["FROM_SIDEBAR\n", "FROM_TAB\n"]);
    const tB = diffTabs()[docs.findIndex((d) => d.getText() === "FROM_TAB\n")];
    await vscode.commands.executeCommand("vscode.diff", (tB.input as vscode.TabInputTextDiff).original, (tB.input as vscode.TabInputTextDiff).modified);
    await vscode.commands.executeCommand("tai.acceptDiff");
    await pb.turnEnd(mb);
    assert.strictEqual(fs.readFileSync(path.join(WS, "same.txt"), "utf8"), "FROM_TAB\n");
    assert.strictEqual(diffTabs().length, 1, "the side bar's diff is still open");
    await probe.webview({ type: "approve", requestId: apA.requestId, decision: "yes" });
    const endA = await probe.turnEnd(ma);
    assert.strictEqual(endA.reason, "done");
    assert.match(probe.since(ma, "tool_end")[0].summary, /changed while waiting/);
    const idB = b.state!.session.id;
    await ext.sidebar.loadSession(idB);
    assert.notStrictEqual(ext.sidebar.state.session.id, idB, "same conversation not opened twice");
    ext.panels.get(b).dispose();
    await until(() => ext.panels.size === 0, 5000, "panel closed");
  });

  it("/compact while busy reports back instead of hanging", async () => {
    const m = probe.mark();
    await probe.webview({ type: "send", text: mock({ text: "x", hang: 20 }), clientId: "cb", includeContext: false });
    await until(() => ext.sidebar.busy, 5000, "busy");
    await probe.webview({ type: "command", name: "compact", arg: "" });
    const e = probe.since(m, "compact_end")[0];
    assert.ok(e && /Stop the current task/.test(e.error));
    await probe.webview({ type: "cancel" });
    await probe.turnEnd(m);
    assert.strictEqual(ext.sidebar.busy, false);
  });

  it("recovers when the tai process dies", async () => {
    const client = (ext.sidebar as any).client;
    const id = ext.sidebar.state.session.id;
    const m = probe.mark();
    (client as any).proc.kill("SIGKILL");
    await until(() => probe.since(m, "status").find((s: any) => s.status === "stopped"), 5000, "stopped status");
    const end = await probe.send("are you back?");
    assert.strictEqual(end.reason, "done", end.error);
    assert.strictEqual(ext.sidebar.state.session.id, id, "same conversation after restart");
  });

  it("reports a missing tai program clearly", async () => {
    const cfg = vscode.workspace.getConfiguration("tai");
    await cfg.update("executablePath", "/nonexistent/tai", vscode.ConfigurationTarget.Workspace);
    try {
      const m = probe.mark();
      await ext.sidebar.restart();
      const st = await until(() => probe.since(m, "status").find((s: any) => s.status === "error"), 10000, "error status");
      assert.match(st.message, /Could not find the tai program/);
    } finally {
      await cfg.update("executablePath", undefined, vscode.ConfigurationTarget.Workspace);
      await ext.sidebar.restart();
      await until(() => (ext.sidebar as any).client?.running, 10000, "restarted");
    }
  });

  it("opens the conversation in a terminal", async () => {
    await vscode.commands.executeCommand("tai.openInTerminal");
    const term = await until(() => vscode.window.terminals.find((t) => t.name === "tai"), 5000, "terminal");
    term.dispose();
  });

  it("shows /usage and toggles tools", async () => {
    const m = probe.mark();
    await probe.webview({ type: "command", name: "usage", arg: "" });
    assert.match(probe.since(m, "notice")[0].text, /This session: .* in · .* out/);
    await probe.webview({ type: "command", name: "tools", arg: "" });
    assert.strictEqual(ext.sidebar.state.tools, false);
    await probe.webview({ type: "command", name: "tools", arg: "" });
    assert.strictEqual(ext.sidebar.state.tools, true);
  });

  it("compacts the conversation", async () => {
    await probe.send("something to compact");
    const m = probe.mark();
    await probe.webview({ type: "command", name: "compact", arg: "" });
    const e = await until(() => probe.since(m, "compact_end")[0], 15000, "compact_end");
    assert.strictEqual(e.error, null);
    assert.strictEqual(ext.sidebar.busy, false);
  });

  it("exports the conversation with /save", async () => {
    await probe.webview({ type: "command", name: "save", arg: "export.md" });
    assert.match(fs.readFileSync(path.join(WS, "export.md"), "utf8"), /## You/);
  });
});

// Optional: screenshots of the real VS Code window (set TAI_SHOTS to a folder; needs ImageMagick `import`).
describe("screenshots", function () {
  const dir = process.env.TAI_SHOTS;
  before(function () {
    if (!dir) this.skip();
  });
  const shot = (name: string) =>
    require("child_process").execSync(`import -window root ${JSON.stringify(path.join(dir!, name + ".png"))}`);

  it("captures the chat with a pending edit", async () => {
    fs.mkdirSync(dir!, { recursive: true });
    await vscode.commands.executeCommand("workbench.action.closeAllEditors");
    await ext.sidebar.newSession();
    const f = path.join(WS, "analysis.py");
    fs.writeFileSync(f, "import pandas as pd\n\ndef load(path):\n    df = pd.read_csv(path)\n    return df.dropna()\n\nprint(load('data.csv').describe())\n");
    await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(f));
    await probe.send(mock({ text: "Let me look at the script first.", tools: [{ name: "read_file", args: { path: "analysis.py" } }] },
      { text: "The script drops **all** rows with any missing value. I'll only drop rows where `income` is missing:" ,
        tools: [{ name: "edit_file", args: { path: "analysis.py", old_string: "    return df.dropna()", new_string: "    # keep rows with partial data; income is required\n    return df.dropna(subset=[\"income\"])" } }] },
      { text: "Done. `analysis.py:5` now keeps rows that only miss other columns." }), false, 3000).catch(() => undefined);
    await until(() => diffTabs().length === 1, 15000, "diff tab");
    await sleep(1500);
    shot("vscode-approval");
    const ap = [...probe.posted].reverse().find((x) => x.type === "approval") as any;
    await probe.webview({ type: "approve", requestId: ap.requestId, decision: "yes" });
    await until(() => !ext.sidebar.busy, 15000, "idle");
    await sleep(1200);
    shot("vscode-done");
  });
});

// Opt-in end-to-end run against the REAL Tilburg.ai API (TAI_REAL=1; uses your key, costs a few cents).
describe("real API", function () {
  before(function () {
    if (!process.env.TAI_REAL) this.skip();
  });
  this.timeout(240000);

  it("creates and runs a file in auto mode", async () => {
    await ext.sidebar.newSession();
    await probe.webview({ type: "setMode", mode: "auto" });
    const end = await probe.send("Create greet.py with a function greet(name) that returns 'Hello, ' + name + '!' and " +
      "prints greet('Tilburg') when run. Then run it with python3 and tell me the exact output.", false, 200000);
    assert.strictEqual(end.reason, "done", end.error);
    assert.match(fs.readFileSync(path.join(WS, "greet.py"), "utf8"), /def greet/);
    const tools = probe.events("tool_start").map((e: any) => e.name);
    assert.ok(tools.includes("write_file") && tools.includes("bash"), tools.join(","));
    const text = probe.events("text").map((e: any) => e.delta).join("");
    assert.match(text, /Hello, Tilburg!/);
  });

  it("asks before editing in manual mode; accepted from the diff editor", async () => {
    await probe.webview({ type: "setMode", mode: "manual" });
    const m = probe.mark();
    await probe.webview({ type: "send", text: "In greet.py change the greeting word from Hello to Hi. Only that change; don't run anything.", clientId: "real2", includeContext: false });
    const tab = await until(() => diffTabs()[0], 200000, "diff tab");
    await vscode.commands.executeCommand("vscode.diff", (tab.input as vscode.TabInputTextDiff).original,
      (tab.input as vscode.TabInputTextDiff).modified);
    await vscode.commands.executeCommand("tai.acceptDiff");
    const end = await probe.turnEnd(m, 200000);
    assert.strictEqual(end.reason, "done", end.error);
    assert.match(fs.readFileSync(path.join(WS, "greet.py"), "utf8"), /Hi, /);
  });

  it("runs sub-agents", async () => {
    await probe.webview({ type: "setMode", mode: "auto" });
    const m = probe.mark();
    const end = await probe.send("Use the task tool to start two sub-agents in parallel: one reports how many lines greet.py has, " +
      "the other lists the files in this folder. Then summarize both reports in two bullet points.", false, 220000);
    assert.strictEqual(end.reason, "done", end.error);
    assert.ok(probe.since(m, "task_end").length >= 2, "two sub-agents finished");
  });

  it("answers /btw and can be stopped mid-answer", async () => {
    const m = probe.mark();
    await probe.webview({ type: "btw", question: "In one word: what language is greet.py written in?", id: "rb" });
    const b = await until(() => probe.since(m, "btw_end")[0], 120000, "btw_end");
    assert.strictEqual(b.error, null);
    assert.match(probe.since(m, "btw_text").map((e: any) => e.delta).join(""), /python/i);
    const m2 = probe.mark();
    await probe.webview({ type: "send", text: "Write a 1500-word essay about the history of Tilburg.", clientId: "rc", includeContext: false });
    await until(() => probe.since(m2, "turn_start").length, 30000, "turn start");
    await sleep(5000);                       // thinking or streaming: either way Stop must work at once
    await probe.webview({ type: "cancel" });
    const t = Date.now();
    const end = await probe.turnEnd(m2, 10000);
    assert.strictEqual(end.reason, "cancelled");
    assert.ok(Date.now() - t < 2000, "stopped quickly");
  });
});
