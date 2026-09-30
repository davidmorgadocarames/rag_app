// Server-Sent Events consumer for POST /chat/stream (T11.2.16).
//
// Contract: the first event is {"type":"conversation","conversation_id":...}; then stage /
// token events; the stream ends with exactly one "done" or "error" event, both carrying the
// conversation id. This consumer guarantees the UI one terminal callback in every case:
// a stream that ends (or breaks) without "done"/"error" is reported as an error, so the UI
// never hangs on a stage. Comment frames (": keep-alive", sent while a stage takes long)
// carry no data line and are ignored.
//
// Plain TypeScript with no imports, so `node --test` can run it with type stripping.

export interface StreamEvent {
  type: string;
  [key: string]: unknown;
}

export interface StreamHandlers {
  onConversation?: (id: string) => void;
  onStage?: (stage: string) => void;
  onToken?: (text: string) => void;
  onDone?: (event: StreamEvent) => void;
  onError?: (detail: string, conversationId: string | null) => void;
}

export const STREAM_INTERRUPTED =
  "The connection to the server was interrupted before the answer finished. Please try again.";

export const CONVERSATION_GONE =
  "This conversation no longer exists (it was deleted). " +
  "Your next message starts a new conversation.";

export const CONVERSATION_UNREADABLE =
  "This conversation cannot be decrypted right now, so it cannot be shown or continued. " +
  "Start a new chat, or delete it from the list.";

/**
 * A /chat/stream request refused before any streaming (DA-G2-7): the message to show and the
 * conversation the NEXT message should continue. A 404 means the conversation is gone
 * (deleted elsewhere): the id is dropped, so the next message starts a new conversation
 * instead of failing with 404 again. Any other failure keeps the id.
 */
export function refusedStream(
  status: number,
  detail: string,
  conversationId: string | null,
): { detail: string; conversationId: string | null; dropConversation: boolean } {
  if (status === 404) {
    return { detail: CONVERSATION_GONE, conversationId: null, dropConversation: true };
  }
  return { detail, conversationId, dropConversation: false };
}

/**
 * Loading a conversation failed (DA-G2-7): 409 = its data cannot be decrypted (a clear
 * message instead of a blank view); 404 = it is gone. `clear` = leave that conversation (the
 * thread is emptied, so nothing is sent into it).
 */
export function conversationLoadError(
  status: number | undefined,
  detail: string,
): { message: string; clear: boolean } {
  if (status === 409) return { message: CONVERSATION_UNREADABLE, clear: true };
  if (status === 404) return { message: CONVERSATION_GONE, clear: true };
  return { message: detail, clear: false };
}

/** Split complete SSE frames off the buffer; returns [events, rest of the buffer]. */
export function parseFrames(buffer: string): [StreamEvent[], string] {
  const events: StreamEvent[] = [];
  let rest = buffer;
  let sep: number;
  while ((sep = rest.indexOf("\n\n")) >= 0) {
    const frame = rest.slice(0, sep);
    rest = rest.slice(sep + 2);
    const line = frame.split("\n").find((l) => l.startsWith("data:"));
    if (!line) continue;
    try {
      const event = JSON.parse(line.slice(5).trim()) as StreamEvent;
      if (event && typeof event.type === "string") events.push(event);
    } catch {
      // a malformed frame is skipped; the missing terminal event is reported at the end
    }
  }
  return [events, rest];
}

/** Read the SSE body to the end, dispatching events; always ends in onDone or onError. */
export async function consumeChatStream(
  body: ReadableStream<Uint8Array>,
  handlers: StreamHandlers,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let conversationId: string | null = null;
  let finished = false;

  const dispatch = (event: StreamEvent) => {
    const id = typeof event.conversation_id === "string" ? event.conversation_id : null;
    if (id && id !== conversationId) {
      conversationId = id;
      handlers.onConversation?.(id);
    }
    if (event.type === "stage") handlers.onStage?.(String(event.stage));
    else if (event.type === "token") handlers.onToken?.(String(event.text));
    else if (event.type === "done") {
      finished = true;
      handlers.onDone?.(event);
    } else if (event.type === "error") {
      finished = true;
      handlers.onError?.(String(event.detail ?? "Something went wrong."), conversationId);
    }
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const [events, rest] = parseFrames(buffer);
      buffer = rest;
      for (const event of events) {
        if (!finished) dispatch(event);
      }
    }
    const [events] = parseFrames(buffer + decoder.decode() + "\n\n");
    for (const event of events) {
      if (!finished) dispatch(event);
    }
  } catch {
    // network error mid-stream: reported below
  }
  if (!finished) handlers.onError?.(STREAM_INTERRUPTED, conversationId);
}
