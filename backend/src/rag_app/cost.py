"""Token-cost estimation per answer (T14.5.4).

Local Ollama is always free -- no ``Settings`` field could ever make it otherwise; this
module never even looks at ``azure_openai_cost_per_1k_*`` for any provider other than
``"azure_openai"``. Azure OpenAI's per-1,000-token prices are plain ``Settings`` fields with
a documented, deliberately approximate default (see ``config.Settings``'s own comment) --
**never** read from the private ``status/azure_prices.json`` file: that file is private
planning material outside this repo and must never be opened by the running application
(PHASE_PLANNING 14.5 / the ADR's own rule). An operator overrides the defaults via env vars
for an accurate estimate; a wrong default only ever skews the chat footer/admin dashboard's
own ESTIMATE, never a real Azure invoice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rag_app.config import Settings


@dataclass(frozen=True)
class CostEstimate:
    amount: float
    currency: str


def estimate_cost(
    provider: str, prompt_tokens: int, completion_tokens: int, settings: Settings
) -> CostEstimate:
    """``amount`` is always ``0.0`` for any ``provider`` other than ``"azure_openai"`` --
    today that is only ``"ollama"``, always local, always free, never a pricing question."""
    if provider != "azure_openai":
        return CostEstimate(amount=0.0, currency=settings.cost_currency)
    amount = (prompt_tokens / 1000) * settings.azure_openai_cost_per_1k_input_tokens + (
        completion_tokens / 1000
    ) * settings.azure_openai_cost_per_1k_output_tokens
    return CostEstimate(amount=round(amount, 6), currency=settings.cost_currency)
