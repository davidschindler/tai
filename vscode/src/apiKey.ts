import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";

export const KEY_VAR = "TILBURG_AI_API_KEY";
const SECRET_ID = "tai.apiKey";

/** Reads `export TILBURG_AI_API_KEY=...` (or without export) from a shell file such as ~/.secrets. */
export function keyFromShellFile(file: string): string | undefined {
  let text: string;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch {
    return undefined;
  }
  const re = new RegExp(`^\\s*(?:export\\s+)?${KEY_VAR}\\s*=\\s*(.*)$`, "m");
  const m = re.exec(text);
  if (!m) return undefined;
  let v = m[1].trim();
  const q = v[0];
  if ((q === '"' || q === "'") && v.lastIndexOf(q) > 0) v = v.slice(1, v.lastIndexOf(q));
  else v = v.replace(/\s+#.*$/, "");
  return v && !v.includes("$") ? v : undefined;
}

/** Key lookup order: environment of VS Code, the extension's secret storage, ~/.secrets. */
export async function resolveApiKey(secrets: vscode.SecretStorage, extraEnv: Record<string, string> = {}):
  Promise<string | undefined> {
  if (extraEnv[KEY_VAR]) return extraEnv[KEY_VAR];
  if (process.env[KEY_VAR]) return process.env[KEY_VAR];
  const stored = await secrets.get(SECRET_ID);
  if (stored) return stored;
  return keyFromShellFile(path.join(os.homedir(), ".secrets"));
}

export async function promptForApiKey(secrets: vscode.SecretStorage): Promise<boolean> {
  const key = await vscode.window.showInputBox({
    title: "Tilburg.ai API key",
    prompt: "Stored in VS Code's secret storage and passed to tai as TILBURG_AI_API_KEY.",
    password: true,
    ignoreFocusOut: true,
    validateInput: (v) => (v.trim() ? undefined : "Enter a key"),
  });
  if (!key) return false;
  await secrets.store(SECRET_ID, key.trim());
  return true;
}
