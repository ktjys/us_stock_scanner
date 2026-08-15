"""backfill_daily 백필 스코어링 단위 테스트 (행 단위, lookahead bias 없음)."""

import random

import pandas as pd

from backfill_daily import _backfill_ticker
from stock_scanner import compute_signal

SCHEMA_KEYS = ("date", "ticker", "price", "rsi", "prev_rsi", "ma20", "ma50",
               "drawdown", "volume_ratio", "score")


def _make_df(n=100, seed=42):
    rng = random.Random(seed)
    closes = [100.0]
    for _ in range(n - 1):
        closes.append(closes[-1] * (1 + rng.uniform(-0.02, 0.02)))
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "Open": closes,
        "High": [c * 1.01 for c in closes],
        "Low": [c * 0.99 for c in closes],
        "Close": closes,
        "Volume": [1_000_000 + rng.randint(-200_000, 200_000) for _ in range(n)],
    }, index=idx)


def _backfill(ticker, df):
    start = str(df.index[0].date())
    end = str(df.index[-1].date())
    return _backfill_ticker(ticker, df, start, end)


def test_backfill_scores_match_compute_signal_on_valid_data():
    df = _make_df()
    live = compute_signal("TEST", df)
    assert live is not None
    records = _backfill("TEST", df)
    last = [r for r in records if r["date"] == str(df.index[-1].date())]
    assert len(last) == 1
    assert last[0]["ticker"] == "TEST"
    assert last[0]["price"] == live["price"]
    assert last[0]["rsi"] == live["rsi"]
    assert last[0]["prev_rsi"] == live["prev_rsi"]
    assert last[0]["ma20"] == live["ma20"]
    assert last[0]["ma50"] == live["ma50"]
    assert last[0]["drawdown"] == live["drawdown"]
    assert last[0]["volume_ratio"] == live["volume_ratio"]
    assert last[0]["score"] == live["score"]


def test_backfill_records_have_daily_data_schema():
    records = _backfill("TEST", _make_df())
    assert records
    assert all(set(r) == set(SCHEMA_KEYS) for r in records)


def test_backfill_skips_rows_until_all_indicators_valid():
    # rsi(14)/ma20(20)/ma50(50)/high60(60)/avgvol(20) 전부 유효한 첫 행 = index 59
    df = _make_df()
    records = _backfill("TEST", df)
    assert records[0]["date"] == str(df.index[59].date())
    assert records[-1]["date"] == str(df.index[-1].date())
    assert len(records) == len(df) - 59


def test_backfill_skips_rows_with_nan_high():
    # high60만 NaN인 60행 구간(100~159)은 스킵되고, 이후 유효 구간은 복원된다
    clean = {r["date"] for r in _backfill("TEST", _make_df(180))}
    injected = _make_df(180)
    injected.loc[injected.index[100], "High"] = float("nan")
    dates = {r["date"] for r in _backfill("TEST", injected)}
    block = {str(d.date()) for d in pd.date_range("2026-01-01", periods=180, freq="B")[100:160]}
    assert not (dates & block)
    assert dates == clean - block


def test_backfill_skips_rows_with_nan_volume():
    # avgvol만 NaN인 20행 구간(100~119)은 스킵되고, 이후 유효 구간은 복원된다
    clean = {r["date"] for r in _backfill("TEST", _make_df(180))}
    injected = _make_df(180)
    injected.loc[injected.index[100], "Volume"] = float("nan")
    dates = {r["date"] for r in _backfill("TEST", injected)}
    block = {str(d.date()) for d in pd.date_range("2026-01-01", periods=180, freq="B")[100:120]}
    assert not (dates & block)
    assert dates == clean - block


def test_backfill_respects_period():
    df = _make_df()
    start = str(df.index[60].date())
    end = str(df.index[-1].date())
    records = _backfill_ticker("TEST", df, start, end)
    assert records[0]["date"] == start
    assert records[-1]["date"] == end
    assert all(start <= r["date"] <= end for r in records)