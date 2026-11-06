import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import type { Attachment, EditorContext, FileHit } from "./protocol";

const MAX_SELECTION = 20000;

/** Selection → 1-based inclusive line range; a selection ending at column 0 excludes that line. */
export function selectionLines(sel: vscode.Selection): { start: number; end: number } {
  let end = sel.end.line;
  if (sel.end.character === 0 && end > sel.start.line) end -= 1;
  return { start: sel.start.line + 1, end: end + 1 };
}

export function relPath(cwd: string, abs: string): string {
  const r = path.relative(cwd, abs);
  return r && !r.startsWith("..") && !path.isAbsolute(r) ? r.split(path.sep).join("/") : abs;
}

/** The mention token for a path: @rel, @"rel with spaces", optional #L5-9. */
export function mentionFor(rel: string, range?: { start: number; end: number }): string {
  const p = /\s/.test(rel) ? `"${rel}"` : rel;
  const r = range ? (range.start === range.end ? `#L${range.start}` : `#L${range.start}-${range.end}`) : "";
  return `@${p}${r}`;
}

/** Finds @-mentions in a message that name existing files or folders. */
export function parseMentions(text: string, cwd: string): Attachment[] {
  const out: Attachment[] = [];
  const seen = new Set<string>();
  const re = /(^|[\s(\[{,;])@(?:"([^"]+)"|([^\s"'`)\]},;]+))(?:#L(\d+)(?:-(\d+))?)?/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    let p = (m[2] ?? m[3] ?? "").replace(/[.:!?]+$/, "");
    if (!p) continue;
    if (p.startsWith("~")) p = path.join(process.env.HOME ?? "", p.slice(1));
    const abs = path.resolve(cwd, p);
    if (!fs.existsSync(abs)) continue;
    const a: Attachment = { path: abs };
    if (m[4]) {
      const s = parseInt(m[4], 10);
      const e = m[5] ? parseInt(m[5], 10) : s;
      a.range = [Math.min(s, e), Math.max(s, e)];
    }
    const key = abs + (a.range ? `#${a.range}` : "");
    if (!seen.has(key)) {
      seen.add(key);
      out.push(a);
    }
  }
  return out;
}

/** Tracks the file open in the editor and its selection (ignores the chat, output panes etc.). */
export class EditorTracker implements vscode.Disposable {
  private editor?: vscode.TextEditor;
  private listeners = new Set<() => void>();
  private subs: vscode.Disposable[] = [];
  private timer?: NodeJS.Timeout;

  constructor() {
    this.take(vscode.window.activeTextEditor);
    this.subs.push(
      vscode.window.onDidChangeActiveTextEditor((e) => {
        if (e) this.take(e);
        else if (this.editor && !vscode.window.visibleTextEditors.includes(this.editor)) {
          this.editor = undefined;
          this.changed();
        }
      }),
      vscode.window.onDidChangeTextEditorSelection((e) => {
        if (e.textEditor === this.editor || this.take(e.textEditor)) this.changed();
      }),
      vscode.window.onDidChangeVisibleTextEditors((eds) => {
        if (this.editor && !eds.includes(this.editor)) {
          this.editor = undefined;
          this.take(vscode.window.activeTextEditor);
          this.changed();
        }
      }),
      vscode.workspace.onDidCloseTextDocument((d) => {
        if (this.editor?.document === d) {
          this.editor = undefined;
          this.changed();
        }
      }),
    );
  }

  private take(e: vscode.TextEditor | undefined): boolean {
    if (!e || e.document.uri.scheme !== "file") return false;
    if (e !== this.editor) {
      this.editor = e;
      this.changed();
    }
    return true;
  }

  private changed() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => this.listeners.forEach((l) => l()), 80);
  }

  onChange(l: () => void): vscode.Disposable {
    this.listeners.add(l);
    return { dispose: () => this.listeners.delete(l) };
  }

  get activeEditor(): vscode.TextEditor | undefined {
    return this.editor;
  }

  context(cwd: string): EditorContext | null {
    const e = this.editor;
    if (!e || e.document.isClosed) return null;
    const abs = e.document.uri.fsPath;
    const ctx: EditorContext = { path: abs, rel: relPath(cwd, abs) };
    const sel = e.selection;
    if (!sel.isEmpty) {
      const { start, end } = selectionLines(sel);
      ctx.selection = { start, end, text: e.document.getText(sel).slice(0, MAX_SELECTION) };
    }
    return ctx;
  }

  dispose() {
    if (this.timer) clearTimeout(this.timer);
    this.subs.forEach((s) => s.dispose());
    this.listeners.clear();
  }
}

/** Fuzzy score of `query` against `target` (higher is better; -1 = no match). */
export function fuzzyScore(query: string, target: string): number {
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  if (!q) return 0;
  const base = t.slice(t.lastIndexOf("/") + 1);
  if (base === q) return 1000;
  if (base.startsWith(q)) return 800 - base.length;
  const bi = base.indexOf(q);
  if (bi >= 0) return 600 - bi - base.length / 10;
  const ti = t.indexOf(q);
  if (ti >= 0) return 400 - ti / 10 - t.length / 100;
  // subsequence match, rewarding consecutive characters and starts of path segments
  let score = 0, ti2 = 0, run = 0;
  for (const ch of q) {
    const found = t.indexOf(ch, ti2);
    if (found < 0) return -1;
    run = found === ti2 ? run + 1 : 0;
    score += 2 + run * 3 + (found === 0 || "/._-".includes(t[found - 1]) ? 4 : 0);
    ti2 = found + 1;
  }
  return Math.min(score, 300) - t.length / 50;
}

const EXCLUDE = "{**/node_modules/**,**/.git/**,**/__pycache__/**,**/.venv/**,**/venv/**,**/.mypy_cache/**,**/.pytest_cache/**,**/.ipynb_checkpoints/**,**/.Rproj.user/**,**/dist/**,**/out/**,**/.next/**,**/target/**}";

/** Cached list of workspace files for @-mention completion. */
export class FileIndex implements vscode.Disposable {
  private files?: Promise<vscode.Uri[]>;
  private watcher?: vscode.FileSystemWatcher;
  private timer?: NodeJS.Timeout;

  constructor() {
    if (vscode.workspace.workspaceFolders?.length) {
      this.watcher = vscode.workspace.createFileSystemWatcher("**/*", false, true, false);
      const inval = () => {
        if (this.timer) clearTimeout(this.timer);
        this.timer = setTimeout(() => (this.files = undefined), 500);
      };
      this.watcher.onDidCreate(inval);
      this.watcher.onDidDelete(inval);
    }
  }

  private list(): Promise<vscode.Uri[]> {
    if (!this.files) {
      this.files = Promise.resolve(vscode.workspace.findFiles("**/*", EXCLUDE, 30000)).then(
        (r) => r, () => [] as vscode.Uri[]);
    }
    return this.files;
  }

  async search(query: string, cwd: string, limit = 40): Promise<FileHit[]> {
    const uris = await this.list();
    const hits = new Map<string, FileHit>();
    for (const u of uris) {
      const rel = relPath(cwd, u.fsPath);
      if (path.isAbsolute(rel)) continue;
      hits.set(rel, { path: u.fsPath, rel, dir: false });
      let d = path.posix.dirname(rel);
      while (d && d !== "." && !hits.has(d + "/")) {
        hits.set(d + "/", { path: path.join(cwd, d), rel: d + "/", dir: true });
        d = path.posix.dirname(d);
      }
    }
    if (!uris.length && !vscode.workspace.workspaceFolders?.length) {
      // no folder open: offer the entries of cwd
      try {
        for (const e of fs.readdirSync(cwd, { withFileTypes: true }).slice(0, 2000)) {
          if (e.name.startsWith(".")) continue;
          const rel = e.isDirectory() ? e.name + "/" : e.name;
          hits.set(rel, { path: path.join(cwd, e.name), rel, dir: e.isDirectory() });
        }
      } catch { /* ignore */ }
    }
    const q = query.replace(/^["']/, "");
    if (!q) {
      const open = vscode.window.tabGroups.all.flatMap((g) => g.tabs)
        .map((t) => (t.input instanceof vscode.TabInputText ? t.input.uri : undefined))
        .filter((u): u is vscode.Uri => !!u && u.scheme === "file")
        .map((u) => relPath(cwd, u.fsPath)).filter((r) => hits.has(r));
      const rest = [...hits.values()].filter((h) => !h.dir && !open.includes(h.rel))
        .sort((a, b) => a.rel.split("/").length - b.rel.split("/").length || a.rel.localeCompare(b.rel));
      return [...new Set(open)].map((r) => hits.get(r)!).concat(rest).slice(0, limit);
    }
    const scored: [number, FileHit][] = [];
    for (const h of hits.values()) {
      const s = fuzzyScore(q, h.dir ? h.rel.slice(0, -1) : h.rel);
      if (s >= 0) scored.push([s - (h.dir ? 5 : 0), h]);
    }
    scored.sort((a, b) => b[0] - a[0] || a[1].rel.localeCompare(b[1].rel));
    return scored.slice(0, limit).map((x) => x[1]);
  }

  dispose() {
    if (this.timer) clearTimeout(this.timer);
    this.watcher?.dispose();
  }
}
