import * as path from "path";
import * as vscode from "vscode";
import type { Decision } from "./protocol";

export const PROPOSED = "tai-proposed";   // right side of an approval diff (gets Accept/Reject buttons)
export const ORIGINAL = "tai-original";   // left side
export const VIEW = "tai-view";           // both sides of a read-only "show change" diff

interface Proposal {
  decide: (d: Decision) => void;
  uris: string[];
}

/** Serves diff contents from memory and maps diff tabs back to pending approvals. */
export class DiffManager implements vscode.TextDocumentContentProvider, vscode.Disposable {
  private contents = new Map<string, string>();
  private proposals = new Map<string, Proposal>();
  private emitter = new vscode.EventEmitter<vscode.Uri>();
  readonly onDidChange = this.emitter.event;
  private subs: vscode.Disposable[] = [];
  private n = 0;

  constructor() {
    for (const s of [PROPOSED, ORIGINAL, VIEW]) {
      this.subs.push(vscode.workspace.registerTextDocumentContentProvider(s, this));
    }
    // forget contents of read-only diffs once their tab is closed
    this.subs.push(vscode.window.tabGroups.onDidChangeTabs((e) => {
      for (const t of e.closed) {
        if (t.input instanceof vscode.TabInputTextDiff) {
          for (const u of [t.input.original, t.input.modified]) {
            if (u.scheme === VIEW) this.contents.delete(u.toString());
          }
        }
      }
    }));
  }

  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.contents.get(uri.toString()) ?? "";
  }

  private uri(scheme: string, key: string, file: string): vscode.Uri {
    // the file name keeps syntax highlighting; the query keeps URIs unique
    return vscode.Uri.from({ scheme, path: "/" + path.basename(file), query: key });
  }

  /** Opens the side-by-side diff for an approval; `decide` is called by the Accept/Reject buttons. */
  async openProposal(requestId: string, file: string, oldText: string | null, newText: string,
    decide: (d: Decision) => void, preserveFocus = true): Promise<void> {
    const left = this.uri(ORIGINAL, requestId, file);
    const right = this.uri(PROPOSED, requestId, file);
    this.contents.set(left.toString(), oldText ?? "");
    this.contents.set(right.toString(), newText);
    this.proposals.set(requestId, { decide, uris: [left.toString(), right.toString()] });
    const title = `${path.basename(file)} ${oldText === null ? "(new file)" : ""} ↔ tai's proposal`.replace("  ", " ");
    await vscode.commands.executeCommand("vscode.diff", left, right, title,
      { preview: false, preserveFocus, viewColumn: vscode.ViewColumn.Active });
  }

  /** Opens a read-only diff of a change tai already made. */
  async openView(file: string, oldText: string | null, newText: string): Promise<void> {
    const key = `v${++this.n}`;
    const left = this.uri(VIEW, key + "a", file);
    const right = this.uri(VIEW, key + "b", file);
    this.contents.set(left.toString(), oldText ?? "");
    this.contents.set(right.toString(), newText);
    await vscode.commands.executeCommand("vscode.diff", left, right,
      `${path.basename(file)} (tai's change)`, { preview: true });
  }

  hasProposal(requestId: string): boolean {
    return this.proposals.has(requestId);
  }

  /** Accept/Reject from the editor title: finds the approval behind the active diff tab. */
  decideActive(d: Decision): boolean {
    const tab = vscode.window.tabGroups.activeTabGroup.activeTab;
    let uri: vscode.Uri | undefined;
    if (tab?.input instanceof vscode.TabInputTextDiff) uri = tab.input.modified;
    else uri = vscode.window.activeTextEditor?.document.uri;
    if (!uri || uri.scheme !== PROPOSED) return false;
    const p = this.proposals.get(uri.query);
    if (!p) return false;
    p.decide(d);
    return true;
  }

  /** The approval was answered or withdrawn: drop it and close its diff tab. */
  async close(requestId: string): Promise<void> {
    const p = this.proposals.get(requestId);
    if (!p) return;
    this.proposals.delete(requestId);
    const tabs = vscode.window.tabGroups.all.flatMap((g) => g.tabs).filter((t) =>
      t.input instanceof vscode.TabInputTextDiff && p.uris.includes(t.input.modified.toString()));
    try {
      if (tabs.length) await vscode.window.tabGroups.close(tabs, true);
    } catch { /* tab already gone */ }
    for (const u of p.uris) this.contents.delete(u);
  }

  dispose() {
    this.subs.forEach((s) => s.dispose());
    this.emitter.dispose();
  }
}
