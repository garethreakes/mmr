"""Integration test: ``backtest_correlation`` against ``BacktestStore``.

Verifies the round trip works end-to-end:

  1. Persist BacktestRecords with hand-crafted equity_curve_json blobs
     into a real (temp) DuckDB via the production BacktestStore writer.
  2. Read them back via ``store.get(run_id)``.
  3. Pass the curves through ``compute_correlation_matrix`` and assert
     the result aligns with the construction (perfect anti-correlation
     between the rising and falling curves; symmetric matrix; diagonal
     of 1.0; reasonable n_observations).

This catches schema-drift bugs that the pure unit tests don't —
specifically that the on-disk JSON shape we read back is the same shape
``compute_correlation_matrix`` expects to parse.
"""

import datetime as dt
import json

import pytest

from trader.data.backtest_correlation import compute_correlation_matrix
from trader.data.backtest_store import BacktestRecord, BacktestStore


def _equity_curve_json(items: list[tuple[str, float]]) -> str:
    """Build an equity_curve_json blob in the production shape (mirrors
    what ``trader/mmr_cli.py`` writes when a backtest is persisted)."""
    return json.dumps([
        {"timestamp": str(ts), "value": float(v)} for ts, v in items
    ])


def _make_record(
    class_name: str,
    equity_curve_json: str,
    total_return: float = 0.05,
) -> BacktestRecord:
    return BacktestRecord(
        strategy_path=f'strategies/{class_name.lower()}.py',
        class_name=class_name,
        conids=[265598],  # AAPL — value doesn't matter for correlation
        universe='',
        start_date=dt.datetime(2024, 1, 1),
        end_date=dt.datetime(2024, 12, 31),
        bar_size='1 day',
        initial_capital=100_000.0,
        fill_policy='next_open',
        slippage_bps=1.0,
        commission_per_share=0.005,
        params={},
        code_hash='deadbeef',
        total_trades=30,
        total_return=total_return,
        sharpe_ratio=1.0,
        max_drawdown=-0.05,
        win_rate=0.55,
        final_equity=100_000.0 * (1 + total_return),
        equity_curve_json=equity_curve_json,
    )


class TestStoreCorrelationRoundTrip:

    def test_two_anti_correlated_runs_round_trip(self, tmp_duckdb_path):
        # Build two curves whose daily returns are exact mirrors. After
        # a store→get round trip, the correlation should still be -1.0.
        n = 30
        a_pts: list[tuple[str, float]] = [("2024-01-01", 100_000.0)]
        b_pts: list[tuple[str, float]] = [("2024-01-01", 100_000.0)]
        eq_a = eq_b = 100_000.0
        for i in range(2, n + 2):
            r = ((i % 5) - 2) * 0.004  # non-constant alternating returns
            eq_a *= (1.0 + r)
            eq_b *= (1.0 - r)
            d = f"2024-01-{i:02d}" if i <= 31 else f"2024-02-{i - 31:02d}"
            a_pts.append((d, eq_a))
            b_pts.append((d, eq_b))

        store = BacktestStore(tmp_duckdb_path)
        rid_a = store.add(_make_record('RisingStrategy', _equity_curve_json(a_pts)))
        rid_b = store.add(_make_record('FallingStrategy', _equity_curve_json(b_pts)))

        # Read back from DuckDB — this exercises the column-position
        # parsing in BacktestStore that has historically been a bug
        # surface. If a column shifts, equity_curve_json comes back
        # empty / malformed and the correlation result will surface a
        # parse failure instead of a number.
        rec_a = store.get(rid_a)
        rec_b = store.get(rid_b)
        assert rec_a is not None and rec_a.equity_curve_json
        assert rec_b is not None and rec_b.equity_curve_json

        result = compute_correlation_matrix([
            ('a', rec_a.equity_curve_json),
            ('b', rec_b.equity_curve_json),
        ])
        assert result.matrix is not None, result.reason
        assert result.matrix[0][1] == pytest.approx(-1.0, abs=1e-6)
        assert result.n_observations == n

    def test_three_independent_runs_produce_well_formed_matrix(self, tmp_duckdb_path):
        # Three runs with deterministic, non-trivial return sequences —
        # we don't assert exact correlations (those depend on the chosen
        # noise pattern), only the shape of the result.
        n = 35
        store = BacktestStore(tmp_duckdb_path)
        for label, seed_offset in [('orb', 1), ('momentum', 7), ('pairs', 13)]:
            pts: list[tuple[str, float]] = [("2024-03-01", 100_000.0)]
            eq = 100_000.0
            for i in range(2, n + 2):
                # A trigonometric mix gives non-constant, well-behaved
                # returns. Using different seed_offsets produces curves
                # that aren't identical and aren't exact mirrors.
                from math import sin, cos
                r = 0.005 * sin((i + seed_offset) / 3.0) + 0.002 * cos((i + seed_offset) / 5.0)
                eq *= (1.0 + r)
                d = f"2024-03-{i:02d}" if i <= 31 else f"2024-04-{i - 31:02d}"
                pts.append((d, eq))
            store.add(_make_record(label, _equity_curve_json(pts)))

        recs = [store.get(rid) for rid in (1, 2, 3)]
        labelled = [(rec.class_name, rec.equity_curve_json) for rec in recs]
        result = compute_correlation_matrix(labelled)

        assert result.matrix is not None, result.reason
        assert len(result.matrix) == 3
        assert all(len(row) == 3 for row in result.matrix)
        # Diagonal exactly 1.0
        for i in range(3):
            assert result.matrix[i][i] == pytest.approx(1.0)
        # Symmetric
        for i in range(3):
            for j in range(3):
                assert result.matrix[i][j] == pytest.approx(result.matrix[j][i])
        # Off-diagonal correlations should be in (-1, 1)
        for i in range(3):
            for j in range(i + 1, 3):
                r = result.matrix[i][j]
                assert -1.0 < r < 1.0
        assert result.labels == ['orb', 'momentum', 'pairs']
        assert result.n_observations is not None and result.n_observations >= 20

    def test_run_without_equity_curve_surfaces_in_reason(self, tmp_duckdb_path):
        # A run persisted WITHOUT --save-trades has equity_curve_json=''.
        # The matrix builder should refuse and name the offending label
        # so the caller knows which run to re-run.
        n = 30
        good_pts = [(f"2024-01-{i:02d}", 100_000.0 * (1.001 ** i)) for i in range(1, n + 1)]
        store = BacktestStore(tmp_duckdb_path)
        store.add(_make_record('GoodStrategy', _equity_curve_json(good_pts)))
        store.add(_make_record('NoCurveStrategy', ''))

        recs = [store.get(1), store.get(2)]
        result = compute_correlation_matrix([
            (recs[0].class_name, recs[0].equity_curve_json),
            (recs[1].class_name, recs[1].equity_curve_json),
        ])
        assert result.matrix is None
        assert 'NoCurveStrategy' in (result.reason or '')
