// Loads the real webview bundle in headless Chrome and connects it to a real `tai --serve`
// (talking to the mock API) through the real TaiClient. A tiny stand-in for ChatController
// forwards messages the same way the extension does.
import { spawn } from "child_process";
import fs from "fs";
import os from "os";
import path from "path";
import { createRequire } from "module";
import puppeteer from "puppeteer-core";

const require = createRequire(import.meta.url);
const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../..");
const { TaiClient } = require(path.join(ROOT, "dist/test/taiClient.js"));
export const TAI = process.env.TAI_BIN || path.join(os.homedir(), ".local/bin/tai");

export const THEMES = {
  dark: {
    cls: "vscode-dark",
    vars: {
      "--vscode-foreground": "#cccccc", "--vscode-editor-background": "#1f1f1f", "--vscode-sideBar-background": "#181818",
      "--vscode-input-background": "#313131", "--vscode-input-border": "#3c3c3c", "--vscode-focusBorder": "#0078d4",
      "--vscode-descriptionForeground": "#9d9d9d", "--vscode-textLink-foreground": "#4daafc", "--vscode-widget-border": "#313131",
      "--vscode-textCodeBlock-background": "#2b2b2b", "--vscode-font-family": "system-ui, Ubuntu, sans-serif",
      "--vscode-font-size": "13px", "--vscode-editor-font-family": "'Droid Sans Mono', monospace", "--vscode-editor-font-size": "13px",
      "--vscode-list-hoverBackground": "#2a2d2e", "--vscode-errorForeground": "#f85149", "--vscode-input-foreground": "#cccccc",
      "--vscode-input-placeholderForeground": "#989898", "--vscode-editorWidget-background": "#202020",
      "--vscode-quickInput-background": "#222222", "--vscode-quickInputList-focusBackground": "#04395e",
      "--vscode-quickInputList-focusForeground": "#ffffff",
      "--vscode-diffEditor-insertedLineBackground": "rgba(155,185,85,.2)", "--vscode-diffEditor-removedLineBackground": "rgba(255,0,0,.2)",
    },
  },
  light: {
    cls: "vscode-light",
    vars: {
      "--vscode-foreground": "#3b3b3b", "--vscode-editor-background": "#ffffff", "--vscode-sideBar-background": "#f8f8f8",
      "--vscode-input-background": "#ffffff", "--vscode-input-border": "#cecece", "--vscode-focusBorder": "#005fb8",
      "--vscode-descriptionForeground": "#616161", "--vscode-textLink-foreground": "#005fb8", "--vscode-widget-border": "#e5e5e5",
      "--vscode-textCodeBlock-background": "#f2f2f2", "--vscode-font-family": "system-ui, Ubuntu, sans-serif",
      "--vscode-font-size": "13px", "--vscode-editor-font-family": "'Droid Sans Mono', monospace", "--vscode-editor-font-size": "13px",
      "--vscode-list-hoverBackground": "#f2f2f2", "--vscode-errorForeground": "#f85149", "--vscode-input-foreground": "#3b3b3b",
      "--vscode-input-placeholderForeground": "#767676", "--vscode-editorWidget-background": "#f8f8f8",
      "--vscode-quickInput-background": "#f8f8f8", "--vscode-quickInputList-focusBackground": "#e8e8e8",
      "--vscode-diffEditor-insertedLineBackground": "rgba(155,185,85,.25)", "--vscode-diffEditor-removedLineBackground": "rgba(255,0,0,.15)",
    },
  },
};

export async function startMock() {
  const p = spawn("python3", [path.join(ROOT, "test/mock_api.py")], { stdio: ["ignore", "pipe", "inherit"] });
  const port = await new Promise((resolve, reject) => {
    p.stdout.once("data", (d) => {
      const m = /PORT (\d+)/.exec(String(d));
      m ? resolve(m[1]) : reject(new Error("mock did not start"));
    });
  });
  return { url: `http://127.0.0.1:${port}/v1`, stop: () => p.kill() };
}

export function mock(...steps) {
  return "MOCK: " + JSON.stringify(steps);
}

export class Harness {
  constructor(browser, api, { theme = "dark", width = 420, height = 820, cwd } = {}) {
    this.browser = browser;
    this.api = api;
    this.theme = theme;
    this.size = { width, height };
    this.toHost = [];              // every message the webview posted
    this.errors = [];
    this.approvalWaiters = new Map();
    this.ws = cwd ?? fs.mkdtempSync(path.join(os.tmpdir(), "tai-ws-"));
    this.state = fs.mkdtempSync(path.join(os.tmpdir(), "tai-state-"));
  }

  async open() {
    const page = (this.page = await this.browser.newPage());
    await page.setViewport(this.size);
    page.on("pageerror", (e) => this.errors.push("pageerror: " + e.message));
    page.on("console", (m) => { if (m.type() === "error") this.errors.push("console: " + m.text()); });
    await page.exposeFunction("__toHost", (m) => this.onWebview(m));
    const t = THEMES[this.theme];
    const vars = Object.entries(t.vars).map(([k, v]) => `${k}:${v}`).join(";");
    const png = (f) => "data:image/png;base64," + fs.readFileSync(path.join(ROOT, "media", f)).toString("base64");
    const logo = png("tai-logo-light.png"), logoDark = png("tai-logo-dark.png");
    const html = `<!DOCTYPE html><html style="${vars}"><head><meta charset="utf-8"></head>
      <body class="${t.cls}"><div id="app" data-logo="${logo}" data-logo-dark="${logoDark}"></div>
      <script>
        let st = {};
        window.acquireVsCodeApi = () => ({ postMessage: (m) => window.__toHost(m), getState: () => st, setState: (s) => { st = s; } });
      </script>
      <script>${fs.readFileSync(path.join(ROOT, "dist/webview.js"), "utf8")}</script></body></html>`;
    await page.setContent(html, { waitUntil: "load" });
    await this.waitFor((m) => m.type === "ready");
  }

  /** Starts tai --serve with the real TaiClient and forwards like ChatController. */
  async startTai({ mode = "manual" } = {}) {
    const env = { ...process.env, TAI_API_BASE: this.api, TILBURG_AI_API_KEY: "test-key", XDG_STATE_HOME: this.state };
    const c = (this.client = new TaiClient({ executable: TAI, cwd: this.ws, env, log: () => {} }));
    c.onEvent((e) => this.post({ type: "event", event: e }));
    c.onRequest((method, params, id) => {
      if (method === "approval") {
        return new Promise((resolve) => {
          this.approvalWaiters.set(id, resolve);
          this.post({ type: "approval", requestId: id, params });
        });
      }
      if (method === "ide/diagnostics") return Promise.resolve([]);
      return Promise.resolve({});
    });
    c.onEvent((e) => {
      if (e.type === "request_cancelled" && this.approvalWaiters.has(e.requestId)) {
        this.approvalWaiters.delete(e.requestId);
        this.post({ type: "approvalDone", requestId: e.requestId });
      }
    });
    await c.start();
    const init = await c.call("initialize", { ide: true, mode });
    this.post({ type: "status", status: "ready" });
    this.post({ type: "init", state: init.state, settings: { ctrlEnterToSend: false, attachOpenFile: true, showTimestamps: false },
      version: init.version, cwd: this.ws, modeInfo: init.modeInfo });
  }

  async onWebview(m) {
    this.toHost.push(m);
    const c = this.client;
    if (!c) return;
    try {
      switch (m.type) {
        case "send": {
          const attachments = [...m.text.matchAll(/(?:^|\s)@([^\s]+)/g)].map((x) => ({ path: x[1] }))
            .filter((a) => fs.existsSync(path.resolve(this.ws, a.path)));
          const context = m.includeContext && this.context ? this.context : null;
          await c.call("send", { text: m.text, clientId: m.clientId, attachments, context });
          break;
        }
        case "cancel": {
          const r = await c.call("cancel");
          if (r.unsent?.length) this.post({ type: "prefill", text: r.unsent.join("\n\n") });
          break;
        }
        case "approve": {
          const w = this.approvalWaiters.get(m.requestId);
          if (w) {
            this.approvalWaiters.delete(m.requestId);
            w({ decision: m.decision, feedback: m.feedback ?? "" });
            this.post({ type: "approvalDone", requestId: m.requestId });
          }
          break;
        }
        case "setMode": {
          const st = await c.call("set", { mode: m.mode });
          this.post({ type: "state", state: st });
          break;
        }
        case "btw":
          await c.call("btw", { question: m.question, id: m.id });
          break;
        case "newSession": {
          const st = await c.call("new_session");
          this.post({ type: "transcript", items: [], state: st });
          break;
        }
        case "searchFiles": {
          const files = fs.readdirSync(this.ws, { recursive: true }).map(String)
            .filter((f) => f.toLowerCase().includes(m.query.toLowerCase()))
            .map((f) => ({ path: path.join(this.ws, f), rel: f, dir: fs.statSync(path.join(this.ws, f)).isDirectory() }));
          this.post({ type: "fileResults", requestId: m.requestId, files });
          break;
        }
        case "command":
          if (m.name === "compact") await c.call("compact", { instructions: m.arg });
          break;
      }
    } catch (e) {
      this.post({ type: "notice", level: "error", text: String(e.message ?? e) });
    }
  }

  setContext(ctx) {
    this.context = ctx;
    this.post({ type: "context", context: ctx });
  }

  post(m) {
    return this.page.evaluate((msg) => window.postMessage(msg, "*"), m).catch(() => {});
  }

  async waitFor(pred, timeout = 10000) {
    const t = Date.now();
    while (Date.now() - t < timeout) {
      const hit = this.toHost.find(pred);
      if (hit) return hit;
      await new Promise((r) => setTimeout(r, 25));
    }
    throw new Error("webview never posted the expected message; got " + JSON.stringify(this.toHost.map((m) => m.type)));
  }

  async waitSel(sel, timeout = 10000) {
    return this.page.waitForSelector(sel, { timeout });
  }

  async waitIdle(timeout = 15000) {
    await this.page.waitForFunction(() => document.getElementById("thinking").hidden &&
      !document.querySelector(".msg.user.pending, .msg.user.queued"), { timeout });
    await new Promise((r) => setTimeout(r, 120));
  }

  async type(text) {
    await this.page.focus("#input");
    await this.page.keyboard.type(text);
  }

  async sendText(text) {
    await this.page.evaluate((t) => {
      const i = document.getElementById("input");
      i.value = t;
      i.dispatchEvent(new Event("input"));
    }, text);
    await this.page.focus("#input");
    await this.page.keyboard.press("Enter");
  }

  text(sel) {
    return this.page.$$eval(sel, (els) => els.map((e) => e.textContent.trim()));
  }

  async shot(dir, name) {
    fs.mkdirSync(dir, { recursive: true });
    await this.page.screenshot({ path: path.join(dir, `${name}-${this.theme}.png`) });
  }

  async close() {
    if (this.client) await this.client.dispose();
    await this.page?.close();
  }
}

export async function launch() {
  return puppeteer.launch({ executablePath: process.env.CHROME || "/usr/bin/google-chrome", headless: true,
    args: ["--no-sandbox", "--disable-gpu", "--font-render-hinting=none"] });
}
