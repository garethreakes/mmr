"""Pairwise correlation computation across backtest equity curves.

Each backtest persists ``equity_curve_json`` as a list of
``{"timestamp": <iso>, "value": <float>}`` records (see
``trader/mmr_cli.py`` and ``trader/simulation/backtester.py``). To rank
candidate strategies for portfolio inclusion, an LLM agent (or a human)
needs the pairwise correlation between the strategies' return streams,
not the raw curves themselves — the curves are multi-MB; the correlation
matrix is ~50 bytes per cell.

This module provides the pure computation: parse the JSON curves,
compute daily-frequency returns, intersect on the dates all runs share,
and emit a labelled Pearson correlation matrix. It deliberately has zero
DuckDB or argparse dependencies so the math can be unit-tested with
hand-crafted inputs.

Design constraints:

  - Heterogeneous bar sizes: a 1-min intraday run and a 1-day run can be
    compared by resampling the intraday equity curve to last-of-day,
    then computing daily returns. Resampling is end-of-period to avoid
    look-ahead bias.
  - Date alignment: runs cover different windows. We intersect on the
    set of dates appearing in EVERY run. If the intersection is below
    ``min_observations``, return ``None`` for the matrix and surface
    the reason — never silently emit a low-N correlation.
  - Period limiter: ``period_days`` clips each curve to its last N
    *trading* days BEFORE the intersection so a 5-year backtest doesn't
    drag a 6-month one down to 6 months and then truncate further.

Example:

    >>> labelled = [
    ...     ("orb_gld",   '[{"timestamp":"2024-01-02","value":100000},'
    ...                   ' {"timestamp":"2024-01-03","value":100500}]'),
    ...     ("orb_googl", '[{"timestamp":"2024-01-02","value":100000},'
    ...                   ' {"timestamp":"2024-01-03","value": 99800}]'),
    ... ]
    >>> result = compute_correlation_matrix(labelled)
    >>> result.matrix[0][1]  # symmetric
    -1.0
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Optional


# Minimum overlapping observations required to emit a correlation. Below
# this, Pearson correlation is too noisy to be useful (a 5-day overlap
# can produce |r| = 0.95 by chance for any pair). 20 is a defensible
# floor; 30+ is preferable but rules out short live-trading windows
# entirely.
DEFAULT_MIN_OBSERVATIONS = 20


@dataclass
class CorrelationResult:
    """Result of a pairwise correlation computation across N curves.

    Attributes:
        matrix: N×N Pearson correlation matrix (symmetric, 1.0 on the
            diagonal). ``None`` if the computation could not produce a
            valid matrix (e.g. fewer than ``min_observations`` overlapping
            dates).
        labels: Length-N list of curve labels in matrix order.
        n_observations: Number of overlapping dates used for the
            computation. ``None`` when the matrix is ``None``.
        method: Always ``"pearson"`` for now; reserved for future
            ``"spearman"`` support.
        reason: Free-text explanation when ``matrix`` is ``None`` (e.g.
            ``"only 7 overlapping dates, need ≥ 20"``). ``None`` on
            success.
    """

    matrix: Optional[list[list[float]]]
    labels: list[str]
    n_observations: Optional[int]
    method: str = "pearson"
    reason: Optional[str] = None


def parse_equity_curve_json(curve_json: str) -> list[tuple[str, float]]:
    """Parse a stored ``equity_curve_json`` blob into [(date_iso, value)].

    The blob is a JSON list of ``{"timestamp": <str>, "value": <number>}``
    records (see backtester serialization). We keep the timestamp string
    in its native form here — date-aware alignment happens later.

    Returns an empty list if the blob is empty, non-JSON, or doesn't
    match the expected shape. Never raises on parse error so a single
    malformed run doesn't kill a multi-run correlation request.
    """
    if not curve_json:
        return []
    try:
        records = json.loads(curve_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(records, list):
        return []
    out: list[tuple[str, float]] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        ts = r.get("timestamp")
        v = r.get("value")
        if ts is None or v is None:
            continue
        try:
            out.append((str(ts), float(v)))
        except (TypeError, ValueError):
            continue
    return out


def _date_part(timestamp: str) -> str:
    """Extract YYYY-MM-DD from a possibly datetime-shaped ISO string.

    Used to group intraday bars to the trading day they belong to so a
    1-min curve can be aligned with a 1-day curve. Stops at the first
    space or 'T' separator; if the string has no separator it's returned
    as-is (already a date).
    """
    if not timestamp:
        return ""
    # Common ISO forms: "2024-01-02 09:30:00", "2024-01-02T09:30:00",
    # "2024-01-02". Take everything before whitespace/'T'.
    for sep in (" ", "T"):
        idx = timestamp.find(sep)
        if idx != -1:
            return timestamp[:idx]
    return timestamp


def to_daily_close_series(curve: list[tuple[str, float]]) -> dict[str, float]:
    """Collapse a (possibly intraday) curve to last-value-per-trading-day.

    For a 1-day curve this is effectively a date→value dict already. For
    a 1-min curve, takes the last bar of each day (close) — the natural
    daily-equity equivalent that avoids look-ahead.

    Returns ``{}`` for an empty curve.
    """
    by_day: dict[str, float] = {}
    for ts, v in curve:
        day = _date_part(ts)
        if not day:
            continue
        by_day[day] = v  # later writes win → end-of-day close
    return by_day


def daily_returns(daily_curve: dict[str, float]) -> dict[str, float]:
    """Compute daily simple returns (r_t = v_t / v_{t-1} - 1) over the
    sorted series of (date, equity) pairs.

    Returns are keyed by the destination date — the day on which the
    return was *realised* (i.e. r_t is keyed by t, not t-1). The first
    date has no predecessor and is dropped.

    Skips days where the prior equity was non-positive (would yield
    division-by-zero); this is a safety net — well-formed equity
    curves never go to zero — but never raises.
    """
    days = sorted(daily_curve.keys())
    out: dict[str, float] = {}
    for i in range(1, len(days)):
        prev = daily_curve[days[i - 1]]
        cur = daily_curve[days[i]]
        if prev <= 0:
            continue
        out[days[i]] = (cur / prev) - 1.0
    return out


def _clip_to_period(
    returns_by_date: dict[str, float],
    period_days: Optional[int],
) -> dict[str, float]:
    """Keep only the most recent ``period_days`` dates.

    ``period_days <= 0`` or ``None`` means "no clipping" — keep all
    dates. The function operates on dates lexicographically (ISO sorts
    correctly), no calendar awareness needed.
    """
    if not period_days or period_days <= 0:
        return returns_by_date
    days = sorted(returns_by_date.keys())
    if len(days) <= period_days:
        return returns_by_date
    keep = set(days[-period_days:])
    return {d: returns_by_date[d] for d in keep}


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation between two equal-length numeric sequences.

    Returns 0.0 when either series has zero variance — a degenerate
    series cannot correlate, and propagating NaN here would force every
    caller to special-case it. The caller can detect "constant series"
    by seeing zeros along an entire row of the matrix.

    Implemented inline (no scipy/numpy dependency in this module) so the
    helper stays test-isolatable.
    """
    n = len(xs)
    if n < 2 or n != len(ys):
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0.0 or sy == 0.0:
        return 0.0
    return cov / (sx * sy)


def compute_correlation_matrix(
    labelled_curves: list[tuple[str, str]],
    period_days: Optional[int] = None,
    min_observations: int = DEFAULT_MIN_OBSERVATIONS,
) -> CorrelationResult:
    """Compute the pairwise Pearson correlation between equity curves.

    Args:
        labelled_curves: List of ``(label, equity_curve_json)`` pairs.
            Labels are surfaced in ``CorrelationResult.labels`` in the
            same order as the matrix rows/columns.
        period_days: Optional window cap. When set, each curve is
            clipped to its last ``period_days`` trading days BEFORE the
            inter-curve date intersection — so a 5-year run doesn't
            drag a 6-month run down. ``None`` = use full overlap.
        min_observations: Floor on overlapping dates required to emit a
            matrix. Defaults to ``DEFAULT_MIN_OBSERVATIONS`` (20).

    Returns:
        ``CorrelationResult`` with the matrix or with ``matrix=None``
        and a populated ``reason`` if the inputs aren't usable.
    """
    labels = [label for label, _ in labelled_curves]
    if len(labelled_curves) < 2:
        return CorrelationResult(
            matrix=None,
            labels=labels,
            n_observations=None,
            reason="need at least 2 curves to compute pairwise correlation",
        )

    # Step 1: parse + collapse each curve to daily returns.
    per_curve_returns: list[dict[str, float]] = []
    empty_labels: list[str] = []
    for label, curve_json in labelled_curves:
        parsed = parse_equity_curve_json(curve_json)
        daily = to_daily_close_series(parsed)
        rets = daily_returns(daily)
        rets = _clip_to_period(rets, period_days)
        if not rets:
            empty_labels.append(label)
        per_curve_returns.append(rets)

    if empty_labels:
        return CorrelationResult(
            matrix=None,
            labels=labels,
            n_observations=None,
            reason=(
                f"curves missing or unparseable: {empty_labels} — "
                "re-run those backtests with --save-trades to persist "
                "the equity curve, or omit them from the call"
            ),
        )

    # Step 2: intersect on dates appearing in every curve.
    intersection: set[str] = set(per_curve_returns[0].keys())
    for rets in per_curve_returns[1:]:
        intersection &= set(rets.keys())

    if len(intersection) < min_observations:
        return CorrelationResult(
            matrix=None,
            labels=labels,
            n_observations=len(intersection),
            reason=(
                f"only {len(intersection)} overlapping dates across the "
                f"{len(labelled_curves)} curves, need ≥ {min_observations} "
                "for a meaningful Pearson correlation"
            ),
        )

    sorted_dates = sorted(intersection)
    series: list[list[float]] = [
        [rets[d] for d in sorted_dates] for rets in per_curve_returns
    ]

    # Step 3: pairwise Pearson. Symmetric, 1.0 on the diagonal.
    n = len(series)
    matrix: list[list[float]] = [[1.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            r = _pearson(series[i], series[j])
            matrix[i][j] = r
            matrix[j][i] = r

    return CorrelationResult(
        matrix=matrix,
        labels=labels,
        n_observations=len(sorted_dates),
        method="pearson",
        reason=None,
    )
