"""LLM-as-judge for answer correctness (qwen, temperature 0).

The judge compares a candidate answer to an INDEPENDENT reference answer — not to
the retrieved context — so it catches answers that are faithful to stale context but
factually wrong. NOTE: an LLM judge must be validated against human labels before its
scores are trusted; see docs/DEFINITION_OF_DONE.md (Phase 4).
"""

from __future__ import annotations

from rag_app.config import get_settings
from rag_app.llm import Message, OllamaChat

_JUDGE_SYSTEM = (
    "You grade whether a CANDIDATE answer is factually correct according to the "
    "REFERENCE answer for the QUESTION. Ignore wording and extra detail; judge only "
    "factual agreement. Reply with exactly one word: CORRECT or INCORRECT."
)
# The verdict is one word; bounded like every other LLM call (DA-31b-3). Eval only, on
# the local Ollama judge — generous so a short preamble never truncates the verdict.
JUDGE_MAX_TOKENS = 64


def parse_verdict(raw: str) -> bool:
    upper = raw.upper()
    if "INCORRECT" in upper:
        return False
    return "CORRECT" in upper


def judge_correctness(chat: OllamaChat, question: str, reference: str, candidate: str) -> bool:
    """Return True if the candidate answer is judged factually correct.

    DA-11bB-1 (block C): ``eval/benchmark.py`` and ``eval/runner.py`` reuse ONE ``OllamaChat``
    for the whole golden-set loop, calling generate -> groundedness -> this judge back to
    back on the SAME resident qwen — exactly the "same process/keep-alive window" T11.3.4's
    reload-cost finding warns about, not a hypothetical future one. ``num_ctx`` is therefore
    pinned to ``num_ctx_answer`` (equal to ``num_ctx_groundedness``, config.py) so the judge
    never forces a reload before the next item's generate call.
    """
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
    return parse_verdict(
        chat.chat(
            messages,
            temperature=0.0,
            max_tokens=JUDGE_MAX_TOKENS,
            num_ctx=get_settings().num_ctx_answer,
            call_type="judge",
        )
    )
