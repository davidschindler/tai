// Types shared by the extension host and the webview (no runtime code here).

export type Mode = "manual" | "accept-edits" | "auto";

export interface SessionMeta {
  id: string;
  title: string;
  cwd: string;
  created: number;
  updated: number;
  model: string;
  tools: boolean;
  system: string;
  messages: number;
}

export interface Usage {
  input: number;
  output: number;
  ctx: number;
  maxCtx: number | null;
}

export interface TaiState {
  session: SessionMeta;
  model: string;
  mode: Mode;
  tools: boolean;
  system: string;
  busy: boolean;
  usage: Usage;
}

export interface FileDiff {
  path: string;
  old: string | null;
  new: string;
}

export type TranscriptItem =
  | { type: "user"; index: number; text: string; files: string[]; context: string; kind: string; time?: number }
  | { type: "assistant"; text: string; streaming?: boolean }
  | { type: "tool"; id: string; name: string; label: string; shown: string; args: Record<string, unknown>;
      output: string; summary: string; error: boolean; done: boolean }
  | { type: "task"; id: string; label: string; prompt: string; result: string; done: boolean };

/** Events from `tai --serve` (method "event"). */
export type TaiEvent =
  | { type: "ready"; version: string; protocol: number; hasKey?: boolean }
  | { type: "user"; index: number; clientId?: string | null; injected: boolean; text: string; files: string[]; context: string }
  | { type: "turn_start"; index: number }
  | { type: "text"; delta: string }
  | { type: "message_end" }
  | { type: "tool_writing"; name: string; tokens: number; agent?: string | null }
  | { type: "tool_start"; id: string; name: string; label: string; shown: string; args: Record<string, unknown>; agent?: string | null }
  | { type: "tool_end"; id: string; summary: string; error: boolean; output: string; diff: FileDiff | null; agent?: string | null }
  | { type: "task_start"; id: string; label: string; prompt: string; model?: string }
  | { type: "task_note"; id: string; text: string }
  | { type: "task_end"; id: string; result: string; error: boolean; uses?: number; tokens?: number }
  | { type: "usage"; usage: Usage }
  | { type: "turn_end"; reason: "done" | "cancelled" | "error" | "max_steps"; error: string | null;
      removedUser: { index: number; text: string } | null; unsent: string[];
      stats: { input: number; output: number; seconds: number; model: string }; usage: Usage }
  | { type: "file_changed"; path: string; created: boolean }
  | { type: "session"; session: SessionMeta }
  | { type: "state"; state: TaiState }
  | { type: "notice"; level: "info" | "warning" | "error"; text: string }
  | { type: "request_cancelled"; requestId: string }
  | { type: "btw_text"; id: string; delta: string }
  | { type: "btw_end"; id: string; error: string | null }
  | { type: "compact_end"; error: string | null; transcript: TranscriptItem[] | null; usage: Usage }
  | { type: "auto_compact"; phase: "start" } | { type: "auto_compact"; phase: "end"; error: string | null;
      before: number; after: number | null; usage: Usage };

export interface ApprovalParams {
  kind: string;
  title: string;
  question: string;
  always: string;
  agent: string;
  detail: {
    type?: "edit" | "bash" | "fetch" | "read";
    shell?: string;               // "Bash" or "PowerShell"
    path?: string;
    old?: string | null;
    new?: string | null;
    tooLarge?: boolean;
    command?: string;
    url?: string;
    host?: string;
  };
}

export type Decision = "yes" | "always" | "no";

export interface Attachment {
  path: string;                 // as typed (relative to the workspace) or absolute
  range?: [number, number];
}

export interface EditorContext {
  path: string;                 // absolute
  rel: string;
  selection?: { start: number; end: number; text: string };
}

export interface FileHit {
  path: string;
  rel: string;
  dir: boolean;
}

export interface Settings {
  ctrlEnterToSend: boolean;
  attachOpenFile: boolean;
  showTimestamps: boolean;
}

export interface ModelInfo {
  id: string;
  maxInput: number | null;
  embedding: boolean;
}

/** Extension host → webview. */
export type HostMessage =
  | { type: "status"; status: "starting" | "ready" | "stopped" | "error"; message?: string; canRestart?: boolean;
      needsKey?: boolean }
  | { type: "init"; state: TaiState; settings: Settings; version: string; cwd: string; modeInfo: Record<string, string> }
  | { type: "event"; event: TaiEvent }
  | { type: "approval"; requestId: string; params: ApprovalParams }
  | { type: "approvalDone"; requestId: string }
  | { type: "transcript"; items: TranscriptItem[]; state: TaiState }
  | { type: "state"; state: TaiState }
  | { type: "settings"; settings: Settings }
  | { type: "context"; context: EditorContext | null }
  | { type: "fileResults"; requestId: number; files: FileHit[] }
  | { type: "insertMention"; text: string }
  | { type: "prefill"; text: string }
  | { type: "notice"; level: "info" | "warning" | "error"; text: string }
  | { type: "focus" }
  | { type: "stop" };

/** Webview → extension host. */
export type WebviewMessage =
  | { type: "ready" }
  | { type: "send"; text: string; clientId: string; includeContext: boolean }
  | { type: "cancel" }
  | { type: "approve"; requestId: string; decision: Decision; feedback?: string }
  | { type: "openDiff"; requestId?: string; diff?: FileDiff }
  | { type: "openFile"; path: string; line?: number }
  | { type: "searchFiles"; query: string; requestId: number }
  | { type: "command"; name: string; arg: string }
  | { type: "setMode"; mode: Mode }
  | { type: "pickModel" }
  | { type: "history" }
  | { type: "newSession" }
  | { type: "rewind"; index: number }
  | { type: "btw"; question: string; id: string }
  | { type: "copy"; text: string }
  | { type: "insertCode"; text: string }
  | { type: "restart" }
  | { type: "setApiKey" }
  | { type: "openTerminal" }
  | { type: "focusChanged"; focused: boolean }
  | { type: "saveDraft"; text: string };
