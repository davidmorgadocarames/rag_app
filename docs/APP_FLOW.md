# App Flow — SecRAG

This maps the user journey: the screens, what each does, what happens on each action, and which page
comes next.

## 1. Screen map

```
Landing (/)
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

1. User opens **`/login`**, enters credentials.
2. On **Sign in**:
   - Server checks **login rate limit** (token bucket) for the IP/account.
   - Verifies argon2 hash; requires `email_verified = true`.
   - Success → issues session/JWT → redirect to **`/chat`**.
   - Failure → generic error (no user enumeration); increment `login_attempts`.

### 2.4 Ask a question (core loop)

1. On **`/chat`**, user types a question and presses **Send**.
2. Client → `POST /api/chat`. Server:
   - Applies **per-user rate limit / quota** (cost-aware). Over limit → 429 with a friendly message.
   - Runs the RAG pipeline: route → retrieve → rerank → grade → generate → groundedness check.
3. Response rendering:
   - **Answered** → answer text + **citations** (doc title, version, effective date). Message pair
     saved to history.
   - **Abstained** → "I don't have a reliable source for that in the corpus." (no fabricated content).
   - **Error/timeout** → retry affordance; nothing partial is presented as authoritative.
4. User can ask follow-ups (same conversation) or start a new one.

### 2.5 History

1. **`/history`** lists the user's past conversations (most recent first).
2. Selecting one opens it read-only in the chat view.

### 2.6 Account & data deletion

1. **`/account`** shows email and account controls.
2. **Delete my data** → confirmation modal ("This is irreversible").
3. On confirm → `DELETE /api/account`:
   - **Crypto-shred**: destroy the user's encryption key.
   - Remove relational rows, **vectors**, **cache entries**, and **trace/log** references tied to the user.
   - Invalidate sessions → redirect to **Landing** with confirmation.

## 3. Route → auth matrix

| Route | Auth required | Notes |
|-------|---------------|-------|
| `/` | no | Landing |
| `/signup`, `/login`, `/verify` | no | Redirect to `/chat` if already authenticated |
| `/chat`, `/history`, `/account` | yes | Redirect to `/login` if not authenticated |
| `POST /api/chat`, `DELETE /api/account` | yes | Rate-limited |

## 4. Key states to design for

- Loading (retrieval/generation in progress), empty (no history yet), abstention, rate-limited (429),
  unverified-email, and error states — each screen must handle these explicitly.
