"use client";

// T13.2.5/T13.8.7 (13-G3): the Pipeline section's average waterfall -- one row per UI pipeline
// stage (classifying/retrieving/reranking/generating/checking/responding, in the backend's
// own STEP_ALLOWLIST order), expandable to its own atomic steps, each bar starting where the
// previous one ends (DA-13-G1-1: a stage's total is ALWAYS the sum of its own steps, read from
// `pipeline`, NEVER reconciled against the separate `stages` field for the same name -- see
// adminPipeline.ts's own docstring and the ADR "Pipeline waterfall semantics" section). No
// Recharts here any more: every bar below is a plain absolutely-positioned `<span>` with an
// inline `style` (same CSP-exercising "real inline style" the security-headers e2e spec walks
// this section for, DEC-036).

import { useState } from "react";

import type { AdminLatencyResponse } from "@/lib/api";
import { buildPipelineWaterfall, waterfallBarPercent, type WaterfallStage } from "@/lib/adminPipeline";

interface LatencyPanelProps {
  latency: AdminLatencyResponse | null;
}

// Presentational-only copy (not logic, not unit-tested): a short, human description of what
// each atomic step actually measures, in the same pipeline order the backend returns them.
// Step ids are unique across every stage (metrics.STEP_ALLOWLIST), so one flat lookup suffices.
const STEP_WHAT: Record<string, string> = {
  classify_intent: "intent classifier (RAG / chit-chat / abstain)",
  embed_query: "query embedding model",
  vector_search: "pgvector nearest neighbours",
  bm25_search: "full-text (BM25) search",
  rrf_fusion: "reciprocal rank fusion",
  load_chunks: "fetch chunk text + metadata",
  wait_slot: "queue for a rerank slot (concurrency cap)",
  cross_encoder: "tokenise + cross-encoder inference",
  order_by_scores: "sort + keep top_n",
  build_prompt: "context + citations prompt",
  llm_first_token: "LLM time to first token",
  token_stream: "stream the remaining tokens (non-streaming answers never record this step)",
  parse_answer: "citations + abstention parse",
  build_check_prompt: "answer + context prompt",
  llm_verdict: "groundedness LLM call",
  parse_verdict: "GROUNDED / NOT_GROUNDED parse",
  stream_canned_reply: "chit-chat path only -- no retrieval/rerank/generation call",
};

function fmt(seconds: number | null): string {
  return seconds === null ? "n/a" : `${seconds.toFixed(2)}s`;
}

function StageRow({
  stage,
  totalSeconds,
  expanded,
  onToggle,
}: {
  stage: WaterfallStage;
  totalSeconds: number;
  expanded: boolean;
  onToggle: () => void;
}) {
  const bar = waterfallBarPercent(stage.startSeconds, stage.totalAvgSeconds, totalSeconds);
  return (
    <div
      data-testid={`pipeline-stage-${stage.stage}`}
      className="flex flex-col gap-1 border-b border-vault-line/70 py-1.5"
    >
      <button
        type="button"
        data-testid={`pipeline-toggle-${stage.stage}`}
        aria-expanded={expanded}
        onClick={onToggle}
        className="flex w-full flex-wrap items-center gap-2 text-left"
      >
        <span className="flex w-40 flex-shrink-0 items-center gap-1.5 font-mono text-xs font-semibold text-vault-text-strong">
          <span
            aria-hidden="true"
            className={`inline-block w-3 text-vault-amber transition-transform ${expanded ? "rotate-90" : ""}`}
          >
            &rsaquo;
          </span>
          {stage.stage}
          {stage.drawnFromZero && (
            <span className="font-sans text-[10px] font-normal text-vault-text-muted">(chit-chat)</span>
          )}
        </span>
        <span className="relative h-5 min-w-[7rem] flex-1 rounded border border-vault-line bg-vault-bg">
          <span
            className="absolute inset-y-0 rounded bg-vault-steel"
            style={{ left: `${bar.leftPercent}%`, width: `${bar.widthPercent}%` }}
          />
        </span>
        <span className="w-32 flex-shrink-0 text-right font-mono text-xs text-vault-text-strong">
          {fmt(stage.totalAvgSeconds)}{" "}
          <span className="text-vault-text-muted">&middot; p95 {fmt(stage.totalP95Seconds)}</span>
        </span>
      </button>

      {expanded && (
        <div className="flex flex-col gap-1 pl-5">
          {stage.steps.map((step) => {
            const stepBar = waterfallBarPercent(step.startSeconds, step.avgSeconds, totalSeconds);
            return (
              <div
                key={step.step}
                data-testid={`pipeline-step-${stage.stage}-${step.step}`}
                className="flex flex-wrap items-center gap-2"
              >
                <span className="flex w-40 flex-shrink-0 flex-col">
                  <span className="font-mono text-xs text-vault-steel-light">{step.step}</span>
                  <span className="text-[11px] text-vault-text-muted">
                    {STEP_WHAT[step.step] ?? ""}
                  </span>
                </span>
                <span className="relative h-2 min-w-[7rem] flex-1">
                  <span
                    className={`absolute inset-y-0 rounded ${step.isSlowest ? "bg-vault-amber" : "bg-vault-steel-dark"}`}
                    style={{ left: `${stepBar.leftPercent}%`, width: `${stepBar.widthPercent}%` }}
                  />
                </span>
                <span className="w-32 flex-shrink-0 text-right font-mono text-xs text-vault-steel-light">
                  {step.observed ? fmt(step.avgSeconds) : "n/a"}{" "}
                  <span className="text-vault-text-muted">
                    &middot; p95 {step.observed ? fmt(step.p95Seconds) : "n/a"}
                  </span>
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function LatencyPanel({ latency }: LatencyPanelProps) {
  const pipeline = latency?.pipeline ?? [];
  const waterfall = buildPipelineWaterfall(pipeline);
  const [openMap, setOpenMap] = useState<Record<string, boolean>>({});
  const isOpen = (stageName: string) => openMap[stageName] ?? true; // default: expanded
  const setAll = (value: boolean) => {
    const next: Record<string, boolean> = {};
    for (const s of waterfall.stages) next[s.stage] = value;
    setOpenMap(next);
  };

  // "total" (timing.py's recorder.emit()) counts every chat turn, RAG or chit-chat alike --
  // the one authoritative chat count, never derived from the pipeline sum (DA-13-G1-1).
  const chatCount = latency?.stages.find((s) => s.stage === "total")?.count ?? null;

  return (
    <section
      data-testid="admin-latency-panel"
      aria-label="Chat pipeline latency"
      className="flex flex-col gap-3 rounded-lg border border-vault-line bg-vault-surface p-4"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm tracking-[0.18em] text-vault-text-strong">
          CHAT PIPELINE &middot; AVERAGE PER STAGE
        </h2>
        <span className="text-xs text-vault-text-muted">
          This replica since start &middot;{" "}
          <span className="font-mono text-vault-steel-light">{chatCount ?? "n/a"}</span> chats &middot;
          not per-request (per-request traces: 16-F)
        </span>
      </div>

      {!waterfall.hasAnyData || pipeline.length === 0 ? (
        <p data-testid="pipeline-empty-state" className="text-xs text-vault-text-muted">
          No chat turns observed yet on this replica -- the waterfall fills in as real chats
          happen.
        </p>
      ) : (
        <>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              data-testid="pipeline-expand-all"
              onClick={() => setAll(true)}
              className="cursor-pointer rounded border border-vault-steel-dark px-3 py-1 text-xs tracking-[0.1em] text-vault-amber-bright hover:border-vault-steel"
            >
              Expand all
            </button>
            <button
              type="button"
              data-testid="pipeline-stages-only"
              onClick={() => setAll(false)}
              className="cursor-pointer rounded border border-vault-steel-dark px-3 py-1 text-xs tracking-[0.1em] text-vault-steel-light hover:border-vault-steel"
            >
              Stages only
            </button>
          </div>

          <div className="flex flex-col">
            {waterfall.stages.map((stage) => (
              <StageRow
                key={stage.stage}
                stage={stage}
                totalSeconds={waterfall.totalAvgSeconds}
                expanded={isOpen(stage.stage)}
                onToggle={() => setOpenMap((prev) => ({ ...prev, [stage.stage]: !isOpen(stage.stage) }))}
              />
            ))}
          </div>

          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-vault-line pt-2 text-xs text-vault-text-muted">
            <span>
              Average waterfall: each bar starts where the previous step ends. Amber = the
              single slowest observed step. &quot;responding&quot; is the chit-chat path only, so
              it starts at 0 and is not part of the RAG total. Non-streaming answers never
              record a <code className="font-mono">token_stream</code> step.
            </span>
            <span className="font-mono text-vault-text-strong">
              RAG path total avg {fmt(waterfall.totalAvgSeconds)}
            </span>
          </div>
        </>
      )}
    </section>
  );
}
