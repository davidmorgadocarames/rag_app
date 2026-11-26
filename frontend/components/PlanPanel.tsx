"use client";

// T14.4.2 (block 14-D2): the chat UI's visible "plan" panel -- collapsed by default, with a
// live one-line status (the latest recognised step), expandable to the full transcript so
// far. No approved mockup exists for this panel (memory: "follow mockup exactly" ->
// "no mockup -> follow the existing chat/app style exactly") -- reuses WakeBanner.tsx's own
// design tokens (vault-amber for the live/current line, vault-steel-dark borders,
// vault-steel-light for past entries) and the SAME monospace typography the whole app
// already uses (frontend/app/globals.css's body font-family) -- no new colours/fonts.
//
// Accessibility: a plain, native <button> toggles `expanded` -- always keyboard-reachable
// (Tab to focus, Enter/Space to activate, both free with a real <button>), `aria-expanded`
// reflects the current state, `aria-controls` points at the expanded list's id so a screen
// reader announces what the control reveals. `prefers-reduced-motion`: this panel starts no
// animation/transition of its own (the expand/collapse is a plain conditional render, not a
// CSS transition) -- the project-wide `@media (prefers-reduced-motion: reduce)` rule in
// globals.css already neutralises every `animate-pulse`/transition utility elsewhere, so
// there is nothing extra to opt out of here.
//
// XSS: every `entry.label`/the current status text is rendered as a plain JSX text child
// (React's default escaping) -- NEVER `dangerouslySetInnerHTML`, never passed through a
// markdown/HTML renderer. The label may contain the user's own `standalone_query` text
// (T14.4.1), which is exactly why this matters -- see planSteps.test.tsx's own XSS-shaped
// test for the frontend-side proof.

import { useId, useState } from "react";

import type { PlanStepsState } from "@/lib/planSteps";

export default function PlanPanel({
  state,
  testId = "plan-panel",
}: {
  state: PlanStepsState;
  testId?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const listId = useId();

  if (state.entries.length === 0) return null;
  const current = state.entries[state.entries.length - 1];

  return (
    <div
      data-testid={testId}
      className="flex flex-col gap-1 border border-vault-steel-dark p-2.5 text-xs text-vault-steel"
    >
      <button
        type="button"
        data-testid={`${testId}-toggle`}
        aria-expanded={expanded}
        aria-controls={listId}
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full cursor-pointer items-center gap-2 text-left text-vault-amber outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-vault-amber"
      >
        <span aria-hidden="true">{expanded ? "▾" : "▸"}</span>
        <span className="flex-1 truncate" data-testid={`${testId}-current`}>
          {current.label}
        </span>
      </button>
      {expanded && (
        <ul
          id={listId}
          data-testid={`${testId}-list`}
          className="flex flex-col gap-0.5 border-t border-vault-steel-dark pt-2"
        >
          {state.entries.map((entry, i) => (
            <li
              key={`${entry.id}-${i}`}
              data-testid={`${testId}-entry-${entry.id}`}
              className={
                i === state.entries.length - 1 ? "text-vault-amber" : "text-vault-steel-light"
              }
            >
              {entry.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
