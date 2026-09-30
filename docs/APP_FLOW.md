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
   - On success: user row created (`email_verified = false`), a verification token is emailed.
   - Redirect to **`/verify`** ("Check your email").
3. If email already exists → inline error, stay on page.

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
3. Response rendering (streamed live):
   - A **stage indicator** shows the current step so the wait is legible.
   - **Answered** → answer text + **citations** (heading, version, effective date) + **token usage** for
     that answer; a **running conversation total** shows in the header.
   - **Abstained** → dignified card ("No reliable source in the corpus") — no fabricated content.
   - **Error** → the assistant bubble shows the error message (also for a stream that breaks without a
     final event); nothing partial is presented as authoritative. The client keeps the
     `conversation_id` from the first event, so the next message continues the same conversation.
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
   verification** button issues a fresh token (`POST /auth/resend-verification`); in dev (no SMTP) the
   API returns the verification link so the user can complete `GET /auth/verify` directly.
2. **Delete my data** → confirmation modal ("This is irreversible").
3. On confirm → `DELETE /api/account` → **202 Accepted** (asynchronous erasure, ADR phase 11
   decision 6). One short transaction:
   - **Crypto-shred**: delete the user's wrapped data key — every stored message and title
     is unreadable from that moment.
   - **Scrub** the email and password hash; mark the account deleted (`deleted_at`): the
     current token stops working at once and the email can be registered again.
   - Queue a **tombstone** (`pending`).
4. The page shows the server's message ("Account deleted. Your data is unreadable from now
   on; remaining encrypted rows are removed within 24 h and backup copies within 14 days.")
   and signs the user out; **Back to the home page** leads to Landing.
5. The **purger** (hourly Job on Azure, compose `purger` locally) deletes the remaining rows
   in small batches, leaf-first — messages → conversations → verification tokens → the user
   row — and marks the tombstone `done`. Restores replay the tombstones before reopening.

## 3. Route → auth matrix

| Route | Auth required | Notes |
|-------|---------------|-------|
| `/` | no | Landing |
| `/signup`, `/login`, `/verify` | no | Redirect to `/chat` if already authenticated |
| `/chat`, `/account` | yes | Redirect to `/login` if not authenticated |
| `POST /chat/stream`, `GET/PATCH/DELETE /conversations…` | yes | Chat is rate-limited (cost-aware) |
| `POST /auth/resend-verification`, `DELETE /account` | yes | |

## 4. Key states to design for

- Loading (retrieval/generation in progress), empty (no history yet), abstention, rate-limited (429),
  unverified-email, and error states — each screen must handle these explicitly.
