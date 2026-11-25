"""The shared understanding step (T14.2.1-T14.2.3): fast path, then the LLM router.

**Scope interpretation, self-reported (DA-13-J2-1 discipline):** ``:func:`understand``` is
the genuinely NEW piece T14.2.1 asks for -- before this block there was no shared
understanding step at all: ``generation.answer_question`` had none (every ``POST /chat`` ran
full RAG, even for "hi"), ``generation.answer_question_stream`` had its own inline
``classify_intent`` check, and the 14-A2 eval-runner placeholder re-implemented that same
binary check a THIRD time, independently. From this block, all three call ``understand()``
-- ONE implementation, not three.

The RETRIEVE/GENERATE/CHECK part deliberately stays where it already was
(``generation.answer_question``/``answer_question_stream``'s own bodies, ``eval.runner``'s own
direct calls to ``reranking.retrieve``/``generation.answer_from_chunks``) rather than being
relocated into this module too: those functions' tests monkeypatch module-qualified names
(``generation.hybrid_search``, ``generation.embed_query``) that only work because the calling
code lives textually inside ``generation.py`` -- moving it here would silently break every one
of those tests' patch target (the code would still run, against a REAL, unpatched
``hybrid_search``/``embed_query``, instead of failing loudly). Collapsing stream/non-stream/
eval into one single generator end-to-end is a larger refactor than named by T14.2.1-3's rows;
tracked as a follow-up, not silently dropped.

Memory (``summary``/``last_turn``, block 14-C1) is accepted as an optional parameter and is
read/written by the three entry points that have a conversation (DEC-059 option A).
Conversational handling of ``followup``/``ambiguous``/``off_topic`` (a clarifying question, a
polite redirect -- T14.3/14-D1) is not built yet: those three intents fall through to the SAME
retrieve-and-answer path as ``security`` today (the existing conservative default); only
``chitchat`` gets a different (LLM small-talk) reply in this block.

**T14.2.7 (block 14-C2):** retrieval now searches with the router's English
``standalone_query`` instead of the original (possibly non-English, possibly
reference-only -- "what about that?") message -- see :func:`retrieval_query` below, used
by all three entry points (``generation.answer_question``, ``generation.answer_question_stream``,
``eval.runner.evaluate_dialogues``, DEC-059 option A, same pattern 14-C1 already used for
memory). Generation itself still receives the ORIGINAL message, never the rewrite: the
answer must address what the user actually typed, in the language they typed it in
(D14.1) -- a concern T14.2.7 explicitly does not touch.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from rag_app.assistant.fast_path import match_fast_path
from rag_app.assistant.router import route
from rag_app.llm import ChatClient


@dataclass
class Understanding:
    """What :func:`understand` decided for one message (T14.2.1-T14.2.3)."""

    intent: str  # chitchat | security | followup | ambiguous | off_topic
    language: str  # en | es
    standalone_query: str
    clarification: str | None
    used_fast_path: bool
    # Wall time of the LLM router call ALONE; ``None`` when the fast path matched (no router
    # call happened to time) -- never 0.0 as a stand-in for "not measured".
    router_latency_s: float | None


def understand(
    chat: ChatClient,
    query: str,
    *,
    summary: str | None = None,
    last_turn: str | None = None,
) -> Understanding:
    """Fast path first (T14.2.2, EXACT greeting/thanks/farewell only); otherwise the LLM
    router (T14.2.3)."""
    fast_language = match_fast_path(query)
    if fast_language is not None:
        return Understanding(
            intent="chitchat",
            language=fast_language,
            standalone_query=query,
            clarification=None,
            used_fast_path=True,
            router_latency_s=None,
        )
    start = time.perf_counter()
    router_out = route(chat, query, summary=summary, last_turn=last_turn)
    router_latency_s = time.perf_counter() - start
    return Understanding(
        intent=router_out.intent,
        language=router_out.language,
        standalone_query=router_out.standalone_query,
        clarification=router_out.clarification,
        used_fast_path=False,
        router_latency_s=router_latency_s,
    )


def retrieval_query(decision: Understanding, query: str) -> str:
    """The query retrieval should search with (T14.2.7).

    Uses the router's English ``standalone_query`` when it is present and safe -- a
    genuine, non-empty rewrite -- so retrieval for a follow-up ("what about that?")
    searches with the resolved, self-contained English question instead of a pronoun that
    means nothing to hybrid search. Falls back to the original, raw ``query`` whenever
    ``standalone_query`` is empty/blank (a defensive guard; no live caller actually produces
    this today -- the exact fast path and the router's own ``_security_fallback`` both
    already set ``standalone_query == query`` by construction, so this branch exists for
    robustness, not because it is exercised in practice). Generation (the answer the user
    reads) is UNCHANGED by this function -- callers keep passing the original ``query`` to
    ``answer_from_chunks``/``_stream_answer_tokens`` so the reply still addresses what the
    user actually typed, in their own language (D14.1).
    """
    candidate = decision.standalone_query.strip()
    return candidate if candidate else query


# T14.2.9 (block 14-C3, DEC-062): the corpus's only three OWASP Top 10 editions -- not a
# generic "any 4-digit number" match, so an unrelated year mentioned in a question never
# triggers a filter that would wrongly exclude every corpus document.
_KNOWN_OWASP_EDITIONS = ("2017", "2021", "2025")
_OWASP_EDITION_RE = re.compile(r"\b(2017|2021|2025)\b")


def named_owasp_version(text: str) -> str | None:
    """The OWASP Top 10 edition explicitly named in ``text``, or ``None`` (T14.2.9, DEC-062
    option A's "owasp_version ... as a filter or boost when the user named an edition (never
    when not named)"). Deliberately reads the RESOLVED retrieval text (what
    :func:`retrieval_query` actually returns -- the router's own ``standalone_query`` when
    present, else the raw message), never the conversation summary's own
    ``ConversationSummary.owasp_version`` field directly: the router already folds
    summary/last-turn context into its rewrite (e.g. a terse "What about the 2025 edition?"
    follow-up resolves into a standalone_query that itself names "2025"), so checking the
    resolved text is both simpler AND safer than also consulting the summary -- a topic
    change several turns after an edition was once named can never leak a stale version into
    an unrelated query this way, because nothing in the CURRENT search text repeats it.
    """
    match = _OWASP_EDITION_RE.search(text)
    return match.group(1) if match else None
