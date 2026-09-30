# App Flow — SecRAG

This maps the user journey: the screens, what each does, what happens on each action, and which page
comes next.

## 1. Screen map

```
Landing (/)  — static vault-door hero + "Inside the vault" corpus explainer
  ├─▶ Sign up (/signup) ─▶ Verify email (/verify) ─▶ Login (/login)
  ├─▶ Login (/login) ─────────────────────────────────▶ Chat (/chat)
  └─▶ (public) About / docs links

Chat (/chat)              [authenticated]
  ├─ ask a question ─▶ answer with citations  OR  abstention
  ├─▶ History (/history)
  └─▶ Account (/account) ─▶ Delete my data (confirm) ─▶ Landing
```

## 2. Flows

### 2.1 Sign up

1. User opens **`/signup`**, enters email + password.
2. On **Create account**:
   - Client validates format; server enforces password policy.
   - Server runs **risk scoring** (IP velocity, disposable-email check, account-age heuristics). High
     risk → challenge (CAPTCHA) or block.
   - On success: user row created (`email_verified = false`), a verification token is emailed
     (the address and the link are never written to a log; the cloud deployment has no SMTP
     yet, so it does not require verification).
   - Redirect to **`/verify`** ("Check your email").
3. If email already exists → inline error (409, also when two sign-ups race), stay on page.

### 2.2 Verify email

1. User clicks the link → **`/verify?token=…`**.
2. Server validates the token (unexpired, unused) → sets `email_verified = true`, consumes token.
3. Redirect to **`/login`** with a success banner. Invalid/expired token → offer to resend.

### 2.3 Login

1. User opens **`/login`**: the **vault door** fills the screen (idles with a periodic "clic"). This is
   a purely visual layer — the auth logic below is unchanged.
2. User presses **ABRIR** → the wheel spins, the bolts retract, the view zooms into the vault, and a
   minimal **access panel** (email + password, ENTER, and a sign in / register toggle) fades in. With
   `prefers-reduced-motion`, ABRIR reveals the panel immediately (no spin/zoom).
3. User enters credentials. On **ENTER**:
   - Server checks **login rate limit** (token bucket) for the IP/account.
   - Verifies argon2 hash; requires `email_verified = true`.
   - Success → issues session/JWT → redirect to **`/chat`**.
   - Failure → generic error (no user enumeration); increment `login_attempts`.

### 2.4 Ask a question (core loop)

1. On **`/chat`** (a message thread with the input pinned at the bottom), the user types a question and
   presses **Send** (Enter; Shift+Enter for a newline). Requires auth → redirects to `/login` otherwise.
2. Client → `POST /chat/stream` (Bearer token). Server:
   - Applies the **per-user rate limit / quota** (cost-aware). Over limit → 429.
   - Checks the **global daily answer cap** (`DAILY_ANSWER_CAP` answers per UTC day for all
     users together — a cost guard for the public demo; 300 by default). The answer is
     counted **before** any LLM call, after the user's data key is known to unwrap. Once the
     cap is reached the stream sends the `conversation` event and one `error` event
     (`daily_cap_reached`, "SecRAG has reached its daily answer limit … come back after
     midnight UTC") and stores nothing; the one-shot `POST /chat` answers **429** with
     `Retry-After` (seconds to UTC midnight). A greeting (fast path, no LLM call) is never
     counted or refused.
   - **Classifies intent.** Small talk / greetings ("hello", "thanks", "who are you") take a **fast-path**:
     an instant, honest, non-sourced reply — no retrieval, no citations, no abstention.
   - Security questions run the grounded RAG pipeline: retrieve → rerank → generate → groundedness check.
   - Streams **Server-Sent Events** back. The **first** event is `conversation` with the
     `conversation_id` (the conversation and the user message are stored first, in one
     transaction); then `stage` (classifying/retrieving/reranking/generating/checking) and `token`
     deltas; the stream ends with exactly one `done` (the authoritative answer) or `error` event, both
     carrying the `conversation_id`.
   - **Every failure** — data key cannot be unwrapped, storage, embeddings/retrieval, reranking, LLM —
     ends the stream with an `error` event (`code` + user-facing `detail`), never a cut stream. The
     failed turn is stored as an **assistant error marker**, so no conversation holds a user message
     without a reply. When the data key cannot be unwrapped nothing is stored (nothing can be
     encrypted) and the event carries the requested id, or `null` for a new conversation.
   - A client that goes away mid-stream (closed tab, network drop) still gets its turn answered:
     the server stores an `interrupted` error marker. While a stage takes long (e.g. the reranker
     download at a cold start) the server sends an SSE comment `: keep-alive` every 15 s, so no
     proxy cuts the idle connection; the client ignores it.
   - The request's own database session is closed before streaming starts; the pipeline uses its
     own session and ends its read transaction after every event, so no connection stays idle
     in a transaction while the model generates.
3. Response rendering (streamed live):
   - A **stage indicator** shows the current step so the wait is legible.
   - **Answered** → answer text + **citations** (heading, version, effective date) + **token usage** for
     that answer; a **running conversation total** shows in the header.
   - **Abstained** → dignified card ("No reliable source in the corpus") — no fabricated content.
   - **Error** → the assistant bubble shows the error message (also for a stream that breaks without a
     final event); nothing partial is presented as authoritative. The client keeps the
     `conversation_id` from the first event, so the next message continues the same conversation.
     If the conversation was deleted elsewhere (404 before streaming), the client drops the id and
     says so; the next message starts a new conversation. Opening a conversation that cannot be
     decrypted (409) shows a clear message and an empty thread instead of a blank view.
4. The user + assistant messages are **persisted encrypted** (per-user key). Follow-ups continue the same
   conversation; **New chat** starts a fresh one.

### 2.5 History (sidebar)

1. A **left sidebar** lists the user's conversations (most recent first) with per-conversation token
   totals; titles derive from the first message (or a user-set rename). A conversation that cannot be
   decrypted is listed as "Unreadable conversation" — one bad row never fails the list.
2. **Select** one to load its full thread; **New chat**, inline **rename**, and **delete** are available.
   Deleting a conversation cascades to its messages.

### 2.6 Account & data deletion

1. **`/account`** shows email and account controls. If the email isn't verified, a **Resend
   verification** button issues a fresh token (`POST /auth/resend-verification`); only with `ENV=dev`
   and no SMTP does the API return the verification link, so the user can complete
   `GET /auth/verify` directly. In prod it never returns the link (without SMTP anyone could
   otherwise verify an address they do not own).
2. **Delete my data** → confirmation modal ("This is irreversible").
3. On confirm → `DELETE /api/account` → **202 Accepted** (asynchronous erasure, ADR phase 11
   decision 6). One short transaction:
   - **Crypto-shred**: delete the user's wrapped data key — every stored message and title
     is unreadable from that moment.
   - **Scrub** the email and password hash; mark the account deleted (`deleted_at`): the
     current token stops working at once and the email can be registered again.
   - Queue a **tombstone** (`pending`).
4. The page shows the server's message (`ERASURE_ACCEPTED_MESSAGE`: the data in the live
   service is unreadable from now on and the remaining rows go within 24 h; backup copies
   still hold the wrapped key and the email until they are deleted within 14 days; a minimal
   erasure record — random id, dates, status — is kept, its exports for 30 days)
   and signs the user out; **Back to the home page** leads to Landing. If another
   transaction holds the account for more than 2 s, the server answers **503** with
   `Retry-After` and nothing is deleted: the page shows the message, the user stays signed
   in and can try again.
5. The **purger** (hourly Job on Azure, compose `purger` locally) deletes the remaining rows
   in small batches, leaf-first — messages → conversations → verification tokens → the user
   row — and marks the tombstone `done`. Restores replay the tombstones before reopening.

## 3. Route → auth matrix

| Route | Auth required | Notes |
|-------|---------------|-------|
| `/` | no | Landing |
| `/signup`, `/login`, `/verify` | no | Redirect to `/chat` if already authenticated |
| `/chat`, `/account` | yes | Redirect to `/login` if not authenticated |
| `POST /chat/stream`, `POST /chat`, `GET/PATCH/DELETE /conversations…` | yes | Chat is rate-limited (cost-aware) and counted against the global daily answer cap |
| `POST /auth/resend-verification`, `DELETE /account` | yes | |

## 4. Key states to design for

- Loading (retrieval/generation in progress), empty (no history yet), abstention, rate-limited (429),
  daily answer cap reached (`daily_cap_reached` / 429 — the bubble shows the server's message),
  unverified-email, erasure accepted (202) or busy (503 + `Retry-After`), unreadable
  conversation (409), and error states — each screen must handle these explicitly.
