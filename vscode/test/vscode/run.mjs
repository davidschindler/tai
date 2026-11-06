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
const real = !!process.env.TAI_REAL;       // end-to-end against the real API with the user's key
fs.mkdirSync(path.join(ws, ".vscode"));
fs.writeFileSync(path.join(ws, ".vscode", "settings.json"), JSON.stringify({
  "tai.environmentVariables": real ? { XDG_STATE_HOME: state }
    : { TILBURG_AI_API_KEY: "test-key", TAI_API_BASE: apiBase, XDG_STATE_HOME: state },
}));
if (!real) delete process.env.TILBURG_AI_API_KEY;
// Keep the test window off the user's desktop: X11 only, on the display we were given.
if (process.env.TAI_TEST_DISPLAY) process.env.DISPLAY = process.env.TAI_TEST_DISPLAY;
delete process.env.WAYLAND_DISPLAY;
process.env.ELECTRON_OZONE_PLATFORM_HINT = "x11";

let code = 0;
try {
  await runTests({
    version: process.env.VSCODE_VERSION || "stable",
    extensionDevelopmentPath: ROOT,
    extensionTestsPath: path.join(ROOT, "dist/test/index.js"),
    launchArgs: [ws, "--disable-extensions", "--user-data-dir", userData, "--skip-welcome", "--skip-release-notes",
      "--disable-workspace-trust", "--disable-gpu", "--ozone-platform=x11"],
    extensionTestsEnv: {
      ...(real ? { TAI_REAL: "1" } : { TAI_API_BASE: apiBase }), XDG_STATE_HOME: state,
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
