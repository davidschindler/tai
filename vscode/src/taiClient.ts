import { ChildProcess, spawn, spawnSync } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as readline from "readline";
import type { TaiEvent } from "./protocol";

export const PROTOCOL_VERSION = 1;

export interface ClientOptions {
  executable: string;           // path to tai (or tai.py)
  python?: string;              // interpreter to run it with
  cwd: string;
  env: NodeJS.ProcessEnv;
  args?: string[];
  log?: (line: string) => void;
}

type Pending = { resolve: (v: any) => void; reject: (e: Error) => void; timer?: NodeJS.Timeout };
export type RequestHandler = (method: string, params: any, id: string) => Promise<unknown>;

const WIN = process.platform === "win32";
const expandHome = (p: string) => (p.startsWith("~") ? path.join(os.homedir(), p.slice(1)) : p);

/** How to run tai: `python executable …` when python is set, otherwise `executable …`. */
export interface TaiCommand {
  executable: string;
  python?: string;
  source: "setting" | "bundled" | "installed";
}

/** A working Python ≥ 3.9: the setting, else the usual names (on Windows the `py` launcher first;
 *  the Microsoft Store's `python3` stub prints nothing and is rejected by the version check). */
const pythonCache = new Map<string, string | undefined>();
export function findPython(setting?: string): string | undefined {
  const key = (setting ?? "").trim();
  if (pythonCache.has(key)) return pythonCache.get(key);
  const cands = key ? [expandHome(key)] : WIN ? ["py", "python", "python3"] : ["python3", "python"];
  let found: string | undefined;
  for (const c of cands) {
    try {
      // returns the real interpreter path (so the `py` launcher is not left in between)
      const r = spawnSync(c, ["-c", "import sys; print(sys.version_info >= (3, 9), sys.executable)"],
        { encoding: "utf8", timeout: 15000, windowsHide: true });
      const m = /^True (.+)$/.exec((r.stdout ?? "").trim());
      if (r.status === 0 && m) { found = m[1].trim() || c; break; }
    } catch { /* try the next */ }
  }
  if (found) pythonCache.set(key, found);      // a failure is retried next time (Python may get installed)
  return found;
}

/** `tai` installed on PATH (pipx/pip: tai.exe on Windows), else ~/.local/bin/tai. */
export function findInstalled(envPath = process.env.PATH ?? ""): string | undefined {
  const names = WIN ? ["tai.exe"] : ["tai"];
  for (const dir of envPath.split(path.delimiter)) {
    if (!dir) continue;
    for (const n of names) {
      const p = path.join(expandHome(dir), n);
      try {
        if (!WIN) fs.accessSync(p, fs.constants.X_OK);
        if (fs.statSync(p).isFile()) return p;
      } catch { /* keep looking */ }
    }
  }
  const fallback = path.join(os.homedir(), ".local", "bin", WIN ? "tai.exe" : "tai");
  return fs.existsSync(fallback) ? fallback : undefined;
}

/**
 * Finds tai. Order: the `tai.executablePath` setting; the copy bundled with the extension (always
 * the matching protocol; needs only Python, no packages); an installed `tai`.
 * `preferInstalled` (for the terminal, where the installed one has the nicer interface) swaps the last two.
 */
export function findTai(o: { setting?: string; pythonSetting?: string; bundled?: string; envPath?: string;
  preferInstalled?: boolean }): TaiCommand | undefined {
  const py = () => findPython(o.pythonSetting);
  if (o.pythonSetting && o.pythonSetting.trim() && !py()) return undefined;   // a configured Python must work
  if (o.setting && o.setting.trim()) {
    const p = expandHome(o.setting.trim());
    if (!fs.existsSync(p)) return undefined;
    // a .py file (or any file when a Python is configured) runs through Python
    if (p.endsWith(".py") || (o.pythonSetting && o.pythonSetting.trim())) {
      const python = py();
      return python ? { executable: p, python, source: "setting" } : undefined;
    }
    return { executable: p, source: "setting" };
  }
  const bundled = () => {
    if (!o.bundled || !fs.existsSync(o.bundled)) return undefined;
    const python = py();
    return python ? { executable: o.bundled, python, source: "bundled" as const } : undefined;
  };
  const installed = () => {
    const p = findInstalled(o.envPath);
    if (!p) return undefined;
    // a configured interpreter runs a script-style tai (not a Windows .exe launcher)
    const python = o.pythonSetting && o.pythonSetting.trim() && !p.endsWith(".exe") ? py() : undefined;
    return { executable: p, python, source: "installed" as const };
  };
  return o.preferInstalled ? installed() ?? bundled() : bundled() ?? installed();
}

/**
 * One `tai --serve` process. Requests are JSON lines; replies resolve promises; events and
 * server-initiated requests go to the registered handlers.
 */
export class TaiClient {
  private proc?: ChildProcess;
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private eventHandlers: ((e: TaiEvent) => void)[] = [];
  private exitHandlers: ((code: number | null, stderr: string) => void)[] = [];
  private requestHandler?: RequestHandler;
  private stderrTail = "";
  private exited = false;
  private readyPromise?: Promise<{ version: string; protocol: number; hasKey?: boolean }>;

  constructor(private opts: ClientOptions) {}

  get running(): boolean {
    return !!this.proc && !this.exited;
  }

  onEvent(h: (e: TaiEvent) => void): void {
    this.eventHandlers.push(h);
  }

  onExit(h: (code: number | null, stderr: string) => void): void {
    this.exitHandlers.push(h);
  }

  onRequest(h: RequestHandler): void {
    this.requestHandler = h;
  }

  /** Spawn the process and wait for its "ready" event. */
  start(timeoutMs = 20000): Promise<{ version: string; protocol: number; hasKey?: boolean }> {
    if (this.readyPromise) return this.readyPromise;
    const { executable, python, cwd, env, args = [] } = this.opts;
    const cmd = python ? python : executable;
    const argv = python ? [executable, "--serve", ...args] : ["--serve", ...args];
    this.readyPromise = new Promise((resolve, reject) => {
      let settled = false;
      const fail = (msg: string) => {
        if (!settled) { settled = true; reject(new Error(msg)); }
      };
      const timer = setTimeout(() => fail(`tai did not start within ${timeoutMs / 1000}s`), timeoutMs);
      let proc: ChildProcess;
      try {
        proc = spawn(cmd, argv, { cwd, env: { ...env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
          stdio: ["pipe", "pipe", "pipe"], windowsHide: true });
      } catch (e: any) {
        clearTimeout(timer);
        fail(`could not start ${cmd}: ${e.message}`);
        return;
      }
      this.proc = proc;
      proc.on("error", (e: NodeJS.ErrnoException) => {
        clearTimeout(timer);
        this.exited = true;
        fail(e.code === "ENOENT" ? `not found: ${cmd}` : e.code === "EACCES" ? `not executable: ${cmd}` : e.message);
      });
      proc.stdin!.on("error", () => { /* process gone; handled by exit */ });
      proc.stderr!.setEncoding("utf8");
      proc.stderr!.on("data", (d: string) => {
        this.stderrTail = (this.stderrTail + d).slice(-4000);
        for (const line of d.split("\n")) if (line.trim()) this.opts.log?.(`[tai] ${line}`);
      });
      const rl = readline.createInterface({ input: proc.stdout!, crlfDelay: Infinity });
      rl.on("line", (line) => {
        if (!line.trim()) return;
        let msg: any;
        try {
          msg = JSON.parse(line);
        } catch {
          this.opts.log?.(`[stdout] ${line}`);
          return;
        }
        if (msg.method === "event") {
          const ev = msg.params as TaiEvent;
          if (ev.type === "ready" && !settled) {
            clearTimeout(timer);
            if (ev.protocol !== PROTOCOL_VERSION) {
              fail(`tai speaks protocol ${ev.protocol}, this extension needs ${PROTOCOL_VERSION}; update tai or the extension`);
              this.kill();
              return;
            }
            settled = true;
            resolve({ version: ev.version, protocol: ev.protocol, hasKey: ev.hasKey });
          }
          for (const h of this.eventHandlers) {
            try { h(ev); } catch (e: any) { this.opts.log?.(`event handler failed: ${e?.stack ?? e}`); }
          }
        } else if (msg.method) {
          this.handleServerRequest(msg);
        } else if (typeof msg.id === "number") {
          const p = this.pending.get(msg.id);
          if (!p) return;
          this.pending.delete(msg.id);
          if (p.timer) clearTimeout(p.timer);
          if (msg.error) p.reject(new Error(msg.error.message ?? String(msg.error)));
          else p.resolve(msg.result);
        }
      });
      proc.on("exit", (code) => {
        clearTimeout(timer);
        this.exited = true;
        const tail = this.stderrTail.trim();
        if (/unrecognized arguments: --serve/.test(tail)) {
          fail("this version of tai has no VS Code support (--serve); update ~/.local/bin/tai");
        } else {
          fail(`tai exited (code ${code})${tail ? ": " + tail.split("\n").slice(-3).join(" ") : ""}`);
        }
        for (const p of this.pending.values()) {
          if (p.timer) clearTimeout(p.timer);
          p.reject(new Error("tai stopped"));
        }
        this.pending.clear();
        for (const h of this.exitHandlers) h(code, tail);
      });
    });
    return this.readyPromise;
  }

  private async handleServerRequest(msg: { id: string; method: string; params: any }) {
    let result: unknown = null;
    let error: string | undefined;
    try {
      if (!this.requestHandler) throw new Error("no handler");
      result = await this.requestHandler(msg.method, msg.params, msg.id);
    } catch (e: any) {
      error = e?.message ?? String(e);
    }
    this.write(error !== undefined ? { id: msg.id, error: { message: error } } : { id: msg.id, result: result ?? null });
  }

  private write(obj: unknown): boolean {
    if (!this.proc || this.exited || !this.proc.stdin?.writable) return false;
    this.proc.stdin.write(JSON.stringify(obj) + "\n");
    return true;
  }

  call<T = any>(method: string, params: object = {}, timeoutMs = 0): Promise<T> {
    const id = this.nextId++;
    return new Promise<T>((resolve, reject) => {
      if (!this.write({ id, method, params })) {
        reject(new Error("tai is not running"));
        return;
      }
      const p: Pending = { resolve, reject };
      if (timeoutMs) {
        p.timer = setTimeout(() => {
          this.pending.delete(id);
          reject(new Error(`tai did not answer ${method} in time`));
        }, timeoutMs);
      }
      this.pending.set(id, p);
    });
  }

  kill(): void {
    if (this.proc && !this.exited) {
      try { this.proc.kill("SIGTERM"); } catch { /* already gone */ }
    }
  }

  /** Ask tai to shut down (it stops running work and saves), then make sure it is gone. */
  async dispose(): Promise<void> {
    if (!this.proc || this.exited) return;
    const proc = this.proc;
    const gone = new Promise<void>((r) => proc.once("exit", () => r()));
    try { proc.stdin?.end(); } catch { /* ignore */ }
    const t = setTimeout(() => this.kill(), 6000);
    await Promise.race([gone, new Promise((r) => setTimeout(r, 7000))]);
    clearTimeout(t);
  }
}
