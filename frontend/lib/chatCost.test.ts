import assert from "node:assert/strict";
import { test } from "node:test";

import { formatCostFooter } from "./chatCost.ts";

test("formatCostFooter shows tokens in/out and €0 for a free local Ollama turn", () => {
  const text = formatCostFooter({
    prompt_tokens: 1234,
    completion_tokens: 56,
    estimated_cost: 0,
    cost_currency: "EUR",
  });
  assert.equal(text, "1234 in · 56 out · €0");
});

test("formatCostFooter shows a real estimate for a priced Azure OpenAI turn", () => {
  const text = formatCostFooter({
    prompt_tokens: 2000,
    completion_tokens: 1000,
    estimated_cost: 0.004,
    cost_currency: "EUR",
  });
  assert.equal(text, "2000 in · 1000 out · €0.0040");
});

test("formatCostFooter falls back to the raw currency code for an unknown currency", () => {
  const text = formatCostFooter({
    prompt_tokens: 10,
    completion_tokens: 5,
    estimated_cost: 0.1,
    cost_currency: "JPY",
  });
  assert.equal(text, "10 in · 5 out · JPY 0.1000");
});

test("formatCostFooter never shows a negative cost as free-looking zero text by accident", () => {
  // Defensive: estimate_cost never returns a negative amount in practice, but the ">0" check
  // must not treat 0 and "effectively 0" the same as a hidden negative would look like.
  const text = formatCostFooter({
    prompt_tokens: 1,
    completion_tokens: 1,
    estimated_cost: 0,
    cost_currency: "USD",
  });
  assert.equal(text, "1 in · 1 out · $0");
});
