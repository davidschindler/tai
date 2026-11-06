// Launches an isolated VS Code (downloaded by @vscode/test-electron) with the extension and runs
// the integration suite. Needs a display: uses $TAI_TEST_DISPLAY (e.g. a private Xvfb) or $DISPLAY.
import { spawn } from "child_process";
import fs from "fs";
import os from "os";
import path from "path";
import { runTests } from "@vscode/test-electron";

const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../..");

const mock = spawn("python3", [path.join(ROOT, "test/mock_api.py")], { stdio: ["ignore", "pipe", "inherit"] });
const port = await new Promise((resolve) => mock.stdout.once("data", (d) => resolve(/PORT (\d+)/.exec(String(d))[1])));
const ws = fs.mkdtempSync(path.join(os.tmpdir(), "tai-vscode-ws-"));
const userData = fs.mkdtempSync(path.join(os.tmpdir(), "tai-vscode-user-"));
const state = fs.mkdtempSync(path.join(os.tmpdir(), "tai-vscode-state-"));
const apiBase = `http://127.0.0.1:${port}/v1`;
// VS Code resolves the login shell's environment (which may hold the real key): pin the test
// values through the extension's own setting, which takes precedence.
fs.mkdirSync(path.join(ws, ".vscode"));
fs.writeFileSync(path.join(ws, ".vscode", "settings.json"), JSON.stringify({
  "tai.environmentVariables": { TILBURG_AI_API_KEY: "test-key", TAI_API_BASE: apiBase, XDG_STATE_HOME: state },
}));
delete process.env.TILBURG_AI_API_KEY;

let code = 0;
try {
  await runTests({
    version: process.env.VSCODE_VERSION || "stable",
    extensionDevelopmentPath: ROOT,
    extensionTestsPath: path.join(ROOT, "dist/test/index.js"),
    launchArgs: [ws, "--disable-extensions", "--user-data-dir", userData, "--skip-welcome", "--skip-release-notes",
      "--disable-workspace-trust", "--disable-gpu"],
    extensionTestsEnv: {
      TAI_API_BASE: apiBase, XDG_STATE_HOME: state,
      DISPLAY: process.env.TAI_TEST_DISPLAY || process.env.DISPLAY, ELECTRON_ENABLE_LOGGING: "",
    },
  });
} catch (e) {
  console.error("integration tests failed:", e.message);
  code = 1;
} finally {
  mock.kill();
}
process.exit(code);
