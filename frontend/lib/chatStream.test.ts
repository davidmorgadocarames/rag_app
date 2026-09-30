// Unit tests for the SSE consumer (T11.2.16): `npm test` (node --test, type stripping).
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CONVERSATION_GONE,
  CONVERSATION_UNREADABLE,
  STREAM_INTERRUPTED,
  consumeChatStream,
  conversationLoadError,
  parseFrames,
  refusedStream,
} from "./chatStream.ts";

function body(...chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

const sse = (payload: object) => `data: ${JSON.stringify(payload)}\n\n`;

function record() {
  const calls: string[] = [];
  return {
    calls,
    handlers: {
      onConversation: (id: string) => calls.push(`conversation:${id}`),
      onStage: (stage: string) => calls.push(`stage:${stage}`),
      onToken: (text: string) => calls.push(`token:${text}`),
      onDone: () => calls.push("done"),
      onError: (detail: string, id: string | null) => calls.push(`error:${id}:${detail}`),
    },
  };
}

test("the id from the first event is kept and reported with the error", async () => {
  const { calls, handlers } = record();
  await consumeChatStream(
    body(
      sse({ type: "conversation", conversation_id: "c1" }),
      sse({ type: "stage", stage: "retrieving" }),
      sse({ type: "error", conversation_id: "c1", code: "retrieval_failed", detail: "down" }),
    ),
    handlers,
  );
  assert.deepEqual(calls, ["conversation:c1", "stage:retrieving", "error:c1:down"]);
});

test("frames split across chunks are reassembled; done ends the stream", async () => {
  const { calls, handlers } = record();
  const all = sse({ type: "conversation", conversation_id: "c2" }) + sse({ type: "token", text: "hi" });
  await consumeChatStream(
    body(all.slice(0, 7), all.slice(7, 40), all.slice(40), sse({ type: "done", conversation_id: "c2" })),
    handlers,
  );
  assert.deepEqual(calls, ["conversation:c2", "token:hi", "done"]);
});

test("a stream that ends without done/error is reported, never left hanging", async () => {
  const { calls, handlers } = record();
  await consumeChatStream(
    body(sse({ type: "conversation", conversation_id: "c3" }), sse({ type: "stage", stage: "generating" })),
    handlers,
  );
  assert.deepEqual(calls, ["conversation:c3", "stage:generating", `error:c3:${STREAM_INTERRUPTED}`]);
});

test("a broken connection is reported as an error with the id", async () => {
  const { calls, handlers } = record();
  const encoder = new TextEncoder();
  let pulls = 0;
  const broken = new ReadableStream<Uint8Array>({
    pull(controller) {
      pulls += 1;
      if (pulls === 1) {
        controller.enqueue(encoder.encode(sse({ type: "conversation", conversation_id: "c4" })));
      } else {
        controller.error(new Error("network down"));
      }
    },
  });
  await consumeChatStream(broken, handlers);
  assert.equal(calls.at(-1), `error:c4:${STREAM_INTERRUPTED}`);
});

test("malformed frames are skipped", () => {
  const [events, rest] = parseFrames("data: {oops\n\n" + sse({ type: "stage", stage: "x" }) + "data: {");
  assert.deepEqual(events, [{ type: "stage", stage: "x" }]);
  assert.equal(rest, "data: {");
});

test("keep-alive comment frames are ignored (DA-G2-3)", async () => {
  const { calls, handlers } = record();
  await consumeChatStream(
    body(": keep-alive\n\n", sse({ type: "conversation", conversation_id: "c1" }), ": keep-alive\n\n",
      sse({ type: "done", conversation_id: "c1" })),
    handlers,
  );
  assert.deepEqual(calls, ["conversation:c1", "done"]);
});

test("a 404 before streaming drops the stale conversation id (DA-G2-7)", () => {
  assert.deepEqual(refusedStream(404, "conversation not found", "c1"), {
    detail: CONVERSATION_GONE,
    conversationId: null,
    dropConversation: true,
  });
  assert.deepEqual(refusedStream(429, "rate limit exceeded", "c1"), {
    detail: "rate limit exceeded",
    conversationId: "c1",
    dropConversation: false,
  });
});

test("an unreadable (409) or vanished (404) conversation gets a clear message (DA-G2-7)", () => {
  assert.deepEqual(conversationLoadError(409, "this conversation cannot be decrypted right now"), {
    message: CONVERSATION_UNREADABLE,
    clear: true,
  });
  assert.deepEqual(conversationLoadError(404, "conversation not found"), {
    message: CONVERSATION_GONE,
    clear: true,
  });
  assert.deepEqual(conversationLoadError(500, "request failed (500)"), {
    message: "request failed (500)",
    clear: false,
  });
});
