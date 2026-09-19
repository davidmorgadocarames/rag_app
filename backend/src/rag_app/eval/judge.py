"""LLM-as-judge for answer correctness (qwen, temperature 0).

The judge compares a candidate answer to an INDEPENDENT reference answer — not to
the retrieved context — so it catches answers that are faithful to stale context but
factually wrong. NOTE: an LLM judge must be validated against human labels before its
scores are trusted; see docs/DEFINITION_OF_DONE.md (Phase 4).
"""

from __future__ import annotations

from rag_app.llm import Message, OllamaChat

_JUDGE_SYSTEM = (
    "You grade whether a CANDIDATE answer is factually correct according to the "
    "REFERENCE answer for the QUESTION. Ignore wording and extra detail; judge only "
    "factual agreement. Reply with exactly one word: CORRECT or INCORRECT."
)


def parse_verdict(raw: str) -> bool:
    upper = raw.upper()
    if "INCORRECT" in upper:
        return False
    return "CORRECT" in upper


def judge_correctness(chat: OllamaChat, question: str, reference: str, candidate: str) -> bool:
    """Return True if the candidate answer is judged factually correct."""
    user = (
        f"QUESTION:\n{question}\n\n"
        f"REFERENCE:\n{reference}\n\n"
        f"CANDIDATE:\n{candidate}\n\n"
        "Is the CANDIDATE factually correct per the REFERENCE? CORRECT or INCORRECT."
    )
    messages: list[Message] = [
        {"role": "system", "content": _JUDGE_SYSTEM},
        {"role": "user", "content": user},
    ]
    return parse_verdict(chat.chat(messages, temperature=0.0))
