import { ChildProcess, spawn } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as readline from "readline";
import type { TaiEvent } from "./protocol";

export const PROTOCOL_VERSION = 1;

export interface ClientOptions {
  executable: string;           // path to tai
  python?: string;              // optional interpreter
  cwd: string;
  env: NodeJS.ProcessEnv;
  args?: string[];
  log?: (line: string) => void;
}

type Pending = { resolve: (v: any) => void; reject: (e: Error) => void; timer?: NodeJS.Timeout };
export type RequestHandler = (method: string, params: any, id: string) => Promise<unknown>;

/** Finds tai: explicit setting, then PATH, then ~/.local/bin/tai. */
export function findExecutable(setting: string | undefined, envPath = process.env.PATH ?? ""): string | undefined {
  const expand = (p: string) => (p.startsWith("~") ? path.join(os.homedir(), p.slice(1)) : p);
  if (setting && setting.trim()) {
    const p = expand(setting.trim());
    return fs.existsSync(p) ? p : undefined;
  }
  for (const dir of envPath.split(path.delimiter)) {
    if (!dir) continue;
    const p = path.join(expand(dir), "tai");
    try {
      fs.accessSync(p, fs.constants.X_OK);
      if (fs.statSync(p).isFile()) return p;
    } catch { /* keep looking */ }
  }
  const fallback = path.join(os.homedir(), ".local", "bin", "tai");
  return fs.existsSync(fallback) ? fallback : undefined;
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
  private readyPromise?: Promise<{ version: string; protocol: number }>;

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
  start(timeoutMs = 20000): Promise<{ version: string; protocol: number }> {
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
        proc = spawn(cmd, argv, { cwd, env: { ...env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" },
          stdio: ["pipe", "pipe", "pipe"] });
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
            resolve({ version: ev.version, protocol: ev.protocol });
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
