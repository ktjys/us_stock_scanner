"""백테스트 스코어링 정합성 테스트 (실전 compute_signal_v8과 동일 dropna 조건)."""

import random

import pandas as pd

from backtest import _backtest_ticker
from stock_scanner import compute_signal_v8


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


def _backtest(ticker, df, thresholds=(0,)):
    start = str(df.index[0].date())
    end = str(df.index[-1].date())
    return _backtest_ticker(ticker, df, list(thresholds), start, end)


def test_backtest_scores_match_compute_signal_v8_on_valid_data():
    df = _make_df()
    live = compute_signal_v8("TEST", df, strategy="general")
    assert live is not None
    records = _backtest("TEST", df)
    last = [r for r in records if r["date"] == str(df.index[-1].date())]
    assert len(last) == 1
    assert last[0]["ticker"] == "TEST"
    assert last[0]["price"] == live["price"]
    assert last[0]["score"] == live["score"]


def test_backtest_skips_rows_until_all_indicators_valid():
    # rsi(14)/ma20(20)/ma50(50)/high60(60)/avgvol(20) 전부 유효한 첫 행 = index 59
    df = _make_df()
    records = _backtest("TEST", df)
    assert records[0]["date"] == str(df.index[59].date())
    assert records[-1]["date"] == str(df.index[-1].date())
    assert len(records) == len(df) - 59


def _dates_after_nan(col, nan_at, n=180):
    clean = {r["date"] for r in _backtest("TEST", _make_df(n))}
    injected = _make_df(n)
    injected.loc[injected.index[nan_at], col] = float("nan")
    dates = {r["date"] for r in _backtest("TEST", injected)}
    return clean, dates


def test_backtest_skips_rows_with_nan_close():
    # Close NaN은 rsi/ma20/ma50/high60/avgvol 전부에 전파되어 끝까지 스킵된다
    clean, dates = _dates_after_nan("Close", nan_at=100)
    block = {str(d.date()) for d in pd.date_range("2026-01-01", periods=180, freq="B")[100:]}
    assert not (dates & block)
    assert dates == clean - block


def test_backtest_skips_rows_with_nan_high():
    # high60만 NaN인 60행 구간(100~159)은 스킵되고, 이후 유효 구간은 복원된다
    clean, dates = _dates_after_nan("High", nan_at=100)
    block = {str(d.date()) for d in pd.date_range("2026-01-01", periods=180, freq="B")[100:160]}
    assert not (dates & block)
    assert dates == clean - block


def test_backtest_skips_rows_with_nan_volume():
    # avgvol만 NaN인 20행 구간(100~119)은 스킵되고, 이후 유효 구간은 복원된다
    clean, dates = _dates_after_nan("Volume", nan_at=100)
    block = {str(d.date()) for d in pd.date_range("2026-01-01", periods=180, freq="B")[100:120]}
    assert not (dates & block)
    assert dates == clean - block
