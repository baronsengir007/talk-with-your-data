// The page's side of web/app.py: the tab id, /api/ask as a stream of events, /api/reset.

import type { Answer, Step } from "./types";

const TAB_KEY = "twyd-tab-id";

/** One browser tab is one conversation on the server (ARCHITECTURE data flow 4). */
export function tabId(): string {
  let id = sessionStorage.getItem(TAB_KEY);
  if (!id) id = newTabId();
  return id;
}

export function newTabId(): string {
  const id = crypto.randomUUID();
  sessionStorage.setItem(TAB_KEY, id);
  return id;
}

export type ServerEvent =
  | ({ type: "step" } & Step)
  | ({ type: "answer" } & Answer)
  | { type: "error"; headline: string; detail: string }
  | { type: "config_error"; variable: string; headline: string };

/** Reads `data: <json>` events from an SSE body, one at a time. */
async function* events(body: ReadableStream<Uint8Array>): AsyncGenerator<ServerEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end = buffer.indexOf("\n\n");
    while (end >= 0) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      for (const line of block.split("\n")) {
        if (line.startsWith("data: ")) yield JSON.parse(line.slice(6)) as ServerEvent;
      }
      end = buffer.indexOf("\n\n");
    }
  }
}

/** Posts one message and hands every event to `onEvent` as it arrives. Throws when the server cannot be reached. */
export async function ask(tab: string, message: string, onEvent: (ev: ServerEvent) => void): Promise<void> {
  const res = await fetch("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tab_id: tab, message }),
  });
  if (!res.ok || !res.body) throw new Error("the server answered " + res.status);
  for await (const ev of events(res.body)) onEvent(ev);
}

export async function reset(tab: string): Promise<void> {
  await fetch("/api/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tab_id: tab }),
  });
}
