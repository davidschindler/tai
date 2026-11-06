import * as crypto from "crypto";
import * as vscode from "vscode";

export function webviewHtml(webview: vscode.Webview, extUri: vscode.Uri): string {
  const nonce = crypto.randomBytes(16).toString("base64");
  const script = webview.asWebviewUri(vscode.Uri.joinPath(extUri, "dist", "webview.js"));
  const media = (f: string) => webview.asWebviewUri(vscode.Uri.joinPath(extUri, "media", f));
  const logo = media("tai-logo-light.png"), logoDark = media("tai-logo-dark.png");
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
<div id="app" data-logo="${logo}" data-logo-dark="${logoDark}"></div>
<script nonce="${nonce}" src="${script}"></script>
</body>
</html>`;
}
