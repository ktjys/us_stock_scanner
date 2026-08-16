"""backfill_daily 백필 스코어링 단위 테스트 (행 단위, lookahead bias 없음)."""

import random

import pandas as pd
import pytest

from backfill_daily import _backfill_ticker, _promote_signals
from stock_scanner import compute_signal

SCHEMA_KEYS = ("date", "ticker", "price", "rsi", "prev_rsi", "ma20", "ma50",
               "drawdown", "volume_ratio", "relative_strength_5d", "score", "score_version")


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


def _make_signal_df():
    """급락 후 반등 구간을 만들어 65점 이상 신호가 나오는 df."""
    closes = []
    c = 100.0
    for _ in range(60):  # 완만한 하락 (고점 100 유지)
        c *= 0.998
        closes.append(c)
    for _ in range(20):  # 급락 (RSI 과매도 + 고점대비 -10% 이상)
        c *= 0.99
        closes.append(c)
    for _ in range(10):  # 반등 (RSI 반등 + 거래량 증가)
        c *= 1.015
        closes.append(c)
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="B")
    vol = [1_000_000] * (len(closes) - 10) + [1_800_000] * 10
    return pd.DataFrame({
        "Open": closes,
        "High": [x * 1.01 for x in closes],
        "Low": [x * 0.99 for x in closes],
        "Close": closes,
        "Volume": vol,
    }, index=idx)


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


def test_promote_signals_only_high_score_rows():
    records = _backfill("TEST", _make_signal_df())
    signals = _promote_signals(records, 65)
    assert signals
    assert all(s["score"] >= 65 for s in signals)
    high = [r for r in records if r["score"] >= 65]
    selected_dates = {s["signal_date"] for s in signals}
    assert selected_dates
    assert selected_dates <= {r["date"] for r in high}
    # 동일 종목 5일 cooldown 내에서는 중복 신호가 없다
    selected = sorted(pd.Timestamp(d) for d in selected_dates)
    assert all((selected[i] - selected[i-1]).days > 5 for i in range(1, len(selected)))
    assert _promote_signals(records, 101) == []


def test_promote_signals_returns_match_manual_calc():
    records = _backfill("TEST", _make_signal_df())
    signals = _promote_signals(records, 65)
    assert signals
    rows = [r for r in records if r["ticker"] == "TEST"]
    for s in signals:
        after = [r for r in rows if r["date"] > s["signal_date"]]
        for n, key in ((5, "return_5d"), (10, "return_10d"), (20, "return_20d")):
            if len(after) >= n:
                expected = (after[n - 1]["price"] / s["signal_price"] - 1) * 100
                assert s[key] == pytest.approx(expected)
            else:
                assert s[key] is None


def test_promote_signals_none_when_insufficient_days():
    records = _backfill("TEST", _make_signal_df())
    signals = _promote_signals(records, 0)  # 임계값 0 = 전부 승격
    rows = [r for r in records if r["ticker"] == "TEST"]
    # 마지막 신호는 이후 거래일이 없어 수익률이 모두 None이다
    last = signals[-1]
    assert last["signal_date"] == rows[-1]["date"]
    assert last["return_5d"] is None
    assert last["return_10d"] is None
    assert last["return_20d"] is None
    # 마지막에서 두 번째는 이후 1행뿐이라 5일 수익률도 계산되지 않는다
    second = signals[-2]
    assert second["return_5d"] is None
    assert second["return_10d"] is None
    assert second["return_20d"] is None
    # 마지막에서 6번째는 이후 5행뿐이라 5일만 계산된다
    sixth = signals[-6]
    assert sixth["return_5d"] is not None
    assert sixth["return_10d"] is None
    assert sixth["return_20d"] is None