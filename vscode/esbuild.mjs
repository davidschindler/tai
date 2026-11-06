import * as esbuild from "esbuild";
import fs from "fs";

// tai itself ships inside the extension (tai/tai.py), so it works with just Python installed.
fs.mkdirSync("tai", { recursive: true });
fs.copyFileSync("../tai.py", "tai/tai.py");
fs.copyFileSync("../LICENSE", "LICENSE");      // shipped in the .vsix

const watch = process.argv.includes("--watch");
const builds = [
  { entryPoints: ["src/extension.ts"], outfile: "dist/extension.js", platform: "node", format: "cjs",
    external: ["vscode"], target: "node18" },
  { entryPoints: ["webview/main.ts"], outfile: "dist/webview.js", platform: "browser", format: "iife",
    target: "es2022", loader: { ".css": "text" } },
  { entryPoints: ["src/taiClient.ts"], outfile: "dist/test/taiClient.js", platform: "node", format: "cjs",
    target: "node18" },
  { entryPoints: ["test/vscode/index.ts"], outfile: "dist/test/index.js", platform: "node", format: "cjs",
    external: ["vscode", "mocha"], target: "node18" },
  { entryPoints: ["test/vscode/suite.ts"], outfile: "dist/test/suite.js", platform: "node", format: "cjs",
    external: ["vscode", "mocha"], target: "node18" },
];
for (const b of builds) {
  const opts = { bundle: true, sourcemap: true, minify: process.argv.includes("--minify"), logLevel: "info", ...b };
  if (watch) await (await esbuild.context(opts)).watch();
  else await esbuild.build(opts);
}
