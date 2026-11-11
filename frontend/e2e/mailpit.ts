// A minimal Mailpit HTTP API client for the `e2e` gate step's register -> verify flow
// (T12.4.4, "verification via Mailpit in local/e2e"). The gate's own Mailpit
// (compose.gate.yml; a different container/ports from the development stack's) is brought
// up by `e2e_start_backend`; its base URL arrives here as MAILPIT_API_URL
// (scripts/gate.sh's step_e2e).
//
// Polls (sending + delivery are not instantaneous relative to this test's own HTTP calls);
// never logs/prints the email body (it carries a live verification token).

interface MailpitSearchResult {
  messages?: { ID: string }[];
}

interface MailpitMessage {
  Text?: string;
  HTML?: string;
}

async function latestMessageIdTo(apiBase: string, email: string): Promise<string | null> {
  const res = await fetch(`${apiBase}/api/v1/search?query=${encodeURIComponent(`to:${email}`)}`);
  if (!res.ok) return null;
  const body = (await res.json()) as MailpitSearchResult;
  return body.messages?.[0]?.ID ?? null;
}

/** Polls Mailpit until an email to `email` arrives, then returns its plain-text body. */
export async function waitForEmailBody(
  apiBase: string,
  email: string,
  { timeoutMs = 20_000, intervalMs = 500 }: { timeoutMs?: number; intervalMs?: number } = {},
): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const id = await latestMessageIdTo(apiBase, email);
    if (id) {
      const res = await fetch(`${apiBase}/api/v1/message/${id}`);
      const body = (await res.json()) as MailpitMessage;
      return body.Text ?? body.HTML ?? "";
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error(`no email arrived at Mailpit for this address within ${timeoutMs}ms`);
}

/** The `token=...` query value out of a verification/reset link in an email body. */
export function extractToken(body: string): string {
  const match = /token=([^\s&"'<]+)/.exec(body);
  if (!match) throw new Error("no 'token=' found in the email body");
  return match[1];
}
