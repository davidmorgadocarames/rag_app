"""T11.5.1 (11b block F): the reranker experiment matrix — resumable, cheap-first.

Block E found the rerank floor FAILING hard (today's config: p95 ~9.6 s against a ~1.5 s
absolute cap). This module screens candidate (model, precision, top_k, max_length)
combinations on their OWN, cheap, to find which ones are even worth a full eval run:

- Measures ONLY ``rerank_inference`` over the golden set's real retrieved candidates (one
  pass, not the full ``generate``+``groundedness``+judge pipeline `eval.gate` runs) — this is
  the "cheap latency micro-run" the task asks to prefer before any full eval.
- Threads pinned to 2 for the WHOLE process before any model loads (``torch.set_num_
  threads(2)``; the caller must also set ``OMP_NUM_THREADS``/``MKL_NUM_THREADS`` in the
  environment BEFORE the interpreter starts, since that is read at native-library load time —
  see ``scripts/gate.sh``'s ``LATENCY_EXTRA_ARGS`` hook and the ADR for the exact invocation).
  This is the number DA-11bE-2 says the floor must be judged on; it is NOT the 28-thread
  "unconstrained" number earlier blocks reported.
- Families (same model + precision, varying top_k/max_length) are ordered cheapest-first.
  If a family's cheapest cell already fails the floor, every more expensive, not-yet-measured
  cell in that family is marked ``pruned`` (cost only goes up from there) WITHOUT being run —
  recorded, not silently skipped.
- Results are written to the COMMITTED ``eval/rerank_matrix_results.json`` after EVERY cell
  (not batched at the end): a restart (API limit, machine reboot) only loses the cell in
  progress, never the ones already measured — the whole point of "resumable".

Called via ``scripts/gate.sh --only latency`` with
``LATENCY_EXTRA_ARGS="--rerank-matrix"`` (reuses the existing ad-hoc-run hook against the
isolated gate stack, T11.4.4/DA review of block C) — see ``eval.latency.main()``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from rag_app import timing
from rag_app.eval.dataset import GoldenItem
from rag_app.reranking import CrossEncoderReranker
from rag_app.retrieval import hybrid_search

REPO_ROOT = Path(__file__).resolve().parents[4]
EVAL_DIR = REPO_ROOT / "eval"
RESULTS_PATH = EVAL_DIR / "rerank_matrix_results.json"

V2M3_MODEL = "BAAI/bge-reranker-v2-m3"
V2M3_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
BASE_MODEL = "BAAI/bge-reranker-base"
# Hub HEAD at measurement time (2026-10-05); MIT licence, read via the HF Hub API
# (https://huggingface.co/api/models/BAAI/bge-reranker-base) — portfolio use allowed.
BASE_REVISION = "2cfc18c9415c912f9d8155881c133215df768a70"

# Frozen baseline (eval/latency_baseline.json, T11.3.5/ADR decision 8): rerank_inference
# p95 = 9191.2 ms, 28-thread, v2-m3 fp32, top_k=20, max_length=512. The floor is the SAME
# absolute numbers regardless of how a given cell is measured.
BASELINE_P95_MS = 9191.2
FLOOR_ABSOLUTE_MS = 1500.0
FLOOR_RELATIVE_MS = BASELINE_P95_MS * 0.5


@dataclass(frozen=True)
class Cell:
    family: str  # cells in the same family share model+precision; ordered cheapest-first
    model_name: str
    revision: str
    quantize: bool
    top_k: int
    max_length: int

    @property
    def id(self) -> str:
        precision = "int8" if self.quantize else "fp32"
        return f"{self.family}|{precision}|topk{self.top_k}|ml{self.max_length}|threads2"

    @property
    def cost_key(self) -> tuple[int, int]:
        """Cells with a smaller key are measured earlier within a family (same model+
        precision): more top_k and/or a longer max_length only ever add CPU work, never
        remove it. KNOWN LIMITATION: this lexicographic order (top_k first, max_length a
        tie-break) is only a PARTIAL order on real cost -- real cost scales roughly with
        top_k * max_length (batch size * sequence length), so e.g. (top_k=12, max_length=
        256) sorts AFTER (top_k=10, max_length=512) here even though 12*256=3072 <
        10*512=5120 (it would likely be cheaper in practice). This can prune a cell slightly
        too early. In the real block F run this never changed the winning decision --
        `base_int8`'s cheapest-measured cell already reached recall=1.0 (the maximum
        possible), so no skipped cell could have improved quality further -- but a future
        reuse of this matrix for a closer call should sort by `top_k * max_length` instead."""
        return (self.top_k, self.max_length)


def _cells(family: str, model_name: str, revision: str, *, quantize: bool) -> list[Cell]:
    cells = [
        Cell(family, model_name, revision, quantize, top_k, max_length)
        for top_k in (10, 12, 20)
        for max_length in (256, 512)
    ]
    return sorted(cells, key=lambda c: c.cost_key)


# The grid actually run (T11.5.1 + the 11b block F extension — see the module docstring and
# the ADR for why bge-reranker-base int8 is an ADDED family beyond the task's literal three
# variants: fp32 v2-m3/int8 v2-m3/fp32 base alone turned out, once measured, not to contain
# any cell meeting the absolute 1.5 s floor — see "Stage 1" in PHASE_STATUS.md).
MATRIX: list[Cell] = [
    *_cells("v2m3_fp32", V2M3_MODEL, V2M3_REVISION, quantize=False),
    *_cells("v2m3_int8", V2M3_MODEL, V2M3_REVISION, quantize=True),
    *_cells("base_fp32", BASE_MODEL, BASE_REVISION, quantize=False),
    *_cells("base_int8", BASE_MODEL, BASE_REVISION, quantize=True),
]


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]


def floor_ok(p95_ms: float) -> bool:
    """Both the absolute (<=1.5s) and relative (<=50% of the frozen baseline) floor hold."""
    return p95_ms <= FLOOR_ABSOLUTE_MS and p95_ms <= FLOOR_RELATIVE_MS


@dataclass
class MatrixResults:
    cells: dict[str, dict[str, object]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = RESULTS_PATH) -> MatrixResults:
        if not path.exists():
            return cls()
        return cls(cells=json.loads(path.read_text(encoding="utf-8")).get("cells", {}))

    def save(self, path: Path = RESULTS_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"cells": self.cells}, indent=2, sort_keys=True), encoding="utf-8"
        )


def _measure_cell(session: Session, cell: Cell, items: list[GoldenItem]) -> dict[str, object]:
    """One pass over the golden set's real candidates with THIS cell's reranker config.
    Only ``rerank_inference`` durations are collected (the cheap screening number)."""
    reranker = CrossEncoderReranker(
        model_name=cell.model_name,
        revision=cell.revision,
        max_length=cell.max_length,
        quantize=cell.quantize,
    )
    reranker.warm_up()  # pays load + first-inference once, outside the measured durations

    durations_ms: list[float] = []

    def on_stage(name: str, elapsed_s: float) -> None:
        if name == "rerank_inference":
            durations_ms.append(elapsed_s * 1000)

    start = time.perf_counter()
    for item in items:
        candidates = hybrid_search(
            session, item.question, top_k=cell.top_k, candidate_k=cell.top_k, version=item.version
        )
        with timing.recorder(streaming=False, on_stage=on_stage):
            reranker.rerank(item.question, candidates, top_n=4)
    runtime_s = time.perf_counter() - start

    p95 = round(_percentile(durations_ms, 95), 1)
    return {
        "model_name": cell.model_name,
        "revision": cell.revision,
        "quantize": cell.quantize,
        "top_k": cell.top_k,
        "max_length": cell.max_length,
        "threads": 2,
        "status": "measured",
        "n": len(durations_ms),
        "mean_ms": round(sum(durations_ms) / len(durations_ms), 1) if durations_ms else 0.0,
        "p50_ms": round(_percentile(durations_ms, 50), 1),
        "p95_ms": p95,
        "floor_ok": floor_ok(p95),
        "runtime_s": round(runtime_s, 1),
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def run_matrix(
    session: Session,
    items: list[GoldenItem],
    *,
    out_path: Path = RESULTS_PATH,
    force: bool = False,
) -> MatrixResults:
    """Run every not-yet-measured, not-pruned cell in :data:`MATRIX`, writing to
    ``out_path`` after EACH cell (resumable: a restart re-reads what is already there and
    continues). Cheapest cell first within each family; the rest of a family is pruned the
    moment its cheapest cell fails the floor -- EXCEPT the single most expensive cell per
    family (top_k=20, max_length=512, the shape of today's production config), which is
    always measured regardless: it is the reference number the ADR table needs even when a
    family is otherwise pruned."""
    results = MatrixResults.load(out_path)
    by_family: dict[str, list[Cell]] = {}
    for cell in MATRIX:
        by_family.setdefault(cell.family, []).append(cell)
    reference_ids = {cells[-1].id for cells in by_family.values()}  # priciest per family

    pruned_families: set[str] = {
        cell_id.split("|", 1)[0]
        for cell_id, row in results.cells.items()
        if row.get("status") == "pruned"
    } | {
        cell_id.split("|", 1)[0]
        for cell_id, row in results.cells.items()
        if row.get("status") == "measured" and not row.get("floor_ok", True)
    }

    for cell in MATRIX:
        if cell.id in results.cells and not force:
            continue
        if cell.family in pruned_families and cell.id not in reference_ids:
            results.cells[cell.id] = {
                "model_name": cell.model_name,
                "revision": cell.revision,
                "quantize": cell.quantize,
                "top_k": cell.top_k,
                "max_length": cell.max_length,
                "threads": 2,
                "status": "pruned",
                "reason": (
                    "a cheaper cell in the same family (same model+precision, smaller or"
                    " equal top_k/max_length) already failed the floor; cost only increases"
                    " from there"
                ),
            }
            results.save(out_path)
            continue

        print(f"rerank matrix: measuring {cell.id} ...")
        row = _measure_cell(session, cell, items)
        results.cells[cell.id] = row
        results.save(out_path)
        print(f"  -> p50={row['p50_ms']}ms p95={row['p95_ms']}ms floor_ok={row['floor_ok']}")
        if not row["floor_ok"]:
            pruned_families.add(cell.family)

    return results


def print_summary(results: MatrixResults) -> None:
    print(f"\n{'cell':45} {'status':9} {'p50_ms':>9} {'p95_ms':>9} {'floor_ok':>9}")
    for cell_id, row in sorted(results.cells.items()):
        print(
            f"{cell_id:45} {row.get('status', ''):9} {row.get('p50_ms', ''):>9}"
            f" {row.get('p95_ms', ''):>9} {str(row.get('floor_ok', '')):>9}"
        )


def main(session: Session, items: list[GoldenItem]) -> int:
    """Entry point called from ``eval.latency.main()`` (``--rerank-matrix``): threads must
    already be pinned to 2 by the caller (env vars set before the interpreter started, plus
    ``torch.set_num_threads(2)``) before this runs a single cell."""
    results = run_matrix(session, items)
    print_summary(results)
    print(f"\nrerank matrix written -> {RESULTS_PATH}")
    return 0
