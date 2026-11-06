import * as crypto from "crypto";
import * as vscode from "vscode";

export function webviewHtml(webview: vscode.Webview, extUri: vscode.Uri): string {
  const nonce = crypto.randomBytes(16).toString("base64");
  const script = webview.asWebviewUri(vscode.Uri.joinPath(extUri, "dist", "webview.js"));
  const logo = webview.asWebviewUri(vscode.Uri.joinPath(extUri, "media", "tai-logo.svg"));
  const csp = [
    "default-src 'none'",
    `img-src ${webview.cspSource} data:`,       // no remote images: they could leak data in URLs
    `style-src ${webview.cspSource} 'unsafe-inline'`,
    `font-src ${webview.cspSource}`,
    `script-src 'nonce-${nonce}'`,
  ].join("; ");
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>tai</title>
</head>
<body>
<div id="app" data-logo="${logo}"></div>
<script nonce="${nonce}" src="${script}"></script>
</body>
</html>`;
}
