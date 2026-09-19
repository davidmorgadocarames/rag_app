# UI/UX Design Brief — SecRAG

## 1. Look & feel

Calm, trustworthy, and technical — a **security tool**, not a playful chatbot. Clean layout, generous
whitespace, strong typographic hierarchy, and **citations treated as first-class UI**. The emotional
goal: the user should feel the answers are *sourced and honest*.

Principles:
- **Sources are visible, always.** Every answer shows where it came from.
- **Honesty over confidence.** Abstention is a designed, dignified state — not an error.
- **Accessible by default.** WCAG AA contrast, keyboard navigable, respects reduced motion.
- **Light and dark** themes both supported.

## 2. Color palette

Neutral slate base with a single trustworthy blue accent and clear semantic colors. (Final hex values
tuned during implementation; these are the intended roles.)

| Role | Light | Dark |
|------|-------|------|
| Background | `#ffffff` | `#0f172a` (slate-950) |
| Surface / card | `#f8fafc` | `#1e293b` (slate-800) |
| Text primary | `#0f172a` | `#f1f5f9` |
| Text secondary | `#475569` | `#94a3b8` |
| Accent (actions, links) | `#2563eb` (blue-600) | `#3b82f6` (blue-500) |
| Success (answered) | `#059669` | `#10b981` |
| Warning (abstention) | `#d97706` | `#f59e0b` |
| Danger (delete/errors) | `#dc2626` | `#ef4444` |
| Border | `#e2e8f0` | `#334155` |

Accent is used sparingly — primarily for primary actions and links. Citations use a subtle surface
tint, not the accent, so they read as evidence rather than buttons.

## 3. Typography

- **UI / body**: Inter (system-ui fallback). Base 16px, line-height 1.6.
- **Code / citations metadata**: a monospace stack (ui-monospace, SFMono, Menlo).
- **Scale**: `text-4xl` page titles, `text-lg` intro, `text-base` body, `text-sm` metadata.
- Weight: 600–700 for headings, 400–500 for body.

## 4. Components

- **Buttons**: primary (accent fill), secondary (outline), destructive (danger). Clear focus ring.
- **Input / textarea**: rounded, visible focus, inline validation messages.
- **Message bubble**: user vs assistant; assistant messages contain the answer body + a **citations
  block**.
- **Citation chip**: `doc title · version · effective date`, monospace metadata, links to the source
  section. This is the signature component.
- **Abstention card**: warning-toned, calm copy ("No reliable source in the corpus"), never styled as a
  failure.
- **Rate-limit / quota banner**: informative, non-alarming.
- **Confirmation modal**: used for irreversible actions (data deletion), danger-toned confirm button.
- **Empty states**: history empty, first-time chat.
- **Loading**: skeleton / typing indicator while retrieval+generation run.

## 5. Screen sketches (rough)

**Chat (`/chat`)**
```
┌───────────────────────────────────────────────┐
│  SecRAG            History   Account   [avatar] │
├───────────────────────────────────────────────┤
│                                                 │
│  ▸ user:  How do I prevent SQL injection?       │
│                                                 │
│  ▸ assistant:                                   │
│    Use parameterized queries … [answer body]    │
│    ┌───────────────────────────────────────┐   │
│    │ 📄 OWASP A03 Injection · 2021 · 2021-09 │   │
│    └───────────────────────────────────────┘   │
│                                                 │
├───────────────────────────────────────────────┤
│  [ Ask a security question…            ] [Send] │
└───────────────────────────────────────────────┘
```

**Abstention**
```
┌───────────────────────────────────────────────┐
│ ⚠ No reliable source in the corpus              │
│ I couldn't find this in the OWASP material I    │
│ have. I won't guess. Try rephrasing or ask      │
│ about an OWASP topic.                            │
└───────────────────────────────────────────────┘
```

**Auth screens (`/signup`, `/login`, `/verify`)**: single centered card, minimal fields, clear primary
action, secondary link to the other flow.

**Account (`/account`)**: email + a clearly separated, danger-zoned "Delete my data" section.

## 6. Responsiveness & motion

- Mobile-first; chat input pinned to the bottom; content column max-width ~720px on desktop.
- Subtle transitions only; honor `prefers-reduced-motion`.
