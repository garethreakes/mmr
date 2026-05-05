"""Tests for ``trader.data.backtest_correlation``.

These exercise the pure correlation computation independent of DuckDB
and the CLI wrapper. The tests are deliberately about *behaviour and
shape* (parsing, alignment, symmetry, monotonicity, edge cases) rather
than literal correlation values when the math libraries aren't fixed —
the small Pearson implementation is exact, but we only assert exact
values for hand-crafted curves where the answer is provably 1.0 / -1.0
/ 0.0.
"""

import json
import math

import pytest

from trader.data.backtest_correlation import (
    CorrelationResult,
    DEFAULT_MIN_OBSERVATIONS,
    _clip_to_period,
    _date_part,
    _pearson,
    compute_correlation_matrix,
    daily_returns,
    parse_equity_curve_json,
    to_daily_close_series,
)


# ---------------------------------------------------------------------------
# Helpers — generate equity_curve_json strings the way backtester.py does
# ---------------------------------------------------------------------------

def _curve(*items) -> str:
    """Build an equity_curve_json blob from (timestamp, value) tuples."""
    return json.dumps([
        {"timestamp": str(ts), "value": float(v)} for ts, v in items
    ])


def _walk(start: float, returns_by_date: dict[str, float]) -> list[tuple[str, float]]:
    """Build a (date, equity) walk from a start value + a dict of daily
    returns keyed by date. Used to construct curves whose pairwise
    correlation has a known closed-form answer."""
    out: list[tuple[str, float]] = [(min(returns_by_date), start / (1 + returns_by_date[min(returns_by_date)]))]
    days = sorted(returns_by_date)
    eq = start
    out = []
    # Seed: include a "day before" the first dated return at value `start`
    # so the first dated return resolves to (start * (1+r0)) on day 0.
    seed_date = "0000-00-00"  # sorts first lexicographically
    out.append((seed_date, start))
    for d in days:
        eq = eq * (1.0 + returns_by_date[d])
        out.append((d, eq))
    return out


# ---------------------------------------------------------------------------
# parse_equity_curve_json
# ---------------------------------------------------------------------------

class TestParseEquityCurveJson:

    def test_empty_string_returns_empty(self):
        assert parse_equity_curve_json("") == []

    def test_well_formed(self):
        blob = _curve(("2024-01-02", 100000.0), ("2024-01-03", 100500.0))
        assert parse_equity_curve_json(blob) == [
            ("2024-01-02", 100000.0),
            ("2024-01-03", 100500.0),
        ]

    def test_non_json_string_returns_empty_not_raises(self):
        assert parse_equity_curve_json("not-json{{{") == []

    def test_top_level_dict_returns_empty(self):
        # We expect a list; a dict at the top level is malformed.
        assert parse_equity_curve_json('{"timestamp": "x", "value": 1.0}') == []

    def test_records_missing_value_are_skipped(self):
        blob = json.dumps([
            {"timestamp": "2024-01-02", "value": 100.0},
            {"timestamp": "2024-01-03"},  # no value
            {"value": 102.0},  # no timestamp
            {"timestamp": "2024-01-04", "value": 103.0},
        ])
        assert parse_equity_curve_json(blob) == [
            ("2024-01-02", 100.0),
            ("2024-01-04", 103.0),
        ]

    def test_non_numeric_value_skipped(self):
        blob = json.dumps([
            {"timestamp": "2024-01-02", "value": "oops"},
            {"timestamp": "2024-01-03", "value": 100.0},
        ])
        assert parse_equity_curve_json(blob) == [("2024-01-03", 100.0)]


# ---------------------------------------------------------------------------
# _date_part — strip time component for daily alignment
# ---------------------------------------------------------------------------

class TestDatePart:

    @pytest.mark.parametrize("ts,expected", [
        ("2024-01-02", "2024-01-02"),
        ("2024-01-02 09:30:00", "2024-01-02"),
        ("2024-01-02T15:59:00", "2024-01-02"),
        ("2024-01-02T15:59:00.123456", "2024-01-02"),
        ("", ""),
    ])
    def test_extracts_date(self, ts, expected):
        assert _date_part(ts) == expected


# ---------------------------------------------------------------------------
# to_daily_close_series — collapse intraday to last-of-day
# ---------------------------------------------------------------------------

class TestToDailyCloseSeries:

    def test_already_daily_passes_through(self):
        curve = [("2024-01-02", 100.0), ("2024-01-03", 101.0)]
        assert to_daily_close_series(curve) == {"2024-01-02": 100.0, "2024-01-03": 101.0}

    def test_intraday_takes_last_of_day(self):
        # Three bars on 01-02 and two bars on 01-03; the last bar of each
        # day should win (close-of-day equity).
        curve = [
            ("2024-01-02 09:30:00", 100000.0),
            ("2024-01-02 12:00:00", 100200.0),
            ("2024-01-02 15:59:00", 100500.0),
            ("2024-01-03 09:30:00", 100500.0),
            ("2024-01-03 15:59:00",  99800.0),
        ]
        assert to_daily_close_series(curve) == {
            "2024-01-02": 100500.0,
            "2024-01-03":  99800.0,
        }

    def test_empty_curve(self):
        assert to_daily_close_series([]) == {}


# ---------------------------------------------------------------------------
# daily_returns — pct change keyed by destination date
# ---------------------------------------------------------------------------

class TestDailyReturns:

    def test_simple_returns(self):
        curve = {"2024-01-02": 100.0, "2024-01-03": 101.0, "2024-01-04": 99.99}
        rets = daily_returns(curve)
        assert set(rets.keys()) == {"2024-01-03", "2024-01-04"}
        assert rets["2024-01-03"] == pytest.approx(0.01)
        assert rets["2024-01-04"] == pytest.approx(-0.01, abs=1e-4)

    def test_drops_first_date(self):
        # First date has no predecessor → no return for it.
        curve = {"2024-01-02": 100.0, "2024-01-03": 101.0}
        assert "2024-01-02" not in daily_returns(curve)

    def test_skips_zero_predecessor_safely(self):
        curve = {"2024-01-02": 0.0, "2024-01-03": 100.0, "2024-01-04": 101.0}
        rets = daily_returns(curve)
        # The 01-03 return would be a div-by-zero — skipped, not raised.
        assert "2024-01-03" not in rets
        assert "2024-01-04" in rets

    def test_single_point_returns_empty(self):
        assert daily_returns({"2024-01-02": 100.0}) == {}


# ---------------------------------------------------------------------------
# _clip_to_period — keep only the last N dates
# ---------------------------------------------------------------------------

class TestClipToPeriod:

    def test_no_clip_when_period_none(self):
        rets = {f"2024-01-{i:02d}": 0.001 for i in range(2, 12)}
        assert _clip_to_period(rets, None) == rets

    def test_no_clip_when_period_zero(self):
        rets = {f"2024-01-{i:02d}": 0.001 for i in range(2, 12)}
        assert _clip_to_period(rets, 0) == rets

    def test_clip_keeps_most_recent(self):
        rets = {f"2024-01-{i:02d}": 0.001 for i in range(2, 12)}  # 10 dates
        clipped = _clip_to_period(rets, 3)
        assert len(clipped) == 3
        assert set(clipped.keys()) == {"2024-01-09", "2024-01-10", "2024-01-11"}

    def test_clip_no_op_when_period_exceeds(self):
        rets = {f"2024-01-{i:02d}": 0.001 for i in range(2, 5)}  # 3 dates
        assert _clip_to_period(rets, 10) == rets


# ---------------------------------------------------------------------------
# _pearson — internal correlation primitive
# ---------------------------------------------------------------------------

class TestPearson:

    def test_perfect_positive(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [2.0, 4.0, 6.0, 8.0, 10.0]
        assert _pearson(xs, ys) == pytest.approx(1.0)

    def test_perfect_negative(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [10.0, 8.0, 6.0, 4.0, 2.0]
        assert _pearson(xs, ys) == pytest.approx(-1.0)

    def test_uncorrelated_orthogonal(self):
        # Mean-centred, perpendicular vectors — exact zero correlation.
        xs = [1.0, -1.0, 1.0, -1.0]
        ys = [1.0, 1.0, -1.0, -1.0]
        assert _pearson(xs, ys) == pytest.approx(0.0)

    def test_constant_series_returns_zero_not_nan(self):
        xs = [1.0, 2.0, 3.0]
        ys = [5.0, 5.0, 5.0]
        # zero variance on ys → undefined; we pick 0.0 over NaN so the
        # caller doesn't have to special-case constant series.
        result = _pearson(xs, ys)
        assert result == 0.0
        assert not math.isnan(result)

    def test_mismatched_lengths_returns_zero(self):
        assert _pearson([1.0, 2.0, 3.0], [1.0, 2.0]) == 0.0

    def test_too_short_returns_zero(self):
        assert _pearson([1.0], [2.0]) == 0.0


# ---------------------------------------------------------------------------
# compute_correlation_matrix — top-level integration
# ---------------------------------------------------------------------------

class TestComputeCorrelationMatrix:

    def test_identical_curves_correlate_one(self):
        # Two identical equity curves → r = 1.0 between them.
        # Use 25 daily points so we clear the min_observations=20 floor.
        curve = _curve(*[
            (f"2024-01-{i:02d}", 100000.0 * (1.005 ** i))
            for i in range(1, 27)
        ])
        result = compute_correlation_matrix(
            [("a", curve), ("b", curve)],
        )
        assert result.matrix is not None
        assert result.matrix[0][0] == pytest.approx(1.0)
        assert result.matrix[1][1] == pytest.approx(1.0)
        assert result.matrix[0][1] == pytest.approx(1.0)
        assert result.matrix[1][0] == pytest.approx(1.0)
        assert result.reason is None

    def test_perfectly_anti_correlated_returns_minus_one(self):
        # Construct two curves whose daily returns are exact mirrors:
        # if curve A goes +1% then curve B goes -1%, etc.
        n = 25
        a_pts: list[tuple[str, float]] = []
        b_pts: list[tuple[str, float]] = []
        eq_a = 100000.0
        eq_b = 100000.0
        a_pts.append(("2024-01-01", eq_a))
        b_pts.append(("2024-01-01", eq_b))
        for i in range(2, n + 2):
            r = ((i % 7) - 3) * 0.005  # alternating, non-constant returns
            eq_a *= (1.0 + r)
            eq_b *= (1.0 - r)
            d = f"2024-01-{i:02d}"
            a_pts.append((d, eq_a))
            b_pts.append((d, eq_b))
        result = compute_correlation_matrix([
            ("a", _curve(*a_pts)),
            ("b", _curve(*b_pts)),
        ])
        assert result.matrix is not None
        assert result.matrix[0][1] == pytest.approx(-1.0, abs=1e-6)

    def test_intraday_curve_aligns_with_daily(self):
        # A 1-min curve and a 1-day curve covering the same dates should
        # collapse to a single shared trading-day index. We make their
        # daily-frequency returns identical; correlation must be 1.0.
        n = 25
        daily_pts = [("2024-01-01", 100000.0)]
        intraday_pts = [
            ("2024-01-01 09:30:00", 100000.0),
            ("2024-01-01 12:00:00", 100100.0),
            ("2024-01-01 15:59:00", 100000.0),  # closes flat
        ]
        eq = 100000.0
        for i in range(2, n + 2):
            r = 0.003 * (i % 3 - 1)
            eq *= (1.0 + r)
            d = f"2024-01-{i:02d}"
            daily_pts.append((d, eq))
            intraday_pts.append((f"{d} 09:30:00", eq * 0.999))
            intraday_pts.append((f"{d} 12:00:00", eq * 1.001))
            intraday_pts.append((f"{d} 15:59:00", eq))  # close == daily value
        result = compute_correlation_matrix([
            ("daily", _curve(*daily_pts)),
            ("intraday", _curve(*intraday_pts)),
        ])
        assert result.matrix is not None
        assert result.matrix[0][1] == pytest.approx(1.0, abs=1e-9)
        assert result.n_observations == n  # all overlapping days used

    def test_below_min_observations_returns_none_with_reason(self):
        # 5 overlapping dates; default floor is 20 → matrix is None.
        a = _curve(*[(f"2024-01-{i:02d}", 100.0 + i) for i in range(1, 7)])
        b = _curve(*[(f"2024-01-{i:02d}", 200.0 - i) for i in range(1, 7)])
        result = compute_correlation_matrix([("a", a), ("b", b)])
        assert result.matrix is None
        assert result.n_observations == 5
        assert "overlapping" in (result.reason or "")
        assert "20" in (result.reason or "")

    def test_custom_min_observations_lower_threshold(self):
        # Build two curves whose daily RETURNS (not levels) are mirror-
        # image — a +1% / b -1%, then a -2% / b +2%, etc. Levels alone
        # don't determine correlation; only the per-bar return sequence
        # does. Five returns, anti-correlated by construction.
        eq_a, eq_b = 100.0, 100.0
        a_pts = [("2024-01-01", eq_a)]
        b_pts = [("2024-01-01", eq_b)]
        for i, r in enumerate([0.01, -0.02, 0.015, -0.005, 0.008], start=2):
            eq_a *= (1.0 + r)
            eq_b *= (1.0 - r)
            d = f"2024-01-{i:02d}"
            a_pts.append((d, eq_a))
            b_pts.append((d, eq_b))
        result = compute_correlation_matrix(
            [("a", _curve(*a_pts)), ("b", _curve(*b_pts))],
            min_observations=4,
        )
        assert result.matrix is not None
        assert result.matrix[0][1] == pytest.approx(-1.0, abs=1e-6)

    def test_disjoint_dates_zero_overlap(self):
        a = _curve(*[(f"2024-01-{i:02d}", 100.0 + i) for i in range(1, 27)])
        b = _curve(*[(f"2024-02-{i:02d}", 200.0 - i) for i in range(1, 27)])
        result = compute_correlation_matrix([("a", a), ("b", b)])
        assert result.matrix is None
        # Each curve's daily_returns drops the first date, leaving 25
        # dates per curve but ZERO overlap between them.
        assert result.n_observations == 0

    def test_period_days_clips_before_intersection(self):
        # Curve A covers all of February (28 daily points → 27 returns).
        # Curve B starts later (Feb 5) so without clipping the natural
        # overlap is the 23 returns on Feb 7-Feb 28. With period_days=15,
        # both curves are clipped to their last 15 returns first; the
        # intersection then collapses to 15. We use min_observations=5
        # to keep both calls below the matrix-emit floor.
        a = _curve(*[
            (f"2024-02-{i:02d}", 100.0 * (1.001 ** i)) for i in range(1, 29)
        ])
        b = _curve(*[
            (f"2024-02-{i:02d}", 100.0 * (0.999 ** i)) for i in range(5, 29)
        ])
        unclipped = compute_correlation_matrix(
            [("a", a), ("b", b)], min_observations=5
        )
        clipped = compute_correlation_matrix(
            [("a", a), ("b", b)], period_days=15, min_observations=5
        )
        assert unclipped.matrix is not None
        assert clipped.matrix is not None
        # Without clipping → 23 overlapping returns; with clipping → 15.
        assert unclipped.n_observations == 23
        assert clipped.n_observations == 15

    def test_missing_curve_surfaces_label_in_reason(self):
        a = _curve(*[(f"2024-01-{i:02d}", 100.0 + i) for i in range(1, 27)])
        result = compute_correlation_matrix([
            ("a", a),
            ("b_missing", ""),
        ])
        assert result.matrix is None
        assert "b_missing" in (result.reason or "")
        assert result.labels == ["a", "b_missing"]

    def test_single_curve_refused(self):
        a = _curve(("2024-01-02", 100.0), ("2024-01-03", 101.0))
        result = compute_correlation_matrix([("a", a)])
        assert result.matrix is None
        assert "at least 2" in (result.reason or "")

    def test_three_curves_produces_3x3_symmetric(self):
        # Three curves with 30 dates each (overlap of 25 after returns
        # drop the first date) — well above the 20 floor.
        n = 30
        a = _curve(*[(f"2024-01-{i:02d}", 100.0 * (1.001 ** i)) for i in range(1, n + 1)])
        b = _curve(*[(f"2024-01-{i:02d}", 100.0 * (1.002 ** i)) for i in range(1, n + 1)])
        c = _curve(*[(f"2024-01-{i:02d}", 100.0 * (0.999 ** i)) for i in range(1, n + 1)])
        result = compute_correlation_matrix([("a", a), ("b", b), ("c", c)])
        assert result.matrix is not None
        m = result.matrix
        # Square 3×3
        assert len(m) == 3 and all(len(row) == 3 for row in m)
        # Diagonal == 1.0
        for i in range(3):
            assert m[i][i] == pytest.approx(1.0)
        # Symmetric
        for i in range(3):
            for j in range(3):
                assert m[i][j] == pytest.approx(m[j][i])
        assert result.labels == ["a", "b", "c"]
        assert result.n_observations is not None and result.n_observations >= 20

    def test_method_is_pearson_by_default(self):
        a = _curve(*[(f"2024-01-{i:02d}", 100.0 + i) for i in range(1, 27)])
        b = _curve(*[(f"2024-01-{i:02d}", 100.0 + i * 2) for i in range(1, 27)])
        result = compute_correlation_matrix([("a", a), ("b", b)])
        assert result.method == "pearson"


class TestCorrelationResultDataclass:
    """Smoke tests on the dataclass shape itself — kept tight since the
    matrix-building tests cover behaviour."""

    def test_unsuccessful_result_has_none_matrix_and_reason(self):
        r = CorrelationResult(
            matrix=None,
            labels=["a", "b"],
            n_observations=3,
            reason="too few observations",
        )
        assert r.matrix is None
        assert r.method == "pearson"
        assert r.reason == "too few observations"

    def test_default_min_observations_is_twenty(self):
        # Document the chosen floor as part of the public contract — any
        # change should be a deliberate API change visible in this test.
        assert DEFAULT_MIN_OBSERVATIONS == 20
