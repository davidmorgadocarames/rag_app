"""T11.5.1 (11b block F): the experiment matrix runner -- pruning + resumability, no torch/
model loading (``_measure_cell`` is monkeypatched to a cheap fake everywhere here)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_app.eval import rerank_matrix as rm


def test_floor_ok_boundaries() -> None:
    assert rm.floor_ok(1500.0) is True  # exactly at the absolute cap
    assert rm.floor_ok(1500.1) is False
    # the relative floor (50% of the frozen baseline) is looser than 1.5s for THIS baseline,
    # so the absolute cap is always the binding constraint -- guard that assumption explicitly
    # (a value between the two caps must still fail, via the relative check, if it existed
    # alone -- but since the absolute cap is tighter here, it is what actually rejects it).
    assert rm.FLOOR_RELATIVE_MS > rm.FLOOR_ABSOLUTE_MS
    assert rm.floor_ok(rm.FLOOR_RELATIVE_MS) is False  # fails the (tighter) absolute cap


def test_cells_within_a_family_are_sorted_cheapest_first() -> None:
    cells = rm._cells("x", "m", "r", quantize=False)
    costs = [c.cost_key for c in cells]
    assert costs == sorted(costs)
    assert cells[0].cost_key == (10, 256)  # cheapest: smallest top_k, smallest max_length
    assert cells[-1].cost_key == (20, 512)  # reference: today's production shape


def _fake_measure(latencies: dict[str, float]):  # noqa: ANN202 - test helper
    calls: list[str] = []

    def _measure(session: object, cell: rm.Cell, items: list[object]) -> dict[str, object]:
        calls.append(cell.id)
        p95 = latencies[cell.id]
        return {
            "model_name": cell.model_name,
            "revision": cell.revision,
            "quantize": cell.quantize,
            "top_k": cell.top_k,
            "max_length": cell.max_length,
            "threads": 2,
            "status": "measured",
            "n": 14,
            "mean_ms": p95,
            "p50_ms": p95,
            "p95_ms": p95,
            "floor_ok": rm.floor_ok(p95),
            "runtime_s": 1.0,
            "measured_at": "2026-10-05T00:00:00Z",
        }

    return _measure, calls


def test_a_failing_cheapest_cell_prunes_the_middle_but_still_measures_the_reference_cell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Family v2m3_fp32's cheapest cell (10, 256) fails the floor in this fake -> every
    OTHER cell in that family is pruned EXCEPT the (20, 512) reference, which is still
    measured for the ADR table regardless of pass/fail."""
    latencies = {cell.id: 9999.0 for cell in rm.MATRIX}  # everything "fails" by default
    fake_measure, calls = _fake_measure(latencies)
    monkeypatch.setattr(rm, "_measure_cell", fake_measure)

    out_path = tmp_path / "rerank_matrix_results.json"
    results = rm.run_matrix(session=object(), items=[], out_path=out_path, force=False)  # type: ignore[arg-type]

    v2m3_cells = {cid: row for cid, row in results.cells.items() if cid.startswith("v2m3_fp32|")}
    assert len(v2m3_cells) == 6  # every cell gets a recorded status, one way or another
    statuses = {cid: row["status"] for cid, row in v2m3_cells.items()}
    measured = {cid for cid, s in statuses.items() if s == "measured"}
    pruned = {cid for cid, s in statuses.items() if s == "pruned"}
    assert len(pruned) == 4  # the 4 "middle" cells, never even run
    assert len(measured) == 2  # cheapest (always run first) + reference (exempt from pruning)
    reference_id = next(
        c.id for c in rm.MATRIX if c.family == "v2m3_fp32" and c.top_k == 20 and c.max_length == 512
    )
    assert reference_id in measured
    # pruned cells were never actually measured (never reached _measure_cell)
    assert set(calls) & pruned == set()
    assert out_path.exists()


def test_a_passing_family_is_measured_in_full_no_pruning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When even the cheapest cell passes, nothing in that family is pruned -- every cell is
    measured, since interior cells may still differ meaningfully on quality/latency trade-off
    (this is exactly base_int8's real situation, block F's winning family)."""
    latencies = {cell.id: 100.0 for cell in rm.MATRIX}  # everything "passes"
    fake_measure, calls = _fake_measure(latencies)
    monkeypatch.setattr(rm, "_measure_cell", fake_measure)

    out_path = tmp_path / "rerank_matrix_results.json"
    results = rm.run_matrix(session=object(), items=[], out_path=out_path, force=False)  # type: ignore[arg-type]

    base_int8_cells = [cid for cid in results.cells if cid.startswith("base_int8|")]
    assert len(base_int8_cells) == 6
    assert all(results.cells[cid]["status"] == "measured" for cid in base_int8_cells)
    assert set(base_int8_cells).issubset(calls)


def test_resuming_skips_already_measured_cells(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A restart must not re-measure a cell that is already in the results file -- the whole
    point of resumability (API limits / machine restarts mid-matrix)."""
    latencies = {cell.id: 9999.0 for cell in rm.MATRIX}
    fake_measure, calls = _fake_measure(latencies)
    monkeypatch.setattr(rm, "_measure_cell", fake_measure)
    out_path = tmp_path / "rerank_matrix_results.json"

    rm.run_matrix(session=object(), items=[], out_path=out_path, force=False)  # type: ignore[arg-type]
    first_call_count = len(calls)
    assert first_call_count > 0

    calls.clear()
    results_again = rm.run_matrix(session=object(), items=[], out_path=out_path, force=False)  # type: ignore[arg-type]

    assert calls == []  # nothing was re-measured on the second (resumed) run
    assert len(results_again.cells) == len(rm.MATRIX)  # every cell still accounted for


def test_force_re_measures_even_completed_cells(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    latencies = {cell.id: 9999.0 for cell in rm.MATRIX}
    fake_measure, calls = _fake_measure(latencies)
    monkeypatch.setattr(rm, "_measure_cell", fake_measure)
    out_path = tmp_path / "rerank_matrix_results.json"

    rm.run_matrix(session=object(), items=[], out_path=out_path, force=False)  # type: ignore[arg-type]
    calls.clear()
    rm.run_matrix(session=object(), items=[], out_path=out_path, force=True)  # type: ignore[arg-type]
    assert len(calls) > 0  # --force actually re-ran cells, not just read the cache


def test_matrix_results_round_trip_through_json(tmp_path: Path) -> None:
    out_path = tmp_path / "rerank_matrix_results.json"
    results = rm.MatrixResults(cells={"a|fp32|topk10|ml256|threads2": {"status": "measured"}})
    results.save(out_path)
    reloaded = rm.MatrixResults.load(out_path)
    assert reloaded.cells == results.cells


def test_matrix_results_load_missing_file_is_empty(tmp_path: Path) -> None:
    results = rm.MatrixResults.load(tmp_path / "does-not-exist.json")
    assert results.cells == {}
