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

Memory (``summary``/``last_turn``, block 14-C1) is accepted as an optional parameter but is
always ``None`` from every 14-B2 call site. Retrieval still uses the ORIGINAL message, never
``standalone_query`` (T14.2.7, block 14-C2) -- the router already produces it, nothing
downstream consumes it yet. Conversational handling of ``followup``/``ambiguous``/``off_topic``
(a clarifying question, a polite redirect -- T14.3/14-D1) is not built yet: those three intents
fall through to the SAME retrieve-and-answer path as ``security`` today (the existing
conservative default); only ``chitchat`` gets a different (LLM small-talk) reply in this block.
"""

from __future__ import annotations

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
