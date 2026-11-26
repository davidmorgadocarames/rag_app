// T14.5.4: pure, DOM-free formatting for the chat footer's per-answer token/cost line (C5
// pattern, same as lib/adminKpi.ts) -- "tokens in/out + estimated cost for the active
// provider; local Ollama = €0 / tokens only" (PHASE_PLANNING 14.5). The backend's own
// rag_app.cost.estimate_cost already guarantees `estimated_cost` is 0 for a local Ollama
// turn, whatever the token counts -- this module only ever formats what it is given, never
// decides pricing itself.

export interface TurnUsage {
  prompt_tokens: number;
  completion_tokens: number;
  estimated_cost: number;
  cost_currency: string;
}

const CURRENCY_SYMBOLS: Record<string, string> = { EUR: "€", USD: "$", GBP: "£" };

// Exported so the admin dashboard's aggregate cost line (lib/adminKpi.ts, T14.5.4) uses the
// IDENTICAL currency formatting as the chat footer, never a second, drifting copy.
export function currencySymbol(currency: string): string {
  return CURRENCY_SYMBOLS[currency] ?? `${currency} `;
}

export function formatCostFooter(usage: TurnUsage): string {
  const symbol = currencySymbol(usage.cost_currency);
  const costText =
    usage.estimated_cost > 0 ? `${symbol}${usage.estimated_cost.toFixed(4)}` : `${symbol}0`;
  return `${usage.prompt_tokens} in · ${usage.completion_tokens} out · ${costText}`;
}
