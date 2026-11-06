// Markdown, code highlighting and diff rendering for the chat webview.
import DOMPurify from "dompurify";
import { structuredPatch } from "diff";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import css from "highlight.js/lib/languages/css";
import diffLang from "highlight.js/lib/languages/diff";
import go from "highlight.js/lib/languages/go";
import ini from "highlight.js/lib/languages/ini";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import latex from "highlight.js/lib/languages/latex";
import markdown from "highlight.js/lib/languages/markdown";
import plaintext from "highlight.js/lib/languages/plaintext";
import python from "highlight.js/lib/languages/python";
import r from "highlight.js/lib/languages/r";
import rust from "highlight.js/lib/languages/rust";
import shell from "highlight.js/lib/languages/shell";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";
import { Marked } from "marked";

const langs: Record<string, any> = { bash, c, cpp, css, diff: diffLang, go, ini, java, javascript, json, latex,
  markdown, plaintext, python, r, rust, shell, sql, typescript, xml, yaml };
for (const [k, v] of Object.entries(langs)) hljs.registerLanguage(k, v);
hljs.registerAliases(["sh", "zsh", "console"], { languageName: "bash" });
hljs.registerAliases(["tex"], { languageName: "latex" });
hljs.registerAliases(["html", "svg"], { languageName: "xml" });
hljs.registerAliases(["py"], { languageName: "python" });
hljs.registerAliases(["js", "jsx", "mjs"], { languageName: "javascript" });
hljs.registerAliases(["ts", "tsx"], { languageName: "typescript" });
hljs.registerAliases(["yml"], { languageName: "yaml" });
hljs.registerAliases(["toml"], { languageName: "ini" });
hljs.registerAliases(["text", "txt"], { languageName: "plaintext" });

const marked = new Marked({ gfm: true, breaks: false });

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]!));
}

DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") node.setAttribute("rel", "noopener");
});

/** Markdown → sanitized HTML. */
export function mdToHtml(text: string): string {
  const html = marked.parse(text, { async: false }) as string;
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true }, FORBID_TAGS: ["style", "form", "input"],
    FORBID_ATTR: ["style"] });
}

const PATH_RE = /^(?:~|\.{1,2})?\/?(?:[\w.@+-]+\/)*[\w.@+-]+\.[A-Za-z0-9]{1,8}(?::(\d+)(?:[-:]\d+)?)?$/;
const DIR_RE = /^(?:\.{0,2}\/)?(?:[\w.@+-]+\/)+$/;

/** If `s` looks like a file reference (path, path:line), returns {path, line}. */
export function asPathRef(s: string): { path: string; line?: number } | null {
  const t = s.trim();
  if (t.length > 300 || /\s/.test(t) || /^https?:/.test(t)) return null;
  if (DIR_RE.test(t)) return { path: t };
  const m = PATH_RE.exec(t);
  if (!m) return null;
  if (/^\d+(\.\d+)+$/.test(t.split(":")[0])) return null;      // version numbers like 1.2.3
  const p = t.replace(/:(\d+)(?:[-:]\d+)?$/, "");
  return { path: p, line: m[1] ? parseInt(m[1], 10) : undefined };
}

export interface RenderHooks {
  openFile: (path: string, line?: number) => void;
  copy: (text: string) => void;
  insert: (text: string) => void;
}

/** Renders markdown into `el`. `final` adds highlighting and code-block toolbars. */
export function renderMarkdown(el: HTMLElement, text: string, hooks: RenderHooks, final: boolean): void {
  el.innerHTML = mdToHtml(text);
  el.querySelectorAll("code").forEach((code) => {
    if (code.parentElement?.tagName === "PRE") return;
    const ref = asPathRef(code.textContent ?? "");
    if (ref) {
      code.classList.add("path-link");
      code.title = "Open " + ref.path + (ref.line ? ` at line ${ref.line}` : "");
      code.addEventListener("click", () => hooks.openFile(ref.path, ref.line));
    }
  });
  el.querySelectorAll("a").forEach((a) => {
    const href = a.getAttribute("href") ?? "";
    if (/^(https?|mailto):/i.test(href)) return;
    a.addEventListener("click", (ev) => {
      ev.preventDefault();
      const m = /^(.*?)(?:#L(\d+))?$/.exec(decodeURI(href));
      if (m && m[1]) hooks.openFile(m[1], m[2] ? parseInt(m[2], 10) : undefined);
    });
  });
  el.querySelectorAll("pre > code").forEach((code) => {
    const pre = code.parentElement as HTMLElement;
    const lang = (/language-([\w+#-]+)/.exec(code.className) ?? [])[1] ?? "";
    if (final) {
      try {
        if (lang && hljs.getLanguage(lang)) {
          code.innerHTML = hljs.highlight(code.textContent ?? "", { language: lang, ignoreIllegals: true }).value;
        }
        code.classList.add("hljs");
      } catch { /* leave plain */ }
    }
    const wrap = document.createElement("div");
    wrap.className = "codeblock";
    const bar = document.createElement("div");
    bar.className = "codebar";
    const label = document.createElement("span");
    label.textContent = lang || "text";
    bar.append(label);
    if (final) {
      const raw = code.textContent ?? "";
      bar.append(iconButton("copy", "Copy", () => hooks.copy(raw)), iconButton("insert", "Insert at cursor", () => hooks.insert(raw)));
    }
    pre.replaceWith(wrap);
    wrap.append(bar, pre);
  });
}

// ------------------------------------------------------------------ icons
const ICONS: Record<string, string> = {
  copy: '<path d="M4 4h7v9H4z" fill="none" stroke="currentColor"/><path d="M6 2h7v9" fill="none" stroke="currentColor"/>',
  insert: '<path d="M8 2v8M5 7l3 3 3-3M3 13h10" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"/>',
  rewind: '<path d="M3 3v4h4" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"/><path d="M3.5 7A5 5 0 1 1 4 10.5" fill="none" stroke="currentColor" stroke-linecap="round"/>',
  send: '<path d="M8 13V3M4 7l4-4 4 4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
  stop: '<rect x="4" y="4" width="8" height="8" rx="1.5" fill="currentColor"/>',
  close: '<path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-linecap="round"/>',
  diff: '<path d="M5 2v8M2 6h6M10 9h4M11 14V4" fill="none" stroke="currentColor" stroke-linecap="round"/>',
  file: '<path d="M4 1.5h5l3 3v10H4z" fill="none" stroke="currentColor" stroke-linejoin="round"/><path d="M9 1.5v3h3" fill="none" stroke="currentColor"/>',
  eye: '<path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z" fill="none" stroke="currentColor"/><circle cx="8" cy="8" r="2" fill="none" stroke="currentColor"/>',
  eyeoff: '<path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z" fill="none" stroke="currentColor"/><path d="M2 14L14 2" stroke="currentColor"/>',
  history: '<circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor"/><path d="M8 5v3l2 1.5" fill="none" stroke="currentColor" stroke-linecap="round"/>',
  plus: '<path d="M8 3v10M3 8h10" stroke="currentColor" stroke-linecap="round"/>',
  chevron: '<path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"/>',
  at: '<circle cx="8" cy="8" r="2.5" fill="none" stroke="currentColor"/><path d="M10.5 8v1a2 2 0 0 0 4 0V8a6.5 6.5 0 1 0-3 5.5" fill="none" stroke="currentColor" stroke-linecap="round"/>',
};

export function icon(name: string): string {
  return `<svg class="icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">${ICONS[name] ?? ""}</svg>`;
}

export function iconButton(name: string, title: string, onClick: () => void, cls = "iconbtn"): HTMLButtonElement {
  const b = document.createElement("button");
  b.className = cls;
  b.title = title;
  b.setAttribute("aria-label", title);
  b.innerHTML = icon(name);
  b.addEventListener("click", (e) => {
    e.stopPropagation();
    onClick();
  });
  return b;
}

// ------------------------------------------------------------------ diffs
export interface DiffStats { added: number; removed: number }

/** Line-numbered diff (like tai's terminal view); `maxLines` rows, then a "more" marker. */
export function renderDiff(oldText: string | null, newText: string, maxLines = 400): { el: HTMLElement; stats: DiffStats } {
  const el = document.createElement("div");
  el.className = "diff";
  const patch = structuredPatch("a", "b", oldText ?? "", newText, "", "", { context: 2 });
  let added = 0, removed = 0, rows = 0;
  const table = document.createElement("div");
  table.className = "diff-rows";
  patch.hunks.forEach((h, hi) => {
    if (hi > 0) {
      const sep = document.createElement("div");
      sep.className = "diff-row sep";
      sep.textContent = "⋮";
      table.append(sep);
    }
    let o = h.oldStart, n = h.newStart;
    for (const line of h.lines) {
      const sign = line[0];
      if (sign === "\\") continue;
      if (sign === "+") added++;
      if (sign === "-") removed++;
      if (rows < maxLines) {
        const row = document.createElement("div");
        row.className = "diff-row" + (sign === "+" ? " add" : sign === "-" ? " del" : "");
        const num = document.createElement("span");
        num.className = "ln";
        num.textContent = String(sign === "-" ? o : n);
        const txt = document.createElement("span");
        txt.className = "tx";
        txt.textContent = (sign === " " ? "  " : sign + " ") + line.slice(1);
        row.append(num, txt);
        table.append(row);
      }
      rows++;
      if (sign !== "+") o++;
      if (sign !== "-") n++;
    }
  });
  el.append(table);
  if (rows > maxLines) {
    const more = document.createElement("div");
    more.className = "diff-more";
    more.textContent = `… ${rows - maxLines} more lines`;
    el.append(more);
  }
  if (!patch.hunks.length) {
    const none = document.createElement("div");
    none.className = "diff-more";
    none.textContent = oldText === null ? "(empty file)" : "(no changes)";
    el.append(none);
  }
  return { el, stats: { added, removed } };
}
